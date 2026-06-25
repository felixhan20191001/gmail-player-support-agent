"""Scheduler/worker for automatic Gmail feedback processing."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import uuid
from typing import Any

from .agent_runner import (
    DEFAULT_CONFIG,
    LIVE_CONFIRMATION,
    AgentRunConfig,
    SupportAgentRunner,
    add_model_cli_args,
    build_run_config_from_args,
    resolve_run_config,
    resolve_support_model_config,
    run_config_has_api_key,
)
from .auto_task_builder import build_auto_task
from .candidate_discovery_filter import partition_player_feedback_candidates
from .processed_message_store import ProcessedMessageStore
from .processed_message_store import format_auto_run_status_text
from .processed_message_store import summarize_run_status
from .processed_message_store import text_preview
from .readiness_check import format_readiness_report, run_readiness_checks
from .run_result_summary import build_run_result_summary
from .tools.config import SupportAgentConfig, load_config
from .tools.gmail_tools import GmailTools
from .tools.notify_tools import NotifyTools


async def attach_human_summary(
    result: dict[str, Any],
    *,
    support_config: SupportAgentConfig,
    store: ProcessedMessageStore,
) -> dict[str, Any]:
    outcomes = list(result.get("outcomes") or [])
    message_ids: list[str] = []
    for item in outcomes:
        message_id = item.get("message_id")
        if message_id:
            message_ids.append(str(message_id))
    if not outcomes:
        for item in result.get("candidates") or []:
            message_id = item.get("message_id")
            if message_id:
                message_ids.append(str(message_id))
        for item in result.get("skipped_details") or []:
            message_id = item.get("message_id")
            if message_id:
                message_ids.append(str(message_id))
    message_ids = list(dict.fromkeys(message_ids))

    email_metadata: dict[str, dict[str, Any]] = {}
    if message_ids:
        gmail = GmailTools(support_config.gmail)
        try:
            email_metadata = await gmail.get_message_summaries(message_ids)
        except Exception:
            email_metadata = {}
        finally:
            await gmail.aclose()

    store_messages = store._read().get("messages", {})
    case_data_by_id = {
        message_id: store_messages.get(message_id, {}).get("data") or {}
        for message_id in message_ids
    }
    result["human_summary"] = build_run_result_summary(
        result,
        support_config=support_config,
        email_metadata=email_metadata,
        case_data_by_id=case_data_by_id,
    )
    return result


def new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"auto-{timestamp}-{uuid.uuid4().hex[:8]}"


def sort_candidates_newest_first(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer the newest unread candidate when the scheduler only processes a subset."""

    return sorted(
        candidates,
        key=lambda item: int(item.get("internal_date") or 0),
        reverse=True,
    )


