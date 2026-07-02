"""Lifecycle helpers for WebUI-owned local llama-server processes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
from threading import Lock
import time
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from .agent_runner import AgentRunConfig, DEFAULT_GGUF
from .paths import default_var_dir


DEFAULT_LLAMA_SERVER_BIN = "/opt/homebrew/bin/llama-server"
DEFAULT_LLAMA_HOST = "127.0.0.1"
DEFAULT_LLAMA_PORT = 8080
DEFAULT_LLAMA_NGL = "999"
DEFAULT_LLAMA_LOG_PATH = default_var_dir() / "llama-server.web-ui.log"
DEFAULT_LLAMA_PID_PATH = default_var_dir() / "llama-server.web-ui.pid"


def _safe_error(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _int_value(value: str | int | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/v1"


def _host_port_from_base_url(base_url: str | None) -> tuple[str | None, int | None]:
    if not base_url:
        return None, None
    parsed = urlparse(base_url)
    if not parsed.hostname:
        return None, None
    return parsed.hostname, parsed.port


@dataclass(frozen=True)
class LocalModelLaunchConfig:
    """Configuration needed to launch a local llama-server."""

    server_bin: str = DEFAULT_LLAMA_SERVER_BIN
    gguf_path: str = DEFAULT_GGUF
    host: str = DEFAULT_LLAMA_HOST
    port: int = DEFAULT_LLAMA_PORT
    ngl: str = DEFAULT_LLAMA_NGL
    log_path: Path = DEFAULT_LLAMA_LOG_PATH
    pid_path: Path = DEFAULT_LLAMA_PID_PATH

    @property
    def base_url(self) -> str:
        return _base_url(self.host, self.port)

    @classmethod
    def from_web_ui_values(
        cls,
        values: dict[str, str],
        *,
        fallback_gguf_path: str | None = None,
    ) -> "LocalModelLaunchConfig":
        return cls(
            server_bin=values.get("LLAMA_SERVER_BIN") or DEFAULT_LLAMA_SERVER_BIN,
            gguf_path=values.get("GGUF_PATH") or fallback_gguf_path or DEFAULT_GGUF,
            host=values.get("LLAMA_HOST") or DEFAULT_LLAMA_HOST,
            port=_int_value(values.get("LLAMA_PORT"), DEFAULT_LLAMA_PORT),
            ngl=values.get("LLAMA_NGL") or DEFAULT_LLAMA_NGL,
        )

    def for_run_config(self, run_config: AgentRunConfig | None) -> "LocalModelLaunchConfig":
        if run_config is None:
            return self
        host, port = _host_port_from_base_url(run_config.base_url)
        return LocalModelLaunchConfig(
            server_bin=self.server_bin,
            gguf_path=run_config.gguf_path or self.gguf_path,
            host=host or self.host,
            port=port or self.port,
            ngl=self.ngl,
            log_path=self.log_path,
            pid_path=self.pid_path,
        )


class LocalModelServiceManager:
    """Start and stop only the llama-server process owned by this WebUI session."""

    def __init__(
        self,
        launch_config: LocalModelLaunchConfig | None = None,
        *,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        health_checker: Callable[[str], bool] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.launch_config = launch_config or LocalModelLaunchConfig()
        self._popen_factory = popen_factory
        self._health_checker = health_checker or self._default_health_checker
        self._sleeper = sleeper
        self._lock = Lock()
        self._owned_process: Any | None = None
        self._active_config = self.launch_config
        self._last_error: str | None = None

    def _default_health_checker(self, base_url: str) -> bool:
        try:
            resp = httpx.get(base_url.rstrip("/") + "/models", timeout=5.0)
            resp.raise_for_status()
        except Exception:
            return False
        return True

    def _process_running(self) -> bool:
        return self._owned_process is not None and self._owned_process.poll() is None

    def _is_healthy(self, base_url: str) -> bool:
        try:
            return bool(self._health_checker(base_url))
        except Exception:
            return False

    def _status_unlocked(self, *, state: str | None = None) -> dict[str, Any]:
        config = self._active_config
        owned = self._process_running()
        healthy = self._is_healthy(config.base_url)
        if state is None:
            if owned and healthy:
                state = "running"
            elif owned:
                state = "starting"
            elif healthy:
                state = "external"
            elif self._last_error:
                state = "failed"
            else:
                state = "stopped"
        return {
            "state": state,
            "owned": owned,
            "pid": getattr(self._owned_process, "pid", None) if owned else None,
            "base_url": config.base_url,
            "gguf_path": config.gguf_path,
            "server_bin": config.server_bin,
            "log_path": str(config.log_path),
            "pid_path": str(config.pid_path),
            "last_error": self._last_error,
            "healthy": healthy,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status_unlocked()

    def _validate_launch_config(self, config: LocalModelLaunchConfig) -> None:
        server_bin = Path(config.server_bin).expanduser()
        if not server_bin.exists() or not os.access(server_bin, os.X_OK):
            raise FileNotFoundError(f"llama-server is not executable: {server_bin}")
        gguf = Path(config.gguf_path).expanduser()
        if not gguf.exists():
            raise FileNotFoundError(f"GGUF model file does not exist: {gguf}")

    def _launch(self, config: LocalModelLaunchConfig) -> Any:
        config.log_path.parent.mkdir(parents=True, exist_ok=True)
        config.pid_path.parent.mkdir(parents=True, exist_ok=True)
        args = [
            config.server_bin,
            "-m",
            config.gguf_path,
            "--jinja",
            "-ngl",
            str(config.ngl),
            "--host",
            config.host,
            "--port",
            str(config.port),
        ]
        with config.log_path.open("ab") as log:
            process = self._popen_factory(args, stdout=log, stderr=log)
        config.pid_path.write_text(str(getattr(process, "pid", "")), encoding="utf-8")
        return process

    def start(
        self,
        run_config: AgentRunConfig | None = None,
        *,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        interval = 1.0
        deadline = time.monotonic() + max(1, timeout_seconds)
        with self._lock:
            self._active_config = self.launch_config.for_run_config(run_config)
            config = self._active_config
            if self._process_running() and self._is_healthy(config.base_url):
                self._last_error = None
                return self._status_unlocked(state="running")
            if self._is_healthy(config.base_url):
                self._last_error = None
                return self._status_unlocked(state="external")
            try:
                self._validate_launch_config(config)
                self._owned_process = self._launch(config)
                self._last_error = None
            except Exception as exc:
                self._last_error = _safe_error(exc)
                return self._status_unlocked(state="failed")

        while time.monotonic() < deadline:
            with self._lock:
                config = self._active_config
                if self._is_healthy(config.base_url):
                    self._last_error = None
                    return self._status_unlocked(state="running")
                if (
                    self._owned_process is not None
                    and self._owned_process.poll() is not None
                ):
                    self._last_error = "llama-server exited before becoming healthy."
                    return self._status_unlocked(state="failed")
            self._sleeper(interval)

        with self._lock:
            self._last_error = (
                f"llama-server did not become healthy within {timeout_seconds} seconds."
            )
            return self._status_unlocked(state="failed")

    def stop(self) -> dict[str, Any]:
        with self._lock:
            process = self._owned_process
            if process is None or process.poll() is not None:
                self._owned_process = None
                try:
                    self._active_config.pid_path.unlink()
                except FileNotFoundError:
                    pass
                return self._status_unlocked()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            self._owned_process = None
            self._last_error = None
            try:
                self._active_config.pid_path.unlink()
            except FileNotFoundError:
                pass
            return self._status_unlocked(state="stopped")
