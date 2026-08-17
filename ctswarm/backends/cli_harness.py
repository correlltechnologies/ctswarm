"""Committee members backed by the Claude Code and Codex CLIs.

Every other backend in this package speaks HTTP. These two cannot: `claude` and
`codex` are subscription-driven command-line harnesses with no OpenAI-compatible
endpoint, which is the constraint the whole two-tier design exists to work
around (see docs/VERIFIED.md and the module docstring in ``ctswarm/capacity.py``).

They are wrapped as a ``Backend`` anyway, for one narrow reason: the verification
committee. `ctswarm/committee.py` draws members from the bench-eligible routing
table, and a subscriptions-only host has no routing table at all, so
``eligible_members`` returns nothing and every gated build blocks on
"completion committee quorum is unavailable".

Wrapping the CLIs restores a real committee, and arguably a better one than the
local path had. README's rule is that independence is by *model family* -- three
Qwen models agreeing is one opinion sampled three times. Claude and Codex are
genuinely different vendors, trained on different data by different teams, so an
Anthropic/OpenAI panel is two real families rather than one wearing two hats.

This backend is deliberately **not** registered in ``build_backends``. It cannot
serve agent roles (SWE-AF drives those CLIs itself, with its own session and tool
handling) and it must never appear in the model catalog as something the router
can dispatch to. It exists for committee votes and nothing else.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time

from .base import Backend, ChatRequest, ChatResponse, FailureKind

#: Model references the committee uses to address each harness. The prefix is
#: what makes ``family_of`` in committee.py resolve them to "anthropic" and
#: "openai" respectively, which is what the independence check counts.
CLAUDE_MEMBER = "claude_code:sonnet"
CODEX_MEMBER = "codex:gpt-5.5"


def _harness_of(model_ref: str) -> str:
    return "codex" if model_ref.lower().startswith("codex") else "claude"


def _model_of(model_ref: str) -> str:
    _, _, tail = model_ref.partition(":")
    return tail.strip()


class CliHarnessBackend(Backend):
    """Runs one committee prompt through `claude -p` or `codex exec`.

    Both CLIs are addressed in their non-interactive, single-shot forms and given
    no tools, no filesystem write access, and no session continuity. A committee
    vote is a judgment on text that was already handed to it; a reviewer that can
    go and change the repository is not a reviewer.
    """

    name = "cli_harness"
    #: Calls draw down a subscription window rather than a per-token bill, so
    #: there is no price to attach. Capacity accounting happens in
    #: ``CapacityManager.record_usage``, not here.
    metered = False

    def __init__(self, *, timeout_s: float = 240.0, env: dict | None = None) -> None:
        self.timeout_s = timeout_s
        self.env = env if env is not None else dict(os.environ)

    # -- availability ------------------------------------------------------

    def _executable(self, harness: str) -> str | None:
        return shutil.which(harness)

    async def health(self) -> bool:
        """True when at least one harness binary is on PATH. Must not raise."""
        try:
            return any(self._executable(h) for h in ("claude", "codex"))
        except Exception:  # noqa: BLE001 - health must never raise
            return False

    async def list_models(self) -> list[str]:
        """Which harnesses this host can actually invoke. Must not raise."""
        try:
            available = []
            if self._executable("claude"):
                available.append(CLAUDE_MEMBER)
            if self._executable("codex"):
                available.append(CODEX_MEMBER)
            return available
        except Exception:  # noqa: BLE001 - must not raise
            return []

    # -- execution ---------------------------------------------------------

    def _command(self, harness: str, model: str, prompt: str) -> list[str]:
        if harness == "codex":
            # `exec` is codex's non-interactive mode. Sandbox and approval flags
            # are pinned rather than inherited so a permissive global config
            # cannot hand a committee member write access.
            return [
                "codex",
                "exec",
                "--model",
                model,
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                prompt,
            ]
        # `-p` is claude's print mode: one prompt, one answer, no session.
        return [
            "claude",
            "-p",
            prompt,
            "--model",
            model,
            "--output-format",
            "json",
            "--allowed-tools",
            "",
        ]

    async def chat(self, request: ChatRequest, model_ref: str) -> ChatResponse:
        """Run one prompt through a CLI harness.

        Must not raise: every failure comes back as ``ok=False`` with a
        classified ``failure_kind`` so the committee records an abstention
        rather than crashing the gate.
        """
        started = time.monotonic()
        harness = _harness_of(model_ref)
        model = _model_of(model_ref)

        def _fail(kind: str, detail: str) -> ChatResponse:
            return ChatResponse(
                ok=False,
                body={},
                backend=self.name,
                model_ref=model_ref,
                latency_ms=int((time.monotonic() - started) * 1000),
                failure_kind=kind,
                error_detail=detail[:400],
            )

        if not self._executable(harness):
            return _fail(
                FailureKind.MODEL_NOT_FOUND,
                f"{harness} is not installed on this host",
            )

        prompt = "\n\n".join(
            str(message.get("content") or "") for message in request.messages
        ).strip()
        if not prompt:
            return _fail(FailureKind.BAD_REQUEST, "empty prompt")

        try:
            process = await asyncio.create_subprocess_exec(
                *self._command(harness, model, prompt),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
            )
        except OSError as exc:
            return _fail(FailureKind.CONNECTION_ERROR, f"{type(exc).__name__}: {exc}")

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_s
            )
        except asyncio.TimeoutError:
            # Leaving a wedged harness running would hold a subscription slot
            # and, on a small host, its memory too.
            process.kill()
            await process.wait()
            return _fail(
                FailureKind.TIMEOUT, f"no answer within {self.timeout_s:.0f}s"
            )

        out = (stdout or b"").decode("utf-8", "replace").strip()
        err = (stderr or b"").decode("utf-8", "replace").strip()

        if process.returncode != 0:
            return _fail(
                _classify_cli_failure(err or out),
                err or out or f"exit code {process.returncode}",
            )

        content, prompt_tokens, output_tokens, cost = _parse_harness_output(
            harness, out
        )
        if not content.strip():
            return _fail(FailureKind.EMPTY_RESPONSE, "harness returned no content")

        return ChatResponse(
            ok=True,
            # Rendered in the OpenAI shape the committee's parser already reads,
            # so `_ask_member` needs no special case for CLI members.
            body={"choices": [{"message": {"content": content}}]},
            backend=self.name,
            model_ref=model_ref,
            latency_ms=int((time.monotonic() - started) * 1000),
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )


def _classify_cli_failure(text: str) -> str:
    """Map harness stderr onto the shared failure taxonomy."""
    lowered = text.lower()
    if "rate limit" in lowered or "usage limit" in lowered or "quota" in lowered:
        return FailureKind.RATE_LIMITED
    if "not logged in" in lowered or "login" in lowered or "unauthorized" in lowered:
        return FailureKind.AUTH_ERROR
    if "context" in lowered and ("too long" in lowered or "exceed" in lowered):
        return FailureKind.CONTEXT_OVERFLOW
    return FailureKind.SERVER_ERROR


def _parse_harness_output(harness: str, out: str) -> tuple[str, int, int, float]:
    """Extract content and accounting from a harness's stdout.

    Claude's `--output-format json` gives structured usage and a real
    `total_cost_usd`, which is the only usage signal a subscription exposes.
    Codex's `exec` prints prose, so accounting stays at zero rather than being
    invented.
    """
    if harness == "claude":
        try:
            payload = json.loads(out)
        except (TypeError, ValueError):
            return out, 0, 0, 0.0
        if isinstance(payload, list):  # stream-json emits a list of events
            payload = next(
                (
                    event
                    for event in reversed(payload)
                    if isinstance(event, dict) and event.get("type") == "result"
                ),
                {},
            )
        if not isinstance(payload, dict):
            return out, 0, 0, 0.0
        usage = payload.get("usage") or {}
        return (
            str(payload.get("result") or payload.get("content") or ""),
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
            float(payload.get("total_cost_usd") or 0.0),
        )
    return out, 0, 0, 0.0
