import inspect

import httpx
import pytest
from pytest import MonkeyPatch

from player_support_agent.tools.config import GmailConfig
from player_support_agent.tools.forge_tools import ApplyExistingGmailLabelsParams
from player_support_agent.tools.gmail_tools import (
    GmailTools,
    _raise_http_error,
    _reply_recipient_from_messages,
    build_sender_feedback_query,
    normalize_sender_email,
)
from player_support_agent.tools.tool_shared_state import ToolSharedState
from forge.errors import ToolResolutionError


def test_apply_existing_labels_schema_has_no_remove_labels_field():
    schema = ApplyExistingGmailLabelsParams.model_json_schema()

    assert "remove_label_names" not in schema["properties"]


def test_apply_existing_labels_callable_has_no_remove_labels_param():
    signature = inspect.signature(GmailTools.apply_existing_gmail_labels)

    assert "remove_label_names" not in signature.parameters


def test_gmail_connect_error_includes_network_hint():
    request = httpx.Request(
        "GET",
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
    )
    exc = httpx.ConnectError("", request=request)

    with pytest.raises(RuntimeError) as exc_info:
        _raise_http_error("Gmail message search", str(request.url), exc)

    text = str(exc_info.value)
    assert "HTTPS_PROXY" in text or "VPN" in text


