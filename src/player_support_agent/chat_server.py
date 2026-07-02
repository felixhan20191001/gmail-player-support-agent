"""Small local Web UI for the model-driven multi-project player-support agent."""

from __future__ import annotations

import argparse
import asyncio
import atexit
from dataclasses import dataclass
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .agent_runner import (
    DEFAULT_CONFIG,
    DEFAULT_OPENAI_BASE_URL,
    LIVE_CONFIRMATION,
    AgentRunConfig,
    ChatMemory,
    SupportAgentRunner,
    add_model_cli_args,
    build_run_config_from_args,
    resolve_run_config,
    run_config_has_api_key,
)
from .automation_session_summary import CycleSnapshot, build_automation_feed
from .auto_worker import run_once
from .local_model_service import LocalModelLaunchConfig, LocalModelServiceManager
from .main import build_preflight_summary
from .manual_trigger import DEFAULT_MANUAL_STORE
from .paths import default_var_dir, project_root
from .processed_message_store import (
    ProcessedMessageStore,
    new_interactive_run_id,
    summarize_interactive_run_status,
)
from .readiness_check import format_readiness_report, run_readiness_checks
from .run_trace import RunTrace
from .tools.config import ModelConfig, SupportAgentConfig, load_config
from .tools.gmail_tools import build_sender_feedback_query, normalize_sender_email


HELP_TEXT = f"""\
我可以帮你控制多项目邮件 agent。

你可以直接输入自然语言问题，例如：

- 查看所有项目未读玩家反馈
- 帮我分析 BlackHole 标签下最新一封邮件
- 查一下某个玩家说购买不到账是否可信
- 处理这几封指定邮件

每次提问都会进入当前选择的模型，模型会自行决定是否调用 Gmail、ClickHouse、规则库或草稿工具。
默认是 dry-run，不会真的打标签或创建草稿。只有包含“{LIVE_CONFIRMATION}”时才会执行正式写操作。
"""

LOCAL_BACKENDS = {"llamaserver", "ollama"}
CLOUD_BACKENDS = {"openai-compatible", "openai"}
DEFAULT_CLOUD_KEY_DIR = str(
    default_var_dir() / "player_support_agent" / "cloud_model_keys"
)
_LEGACY_CLOUD_KEY_DIR = str(default_var_dir() / "cloud_model_keys")
STARTUP_CLOUD_KEY_NAME = "startup"
_WEB_UI_LOCAL_CONFIG = project_root() / "scripts" / "web-ui.config.local.sh"
_LOCAL_CONFIG_KEYS = {
    "CLOUD_API_KEY",
    "CLOUD_MODEL",
    "CLOUD_BASE_URL",
    "LLAMA_SERVER_BIN",
    "GGUF_PATH",
    "LLAMA_HOST",
    "LLAMA_PORT",
    "LLAMA_NGL",
}
_LOCAL_CONFIG_RE = re.compile(r'^\s*([A-Z0-9_]+)\s*=\s*"(.*)"\s*$')
_CLOUD_KEY_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class BadRequest(ValueError):
    """Raised when a Web-control request is invalid."""


