"""Shared state between tool classes.

This module provides a simple shared state object that allows tool classes
to exchange information without changing their public APIs.
"""

from __future__ import annotations

from typing import Any


class ToolSharedState:
    """Lightweight shared state for tool coordination.

    Currently used to:
    - Pass the last extract_feedback_claim result to apply_existing_gmail_labels
      so that label applications can be checked against recommended_labels.
    - Pass the last read_email_thread thread_id and sender email to
      create_gmail_draft so that hallucinated thread_ids or recipients can be
      corrected when the Gmail API returns 404.
    """

    def __init__(self) -> None:
        self._last_extract_claim: dict[str, Any] = {}
        self._last_thread_id: str | None = None
        self._last_sender_email: str | None = None

    def set_last_extract_claim(self, payload: dict[str, Any]) -> None:
        self._last_extract_claim = dict(payload)

    def get_last_extract_claim(self) -> dict[str, Any]:
        return dict(self._last_extract_claim)

    def get_recommended_labels(self) -> list[str]:
        return list(self._last_extract_claim.get("recommended_labels") or [])

    def set_last_thread_context(self, thread_id: str, sender_email: str | None) -> None:
        self._last_thread_id = thread_id
        self._last_sender_email = sender_email

    def get_last_thread_id(self) -> str | None:
        return self._last_thread_id

    def get_last_sender_email(self) -> str | None:
        return self._last_sender_email
