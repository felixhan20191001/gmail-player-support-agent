"""Dry-run wrappers for side-effecting support tools."""

from __future__ import annotations

from collections.abc import Callable
import inspect
from typing import Any

from forge import ToolDef


SIDE_EFFECT_TOOLS = {
    "apply_existing_gmail_labels",
    "mark_gmail_messages_read",
    "create_gmail_draft",
    "notify_human_support",
    "save_case_state",
    "write_audit_log",
}


READ_ONLY_EXTERNAL_TOOLS = {
    "query_clickhouse",
    "query_support_evidence",
}


def _dry_result(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "dry_run": True,
        "tool": tool_name,
        "would_execute": True,
        "args": args,
    }


def _wrap_callable(tool_name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    async def async_wrapper(**kwargs: Any) -> dict[str, Any]:
        return _dry_result(tool_name, kwargs)

    def sync_wrapper(**kwargs: Any) -> dict[str, Any]:
        return _dry_result(tool_name, kwargs)

    if inspect.iscoroutinefunction(fn):
        return async_wrapper
    return sync_wrapper


def apply_dry_run(
    tools: dict[str, ToolDef],
    *,
    allow_db: bool = False,
) -> dict[str, ToolDef]:
    """Replace side-effecting tool callables with dry-run callables."""

    blocked = set(SIDE_EFFECT_TOOLS)
    if not allow_db:
        blocked |= READ_ONLY_EXTERNAL_TOOLS

    wrapped: dict[str, ToolDef] = {}
    for name, tool in tools.items():
        if name in blocked:
            wrapped[name] = ToolDef(
                spec=tool.spec,
                callable=_wrap_callable(name, tool.callable),
                prerequisites=tool.prerequisites,
            )
        else:
            wrapped[name] = tool
    return wrapped