class CloudKeyStore:
    """Local filesystem store for named cloud model API keys."""

    def __init__(self, directory: str | Path = DEFAULT_CLOUD_KEY_DIR) -> None:
        self.directory = Path(directory)

    def _validate_name(self, name: str) -> str:
        cleaned = str(name or "").strip()
        if not _CLOUD_KEY_NAME_RE.fullmatch(cleaned):
            raise BadRequest(
                "Cloud API key name must use 1-64 letters, numbers, dots, underscores, or hyphens."
            )
        return cleaned

    def key_path(self, name: str) -> Path:
        return self.directory / f"{self._validate_name(name)}.key"

    def write_key(self, name: str, api_key: str) -> Path:
        value = str(api_key or "").strip()
        if not value:
            raise BadRequest("Cloud API key value cannot be empty.")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.key_path(name)
        path.write_text(value, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return path

    def require_existing(self, name: str) -> Path:
        path = self.key_path(name)
        if not path.exists() or not path.read_text(encoding="utf-8").strip():
            raise BadRequest(f"No saved cloud API key named: {name}")
        return path

    def list_keys(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(
            path.stem
            for path in self.directory.glob("*.key")
            if path.is_file() and _CLOUD_KEY_NAME_RE.fullmatch(path.stem)
        )


@dataclass(frozen=True)
class ServerConfig:
    run_config: AgentRunConfig


def _startup_cloud_key_path() -> Path:
    return Path(DEFAULT_CLOUD_KEY_DIR) / f"{STARTUP_CLOUD_KEY_NAME}.key"


def _parse_web_ui_local_config(path: Path | None = None) -> dict[str, str]:
    resolved = path or _WEB_UI_LOCAL_CONFIG
    if not resolved.exists():
        return {}
    values: dict[str, str] = {}
    for line in resolved.read_text(encoding="utf-8").splitlines():
        match = _LOCAL_CONFIG_RE.match(line)
        if match and match.group(1) in _LOCAL_CONFIG_KEYS:
            values[match.group(1)] = match.group(2)
    return values


def _read_startup_cloud_key_file() -> str:
    for directory in (DEFAULT_CLOUD_KEY_DIR, _LEGACY_CLOUD_KEY_DIR):
        key_path = Path(directory) / f"{STARTUP_CLOUD_KEY_NAME}.key"
        if not key_path.exists():
            continue
        value = key_path.read_text(encoding="utf-8").strip()
        if value:
            return value
    return ""


def _bootstrap_cloud_env_from_local_files() -> None:
    """Load startup cloud credentials without echoing secrets in API responses."""

    if os.getenv("OPENAI_API_KEY", "").strip():
        return
    startup_key = _read_startup_cloud_key_file()
    if startup_key:
        os.environ["OPENAI_API_KEY"] = startup_key
        return
    secrets = _parse_web_ui_local_config()
    api_key = str(secrets.get("CLOUD_API_KEY", "")).strip()
    if api_key:
        os.environ["OPENAI_API_KEY"] = api_key


def _cloud_credential_help_text() -> str:
    return (
        "云模型 API Key 未配置。请先点击左侧「云 Key」保存密钥，"
        "或用 ./scripts/start-web-ui.sh 选择云模型启动，"
        "或在 scripts/web-ui.config.local.sh 中设置 CLOUD_API_KEY。"
    )


def _validate_cloud_runner_credentials(run_config: AgentRunConfig) -> None:
    backend = run_config.backend or "llamaserver"
    if backend not in CLOUD_BACKENDS:
        return
    if run_config_has_api_key(run_config):
        return
    _bootstrap_cloud_env_from_local_files()
    if run_config_has_api_key(run_config):
        return
    raise BadRequest(_cloud_credential_help_text())


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _bool_value(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)


def _bounded_int(
    payload: dict[str, Any],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = payload.get(key, default)
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"{key} must be an integer.") from exc
    if value < minimum or value > maximum:
        raise BadRequest(f"{key} must be between {minimum} and {maximum}.")
    return value


def _normalize_cloud_model_config(model: ModelConfig) -> ModelConfig:
    """Accept legacy configs that stored the API key in api_key_env by mistake."""

    env_name = str(model.api_key_env or "").strip()
    if (
        model.backend in CLOUD_BACKENDS
        and not model.api_key
        and not model.api_key_file
        and env_name
        and env_name not in {"OPENAI_API_KEY", "SUPPORT_MODEL_API_KEY"}
        and not os.getenv(env_name)
        and ("sk-" in env_name or env_name.startswith("gsk_"))
    ):
        return model.model_copy(
            update={"api_key": env_name, "api_key_env": "OPENAI_API_KEY"},
        )
    return model


def _model_profile_run_config(
    base: AgentRunConfig,
    model: ModelConfig,
) -> AgentRunConfig:
    model = _normalize_cloud_model_config(model)
    api_key_file = _coalesce(model.api_key_file, base.api_key_file)
    if model.backend in CLOUD_BACKENDS and not api_key_file:
        startup_key = _read_startup_cloud_key_file()
        if startup_key:
            for directory in (DEFAULT_CLOUD_KEY_DIR, _LEGACY_CLOUD_KEY_DIR):
                key_path = Path(directory) / f"{STARTUP_CLOUD_KEY_NAME}.key"
                if key_path.exists() and key_path.read_text(encoding="utf-8").strip():
                    api_key_file = str(key_path)
                    break
    return AgentRunConfig(
        config_path=base.config_path,
        profile=base.profile,
        backend=model.backend,
        model=model.model,
        gguf_path=model.gguf_path,
        base_url=model.base_url,
        api_key=model.api_key,
        api_key_env=model.api_key_env,
        api_key_file=api_key_file,
        llamafile_mode=model.llamafile_mode,
        timeout_seconds=_coalesce(model.timeout_seconds, base.timeout_seconds),
        budget_tokens=_coalesce(model.budget_tokens, base.budget_tokens),
        max_iterations=_coalesce(model.max_iterations, base.max_iterations),
        allow_db_in_dry_run=base.allow_db_in_dry_run,
        default_max_iterations=base.default_max_iterations,
    )


def _model_profiles(config: SupportAgentConfig) -> dict[str, ModelConfig]:
    profiles = dict(config.model_profiles)
    default_name = "cloud" if config.model.backend in CLOUD_BACKENDS else "local"
    profiles.setdefault(default_name, config.model)
    return profiles


def _profile_slot_model(profile_name: str) -> ModelConfig:
    if profile_name == "cloud":
        return ModelConfig(
            backend="openai-compatible",
            base_url=DEFAULT_OPENAI_BASE_URL,
        )
    return ModelConfig()


def _profile_sort_key(item: tuple[str, ModelConfig]) -> tuple[int, str]:
    order = {"local": 0, "cloud": 1}
    return (order.get(item[0], 10), item[0])


def _same_model_config(left: AgentRunConfig, right: AgentRunConfig) -> bool:
    return (
        left.backend == right.backend
        and left.model == right.model
        and left.gguf_path == right.gguf_path
        and left.base_url == right.base_url
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class AutomationSettings:
    interval_seconds: int
    max_candidates: int
    max_new: int
    max_retries: int
    retry_failed: bool
    live_run: bool
    query: str | None


class AutomationScheduler:
    """Background loop that repeatedly calls the automatic Gmail worker."""

    MIN_INTERVAL_SECONDS = 60
    MAX_INTERVAL_SECONDS = 86_400

    def __init__(self, control: "ControlState") -> None:
        self.control = control
        self._lock = Lock()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self.settings: AutomationSettings | None = None
        self.running = False
        self.cycle_count = 0
        self.started_at: str | None = None
        self.last_run_at: str | None = None
        self.last_status: str | None = None
        self.last_error: str | None = None
        self.last_run_id: str | None = None
        self.session_id: str | None = None
        self.session_cycles: list[CycleSnapshot] = []

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status_unlocked()

    def _status_unlocked(self) -> dict[str, Any]:
        settings = self.settings
        return {
            "running": self.running,
            "cycle_count": self.cycle_count,
            "started_at": self.started_at,
            "last_run_at": self.last_run_at,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "last_run_id": self.last_run_id,
            "interval_seconds": settings.interval_seconds if settings else None,
            "live_run": settings.live_run if settings else False,
            "max_new": settings.max_new if settings else None,
            "processed_store_path": str(self.control.store.path),
            "session_id": self.session_id,
        }

    def feed(self, *, after_run_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            settings = self.settings
            return build_automation_feed(
                running=self.running,
                session_id=self.session_id,
                started_at=self.started_at,
                live_run=settings.live_run if settings else False,
                interval_seconds=settings.interval_seconds if settings else None,
                cycles=list(self.session_cycles),
                after_run_id=after_run_id,
                include_history=after_run_id is None,
            )

    def _append_cycle_snapshot(self, result: dict[str, Any]) -> None:
        created_at = self.last_run_at or _utc_now_iso()
        self.session_cycles.append(
            CycleSnapshot.from_run_result(result, created_at=created_at)
        )

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        live_run = _bool_value(payload.get("live"), default=False)
        if live_run and str(payload.get("confirm_live", "")).strip() != LIVE_CONFIRMATION:
            raise BadRequest(f"Live Gmail draft runs require: {LIVE_CONFIRMATION}")

        interval_seconds = _bounded_int(
            payload,
            "interval_seconds",
            default=300,
            minimum=self.MIN_INTERVAL_SECONDS,
            maximum=self.MAX_INTERVAL_SECONDS,
        )
        settings = AutomationSettings(
            interval_seconds=interval_seconds,
            max_candidates=_bounded_int(
                payload,
                "max_candidates",
                default=20,
                minimum=1,
                maximum=200,
            ),
            max_new=_bounded_int(payload, "max_new", default=1, minimum=1, maximum=20),
            max_retries=_bounded_int(
                payload,
                "max_retries",
                default=3,
                minimum=0,
                maximum=20,
            ),
            retry_failed=_bool_value(payload.get("retry_failed"), default=False),
            live_run=live_run,
            query=str(payload.get("query") or "").strip() or None,
        )

        with self._lock:
            if self.running:
                raise BadRequest("自动处理已在运行中。请先停止后再启动。")
            self.settings = settings
            self.running = True
            self.cycle_count = 0
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            self.session_id = f"auto-session-{timestamp}-{uuid.uuid4().hex[:8]}"
            self.session_cycles = []
            self.started_at = _utc_now_iso()
            self.last_run_at = None
            self.last_status = None
            self.last_error = None
            self.last_run_id = None
            self._stop_event.clear()
            self._thread = Thread(
                target=self._run_loop,
                name="player-support-automation",
                daemon=True,
            )
            self._thread.start()
            return self._status_unlocked()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self.running:
                return self._status_unlocked()
            self._stop_event.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        with self._lock:
            self.running = False
            self._thread = None
            return self._status_unlocked()

    def _run_loop(self) -> None:
        """Automation 轮巡主循环。

        当一次发现有多封新未读邮件时，会连续处理（多次调用 run_once 处理每批 max_new），
        直到本次发现的候选被处理完（select 返回 0 或 already_processed/skipped），
        然后才 sleep interval。保证“依次都处理完”。
        """
        SKIP_NO_WORK = {"skipped", "already_processed", "discovery_only"}
        while not self._stop_event.is_set():
            trigger_at = _utc_now_iso()
            try:
                result = asyncio.run(self._run_cycle())
            except Exception as exc:
                with self._lock:
                    self.last_run_at = trigger_at
                    self.last_status = "failed"
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.cycle_count += 1
                # 错误后进入 sleep，避免无限打转
            else:
                with self._lock:
                    self.last_run_at = trigger_at
                    self.last_status = str(result.get("status") or "completed")
                    self.last_run_id = str(result.get("run_id") or "") or None
                    self.last_error = str(result.get("error") or "") or None
                    self.cycle_count += 1
                    self._append_cycle_snapshot(result)

                sel_count = int(result.get("selected_count") or 0)
                cand_count = int(result.get("candidate_count") or 0)
                st = str(result.get("status") or "")
                # 即使本批是 skipped/no_content，只要本次发现有候选，就继续立即 drain
                # （配合 no_content 也会 mark_read，可快速清空 junk backlog，避免每5分钟只清1封）
                if (sel_count > 0 or cand_count > 0) and st not in {"already_processed", "discovery_only"}:
                    continue

            with self._lock:
                settings = self.settings
            if settings is None or self._stop_event.is_set():
                break
            if self._stop_event.wait(settings.interval_seconds):
                break

        with self._lock:
            self.running = False
            self._thread = None

    async def _run_cycle(self) -> dict[str, Any]:
        with self.control.execution_lock:
            with self.control.lock:
                _validate_cloud_runner_credentials(self.control.runner.run_config)
                support_config = self.control.support_config
                run_config = self.control.runner.run_config
                store = self.control.store
                settings = self.settings
            if settings is None:
                raise RuntimeError("automation settings missing")
            with self._lock:
                session_id = self.session_id
                clear_store_state = settings.live_run and self.cycle_count == 0
            return await run_once(
                support_config=support_config,
                run_config=run_config,
                store=store,
                max_candidates=settings.max_candidates,
                max_new=settings.max_new,
                retry_failed=settings.retry_failed,
                max_retries=settings.max_retries,
                live_run=settings.live_run,
                query=settings.query,
                ignore_store=False,
                discovery_only=False,
                clear_store_state=clear_store_state,
                run_source="automation",
                automation_session_id=session_id,
            )


def _safe_model_summary(config: AgentRunConfig) -> dict[str, Any]:
    backend = config.backend or "llamaserver"
    return {
        "backend": backend,
        "model": config.model,
        "gguf_path": config.gguf_path if backend in LOCAL_BACKENDS else None,
        "base_url": config.base_url,
        "timeout_seconds": config.timeout_seconds,
        "budget_tokens": config.budget_tokens,
        "max_iterations": config.max_iterations,
        "allow_db_in_dry_run": config.allow_db_in_dry_run,
        "credential_configured": (
            run_config_has_api_key(config) if backend in CLOUD_BACKENDS else False
        ),
    }


class ControlState:
    """Shared mutable state for the local Web control server."""

    def __init__(
        self,
        run_config: AgentRunConfig,
        *,
        support_config: SupportAgentConfig | None = None,
        manual_store_path: str = DEFAULT_MANUAL_STORE,
        cloud_key_dir: str = DEFAULT_CLOUD_KEY_DIR,
        local_model_manager: LocalModelServiceManager | None = None,
    ) -> None:
        _bootstrap_cloud_env_from_local_files()
        self.base_run_config = run_config
        self.support_config = support_config or load_config(run_config.config_path)
        self.manual_store_path = manual_store_path
        self.cloud_key_store = CloudKeyStore(cloud_key_dir)
        local_values = _parse_web_ui_local_config()
        fallback_gguf = (
            run_config.gguf_path
            or self.support_config.model.gguf_path
            or None
        )
        self.local_model_manager = local_model_manager or LocalModelServiceManager(
            LocalModelLaunchConfig.from_web_ui_values(
                local_values,
                fallback_gguf_path=fallback_gguf,
            )
        )
        self.active_cloud_key: str | None = None
        self.lock = Lock()
        self.execution_lock = Lock()
        self.dynamic_profiles: dict[str, ModelConfig] = {}
        self.runner = SupportAgentRunner(run_config, support_config=self.support_config)
        self.memory = ChatMemory()
        self.store = ProcessedMessageStore(self.support_config.state.processed_store_path)
        self.automation = AutomationScheduler(self)
        self.active_profile = self._infer_active_profile(self.runner.run_config)

    def _infer_active_profile(self, resolved_run_config: AgentRunConfig) -> str:
        for name, model in sorted(
            self._configured_profiles_unlocked().items(),
            key=_profile_sort_key,
        ):
            candidate = resolve_run_config(
                _model_profile_run_config(self.base_run_config, model),
                self.support_config.model,
            )
            if _same_model_config(resolved_run_config, candidate):
                return name
        return "startup"

    def _configured_profiles_unlocked(self) -> dict[str, ModelConfig]:
        profiles = _model_profiles(self.support_config)
        profiles.update(self.dynamic_profiles)
        return profiles

    def _profiles_unlocked(self) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        configured = self._configured_profiles_unlocked()
        names = set(configured) | {"local", "cloud"}
        for name in sorted(names, key=lambda item: _profile_sort_key((item, configured.get(item, _profile_slot_model(item))))):
            model = configured.get(name) or _profile_slot_model(name)
            resolved = resolve_run_config(
                _model_profile_run_config(self.base_run_config, model),
                self.support_config.model,
            )
            profiles.append(
                {
                    "name": name,
                    "active": name == self.active_profile,
                    "configured": name in configured,
                    "model": _safe_model_summary(resolved),
                }
            )
        return profiles

    def _status_unlocked(
        self,
        *,
        local_model_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "active_profile": self.active_profile,
            "model": _safe_model_summary(self.runner.run_config),
            "profiles": self._profiles_unlocked(),
            "cloud_keys": [
                {"name": name, "active": name == self.active_cloud_key}
                for name in self.cloud_key_store.list_keys()
            ],
            "active_cloud_key": self.active_cloud_key,
            "live_confirmation": LIVE_CONFIRMATION,
            "manual_store_path": self.manual_store_path,
            "processed_store_path": str(self.store.path),
            "automation": self.automation.status(),
            "execution_busy": self.execution_lock.locked(),
            "local_model": local_model_status or self.local_model_manager.status(),
        }

    def status(self) -> dict[str, Any]:
        with self.lock:
            return self._status_unlocked()

    def _profile_from_ui_config(
        self,
        profile_name: str,
        profile_config: dict[str, Any],
    ) -> ModelConfig:
        if profile_config.get("api_key"):
            raise BadRequest("Use api_key_env or api_key_file instead of a plain api_key.")
        defaults: dict[str, Any] = {}
        if profile_name == "cloud":
            defaults = {
                "backend": "openai-compatible",
                "base_url": DEFAULT_OPENAI_BASE_URL,
                "api_key_env": "OPENAI_API_KEY",
            }
        model = ModelConfig.model_validate({**defaults, **profile_config})
        if model.backend in CLOUD_BACKENDS and not model.model:
            raise BadRequest("Cloud model profile requires a model name.")
        return model

    def select_model_profile(
        self,
        profile_name: str,
        *,
        profile_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        profile_name = str(profile_name or "").strip()
        if not profile_name:
            raise BadRequest("model profile is required.")
        with self.execution_lock:
            with self.lock:
                profiles = self._configured_profiles_unlocked()
                if profile_config is not None:
                    if not isinstance(profile_config, dict):
                        raise BadRequest("profile_config must be an object.")
                    profiles[profile_name] = self._profile_from_ui_config(
                        profile_name,
                        profile_config,
                    )
                    self.dynamic_profiles[profile_name] = profiles[profile_name]
                if profile_name not in profiles:
                    available = ", ".join(sorted(profiles)) or "none"
                    raise BadRequest(
                        f"Model profile is not configured: {profile_name}. Available: {available}."
                    )
                run_config = _model_profile_run_config(
                    self.base_run_config,
                    profiles[profile_name],
                )
                resolved_run_config = resolve_run_config(
                    run_config,
                    self.support_config.model,
                )
                local_model_status = None
                if resolved_run_config.backend == "llamaserver":
                    local_model_status = self.local_model_manager.start(
                        resolved_run_config
                    )
                self.runner = SupportAgentRunner(
                    run_config,
                    support_config=self.support_config,
                )
                _validate_cloud_runner_credentials(self.runner.run_config)
                self.memory = ChatMemory()
                self.active_profile = profile_name
                if profile_name != "cloud":
                    self.active_cloud_key = None
                return self._status_unlocked(local_model_status=local_model_status)

    def _local_profile_run_config_unlocked(self) -> AgentRunConfig:
        profiles = self._configured_profiles_unlocked()
        model = profiles.get("local") or _profile_slot_model("local")
        return resolve_run_config(
            _model_profile_run_config(self.base_run_config, model),
            self.support_config.model,
        )

    def start_local_model(self) -> dict[str, Any]:
        with self.execution_lock:
            with self.lock:
                run_config = self._local_profile_run_config_unlocked()
            local_model_status = self.local_model_manager.start(run_config)
            with self.lock:
                return self._status_unlocked(local_model_status=local_model_status)

    def stop_local_model(self) -> dict[str, Any]:
        with self.execution_lock:
            local_model_status = self.local_model_manager.stop()
            with self.lock:
                return self._status_unlocked(local_model_status=local_model_status)

    def recent_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.lock:
            data = self.store._read()
        runs = data.get("runs", [])
        return list(reversed(runs[-limit:]))

    def save_cloud_api_key(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise BadRequest("Cloud API key name is required.")
        api_key = str(payload.get("api_key") or "").strip()
        if api_key:
            key_path = self.cloud_key_store.write_key(name, api_key)
        else:
            key_path = self.cloud_key_store.require_existing(name)

        model = str(payload.get("model") or "").strip()
        if not model:
            raise BadRequest("Cloud model name is required.")
        base_url = str(payload.get("base_url") or DEFAULT_OPENAI_BASE_URL).strip()
        if not base_url:
            raise BadRequest("Cloud model base_url is required.")
        profile = ModelConfig(
            backend="openai-compatible",
            model=model,
            base_url=base_url,
            api_key_file=str(key_path),
            timeout_seconds=_bounded_int(
                payload,
                "timeout_seconds",
                default=900,
                minimum=1,
                maximum=3600,
            ),
            budget_tokens=_bounded_int(
                payload,
                "budget_tokens",
                default=8192,
                minimum=512,
                maximum=262144,
            ),
            max_iterations=_bounded_int(
                payload,
                "max_iterations",
                default=28,
                minimum=1,
                maximum=100,
            ),
        )
        with self.execution_lock:
            with self.lock:
                self.dynamic_profiles["cloud"] = profile
                self.runner = SupportAgentRunner(
                    _model_profile_run_config(self.base_run_config, profile),
                    support_config=self.support_config,
                )
                self.memory = ChatMemory()
                self.active_profile = "cloud"
                self.active_cloud_key = self.cloud_key_store._validate_name(name)
                return self._status_unlocked()


def resolve_manual_discovery_query(
    support_config: SupportAgentConfig,
    payload: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Resolve Gmail discovery query and optional normalized sender email."""

    sender_raw = str(payload.get("sender_email") or "").strip()
    explicit_query = str(payload.get("query") or "").strip() or None
    if not sender_raw:
        return explicit_query, None
    try:
        sender = normalize_sender_email(sender_raw)
    except ValueError as exc:
        raise BadRequest("发件人邮箱格式无效，请输入例如 user@example.com") from exc
    base = explicit_query or support_config.gmail.feedback_query
    return build_sender_feedback_query(base, sender), sender


async def run_readiness(control: ControlState, payload: dict[str, Any]) -> dict[str, Any]:
    include_discovery = _bool_value(payload.get("include_discovery"), default=False)
    with control.execution_lock:
        with control.lock:
            support_config = control.support_config
            run_config = control.runner.run_config
        report = await run_readiness_checks(
            support_config,
            gguf_path=run_config.gguf_path or "",
            base_url=run_config.base_url or "",
            model_backend=run_config.backend or "llamaserver",
            cloud_api_key_configured=run_config_has_api_key(run_config),
            include_discovery=include_discovery,
        )
    return {
        "report": report,
        "text": format_readiness_report(report),
    }


async def run_manual_trigger(
    control: ControlState,
    payload: dict[str, Any],
    *,
    stream_emit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    with control.lock:
        _validate_cloud_runner_credentials(control.runner.run_config)
        support_config = control.support_config
    live_run = _bool_value(payload.get("live"), default=False)
    if live_run and str(payload.get("confirm_live", "")).strip() != LIVE_CONFIRMATION:
        raise BadRequest(f"Live Gmail draft runs require: {LIVE_CONFIRMATION}")

    query, sender_email = resolve_manual_discovery_query(support_config, payload)

    max_candidates = _bounded_int(
        payload,
        "max_candidates",
        default=20,
        minimum=1,
        maximum=200,
    )
    max_new = _bounded_int(payload, "max_new", default=1, minimum=1, maximum=20)
    max_retries = _bounded_int(
        payload,
        "max_retries",
        default=3,
        minimum=0,
        maximum=20,
    )
    retry_failed = _bool_value(payload.get("retry_failed"), default=False)
    ignore_store = _bool_value(payload.get("ignore_store"), default=False)
    discovery_only = _bool_value(payload.get("discovery_only"), default=False)
    use_config_store = _bool_value(payload.get("use_config_store"), default=False)
    statuses: list[str] = []
    run_id = new_interactive_run_id()
    trace = RunTrace(run_id=run_id)
    trace.record(
        "manual_start",
        live_run=live_run,
        max_new=max_new,
        max_candidates=max_candidates,
        ignore_store=ignore_store,
        use_config_store=use_config_store,
        sender_email=sender_email,
        query=query,
    )

    def emit_status(status: str) -> None:
        trace.status(status)
        statuses.append(status)
        if stream_emit is not None:
            stream_emit({"type": "status", "message": status})

    if stream_emit is not None:
        mode = "发现邮件" if discovery_only else "Live 草稿" if live_run else "Dry-run"
        sender_hint = f"，发件人={sender_email}" if sender_email else ""
        stream_emit(
            {
                "type": "status",
                "message": (
                    f"[开始] 手动{mode}{sender_hint}："
                    f"max_new={max_new}，max_candidates={max_candidates}"
                ),
            }
        )

    with control.execution_lock:
        with control.lock:
            support_config = control.support_config
            run_config = control.runner.run_config
            _validate_cloud_runner_credentials(run_config)
            store = (
                control.store
                if use_config_store
                else ProcessedMessageStore(control.manual_store_path)
            )
        try:
            result = await run_once(
                support_config=support_config,
                run_config=run_config,
                store=store,
                max_candidates=max_candidates,
                max_new=max_new,
                retry_failed=retry_failed,
                max_retries=max_retries,
                live_run=live_run,
                query=query,
                ignore_store=ignore_store,
                discovery_only=discovery_only,
                status_sink=emit_status,
                run_trace=trace,
            )
        except Exception as exc:
            trace.result(
                status="failed",
                live_run=live_run,
                error_message=f"{type(exc).__name__}: {exc}",
            )
            raise
    trace.result(
        status=str(result.get("status") or "completed"),
        live_run=live_run,
        answer=result.get("message"),
        case_states=result.get("outcomes"),
    )
    response = {
        "result": result,
        "statuses": statuses,
        "store_path": str(store.path),
        "model": _safe_model_summary(run_config),
        "trace_log": str(trace.log_path),
    }
    if stream_emit is not None:
        stream_emit({"type": "done", **response})
    return response


def start_automation(control: ControlState, payload: dict[str, Any]) -> dict[str, Any]:
    with control.lock:
        _validate_cloud_runner_credentials(control.runner.run_config)
    return control.automation.start(payload)


def stop_automation(control: ControlState) -> dict[str, Any]:
    return control.automation.stop()


def start_local_model(control: ControlState) -> dict[str, Any]:
    return control.start_local_model()


def stop_local_model(control: ControlState) -> dict[str, Any]:
    return control.stop_local_model()


def automation_feed(
    control: ControlState,
    *,
    after_run_id: str | None = None,
) -> dict[str, Any]:
    return control.automation.feed(after_run_id=after_run_id)


PAGE = """\
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Player Support Forge Control</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f6f3;
      --panel: #fff;
      --line: #d9d7d0;
      --text: #222;
      --muted: #6b6860;
      --primary: #1f2933;
      --accent: #0f5f8d;
      --good: #17633a;
      --warn: #8a4b00;
      --bad: #9f1d20;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 20px;
    }
    header {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 14px;
    }
    h1 {
      font-size: 22px;
      margin: 0 0 6px;
    }
    .subtle {
      color: var(--muted);
      font-size: 13px;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(280px, 360px) minmax(0, 1fr);
      gap: 14px;
      align-items: start;
    }
    section, #log {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    section {
      padding: 14px;
      margin-bottom: 12px;
    }
    h2 {
      font-size: 15px;
      margin: 0 0 10px;
    }
    .field {
      margin-bottom: 10px;
    }
    .field label {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 4px;
    }
    input, textarea {
      width: 100%;
      font: inherit;
      border: 1px solid #bbb8ad;
      border-radius: 8px;
      padding: 8px 9px;
      background: #fff;
      color: var(--text);
    }
    textarea {
      min-height: 58px;
      resize: vertical;
    }
    .grid2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }
    .checks {
      display: grid;
      gap: 7px;
      margin: 8px 0 2px;
    }
    .checks label {
      display: flex;
      gap: 8px;
      align-items: center;
      color: var(--text);
      font-size: 13px;
    }
    .checks input {
      width: auto;
    }
    .button-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 8px;
    }
    button {
      min-height: 34px;
      border: 0;
      border-radius: 8px;
      background: var(--primary);
      color: #fff;
      font: inherit;
      padding: 7px 11px;
      cursor: pointer;
    }
    button.secondary {
      background: #e8e5dc;
      color: #222;
      border: 1px solid #cbc7ba;
    }
    button.warn {
      background: #8a4b00;
    }
    button:disabled {
      opacity: 0.52;
      cursor: not-allowed;
    }
    body.busy {
      cursor: wait;
    }
    button[data-tooltip] {
      position: relative;
    }
    button[data-tooltip]:hover::after,
    button[data-tooltip]:focus-visible::after {
      content: attr(data-tooltip);
      position: absolute;
      left: 50%;
      bottom: calc(100% + 9px);
      transform: translateX(-50%);
      z-index: 5;
      width: max-content;
      max-width: 280px;
      padding: 7px 9px;
      border-radius: 8px;
      background: #1f2933;
      color: #fff;
      box-shadow: 0 8px 22px rgba(0, 0, 0, 0.18);
      font-size: 12px;
      line-height: 1.4;
      text-align: left;
      white-space: normal;
    }
    button[data-tooltip]:hover::before,
    button[data-tooltip]:focus-visible::before {
      content: "";
      position: absolute;
      left: 50%;
      bottom: calc(100% + 3px);
      transform: translateX(-50%);
      border: 6px solid transparent;
      border-top-color: #1f2933;
      z-index: 6;
    }
    .chip {
      display: inline-flex;
      align-items: center;
      max-width: 100%;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 9px;
      background: #fff;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .chip strong {
      color: var(--text);
      margin-right: 4px;
    }
    #log {
      min-height: 58vh;
      padding: 12px;
      overflow: auto;
      white-space: pre-wrap;
    }
    .msg {
      padding: 10px 8px;
      border-bottom: 1px solid #eee;
    }
    .msg:last-child {
      border-bottom: 0;
    }
    .msg.progress {
      background: #f3f1ea;
      border-bottom-style: dashed;
      color: var(--muted);
      font-size: 13px;
      position: sticky;
      bottom: 0;
      z-index: 1;
    }
    #automation-session-banner {
      display: none;
      position: sticky;
      top: 0;
      z-index: 2;
      margin: 0 0 8px;
      padding: 10px 12px;
      border: 1px solid #d5e5f2;
      border-radius: 10px;
      background: #eef6fc;
      color: #17445f;
      font-size: 13px;
      line-height: 1.45;
      white-space: pre-wrap;
    }
    #automation-session-banner.visible {
      display: block;
    }
    .user {
      color: var(--accent);
      font-weight: 700;
    }
    .assistant {
      color: var(--good);
      font-weight: 700;
    }
    .error {
      color: var(--bad);
      font-weight: 700;
    }
    .modal-backdrop {
      align-items: center;
      background: rgba(31, 41, 51, 0.28);
      bottom: 0;
      display: flex;
      justify-content: center;
      left: 0;
      padding: 18px;
      position: fixed;
      right: 0;
      top: 0;
      z-index: 20;
    }
    .modal-backdrop.hidden {
      display: none;
    }
    .modal {
      background: #fff;
      border-radius: 8px;
      box-shadow: 0 18px 55px rgba(0, 0, 0, 0.24);
      max-width: 480px;
      padding: 16px;
      width: 100%;
    }
    .modal p {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      margin: 0 0 12px;
    }
    form {
      display: flex;
      gap: 10px;
      margin-top: 10px;
    }
    form button {
      width: 104px;
      flex: 0 0 auto;
    }
    pre {
      max-height: 240px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fbfaf7;
      white-space: pre-wrap;
      font-size: 12px;
    }
    .run-list {
      display: grid;
      gap: 8px;
      max-height: 280px;
      overflow: auto;
    }
    .run-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfaf7;
      padding: 9px;
    }
    .run-head {
      align-items: center;
      display: flex;
      gap: 6px;
      justify-content: space-between;
      margin-bottom: 6px;
    }
    .run-title {
      font-weight: 700;
    }
    .run-status {
      border-radius: 999px;
      background: #e8e5dc;
      color: var(--muted);
      font-size: 11px;
      padding: 2px 7px;
      white-space: nowrap;
    }
    .run-status.completed {
      background: #e8f3ec;
      color: var(--good);
    }
    .run-status.failed {
      background: #f7e7e7;
      color: var(--bad);
    }
    .run-line {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .run-empty {
      color: var(--muted);
      font-size: 13px;
      padding: 8px 0;
    }
    @media (max-width: 820px) {
      main { padding: 14px; }
      header, form { flex-direction: column; }
      form button { width: 100%; }
      .layout { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Player Support Forge Control</h1>
        <div class="subtle">所有业务判断仍由模型通过 Forge tools 完成；这里是控制台，不是业务规则入口。</div>
      </div>
      <div id="model-chip" class="chip">模型状态加载中</div>
    </header>

    <div class="layout">
      <aside>
        <section>
          <h2>模型</h2>
          <div id="model-detail" class="subtle">加载中...</div>
          <div id="local-model-detail" class="subtle">本地模型服务：加载中...</div>
          <div class="button-row">
            <button class="secondary" data-profile="local">本地模型</button>
            <button class="secondary" data-profile="cloud">云模型</button>
            <button class="secondary" id="cloud-key" type="button" data-tooltip="保存或切换云模型 API key。密钥只写入本机 var 目录，页面不会回显。">云 Key</button>
            <button class="secondary" id="local-model-start" type="button" data-tooltip="按本机配置启动 llama-server，但不自动处理邮件。">启动本地服务</button>
            <button class="secondary" id="local-model-stop" type="button" data-tooltip="只关闭这个 WebUI 会话启动的 llama-server，不会强杀外部进程。">关闭本地模型</button>
            <button class="secondary" id="refresh-status" type="button">刷新</button>
          </div>
        </section>

        <section>
          <h2>运行前检查</h2>
          <div class="button-row">
            <button class="secondary" id="preflight" type="button" data-tooltip="只读取本地配置并输出安全摘要，不连接模型或 Gmail。">Preflight</button>
            <button class="secondary" id="readiness" type="button" data-tooltip="检查模型服务、Gmail 凭据、ClickHouse 和状态目录是否可用。">Readiness</button>
            <button class="secondary" id="readiness-discovery" type="button" data-tooltip="在 Readiness 基础上额外扫描 Gmail 未读项目候选，不调用模型。">Readiness + Gmail</button>
          </div>
        </section>

        <section>
          <h2>Gmail 试跑</h2>
          <div class="grid2">
            <div class="field">
              <label for="max-candidates">候选数</label>
              <input id="max-candidates" type="number" min="1" max="200" value="20" />
            </div>
            <div class="field">
              <label for="max-new">处理数</label>
              <input id="max-new" type="number" min="1" max="20" value="1" />
            </div>
            <div class="field">
              <label for="max-retries">最大重试</label>
              <input id="max-retries" type="number" min="0" max="20" value="3" />
            </div>
            <div class="field">
              <label for="manual-query">Gmail query</label>
              <input id="manual-query" placeholder='可选，例如 label:"BlackHole"' />
            </div>
            <div class="field">
              <label for="sender-email">发件人邮箱</label>
              <input id="sender-email" type="email" placeholder="player@example.com" />
            </div>
          </div>
          <div class="checks">
            <label><input id="retry-failed" type="checkbox" />重试失败邮件</label>
            <label><input id="ignore-store" type="checkbox" />忽略测试状态重跑</label>
            <label><input id="use-config-store" type="checkbox" />使用正式 processed store</label>
          </div>
          <div class="button-row">
            <button class="secondary" id="manual-discovery" type="button">只发现邮件</button>
            <button id="manual-dry" type="button">Dry-run 处理</button>
            <button class="warn" id="manual-live" type="button">Live 草稿</button>
            <button class="warn" id="manual-sender-live" type="button">按发件人正式处理</button>
          </div>
        </section>

        <section>
          <h2>自动处理</h2>
          <div class="subtle">按固定间隔循环运行正式 worker，始终使用上方参数和 config 中的 processed store。</div>
          <div class="field">
            <label for="auto-interval">轮询间隔（秒）</label>
            <input id="auto-interval" type="number" min="60" max="86400" value="300" />
          </div>
          <div id="auto-status" class="subtle">状态：已停止</div>
          <div class="button-row">
            <button id="auto-start-dry" type="button">启动 Dry-run</button>
            <button class="warn" id="auto-start-live" type="button">启动 Live 草稿</button>
            <button class="secondary" id="auto-stop" type="button" disabled>停止自动处理</button>
          </div>
        </section>

        <section>
          <h2>快捷对话</h2>
          <div class="button-row">
            <button class="secondary quick" type="button" data-prompt="目前所有未读邮件有哪些">未读概况</button>
            <button class="secondary quick" type="button" data-prompt="总结每个未读邮件里用户表达的主题">主题总结</button>
            <button class="secondary quick" type="button" data-prompt="查看所有项目未读玩家反馈">扫描项目</button>
            <button class="secondary" id="quick-live-one" type="button">正式处理 1 封</button>
          </div>
        </section>

        <section>
          <h2>最近运行</h2>
          <div class="button-row">
            <button class="secondary" id="refresh-runs" type="button">刷新运行记录</button>
          </div>
          <div id="runs" class="run-list"><div class="run-empty">暂无数据</div></div>
        </section>
      </aside>

      <section>
        <h2>对话与输出</h2>
        <div id="log"><div class="msg"><span class="assistant">Forge:</span> __HELP_TEXT__</div></div>
        <form id="form">
          <textarea id="input" placeholder="例如：查看所有项目未读玩家反馈"></textarea>
          <button id="send" type="submit">发送</button>
        </form>
      </section>
    </div>

    <div id="cloud-key-modal" class="modal-backdrop hidden">
      <div class="modal">
        <h2>配置云模型 API Key</h2>
        <p>输入一个 key 名称和密钥值会保存到本机私有文件；如果只填名称、不填密钥值，则切换到已保存的同名 key。</p>
        <div class="field">
          <label for="cloud-key-name">Key 名称</label>
          <input id="cloud-key-name" autocomplete="off" placeholder="primary" />
        </div>
        <div class="field">
          <label for="cloud-key-model">云模型名称</label>
          <input id="cloud-key-model" autocomplete="off" placeholder="your-cloud-model" />
        </div>
        <div class="field">
          <label for="cloud-key-base-url">Base URL</label>
          <input id="cloud-key-base-url" autocomplete="off" value="https://api.openai.com/v1" />
        </div>
        <div class="field">
          <label for="cloud-key-value">API key（留空表示使用已保存的同名 key）</label>
          <input id="cloud-key-value" autocomplete="new-password" type="password" placeholder="只在保存时填写" />
        </div>
        <div class="button-row">
          <button id="cloud-key-save" type="button">保存并切换</button>
          <button class="secondary" id="cloud-key-cancel" type="button">取消</button>
        </div>
      </div>
    </div>
  </main>
  <script>
    const log = document.getElementById('log');
    const form = document.getElementById('form');
    const input = document.getElementById('input');
    const send = document.getElementById('send');
    const modelChip = document.getElementById('model-chip');
    const modelDetail = document.getElementById('model-detail');
    const localModelDetail = document.getElementById('local-model-detail');
    const runs = document.getElementById('runs');
    const cloudKeyModal = document.getElementById('cloud-key-modal');
    let statusCache = null;
    let chatRunning = false;
    let manualRunning = false;
    let progressLine = null;
    let automationPollTimer = null;
    let automationLastRunId = null;
    let automationSeenEventKeys = new Set();
    let automationSessionStarted = false;
    const AUTOMATION_POLL_MS = 10000;

    function add(role, text) {
      const div = document.createElement('div');
      div.className = 'msg';
      const label = role === 'user' ? '你' : role === 'error' ? '错误' : 'Forge';
      div.innerHTML = `<span class="${role}">${label}:</span> `;
      div.appendChild(document.createTextNode(text));
      log.appendChild(div);
      log.scrollTop = log.scrollHeight;
      return div;
    }

    function isProgressStatus(message) {
      const text = String(message || '').trim();
      return text.startsWith('[');
    }

    function setProgressLine(text) {
      const value = String(text || '').trim();
      if (!value) return;
      if (!progressLine) {
        progressLine = document.createElement('div');
        progressLine.className = 'msg progress';
        progressLine.innerHTML = '<span class="assistant">Forge:</span> ';
        progressLine.appendChild(document.createTextNode(value));
        log.appendChild(progressLine);
      } else {
        const label = progressLine.querySelector('.assistant');
        progressLine.replaceChildren();
        if (label) progressLine.appendChild(label);
        progressLine.appendChild(document.createTextNode(' ' + value));
      }
      log.scrollTop = log.scrollHeight;
    }

    function clearProgressLine() {
      if (progressLine) {
        progressLine.remove();
        progressLine = null;
      }
    }

    function formatManualDone(event) {
      const summary = event.result?.human_summary;
      const lines = [];
      if (summary?.text) {
        lines.push(summary.text);
      }
      if (summary?.notify_description) {
        lines.push(`通知配置：${summary.notify_description}`);
      }
      if (event.trace_log) {
        lines.push(`运行日志：${event.trace_log}`);
      }
      if (!lines.length) {
        lines.push(JSON.stringify(event.result, null, 2));
      }
      return lines.join('\\n\\n');
    }

    async function requestJson(path, payload = null) {
      const options = payload === null ? {} : {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      };
      const resp = await fetch(path, options);
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.error || data.reply || JSON.stringify(data));
      }
      return data;
    }

    function modelName(model) {
      return model.model || model.gguf_path || model.base_url || model.backend;
    }

    function renderLocalModelStatus(localModel) {
      const info = localModel || {};
      const stateText = {
        running: '运行中',
        external: '外部运行中',
        starting: '启动中',
        stopped: '已停止',
        failed: '启动失败'
      }[info.state] || (info.state || '未知');
      const ownedText = info.owned ? 'WebUI 管理' : info.state === 'external' ? '外部服务' : '未托管';
      const pidText = info.pid ? ` | pid:${info.pid}` : '';
      const logText = info.log_path ? ` | log:${info.log_path}` : '';
      const errorText = info.last_error ? ` | 错误:${shortText(info.last_error, 96)}` : '';
      localModelDetail.textContent = `本地模型服务：${stateText} | ${ownedText} | ${info.base_url || ''}${pidText}${logText}${errorText}`;
      const startBtn = document.getElementById('local-model-start');
      const stopBtn = document.getElementById('local-model-stop');
      const busy = chatRunning || manualRunning;
      if (startBtn) {
        startBtn.disabled = busy || Boolean(info.owned && ['running', 'starting'].includes(info.state));
      }
      if (stopBtn) {
        stopBtn.disabled = busy || !Boolean(info.owned);
      }
    }

    function ensureAutomationBanner() {
      let banner = document.getElementById('automation-session-banner');
      if (!banner) {
        banner = document.createElement('div');
        banner.id = 'automation-session-banner';
        log.prepend(banner);
      }
      return banner;
    }

    function renderAutomationBanner(summaryText, visible) {
      const banner = ensureAutomationBanner();
      if (!visible || !summaryText) {
        banner.classList.remove('visible');
        banner.textContent = '';
        return;
      }
      banner.textContent = summaryText;
      banner.classList.add('visible');
    }

    function resetAutomationFeedState() {
      automationLastRunId = null;
      automationSeenEventKeys = new Set();
      automationSessionStarted = false;
    }

    function stopAutomationPolling() {
      if (automationPollTimer) {
        clearInterval(automationPollTimer);
        automationPollTimer = null;
      }
    }

    function startAutomationPolling() {
      if (automationPollTimer) return;
      pollAutomationFeed().catch((error) => add('error', error.message));
      automationPollTimer = setInterval(() => {
        pollAutomationFeed().catch((error) => add('error', error.message));
      }, AUTOMATION_POLL_MS);
    }

    async function pollAutomationFeed() {
      const query = automationLastRunId ? `?after_run_id=${encodeURIComponent(automationLastRunId)}` : '';
      const data = await requestJson(`/api/automation/feed${query}`);
      if (data.summary_text) {
        renderAutomationBanner(
          data.summary_text,
          Boolean(data.session_id || data.summary?.cycle_count)
        );
      }
      for (const event of data.events || []) {
        const key = event.type === 'cycle_done'
          ? `cycle:${event.run_id}`
          : `${event.type}:${event.session_id || event.message || ''}`;
        if (automationSeenEventKeys.has(key)) continue;
        automationSeenEventKeys.add(key);
        if (event.type === 'session_started') {
          if (!automationSessionStarted) {
            automationSessionStarted = true;
            add('assistant', event.message);
          }
        } else if (event.type === 'cycle_done') {
          const idx = event.cycle_index || '?';
          const t = event.created_at ? formatRunTime(event.created_at) : '';
          const timePart = t ? ` (${t})` : '';
          const body = (event.text || '').replace(/^\\[自动\\] 第 \\d+ 轮(\\s*\\([^)]*\\))?\\n?/, '') || event.headline || event.status || '';
          const displayText = `[自动] 第 ${idx} 轮${timePart}\n${body}`.trim();
          add('assistant', displayText);
          automationLastRunId = event.run_id || automationLastRunId;
        }
      }
      if (!data.running) {
        stopAutomationPolling();
      }
    }

    function renderAutomationStatus(automation) {
      const statusEl = document.getElementById('auto-status');
      const startDry = document.getElementById('auto-start-dry');
      const startLive = document.getElementById('auto-start-live');
      const stopBtn = document.getElementById('auto-stop');
      const info = automation || {};
      if (info.running) {
        const liveText = info.live_run ? 'Live 草稿' : 'Dry-run';
        const lastRun = info.last_run_at ? ` · 上次 ${formatRunTime(info.last_run_at)}` : '';
        const lastStatus = info.last_status ? ` · ${info.last_status}` : '';
        statusEl.textContent = `状态：运行中（${liveText}，间隔 ${info.interval_seconds}s，已跑 ${info.cycle_count || 0} 轮${lastRun}${lastStatus}）`;
        startDry.disabled = true;
        startLive.disabled = true;
        stopBtn.disabled = false;
        startAutomationPolling();
      } else {
        const err = info.last_error ? ` · 最近错误：${shortText(info.last_error, 72)}` : '';
        statusEl.textContent = `状态：已停止${err}`;
        startDry.disabled = false;
        startLive.disabled = false;
        stopBtn.disabled = true;
        stopAutomationPolling();
        if (info.session_id) {
          pollAutomationFeed().catch(() => {});
        }
      }
    }

    function renderStatus(data) {
      statusCache = data;
      modelChip.innerHTML = `<strong>${data.active_profile}</strong>${data.model.backend}`;
      const keyText = data.active_cloud_key ? ` | key:${data.active_cloud_key}` : '';
      modelDetail.textContent = `${data.model.backend} | ${modelName(data.model)} | ${data.model.base_url || ''}${keyText}`;
      renderLocalModelStatus(data.local_model);
      renderAutomationStatus(data.automation);
      document.querySelectorAll('[data-profile]').forEach((button) => {
        const name = button.dataset.profile;
        const profile = (data.profiles || []).find((item) => item.name === name);
        button.disabled = false;
        const suffix = profile && profile.configured === false ? '（需配置）' : '';
        const active = name === data.active_profile ? '（当前）' : '';
        const baseText = name === 'local' ? '本地模型' : name === 'cloud' ? '云模型' : name;
        button.textContent = name === data.active_profile
          ? `${baseText}${active}`
          : `${baseText}${suffix}`;
      });
    }

    async function refreshStatus() {
      renderStatus(await requestJson('/api/status'));
    }

    async function refreshRuns() {
      const data = await requestJson('/api/runs');
      renderRuns(data.runs || []);
    }

    function shortText(value, max = 92) {
      const text = String(value || '').replace(/\\s+/g, ' ').trim();
      if (!text) return '';
      return text.length > max ? text.slice(0, max - 1) + '…' : text;
    }

    function formatRunTime(value) {
      if (!value) return '';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return shortText(value, 24);
      return date.toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit'
      });
    }

    function renderRunSummary(run) {
      const payload = run.payload || {};
      const mode = payload.mode || payload.stage || 'run';
      const live = payload.live_run ? '正式' : 'dry-run';
      const title = mode === 'interactive' ? '对话' : mode === 'manual' ? '手动处理' : mode;
      const caseCount = run.case_state_count ?? payload.case_state_count ?? 0;
      const lines = [
        payload.input_preview ? `输入：${shortText(payload.input_preview)}` : '',
        payload.answer_preview ? `回答：${shortText(payload.answer_preview)}` : '',
        run.message ? `消息：${shortText(run.message)}` : '',
        run.error_message ? `错误：${shortText(run.error_message)}` : '',
        caseCount ? `case：${caseCount}` : ''
      ].filter(Boolean);
      const div = document.createElement('div');
      div.className = 'run-item';
      div.innerHTML = `
        <div class="run-head">
          <span class="run-title"></span>
          <span class="run-status"></span>
        </div>
      `;
      div.querySelector('.run-title').textContent = `${title} · ${live}`;
      const status = div.querySelector('.run-status');
      status.textContent = run.status || 'unknown';
      status.classList.add(run.status || 'unknown');
      const meta = document.createElement('div');
      meta.className = 'run-line';
      meta.textContent = `${formatRunTime(run.created_at)} · ${shortText(run.run_id, 28)}`;
      div.appendChild(meta);
      for (const line of lines.slice(0, 3)) {
        const row = document.createElement('div');
        row.className = 'run-line';
        row.textContent = line;
        div.appendChild(row);
      }
      return div;
    }

    function renderRuns(items) {
      runs.replaceChildren();
      if (!items.length) {
        const empty = document.createElement('div');
        empty.className = 'run-empty';
        empty.textContent = '暂无运行记录';
        runs.appendChild(empty);
        return;
      }
      for (const item of items.slice(0, 12)) {
        runs.appendChild(renderRunSummary(item));
      }
    }

    function manualPayload(extra) {
      return {
        max_candidates: Number(document.getElementById('max-candidates').value || 20),
        max_new: Number(document.getElementById('max-new').value || 1),
        max_retries: Number(document.getElementById('max-retries').value || 3),
        query: document.getElementById('manual-query').value.trim(),
        retry_failed: document.getElementById('retry-failed').checked,
        ignore_store: document.getElementById('ignore-store').checked,
        use_config_store: document.getElementById('use-config-store').checked,
        ...extra
      };
    }

    function automationPayload(extra) {
      return {
        interval_seconds: Number(document.getElementById('auto-interval').value || 300),
        max_candidates: Number(document.getElementById('max-candidates').value || 20),
        max_new: Number(document.getElementById('max-new').value || 1),
        max_retries: Number(document.getElementById('max-retries').value || 3),
        query: document.getElementById('manual-query').value.trim(),
        retry_failed: document.getElementById('retry-failed').checked,
        ...extra
      };
    }

    async function startAutomation(extra) {
      resetAutomationFeedState();
      renderAutomationBanner('', false);
      const data = await requestJson('/api/automation/start', automationPayload(extra));
      renderAutomationStatus(data);
      automationSessionStarted = true;
      add('assistant', `自动处理已启动：间隔 ${data.interval_seconds}s，模式 ${data.live_run ? 'Live 草稿' : 'Dry-run'}`);
      await pollAutomationFeed();
      await refreshRuns();
    }

    async function stopAutomation() {
      const data = await requestJson('/api/automation/stop', {});
      renderAutomationStatus(data);
      await pollAutomationFeed();
      add('assistant', '自动处理已停止。');
    }

    async function startLocalModelService() {
      add('user', '启动本地模型服务');
      const data = await requestJson('/api/local-model/start', {});
      renderStatus(data);
      const local = data.local_model || {};
      add('assistant', `本地模型服务：${local.state || 'unknown'} / ${local.base_url || ''}`);
    }

    async function stopLocalModelService() {
      add('user', '关闭本地模型服务');
      const data = await requestJson('/api/local-model/stop', {});
      renderStatus(data);
      const local = data.local_model || {};
      add('assistant', `本地模型服务：${local.state || 'unknown'}`);
    }

    async function requestManualStream(payload, onEvent) {
      const resp = await fetch('/api/manual-run/stream', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.error || data.reply || `HTTP ${resp.status}`);
      }
      if (!resp.body) {
        throw new Error('浏览器不支持流式响应。');
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        const lines = buffer.split('\\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          const event = parseStreamLine(line);
          if (event) onEvent(event);
        }
      }
      buffer += decoder.decode();
      const event = parseStreamLine(buffer);
      if (event) onEvent(event);
    }

    function manualActionLabel(extra) {
      if (extra.discovery_only) return '只发现邮件';
      if (extra.live) return '正式处理';
      return 'Dry-run 处理';
    }

    function setManualRunning(active) {
      manualRunning = active;
      updateInteractiveBusy();
    }

    function validateSenderEmail(email) {
      const value = String(email || '').trim();
      if (!value) return '请先填写发件人邮箱。';
      if (!/^[^\\s@<>\"']+@[^\\s@<>\"']+\\.[^\\s@<>\"']+$/.test(value)) {
        return '发件人邮箱格式无效。';
      }
      return null;
    }

    async function runManual(extra) {
      const payload = manualPayload(extra);
      const label = manualActionLabel(extra);
      let userLine = `${label}（处理 ${payload.max_new} 封，候选 ${payload.max_candidates}）`;
      if (payload.sender_email) {
        userLine = `按发件人${label}（发件人：${payload.sender_email}，处理 ${payload.max_new} 封，候选 ${payload.max_candidates}）`;
      }
      add('user', userLine);
      setManualRunning(true);
      try {
        await requestManualStream(payload, (event) => {
          if (event.type === 'status') {
            if (isProgressStatus(event.message)) {
              setProgressLine(event.message);
            } else {
              add('assistant', event.message);
            }
          } else if (event.type === 'done') {
            clearProgressLine();
            add('assistant', formatManualDone(event));
          } else if (event.type === 'error') {
            throw new Error(event.message || '手动处理失败');
          }
        });
        await refreshRuns();
      } catch (error) {
        clearProgressLine();
        add('error', '请求失败：' + error.message);
      } finally {
        clearProgressLine();
        setManualRunning(false);
        input.focus();
      }
    }

    function parseStreamLine(line) {
      const text = line.trim();
      if (!text) return null;
      try {
        return JSON.parse(text);
      } catch (error) {
        return {type: 'error', message: `无法解析流式状态：${text}`};
      }
    }

    async function requestChatStream(message, onEvent) {
      const resp = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message})
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.error || data.reply || `HTTP ${resp.status}`);
      }
      if (!resp.body) {
        throw new Error('浏览器不支持流式响应。');
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        const lines = buffer.split('\\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          const event = parseStreamLine(line);
          if (event) onEvent(event);
        }
      }
      buffer += decoder.decode();
      const event = parseStreamLine(buffer);
      if (event) onEvent(event);
    }

    function openCloudKeyModal() {
      const active = statusCache?.active_cloud_key || (statusCache?.cloud_keys || [])[0]?.name || 'primary';
      document.getElementById('cloud-key-name').value = active;
      document.getElementById('cloud-key-model').value = statusCache?.model?.model || '';
      document.getElementById('cloud-key-base-url').value = statusCache?.model?.base_url || 'https://api.openai.com/v1';
      document.getElementById('cloud-key-value').value = '';
      cloudKeyModal.classList.remove('hidden');
      document.getElementById('cloud-key-name').focus();
    }

    function closeCloudKeyModal() {
      cloudKeyModal.classList.add('hidden');
      document.getElementById('cloud-key-value').value = '';
    }

    async function saveCloudKey() {
      const payload = {
        name: document.getElementById('cloud-key-name').value.trim(),
        model: document.getElementById('cloud-key-model').value.trim(),
        base_url: document.getElementById('cloud-key-base-url').value.trim(),
        api_key: document.getElementById('cloud-key-value').value.trim()
      };
      const data = await requestJson('/api/cloud-key', payload);
      renderStatus(data);
      closeCloudKeyModal();
      add('assistant', `已切换云模型 key：${data.active_cloud_key}`);
    }

    function updateInteractiveBusy() {
      const busy = chatRunning || manualRunning;
      document.body.classList.toggle('busy', busy);
      send.disabled = busy;
      document.querySelectorAll('.quick').forEach((button) => {
        button.disabled = busy;
      });
      for (const id of ['quick-live-one', 'manual-discovery', 'manual-dry', 'manual-live', 'manual-sender-live']) {
        const button = document.getElementById(id);
        if (button) button.disabled = busy;
      }
      for (const id of ['local-model-start', 'local-model-stop']) {
        const button = document.getElementById(id);
        if (button && busy) button.disabled = true;
      }
      if (!busy && statusCache?.local_model) {
        renderLocalModelStatus(statusCache.local_model);
      }
    }

    function setChatRunning(active) {
      chatRunning = active;
      updateInteractiveBusy();
    }

    async function runChat(message, {clearInput = false} = {}) {
      const text = String(message || '').trim();
      if (!text || chatRunning) return;
      add('user', text);
      if (clearInput) input.value = '';
      setChatRunning(true);
      try {
        await requestChatStream(text, (event) => {
          if (event.type === 'status') {
            if (isProgressStatus(event.message)) {
              setProgressLine(event.message);
            } else {
              add('assistant', event.message);
            }
          } else if (event.type === 'done') {
            clearProgressLine();
            add('assistant', event.reply || '');
          } else if (event.type === 'error') {
            throw new Error(event.message || 'agent 运行失败');
          }
        });
        await refreshRuns();
      } catch (error) {
        clearProgressLine();
        add('error', '请求失败：' + error.message);
      } finally {
        clearProgressLine();
        setChatRunning(false);
        input.focus();
      }
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      await runChat(input.value, {clearInput: true});
    });

    document.querySelectorAll('[data-profile]').forEach((button) => {
      button.addEventListener('click', async () => {
        try {
          const profileName = button.dataset.profile;
          const profile = (statusCache?.profiles || []).find((item) => item.name === profileName);
          const payload = {profile: profileName};
          if (profile && profile.configured === false && profileName === 'cloud') {
            const model = prompt('输入云模型名称，例如 gpt-4.1-mini 或你的供应商模型名：');
            if (!model) return;
            const baseUrl = prompt('输入 OpenAI-compatible base URL：', profile.model.base_url || 'https://api.openai.com/v1');
            if (!baseUrl) return;
            const apiKeyEnv = prompt('输入保存 API key 的环境变量名，不要输入密钥值：', 'OPENAI_API_KEY');
            if (!apiKeyEnv) return;
            payload.profile_config = {
              model,
              base_url: baseUrl,
              api_key_env: apiKeyEnv
            };
          }
          const data = await requestJson('/api/model/select', payload);
          renderStatus(data);
          add('assistant', `已切换模型：${data.active_profile} / ${data.model.backend}`);
        } catch (error) {
          add('error', error.message);
        }
      });
    });

    document.querySelectorAll('.quick').forEach((button) => {
      button.addEventListener('click', () => {
        runChat(button.dataset.prompt);
      });
    });

    document.getElementById('quick-live-one').addEventListener('click', () => {
      const confirmation = statusCache?.live_confirmation || '__LIVE_CONFIRMATION__';
      runManual({
        discovery_only: false,
        live: true,
        confirm_live: confirmation,
        max_new: 1,
      }).catch((error) => add('error', error.message));
    });

    document.getElementById('refresh-status').addEventListener('click', () => refreshStatus().catch((error) => add('error', error.message)));
    document.getElementById('refresh-runs').addEventListener('click', () => refreshRuns().catch((error) => add('error', error.message)));
    document.getElementById('local-model-start').addEventListener('click', () => {
      startLocalModelService().catch((error) => add('error', error.message));
    });
    document.getElementById('local-model-stop').addEventListener('click', () => {
      stopLocalModelService().catch((error) => add('error', error.message));
    });
    document.getElementById('cloud-key').addEventListener('click', openCloudKeyModal);
    document.getElementById('cloud-key-cancel').addEventListener('click', closeCloudKeyModal);
    document.getElementById('cloud-key-save').addEventListener('click', () => {
      saveCloudKey().catch((error) => add('error', error.message));
    });
    cloudKeyModal.addEventListener('click', (event) => {
      if (event.target === cloudKeyModal) closeCloudKeyModal();
    });
    document.getElementById('preflight').addEventListener('click', async () => {
      try {
        const data = await requestJson('/api/preflight', {});
        add('assistant', JSON.stringify(data.summary, null, 2));
      } catch (error) {
        add('error', error.message);
      }
    });
    document.getElementById('readiness').addEventListener('click', async () => {
      try {
        const data = await requestJson('/api/readiness', {include_discovery: false});
        add('assistant', data.text);
      } catch (error) {
        add('error', error.message);
      }
    });
    document.getElementById('readiness-discovery').addEventListener('click', async () => {
      try {
        const data = await requestJson('/api/readiness', {include_discovery: true});
        add('assistant', data.text);
      } catch (error) {
        add('error', error.message);
      }
    });
    document.getElementById('manual-discovery').addEventListener('click', () => {
      runManual({discovery_only: true}).catch((error) => add('error', error.message));
    });
    document.getElementById('manual-dry').addEventListener('click', () => {
      runManual({discovery_only: false, live: false}).catch((error) => add('error', error.message));
    });
    document.getElementById('manual-live').addEventListener('click', () => {
      const confirmation = prompt(`输入确认短语以创建 Gmail 草稿：${statusCache?.live_confirmation || '__LIVE_CONFIRMATION__'}`);
      runManual({
        discovery_only: false,
        live: true,
        confirm_live: confirmation || ''
      }).catch((error) => add('error', error.message));
    });
    document.getElementById('manual-sender-live').addEventListener('click', () => {
      const sender = document.getElementById('sender-email').value.trim();
      const validationError = validateSenderEmail(sender);
      if (validationError) {
        add('error', validationError);
        return;
      }
      const confirmation = statusCache?.live_confirmation || '__LIVE_CONFIRMATION__';
      runManual({
        discovery_only: false,
        live: true,
        confirm_live: confirmation,
        sender_email: sender,
      }).catch((error) => add('error', error.message));
    });
    document.getElementById('auto-start-dry').addEventListener('click', () => {
      startAutomation({live: false}).catch((error) => add('error', error.message));
    });
    document.getElementById('auto-start-live').addEventListener('click', () => {
      const confirmation = prompt(`输入确认短语以启动 Live 自动处理：${statusCache?.live_confirmation || '__LIVE_CONFIRMATION__'}`);
      if (!confirmation) return;
      startAutomation({
        live: true,
        confirm_live: confirmation
      }).catch((error) => add('error', error.message));
    });
    document.getElementById('auto-stop').addEventListener('click', () => {
      stopAutomation().catch((error) => add('error', error.message));
    });

    refreshStatus().catch((error) => add('error', error.message));
    refreshRuns().catch(() => {});
  </script>
