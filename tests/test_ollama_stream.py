"""Regression tests for the native Ollama-to-SSE bridge."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from ctswarm.backends.base import ChatRequest, ChatResponse, FailureKind
from ctswarm.backends.ollama import (
    OllamaBackend,
    _to_native_messages,
    _to_sse_lines,
)


def test_native_messages_decode_openai_tool_history() -> None:
    messages = [
        {"role": "user", "content": "write it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "write",
                        "arguments": '{"filePath":"proof.txt","content":"ok\\n"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "Wrote file successfully.",
        },
    ]

    native = _to_native_messages(messages)

    assert native[1]["tool_calls"] == [
        {
            "function": {
                "name": "write",
                "arguments": {"filePath": "proof.txt", "content": "ok\n"},
            }
        }
    ]
    assert native[2] == {"role": "tool", "content": "Wrote file successfully."}
    assert messages[1]["tool_calls"][0]["function"]["arguments"].startswith("{")


def test_sse_bridge_preserves_tool_call_and_hides_reasoning() -> None:
    body = {
        "id": "ollama-test",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning": "private chain of thought",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "write",
                                "arguments": {
                                    "filePath": "proof.txt",
                                    "content": "ok\n",
                                },
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }

    lines = _to_sse_lines(body, "qwen3.5:9b")
    event = json.loads(lines[0].removeprefix("data: "))
    delta = event["choices"][0]["delta"]

    assert "reasoning" not in delta
    assert "private chain of thought" not in lines[0]
    assert event["choices"][0]["finish_reason"] == "tool_calls"
    assert delta["tool_calls"][0]["function"] == {
        "name": "write",
        "arguments": '{"filePath":"proof.txt","content":"ok\\n"}',
    }
    assert lines[-1] == "data: [DONE]\n"


async def test_ollama_stream_uses_native_chat_result() -> None:
    backend = OllamaBackend(host="http://unused.invalid")
    backend.chat = AsyncMock(
        return_value=ChatResponse(
            ok=True,
            body={
                "id": "ollama-test",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
            },
            backend="ollama",
            model_ref="qwen3.5:9b",
            latency_ms=1,
            prompt_tokens=37,
            output_tokens=11,
        )
    )
    request = ChatRequest(
        messages=[{"role": "user", "content": "do it"}],
        model="ctswarm/med",
        stream=True,
    )

    events = [event async for event in backend.stream(request, "qwen3.5:9b")]
    await backend.close()

    backend.chat.assert_awaited_once_with(request, "qwen3.5:9b")
    assert events[0] == ("ok", None)
    assert '"content":"done"' in events[1][1]
    assert events[-1][0] == "done"
    assert events[-1][1].prompt_tokens == 37
    assert events[-1][1].output_tokens == 11


async def test_ollama_stream_fails_before_first_byte() -> None:
    backend = OllamaBackend(host="http://unused.invalid")
    failure = ChatResponse(
        ok=False,
        body={},
        backend="ollama",
        model_ref="qwen3.5:9b",
        latency_ms=1,
        failure_kind=FailureKind.MALFORMED_TOOL_CALL,
    )
    backend.chat = AsyncMock(return_value=failure)

    events = [
        event
        async for event in backend.stream(
            ChatRequest(messages=[], model="ctswarm/med", stream=True),
            "qwen3.5:9b",
        )
    ]
    await backend.close()

    assert events == [("error", failure)]
