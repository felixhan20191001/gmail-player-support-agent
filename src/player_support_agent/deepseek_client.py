"""DeepSeek OpenAI-compatible client helpers.

DeepSeek thinking models return ``reasoning_content`` on assistant tool-call
turns and require that field on every subsequent request. Forge's generic
``OpenAICompatClient`` and ``fold_and_serialize`` only round-trip reasoning via
the assistant ``content`` field, which DeepSeek rejects with HTTP 400.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx
from forge import Message, MessageRole, MessageType
from forge.clients.base import ChunkType, StreamChunk
from forge.context.strategies import TieredCompact, _estimate_tokens
from forge.clients.openai_compat import OpenAICompatClient
from forge.core.reasoning import DEFAULT_REASONING_REPLAY, ReasoningReplay
from forge.core.workflow import LLMResponse, TextResponse, ToolCall, ToolSpec
from forge.errors import BackendError


def is_deepseek_base_url(base_url: str | None) -> bool:
    if not base_url:
        return False
    host = urlparse(base_url).netloc.lower()
    return host == "api.deepseek.com" or host.endswith(".deepseek.com")


def repair_openai_tool_messages(
    api_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Restore tool_call_id/name dropped by Forge context compaction."""

    pending: list[tuple[str, str]] = []
    repaired: list[dict[str, Any]] = []
    for message in api_messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            pending = []
            for tool_call in message["tool_calls"]:
                function = tool_call.get("function", {})
                pending.append(
                    (
                        str(tool_call.get("id") or ""),
                        str(function.get("name") or ""),
                    )
                )
            repaired.append(message)
            continue
        if message.get("role") == "tool":
            fixed = dict(message)
            if not fixed.get("tool_call_id") and pending:
                call_id, name = pending.pop(0)
                fixed["tool_call_id"] = call_id
                if not fixed.get("name") and name:
                    fixed["name"] = name
            repaired.append(fixed)
            continue
        repaired.append(message)
    return repaired


