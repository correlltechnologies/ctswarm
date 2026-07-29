"""Ollama backend.

Chat goes through the **native** ``/api/chat`` endpoint rather than the
OpenAI-compatible ``/v1`` one, for a reason that is easy to miss and expensive
to miss:

**Ollama defaults every model to a 4096-token context window and silently
truncates anything longer.** Measured here: ``qwen3.6`` advertises 262144 and
was loaded at 4096. A 20k-token prompt does not error, it is quietly cut, and
the model answers confidently from the fragment it received. For repo-scale
agent work that is not a degradation, it is undetectable wrongness.

The window is only settable through ``options.num_ctx`` on the native endpoint.
The ``/v1`` endpoint ignores the field entirely (verified: passing
``options.num_ctx`` there still produced a 4096 window). So the native path is
the only correct one, and this module translates between the two shapes.

Context size is also a **VRAM decision**, not a free upgrade. Raising
``granite4.1:8b`` from 4096 to 32768 moved it from 5.3GB fully resident to 11GB
at 83% CPU offload on a 12GB card. So the window is sized to what a request
actually needs rather than pinned to the model's maximum.

The native path additionally exposes which models are resident right now, which
the router uses to prefer an already-warm model when scores are close.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from .base import ChatRequest, ChatResponse, FailureKind, inspect_completion
from .openai_compat import OpenAICompatBackend, connect_probe_timeout

# Ollama's own default, and the value it silently falls back to.
OLLAMA_DEFAULT_NUM_CTX = 4096

# Buckets to round a computed window up into. Reusing a small set of sizes lets
# Ollama keep a model resident across requests instead of reloading it for every
# slightly different prompt length.
NUM_CTX_BUCKETS = (4096, 8192, 16384, 32768, 65536, 131072)

# Headroom over the estimated prompt so a request near a bucket edge does not
# truncate. Truncation is silent, so erring small is the expensive direction.
NUM_CTX_SAFETY = 1.25


def choose_num_ctx(prompt_tokens: int, max_output_tokens: int, ceiling: int) -> int:
    """Pick the smallest bucket that fits the request without truncating."""
    needed = int((prompt_tokens + max_output_tokens) * NUM_CTX_SAFETY)
    for bucket in NUM_CTX_BUCKETS:
        if bucket >= needed:
            return min(bucket, ceiling) if ceiling else bucket
    return min(NUM_CTX_BUCKETS[-1], ceiling) if ceiling else NUM_CTX_BUCKETS[-1]


def estimate_prompt_tokens(messages: list[dict]) -> int:
    """Crude but deliberately generous prompt-size estimate.

    Over-estimating costs some VRAM. Under-estimating silently truncates the
    prompt, which is the failure this whole module exists to prevent.
    """
    characters = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            characters += len(content)
        elif content is not None:
            characters += len(str(content))
    return int(characters / 3.0) + 512


class OllamaBackend(OpenAICompatBackend):
    """Local Ollama server, driven through its native API."""

    def __init__(
        self,
        *,
        host: str = "http://localhost:11434",
        context_ceiling: int = 0,
        chat_timeout_s: float = 600.0,
        **kwargs,
    ) -> None:
        self.host = host.rstrip("/")
        self.context_ceiling = context_ceiling
        super().__init__(
            name="ollama",
            base_url=f"{self.host}/v1",
            metered=False,
            **kwargs,
        )
        self._native = httpx.AsyncClient(
            base_url=self.host, timeout=connect_probe_timeout()
        )
        # Generation needs a long read timeout (a partially offloaded model is
        # genuinely slow) while discovery stays fast, so they use separate
        # clients rather than one compromise timeout.
        self._native_chat = httpx.AsyncClient(
            base_url=self.host,
            timeout=httpx.Timeout(chat_timeout_s, connect=5.0),
        )
        self._context_limits: dict[str, int] = {}

    async def _context_limit(self, model_ref: str) -> int:
        """The model's own maximum window, cached."""
        if model_ref not in self._context_limits:
            details = await self.model_details(model_ref)
            limit = details.get("context") or 0
            self._context_limits[model_ref] = int(limit) if limit else 0
        return self._context_limits[model_ref]

    async def chat(self, request: ChatRequest, model_ref: str) -> ChatResponse:
        """Generate via the native endpoint with an explicitly sized window."""
        prompt_tokens_est = estimate_prompt_tokens(request.messages)
        model_ceiling = await self._context_limit(model_ref)
        ceiling = min(
            [v for v in (model_ceiling, self.context_ceiling) if v] or [0]
        )
        num_ctx = choose_num_ctx(
            prompt_tokens_est, request.max_tokens or 1024, ceiling
        )

        options: dict = {"num_ctx": num_ctx, "temperature": request.temperature or 0.0}
        if request.max_tokens:
            options["num_predict"] = request.max_tokens

        payload: dict = {
            "model": model_ref,
            "messages": request.messages,
            "stream": False,
            "options": options,
        }
        if request.tools:
            payload["tools"] = request.tools
        if request.response_format:
            # Native uses `format`, which accepts "json" or a JSON schema.
            schema = (request.response_format or {}).get("json_schema")
            payload["format"] = schema.get("schema") if schema else "json"

        started = time.perf_counter()

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        try:
            response = await self._native_chat.post("/api/chat", json=payload)
        except httpx.TimeoutException:
            return self._failure(
                model_ref, FailureKind.TIMEOUT, "request timed out", elapsed_ms()
            )
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            return self._failure(
                model_ref, FailureKind.CONNECTION_ERROR, str(exc), elapsed_ms()
            )

        latency_ms = elapsed_ms()

        if response.status_code != 200:
            from .base import classify_http_failure

            kind = classify_http_failure(response.status_code, response.text[:2000])
            return self._failure(model_ref, kind, response.text[:500], latency_ms)

        try:
            native = response.json()
        except ValueError as exc:
            return self._failure(
                model_ref, FailureKind.SERVER_ERROR, f"non-JSON body: {exc}", latency_ms
            )

        body = _to_openai_shape(native, model_ref)
        prompt_tokens = int(native.get("prompt_eval_count") or 0)
        output_tokens = int(native.get("eval_count") or 0)

        # Surface the window actually used so callers can tell a genuine
        # long-context failure apart from a truncation artefact.
        body["ctswarm_num_ctx"] = num_ctx

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
                error_detail=f"unusable completion (num_ctx={num_ctx})",
            )

        return ChatResponse(
            ok=True,
            body=body,
            backend=self.name,
            model_ref=model_ref,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
        )

    async def health(self) -> bool:
        """Ollama's root path returns a plain-text banner when alive."""
        try:
            response = await self._native.get("/")
            return response.status_code == 200
        except (httpx.HTTPError, asyncio.TimeoutError, OSError):
            return False

    async def list_models(self) -> list[str]:
        """Installed models, via the native tags endpoint.

        Ollama's ``/v1/models`` also works, but the native endpoint additionally
        carries size and family metadata that ``ctswarm doctor`` reports.
        """
        try:
            response = await self._native.get("/api/tags")
            response.raise_for_status()
            return [
                entry["name"]
                for entry in response.json().get("models", [])
                if isinstance(entry, dict) and "name" in entry
            ]
        except (httpx.HTTPError, ValueError, KeyError, asyncio.TimeoutError, OSError):
            return []

    async def loaded_models(self) -> set[str]:
        """Models currently resident in memory.

        Empty set on any failure, which degrades the router to score-only
        ordering rather than breaking routing entirely.
        """
        try:
            response = await self._native.get("/api/ps")
            response.raise_for_status()
            return {
                entry["name"]
                for entry in response.json().get("models", [])
                if isinstance(entry, dict) and "name" in entry
            }
        except (httpx.HTTPError, ValueError, KeyError, asyncio.TimeoutError, OSError):
            return set()

    async def wedged_models(self) -> set[str]:
        """Models stuck mid-unload, which block the whole inference queue.

        Ollama reports a model whose runner will not terminate with an ``expires_at``
        in the past while it is still listed as loaded. Observed in practice: a
        model entered a runaway generation, sat at "Stopping..." with the GPU at
        94%, and every request for *any other model* queued behind it forever.

        Detecting this matters because the natural reading of that situation is
        "the other models are timing out", which would wrongly penalize innocent
        models in the routing table. The bench and the router both consult this
        so a wedged server is reported as a server fault instead.
        """
        import datetime as _dt

        try:
            response = await self._native.get("/api/ps")
            response.raise_for_status()
            entries = response.json().get("models", [])
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError, OSError):
            return set()

        now = _dt.datetime.now(_dt.timezone.utc)
        wedged: set[str] = set()
        for entry in entries:
            name = entry.get("name")
            expires = entry.get("expires_at")
            if not name or not expires:
                continue
            try:
                deadline = _dt.datetime.fromisoformat(expires.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            # Still loaded well past its own expiry means the runner did not
            # release. A small grace period avoids flagging a normal unload.
            if deadline < now - _dt.timedelta(seconds=30):
                wedged.add(name)
        return wedged

    async def model_details(self, model_ref: str) -> dict:
        """Capability metadata for one model, as ``ollama show`` reports it.

        Used by the bench to record whether a model claims tool support before
        measuring whether it can actually deliver it.
        """
        try:
            response = await self._native.post("/api/show", json={"model": model_ref})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError, OSError):
            return {}
        info = body.get("model_info") or {}
        context = next(
            (v for k, v in info.items() if k.endswith(".context_length")), None
        )
        return {
            "capabilities": body.get("capabilities") or [],
            "context": context,
            "parameters": (body.get("details") or {}).get("parameter_size"),
            "quantization": (body.get("details") or {}).get("quantization_level"),
            "family": (body.get("details") or {}).get("family"),
        }

    async def close(self) -> None:
        await self._native.aclose()
        await self._native_chat.aclose()
        await super().close()


