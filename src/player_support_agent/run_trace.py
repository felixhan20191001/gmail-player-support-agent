"""Structured trace logging for interactive and manual agent runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forge import Message, MessageType

from .paths import default_var_dir


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _preview(text: str, *, limit: int = 400) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


@dataclass
class RunTrace:
    """Append-only JSONL trace for one agent run."""

    run_id: str
    log_dir: Path = field(default_factory=lambda: default_var_dir() / "logs" / "interactive")
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / f"{self.run_id}.jsonl"

    def record(self, event_type: str, **payload: Any) -> None:
        event = {"ts": _utc_now(), "type": event_type, **payload}
        self.events.append(event)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def status(self, message: str) -> None:
        self.record("status", message=message)

    def message(self, message: Message) -> None:
        payload: dict[str, Any] = {
            "message_type": message.metadata.type.value,
            "role": message.role.value,
        }
        if message.tool_name:
            payload["tool_name"] = message.tool_name
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "name": tool_call.name,
                    "args_preview": _preview(json.dumps(tool_call.args, ensure_ascii=False)),
                }
                for tool_call in message.tool_calls
            ]
        content = message.content or ""
        if message.metadata.type in {
            MessageType.TOOL_RESULT,
            MessageType.RETRY_NUDGE,
            MessageType.STEP_NUDGE,
            MessageType.PREREQUISITE_NUDGE,
        } or content.startswith("["):
            payload["content_preview"] = _preview(content, limit=800)
        self.record("message", **payload)

    def result(
        self,
        *,
        status: str,
        live_run: bool,
        answer: str | None = None,
        error_message: str | None = None,
        case_states: list[dict[str, Any]] | None = None,
    ) -> None:
        self.record(
            "result",
            status=status,
            live_run=live_run,
            answer_preview=_preview(answer or "", limit=1200),
            error_message=error_message,
            case_state_count=len(case_states or []),
            case_states=case_states or [],
        )