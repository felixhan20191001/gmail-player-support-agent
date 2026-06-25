import json

import pytest
from forge import Message, MessageMeta, MessageRole, MessageType, ToolCallInfo

from player_support_agent.deepseek_client import (
    DeepSeekOpenAICompatClient,
    DeepSeekTieredCompact,
    ensure_openai_tool_turns,
    fold_and_serialize_deepseek,
    is_deepseek_base_url,
    repair_openai_tool_messages,
)


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.deepseek.com", True),
        ("https://api.deepseek.com/v1", True),
        ("https://api.openai.com/v1", False),
        (None, False),
    ],
)
def test_is_deepseek_base_url(base_url, expected):
    assert is_deepseek_base_url(base_url) is expected


def test_repair_openai_tool_messages_restores_missing_tool_call_ids():
    payload = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "List labels.",
            "tool_calls": [
                {
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "read_email_thread", "arguments": "{}"},
                },
                {
                    "id": "call_b",
                    "type": "function",
                    "function": {"name": "get_existing_gmail_labels", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "content": "thread"},
        {"role": "tool", "content": "labels"},
    ]

    repaired = repair_openai_tool_messages(payload)

    assert repaired[1]["tool_call_id"] == "call_a"
    assert repaired[1]["name"] == "read_email_thread"
    assert repaired[2]["tool_call_id"] == "call_b"
    assert repaired[2]["name"] == "get_existing_gmail_labels"


def test_ensure_openai_tool_turns_fills_missing_results_before_user_message():
    payload = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "tool_a", "arguments": "{}"},
                },
                {
                    "id": "call_b",
                    "type": "function",
                    "function": {"name": "tool_b", "arguments": "{}"},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_a",
            "content": "only first result",
        },
        {"role": "user", "content": "retry nudge"},
    ]

    repaired = ensure_openai_tool_turns(payload)

    assert repaired[2]["role"] == "tool"
    assert repaired[2]["tool_call_id"] == "call_b"
    assert repaired[3]["role"] == "user"