def _to_openai_shape(native: dict, model_ref: str) -> dict:
    """Translate a native ``/api/chat`` response into the OpenAI shape.

    Everything downstream (``inspect_completion``, the router, the bench) speaks
    OpenAI, so the translation happens here rather than spreading two response
    formats through the codebase.

    One meaningful difference: native tool-call arguments arrive as a real dict,
    whereas OpenAI encodes them as a JSON *string*. They are left as dicts
    because ``inspect_completion`` accepts both, and re-encoding would throw away
    the fact that Ollama already produced valid structured arguments.
    """
    message = native.get("message") or {}
    tool_calls = []
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        tool_calls.append(
            {
                "id": call.get("id") or f"call_{index}",
                "type": "function",
                "function": {
                    "name": function.get("name"),
                    "arguments": function.get("arguments"),
                },
            }
        )

    openai_message: dict = {
        "role": message.get("role", "assistant"),
        "content": message.get("content") or "",
    }
    if tool_calls:
        openai_message["tool_calls"] = tool_calls
    # Reasoning models return thinking separately. Preserve it: it is not the
    # answer, but it is needed to tell "thought and said nothing" apart from
    # "produced nothing at all".
    if message.get("thinking"):
        openai_message["reasoning"] = message["thinking"]

    done_reason = native.get("done_reason") or "stop"
    finish_reason = {
        "stop": "tool_calls" if tool_calls else "stop",
        "length": "length",
        "load": "stop",
    }.get(done_reason, done_reason)

    return {
        "id": f"ollama-{native.get('created_at', '')}",
        "object": "chat.completion",
        "model": model_ref,
        "choices": [
            {"index": 0, "message": openai_message, "finish_reason": finish_reason}
        ],
        "usage": {
            "prompt_tokens": int(native.get("prompt_eval_count") or 0),
            "completion_tokens": int(native.get("eval_count") or 0),
            "total_tokens": int(native.get("prompt_eval_count") or 0)
            + int(native.get("eval_count") or 0),
        },
    }
