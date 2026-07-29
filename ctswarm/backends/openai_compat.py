"""Generic OpenAI-compatible backend.

Ollama, MLX (`mlx_lm.server`), LM Studio, OpenRouter, and the OpenAI API all
expose ``/v1/chat/completions`` with the same request and response shape. One
adapter parameterized by base URL, auth, and pricing covers all of them; the
per-provider subclasses only override model discovery and quota reporting.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx

from .base import (
    Backend,
    ChatRequest,
    ChatResponse,
    FailureKind,
    classify_http_failure,
    inspect_completion,
)


class OpenAICompatBackend(Backend):
    """Talks to any OpenAI-compatible ``/v1`` endpoint."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: Optional[str] = None,
        timeout_s: float = 300.0,
        connect_timeout_s: float = 5.0,
        metered: bool = False,
        extra_headers: Optional[dict] = None,
    ) -> None:
        self.name = name
        self.metered = metered
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        headers = dict(extra_headers or {})
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        # A long read timeout is required (agent turns legitimately run for
        # minutes on a partially-offloaded local model) but the connect timeout
        # stays short so a dead backend is detected in seconds, not minutes.
        # Probe 2 kills the local backend mid-build and depends on this.
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s),
        )

    async def health(self) -> bool:
        try:
            response = await self._client.get("/models", timeout=connect_probe_timeout())
            return response.status_code < 500
        except (httpx.HTTPError, asyncio.TimeoutError, OSError):
            return False

    async def list_models(self) -> list[str]:
        try:
            response = await self._client.get("/models", timeout=connect_probe_timeout())
            response.raise_for_status()
            data = response.json().get("data") or []
            return [entry["id"] for entry in data if isinstance(entry, dict) and "id" in entry]
        except (httpx.HTTPError, ValueError, KeyError, asyncio.TimeoutError, OSError):
            return []

    async def chat(self, request: ChatRequest, model_ref: str) -> ChatResponse:
        payload = request.for_backend(model_ref)
        started = time.perf_counter()

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        try:
            response = await self._client.post("/chat/completions", json=payload)
        except httpx.TimeoutException:
            return self._failure(model_ref, FailureKind.TIMEOUT, "request timed out", elapsed_ms())
        except asyncio.CancelledError:
            # Cancellation is the caller's decision, never the model's fault, so
            # it must not count against the breaker. Re-raise after recording
            # nothing so structured concurrency still unwinds correctly.
            raise
        except (httpx.HTTPError, OSError) as exc:
            return self._failure(
                model_ref, FailureKind.CONNECTION_ERROR, str(exc), elapsed_ms()
            )

        latency_ms = elapsed_ms()

        if response.status_code != 200:
            kind = classify_http_failure(response.status_code, response.text[:2000])
            return self._failure(model_ref, kind, response.text[:500], latency_ms)

        try:
            body = response.json()
        except ValueError as exc:
            return self._failure(
                model_ref, FailureKind.SERVER_ERROR, f"non-JSON body: {exc}", latency_ms
            )

        usage = body.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)

        failure = inspect_completion(body, expected_tools=request.needs_tools)
        if failure:
            return ChatResponse(
                ok=False,
                body=body,
                backend=self.name,
                model_ref=model_ref,
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                failure_kind=failure,
                error_detail="200 OK but unusable completion",
                cost_usd=self.cost_for(model_ref, prompt_tokens, output_tokens),
            )

        return ChatResponse(
            ok=True,
            body=body,
            backend=self.name,
            model_ref=model_ref,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            cost_usd=self.cost_for(model_ref, prompt_tokens, output_tokens),
        )

    def _failure(
        self, model_ref: str, kind: str, detail: str, latency_ms: int
    ) -> ChatResponse:
        return ChatResponse(
            ok=False,
            body={},
            backend=self.name,
            model_ref=model_ref,
            latency_ms=latency_ms,
            failure_kind=kind,
            error_detail=detail,
        )

    async def close(self) -> None:
        await self._client.aclose()


def connect_probe_timeout() -> httpx.Timeout:
    """Short timeout for liveness and discovery calls.

    Health checks must fail fast. Using the chat timeout here would make a dead
    backend take minutes to detect, which defeats failover.
    """
    return httpx.Timeout(10.0, connect=3.0)
