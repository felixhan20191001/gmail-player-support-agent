"""Helpers for recognizing whether player feedback text is substantive."""

from __future__ import annotations

import html
import re


_FORM_MARKER_RE = re.compile(
    r"(?:my\s+question\s+is|question|message|my\s+message\s+is)\s*:\s*",
    re.IGNORECASE,
)
_METADATA_RE = re.compile(
    r"\b(?:platform|ver|version|userid|user\s*id)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_MEANINGFUL_CHAR_RE = re.compile(r"[A-Za-z0-9\u3400-\u9fff]")
_SIGNATURE_PATTERNS = (
    re.compile(r"^sent from my (?:iphone|ipad|android)(?:\s|$)", re.IGNORECASE),
    re.compile(r"^inviato da iphone(?:\s|$)", re.IGNORECASE),
)


def extract_player_feedback_text(text: str | None) -> str:
    """Return the free-form feedback portion, excluding form metadata."""

    cleaned = html.unescape(str(text or "")).replace("\r", "\n")
    marker_matches = list(_FORM_MARKER_RE.finditer(cleaned))
    if marker_matches:
        cleaned = cleaned[marker_matches[-1].end() :]
    cleaned = _METADATA_RE.sub(" ", cleaned)
    cleaned = re.sub(
        r"\bI\s+need\s+some\s+help\.?\s*$",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.strip(" \t\n\r:;,.!?-_/|")


def has_substantive_feedback_text(text: str | None) -> bool:
    """Return true when text contains a concrete player issue or request."""

    feedback = extract_player_feedback_text(text)
    if not feedback:
        return False
    if any(pattern.search(feedback) for pattern in _SIGNATURE_PATTERNS):
        return False

    words = _WORD_RE.findall(feedback)
    meaningful_chars = _MEANINGFUL_CHAR_RE.findall(feedback)
    cjk_chars = _CJK_RE.findall(feedback)
    if len(words) >= 4:
        return True
    if len(words) >= 2 and len(meaningful_chars) >= 12:
        return True
    if len(cjk_chars) >= 8:
        return True
    return len(meaningful_chars) >= 24
