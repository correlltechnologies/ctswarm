"""Capacity manager: which *runtime* runs a build, based on remaining headroom.

This is the upper of ctswarm's two switching layers. The router picks a model
inside the `open_code` runtime; this picks the runtime itself. They are separate
because `claude_code` and `codex` are CLI harnesses rather than HTTP endpoints,
so no proxy can route across them (see docs/VERIFIED.md).

**Subscription headroom is not pollable.** Neither `claude` nor `codex` exposes a
usage or quota query, so there is no endpoint to ask "how much is left". What the
Claude CLI *does* provide is per-call accounting in its JSON output:

    {"total_cost_usd": 0.345, "usage": {"input_tokens": 2, "output_tokens": 4,
     "cache_creation_input_tokens": 34492, ...}}

So headroom is *reconstructed* by accumulating observed usage against a
configured window, and corrected by failure signals when a rate limit is hit
before the estimate expected it. That combination matters: accounting alone
drifts, and failure-only detection means discovering exhaustion by stalling a
build mid-flight.

A worked number that shapes the defaults: a trivial "Say OK" call measured
**$0.345**, almost entirely from 34,492 cache-creation tokens for the system
prompt. A SWE-AF build makes 400 to 500+ agent invocations. Per-call cost is
dominated by fixed overhead, not by the size of the request, so "it was only a
small prompt" is not a reason to expect a small bill.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum

from .ledger import Ledger


class Runtime(str, Enum):
    """SWE-AF's three execution runtimes."""

    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    OPEN_CODE = "open_code"

    @property
    def is_subscription(self) -> bool:
        """Whether this runtime draws on a metered subscription rather than
        local hardware."""
        return self in (Runtime.CLAUDE_CODE, Runtime.CODEX)


@dataclass(frozen=True)
class WindowBudget:
    """A rolling spend or call budget for one runtime.

    Windows are rolling rather than calendar-aligned because subscription limits
    generally are, and because a calendar reset would let a build burn an entire
    allowance immediately after midnight.
    """

    runtime: Runtime
    window_hours: float
    max_usd: float = 0.0
    max_calls: int = 0

    @property
    def window_seconds(self) -> float:
        return self.window_hours * 3600.0


# Defaults are conservative and overridable from the environment. They are
# starting points, not measurements: the correct values depend on the specific
# subscription tier, which is not machine-readable.
def default_budgets(env: dict | None = None) -> dict[Runtime, WindowBudget]:
    env = env if env is not None else dict(os.environ)

    def _f(key: str, fallback: float) -> float:
        try:
            return float(env.get(key, "") or fallback)
        except (TypeError, ValueError):
            return fallback

    return {
        Runtime.CLAUDE_CODE: WindowBudget(
            runtime=Runtime.CLAUDE_CODE,
            window_hours=_f("CTSWARM_CLAUDE_WINDOW_HOURS", 5.0),
            max_usd=_f("CTSWARM_CLAUDE_WINDOW_USD", 15.0),
        ),
        Runtime.CODEX: WindowBudget(
            runtime=Runtime.CODEX,
            window_hours=_f("CTSWARM_CODEX_WINDOW_HOURS", 5.0),
            max_usd=_f("CTSWARM_CODEX_WINDOW_USD", 15.0),
        ),
        # Local inference has no spend budget. Its constraint is wall-clock, which
        # the router handles through throughput scoring.
        Runtime.OPEN_CODE: WindowBudget(
            runtime=Runtime.OPEN_CODE, window_hours=1.0, max_usd=0.0
        ),
    }


def _has_credentials(path) -> bool:
    """Whether a credentials file holds anything, rather than merely existing."""
    try:
        if not path.is_file():
            return False
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and bool(payload)