</body>
</html>
"""


class ChatHandler(BaseHTTPRequestHandler):
    server_version = "PlayerSupportForgeControl/0.4"

    def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_stream_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _write_stream_event(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n"
        self.wfile.write(line)
        self.wfile.flush()

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BadRequest("请求 JSON 无法解析。") from exc
        if not isinstance(payload, dict):
            raise BadRequest("请求 JSON 必须是对象。")
        return payload

    @property
    def control(self) -> ControlState:
        return self.server.control  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            body = (
                PAGE.replace("__HELP_TEXT__", html.escape(HELP_TEXT))
                .replace("__LIVE_CONFIRMATION__", LIVE_CONFIRMATION)
                .encode("utf-8")
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/status":
            self._send_json(self.control.status())
            return
        if parsed.path == "/api/runs":
            query = parse_qs(parsed.query)
            try:
                limit = int((query.get("limit") or ["20"])[0])
            except ValueError:
                limit = 20
            limit = min(max(limit, 1), 100)
            self._send_json({"runs": self.control.recent_runs(limit=limit)})
            return
        if parsed.path == "/api/automation/feed":
            query = parse_qs(parsed.query)
            after_run_id = str((query.get("after_run_id") or [""])[0]).strip() or None
            self._send_json(automation_feed(self.control, after_run_id=after_run_id))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        try:
            payload = self._read_json()
            if self.path == "/api/chat/stream":
                self._handle_chat_stream(payload)
                return
            if self.path == "/api/chat":
                self._handle_chat(payload)
                return
            if self.path == "/api/model/select":
                self._send_json(
                    self.control.select_model_profile(
                        str(payload.get("profile", "")),
                        profile_config=payload.get("profile_config"),
                    )
                )
                return
            if self.path == "/api/cloud-key":
                self._send_json(self.control.save_cloud_api_key(payload))
                return
            if self.path == "/api/local-model/start":
                self._send_json(start_local_model(self.control))
                return
            if self.path == "/api/local-model/stop":
                self._send_json(stop_local_model(self.control))
                return
            if self.path == "/api/readiness":
                self._send_json(asyncio.run(run_readiness(self.control, payload)))
                return
            if self.path == "/api/manual-run":
                self._send_json(asyncio.run(run_manual_trigger(self.control, payload)))
                return
            if self.path == "/api/manual-run/stream":
                self._handle_manual_run_stream(payload)
                return
            if self.path == "/api/automation/start":
                self._send_json(start_automation(self.control, payload))
                return
            if self.path == "/api/automation/stop":
                self._send_json(stop_automation(self.control))
                return
            if self.path == "/api/preflight":
                with self.control.lock:
                    summary = build_preflight_summary(self.control.support_config)
                self._send_json({"summary": summary})
                return
        except BadRequest as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        except Exception as exc:
            self._send_json(
                {"error": f"{type(exc).__name__}: {exc}"},
                status=500,
            )
            return
        self.send_error(404)

    def _handle_chat(self, payload: dict[str, Any]) -> None:
        message = str(payload.get("message", "")).strip()
        if not message:
            self._send_json({"reply": HELP_TEXT})
            return

        statuses: list[str] = []
        run_id = new_interactive_run_id()
        live_run = LIVE_CONFIRMATION in message

        def record(status: str, **kwargs: Any) -> None:
            with self.control.lock:
                self.control.store.record_interactive_run(
                    run_id=run_id,
                    status=status,
                    user_input=message,
                    live_run=kwargs.pop("live_run", live_run),
                    **kwargs,
                )

        record("processing")
        try:
            with self.control.execution_lock:
                with self.control.lock:
                    runner = self.control.runner
                    memory = self.control.memory
                    _validate_cloud_runner_credentials(runner.run_config)
                result = runner.run_sync(
                    message,
                    memory=memory,
                    status_sink=statuses.append,
                )
        except BadRequest as exc:
            record("failed", error_message=str(exc))
            self._send_json({"reply": str(exc)}, status=400)
            return
        except TimeoutError:
            record("failed", error_message="TimeoutError")
            self._send_json({"reply": "命令超时了。模型可能仍在推理，请稍后重试或缩小范围。"})
            return
        except Exception as exc:
            record("failed", error_message=f"{type(exc).__name__}: {exc}")
            self._send_json(
                {"reply": f"agent 运行失败：{type(exc).__name__}: {exc}"},
                status=500,
            )
            return
        record(
            summarize_interactive_run_status(result.case_states),
            live_run=result.live_run,
            answer=result.answer,
            case_states=result.case_states,
        )
        self._send_json({"reply": result.answer, "statuses": statuses})

    def _handle_manual_run_stream(self, payload: dict[str, Any]) -> None:
        self._send_stream_headers()

        def emit(event: dict[str, Any]) -> None:
            self._write_stream_event(event)

        try:
            asyncio.run(
                run_manual_trigger(self.control, payload, stream_emit=emit)
            )
        except BadRequest as exc:
            emit({"type": "error", "message": str(exc)})
        except Exception as exc:
            emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    def _handle_chat_stream(self, payload: dict[str, Any]) -> None:
        message = str(payload.get("message", "")).strip()
        self._send_stream_headers()

        def emit(event: dict[str, Any]) -> None:
            self._write_stream_event(event)

        if not message:
            emit({"type": "done", "reply": HELP_TEXT})
            return

        run_id = new_interactive_run_id()
        live_run = LIVE_CONFIRMATION in message

        def record(status: str, **kwargs: Any) -> None:
            with self.control.lock:
                self.control.store.record_interactive_run(
                    run_id=run_id,
                    status=status,
                    user_input=message,
                    live_run=kwargs.pop("live_run", live_run),
                    **kwargs,
                )

        trace = RunTrace(run_id=run_id)
        trace.record("start", user_input=message, live_run=live_run)

        def emit_status(status: str) -> None:
            trace.status(status)
            emit({"type": "status", "message": status})

        record("processing")
        try:
            with self.control.execution_lock:
                with self.control.lock:
                    runner = self.control.runner
                    memory = self.control.memory
                    _validate_cloud_runner_credentials(runner.run_config)
                result = runner.run_sync(
                    message,
                    memory=memory,
                    status_sink=emit_status,
                    run_trace=trace,
                )
        except BadRequest as exc:
            trace.result(status="failed", live_run=live_run, error_message=str(exc))
            record("failed", error_message=str(exc))
            emit({"type": "error", "message": str(exc)})
            return
        except TimeoutError:
            trace.result(status="failed", live_run=live_run, error_message="TimeoutError")
            record("failed", error_message="TimeoutError")
            emit({
                "type": "error",
                "message": "命令超时了。模型可能仍在推理，请稍后重试或缩小范围。",
            })
            return
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            trace.result(status="failed", live_run=live_run, error_message=error_message)
            record("failed", error_message=error_message)
            emit({
                "type": "error",
                "message": f"agent 运行失败：{error_message}",
            })
            return
        final_status = summarize_interactive_run_status(result.case_states)
        trace.result(
            status=final_status,
            live_run=result.live_run,
            answer=result.answer,
            case_states=result.case_states,
        )
        record(
            final_status,
            live_run=result.live_run,
            answer=result.answer,
            case_states=result.case_states,
        )
        emit({"type": "done", "reply": result.answer, "trace_log": str(trace.log_path)})

    def log_message(self, format: str, *args: Any) -> None:
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local Forge control chat UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    add_model_cli_args(parser, default_max_iterations=18)
    parser.add_argument(
        "--block-db-in-dry-run",
        action="store_true",
        help="Also simulate ClickHouse query execution during dry-run.",
    )
    return parser.parse_args()


def build_run_config(args: argparse.Namespace) -> AgentRunConfig:
    return build_run_config_from_args(
        args,
        allow_db_in_dry_run=not args.block_db_in_dry_run,
    )


def _stop_local_model_quietly(control: ControlState) -> None:
    try:
        control.local_model_manager.stop()
    except Exception:
        return


def main() -> None:
    args = parse_args()
    run_config = build_run_config(args)
    control = ControlState(run_config)
    atexit.register(_stop_local_model_quietly, control)
    server = ThreadingHTTPServer((args.host, args.port), ChatHandler)
    server.config = ServerConfig(run_config=run_config)  # type: ignore[attr-defined]
    server.control = control  # type: ignore[attr-defined]
    print(f"Forge control UI listening on http://{args.host}:{args.port}")
    print("Model backend:", control.runner.run_config.backend)
    if control.runner.run_config.backend == "llamaserver":
        local_status = control.local_model_manager.start(control.runner.run_config)
        print("Local model service:", local_status.get("state"), local_status.get("base_url"))
    try:
        server.serve_forever()
    finally:
        server.server_close()
        _stop_local_model_quietly(control)


if __name__ == "__main__":
    main()
