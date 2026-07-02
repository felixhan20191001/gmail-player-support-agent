"""Persistent scheduler state for Gmail message processing."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .candidate_discovery_filter import SKIP_CATEGORY_NON_PROJECT


TERMINAL_STATUSES = {
    "draft_created",
    "human_review",
    "skipped",
    "processed",  # Legacy status from earlier scheduler versions.
}
# Local successful outcomes may be re-selected while Gmail still marks UNREAD.
# Failed outcomes require an explicit retry so one bad message cannot loop forever.
REPROCESS_WHEN_UNREAD_STATUSES = TERMINAL_STATUSES
VALID_MESSAGE_STATUSES = TERMINAL_STATUSES | {"failed"}
STATUS_ALIASES = {
    "drafted": "draft_created",
    "draft_created": "draft_created",
    "create_draft": "draft_created",
    "draft_missing_info": "draft_created",
    "draft_for_review": "human_review",
    "human_review": "human_review",
    "handoff_human": "human_review",
    "needs_human": "human_review",
    "failed": "failed",
    "error": "failed",
    "skipped": "skipped",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_interactive_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"chat-{timestamp}-{uuid.uuid4().hex[:8]}"


def normalize_message_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return STATUS_ALIASES.get(status, "failed")


def text_preview(value: Any, *, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def summarize_run_status(outcomes: list[dict[str, Any]]) -> str:
    statuses = {outcome.get("status") for outcome in outcomes}
    if not statuses:
        return "failed"
    if statuses == {"skipped"}:
        return "skipped"
    if "failed" in statuses:
        return "failed"
    if "human_review" in statuses:
        return "human_review"
    if "draft_created" in statuses:
        return "draft_created"
    return "failed"


AUTO_RUN_STATUS_TEXT = {
    "draft_created": "已创建草稿",
    "human_review": "已转人工处理",
    "failed": "处理失败",
    "skipped": "无新邮件",
    "already_processed": "候选均已处理",
    "discovery_only": "只检测新邮件",
}

STORE_STATUS_LABELS = {
    "pending": "待处理",
    "processing": "处理中",
    "draft_created": "已创建草稿",
    "human_review": "已转人工",
    "skipped": "已跳过",
    "failed": "失败",
    "processed": "已处理",
}


def format_auto_run_status_text(
    run_status: str,
    *,
    outcomes: list[dict[str, Any]] | None = None,
    candidate_count: int = 0,
    selected_count: int = 0,
) -> str:
    """Map scheduler run status to user-facing Chinese summary text."""

    if run_status == "skipped" and outcomes:
        return "新邮件无内容"
    if run_status == "skipped" and candidate_count > 0 and selected_count == 0:
        return AUTO_RUN_STATUS_TEXT["already_processed"]
    return AUTO_RUN_STATUS_TEXT.get(run_status, run_status)


def summarize_interactive_run_status(case_states: list[dict[str, Any]]) -> str:
    if not case_states:
        return "completed"
    statuses = {
        normalize_message_status(state.get("status"))
        for state in case_states
    }
    if "failed" in statuses:
        return "failed"
    if "human_review" in statuses:
        return "human_review"
    if "draft_created" in statuses:
        return "draft_created"
    if statuses == {"skipped"}:
        return "skipped"
    return "completed"


def _extract_draft_id(data: dict[str, Any]) -> str | None:
    direct = data.get("draft_id")
    if direct:
        return str(direct)
    draft = data.get("draft")
    if isinstance(draft, dict) and draft.get("draft_id"):
        return str(draft["draft_id"])
    return None


def _extract_labels(data: dict[str, Any]) -> list[str]:
    labels = data.get("applied_labels") or data.get("labels") or []
    if not isinstance(labels, list):
        return []
    return [str(label) for label in labels]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _with_unread_reprocess_state(
    candidate: dict[str, Any],
    state: dict[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    """Attach compact local terminal state needed to finish Gmail cleanup."""

    state_data = state.get("data")
    if not isinstance(state_data, dict):
        state_data = {}

    recommended_labels = _string_list(state_data.get("recommended_labels"))
    if not recommended_labels:
        recommended_labels = _string_list(state_data.get("labels_applied"))
    if not recommended_labels:
        recommended_labels = _string_list(state.get("matched_labels"))

    return {
        **candidate,
        "reprocess_gmail_unread": True,
        "existing_status": status,
        "existing_draft_id": (
            state.get("draft_id")
            or _extract_draft_id(state_data)
            or None
        ),
        "existing_issue_type": (
            state_data.get("issue_type")
            or state_data.get("case_type")
            or None
        ),
        "existing_recommended_labels": recommended_labels,
        "existing_labels_applied": _string_list(state.get("labels_applied")),
    }


class ProcessedMessageStore:
    """JSON-backed store used by the scheduler for dedupe and retries."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"messages": {}, "runs": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.setdefault("messages", {})
        data.setdefault("runs", [])
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def record_run(
        self,
        *,
        run_id: str,
        status: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        data = self._read()
        data["runs"].append(
            {
                "run_id": run_id,
                "status": status,
                "message": message,
                "payload": payload or {},
                "created_at": utc_now(),
            }
        )
        self._write(data)

    def record_interactive_run(
        self,
        *,
        run_id: str,
        status: str,
        user_input: str,
        live_run: bool,
        answer: str | None = None,
        error_message: str | None = None,
        case_states: list[dict[str, Any]] | None = None,
    ) -> None:
        self.record_run(
            run_id=run_id,
            status=status,
            message=f"interactive {status}",
            payload={
                "mode": "interactive",
                "live_run": live_run,
                "input_preview": text_preview(user_input),
                "answer_preview": text_preview(answer),
                "error_message": text_preview(error_message),
                "case_state_count": len(case_states or []),
                "case_states": case_states or [],
            },
        )

    def mark_non_project_ignored(
        self,
        candidates: list[dict[str, Any]],
    ) -> None:
        """Persist discovery-only skips for non-player-feedback mail."""

        data = self._read()
        now = utc_now()
        for candidate in candidates:
            message_id = candidate["message_id"]
            existing = data["messages"].get(message_id, {})
            discovery_metadata = candidate.get("discovery_metadata") or {}
            data["messages"][message_id] = {
                **existing,
                "message_id": message_id,
                "thread_id": candidate.get("thread_id", existing.get("thread_id", "")),
                "project_label": candidate.get(
                    "project_label",
                    existing.get("project_label"),
                ),
                "matched_labels": candidate.get(
                    "matched_labels",
                    existing.get("matched_labels", []),
                ),
                "first_seen_at": existing.get("first_seen_at", now),
                "status": "skipped",
                "skip_category": SKIP_CATEGORY_NON_PROJECT,
                "agent_run_id": None,
                "last_processed_at": now,
                "retry_count": int(existing.get("retry_count", 0)),
                "error_message": None,
                "data": {
                    "issue_type": "non_project",
                    "email_subject": discovery_metadata.get("subject"),
                    "email_from": discovery_metadata.get("from"),
                    "project_labels": discovery_metadata.get("project_labels") or [],
                },
            }
        self._write(data)

    def record_seen(self, candidates: list[dict[str, Any]]) -> None:
        data = self._read()
        messages = data["messages"]
        now = utc_now()
        for candidate in candidates:
            message_id = candidate["message_id"]
            existing = messages.get(message_id, {})
            messages[message_id] = {
                **existing,
                "message_id": message_id,
                "thread_id": candidate.get("thread_id", existing.get("thread_id", "")),
                "project_label": candidate.get(
                    "project_label",
                    existing.get("project_label"),
                ),
                "matched_labels": candidate.get(
                    "matched_labels",
                    existing.get("matched_labels", []),
                ),
                "first_seen_at": existing.get("first_seen_at", now),
                "status": existing.get("status", "pending"),
                "retry_count": int(existing.get("retry_count", 0)),
            }
        self._write(data)

    def clear_candidate_processing_state(self, candidates: list[dict[str, Any]]) -> int:
        """Reset local processing outcomes for the current discovered candidates."""

        data = self._read()
        messages = data["messages"]
        now = utc_now()
        cleared = 0
        seen: set[str] = set()
        for candidate in candidates:
            message_id = str(candidate.get("message_id") or "")
            if not message_id or message_id in seen:
                continue
            seen.add(message_id)
            existing = messages.get(message_id, {})
            reset_state = {
                **existing,
                "message_id": message_id,
                "thread_id": candidate.get("thread_id", existing.get("thread_id", "")),
                "project_label": candidate.get(
                    "project_label",
                    existing.get("project_label"),
                ),
                "matched_labels": candidate.get(
                    "matched_labels",
                    existing.get("matched_labels", []),
                ),
                "first_seen_at": existing.get("first_seen_at", now),
                "status": "pending",
                "retry_count": 0,
                "agent_run_id": None,
                "last_processed_at": None,
                "error_message": None,
            }
            for stale_field in (
                "skip_category",
                "agent_answer_preview",
                "draft_id",
                "labels_applied",
                "human_review_reason",
                "data",
            ):
                reset_state.pop(stale_field, None)
            messages[message_id] = reset_state
            cleared += 1
        if cleared:
            self._write(data)
        return cleared

    def select_unprocessed(
        self,
        candidates: list[dict[str, Any]],
        *,
        limit: int,
        retry_failed: bool,
        max_retries: int,
    ) -> list[dict[str, Any]]:
        data = self._read()
        messages = data["messages"]
        selected: list[dict[str, Any]] = []
        for candidate in candidates:
            message_id = candidate["message_id"]
            state = messages.get(message_id, {})
            status = state.get("status", "pending")
            retry_count = int(state.get("retry_count", 0))
            if status in TERMINAL_STATUSES or status == "processing":
                continue
            if status == "failed" and not retry_failed:
                continue
            if status == "failed" and retry_count >= max_retries:
                continue
            selected.append(candidate)
            if len(selected) >= limit:
                break
        return selected

    def select_candidates_for_run(
        self,
        candidates: list[dict[str, Any]],
        *,
        limit: int,
        retry_failed: bool,
        max_retries: int,
        ignore_store: bool,
        unread_message_ids: set[str] | None = None,
        reprocess_failed_unread: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Select candidates to process and explain skipped ones.

        Gmail UNREAD is the source of truth for successful terminal local
        records: they are re-selected while Gmail still marks the message
        UNREAD, so the worker can clear any unfinished Gmail state. Failed local
        records are different: they are skipped unless the caller explicitly
        enables ``retry_failed``. This prevents one model-loop failure from
        monopolizing every automation round.
        """

        if ignore_store:
            return candidates[:limit], []

        data = self._read()
        messages = data["messages"]
        unread_ids = unread_message_ids or set()
        selected: list[dict[str, Any]] = []
        skipped_details: list[dict[str, Any]] = []

        for candidate in candidates:
            message_id = candidate["message_id"]
            state = messages.get(message_id, {})
            status = state.get("status", "pending")
            retry_count = int(state.get("retry_count", 0))
            gmail_unread = message_id in unread_ids
            skip_category = str(state.get("skip_category") or "")

            if skip_category == SKIP_CATEGORY_NON_PROJECT:
                skipped_details.append(
                    {
                        "message_id": message_id,
                        "store_status": status,
                        "gmail_unread": gmail_unread,
                        "reason": "non_project_mail",
                    }
                )
                continue

            if status == "processing":
                skipped_details.append(
                    {
                        "message_id": message_id,
                        "store_status": status,
                        "gmail_unread": gmail_unread,
                        "reason": "processing_in_progress",
                    }
                )
                continue
            if gmail_unread and status in REPROCESS_WHEN_UNREAD_STATUSES:
                selected.append(
                    _with_unread_reprocess_state(
                        candidate,
                        state,
                        status=status,
                    )
                )
                if len(selected) >= limit:
                    break
                continue
            if status == "failed" and not retry_failed:
                skipped_details.append(
                    {
                        "message_id": message_id,
                        "store_status": status,
                        "gmail_unread": gmail_unread,
                        "reason": "failed_retry_disabled",
                    }
                )
                continue
            if status == "failed" and retry_count >= max_retries:
                skipped_details.append(
                    {
                        "message_id": message_id,
                        "store_status": status,
                        "gmail_unread": gmail_unread,
                        "reason": "failed_retry_exhausted",
                    }
                )
                continue
            if status in TERMINAL_STATUSES:
                skipped_details.append(
                    {
                        "message_id": message_id,
                        "store_status": status,
                        "gmail_unread": gmail_unread,
                        "reason": "terminal_already_processed",
                    }
                )
                continue

            selected.append(candidate)
            if len(selected) >= limit:
                break

        return selected, skipped_details

    def mark_processing(self, selected: list[dict[str, Any]], *, run_id: str) -> None:
        data = self._read()
        now = utc_now()
        for item in selected:
            message_id = item["message_id"]
            existing = data["messages"].get(message_id, {})
            data["messages"][message_id] = {
                **existing,
                "message_id": message_id,
                "thread_id": item.get("thread_id", existing.get("thread_id", "")),
                "project_label": item.get("project_label", existing.get("project_label")),
                "matched_labels": item.get(
                    "matched_labels",
                    existing.get("matched_labels", []),
                ),
                "status": "processing",
                "agent_run_id": run_id,
                "last_processed_at": now,
                "first_seen_at": existing.get("first_seen_at", now),
                "retry_count": int(existing.get("retry_count", 0)),
                "error_message": None,
            }
        self._write(data)

    def mark_outcomes(
        self,
        selected: list[dict[str, Any]],
        *,
        run_id: str,
        answer: str,
        case_states: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        data = self._read()
        now = utc_now()
        by_case_id = {
            str(state.get("case_id")): state
            for state in case_states
            if state.get("case_id") is not None
        }
        outcomes: list[dict[str, Any]] = []
        for item in selected:
            message_id = item["message_id"]
            existing = data["messages"].get(message_id, {})
            state = by_case_id.get(message_id) or by_case_id.get(item.get("thread_id", ""))
            state_data = state.get("data", {}) if state else {}
            if not isinstance(state_data, dict):
                state_data = {"raw_data": state_data}
            status = normalize_message_status(state.get("status") if state else None)
            error_message = state_data.get("error_message")
            if state is None:
                error_message = "Agent did not call save_case_state for this message."
            retry_count = int(existing.get("retry_count", 0))
            if status == "failed":
                retry_count += 1
            outcome = {
                "message_id": message_id,
                "thread_id": item.get("thread_id", existing.get("thread_id", "")),
                "project_label": (
                    state_data.get("project_label")
                    or item.get("project_label")
                    or existing.get("project_label")
                ),
                "matched_labels": item.get(
                    "matched_labels",
                    existing.get("matched_labels", []),
                ),
                "status": status,
                "draft_id": _extract_draft_id(state_data),
                "labels_applied": _extract_labels(state_data),
                "human_review_reason": state_data.get("human_review_reason"),
                "error_message": error_message,
            }
            outcomes.append(outcome)
            data["messages"][message_id] = {
                **existing,
                "message_id": message_id,
                "thread_id": item.get("thread_id", existing.get("thread_id", "")),
                "project_label": outcome["project_label"],
                "matched_labels": outcome["matched_labels"],
                "status": status,
                "agent_run_id": run_id,
                "last_processed_at": now,
                "agent_answer_preview": text_preview(answer, limit=1000),
                "draft_id": outcome["draft_id"],
                "labels_applied": outcome["labels_applied"],
                "human_review_reason": outcome["human_review_reason"],
                "error_message": error_message,
                "retry_count": retry_count,
                "data": state_data,
            }
        self._write(data)
        return outcomes

    def mark_failed(
        self,
        selected: list[dict[str, Any]],
        *,
        run_id: str,
        error_message: str,
    ) -> None:
        data = self._read()
        now = utc_now()
        for item in selected:
            message_id = item["message_id"]
            existing = data["messages"].get(message_id, {})
            retry_count = int(existing.get("retry_count", 0)) + 1
            data["messages"][message_id] = {
                **existing,
                "message_id": message_id,
                "thread_id": item.get("thread_id", existing.get("thread_id", "")),
                "project_label": item.get("project_label", existing.get("project_label")),
                "matched_labels": item.get(
                    "matched_labels",
                    existing.get("matched_labels", []),
                ),
                "status": "failed",
                "agent_run_id": run_id,
                "last_processed_at": now,
                "retry_count": retry_count,
                "error_message": error_message,
            }
        self._write(data)

    def recover_stale_processing(
        self,
        *,
        stale_after_minutes: int,
        target_status: str = "failed",
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Explicitly recover local messages stuck in processing state."""

        if target_status not in {"failed", "pending"}:
            raise ValueError("target_status must be 'failed' or 'pending'")
        if stale_after_minutes < 1:
            raise ValueError("stale_after_minutes must be positive")

        data = self._read()
        messages = data["messages"]
        current = now or datetime.now(timezone.utc)
        recovered: list[dict[str, Any]] = []

        for message_id, state in messages.items():
            if state.get("status") != "processing":
                continue
            raw_processed_at = state.get("last_processed_at")
            if not raw_processed_at:
                continue
            try:
                processed_at = datetime.fromisoformat(str(raw_processed_at))
            except ValueError:
                continue
            if processed_at.tzinfo is None:
                processed_at = processed_at.replace(tzinfo=timezone.utc)
            age_minutes = (current - processed_at).total_seconds() / 60
            if age_minutes < stale_after_minutes:
                continue

            previous_run_id = state.get("agent_run_id")
            state["status"] = target_status
            state["last_processed_at"] = current.isoformat()
            if target_status == "failed":
                state["error_message"] = (
                    "Stale processing recovered after "
                    f"{int(age_minutes)} minutes; previous run_id={previous_run_id}"
                )
            else:
                state["error_message"] = None
            recovered.append(
                {
                    "message_id": str(message_id),
                    "thread_id": str(state.get("thread_id") or ""),
                    "previous_run_id": previous_run_id,
                    "previous_status": "processing",
                    "new_status": target_status,
                }
            )

        if recovered:
            self._write(data)
        return recovered