def test_ensure_openai_tool_turns_drops_orphan_tool_messages():
    payload = [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "call_orphan", "content": "lost assistant"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_x",
                    "type": "function",
                    "function": {"name": "tool_x", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_x", "content": "ok"},
    ]

    repaired = ensure_openai_tool_turns(payload)

    assert repaired[1]["role"] == "assistant"
    assert all(message.get("role") != "tool" or message.get("tool_call_id") == "call_x" for message in repaired)


def test_fold_and_serialize_deepseek_uses_pinned_reasoning_on_tool_calls():
    messages = [
        Message(
            MessageRole.ASSISTANT,
            "Pinned after compaction.",
            MessageMeta(MessageType.TOOL_CALL, step_index=1),
            tool_calls=[
                ToolCallInfo(
                    name="list_unread_inbox_emails",
                    args={"max_results": 1},
                    call_id="call_000000001",
                )
            ],
        ),
    ]

    payload = fold_and_serialize_deepseek(messages, "openai")

    assert payload[0]["reasoning_content"] == "Pinned after compaction."
    assert payload[0]["content"] == ""


def test_deepseek_tiered_compact_pins_reasoning_before_phase_three():
    messages = [
        Message(MessageRole.SYSTEM, "sys", MessageMeta(MessageType.SYSTEM_PROMPT)),
        Message(MessageRole.USER, "go", MessageMeta(MessageType.USER_INPUT)),
        Message(
            MessageRole.ASSISTANT,
            "reason-1",
            MessageMeta(MessageType.REASONING, step_index=1),
        ),
        Message(
            MessageRole.ASSISTANT,
            "",
            MessageMeta(MessageType.TOOL_CALL, step_index=1),
            tool_calls=[
                ToolCallInfo(name="tool_a", args={}, call_id="call_a"),
            ],
        ),
        Message(
            MessageRole.TOOL,
            "ok",
            MessageMeta(MessageType.TOOL_RESULT, step_index=1),
            tool_name="tool_a",
            tool_call_id="call_a",
        ),
        Message(
            MessageRole.ASSISTANT,
            "reason-2",
            MessageMeta(MessageType.REASONING, step_index=2),
        ),
        Message(
            MessageRole.ASSISTANT,
            "",
            MessageMeta(MessageType.TOOL_CALL, step_index=2),
            tool_calls=[
                ToolCallInfo(name="tool_b", args={}, call_id="call_b"),
            ],
        ),
        Message(
            MessageRole.TOOL,
            "ok",
            MessageMeta(MessageType.TOOL_RESULT, step_index=2),
            tool_name="tool_b",
            tool_call_id="call_b",
        ),
        Message(
            MessageRole.ASSISTANT,
            "reason-3",
            MessageMeta(MessageType.REASONING, step_index=3),
        ),
        Message(
            MessageRole.ASSISTANT,
            "",
            MessageMeta(MessageType.TOOL_CALL, step_index=3),
            tool_calls=[
                ToolCallInfo(name="tool_c", args={}, call_id="call_c"),
            ],
        ),
        Message(
            MessageRole.TOOL,
            "ok",
            MessageMeta(MessageType.TOOL_RESULT, step_index=3),
            tool_name="tool_c",
            tool_call_id="call_c",
        ),
        Message(
            MessageRole.ASSISTANT,
            "reason-4",
            MessageMeta(MessageType.REASONING, step_index=4),
        ),
        Message(
            MessageRole.ASSISTANT,
            "",
            MessageMeta(MessageType.TOOL_CALL, step_index=4),
            tool_calls=[
                ToolCallInfo(name="tool_d", args={}, call_id="call_d"),
            ],
        ),
        Message(
            MessageRole.TOOL,
            "ok",
            MessageMeta(MessageType.TOOL_RESULT, step_index=4),
            tool_name="tool_d",
            tool_call_id="call_d",
        ),
    ]

    compacted, phase = DeepSeekTieredCompact(keep_recent=1, compact_threshold=0.01).compact(
        messages,
        budget_tokens=100,
    )

    assert phase == 3
    payload = fold_and_serialize_deepseek(compacted, "openai")
    for index, message in enumerate(payload):
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        assert message.get("reasoning_content"), f"missing reasoning at index {index}"
        following_tools = 0
        for peer in payload[index + 1 :]:
            if peer.get("role") == "tool":
                following_tools += 1
                continue
            break
        assert following_tools == len(message["tool_calls"])


def test_fold_and_serialize_deepseek_uses_reasoning_content_for_tool_calls():
    messages = [
        Message(
            MessageRole.ASSISTANT,
            "Need to list unread emails first.",
            MessageMeta(MessageType.REASONING, step_index=1),
        ),
        Message(
            MessageRole.ASSISTANT,
            "",
            MessageMeta(MessageType.TOOL_CALL, step_index=1),
            tool_calls=[
                ToolCallInfo(
                    name="list_unread_inbox_emails",
                    args={"max_results": 1},
                    call_id="call_000000001",
                )
            ],
        ),
        Message(
            MessageRole.TOOL,
            '{"messages": []}',
            MessageMeta(MessageType.TOOL_RESULT, step_index=1),
            tool_name="list_unread_inbox_emails",
            tool_call_id="call_000000001",
        ),
    ]

    payload = fold_and_serialize_deepseek(messages, "openai")

    assert payload[0]["role"] == "assistant"
    assert payload[0]["reasoning_content"] == "Need to list unread emails first."
    assert payload[0]["tool_calls"][0]["function"]["name"] == "list_unread_inbox_emails"
    assert payload[1]["role"] == "tool"


@pytest.mark.asyncio
async def test_deepseek_client_parses_reasoning_content_on_tool_calls(monkeypatch):
    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [
                    {
                        "message": {
                            "reasoning_content": "Check labels before drafting.",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_existing_gmail_labels",
                                        "arguments": json.dumps({}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }

    async def fake_post(url, json):
        assert json["thinking"] == {"type": "enabled"}
        return FakeResponse()

    client = DeepSeekOpenAICompatClient(
        "deepseek-v4-pro",
        "https://api.deepseek.com",
        api_key="test-key",
        disable_thinking=False,
    )
    monkeypatch.setattr(client._http, "post", fake_post)

    response = await client.send([{"role": "user", "content": "hello"}])

    assert len(response) == 1
    assert response[0].tool == "get_existing_gmail_labels"
    assert response[0].reasoning == "Check labels before drafting."


@pytest.mark.asyncio
async def test_deepseek_agent_client_disables_thinking_by_default(monkeypatch):
    from player_support_agent.agent_runner import AgentRunConfig, build_client

    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [
                    {
                        "message": {
                            "content": "ok",
                        }
                    }
                ]
            }

    async def fake_post(url, json):
        captured["thinking"] = json.get("thinking")
        return FakeResponse()

    client = build_client(
        AgentRunConfig(
            config_path="config.toml",
            backend="openai-compatible",
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            api_key="test-key",
        )
    )
    monkeypatch.setattr(client._http, "post", fake_post)
    await client.send([{"role": "user", "content": "hello"}])
    assert captured["thinking"] == {"type": "disabled"}