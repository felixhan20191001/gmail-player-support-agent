"""Aggregation and feed events for automation session overviews."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .run_result_summary import OUTCOME_STATUS_LABELS

EMPTY_ROUND_STATUSES = frozenset({"skipped", "already_processed", "discovery_only"})


@dataclass
class CycleSnapshot:
    run_id: str
    created_at: str
    status: str
    candidate_count: int = 0
    selected_count: int = 0
    headline: str = ""
    text: str = ""
    outcome_count_by_status: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_run_result(cls, result: dict[str, Any], *, created_at: str) -> CycleSnapshot:
        outcomes = list(result.get("outcomes") or [])
        outcome_count_by_status: dict[str, int] = {}
        for outcome in outcomes:
            status = str(outcome.get("status") or "unknown")
            outcome_count_by_status[status] = (
                outcome_count_by_status.get(status, 0) + 1
            )
        human_summary = result.get("human_summary") or {}
        return cls(
            run_id=str(result.get("run_id") or ""),
            created_at=created_at,
            status=str(result.get("status") or "unknown"),
            candidate_count=int(result.get("candidate_count") or 0),
            selected_count=int(result.get("selected_count") or 0),
            headline=str(human_summary.get("headline") or ""),
            text=str(human_summary.get("text") or ""),
            outcome_count_by_status=outcome_count_by_status,
        )


def aggregate_session(cycles: list[CycleSnapshot]) -> dict[str, Any]:
    """Summarize automation session metrics from cycle snapshots."""

    outcome_by_status: dict[str, int] = {}
    processed_count = 0
    empty_rounds = 0
    last_cycle_at: str | None = None

    for cycle in cycles:
        processed_count += int(cycle.selected_count or 0)
        if cycle.status in EMPTY_ROUND_STATUSES and int(cycle.selected_count or 0) == 0:
            empty_rounds += 1
        for status, count in cycle.outcome_count_by_status.items():
            outcome_by_status[status] = outcome_by_status.get(status, 0) + int(count)
        if cycle.created_at:
            last_cycle_at = cycle.created_at

    return {
        "cycle_count": len(cycles),
        "processed_count": processed_count,
        "outcome_by_status": outcome_by_status,
        "empty_rounds": empty_rounds,
        "last_cycle_at": last_cycle_at,
    }


def format_session_summary_text(
    summary: dict[str, Any],
    *,
    running: bool,
    started_at: str | None,
) -> str:
    """Build a one-line Chinese overview for the dialog banner."""

    parts = ["本次自动处理"]
    if not running:
        parts[0] = "自动处理已停止"
    if started_at:
        parts.append(f"开始 {started_at[:16].replace('T', ' ')}")
    parts.append(f"已跑 {summary.get('cycle_count', 0)} 轮")
    parts.append(f"实际处理 {summary.get('processed_count', 0)} 封")
    outcome_by_status = summary.get("outcome_by_status") or {}
    for status, count in sorted(outcome_by_status.items()):
        if not count:
            continue
        label = OUTCOME_STATUS_LABELS.get(status, status)
        parts.append(f"{label} {count}")
    empty_rounds = int(summary.get("empty_rounds") or 0)
    if empty_rounds:
        parts.append(f"空轮 {empty_rounds}")
    return " · ".join(parts)


def build_session_started_message(
    *,
    session_id: str,
    started_at: str,
    live_run: bool,
    interval_seconds: int,
) -> str:
    mode = "Live 草稿" if live_run else "Dry-run"
    return (
        f"自动处理已启动（{mode}，间隔 {interval_seconds}s）。"
        f" session={session_id}，开始于 {started_at[:19].replace('T', ' ')}Z"
    )


def build_automation_feed(
    *,
    running: bool,
    session_id: str | None,
    started_at: str | None,
    live_run: bool,
    interval_seconds: int | None,
    cycles: list[CycleSnapshot],
    after_run_id: str | None,
    include_history: bool,
) -> dict[str, Any]:
    """Build feed payload for the Web UI poll endpoint."""

    summary = aggregate_session(cycles)
    events: list[dict[str, Any]] = []
    if session_id and started_at and include_history and not after_run_id:
        events.append(
            {
                "type": "session_started",
                "session_id": session_id,
                "message": build_session_started_message(
                    session_id=session_id,
                    started_at=started_at,
                    live_run=live_run,
                    interval_seconds=int(interval_seconds or 0),
                ),
            }
        )

    start_index = 0
    if after_run_id:
        for index, cycle in enumerate(cycles):
            if cycle.run_id == after_run_id:
                start_index = index + 1
                break

    for index in range(start_index, len(cycles)):
        cycle = cycles[index]
        cycle_index = index + 1
        body = cycle.text or cycle.headline or cycle.status
        events.append(
            {
                "type": "cycle_done",
                "run_id": cycle.run_id,
                "cycle_index": cycle_index,
                "headline": cycle.headline,
                "text": f"[自动] 第 {cycle_index} 轮\n{body}".strip(),
                "created_at": cycle.created_at,
                "status": cycle.status,
            }
        )

    return {
        "running": running,
        "session_id": session_id,
        "started_at": started_at,
        "summary": summary,
        "summary_text": format_session_summary_text(
            summary,
            running=running,
            started_at=started_at,
        ),
        "events": events,
    }