async def enrich_candidates_with_internal_dates(
    config: SupportAgentConfig,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not candidates:
        return []

    gmail = GmailTools(config.gmail)
    dates = await gmail.get_message_internal_dates(
        [item["message_id"] for item in candidates if item.get("message_id")]
    )
    enriched: list[dict[str, Any]] = []
    for candidate in candidates:
        message_id = candidate.get("message_id")
        enriched.append(
            {
                **candidate,
                "internal_date": dates.get(message_id or ""),
            }
        )
    return sort_candidates_newest_first(enriched)


async def fetch_new_message_ids(
    config: SupportAgentConfig,
    *,
    max_results: int,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch Gmail candidates for the scheduler.

    This is intentionally the only direct Gmail call in the automatic path.
    It only discovers candidate IDs and does not classify or process mail.
    """

    gmail = GmailTools(config.gmail)
    if query:
        listing = await gmail.list_new_feedback_emails(
            max_results=max_results,
            query=query,
        )
    else:
        listing = await gmail.list_unread_project_emails(
            max_results_per_label=max(1, min(max_results, 50)),
        )
    candidates: list[dict[str, Any]] = []
    for item in listing.get("messages", []):
        message_id = item.get("id") or item.get("message_id")
        thread_id = item.get("threadId") or item.get("thread_id")
        if message_id and thread_id:
            candidate = {
                "message_id": message_id,
                "thread_id": thread_id,
            }
            if item.get("project_label"):
                candidate["project_label"] = item["project_label"]
            if item.get("matched_labels"):
                candidate["matched_labels"] = item["matched_labels"]
            candidates.append(candidate)
    return await enrich_candidates_with_internal_dates(
        config,
        candidates[:max_results],
    )


def build_failed_outcomes(
    selected: list[dict[str, Any]],
    *,
    error_message: str,
) -> list[dict[str, Any]]:
    return [
        {
            "message_id": item["message_id"],
            "thread_id": item.get("thread_id", ""),
            "project_label": item.get("project_label"),
            "matched_labels": item.get("matched_labels", []),
            "status": "failed",
            "draft_id": None,
            "labels_applied": [],
            "human_review_reason": None,
            "error_message": error_message,
        }
        for item in selected
    ]


def build_failure_handoff_summary(
    *,
    run_id: str,
    outcome: dict[str, Any],
    answer: str | None = None,
) -> str:
    lines = [
        "玩家反馈自动邮件处理失败，需要人工检查。",
        f"Run ID: {run_id}",
        f"Message ID: {outcome.get('message_id')}",
        f"Thread ID: {outcome.get('thread_id')}",
        f"Error: {outcome.get('error_message') or 'unknown'}",
    ]
    if answer:
        lines += ["", "Agent answer preview:", text_preview(answer, limit=800) or ""]
    return "\n".join(lines)


async def notify_failed_outcomes(
    *,
    config: SupportAgentConfig,
    run_id: str,
    outcomes: list[dict[str, Any]],
    answer: str | None,
    status_sink,
) -> list[dict[str, Any]]:
    failed = [outcome for outcome in outcomes if outcome.get("status") == "failed"]
    if not failed:
        return []

    notify = NotifyTools(config.notify)
    notifications: list[dict[str, Any]] = []
    for outcome in failed:
        message_id = str(outcome.get("message_id") or "unknown")
        status_sink(f"[人工] 自动处理失败，正在转人工：{message_id}")
        subject = f"玩家反馈自动处理失败: {message_id}"
        summary = build_failure_handoff_summary(
            run_id=run_id,
            outcome=outcome,
            answer=answer,
        )
        try:
            result = await notify.notify_human_support(
                case_id=message_id,
                subject=subject,
                summary_text=summary,
                priority="high",
            )
            if result.get("notified"):
                status_sink(f"[人工] 失败转人工已记录：{message_id}")
            else:
                status_sink(f"[人工] 人工通知未启用：{message_id}")
        except Exception as exc:
            result = {
                "notified": False,
                "mode": config.notify.mode,
                "error": f"{type(exc).__name__}: {exc}",
            }
            status_sink(f"[人工] 失败转人工通知失败：{message_id}")
        notifications.append(
            {
                "message_id": message_id,
                "thread_id": outcome.get("thread_id"),
                "notification": result,
            }
        )
    return notifications


def _automation_run_payload(
    result: dict[str, Any],
    *,
    run_source: str | None,
    automation_session_id: str | None,
    live_run: bool,
) -> dict[str, Any]:
    if run_source != "automation":
        return {}
    human_summary = result.get("human_summary") or {}
    payload: dict[str, Any] = {
        "source": "automation",
        "automation_session_id": automation_session_id,
        "candidate_count": int(result.get("candidate_count") or 0),
        "selected_count": int(result.get("selected_count") or 0),
        "live_run": live_run,
        "human_summary_headline": human_summary.get("headline"),
        "human_summary_text": human_summary.get("text"),
    }
    if result.get("outcomes") is not None:
        payload["outcomes"] = result.get("outcomes")
    if result.get("skipped_details") is not None:
        payload["skipped_details"] = result.get("skipped_details")
    if result.get("error"):
        payload["error"] = result.get("error")
    if result.get("stage"):
        payload["stage"] = result.get("stage")
    return payload


async def _record_and_summarize_run(
    *,
    store: ProcessedMessageStore,
    support_config: SupportAgentConfig,
    run_id: str,
    status: str,
    message: str,
    result: dict[str, Any],
    run_source: str | None = None,
    automation_session_id: str | None = None,
    live_run: bool = False,
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "run_id": run_id,
        **result,
    }
    result = await attach_human_summary(
        result,
        support_config=support_config,
        store=store,
    )
    payload = {
        **(extra_payload or {}),
        **_automation_run_payload(
            result,
            run_source=run_source,
            automation_session_id=automation_session_id,
            live_run=live_run,
        ),
    }
    store.record_run(
        run_id=run_id,
        status=status,
        message=message,
        payload=payload,
    )
    return result


async def run_once(
    *,
    support_config: SupportAgentConfig,
    run_config: AgentRunConfig,
    store: ProcessedMessageStore,
    max_candidates: int,
    max_new: int,
    retry_failed: bool,
    max_retries: int,
    live_run: bool,
    query: str | None = None,
    ignore_store: bool = False,
    discovery_only: bool = False,
    status_sink=print,
    run_trace=None,
    run_source: str | None = None,
    automation_session_id: str | None = None,
) -> dict[str, Any]:
    run_id = new_run_id()
    status_sink("[调度] 正在检查新增 Gmail 玩家反馈")
    try:
        candidates = await fetch_new_message_ids(
            support_config,
            max_results=max_candidates,
            query=query,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        status_sink(f"[错误] Gmail 新邮件检测失败：{error}")
        return await _record_and_summarize_run(
            store=store,
            support_config=support_config,
            run_id=run_id,
            status="failed",
            message=error,
            result={
                "status": "failed",
                "candidate_count": 0,
                "selected_count": 0,
                "stage": "discovery",
                "error": error,
            },
            run_source=run_source,
            automation_session_id=automation_session_id,
            live_run=live_run,
            extra_payload={"stage": "discovery"},
        )
    store.record_seen(candidates)
    non_project_skipped: list[dict[str, Any]] = []
    if candidates and not ignore_store:
        candidates, non_project_skipped = await partition_player_feedback_candidates(
            support_config.gmail,
            candidates,
        )
        if non_project_skipped:
            store.mark_non_project_ignored(non_project_skipped)
            status_sink(
                f"[调度] 跳过非玩家反馈邮件 {len(non_project_skipped)} 封"
                "（无项目标签/主题或非游戏发件人）"
            )
    if discovery_only:
        status_sink(f"[完成] 已检测候选邮件：{len(candidates)}")
        return await _record_and_summarize_run(
            store=store,
            support_config=support_config,
            run_id=run_id,
            status="discovery_only",
            message="只检测新邮件",
            result={
                "status": "discovery_only",
                "candidate_count": len(candidates),
                "selected_count": 0,
                "candidates": candidates,
            },
            run_source=run_source,
            automation_session_id=automation_session_id,
            live_run=live_run,
            extra_payload={
                "candidate_count": len(candidates),
                "candidates": candidates,
            },
        )

    skipped_details: list[dict[str, Any]] = []
    unread_message_ids: set[str] = set()
    if candidates and not ignore_store:
        gmail = GmailTools(support_config.gmail)
        try:
            unread_message_ids = await gmail.get_unread_message_ids(
                [item["message_id"] for item in candidates if item.get("message_id")]
            )
        except Exception:
            unread_message_ids = set()
        finally:
            await gmail.aclose()

    if ignore_store:
        selected = candidates[:max_new]
    else:
        selected, skipped_details = store.select_candidates_for_run(
            candidates,
            limit=max_new,
            retry_failed=retry_failed,
            max_retries=max_retries,
            ignore_store=ignore_store,
            unread_message_ids=unread_message_ids,
        )

    if not selected:
        run_status = "skipped" if not candidates else "already_processed"
        status_message = "无新邮件" if not candidates else "候选均已处理"
        status_sink(f"[完成] {status_message}")
        return await _record_and_summarize_run(
            store=store,
            support_config=support_config,
            run_id=run_id,
            status=run_status,
            message=status_message,
            result={
                "status": run_status,
                "candidate_count": len(candidates),
                "selected_count": 0,
                "candidates": candidates,
                "skipped_details": skipped_details,
            },
            run_source=run_source,
            automation_session_id=automation_session_id,
            live_run=live_run,
            extra_payload={
                "candidate_count": len(candidates),
                "skipped_details": skipped_details,
            },
        )

    if any(item.get("reprocess_gmail_unread") for item in selected):
        status_sink("[调度] 重跑 Gmail 仍为未读的本地已记录邮件")

    store.mark_processing(selected, run_id=run_id)
    task = build_auto_task(selected, live_run=live_run)
    if live_run and LIVE_CONFIRMATION not in task:
        raise RuntimeError("live auto task is missing the confirmation phrase")

    runner = SupportAgentRunner(run_config, support_config=support_config)
    try:
        result = await runner.run(
            task,
            status_sink=status_sink,
            stop_after_case_ids={item["message_id"] for item in selected},
            run_trace=run_trace,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        store.mark_failed(selected, run_id=run_id, error_message=error)
        outcomes = build_failed_outcomes(selected, error_message=error)
        failure_notifications = await notify_failed_outcomes(
            config=support_config,
            run_id=run_id,
            outcomes=outcomes,
            answer=None,
            status_sink=status_sink,
        )
        status_sink(f"[错误] 自动处理失败：{error}")
        return await _record_and_summarize_run(
            store=store,
            support_config=support_config,
            run_id=run_id,
            status="failed",
            message=error,
            result={
                "status": "failed",
                "candidate_count": len(candidates),
                "selected_count": len(selected),
                "outcomes": outcomes,
                "failure_notifications": failure_notifications,
                "error": error,
            },
            run_source=run_source,
            automation_session_id=automation_session_id,
            live_run=live_run,
            extra_payload={
                "selected": selected,
                "outcomes": outcomes,
                "failure_notifications": failure_notifications,
            },
        )

    outcomes = store.mark_outcomes(
        selected,
        run_id=run_id,
        answer=result.answer,
        case_states=result.case_states,
    )
    run_status = summarize_run_status(outcomes)
    failure_notifications = await notify_failed_outcomes(
        config=support_config,
        run_id=run_id,
        outcomes=outcomes,
        answer=result.answer,
        status_sink=status_sink,
    )
    status_text = format_auto_run_status_text(run_status, outcomes=outcomes)
    prefix = "[错误]" if run_status == "failed" else "[完成]"
    status_sink(f"{prefix} 自动处理结果：{status_text}")
    return await _record_and_summarize_run(
        store=store,
        support_config=support_config,
        run_id=run_id,
        status=run_status,
        message="agent completed",
        result={
            "status": run_status,
            "candidate_count": len(candidates),
            "selected_count": len(selected),
            "outcomes": outcomes,
            "failure_notifications": failure_notifications,
            "answer": result.answer,
        },
        run_source=run_source,
        automation_session_id=automation_session_id,
        live_run=live_run,
        extra_payload={
            "selected": selected,
            "outcomes": outcomes,
            "case_states": result.case_states,
            "live_run": result.live_run,
            "answer_preview": text_preview(result.answer, limit=1000),
            "failure_notifications": failure_notifications,
        },
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automatic multi-project Gmail worker.")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    add_model_cli_args(parser, default_max_iterations=28)
    parser.add_argument("--max-candidates", type=int, default=20)
    parser.add_argument("--max-new", type=int, default=5)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow Gmail draft/label/state side effects. Gmail send is still unavailable.",
    )
    parser.add_argument(
        "--block-db-in-dry-run",
        action="store_true",
        help="Also simulate ClickHouse query execution during dry-run.",
    )
    parser.add_argument(
        "--query",
        help="Optional Gmail candidate query override for scheduler discovery only.",
    )
    parser.add_argument(
        "--ignore-store",
        action="store_true",
        help="For manual testing, process discovered candidates even if already terminal.",
    )
    parser.add_argument(
        "--discovery-only",
        action="store_true",
        help="Only run Gmail candidate discovery and do not call the model.",
    )
    parser.add_argument(
        "--readiness-check",
        action="store_true",
        help="Check model, Gmail, ClickHouse, labels, and local state before processing.",
    )
    parser.add_argument(
        "--readiness-include-discovery",
        action="store_true",
        help="Also run Gmail unread-project discovery during --readiness-check.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=0,
        help="Run repeatedly at this interval. Default 0 runs once.",
    )
    return parser.parse_args()


def build_run_config(args: argparse.Namespace) -> AgentRunConfig:
    return build_run_config_from_args(
        args,
        allow_db_in_dry_run=not args.block_db_in_dry_run,
    )


async def main_async() -> None:
    args = parse_args()
    support_config = load_config(args.config)
    run_config = build_run_config(args)
    if args.readiness_check:
        model_config = resolve_support_model_config(
            support_config,
            run_config.profile,
        )
        resolved_run_config = resolve_run_config(run_config, model_config)
        report = await run_readiness_checks(
            support_config,
            gguf_path=resolved_run_config.gguf_path or "",
            base_url=resolved_run_config.base_url or "",
            model_backend=resolved_run_config.backend or "llamaserver",
            cloud_api_key_configured=run_config_has_api_key(resolved_run_config),
            include_discovery=args.readiness_include_discovery,
        )
        print(format_readiness_report(report))
        if report["status"] == "blocked":
            raise SystemExit(1)
        return
    store = ProcessedMessageStore(support_config.state.processed_store_path)

    while True:
        result = await run_once(
            support_config=support_config,
            run_config=run_config,
            store=store,
            max_candidates=args.max_candidates,
            max_new=args.max_new,
            retry_failed=args.retry_failed,
            max_retries=args.max_retries,
            live_run=args.live,
            query=args.query,
            ignore_store=args.ignore_store,
            discovery_only=args.discovery_only,
        )
        if result.get("answer"):
            print("\n回答：")
            print(result["answer"])
        if args.interval_seconds <= 0:
            return
        await asyncio.sleep(args.interval_seconds)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