@pytest.mark.asyncio
async def test_gmail_request_retries_transient_connect_error(monkeypatch: MonkeyPatch):
    gmail = GmailTools(
        GmailConfig(
            access_token="test-token",
            max_request_retries=2,
            retry_backoff_seconds=0.0,
        )
    )
    attempts = {"count": 0}
    request = httpx.Request(
        "GET",
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
    )

    async def fake_request(*_args, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(200, json={"messages": []}, request=request)

    async def _client():
        return type("Client", (), {"request": fake_request, "is_closed": False})()

    async def fake_headers():
        return {"Authorization": "Bearer test-token"}

    monkeypatch.setattr(gmail, "_client", _client)
    monkeypatch.setattr(gmail, "_headers", fake_headers)

    result = await gmail.list_new_feedback_emails(max_results=1)

    assert attempts["count"] == 3
    assert result["messages"] == []


def test_gmail_http_error_includes_sanitized_stage_and_url():
    request = httpx.Request(
        "GET",
        "https://gmail.googleapis.com/gmail/v1/users/me/labels?access_token=secret",
    )
    exc = httpx.ConnectError("", request=request)

    with pytest.raises(RuntimeError) as exc_info:
        _raise_http_error("Gmail label list", str(request.url), exc)

    text = str(exc_info.value)
    assert "Gmail label list failed: ConnectError" in text
    assert "https://gmail.googleapis.com/gmail/v1/users/me/labels" in text
    assert "secret" not in text


@pytest.mark.asyncio
async def test_label_lookup_allows_configured_global_no_content_label():
    gmail = GmailTools(
        GmailConfig(
            allowed_label_names=["无内容"],
            access_token="test-token",
        )
    )
    gmail._label_cache = {"无内容": "Label_NoContent"}

    assert await gmail._label_ids_for_names(["无内容"]) == ["Label_NoContent"]


@pytest.mark.asyncio
async def test_label_lookup_rejects_unconfigured_label():
    gmail = GmailTools(
        GmailConfig(
            allowed_label_names=["NumberCrush/bug反馈"],
            access_token="test-token",
        )
    )
    gmail._label_cache = {"NumberCrush/bug反馈": "Label_1"}

    with pytest.raises(ToolResolutionError):
        await gmail._label_ids_for_names(["NumberCrush/不存在"])


@pytest.mark.asyncio
async def test_label_lookup_rejects_configured_but_missing_gmail_label():
    gmail = GmailTools(
        GmailConfig(
            allowed_label_names=["NumberCrush/bug反馈"],
            access_token="test-token",
        )
    )
    gmail._label_cache = {}

    with pytest.raises(ToolResolutionError):
        await gmail._label_ids_for_names(["NumberCrush/bug反馈"])


@pytest.mark.asyncio
async def test_label_lookup_allows_existing_project_label_without_static_allowlist():
    gmail = GmailTools(
        GmailConfig(
            access_token="test-token",
            allowed_label_names=[],
            project_label_names=["BlackHole"],
            allow_existing_project_labels=True,
        )
    )
    gmail._label_cache = {"BlackHole/bug反馈": "Label_1"}
    gmail._label_details = {
        "BlackHole/bug反馈": {
            "id": "Label_1",
            "name": "BlackHole/bug反馈",
            "type": "user",
        }
    }

    assert await gmail._label_ids_for_names(["BlackHole/bug反馈"]) == ["Label_1"]


@pytest.mark.asyncio
async def test_apply_existing_labels_rejects_empty_label_list_without_fallback(
    monkeypatch,
):
    shared_state = ToolSharedState()
    shared_state.set_last_extract_claim({"recommended_labels": ["无内容"]})
    gmail = GmailTools(
        GmailConfig(
            access_token="test-token",
            allowed_label_names=["无内容"],
        ),
        shared_state=shared_state,
    )
    gmail._label_cache = {"无内容": "Label_NoContent"}

    async def fail_request(*args, **kwargs):
        raise AssertionError("empty labels must not write Gmail using fallback labels")

    monkeypatch.setattr(gmail, "_request", fail_request)

    result = await gmail.apply_existing_gmail_labels(["m1"], [])

    assert result["applied_labels"] == []
    assert result["partial_success"] is False
    assert result["rejected_labels"][0]["error"] == "label_names is required"


@pytest.mark.asyncio
async def test_apply_existing_labels_rejects_mismatch_without_fallback(monkeypatch):
    shared_state = ToolSharedState()
    shared_state.set_last_extract_claim(
        {"recommended_labels": ["BlackHole", "BlackHole/咨询其他"]}
    )
    gmail = GmailTools(
        GmailConfig(
            access_token="test-token",
            project_label_names=["BlackHole"],
            allow_existing_project_labels=True,
        ),
        shared_state=shared_state,
    )
    gmail._label_cache = {
        "BlackHole": "Label_Project",
        "BlackHole/一般问题": "Label_Wrong",
        "BlackHole/咨询其他": "Label_Recommended",
    }
    gmail._label_details = {
        name: {"id": label_id, "name": name, "type": "user"}
        for name, label_id in gmail._label_cache.items()
    }

    async def fail_request(*args, **kwargs):
        raise AssertionError("mismatched labels must not be corrected and written")

    monkeypatch.setattr(gmail, "_request", fail_request)

    result = await gmail.apply_existing_gmail_labels(["m1"], ["BlackHole/一般问题"])

    assert result["applied_labels"] == []
    assert result["partial_success"] is False
    assert result["recommended_labels"] == ["BlackHole", "BlackHole/咨询其他"]
    assert result["rejected_labels"][0]["error"] == (
        "label_names must exactly match extract_feedback_claim.recommended_labels"
    )


@pytest.mark.asyncio
async def test_apply_existing_labels_rejects_when_extract_recommended_no_labels(
    monkeypatch,
):
    shared_state = ToolSharedState()
    shared_state.set_last_extract_claim(
        {"case_type": "no_content", "recommended_labels": []}
    )
    gmail = GmailTools(
        GmailConfig(
            access_token="test-token",
            allowed_label_names=["无内容"],
        ),
        shared_state=shared_state,
    )
    gmail._label_cache = {"无内容": "Label_NoContent"}

    async def fail_request(*args, **kwargs):
        raise AssertionError("labels must not be applied when extract recommended none")

    monkeypatch.setattr(gmail, "_request", fail_request)

    result = await gmail.apply_existing_gmail_labels(["m1"], ["无内容"])

    assert result["applied_labels"] == []
    assert result["partial_success"] is False
    assert result["rejected_labels"][0]["error"] == (
        "extract_feedback_claim.recommended_labels is empty; refusing to apply labels"
    )


def test_project_labels_from_thread_labels_ignores_non_project_user_labels():
    gmail = GmailTools(
        GmailConfig(
            access_token="test-token",
            project_label_names=["BlackHole"],
        )
    )
    gmail._label_details = {
        "BlackHole/bug反馈": {"id": "Label_1", "name": "BlackHole/bug反馈", "type": "user"},
        "AI/Processed": {"id": "Label_2", "name": "AI/Processed", "type": "user"},
        "CATEGORY_PROMOTIONS": {
            "id": "CATEGORY_PROMOTIONS",
            "name": "CATEGORY_PROMOTIONS",
            "type": "system",
        },
    }

    assert gmail._project_labels_from_label_names(
        ["BlackHole/bug反馈", "AI/Processed", "CATEGORY_PROMOTIONS"]
    ) == ["BlackHole"]


def test_reply_recipient_uses_latest_inbound_thread_sender():
    messages = [
        {
            "payload": {
                "headers": [
                    {"name": "From", "value": "Player <first@example.com>"},
                    {"name": "Reply-To", "value": "First Reply <first.reply@example.com>"},
                ]
            }
        },
        {
            "payload": {
                "headers": [
                    {"name": "From", "value": "Support <support@example.com>"},
                ]
            }
        },
        {
            "payload": {
                "headers": [
                    {
                        "name": "From",
                        "value": "Samantha <samanthasamantha123@icloud.com>",
                    },
                ]
            }
        },
    ]

    assert (
        _reply_recipient_from_messages(
            messages,
            account_email="support@example.com",
        )
        == "samanthasamantha123@icloud.com"
    )


@pytest.mark.asyncio
async def test_mark_gmail_messages_read_removes_unread_label(monkeypatch):
    gmail = GmailTools(GmailConfig(access_token="test-token"))
    captured: dict[str, object] = {}

    async def fake_request(method, url, *, action, headers=None, params=None, json=None, data=None):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = json
        request = httpx.Request(method, url)
        return httpx.Response(200, json={}, request=request)

    async def fake_headers():
        return {"Authorization": "Bearer test-token"}

    monkeypatch.setattr(gmail, "_request", fake_request)
    monkeypatch.setattr(gmail, "_headers", fake_headers)

    result = await gmail.mark_gmail_messages_read(["m1", "m2"])

    assert result == {"message_ids": ["m1", "m2"], "marked_read": True}
    assert captured["method"] == "POST"
    assert captured["json"] == {
        "ids": ["m1", "m2"],
        "removeLabelIds": ["UNREAD"],
    }


@pytest.mark.asyncio
async def test_list_unread_project_emails_scans_existing_project_labels():
    gmail = GmailTools(
        GmailConfig(
            access_token="test-token",
            project_label_names=["BlackHole"],
            scan_child_project_labels=True,
        )
    )
    recorded_queries: list[str] = []

    async def fake_labels():
        return {
            "project_parent_labels": ["BlackHole"],
            "project_labels_by_parent": {
                "BlackHole": ["BlackHole", "BlackHole/bug反馈"],
            },
        }

    async def fake_list(max_results=10, query=None):
        recorded_queries.append(query or "")
        if query and "bug反馈" in query:
            return {
                "messages": [{"id": "m1", "threadId": "t1"}],
                "result_size_estimate": 1,
            }
        return {"messages": [], "result_size_estimate": 0}

    gmail.get_existing_gmail_labels = fake_labels
    gmail.list_new_feedback_emails = fake_list

    result = await gmail.list_unread_project_emails(max_results_per_label=5)

    assert len(recorded_queries) == 2
    assert recorded_queries[0] == (
        'is:unread in:inbox category:primary -in:spam -in:trash label:"BlackHole"'
    )
    assert result["messages"] == [
        {
            "message_id": "m1",
            "thread_id": "t1",
            "project_label": "BlackHole",
            "matched_labels": ["BlackHole/bug反馈"],
        }
    ]


def test_normalize_sender_email_accepts_simple_address():
    assert normalize_sender_email(" Player@Example.COM ") == "player@example.com"


def test_normalize_sender_email_rejects_invalid_address():
    with pytest.raises(ValueError, match="invalid sender email"):
        normalize_sender_email("not-an-email")


def test_build_sender_feedback_query_appends_from_filter():
    base = "is:unread in:inbox category:primary"
    assert (
        build_sender_feedback_query(base, "player@example.com")
        == "is:unread in:inbox category:primary from:player@example.com"
    )