def ensure_openai_tool_turns(
    api_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Ensure every assistant tool_calls turn is followed by all tool replies.

    DeepSeek rejects histories where a ``tool`` message is not immediately preceded
    by an assistant ``tool_calls`` message, or where a later user/assistant message
    appears before every tool_call has a matching tool result.
    """

    repaired: list[dict[str, Any]] = []
    pending_ids: list[str] = []

    def flush_missing_tool_results() -> None:
        nonlocal pending_ids
        for call_id in pending_ids:
            if not call_id:
                continue
            repaired.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": "[Tool result unavailable after context compaction]",
                }
            )
        pending_ids = []

    for message in api_messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            flush_missing_tool_results()
            pending_ids = [
                str(tool_call.get("id") or "")
                for tool_call in message["tool_calls"]
            ]
            repaired.append(message)
            continue
        if role == "tool":
            if not pending_ids:
                continue
            call_id = str(message.get("tool_call_id") or "")
            if call_id and call_id in pending_ids:
                pending_ids.remove(call_id)
                repaired.append(message)
                continue
            if pending_ids:
                stub_id = pending_ids.pop(0)
                repaired.append(
                    {
                        **message,
                        "tool_call_id": stub_id,
                    }
                )
            continue
        if role in {"user", "system"} or (
            role == "assistant" and not message.get("tool_calls")
        ):
            flush_missing_tool_results()
            repaired.append(message)
            continue
        repaired.append(message)

    flush_missing_tool_results()
    return repair_openai_tool_messages(repaired)


def prepare_deepseek_messages(
    api_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return ensure_openai_tool_turns(api_messages)


def fold_and_serialize_deepseek(
    messages: list[Message],
    api_format: str,
    reasoning_replay: ReasoningReplay = DEFAULT_REASONING_REPLAY,
) -> list[dict[str, Any]]:
    """Fold REASONING messages into ``reasoning_content`` for DeepSeek tool turns."""

    api_messages: list[dict[str, Any]] = []
    pending_reasoning: str | None = None

    for message in messages:
        if (
            message.metadata.type == MessageType.REASONING
            and message.role == MessageRole.ASSISTANT
        ):
            pending_reasoning = message.content
            continue
        payload = message.to_api_dict(format=api_format)
        if message.tool_calls is not None:
            reasoning = pending_reasoning or (message.content or "").strip() or None
            if reasoning:
                payload["reasoning_content"] = reasoning
                payload["content"] = ""
            pending_reasoning = None
        elif pending_reasoning is not None:
            api_messages.append(
                {
                    "role": "assistant",
                    "reasoning_content": pending_reasoning,
                }
            )
            pending_reasoning = None
        api_messages.append(payload)

    if pending_reasoning is not None:
        api_messages.append(
            {
                "role": "assistant",
                "reasoning_content": pending_reasoning,
            }
        )

    return prepare_deepseek_messages(api_messages)


class DeepSeekTieredCompact(TieredCompact):
    """Compaction strategy that keeps DeepSeek chat history API-valid.

    Forge phase 2 can drop old tool results while leaving assistant tool-call
    skeletons behind, and phase 3 can drop REASONING while keeping tool calls.
    DeepSeek rejects both shapes. We therefore drop whole old tool turns
    together and pin reasoning onto any tool-call skeleton we still keep.
    """

    _ELIGIBLE_TOOL_TYPES = frozenset(
        {
            MessageType.REASONING,
            MessageType.TOOL_CALL,
            MessageType.TOOL_RESULT,
            MessageType.TEXT_RESPONSE,
        }
    )

    @staticmethod
    def _pin_reasoning_on_tool_calls(
        messages: list[Message],
        eligible_end: int,
    ) -> list[Message]:
        pinned = list(messages)
        for index in range(2, eligible_end):
            message = pinned[index]
            if message.metadata.type != MessageType.REASONING:
                continue
            for peer_index in range(index + 1, len(pinned)):
                peer = pinned[peer_index]
                if peer.metadata.step_index != message.metadata.step_index:
                    break
                if peer.tool_calls is None:
                    continue
                pinned[peer_index] = Message(
                    role=peer.role,
                    content=message.content,
                    metadata=peer.metadata,
                    tool_name=peer.tool_name,
                    tool_call_id=peer.tool_call_id,
                    tool_calls=peer.tool_calls,
                )
                break
        return pinned

    def compact(
        self,
        messages: list[Message],
        budget_tokens: int,
        *,
        step_hint: str = "",
    ) -> tuple[list[Message], int]:
        del step_hint
        tokens = _estimate_tokens(messages)
        t1 = int(budget_tokens * self._phase_triggers[0])
        t2 = int(budget_tokens * self._phase_triggers[1])
        t3 = int(budget_tokens * self._phase_triggers[2])
        if tokens < t1:
            return list(messages), 0

        eligible_end = self._find_eligible_end(messages, self.keep_recent)
        result = self._phase1(messages, eligible_end)
        if _estimate_tokens(result) < t2:
            return result, 1
        result = self._phase2(result, eligible_end)
        if _estimate_tokens(result) < t3:
            return result, 2
        result = self._phase3(result, eligible_end)
        return result, 3

    def _phase2(
        self,
        messages: list[Message],
        eligible_end: int,
    ) -> list[Message]:
        result: list[Message] = []
        for index, message in enumerate(messages):
            if 2 <= index < eligible_end:
                if message.metadata.type in (
                    MessageType.STEP_NUDGE,
                    MessageType.PREREQUISITE_NUDGE,
                    MessageType.RETRY_NUDGE,
                ):
                    continue
                if message.metadata.type in self._ELIGIBLE_TOOL_TYPES:
                    continue
            result.append(message)
        return result

    def _phase3(
        self,
        messages: list[Message],
        eligible_end: int,
    ) -> list[Message]:
        del eligible_end
        current_end = self._find_eligible_end(messages, self.keep_recent)
        return super()._phase3(
            self._pin_reasoning_on_tool_calls(messages, current_end),
            current_end,
        )


def _attach_reasoning(
    tool_calls: list[ToolCall],
    reasoning: str | None,
) -> list[ToolCall]:
    if not reasoning or not tool_calls:
        return tool_calls
    first = tool_calls[0]
    return [
        ToolCall(tool=first.tool, args=first.args, reasoning=reasoning),
        *tool_calls[1:],
    ]


class DeepSeekOpenAICompatClient(OpenAICompatClient):
    """OpenAI-compatible client with DeepSeek thinking-mode round-trip."""

    def __init__(
        self,
        model: str,
        base_url: str,
        *,
        disable_thinking: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(model, base_url, **kwargs)
        self._passthrough_defaults: dict[str, Any] = (
            {"thinking": {"type": "disabled"}}
            if disable_thinking
            else {"thinking": {"type": "enabled"}}
        )

    def _build_body(
        self,
        messages: list[dict[str, str]],
        tools: list[ToolSpec] | None,
        sampling: dict[str, Any] | None,
        stream: bool,
        passthrough: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged = dict(self._passthrough_defaults)
        merged.update(passthrough or {})
        return super()._build_body(
            messages,
            tools,
            sampling,
            stream,
            passthrough=merged,
        )

    def _parse_message_response(self, message: dict[str, Any]) -> LLMResponse:
        reasoning = (message.get("reasoning_content") or "").strip() or None
        tool_calls = message.get("tool_calls")
        if tool_calls:
            return _attach_reasoning(self._parse_tool_calls(tool_calls), reasoning)
        content = message.get("content") or ""
        if reasoning and not content:
            return TextResponse(content=reasoning)
        return TextResponse(content=content)

    async def send(
        self,
        messages: list[dict[str, str]],
        tools: list[ToolSpec] | None = None,
        sampling: dict[str, Any] | None = None,
        passthrough: dict[str, Any] | None = None,
        inbound_anthropic_body: dict[str, Any] | None = None,
    ) -> LLMResponse:
        del inbound_anthropic_body
        messages = prepare_deepseek_messages(messages)
        body = self._build_body(messages, tools, sampling, stream=False, passthrough=passthrough)
        try:
            resp = await self._http.post(f"{self.base_url}/chat/completions", json=body)
        except httpx.ReadTimeout as exc:
            raise BackendError(408, "Read timeout") from exc

        if resp.status_code != 200:
            _maybe_log_deepseek_request_failure(messages, resp.status_code, resp.text)
            raise BackendError(resp.status_code, resp.text)

        data = resp.json()
        self._record_usage(data)
        choices = data.get("choices") or []
        if not choices:
            raise BackendError(500, f"response has no choices: {data}")
        return self._parse_message_response(choices[0].get("message", {}))

    async def send_stream(
        self,
        messages: list[dict[str, str]],
        tools: list[ToolSpec] | None = None,
        sampling: dict[str, Any] | None = None,
        passthrough: dict[str, Any] | None = None,
        inbound_anthropic_body: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        del inbound_anthropic_body
        messages = prepare_deepseek_messages(messages)
        body = self._build_body(messages, tools, sampling, stream=True, passthrough=passthrough)

        accumulated_content = ""
        accumulated_reasoning = ""
        tool_calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] | None = None

        async with self._http.stream(
            "POST", f"{self.base_url}/chat/completions", json=body
        ) as response:
            if response.status_code != 200:
                error_body = await response.aread()
                raise BackendError(response.status_code, error_body.decode(errors="replace"))

            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                chunk = json.loads(data_str)
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {})

                reasoning_delta = delta.get("reasoning_content")
                if reasoning_delta:
                    accumulated_reasoning += str(reasoning_delta)

                content = delta.get("content")
                if content is not None:
                    if not isinstance(content, str):
                        content = str(content)
                    if content:
                        accumulated_content += content
                        yield StreamChunk(type=ChunkType.TEXT_DELTA, content=content)

                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tool_calls.setdefault(
                        idx, {"function": {"name": "", "arguments": ""}}
                    )
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        slot["function"]["name"] += str(fn["name"])
                    args_frag = fn.get("arguments")
                    if args_frag is not None:
                        slot["function"]["arguments"] += (
                            args_frag if isinstance(args_frag, str) else json.dumps(args_frag)
                        )

        if usage:
            self._record_usage({"usage": usage})

        if tool_calls:
            ordered = [tool_calls[i] for i in sorted(tool_calls)]
            reasoning = accumulated_reasoning.strip() or None
            final = _attach_reasoning(self._parse_tool_calls(ordered), reasoning)
        else:
            final = TextResponse(content=accumulated_content)
        yield StreamChunk(type=ChunkType.FINAL, response=final)


def _maybe_log_deepseek_request_failure(
    messages: list[dict[str, Any]],
    status_code: int,
    error_text: str,
) -> None:
    if status_code != 400:
        return
    from .paths import default_var_dir

    log_dir = default_var_dir() / "logs" / "deepseek"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "last_request_400.json"
    payload = {
        "status_code": status_code,
        "error": error_text,
        "messages": messages,
    }
    log_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class _DeepSeekFoldPatch:
    def __init__(self) -> None:
        import forge.core.inference as inference

        self._inference = inference
        self._original = inference.fold_and_serialize

    def __enter__(self) -> None:
        self._inference.fold_and_serialize = fold_and_serialize_deepseek

    def __exit__(self, *args: object) -> None:
        self._inference.fold_and_serialize = self._original