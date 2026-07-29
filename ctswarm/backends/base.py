"""Backend protocol and failure classification.

Every local and remote inference target ctswarm can route to speaks the OpenAI
chat-completions shape, so the abstraction is thin on purpose. What is *not* thin
is failure classification: the router's circuit breaker is only as good as its
ability to tell "this model is broken" apart from "this request was bad".
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class FailureKind(str):
    """Failure taxonomy. String-valued so it lands in the ledger unchanged."""

    MALFORMED_TOOL_CALL = "malformed_tool_call"
    SCHEMA_VIOLATION = "schema_violation"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    EMPTY_RESPONSE = "empty_response"
    TRUNCATED_RESPONSE = "truncated_response"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    # Below this line: the request's fault, not the model's. These must never
    # trip the breaker, or a single malformed prompt would sideline a good model.
    BAD_REQUEST = "bad_request"
    AUTH_ERROR = "auth_error"
    MODEL_NOT_FOUND = "model_not_found"
    CONTEXT_OVERFLOW = "context_overflow"
    CANCELLED = "cancelled"


# Token budget that leaves room for a reasoning model to think before answering.
#
# Measured on this hardware: qwen3.5:4b spends 181 reasoning tokens and 2 content
# tokens to answer "Reply with exactly: OK". With max_tokens=8 or 64 it returns
# finish_reason=length and EMPTY content, which is indistinguishable from a
# broken model unless you know to look for it.
#
# The consequence is general: any max_tokens tuned against non-thinking models
# silently converts thinking models into apparent failures. Anywhere ctswarm
# sizes a budget, it must clear reasoning overhead first.
REASONING_BUDGET = 1024

# Errors that mean "stop immediately, retrying cannot help". SWE-AF learned this
# the hard way (issue #49): retrying an exhausted-credit error through every
# layer produces a misleading downstream error instead of the real cause.
FATAL_FAILURES = frozenset(
    {
        FailureKind.AUTH_ERROR,
        FailureKind.MODEL_NOT_FOUND,
    }
)


@dataclass(frozen=True)
class ChatRequest:
    """Normalized inbound request."""

    messages: list[dict]
    model: str
    tools: list[dict] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    response_format: dict | None = None
    extra: dict = field(default_factory=dict)

    def for_backend(self, model_ref: str) -> dict:
        """Render as an OpenAI chat-completions payload for a concrete model."""
        payload: dict[str, Any] = {"model": model_ref, "messages": list(self.messages)}
        if self.tools:
            payload["tools"] = self.tools
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.response_format is not None:
            payload["response_format"] = self.response_format
        if self.stream:
            payload["stream"] = True
        # Pass through anything the caller set that we do not model explicitly,
        # minus routing hints that are ctswarm's own and meaningless downstream.
        for key, value in self.extra.items():
            if key not in ("ctswarm_role", "ctswarm_build_id", "model", "messages"):
                payload[key] = value
        return payload

    @property
    def role(self) -> str | None:
        """SWE-AF role name, when the caller declared one."""
        return self.extra.get("ctswarm_role")

    @property
    def build_id(self) -> str | None:
        return self.extra.get("ctswarm_build_id")

    @property
    def needs_tools(self) -> bool:
        return bool(self.tools)


@dataclass(frozen=True)
class ChatResponse:
    """Normalized response plus the accounting the ledger needs."""

    ok: bool
    body: dict
    backend: str
    model_ref: str
    latency_ms: int
    prompt_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    failure_kind: str | None = None
    error_detail: str = ""

    @property
    def is_fatal(self) -> bool:
        return self.failure_kind in FATAL_FAILURES


_CONTEXT_OVERFLOW_RE = re.compile(
    r"context (length|window)|too many tokens|maximum context|exceeds.*context",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"invalid.*(api|key|token)|unauthorized|authentication|credit balance|"
    r"quota exceeded|account.*disabled",
    re.IGNORECASE,
)


def classify_http_failure(status: int, body_text: str) -> str:
    """Map an HTTP error into the failure taxonomy.

    Status alone is not enough. A 400 may be a genuinely bad request or a context
    overflow, and those route differently: overflow should retry on a
    larger-context model, a bad request should not retry at all.
    """
    if status == 429:
        return FailureKind.RATE_LIMITED
    if status in (401, 403):
        return FailureKind.AUTH_ERROR
    if status == 404:
        return FailureKind.MODEL_NOT_FOUND
    if status >= 500:
        return FailureKind.SERVER_ERROR
    if status == 400:
        if _CONTEXT_OVERFLOW_RE.search(body_text):
            return FailureKind.CONTEXT_OVERFLOW
        if _AUTH_RE.search(body_text):
            return FailureKind.AUTH_ERROR
        return FailureKind.BAD_REQUEST
    return FailureKind.SERVER_ERROR


def inspect_completion(body: dict, *, expected_tools: bool) -> str | None:
    """Validate a 200-OK completion, returning a failure kind or None.

    An HTTP 200 does not mean the model did its job. This is where most local
    model failures actually surface: empty content, a truncated generation, or a
    tool call whose arguments are not valid JSON. SWE-AF's agents consume these
    as typed outputs, so a malformed tool call stalls the DAG just as hard as a
    connection error, and it must be scored the same way.
    """
    choices = body.get("choices") or []
    if not choices:
        return FailureKind.EMPTY_RESPONSE

    choice = choices[0]
    message = choice.get("message") or {}
    finish = choice.get("finish_reason")
    content = message.get("content")
    tool_calls = message.get("tool_calls") or []

    if finish == "length":
        return FailureKind.TRUNCATED_RESPONSE

    for call in tool_calls:
        function = call.get("function") or {}
        if not function.get("name"):
            return FailureKind.MALFORMED_TOOL_CALL
        arguments = function.get("arguments")
        # OpenAI encodes arguments as a JSON *string*. Local models frequently
        # emit a bare dict, truncated JSON, or prose here.
        if isinstance(arguments, str):
            try:
                json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                return FailureKind.MALFORMED_TOOL_CALL
        elif not isinstance(arguments, dict):
            return FailureKind.MALFORMED_TOOL_CALL

    if not tool_calls and not (content or "").strip():
        # No content and no tool call is a non-answer regardless of finish reason.
        return FailureKind.EMPTY_RESPONSE

    if expected_tools and not tool_calls and not (content or "").strip():
        return FailureKind.MALFORMED_TOOL_CALL

    return None


class Backend(ABC):
    """One inference target."""

    #: Stable identifier used in the ledger and routing table.
    name: str
    #: True when calls cost money, which the router weighs against local capacity.
    metered: bool = False

    @abstractmethod
    async def health(self) -> bool:
        """Cheap liveness check. Must not raise.

        Answers "is the server process up", which is necessary but NOT
        sufficient. Use ``probe_generation`` before trusting a backend with real
        work: a single wedged model blocks the whole inference queue while the
        control endpoints keep answering 200.
        """

    async def probe_generation(
        self, model_ref: str, timeout_s: float = 120.0
    ) -> tuple[bool, str]:
        """Confirm the backend can actually *generate*, not merely respond.

        Returns ``(ok, reason)``. The reason is not decorative: an earlier version
        returned a bare bool and callers reported every failure as a timeout,
        which sent debugging in exactly the wrong direction.

        This exists because of an observed failure the ordinary health check could
        not see. A local model entered a runaway generation and never terminated.
        It pinned the GPU and every subsequent request queued behind it, yet
        ``GET /v1/models`` kept returning 200 with a full model list, so the
        backend looked perfectly healthy while serving nothing. Head-of-line
        blocking like that is invisible to a per-model circuit breaker: the other
        models are fine, they simply never get scheduled.

        ``max_tokens`` is deliberately generous. Reasoning models spend their
        budget on thinking tokens before emitting any content, so a probe sized
        for a two-token answer can never pass. Measured here: qwen3.5:4b spends
        181 reasoning tokens and 2 content tokens to answer "Reply with exactly:
        OK". A tight budget makes every thinking model look broken.
        """
        import asyncio as _asyncio

        request = ChatRequest(
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            model=model_ref,
            max_tokens=REASONING_BUDGET,
            temperature=0.0,
        )
        try:
            response = await _asyncio.wait_for(
                self.chat(request, model_ref), timeout=timeout_s
            )
        except _asyncio.TimeoutError:
            return False, f"no response within {timeout_s:.0f}s"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

        if response.ok:
            return True, "ok"
        return False, f"{response.failure_kind}: {response.error_detail[:120]}"

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Model references this backend can currently serve. Must not raise."""

    @abstractmethod
    async def chat(self, request: ChatRequest, model_ref: str) -> ChatResponse:
        """Execute a completion. Must not raise; failures come back as
        ``ChatResponse(ok=False)`` so the router can score them uniformly."""

    async def close(self) -> None:
        """Release resources. Default is a no-op."""
        return None

    def cost_for(self, model_ref: str, prompt_tokens: int, output_tokens: int) -> float:
        """Estimated USD cost. Free backends return zero.

        Prices are never hardcoded in ctswarm: metered backends fetch them from
        the provider's own pricing endpoint and cache them.
        """
        return 0.0
