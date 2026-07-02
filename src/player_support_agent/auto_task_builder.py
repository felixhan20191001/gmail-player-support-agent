"""Build natural-language auto-processing tasks for the agent."""

from __future__ import annotations

from typing import Any

from .agent_runner import LIVE_CONFIRMATION

def _format_task_list(value: Any) -> str:
    if not isinstance(value, list):
        return "[]"
    return str([str(item) for item in value if item is not None])


def build_auto_task(
    messages: list[dict[str, Any]],
    *,
    live_run: bool = False,
) -> str:
    """Build the only task payload the scheduler gives to the model."""

    mode = (
        f"LIVE auto processing. Confirmation phrase present: {LIVE_CONFIRMATION}. "
        "Gmail draft, existing-label, read-state, and case-state tools are enabled; "
        "sending email remains forbidden."
        if live_run
        else (
            "DRY-RUN auto processing. Simulate Gmail writes/state writes, but still "
            "complete the same analysis and final case-state path."
        )
    )
    lines = [
        mode,
        "",
        "Process these scheduler-selected Gmail player-feedback messages.",
        "The scheduler only discovered and deduplicated candidates; all support decisions stay inside the tool workflow.",
        "Support workflow details live in the system prompt. Do not restate them; execute them.",
        "",
        "Messages:",
    ]
    for item in messages:
        line = f"- message_id={item['message_id']} thread_id={item.get('thread_id', '')}"
        if item.get("project_label"):
            line += f" project_label={item['project_label']}"
        if item.get("matched_labels"):
            line += f" matched_labels={item['matched_labels']}"
        if item.get("reprocess_gmail_unread"):
            line += " reprocess_gmail_unread=true"
            if item.get("existing_status"):
                line += f" existing_status={item['existing_status']}"
            if item.get("existing_draft_id"):
                line += f" existing_draft_id={item['existing_draft_id']}"
            if item.get("existing_issue_type"):
                line += f" existing_issue_type={item['existing_issue_type']}"
            if item.get("existing_recommended_labels"):
                line += (
                    " existing_recommended_labels="
                    f"{_format_task_list(item.get('existing_recommended_labels'))}"
                )
            if item.get("existing_labels_applied"):
                line += (
                    " existing_labels_applied="
                    f"{_format_task_list(item.get('existing_labels_applied'))}"
                )
        lines.append(line)
    if any(item.get("reprocess_gmail_unread") for item in messages):
        lines += [
            "",
            "cleanup-only reprocess: local terminal state already exists but Gmail is still UNREAD.",
            "For these messages, do not call read_email_thread, get_existing_gmail_labels unless required by a prerequisite nudge, extract_feedback_claim, get_relevant_support_rules, review_reply_draft, or create_gmail_draft.",
            "In cleanup-only mode, do not call create_gmail_draft or rewrite the reply.",
            "If existing_status=draft_created and existing_draft_id is present, preserve that draft_id.",
            "Use existing_recommended_labels with apply_existing_gmail_labels when available, then mark_gmail_messages_read, then save_case_state with the existing terminal status and compact data.",
        ]
    lines += [
        "",
        "For normal messages, process each message independently through the required support workflow.",
        "Always save final state with case_id equal to the inbound message_id.",
        "Allowed final statuses: draft_created, human_review, failed, skipped.",
        "When applying labels, use the exact recommended_labels from extract_feedback_claim.",
        "When a draft or label-only skip is completed, call apply_existing_gmail_labels when labels are needed, then mark_gmail_messages_read before save_case_state.",
        "Keep saved data compact but include project_label, matched_labels, thread_id, issue_type, applied_labels, draft_id or handoff reason when known.",
    ]
    return "\n".join(lines)