def _claude_login_present(env: dict) -> bool:
    """Whether a Claude Code subscription login exists on this host.

    Three storage locations, because the CLI does not use one:

    - ``CLAUDE_CODE_OAUTH_TOKEN`` from ``claude setup-token``, which is what the
      containers are given.
    - ``~/.claude/.credentials.json`` on Linux, which is what
      ``infra/docker-compose.ctswarm.yml`` mounts into the agent containers.
    - the login Keychain on macOS, where the CLI stores credentials instead of
      writing a file at all.

    Checking only the file makes a logged-in Mac look unauthenticated, and since
    a subscriptions-only build now refuses to launch without a harness, that
    false negative would block every local build.
    """
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True

    from pathlib import Path

    credentials = Path(
        env.get("CTSWARM_CLAUDE_CREDENTIALS")
        or Path.home() / ".claude" / ".credentials.json"
    )
    # Existence is not enough. `bootstrap.sh` writes an empty `{}` at this path
    # so the container bind mount resolves to a file rather than a directory,
    # so a bare `.exists()` would report a login on a host that has none -- and
    # a build would launch against a harness that refuses on its first call.
    if _has_credentials(credentials):
        return True

    if sys.platform != "darwin":
        return False

    cached = _KEYCHAIN_CACHE.get("claude")
    if cached is not None and time.time() - cached[0] < _KEYCHAIN_TTL_S:
        return cached[1]
    try:
        found = (
            subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials"],
                capture_output=True,
                timeout=5,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        found = False
    _KEYCHAIN_CACHE["claude"] = (time.time(), found)
    return found


# The Keychain lookup shells out, and `report()` asks about three runtimes at
# once. A short TTL keeps a dashboard poll from spawning a process per request
# while still noticing a logout within the minute.
_KEYCHAIN_CACHE: dict[str, tuple[float, bool]] = {}
_KEYCHAIN_TTL_S = 60.0


@dataclass(frozen=True)
class Headroom:
    """How much of a runtime's window remains."""

    runtime: Runtime
    available: bool
    fraction_remaining: float
    spent_usd: float
    calls: int
    cooldown_until: float
    reason: str

    def to_dict(self) -> dict:
        return {
            "runtime": self.runtime.value,
            "available": self.available,
            "fraction_remaining": round(self.fraction_remaining, 3),
            "spent_usd": round(self.spent_usd, 4),
            "calls": self.calls,
            "cooldown_remaining_s": max(0, int(self.cooldown_until - time.time())),
            "reason": self.reason,
        }


# Reserve the tail of every subscription window. Section 5 of the plan is
# explicit that quota must be kept for review and unblock work rather than spent
# on low-value parallel chatter, and a build that exhausts its allowance during
# planning cannot verify what it built.
RESERVE_FRACTION = 0.20


class CapacityManager:
    """Chooses a runtime and tracks what each one has consumed."""

    def __init__(
        self,
        ledger: Ledger | None = None,
        budgets: dict[Runtime, WindowBudget] | None = None,
        env: dict | None = None,
    ) -> None:
        self.ledger = ledger or Ledger(os.environ.get("CTSWARM_DB", "var/ctswarm.db"))
        self.budgets = budgets or default_budgets(env)
        self.env = env if env is not None else dict(os.environ)

    @property
    def subscriptions_only(self) -> bool:
        """Whether this host is restricted to CLI subscription harnesses.

        Read on demand rather than cached at construction: the scheduler is a
        long-lived process and an operator can change the mode from Mission
        Control between builds.
        """
        from .execution_mode import subscription_only

        return subscription_only(self.ledger, self.env)

    # -- availability ------------------------------------------------------

    def configured(self, runtime: Runtime) -> bool:
        """Whether credentials exist for this runtime.

        Checked against real artifacts rather than trusting an environment
        variable that may name an expired token.

        In subscriptions-only mode an API key is deliberately *not* a
        credential. The whole point of the mode is that work is billed to a
        subscription; accepting a key here would let a stray `ANTHROPIC_API_KEY`
        in the environment start metered spend the operator never agreed to.
        """
        from pathlib import Path

        subscriptions_only = self.subscriptions_only
        if runtime is Runtime.CLAUDE_CODE:
            has_login = _claude_login_present(self.env)
            if subscriptions_only:
                return has_login
            return has_login or bool(self.env.get("ANTHROPIC_API_KEY"))
        if runtime is Runtime.CODEX:
            has_login = _has_credentials(
                Path(
                    self.env.get("CTSWARM_CODEX_HOME")
                    or Path.home() / ".codex"
                )
                / "auth.json"
            )
            if subscriptions_only:
                return has_login
            return has_login or bool(self.env.get("OPENAI_API_KEY"))
        if subscriptions_only:
            return False  # no local backend is registered in this mode
        return True  # open_code needs only a reachable backend

    def record_usage(
        self,
        runtime: Runtime,
        *,
        cost_usd: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        ok: bool = True,
        failure_kind: str | None = None,
        build_id: str | None = None,
    ) -> None:
        """Record one runtime invocation.

        Called with what the harness reported. For `claude_code` that is the
        CLI's own `total_cost_usd` and token counts, which is the only usage
        signal available for a subscription.
        """
        self.ledger.record_call(
            backend=f"runtime:{runtime.value}",
            model_ref=runtime.value,
            ok=ok,
            build_id=build_id,
            prompt_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            failure_kind=failure_kind,
        )

    def note_rate_limited(self, runtime: Runtime, *, detail: str = "") -> None:
        """Record that a runtime reported exhaustion.

        This is the correction signal. Accumulated accounting is an estimate
        built on a configured window that may not match the real subscription
        tier, so an actual rate-limit response is authoritative and immediately
        overrides the estimate.
        """
        self.ledger.record_event(
            "runtime_rate_limited", {"runtime": runtime.value, "detail": detail[:300]}
        )

    def headroom(self, runtime: Runtime) -> Headroom:
        """Remaining capacity for a runtime in its current window."""
        budget = self.budgets.get(runtime)
        now = time.time()

        if not self.configured(runtime):
            if runtime is Runtime.OPEN_CODE and self.subscriptions_only:
                reason = "disabled by subscriptions-only mode"
            elif self.subscriptions_only:
                reason = (
                    "no subscription login found; run "
                    + ("`claude setup-token`" if runtime is Runtime.CLAUDE_CODE else "`codex login`")
                )
            else:
                reason = "no credentials configured"
            return Headroom(runtime, False, 0.0, 0.0, 0, 0.0, reason)

        if runtime is Runtime.OPEN_CODE:
            return Headroom(runtime, True, 1.0, 0.0, 0, 0.0, "local, unmetered")

        window_start = now - (budget.window_seconds if budget else 18000.0)
        spent, calls = self._window_usage(runtime, window_start)

        # An observed rate limit inside this window overrides the estimate and
        # holds the runtime out until the window rolls past it.
        cooldown_until = 0.0
        for event in self.ledger.events(kind="runtime_rate_limited"):
            import json

            try:
                detail = json.loads(event["detail"])
            except (ValueError, TypeError):
                continue
            if detail.get("runtime") != runtime.value:
                continue
            if event["ts"] >= window_start:
                cooldown_until = max(
                    cooldown_until,
                    event["ts"] + (budget.window_seconds if budget else 18000.0),
                )

        if cooldown_until > now:
            return Headroom(
                runtime,
                False,
                0.0,
                spent,
                calls,
                cooldown_until,
                "rate limited; waiting for the window to roll",
            )

        if not budget or budget.max_usd <= 0:
            return Headroom(runtime, True, 1.0, spent, calls, 0.0, "no budget cap set")

        if calls == 0:
            return Headroom(
                runtime,
                True,
                1.0,
                spent,
                calls,
                0.0,
                "actual subscription quota is not queryable; no metered harness "
                "usage has been observed in this window",
            )

        fraction = max(0.0, 1.0 - (spent / budget.max_usd))
        if fraction <= 0.0:
            return Headroom(
                runtime, False, 0.0, spent, calls, 0.0,
                f"window budget exhausted (${spent:.2f} of ${budget.max_usd:.2f})",
            )
        if fraction <= RESERVE_FRACTION:
            return Headroom(
                runtime, False, fraction, spent, calls, 0.0,
                f"only the {RESERVE_FRACTION:.0%} review reserve remains "
                f"(${spent:.2f} of ${budget.max_usd:.2f})",
            )
        return Headroom(
            runtime, True, fraction, spent, calls, 0.0,
            f"${spent:.2f} of ${budget.max_usd:.2f} used in window",
        )

    def _window_usage(self, runtime: Runtime, since: float) -> tuple[float, int]:
        stats_backend = f"runtime:{runtime.value}"
        with self.ledger._connect() as conn:  # noqa: SLF001 - same package
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) AS spent, COUNT(*) AS n"
                " FROM calls WHERE backend=? AND ts>=?",
                (stats_backend, since),
            ).fetchone()
        return float(row["spent"]), int(row["n"])

    # -- selection ---------------------------------------------------------

    def select(
        self,
        *,
        prefer_local: bool = True,
        require_strong: bool = False,
        privacy_local_only: bool = False,
    ) -> tuple[Runtime, str]:
        """Pick the runtime for a build, with the reason.

        Ordering reflects the plan's economics rather than raw capability: local
        inference is the default because 400+ invocations per build make
        subscription capacity the scarce resource, and `require_strong` is the
        deliberate escape hatch for planning and final-verification work where
        errors propagate across every issue.
        """
        if self.subscriptions_only:
            # No local runtime exists to prefer, and a privacy class that
            # demands one cannot be honoured -- say so rather than silently
            # sending the work to a subscription.
            if privacy_local_only:
                return Runtime.CLAUDE_CODE, (
                    "privacy class requires local-only inference, which "
                    "subscriptions-only mode cannot provide"
                )
            for runtime in (Runtime.CLAUDE_CODE, Runtime.CODEX):
                head = self.headroom(runtime)
                if head.available:
                    return runtime, (
                        f"subscriptions-only mode; {runtime.value} has "
                        f"{head.fraction_remaining:.0%} of its window left"
                    )
            blocked = "; ".join(
                f"{r.value}: {self.headroom(r).reason}"
                for r in (Runtime.CLAUDE_CODE, Runtime.CODEX)
            )
            return Runtime.CLAUDE_CODE, (
                f"subscriptions-only mode with no headroom ({blocked}); "
                "claude_code as last resort"
            )

        if privacy_local_only:
            return Runtime.OPEN_CODE, "privacy class requires local-only inference"

        local_ready = self._open_code_ready()

        if require_strong:
            # Planning and final verification: prefer a subscription runtime, and
            # pick whichever has more headroom so the two wear evenly.
            candidates = [
                (r, self.headroom(r))
                for r in (Runtime.CLAUDE_CODE, Runtime.CODEX)
            ]
            usable = [(r, h) for r, h in candidates if h.available]
            if usable:
                runtime, head = max(usable, key=lambda pair: pair[1].fraction_remaining)
                return runtime, (
                    f"strong runtime requested; {runtime.value} has the most "
                    f"headroom ({head.fraction_remaining:.0%} left)"
                )
            if local_ready:
                blocked = "; ".join(f"{r.value}: {h.reason}" for r, h in candidates)
                return Runtime.OPEN_CODE, (
                    f"strong runtime requested but none available ({blocked}); "
                    "falling back to local, which is a DEGRADATION for this work"
                )
            return Runtime.CLAUDE_CODE, "no runtime available; claude_code as last resort"

        if prefer_local and local_ready:
            return Runtime.OPEN_CODE, "local inference available and preferred"

        for runtime in (Runtime.CLAUDE_CODE, Runtime.CODEX):
            head = self.headroom(runtime)
            if head.available:
                return runtime, f"local unavailable; {runtime.value} has headroom"

        return Runtime.OPEN_CODE, "no subscription headroom; local is the only option"

    def _open_code_ready(self) -> bool:
        """Whether local inference has at least one bench-eligible model.

        An `open_code` runtime with no eligible model is not a usable fallback,
        so this checks the routing table rather than merely whether a backend
        process is listening.
        """
        if self.subscriptions_only:
            return False

        from .router.policy import RoutingTable

        table = RoutingTable.load(
            self.env.get("CTSWARM_ROUTING", "bench/results/routing.json")
        )
        local_backends = {"ollama", "mlx", "lmstudio"}
        return any(
            score.eligible_for_agent_roles and score.backend in local_backends
            for score in table.all()
        )

    def report(self) -> dict:
        """Headroom across every runtime, for `ctswarm capacity`."""
        return {
            runtime.value: self.headroom(runtime).to_dict()
            for runtime in Runtime
        }
