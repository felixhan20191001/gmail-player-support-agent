"""Shared model-driven agent runner for multi-project player support."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import json
from typing import Any

from forge import (
    ContextManager,
    LlamafileClient,
    MaxIterationsError,
    Message,
    MessageType,
    OllamaClient,
    OpenAICompatClient,
    TieredCompact,
    WorkflowRunner,
)
from forge.errors import ToolCallError

from .deepseek_client import (
    DeepSeekOpenAICompatClient,
    DeepSeekTieredCompact,
    _DeepSeekFoldPatch,
    is_deepseek_base_url,
)
from .paths import default_config_path
from .run_trace import RunTrace
from .tools.config import ModelConfig, StateConfig, SupportAgentConfig, load_config
from .tools.state_tools import StateTools
from .workflows import build_multi_project_chat_workflow, build_multi_project_workflow


DEFAULT_CONFIG = str(default_config_path())
DEFAULT_GGUF = "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf"
DEFAULT_OLLAMA_MODEL = "ministral-3:8b-instruct-2512-q4_K_M"
DEFAULT_LOCAL_BASE_URL = "http://localhost:8080/v1"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
LIVE_CONFIRMATION = "确认正式处理"
StatusSink = Callable[[str], None]
BACKEND_CHOICES = ("ollama", "llamaserver", "openai-compatible", "openai")
MAILBOX_LIST_TOOLS = {
    "list_new_feedback_emails",
    "list_unread_inbox_emails",
    "list_unread_project_emails",
}


TOOL_STATUS = {
    "list_new_feedback_emails": ("正在查询 Gmail 邮件", "Gmail 邮件查询完成"),
    "list_unread_project_emails": (
        "正在扫描各项目未读邮件",
        "各项目未读邮件扫描完成",
    ),
    "list_unread_inbox_emails": (
        "正在读取未读邮件摘要",
        "未读邮件摘要读取完成",
    ),
    "read_email_thread": ("正在读取邮件线程", "邮件线程读取完成"),
    "get_existing_gmail_labels": ("正在读取现有 Gmail 标签", "Gmail 标签读取完成"),
    "apply_existing_gmail_labels": ("正在准备打已有标签", "标签处理完成"),
    "mark_gmail_messages_read": ("正在标记邮件为已读", "邮件已标记为已读"),
    "create_gmail_draft": ("正在准备 Gmail 草稿", "草稿处理完成"),
    "get_clickhouse_schema": ("正在读取 ClickHouse 可用表结构", "ClickHouse 表结构读取完成"),
    "validate_clickhouse_sql": ("正在校验 ClickHouse SQL", "ClickHouse SQL 校验完成"),
    "query_clickhouse": ("正在查询 ClickHouse 日志", "ClickHouse 日志查询完成"),
    "summarize_behavior_logs": ("正在汇总日志证据", "日志证据汇总完成"),
    "get_support_evidence_catalog": ("正在读取证据配方", "证据配方读取完成"),
    "query_support_evidence": ("正在查询结构化证据", "结构化证据查询完成"),
    "get_remove_ads_investigation_playbook": (
        "正在读取去广告查库流程",
        "去广告查库流程读取完成",
    ),
    "get_coin_frenzy_investigation_playbook": (
        "正在读取 Coin Frenzy 查库流程",
        "Coin Frenzy 查库流程读取完成",
    ),
    "assess_remove_ads_log_evidence": (
        "正在解读去广告日志证据",
        "去广告日志证据解读完成",
    ),
    "assess_coin_frenzy_log_evidence": (
        "正在解读 Coin Frenzy 日志证据",
        "Coin Frenzy 日志证据解读完成",
    ),
    "get_support_knowledge_summary": ("正在读取知识库摘要", "知识库摘要读取完成"),
    "get_support_coverage_summary": ("正在检查知识覆盖率", "知识覆盖率检查完成"),
    "get_project_support_profile": ("正在读取项目支持画像", "项目支持画像读取完成"),
    "get_relevant_support_rules": ("正在匹配客服规则", "客服规则匹配完成"),
    "search_legacy_reply_templates": ("正在搜索历史回复模板", "历史回复模板搜索完成"),
    "get_reply_template": ("正在读取回复模板", "回复模板读取完成"),
    "extract_feedback_claim": ("正在整理邮件诉求", "邮件诉求整理完成"),
    "resolve_player_identity": ("正在检查玩家身份信息", "玩家身份检查完成"),
    "assess_claim_credibility": ("正在评估证据可信度", "证据可信度评估完成"),
    "decide_support_action": ("正在判断处理动作", "处理动作判断完成"),
    "review_reply_draft": ("正在检查回复草稿安全性", "回复草稿检查完成"),
    "create_human_handoff_summary": ("正在整理人工客服摘要", "人工客服摘要整理完成"),
    "notify_human_support": ("正在准备通知人工客服", "人工客服通知处理完成"),
    "get_case_state": ("正在读取本地 case 状态", "本地 case 状态读取完成"),
    "save_case_state": ("正在保存本地 case 状态", "本地 case 状态处理完成"),
    "write_audit_log": ("正在写入审计记录", "审计记录处理完成"),
}


def support_retry_nudge(raw_response: str) -> str:
    """Nudge local models back into the support tool workflow."""

    return (
        "Your previous response was not a valid Forge tool call. Do not answer "
        "with plain text yet. Continue the support workflow by calling exactly "
        "one valid tool. For automatic Gmail processing, after read_email_thread "
        "and get_existing_gmail_labels, the next usual tool is "
        "extract_feedback_claim. Then call get_relevant_support_rules (once), "
        "resolve_player_identity (MANDATORY, pass player_id; prerequisite for assess), "
        "assess_claim_credibility, decide_support_action, "
        "create_gmail_draft or create_human_handoff_summary, save_case_state, "
        "and finally respond. If information is missing, still call the decision "
        "and state tools with the missing fields instead of writing prose. "
        "Never restart read/extract/rules loops once past get_relevant_support_rules."
    )


@dataclass(frozen=True)
class AgentRunConfig:
    config_path: str = DEFAULT_CONFIG
    profile: str | None = None
    backend: str | None = None
    model: str | None = None
    gguf_path: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    api_key_file: str | None = None
    llamafile_mode: str | None = None
    timeout_seconds: int | None = None
    budget_tokens: int | None = None
    max_iterations: int | None = None
    allow_db_in_dry_run: bool = True
    default_max_iterations: int = 18


class CaseStatesComplete(Exception):
    """Raised internally when automatic cases have all saved state."""


@dataclass
class ChatMemory:
    turns: list[tuple[str, str]] = field(default_factory=list)
    max_turns: int = 4
    mailbox_refs: list[dict[str, str]] = field(default_factory=list)
    mailbox_source: str | None = None
    mailbox_query: str | None = None
    max_mailbox_refs: int = 10

    def render(self) -> str:
        sections: list[str] = []
        refs = self.render_mailbox_refs()
        if refs:
            sections.append(refs)
        if not self.turns:
            return "\n\n".join(sections)
        recent = self.turns[-self.max_turns :]
        lines = ["Recent conversation context:"]
        for question, answer in recent:
            lines.append(f"User: {question}")
            lines.append(f"Assistant: {answer}")
        sections.append("\n".join(lines))
        return "\n\n".join(sections)

    def render_mailbox_refs(self) -> str:
        if not self.mailbox_refs:
            return ""
        lines = [
            "Recent mailbox references:",
            (
                "Use #n for ordinal follow-ups without rescanning Gmail. "
                "Answer metadata only. Do not process the email unless asked."
            ),
        ]
        if self.mailbox_source:
            lines.append(f"source_tool={self.mailbox_source}")
        for ref in self.mailbox_refs:
            parts = [f"#{ref['index']}"]
            for key in (
                "thread_id",
                "message_id",
                "project",
                "subject",
                "from",
            ):
                value = ref.get(key)
                if value:
                    parts.append(f"{key}={value}")
            lines.append(" | ".join(parts))
        return "\n".join(lines)

    def add(self, question: str, answer: str) -> None:
        self.turns.append((question, answer))
        if len(self.turns) > self.max_turns:
            self.turns = self.turns[-self.max_turns :]

    def remember_tool_result(self, message: Message) -> None:
        if (
            message.metadata.type != MessageType.TOOL_RESULT
            or message.tool_name not in MAILBOX_LIST_TOOLS
        ):
            return
        try:
            payload = json.loads(message.content)
        except json.JSONDecodeError:
            return
        if isinstance(payload, dict) and payload.get("dry_run"):
            payload = payload.get("result") or payload.get("data") or payload
        if not isinstance(payload, dict):
            return
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return
        refs: list[dict[str, str]] = []
        for index, item in enumerate(messages[: self.max_mailbox_refs], start=1):
            if not isinstance(item, dict):
                continue
            ref = _mailbox_ref_from_item(index, item)
            if ref is not None:
                refs.append(ref)
        if refs:
            self.mailbox_refs = refs
            self.mailbox_source = message.tool_name
            query = payload.get("query")
            self.mailbox_query = str(query) if query else None


def _compact_memory_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _mailbox_ref_from_item(index: int, item: dict[str, Any]) -> dict[str, str] | None:
    ref: dict[str, str] = {"index": str(index)}
    fields = {
        "message_id": item.get("message_id") or item.get("id"),
        "thread_id": item.get("thread_id") or item.get("threadId"),
        "subject": item.get("subject"),
        "from": item.get("from"),
        "date": item.get("date"),
        "snippet": item.get("snippet"),
    }
    project = item.get("project_label")
    if not project and isinstance(item.get("project_labels"), list):
        project = ", ".join(str(value) for value in item["project_labels"] if value)
    labels = item.get("matched_labels")
    if not labels and isinstance(item.get("label_names"), list):
        labels = item["label_names"]
    if isinstance(labels, list):
        labels = ", ".join(str(value) for value in labels if value)
    fields["project"] = project
    fields["labels"] = labels

    limits = {
        "message_id": 80,
        "thread_id": 80,
        "subject": 80,
        "from": 72,
        "date": 48,
        "snippet": 80,
        "project": 60,
        "labels": 80,
    }
    for key, value in fields.items():
        compacted = _compact_memory_text(value, limit=limits[key])
        if compacted:
            ref[key] = compacted
    return ref if len(ref) > 1 else None


@dataclass(frozen=True)
class AgentRunResult:
    answer: str
    live_run: bool
    case_states: list[dict[str, Any]] = field(default_factory=list)


def add_model_cli_args(
    parser: argparse.ArgumentParser,
    *,
    default_max_iterations: int,
) -> None:
    parser.add_argument(
        "--profile",
        default=None,
        help="Named model profile from config.model_profiles, e.g. cloud.",
    )
    parser.add_argument(
        "--backend",
        choices=BACKEND_CHOICES,
        default=None,
        help="Model backend. Defaults to [model].backend in config.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name for Ollama or cloud OpenAI-compatible APIs.",
    )
    parser.add_argument("--gguf", default=None, help="GGUF path for llama-server.")
    parser.add_argument(
        "--base-url",
        default=None,
        help="Backend base URL, e.g. http://localhost:8080/v1 or https://api.openai.com/v1.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Cloud model API key. Prefer --api-key-env or config for durable use.",
    )
    parser.add_argument(
        "--api-key-env",
        default=None,
        help="Environment variable holding the cloud model API key.",
    )
    parser.add_argument(
        "--api-key-file",
        default=None,
        help="File containing the cloud model API key.",
    )
    parser.add_argument(
        "--llamafile-mode",
        choices=["native", "prompt", "auto"],
        default=None,
        help="Tool-call mode for llama-server.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=None)
    parser.add_argument("--budget-tokens", type=int, default=None)
    parser.add_argument("--max-iterations", type=int, default=None)
    parser.set_defaults(default_max_iterations=default_max_iterations)


def build_run_config_from_args(
    args: argparse.Namespace,
    *,
    allow_db_in_dry_run: bool,
) -> AgentRunConfig:
    return AgentRunConfig(
        config_path=args.config,
        profile=getattr(args, "profile", None),
        backend=getattr(args, "backend", None),
        model=getattr(args, "model", None),
        gguf_path=getattr(args, "gguf", None),
        base_url=getattr(args, "base_url", None),
        api_key=getattr(args, "api_key", None),
        api_key_env=getattr(args, "api_key_env", None),
        api_key_file=getattr(args, "api_key_file", None),
        llamafile_mode=getattr(args, "llamafile_mode", None),
        timeout_seconds=getattr(args, "timeout_seconds", None),
        budget_tokens=getattr(args, "budget_tokens", None),
        max_iterations=getattr(args, "max_iterations", None),
        allow_db_in_dry_run=allow_db_in_dry_run,
        default_max_iterations=getattr(args, "default_max_iterations", 18),
    )


def _coalesce(*values):
    for value in values:
        if value is not None:
            return value
    return None


def resolve_support_model_config(
    support_config: SupportAgentConfig,
    profile_name: str | None = None,
) -> ModelConfig:
    """Return the base model config, optionally overridden by a named profile."""

    if not profile_name:
        return support_config.model
    profiles = support_config.model_profiles
    if profile_name not in profiles:
        available = ", ".join(sorted(profiles)) or "none"
        raise ValueError(
            f"Unknown model profile: {profile_name}. Available: {available}."
        )
    return profiles[profile_name]


def resolve_run_config(
    run_config: AgentRunConfig,
    model_config: ModelConfig,
) -> AgentRunConfig:
    backend = _coalesce(run_config.backend, model_config.backend, "llamaserver")
    if backend == "openai":
        backend = "openai-compatible"
    default_base_url = (
        "http://localhost:11434"
        if backend == "ollama"
        else DEFAULT_OPENAI_BASE_URL
        if backend == "openai-compatible"
        else DEFAULT_LOCAL_BASE_URL
    )
    return AgentRunConfig(
        config_path=run_config.config_path,
        backend=backend,
        model=_coalesce(run_config.model, model_config.model),
        gguf_path=_coalesce(run_config.gguf_path, model_config.gguf_path, DEFAULT_GGUF),
        base_url=_coalesce(run_config.base_url, model_config.base_url, default_base_url),
        api_key=_coalesce(run_config.api_key, model_config.api_key),
        api_key_env=_coalesce(run_config.api_key_env, model_config.api_key_env),
        api_key_file=_coalesce(run_config.api_key_file, model_config.api_key_file),
        llamafile_mode=_coalesce(
            run_config.llamafile_mode,
            model_config.llamafile_mode,
            "prompt",
        ),
        timeout_seconds=_coalesce(
            run_config.timeout_seconds,
            model_config.timeout_seconds,
            900,
        ),
        budget_tokens=_coalesce(
            run_config.budget_tokens,
            model_config.budget_tokens,
            8192,
        ),
        max_iterations=_coalesce(
            run_config.max_iterations,
            model_config.max_iterations,
            run_config.default_max_iterations,
        ),
        allow_db_in_dry_run=run_config.allow_db_in_dry_run,
        default_max_iterations=run_config.default_max_iterations,
    )


def _api_key_from_run_config(config: AgentRunConfig) -> str:
    model_config = ModelConfig(
        api_key=config.api_key,
        api_key_env=config.api_key_env,
        api_key_file=config.api_key_file,
    )
    return model_config.resolve_api_key()


def run_config_has_api_key(config: AgentRunConfig) -> bool:
    model_config = ModelConfig(
        api_key=config.api_key,
        api_key_env=config.api_key_env,
        api_key_file=config.api_key_file,
    )
    return model_config.has_api_key()


def build_model_runtime_context(config: AgentRunConfig) -> str:
    """Return safe runtime details for model self-identification."""

    backend = config.backend or "llamaserver"
    lines = [
        "Current model runtime:",
        f"- backend: {backend}",
    ]
    if config.model:
        lines.append(f"- model: {config.model}")
    if config.base_url:
        lines.append(f"- base_url: {config.base_url}")
    if backend == "llamaserver" and config.gguf_path:
        lines.append(f"- gguf_path: {config.gguf_path}")
    lines.append(
        "If the user asks which model or backend is active, answer from this "
        "runtime context instead of inferring it from general role text."
    )
    return "\n".join(lines)


def build_client(config: AgentRunConfig):
    if config.backend == "ollama":
        return OllamaClient(
            model=config.model or DEFAULT_OLLAMA_MODEL,
            base_url=config.base_url or "http://localhost:11434",
            timeout=float(config.timeout_seconds or 900),
            recommended_sampling=True,
        )
    if config.backend == "openai-compatible":
        if not config.model:
            raise ValueError(
                "Cloud backend requires a model name via [model].model or --model"
            )
        base_url = config.base_url or DEFAULT_OPENAI_BASE_URL
        client_kwargs = {
            "api_key": _api_key_from_run_config(config),
            "timeout": float(config.timeout_seconds or 900),
        }
        if is_deepseek_base_url(base_url):
            return DeepSeekOpenAICompatClient(
                config.model,
                base_url,
                disable_thinking=True,
                **client_kwargs,
            )
        return OpenAICompatClient(
            config.model,
            base_url,
            **client_kwargs,
        )
    return LlamafileClient(
        gguf_path=config.gguf_path or DEFAULT_GGUF,
        base_url=config.base_url or DEFAULT_LOCAL_BASE_URL,
        mode=config.llamafile_mode or "prompt",
        timeout=float(config.timeout_seconds or 900),
        recommended_sampling=True,
    )


def build_user_message(
    question: str,
    *,
    live_run: bool,
    allow_db_in_dry_run: bool,
    memory: ChatMemory | None = None,
    model_context: str | None = None,
    auto_mode: bool = False,
) -> str:
    mode = (
        "LIVE: the user included the exact confirmation phrase, so Gmail/state "
        "side-effect tools are enabled."
        if live_run
        else (
            "DRY-RUN: Gmail writes, state writes, notifications, labels, and "
            "draft creation are simulated. Read-only Gmail tools are available."
        )
    )
    db_note = (
        "Read-only ClickHouse queries are allowed when you have enough scoped evidence."
        if allow_db_in_dry_run or live_run
        else "ClickHouse query execution is blocked in this dry-run."
    )
    history = memory.render() if memory else ""
    parts = [mode, db_note]
    if model_context:
        parts.append(model_context.strip())
    if history:
        parts.append(history)
    parts += ["Automatic processing task:" if auto_mode else "User question:", question]
    if auto_mode:
        parts.append(
            "Follow the support workflow tool sequence from the system prompt. "
            "The final tool call must be save_case_state for each listed message_id. "
            "Do not call respond."
        )
    else:
        parts.append(
            "Think through the request, call whatever tools you need, then answer with respond."
        )
    return "\n\n".join(parts)


def build_status_printer(status_sink: StatusSink | None = None) -> Callable[[Message], None]:
    sink = status_sink or (lambda text: print(text, flush=True))

    def _print_status(message: Message) -> None:
        message_type = message.metadata.type
        if message_type == MessageType.TOOL_CALL:
            for tool_call in message.tool_calls or []:
                if tool_call.name == "respond":
                    sink("[模型] 正在生成处理总结")
                    continue
                status = TOOL_STATUS.get(tool_call.name)
                text = status[0] if status else f"正在调用工具 {tool_call.name}"
                sink(f"[工具] {text}")
        elif message_type == MessageType.TOOL_RESULT:
            if message.tool_name == "respond":
                return
            status = TOOL_STATUS.get(message.tool_name or "")
            text = status[1] if status else f"工具 {message.tool_name or '未知'} 已返回结果"
            sink(f"[工具] {text}")
        elif message_type in {
            MessageType.RETRY_NUDGE,
            MessageType.STEP_NUDGE,
            MessageType.PREREQUISITE_NUDGE,
        }:
            sink("[模型] 正在调整工具调用")
        elif message_type == MessageType.CONTEXT_WARNING:
            sink("[模型] 正在压缩上下文")

    return _print_status


def _saved_case_ids(case_states: list[dict[str, Any]]) -> set[str]:
    return {
        str(state.get("case_id"))
        for state in case_states
        if state.get("case_id") is not None
    }


def _resolve_case_id_for_draft(
    thread_id: str | None,
    *,
    stop_after_case_ids: set[str] | None,
) -> str | None:
    if not stop_after_case_ids:
        return thread_id
    if thread_id and thread_id in stop_after_case_ids:
        return thread_id
    if len(stop_after_case_ids) == 1:
        return next(iter(stop_after_case_ids))
    return None


def extract_draft_result(message: Message) -> dict[str, Any] | None:
    """Extract create_gmail_draft output from a tool result."""

    if (
        message.metadata.type != MessageType.TOOL_RESULT
        or message.tool_name != "create_gmail_draft"
    ):
        return None
    try:
        payload = json.loads(message.content)
    except json.JSONDecodeError:
        return None

    if payload.get("dry_run") and payload.get("tool") == "create_gmail_draft":
        args = payload.get("args", {})
        return {
            "dry_run": True,
            "draft_id": None,
            "thread_id": args.get("thread_id"),
            "subject": args.get("subject"),
        }

    draft_id = payload.get("draft_id")
    if not draft_id:
        return None
    return {
        "dry_run": False,
        "draft_id": draft_id,
        "thread_id": payload.get("thread_id"),
        "subject": payload.get("subject"),
    }


def _auto_save_draft_case_state(
    *,
    draft: dict[str, Any],
    case_states: list[dict[str, Any]],
    stop_after_case_ids: set[str] | None,
    live_run: bool,
    state_config: StateConfig | None,
) -> dict[str, Any] | None:
    case_id = _resolve_case_id_for_draft(
        draft.get("thread_id"),
        stop_after_case_ids=stop_after_case_ids,
    )
    if not case_id or case_id in _saved_case_ids(case_states):
        return None

    data: dict[str, Any] = {
        "draft_id": draft.get("draft_id") or "dry-run",
        "thread_id": draft.get("thread_id"),
        "auto_saved": True,
    }
    if draft.get("subject"):
        data["draft_subject"] = draft["subject"]

    if draft.get("dry_run") or not live_run or state_config is None:
        return {
            "case_id": case_id,
            "status": "draft_created",
            "data": data,
            "dry_run": True,
        }

    saved = StateTools(state_config).save_case_state(
        case_id=case_id,
        status="draft_created",
        data=data,
    )
    return {
        "case_id": case_id,
        "status": "draft_created",
        "data": saved.get("data", data),
        "dry_run": False,
    }


def extract_case_state_result(message: Message) -> dict[str, Any] | None:
    """Extract model-requested save_case_state output from a tool result."""

    if (
        message.metadata.type != MessageType.TOOL_RESULT
        or message.tool_name != "save_case_state"
    ):
        return None
    try:
        payload = json.loads(message.content)
    except json.JSONDecodeError:
        return {
            "case_id": None,
            "status": "failed",
            "data": {"error_message": message.content},
            "dry_run": False,
        }

    if payload.get("dry_run") and payload.get("tool") == "save_case_state":
        args = payload.get("args", {})
        return {
            "case_id": args.get("case_id"),
            "status": args.get("status"),
            "data": args.get("data", {}),
            "dry_run": True,
        }
    return {
        "case_id": payload.get("case_id"),
        "status": payload.get("status"),
        "data": payload.get("data", {}),
        "dry_run": False,
    }


def build_message_observer(
    *,
    status_sink: StatusSink | None,
    case_states: list[dict[str, Any]],
    memory: ChatMemory | None = None,
    stop_after_case_ids: set[str] | None = None,
    run_trace: RunTrace | None = None,
    live_run: bool = False,
    state_config: StateConfig | None = None,
) -> Callable[[Message], None]:
    status_printer = build_status_printer(status_sink)
    last_extract_claim: dict[str, Any] = {}

    def _maybe_stop_after_saved() -> None:
        if not stop_after_case_ids:
            return
        if stop_after_case_ids <= _saved_case_ids(case_states):
            raise CaseStatesComplete

    def _observe(message: Message) -> None:
        if run_trace is not None:
            run_trace.message(message)
        status_printer(message)
        if memory is not None:
            memory.remember_tool_result(message)

        # Track last extract for enriching auto-saved draft cases and reporting
        if (
            message.metadata.type == MessageType.TOOL_RESULT
            and message.tool_name == "extract_feedback_claim"
        ):
            try:
                payload = json.loads(message.content)
                if isinstance(payload, dict):
                    last_extract_claim.clear()
                    last_extract_claim.update(payload)
            except Exception:
                pass

        case_state = extract_case_state_result(message)
        if case_state is not None:
            case_states.append(case_state)
            _maybe_stop_after_saved()
            return

        if stop_after_case_ids:
            draft = extract_draft_result(message)
            if draft is not None:
                auto_saved = _auto_save_draft_case_state(
                    draft=draft,
                    case_states=case_states,
                    stop_after_case_ids=stop_after_case_ids,
                    live_run=live_run,
                    state_config=state_config,
                )
                if auto_saved is not None:
                    # Enrich with info from extract so case reports have
                    # case_type, recommended labels etc even on rescue path.
                    rec_labels = last_extract_claim.get("recommended_labels") or []
                    case_type = last_extract_claim.get("case_type")
                    proj = last_extract_claim.get("project")
                    if proj and not auto_saved.get("project_label"):
                        auto_saved["project_label"] = proj
                    if rec_labels:
                        auto_saved["matched_labels"] = list(rec_labels)
                        # labels_applied left [] until apply tool runs and we update
                        if "labels_applied" not in auto_saved:
                            auto_saved["labels_applied"] = []
                    if case_type and not auto_saved.get("issue_type"):
                        auto_saved["issue_type"] = case_type
                    if last_extract_claim:
                        d = auto_saved.setdefault("data", {})
                        if not d.get("case_type") and case_type:
                            d["case_type"] = case_type
                            d["issue_type"] = case_type
                        if not d.get("recommended_labels") and rec_labels:
                            d["recommended_labels"] = list(rec_labels)
                        if not d.get("applied_labels") and not d.get("labels_applied") and rec_labels:
                            d["labels_applied"] = list(rec_labels)
                    case_states.append(auto_saved)
                    # Do not stop here. Draft auto-save is a safety net for
                    # cases where the model creates a draft but fails to call
                    # save_case_state. Continue the loop so the model can
                    # call apply_existing_gmail_labels (using recommended_labels)
                    # and then the explicit save_case_state as the terminal step.
                    # Explicit saves will trigger stop.

        # When apply succeeds, enrich the latest case record so reports show
        # labels_applied (works for both auto-draft-rescue and normal paths).
        if (
            message.metadata.type == MessageType.TOOL_RESULT
            and message.tool_name == "apply_existing_gmail_labels"
        ):
            try:
                payload = json.loads(message.content)
                applied = payload.get("applied_labels") or []
                if applied and case_states:
                    last = case_states[-1]
                    last["labels_applied"] = list(applied)
                    if isinstance(last.get("data"), dict):
                        last["data"]["labels_applied"] = list(applied)
                        last["data"]["applied_labels"] = list(applied)
            except Exception:
                pass

    return _observe


class SupportAgentRunner:
    """Thin wrapper around Forge WorkflowRunner.

    Entry points should call this class instead of routing user requests with
    keywords or directly invoking Gmail/ClickHouse business tools.
    """

    def __init__(
        self,
        run_config: AgentRunConfig,
        support_config: SupportAgentConfig | None = None,
    ) -> None:
        self.support_config = support_config or load_config(run_config.config_path)
        model_config = resolve_support_model_config(
            self.support_config,
            run_config.profile,
        )
        self.run_config = resolve_run_config(run_config, model_config)

    async def run(
        self,
        user_input: str,
        *,
        memory: ChatMemory | None = None,
        status_sink: StatusSink | None = None,
        stop_after_case_ids: set[str] | None = None,
        run_trace: RunTrace | None = None,
    ) -> AgentRunResult:
        live_run = LIVE_CONFIRMATION in user_input
        auto_mode = stop_after_case_ids is not None
        case_states: list[dict[str, Any]] = []
        if auto_mode:
            workflow = build_multi_project_workflow(
                self.support_config,
                dry_run=not live_run,
                allow_db_in_dry_run=self.run_config.allow_db_in_dry_run,
            )
        else:
            workflow = build_multi_project_chat_workflow(
                self.support_config,
                dry_run=not live_run,
                allow_db_in_dry_run=self.run_config.allow_db_in_dry_run,
            )
        client = build_client(self.run_config)
        deepseek_client = isinstance(client, DeepSeekOpenAICompatClient)
        fold_patch = _DeepSeekFoldPatch() if deepseek_client else None
        compact_strategy = (
            DeepSeekTieredCompact(keep_recent=3)
            if deepseek_client
            else TieredCompact(keep_recent=3)
        )
        runner = WorkflowRunner(
            client=client,
            context_manager=ContextManager(
                strategy=compact_strategy,
                budget_tokens=self.run_config.budget_tokens,
            ),
            max_iterations=self.run_config.max_iterations,
            max_retries_per_step=6,
            retry_nudge=support_retry_nudge,
            on_message=build_message_observer(
                status_sink=status_sink,
                case_states=case_states,
                memory=memory,
                stop_after_case_ids=stop_after_case_ids,
                run_trace=run_trace,
                live_run=live_run,
                state_config=self.support_config.state if auto_mode else None,
            ),
        )
        user_message = build_user_message(
            user_input,
            live_run=live_run,
            allow_db_in_dry_run=self.run_config.allow_db_in_dry_run,
            memory=memory,
            model_context=build_model_runtime_context(self.run_config),
            auto_mode=auto_mode,
        )
        if status_sink:
            status_sink("[模型] 正在分析请求")
        else:
            print("[模型] 正在分析请求", flush=True)
        try:
            if fold_patch is not None:
                fold_patch.__enter__()
            try:
                result = await asyncio.wait_for(
                    runner.run(workflow, user_message),
                    timeout=self.run_config.timeout_seconds,
                )
            finally:
                if fold_patch is not None:
                    fold_patch.__exit__(None, None, None)
        except CaseStatesComplete:
            answer = f"自动处理已保存 {len(case_states)} 个 case 状态。"
            return AgentRunResult(
                answer=answer,
                live_run=live_run,
                case_states=case_states,
            )
        except MaxIterationsError:
            if (
                stop_after_case_ids
                and stop_after_case_ids <= _saved_case_ids(case_states)
            ):
                answer = f"自动处理已保存 {len(case_states)} 个 case 状态。"
                return AgentRunResult(
                    answer=answer,
                    live_run=live_run,
                    case_states=case_states,
                )
            raise
        except ToolCallError as exc:
            if not case_states:
                raise
            answer = (exc.raw_response or "").strip()
            if not answer:
                answer = "模型已保存本轮 case 状态，但最终自然语言总结工具调用失败。"
            return AgentRunResult(
                answer=answer,
                live_run=live_run,
                case_states=case_states,
            )
        answer = result.strip() if isinstance(result, str) else str(result)
        if memory is not None:
            memory.add(user_input, answer)
        return AgentRunResult(
            answer=answer,
            live_run=live_run,
            case_states=case_states,
        )

    def run_sync(
        self,
        user_input: str,
        *,
        memory: ChatMemory | None = None,
        status_sink: StatusSink | None = None,
        stop_after_case_ids: set[str] | None = None,
        run_trace: RunTrace | None = None,
    ) -> AgentRunResult:
        return asyncio.run(
            self.run(
                user_input,
                memory=memory,
                status_sink=status_sink,
                stop_after_case_ids=stop_after_case_ids,
                run_trace=run_trace,
            )
        )
