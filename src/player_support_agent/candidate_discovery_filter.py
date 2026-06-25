"""Discovery-time filters for non-player-feedback Gmail candidates."""

from __future__ import annotations

from email.utils import getaddresses
from typing import Any

from .tools.config import GmailConfig
from .tools.gmail_tools import GmailTools

SKIP_CATEGORY_NON_PROJECT = "non_project"


def _normalized_project_names(project_names: list[str]) -> set[str]:
    return {name.strip().lower() for name in project_names if str(name).strip()}


def extract_sender_email(from_header: str) -> str:
    addresses = [
        email.strip().lower()
        for _name, email in getaddresses([str(from_header or "")])
        if email.strip()
    ]
    return addresses[0] if addresses else ""


def sender_matches_non_project_patterns(
    from_header: str,
    patterns: list[str],
) -> bool:
    sender = extract_sender_email(from_header)
    if not sender:
        return False
    domain = sender.split("@", 1)[-1]
    for raw_pattern in patterns:
        pattern = str(raw_pattern or "").strip().lower()
        if not pattern:
            continue
        if "@" in pattern:
            if sender == pattern:
                return True
        elif domain == pattern or sender.endswith(f"@{pattern}"):
            return True
    return False


def project_name_in_subject(subject: str, project_names: list[str]) -> bool:
    subject_lower = str(subject or "").lower()
    if not subject_lower:
        return False
    for name in _normalized_project_names(project_names):
        if name and name in subject_lower:
            return True
    return False


def candidate_has_project_association(
    candidate: dict[str, Any],
    project_names: list[str],
) -> bool:
    if candidate.get("project_label"):
        return True
    normalized = _normalized_project_names(project_names)
    for label in candidate.get("matched_labels") or []:
        label_text = str(label)
        parent = label_text.split("/", 1)[0]
        if label_text.lower() in normalized or parent.lower() in normalized:
            return True
    return False


def should_skip_as_non_project(
    candidate: dict[str, Any],
    metadata: dict[str, Any],
    gmail_config: GmailConfig,
) -> bool:
    """Return True when discovery should ignore a message without model processing."""

    project_names = gmail_config.project_label_names
    if candidate_has_project_association(candidate, project_names):
        return False

    from_header = str(metadata.get("from") or "")
    if sender_matches_non_project_patterns(
        from_header,
        gmail_config.non_project_sender_patterns,
    ):
        return True

    if not gmail_config.skip_non_project_candidates:
        return False

    if project_name_in_subject(str(metadata.get("subject") or ""), project_names):
        return False

    if metadata.get("project_labels"):
        return False

    for label_name in metadata.get("label_names") or []:
        parent = str(label_name).split("/", 1)[0]
        normalized = _normalized_project_names(project_names)
        if str(label_name).lower() in normalized or parent.lower() in normalized:
            return False

    return True


async def partition_player_feedback_candidates(
    gmail_config: GmailConfig,
    candidates: list[dict[str, Any]],
    *,
    gmail: GmailTools | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split discovered candidates into player feedback vs non-project mail."""

    if not candidates:
        return [], []

    if (
        not gmail_config.skip_non_project_candidates
        and not gmail_config.non_project_sender_patterns
    ):
        return candidates, []

    needs_inspection = [
        candidate
        for candidate in candidates
        if not candidate_has_project_association(
            candidate,
            gmail_config.project_label_names,
        )
    ]
    if not needs_inspection:
        return candidates, []

    owns_gmail = gmail is None
    gmail = gmail or GmailTools(gmail_config)
    metadata_by_id: dict[str, dict[str, Any]] = {}
    try:
        message_ids = [
            str(candidate.get("message_id"))
            for candidate in needs_inspection
            if candidate.get("message_id")
        ]
        metadata_by_id = await gmail.get_message_discovery_metadata(message_ids)
    finally:
        if owns_gmail:
            await gmail.aclose()

    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate_has_project_association(candidate, gmail_config.project_label_names):
            kept.append(candidate)
            continue
        message_id = str(candidate.get("message_id") or "")
        metadata = metadata_by_id.get(message_id, {})
        if should_skip_as_non_project(candidate, metadata, gmail_config):
            skipped.append(
                {
                    **candidate,
                    "discovery_metadata": {
                        "subject": metadata.get("subject"),
                        "from": metadata.get("from"),
                        "project_labels": metadata.get("project_labels") or [],
                    },
                }
            )
            continue
        kept.append(candidate)
    return kept, skipped