"""Human-readable summaries for automatic/manual run results."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .processed_message_store import STORE_STATUS_LABELS, format_auto_run_status_text
from .tools.config import NotifyConfig, SupportAgentConfig

SKIPPED_REASON_LABELS = {
    "processing_in_progress": "正在处理中",
    "failed_retry_disabled": "失败且未启用重试",
    "failed_retry_exhausted": "失败重试次数已用尽",
    "terminal_already_processed": "本地已有终态记录",
    "non_project_mail": "非玩家反馈邮件（已忽略）",
}

OUTCOME_STATUS_LABELS = {
    "draft_created": "已创建草稿",
    "human_review": "已转人工处理",
    "failed": "处理失败",
    "skipped": "已跳过（无内容）",
    "processed": "已处理",
}


def _handoff_path(notify: NotifyConfig, message_id: str) -> Path:
    return Path(notify.output_dir) / f"{message_id}.txt"


def _notify_mode_description(notify: NotifyConfig) -> str:
    mode = notify.mode
    if mode == "file":
        return f"写入转人工文件（目录：{notify.output_dir}）"
    if mode == "feishu":
        return "发送到飞书机器人 Webhook"
    if mode == "webhook":
        return "发送到配置的 Webhook"
    if mode == "smtp":
        target = notify.human_support_email or "未配置收件人"
        return f"发送邮件到 {target}"
    if mode == "none":
        return "未启用（notify.mode=none）"
    return f"模式：{mode}"


def _format_notification_line(
    *,
    notify: NotifyConfig,
    message_id: str,
    status: str,
) -> str:
    if status != "human_review" and status != "failed":
        return ""

    path = _handoff_path(notify, message_id)
    if path.exists():
        return f"转人工通知：已写入 {path}"
    if notify.mode == "none":
        return "转人工通知：未启用（模型可能只保存了 case 状态，未调用 notify_human_support）"
    if status == "human_review":
        return (
            f"转人工通知：当前配置为 {_notify_mode_description(notify)}；"
            f"若模型已调用 notify_human_support，文件通常位于 "
            f"{path}"
        )
    return f"转人工通知：失败时由 worker 写入 {_notify_mode_description(notify)}"


def format_outcome_summary(
    outcome: dict[str, Any],
    *,
    email_meta: dict[str, Any] | None = None,
    case_data: dict[str, Any] | None = None,
    notify: NotifyConfig,
) -> str:
    message_id = str(outcome.get("message_id") or "")
    meta = email_meta or {}
    data = case_data or {}
    project = (
        outcome.get("project_label")
        or data.get("project_label")
        or "未知项目"
    )
    status = str(outcome.get("status") or "unknown")
    status_label = OUTCOME_STATUS_LABELS.get(status, status)
    subject = meta.get("subject") or data.get("email_subject") or "（主题未知）"
    sender = meta.get("from") or data.get("player_summary") or "（发件人未知）"
    issue_type = data.get("issue_type") or outcome.get("issue_type") or "未知类型"
    player_text = data.get("language_source_text") or meta.get("snippet") or ""
    labels = outcome.get("labels_applied") or data.get("applied_labels") or []
    draft_id = outcome.get("draft_id") or data.get("draft_id")
    reason = outcome.get("human_review_reason") or data.get("human_review_reason")
    error_message = outcome.get("error_message")

    lines = [
        f"· 项目：{project} | 状态：{status_label}",
        f"  主题：{subject}",
        f"  发件人：{sender}",
        f"  问题类型：{issue_type}",
    ]
    if player_text:
        preview = str(player_text).replace("\n", " ").strip()
        if len(preview) > 160:
            preview = preview[:159] + "…"
        lines.append(f"  玩家反馈：{preview}")
    if labels:
        lines.append(f"  已打标签：{', '.join(str(label) for label in labels)}")
    if draft_id:
        lines.append(f"  草稿 ID：{draft_id}")
    if reason:
        lines.append(f"  转人工原因：{reason}")
    if error_message:
        lines.append(f"  错误：{error_message}")
    notify_line = _format_notification_line(
        notify=notify,
        message_id=message_id,
        status=status,
    )
    if notify_line:
        lines.append(f"  {notify_line}")
    lines.append(f"  message_id：{message_id}")
    return "\n".join(lines)


def format_skipped_candidate_summary(
    detail: dict[str, Any],
    *,
    email_meta: dict[str, Any] | None = None,
    project_label: str | None = None,
) -> str:
    message_id = str(detail.get("message_id") or "")
    store_status = str(detail.get("store_status") or "unknown")
    store_label = STORE_STATUS_LABELS.get(store_status, store_status)
    reason = str(detail.get("reason") or "")
    reason_label = SKIPPED_REASON_LABELS.get(reason, reason)
    gmail_unread = bool(detail.get("gmail_unread"))
    meta = email_meta or {}
    subject = meta.get("subject") or "（主题未知）"
    sender = meta.get("from") or "（发件人未知）"
    project = project_label or "未知项目"
    unread_text = "仍标记未读" if gmail_unread else "已读"

    lines = [
        f"· 主题：{subject}",
        f"  发件人：{sender}",
        f"  项目：{project} | 本地状态：{store_label} | Gmail：{unread_text}",
        f"  跳过原因：{reason_label}",
        f"  message_id：{message_id}",
    ]
    return "\n".join(lines)


def build_run_result_summary(
    result: dict[str, Any],
    *,
    support_config: SupportAgentConfig,
    email_metadata: dict[str, dict[str, Any]] | None = None,
    case_data_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build operator-facing Chinese summary for a run_once result."""

    outcomes = list(result.get("outcomes") or [])
    run_status = str(result.get("status") or "unknown")
    candidate_count = int(result.get("candidate_count") or 0)
    selected_count = int(result.get("selected_count") or 0)
    headline = format_auto_run_status_text(
        run_status,
        outcomes=outcomes or None,
        candidate_count=candidate_count,
        selected_count=selected_count,
    )
    notify = support_config.notify
    metadata = email_metadata or {}
    case_data = case_data_by_id or {}

    lines = [
        f"本轮结果：{headline}",
        f"候选 {candidate_count} 封，实际处理 {selected_count} 封。",
    ]
    if not outcomes:
        if run_status == "already_processed":
            lines.append(
                "说明：发现候选邮件，但均未进入本轮处理（本地已有终态记录或正在处理中）。"
            )
            lines.append(
                "提示：Gmail 仍为未读的邮件会进入处理；"
                "仅已读且本地已有终态/失败记录的邮件会被跳过。"
            )
        elif run_status == "skipped" and selected_count == 0 and candidate_count == 0:
            lines.append("说明：没有新的待处理邮件。")
        elif result.get("error"):
            lines.append(f"错误：{result.get('error')}")

        skipped_details = list(result.get("skipped_details") or [])
        candidates = list(result.get("candidates") or [])
        if skipped_details or (run_status == "already_processed" and candidates):
            project_by_id = {
                str(item.get("message_id")): item.get("project_label")
                for item in candidates
                if item.get("message_id")
            }
            details = skipped_details or [
                {
                    "message_id": item.get("message_id"),
                    "store_status": "unknown",
                    "gmail_unread": False,
                    "reason": "terminal_already_processed",
                }
                for item in candidates
                if item.get("message_id")
            ]
            lines.append("")
            lines.append("候选邮件：")
            for detail in details:
                message_id = str(detail.get("message_id") or "")
                lines.append(
                    format_skipped_candidate_summary(
                        detail,
                        email_meta=metadata.get(message_id),
                        project_label=project_by_id.get(message_id),
                    )
                )
                lines.append("")

        return {
            "headline": headline,
            "text": "\n".join(lines).strip(),
            "outcome_summaries": [],
            "notify_mode": notify.mode,
            "notify_description": _notify_mode_description(notify),
        }

    outcome_summaries: list[str] = []
    for outcome in outcomes:
        message_id = str(outcome.get("message_id") or "")
        block = format_outcome_summary(
            outcome,
            email_meta=metadata.get(message_id),
            case_data=case_data.get(message_id),
            notify=notify,
        )
        outcome_summaries.append(block)
        lines.append("")
        lines.append(block)

    failure_notifications = list(result.get("failure_notifications") or [])
    if failure_notifications:
        lines.append("")
        lines.append("失败自动转人工：")
        for item in failure_notifications:
            message_id = str(item.get("message_id") or "")
            notification = item.get("notification") or {}
            if notification.get("notified") and notification.get("path"):
                lines.append(
                    f"  · {message_id} -> 已写入 {notification.get('path')}"
                )
            elif notification.get("notified"):
                lines.append(
                    f"  · {message_id} -> 已通过 {notification.get('mode')} 通知"
                )
            else:
                lines.append(
                    f"  · {message_id} -> 未通知（{notification.get('error') or notification.get('mode')}）"
                )

    if result.get("answer"):
        lines.extend(["", f"模型总结：{result.get('answer')}"])

    return {
        "headline": headline,
        "text": "\n".join(lines).strip(),
        "outcome_summaries": outcome_summaries,
        "notify_mode": notify.mode,
        "notify_description": _notify_mode_description(notify),
    }