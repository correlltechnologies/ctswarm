"""The ctswarm router: an OpenAI-compatible gateway with policy-based failover.

SWE-AF's ``open_code`` runtime talks to any OpenAI-compatible base URL. Pointing
it at this gateway means model selection becomes a ctswarm concern that can change
without touching factory configuration.

Callers request a **virtual model** naming a capability tier rather than a
concrete model:

    ctswarm/high   ctswarm/med   ctswarm/low

The router resolves that to a real (backend, model) at request time, and on a
retryable failure walks a fallback chain that deliberately crosses backends. A
concrete pin (``ctswarm/ollama:qwen3.5:9b``) bypasses selection, which the bench
and the verification probes rely on to target one model exactly.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..backends import Backend, ChatRequest, build_backends
from ..backends.base import FATAL_FAILURES, FailureKind
from ..catalog import Tier, tier_for_role
from ..ledger import Ledger
from ..platform_detect import detect_host
from .policy import Privacy, Router, RoutingTable

VIRTUAL_PREFIX = "ctswarm/"

# Failures where trying a different model is pointless or harmful. Context
# overflow is retryable but only onto a larger-context model, which the policy
# layer handles by raising min_context, so it is not in this set.
NON_RETRYABLE = FATAL_FAILURES | {FailureKind.BAD_REQUEST, FailureKind.CANCELLED}


class RouterState:
    """Shared, mutable-by-design runtime state for the gateway."""

    def __init__(self) -> None:
        self.host = detect_host()
        self.ledger = Ledger(os.environ.get("CTSWARM_DB", "var/ctswarm.db"))
        self.table = RoutingTable.load(
            os.environ.get("CTSWARM_ROUTING", "bench/results/routing.json")
        )
        self.backends: dict[str, Backend] = build_backends(self.host)
        self.router = Router(
            host=self.host,
            ledger=self.ledger,
            table=self.table,
            prefer_local=os.environ.get("CTSWARM_PREFER_LOCAL", "1") != "0",
        )
        # Cached so every request does not re-probe the backends. Refreshed on a
        # timer and, critically, immediately after a connection failure so a
        # backend that died mid-build is detected rather than retried blindly.
        self._installed: dict[str, set[str]] = {}
        self._warm: dict[str, set[str]] = {}
        self._discovered_at = 0.0
        self._discovery_lock = asyncio.Lock()

    async def discover(self, *, force: bool = False, ttl_s: float = 30.0) -> None:
        if not force and (time.time() - self._discovered_at) < ttl_s:
            return
        async with self._discovery_lock:
            if not force and (time.time() - self._discovered_at) < ttl_s:
                return
            installed: dict[str, set[str]] = {}
            warm: dict[str, set[str]] = {}
            for name, backend in self.backends.items():
                installed[name] = set(await backend.list_models())
                loaded = getattr(backend, "loaded_models", None)
                warm[name] = set(await loaded()) if loaded else set()
            self._installed, self._warm = installed, warm
            self._discovered_at = time.time()

    @property
    def installed(self) -> set[str]:
        return {ref for refs in self._installed.values() for ref in refs}

    @property
    def warm(self) -> set[str]:
        return {ref for refs in self._warm.values() for ref in refs}

    async def close(self) -> None:
        for backend in self.backends.values():
            await backend.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = RouterState()
    app.state.ctswarm = state
    await state.discover(force=True)
    state.ledger.record_event(
        "router_start",
        {
            "host": state.host.to_dict(),
            "backends": sorted(state.backends),
            "installed": sorted(state.installed),
            "routing_table_measured": not state.table.is_empty,
        },
    )
    try:
        yield
    finally:
        state.ledger.record_event("router_stop", {})
        await state.close()


app = FastAPI(title="ctswarm router", version="0.1.0", lifespan=lifespan)


def _estimate_context(messages: list[dict]) -> int:
    """Rough prompt-size estimate in tokens.

    Deliberately crude and deliberately generous. Its only job is to exclude
    models whose context window cannot hold the request, and under-estimating
    there produces a hard failure mid-build while over-estimating merely picks a
    roomier model.
    """
    characters = sum(len(str(message.get("content") or "")) for message in messages)
    return int(characters / 3.0) + 1024


def _parse_virtual(model: str) -> tuple[Tier | None, tuple[str, str] | None]:
    """Split a requested model into (tier, explicit pin).

    ``ctswarm/med``                 -> (MED, None)
    ``ctswarm/ollama:qwen3.5:9b``   -> (None, ("ollama", "qwen3.5:9b"))
    ``qwen3.5:9b``                  -> (None, (None, "qwen3.5:9b")) treated as a pin
    """
    if not model.startswith(VIRTUAL_PREFIX):
        return None, ("", model)

    suffix = model[len(VIRTUAL_PREFIX) :]
    if suffix in ("high", "med", "low"):
        return Tier(suffix), None
    if ":" in suffix:
        backend, _, ref = suffix.partition(":")
        return None, (backend, ref)
    return None, ("", suffix)


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    state: RouterState = request.app.state.ctswarm
    checks: dict[str, dict] = {}
    for name, backend in state.backends.items():
        entry: dict = {"reachable": await backend.health()}
        # Reachability alone is misleading. A wedged runner keeps the control
        # endpoints answering 200 while serving no inference at all, so surface
        # it explicitly rather than reporting a healthy backend that cannot work.
        wedged_fn = getattr(backend, "wedged_models", None)
        if wedged_fn:
            wedged = sorted(await wedged_fn())
            entry["wedged_models"] = wedged
            entry["degraded"] = bool(wedged)
        checks[name] = entry

    healthy = any(
        entry["reachable"] and not entry.get("degraded") for entry in checks.values()
    )
    return JSONResponse(
        {
            "ok": healthy,
            "backends": checks,
            "host": state.host.to_dict(),
            "routing_table_measured": not state.table.is_empty,
        }
    )


@app.get("/v1/models")
async def list_models(request: Request) -> JSONResponse:
    """Advertise virtual tiers plus every concrete model, OpenAI-shaped."""
    state: RouterState = request.app.state.ctswarm
    await state.discover()
    entries = [
        {"id": f"{VIRTUAL_PREFIX}{tier}", "object": "model", "owned_by": "ctswarm"}
        for tier in ("high", "med", "low")
    ]
    for backend_name, refs in state._installed.items():
        entries.extend(
            {
                "id": f"{VIRTUAL_PREFIX}{backend_name}:{ref}",
                "object": "model",
                "owned_by": backend_name,
            }
            for ref in sorted(refs)
        )
    return JSONResponse({"object": "list", "data": entries})


@app.get("/routing/explain")
async def explain(
    request: Request,
    role: str | None = None,
    tier: str | None = None,
    tools: bool = True,
    context: int = 8192,
) -> JSONResponse:
    """Why the router would pick what it picks.

    Exposed as a first-class endpoint because an opaque routing decision is
    untestable. The verification probes assert against this.
    """
    state: RouterState = request.app.state.ctswarm
    await state.discover()
    decision = state.router.decide(
        role=role,
        tier=Tier(tier) if tier else None,
        needs_tools=tools,
        min_context=context,
        installed=state.installed,
        warm=state.warm,
    )
    return JSONResponse(decision.to_dict())


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    state: RouterState = request.app.state.ctswarm
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(
            {"error": {"message": "invalid JSON body", "type": "bad_request"}},
            status_code=400,
        )

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return JSONResponse(
            {"error": {"message": "messages is required", "type": "bad_request"}},
            status_code=400,
        )

    requested_model = payload.get("model") or f"{VIRTUAL_PREFIX}med"
    role = payload.get("ctswarm_role")
    build_id = payload.get("ctswarm_build_id")

    chat_request = ChatRequest(
        messages=messages,
        model=requested_model,
        tools=payload.get("tools"),
        temperature=payload.get("temperature"),
        max_tokens=payload.get("max_tokens"),
        response_format=payload.get("response_format"),
        # Streaming is accepted but resolved non-streaming. Mid-stream failover
        # would require replaying already-emitted tokens, which cannot be done
        # correctly, and silently degrading reliability to preserve streaming is
        # the wrong trade for an autonomous factory.
        stream=False,
        extra={
            key: value
            for key, value in payload.items()
            if key
            not in (
                "model",
                "messages",
                "tools",
                "temperature",
                "max_tokens",
                "response_format",
                "stream",
            )
        },
    )

    await state.discover()
    tier, pin = _parse_virtual(requested_model)

    if pin is not None:
        backend_name, model_ref = pin
        chain = _pinned_chain(state, backend_name, model_ref)
        if not chain:
            return JSONResponse(
                {
                    "error": {
                        "message": f"pinned model not available: {requested_model}",
                        "type": "model_not_found",
                    }
                },
                status_code=404,
            )
        decision_dict = {"pinned": requested_model}
    else:
        decision = state.router.decide(
            role=role,
            tier=tier,
            needs_tools=bool(chat_request.tools),
            min_context=_estimate_context(messages),
            privacy=payload.get("ctswarm_privacy", Privacy.ANY),
            installed=state.installed,
            warm=state.warm,
        )
        chain = [(candidate.backend, candidate.model_ref) for candidate in decision.chain]
        decision_dict = decision.to_dict()

    if not chain:
        state.ledger.record_event(
            "route_no_candidate",
            {"model": requested_model, "role": role, "decision": decision_dict},
            build_id=build_id,
        )
        return JSONResponse(
            {
                "error": {
                    "message": "no eligible model for this request",
                    "type": "no_candidate",
                    "detail": decision_dict,
                }
            },
            status_code=503,
        )

    resolved_tier = (tier or (tier_for_role(role) if role else Tier.MED)).value
    attempts: list[dict] = []

    for attempt_number, (backend_name, model_ref) in enumerate(chain, start=1):
        backend = state.backends.get(backend_name)
        if backend is None:
            continue

        response = await backend.chat(chat_request, model_ref)

        state.ledger.record_call(
            backend=backend_name,
            model_ref=model_ref,
            ok=response.ok,
            build_id=build_id,
            role=role,
            tier=resolved_tier,
            virtual_model=requested_model,
            prompt_tokens=response.prompt_tokens,
            output_tokens=response.output_tokens,
            latency_ms=response.latency_ms,
            failure_kind=response.failure_kind,
            cost_usd=response.cost_usd,
            attempt=attempt_number,
        )

        if response.ok:
            if attempt_number > 1:
                # Failover actually happened. Probe 2 asserts on this event.
                state.ledger.record_event(
                    "failover_success",
                    {
                        "requested": requested_model,
                        "served_by": {"backend": backend_name, "model": model_ref},
                        "attempts": attempts,
                    },
                    build_id=build_id,
                )
            body = dict(response.body)
            body["ctswarm"] = {
                "backend": backend_name,
                "model_ref": model_ref,
                "attempt": attempt_number,
                "tier": resolved_tier,
            }
            return JSONResponse(body)

        attempts.append(
            {
                "backend": backend_name,
                "model": model_ref,
                "failure": response.failure_kind,
                "detail": response.error_detail[:200],
            }
        )

        if response.failure_kind in NON_RETRYABLE:
            # SWE-AF issue #49: retrying a fatal auth or credit error through
            # every layer buries the real cause under a misleading downstream
            # failure. Surface it immediately instead.
            state.ledger.record_event(
                "fatal_failure",
                {"requested": requested_model, "attempts": attempts},
                build_id=build_id,
            )
            return JSONResponse(
                {
                    "error": {
                        "message": response.error_detail or "fatal backend error",
                        "type": response.failure_kind,
                        "ctswarm_attempts": attempts,
                    }
                },
                status_code=502,
            )

        if response.failure_kind == FailureKind.CONNECTION_ERROR:
            # The backend may have died. Re-probe now so the next candidate is
            # chosen against reality rather than a stale 30-second cache.
            await state.discover(force=True)

    state.ledger.record_event(
        "route_exhausted",
        {"requested": requested_model, "role": role, "attempts": attempts},
        build_id=build_id,
    )
    return JSONResponse(
        {
            "error": {
                "message": "all candidate models failed",
                "type": "route_exhausted",
                "ctswarm_attempts": attempts,
            }
        },
        status_code=503,
    )


def _pinned_chain(
    state: RouterState, backend_name: str, model_ref: str
) -> list[tuple[str, str]]:
    """Resolve an explicit pin, with no fallback.

    A pin means the caller wants that exact model. Silently substituting another
    would invalidate every bench measurement and every probe that depends on
    targeting one model.
    """
    if backend_name and backend_name in state.backends:
        return [(backend_name, model_ref)]
    for name, refs in state._installed.items():
        if model_ref in refs:
            return [(name, model_ref)]
    return []
