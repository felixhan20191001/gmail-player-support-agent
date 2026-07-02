"""Gmail REST tools.

The tools only apply existing labels and create drafts. They do not create
labels and they do not send messages.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from email.utils import getaddresses
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from forge.errors import ToolResolutionError

from .config import GmailConfig
from .tool_shared_state import ToolSharedState

GMAIL_UNREAD_LABEL_ID = "UNREAD"
_SENDER_EMAIL_RE = re.compile(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[^@\s<>\"']+$")


def normalize_sender_email(value: str) -> str:
    """Validate and normalize a sender email for Gmail ``from:`` search."""

    email = str(value or "").strip().lower()
    if not email or not _SENDER_EMAIL_RE.match(email):
        raise ValueError("invalid sender email")
    return email


def build_sender_feedback_query(base_query: str, sender_email: str) -> str:
    """Append a Gmail ``from:`` filter to a discovery query."""

    sender = normalize_sender_email(sender_email)
    base = str(base_query or "").strip()
    if not base:
        return f"from:{sender}"
    return f"{base} from:{sender}"


GMAIL_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


def _b64url_decode(data: str | None) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode(
        "utf-8", errors="replace"
    )


def _b64url_encode_bytes(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {
        h.get("name", "").lower(): h.get("value", "")
        for h in payload.get("headers", [])
    }


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _collect_body_text(payload: dict[str, Any]) -> str:
    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})
    parts = payload.get("parts", [])

    if mime_type == "text/plain":
        return _b64url_decode(body.get("data"))
    if mime_type == "text/html":
        return _strip_html(_b64url_decode(body.get("data")))

    collected: list[str] = []
    for part in parts:
        text = _collect_body_text(part)
        if text:
            collected.append(text)
    return "\n\n".join(collected)


def project_parent_label(label_name: str) -> str:
    """Return the Gmail parent label used as project name."""

    return label_name.split("/", 1)[0].strip()


def _quote_gmail_label(label_name: str) -> str:
    escaped = label_name.replace("\\", "\\\\").replace('"', '\\"')
    return f'label:"{escaped}"'


def _compact_text(text: str, *, limit: int) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def _first_email_address(value: str | None) -> str:
    if not value:
        return ""
    for _name, address in getaddresses([value]):
        address = address.strip()
        if "@" in address:
            return address
    return ""


def _email_key(value: str | None) -> str:
    return _first_email_address(value).lower()


def _reply_recipient_from_messages(
    messages: list[dict[str, Any]],
    *,
    account_email: str | None,
) -> str:
    account_key = _email_key(account_email)
    fallback = ""
    for msg in reversed(messages):
        payload = msg.get("payload")
        headers = _headers(payload) if isinstance(payload, dict) else msg
        sender = headers.get("from", "")
        recipient = _first_email_address(headers.get("reply-to") or sender)
        if not recipient:
            continue
        fallback = fallback or recipient
        if account_key and _email_key(sender) == account_key:
            continue
        return recipient
    return fallback


def _safe_url(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _connection_error_hint(exc: httpx.HTTPError) -> str:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return (
            " Hint: ensure this machine can reach Google APIs (stable internet, VPN, "
            "or HTTPS_PROXY/ALL_PROXY). Transient outages are retried automatically."
        )
    if isinstance(exc, httpx.ReadTimeout):
        return " Hint: Gmail API response timed out; retry or increase gmail.request_timeout_seconds."
    return ""


def _raise_http_error(action: str, url: str, exc: httpx.HTTPError) -> None:
    safe_url = _safe_url(url)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        raise RuntimeError(f"{action} failed: HTTP {status} from {safe_url}") from exc
    raise RuntimeError(
        f"{action} failed: {type(exc).__name__} while connecting to {safe_url}"
        f"{_connection_error_hint(exc)}"
    ) from exc


class GmailTools:
    """Thin Gmail API wrapper for Forge tools."""

    def __init__(
        self,
        config: GmailConfig,
        shared_state: ToolSharedState | None = None,
        *,
        compact_results: bool = False,
    ) -> None:
        self.config = config
        self._label_cache: dict[str, str] | None = None
        self._label_details: dict[str, dict[str, Any]] = {}
        self._cached_access_token: str | None = None
        self._cached_access_token_expires_at: float = 0.0
        self._http_client: httpx.AsyncClient | None = None
        self._shared_state = shared_state or ToolSharedState()
        self.compact_results = compact_results

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _http_timeout(self) -> httpx.Timeout:
        seconds = max(5.0, float(self.config.request_timeout_seconds))
        connect_timeout = min(20.0, seconds)
        return httpx.Timeout(seconds, connect=connect_timeout)

    async def _client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=self._http_timeout(),
                trust_env=True,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._http_client

    async def _request(
        self,
        method: str,
        url: str,
        *,
        action: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> httpx.Response:
        client = await self._client()
        max_attempts = max(1, int(self.config.max_request_retries) + 1)
        last_exc: httpx.HTTPError | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json,
                    data=data,
                )
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError:
                raise
            except GMAIL_RETRYABLE_ERRORS as exc:
                last_exc = exc
                if attempt >= max_attempts:
                    break
                await asyncio.sleep(self.config.retry_backoff_seconds * attempt)
            except httpx.HTTPError as exc:
                _raise_http_error(action, url, exc)
        assert last_exc is not None
        _raise_http_error(action, url, last_exc)
        raise AssertionError("unreachable")

    async def _access_token(self) -> str:
        if (
            self._cached_access_token
            and time.time() < self._cached_access_token_expires_at - 60
        ):
            return self._cached_access_token

        if self.config.has_refresh_credentials():
            payload = {
                "client_id": self.config.resolve_client_id(),
                "client_secret": self.config.resolve_client_secret(),
                "refresh_token": self.config.resolve_refresh_token(),
                "grant_type": "refresh_token",
            }
            resp = await self._request(
                "POST",
                self.config.oauth_token_url,
                action="Gmail OAuth token refresh",
                data=payload,
            )
            data = resp.json()
            token = data["access_token"]
            expires_in = int(data.get("expires_in", 3600))
            self._cached_access_token = token
            self._cached_access_token_expires_at = time.time() + expires_in
            return token

        return self.config.resolve_access_token()

    async def _headers(self) -> dict[str, str]:
        token = await self._access_token()
        return {"Authorization": f"Bearer {token}"}

    def _user_path(self) -> str:
        return quote(self.config.user_id, safe="")

    async def get_message_internal_dates(
        self,
        message_ids: list[str],
    ) -> dict[str, str]:
        """Return Gmail internalDate values for lightweight candidate ordering."""

        dates: dict[str, str] = {}
        if not message_ids:
            return dates

        url_base = f"{self.config.api_base_url}/users/{self._user_path()}/messages"
        for message_id in message_ids:
            url = f"{url_base}/{quote(message_id, safe='')}"
            resp = await self._request(
                "GET",
                url,
                action="Gmail message metadata read",
                headers=await self._headers(),
                params={"format": "metadata"},
            )
            internal_date = resp.json().get("internalDate")
            if internal_date:
                dates[message_id] = str(internal_date)
        return dates

    async def get_message_summaries(
        self,
        message_ids: list[str],
    ) -> dict[str, dict[str, str]]:
        """Return safe Gmail metadata for operator-facing run summaries."""

        summaries: dict[str, dict[str, str]] = {}
        if not message_ids:
            return summaries

        url_base = f"{self.config.api_base_url}/users/{self._user_path()}/messages"
        for message_id in message_ids:
            url = f"{url_base}/{quote(message_id, safe='')}"
            resp = await self._request(
                "GET",
                url,
                action="Gmail message metadata read",
                headers=await self._headers(),
                params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "Subject", "Date"],
                },
            )
            data = resp.json()
            payload = data.get("payload", {})
            headers = _headers(payload)
            summaries[message_id] = {
                "subject": headers.get("subject", ""),
                "from": headers.get("from", ""),
                "date": headers.get("date", ""),
                "snippet": _compact_text(data.get("snippet", ""), limit=240),
            }
        return summaries

    async def get_message_discovery_metadata(
        self,
        message_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Return subject/from and configured project label hints for discovery."""

        metadata: dict[str, dict[str, Any]] = {}
        if not message_ids:
            return metadata

        if self._label_cache is None:
            await self.get_existing_gmail_labels()
        id_to_name = {
            label_id: name for name, label_id in (self._label_cache or {}).items()
        }

        url_base = f"{self.config.api_base_url}/users/{self._user_path()}/messages"
        for message_id in message_ids:
            url = f"{url_base}/{quote(message_id, safe='')}"
            resp = await self._request(
                "GET",
                url,
                action="Gmail message metadata read",
                headers=await self._headers(),
                params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "Subject", "Date"],
                },
            )
            data = resp.json()
            payload = data.get("payload", {})
            headers = _headers(payload)
            label_ids = data.get("labelIds", [])
            label_names = [id_to_name.get(label_id, label_id) for label_id in label_ids]
            metadata[message_id] = {
                "subject": headers.get("subject", ""),
                "from": headers.get("from", ""),
                "date": headers.get("date", ""),
                "snippet": _compact_text(data.get("snippet", ""), limit=240),
                "label_names": label_names,
                "project_labels": self._project_labels_from_label_names(label_names),
            }
        return metadata

    async def get_unread_message_ids(self, message_ids: list[str]) -> set[str]:
        """Return message ids that still carry Gmail's UNREAD label."""

        unread: set[str] = set()
        if not message_ids:
            return unread

        url_base = f"{self.config.api_base_url}/users/{self._user_path()}/messages"
        for message_id in message_ids:
            url = f"{url_base}/{quote(message_id, safe='')}"
            resp = await self._request(
                "GET",
                url,
                action="Gmail message metadata read",
                headers=await self._headers(),
                params={"format": "metadata"},
            )
            if GMAIL_UNREAD_LABEL_ID in resp.json().get("labelIds", []):
                unread.add(message_id)
        return unread

    async def list_new_feedback_emails(
        self,
        max_results: int = 10,
        query: str | None = None,
    ) -> dict[str, Any]:
        """List new feedback message ids from Gmail."""

        q = query or self.config.feedback_query
        params: dict[str, Any] = {"q": q, "maxResults": max_results}
        url = f"{self.config.api_base_url}/users/{self._user_path()}/messages"
        resp = await self._request(
            "GET",
            url,
            action="Gmail message search",
            headers=await self._headers(),
            params=params,
        )
        data = resp.json()
        return {
            "query": q,
            "messages": data.get("messages", []),
            "result_size_estimate": data.get("resultSizeEstimate", 0),
            "next_page_token": data.get("nextPageToken"),
        }

    async def list_unread_inbox_emails(
        self,
        max_results: int = 25,
        query: str | None = None,
        snippet_chars: int = 240,
    ) -> dict[str, Any]:
        """List unread inbox messages with safe metadata and snippets.

        This tool is intended for interactive mailbox questions. It does not
        read full message bodies and never mutates Gmail.
        """

        q = query or self.config.feedback_query
        listing = await self.list_new_feedback_emails(
            max_results=max_results,
            query=q,
        )
        if self._label_cache is None:
            await self.get_existing_gmail_labels()
        id_to_name = {
            label_id: name
            for name, label_id in (self._label_cache or {}).items()
        }

        emails: list[dict[str, Any]] = []
        url_base = f"{self.config.api_base_url}/users/{self._user_path()}/messages"
        for item in listing.get("messages", [])[:max_results]:
            message_id = item.get("id") or item.get("message_id")
            if not message_id:
                continue
            url = f"{url_base}/{quote(message_id, safe='')}"
            resp = await self._request(
                "GET",
                url,
                action="Gmail message metadata read",
                headers=await self._headers(),
                params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "Subject", "Date"],
                },
            )
            data = resp.json()
            payload = data.get("payload", {})
            headers = _headers(payload)
            label_ids = data.get("labelIds", [])
            label_names = [id_to_name.get(label_id, label_id) for label_id in label_ids]
            project_labels = self._project_labels_from_label_names(label_names)
            emails.append(
                {
                    "message_id": data.get("id", message_id),
                    "thread_id": data.get("threadId") or item.get("threadId"),
                    "subject": headers.get("subject", ""),
                    "from": headers.get("from", ""),
                    "date": headers.get("date", ""),
                    "internal_date": data.get("internalDate"),
                    "snippet": _compact_text(
                        data.get("snippet", ""),
                        limit=max(40, min(snippet_chars, 500)),
                    ),
                    "label_names": label_names,
                    "project_labels": project_labels,
                }
            )

        return {
            "query": q,
            "messages": emails,
            "returned_count": len(emails),
            "result_size_estimate": listing.get("result_size_estimate", 0),
            "next_page_token": listing.get("next_page_token"),
        }

    async def list_unread_project_emails(
        self,
        max_results_per_label: int = 10,
        project_labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Discover unread inbox messages under existing project labels."""

        labels_data = await self.get_existing_gmail_labels()
        labels_by_parent: dict[str, list[str]] = labels_data["project_labels_by_parent"]
        parents = project_labels or labels_data["project_parent_labels"]
        candidates_by_id: dict[str, dict[str, Any]] = {}
        scanned_queries: list[str] = []

        for parent in parents:
            label_names = labels_by_parent.get(parent, [])
            if not label_names:
                continue
            if not self.config.scan_child_project_labels and parent in label_names:
                label_names = [parent]
            for label_name in label_names:
                query = (
                    f"{self.config.feedback_query.strip()} "
                    f"{_quote_gmail_label(label_name)}"
                )
                scanned_queries.append(query)
                listing = await self.list_new_feedback_emails(
                    max_results=max_results_per_label,
                    query=query,
                )
                for item in listing.get("messages", []):
                    message_id = item.get("id")
                    thread_id = item.get("threadId")
                    if not message_id or not thread_id:
                        continue
                    existing = candidates_by_id.setdefault(
                        message_id,
                        {
                            "message_id": message_id,
                            "thread_id": thread_id,
                            "project_label": parent,
                            "matched_labels": [],
                        },
                    )
                    if label_name not in existing["matched_labels"]:
                        existing["matched_labels"].append(label_name)

        return {
            "messages": list(candidates_by_id.values()),
            "result_size_estimate": len(candidates_by_id),
            "project_parent_labels": parents,
            "scanned_label_count": len(scanned_queries),
            "scanned_queries": scanned_queries,
        }

    async def read_email_thread(self, thread_id: str) -> dict[str, Any]:
        """Read a Gmail thread and return normalized text for each message."""

        url = (
            f"{self.config.api_base_url}/users/{self._user_path()}"
            f"/threads/{quote(thread_id, safe='')}"
        )
        resp = await self._request(
            "GET",
            url,
            action="Gmail thread read",
            headers=await self._headers(),
            params={"format": "full"},
        )
        data = resp.json()

        if self._label_cache is None:
            await self.get_existing_gmail_labels()
        id_to_name = {
            label_id: name
            for name, label_id in (self._label_cache or {}).items()
        }
        messages: list[dict[str, Any]] = []
        for msg in data.get("messages", []):
            payload = msg.get("payload", {})
            headers = _headers(payload)
            label_ids = msg.get("labelIds", [])
            label_names = [id_to_name.get(label_id, label_id) for label_id in label_ids]
            project_labels = self._project_labels_from_label_names(label_names)
            messages.append(
                {
                    "id": msg.get("id"),
                    "thread_id": msg.get("threadId"),
                    "internal_date": msg.get("internalDate"),
                    "from": headers.get("from", ""),
                    "reply_to": headers.get("reply-to", ""),
                    "to": headers.get("to", ""),
                    "subject": headers.get("subject", ""),
                    "date": headers.get("date", ""),
                    "message_id_header": headers.get("message-id", ""),
                    "body": _collect_body_text(payload),
                    "label_ids": label_ids,
                    "label_names": label_names,
                    "project_labels": project_labels,
                    "snippet": msg.get("snippet", ""),
                }
            )

        result = {
            "thread_id": data.get("id", thread_id),
            "history_id": data.get("historyId"),
            "messages": messages,
        }

        sender_email: str | None = None
        for msg in messages:
            sender = _first_email_address(msg.get("from", ""))
            if sender:
                sender_email = sender
                break
        self._shared_state.set_last_thread_context(
            result["thread_id"], sender_email,
        )

        return result

    async def _reply_recipient_for_thread(self, thread_id: str) -> str:
        url = (
            f"{self.config.api_base_url}/users/{self._user_path()}"
            f"/threads/{quote(thread_id, safe='')}"
        )
        resp = await self._request(
            "GET",
            url,
            action="Gmail thread metadata read",
            headers=await self._headers(),
            params={
                "format": "metadata",
                "metadataHeaders": ["From", "Reply-To"],
            },
        )
        data = resp.json()

        recipient = _reply_recipient_from_messages(
            data.get("messages", []),
            account_email=self.config.account_email,
        )
        if not recipient:
            raise ToolResolutionError(
                "Cannot resolve original sender for reply draft"
            )
        return recipient

    async def get_existing_gmail_labels(self) -> dict[str, Any]:
        """Return existing Gmail labels and cache their name to id mapping."""

        url = f"{self.config.api_base_url}/users/{self._user_path()}/labels"
        resp = await self._request(
            "GET",
            url,
            action="Gmail label list",
            headers=await self._headers(),
        )
        data = resp.json()

        labels = data.get("labels", [])
        self._label_cache = {
            label["name"]: label["id"]
            for label in labels
            if "name" in label and "id" in label
        }
        self._label_details = {
            label["name"]: label
            for label in labels
            if "name" in label and "id" in label
        }
        project_labels_by_parent = self._project_labels_by_parent()
        result: dict[str, Any] = {
            "allowed_label_names": self.config.allowed_label_names,
            "project_parent_labels": sorted(project_labels_by_parent),
            "project_labels_by_parent": project_labels_by_parent,
        }
        if self.compact_results:
            result["label_names"] = sorted(self._label_cache)
            return result
        result["labels"] = labels
        return result

    def _configured_or_discovered_project_parents(self) -> set[str]:
        if self.config.project_label_names:
            return {name.strip() for name in self.config.project_label_names if name.strip()}
        return {
            project_parent_label(name)
            for name, label in self._label_details.items()
            if label.get("type") == "user"
        }

    def _project_labels_from_label_names(self, label_names: list[str]) -> list[str]:
        configured_project_parents = self._configured_or_discovered_project_parents()
        return sorted(
            {
                parent
                for name in label_names
                for parent in [project_parent_label(name)]
                if self._label_details.get(name, {}).get("type") == "user"
                and parent in configured_project_parents
            }
        )

    def _project_labels_by_parent(self) -> dict[str, list[str]]:
        parents = self._configured_or_discovered_project_parents()
        grouped: dict[str, list[str]] = {parent: [] for parent in parents}
        for name, label in self._label_details.items():
            if label.get("type") != "user":
                continue
            parent = project_parent_label(name)
            if parent in parents:
                grouped.setdefault(parent, []).append(name)
        return {
            parent: sorted(labels, key=lambda value: ("/" in value, value))
            for parent, labels in sorted(grouped.items())
        }

    def _is_allowed_existing_project_label(self, name: str) -> bool:
        if not self.config.allow_existing_project_labels:
            return False
        label = self._label_details.get(name)
        if not label or label.get("type") != "user":
            return False
        return project_parent_label(name) in self._configured_or_discovered_project_parents()

    async def _label_ids_for_names(self, label_names: list[str]) -> list[str]:
        if self._label_cache is None:
            await self.get_existing_gmail_labels()
        assert self._label_cache is not None

        allowed = set(self.config.allowed_label_names)
        if not allowed and not self.config.allow_existing_project_labels:
            raise ToolResolutionError(
                "No gmail.allowed_label_names configured; refusing to modify labels"
            )

        label_ids: list[str] = []
        for name in label_names:
            if name not in allowed and not self._is_allowed_existing_project_label(name):
                raise ToolResolutionError(
                    f"Label {name!r} is not configured or under an existing project label"
                )
            label_id = self._label_cache.get(name)
            if label_id is None:
                raise ToolResolutionError(f"Configured label {name!r} does not exist")
            label_ids.append(label_id)
        return label_ids

    async def apply_existing_gmail_labels(
        self,
        message_ids: list[str],
        label_names: list[str],
    ) -> dict[str, Any]:
        """Apply only pre-existing, configured Gmail labels to messages."""

        if not message_ids:
            raise ToolResolutionError("message_ids is required")

        requested_labels: list[str] = []
        rejected_input_labels: list[dict[str, str]] = []
        for raw_name in label_names:
            name = str(raw_name or "").strip()
            if not name:
                rejected_input_labels.append(
                    {"label": "", "error": "label_names must not contain blank labels"}
                )
                continue
            requested_labels.append(name)

        if rejected_input_labels or not requested_labels:
            if not requested_labels and not rejected_input_labels:
                rejected_input_labels.append(
                    {"label": "", "error": "label_names is required"}
                )
            return {
                "message_ids": message_ids,
                "applied_labels": [],
                "rejected_labels": rejected_input_labels,
                "partial_success": False,
                "next_steps": [
                    "Call apply_existing_gmail_labels with the exact non-empty label_names "
                    "from extract_feedback_claim.recommended_labels.",
                    "Do not rely on tool fallback labels.",
                ],
            }

        last_extract_claim = self._shared_state.get_last_extract_claim()
        recommended_labels = [
            str(label).strip()
            for label in last_extract_claim.get("recommended_labels", [])
            if str(label).strip()
        ]
        if (
            last_extract_claim
            and "recommended_labels" in last_extract_claim
            and not recommended_labels
        ):
            return {
                "message_ids": message_ids,
                "applied_labels": [],
                "rejected_labels": [
                    {
                        "label": ", ".join(requested_labels),
                        "error": (
                            "extract_feedback_claim.recommended_labels is empty; "
                            "refusing to apply labels"
                        ),
                    }
                ],
                "recommended_labels": [],
                "partial_success": False,
                "next_steps": [
                    "Do not apply labels when extract_feedback_claim returned no recommended_labels.",
                    "Call save_case_state with a failed or human_review outcome, or re-run "
                    "extract_feedback_claim once if the case_type was clearly wrong.",
                ],
            }
        if recommended_labels and sorted(requested_labels) != sorted(recommended_labels):
            return {
                "message_ids": message_ids,
                "applied_labels": [],
                "rejected_labels": [
                    {
                        "label": ", ".join(requested_labels),
                        "error": (
                            "label_names must exactly match "
                            "extract_feedback_claim.recommended_labels"
                        ),
                    }
                ],
                "recommended_labels": recommended_labels,
                "partial_success": False,
                "next_steps": [
                    "Retry apply_existing_gmail_labels once with exactly the recommended_labels "
                    "returned by extract_feedback_claim.",
                    "If labels still cannot be applied, call save_case_state with the failure "
                    "or handoff state instead of inventing labels.",
                ],
            }

        async def _try_apply(
            names: list[str],
        ) -> tuple[list[str], list[dict[str, str]], list[str]]:
            applied: list[str] = []
            rejected: list[dict[str, str]] = []
            ids: list[str] = []
            for name in names:
                try:
                    label_ids = await self._label_ids_for_names([name])
                except ToolResolutionError as exc:
                    rejected.append({"label": name, "error": str(exc)})
                    continue
                applied.append(name)
                ids.extend(label_ids)
            return applied, rejected, ids

        applied_labels, rejected_labels, add_ids = await _try_apply(requested_labels)

        if not add_ids:
            return {
                "message_ids": message_ids,
                "applied_labels": [],
                "rejected_labels": rejected_labels,
                "partial_success": False,
                "next_steps": [
                    "Use only extract_feedback_claim.recommended_labels (exact list from extract) or labels "
                    "returned by get_existing_gmail_labels. Do not substitute e.g. 功能建议 for 存档转移.",
                    "If a Gmail draft was already created, call save_case_state "
                    "immediately with status=draft_created and the draft_id.",
                    "Do not restart read_email_thread or extract_feedback_claim.",
                ],
            }

        url = (
            f"{self.config.api_base_url}/users/{self._user_path()}"
            "/messages/batchModify"
        )
        body = {
            "ids": message_ids,
            "addLabelIds": add_ids,
        }
        await self._request(
            "POST",
            url,
            action="Gmail label application",
            headers=await self._headers(),
            json=body,
        )

        payload: dict[str, Any] = {
            "message_ids": message_ids,
            "applied_labels": applied_labels,
            "partial_success": bool(rejected_labels),
        }
        if rejected_labels:
            payload["rejected_labels"] = rejected_labels
            payload["next_steps"] = [
                "Some labels were skipped. Apply using exactly extract_feedback_claim.recommended_labels, "
                "then call save_case_state with the applied ones.",
            ]
        return payload

    async def mark_gmail_messages_read(self, message_ids: list[str]) -> dict[str, Any]:
        """Mark Gmail messages as read by removing the UNREAD system label."""

        if not message_ids:
            raise ToolResolutionError("message_ids is required")

        url = (
            f"{self.config.api_base_url}/users/{self._user_path()}"
            "/messages/batchModify"
        )
        body = {
            "ids": message_ids,
            "removeLabelIds": [GMAIL_UNREAD_LABEL_ID],
        }
        await self._request(
            "POST",
            url,
            action="Gmail mark messages read",
            headers=await self._headers(),
            json=body,
        )

        return {
            "message_ids": message_ids,
            "marked_read": True,
        }

    async def create_gmail_draft(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        in_reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a Gmail draft. This tool never sends the draft."""

        requested_to = to
        recipient_source = "model_argument"
        resolved_thread_id = thread_id

        known_thread_id = self._shared_state.get_last_thread_id()
        if thread_id and known_thread_id and thread_id != known_thread_id:
            resolved_thread_id = known_thread_id

        if resolved_thread_id:
            try:
                to = await self._reply_recipient_for_thread(resolved_thread_id)
                recipient_source = "thread_reply_to"
            except Exception:
                fallback_sender = self._shared_state.get_last_sender_email()
                if fallback_sender:
                    to = fallback_sender
                    recipient_source = "cached_sender_fallback"
                resolved_thread_id = known_thread_id or resolved_thread_id

        msg = EmailMessage()
        if self.config.account_email:
            msg["From"] = self.config.account_email
        msg["To"] = to
        msg["Subject"] = subject
        if in_reply_to_message_id:
            msg["In-Reply-To"] = in_reply_to_message_id
            msg["References"] = in_reply_to_message_id
        msg.set_content(body)

        payload: dict[str, Any] = {
            "message": {"raw": _b64url_encode_bytes(msg.as_bytes())}
        }
        if resolved_thread_id:
            payload["message"]["threadId"] = resolved_thread_id

        url = f"{self.config.api_base_url}/users/{self._user_path()}/drafts"
        resp = await self._request(
            "POST",
            url,
            action="Gmail draft creation",
            headers=await self._headers(),
            json=payload,
        )
        data = resp.json()

        result: dict[str, Any] = {
            "draft_id": data.get("id"),
            "message_id": data.get("message", {}).get("id"),
            "thread_id": data.get("message", {}).get("threadId", resolved_thread_id),
            "to": to,
            "requested_to": requested_to,
            "recipient_source": recipient_source,
            "subject": subject,
        }
        if not self.compact_results:
            result["next_steps"] = [
                "Call apply_existing_gmail_labels with the exact label names from "
                "extract_feedback_claim.recommended_labels.",
                "Then call mark_gmail_messages_read with the same message_ids (to clear UNREAD so it is not re-processed).",
                "Then call save_case_state with status=draft_created, this draft_id, "
                "and the case message_id. Do not re-read or re-extract.",
                "Never invent label names; only use those returned by extract_feedback_claim.",
            ]
        return result
