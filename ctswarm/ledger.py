"""Usage, quota, and outcome ledger.

Every model call, routing decision, quota observation, and build outcome lands
here. Three consumers depend on it:

- the router, for circuit-breaker state and success-rate scoring
- the capacity manager, for remaining subscription and budget headroom
- ``ctswarm verify``, which asserts probe outcomes against recorded facts rather
  than against log scraping

SQLite is deliberate. The ledger must survive a stack restart (probe 5 depends on
exactly that) without requiring a database service to be healthy first.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    build_id      TEXT,
    role          TEXT,
    tier          TEXT,
    virtual_model TEXT,
    backend       TEXT    NOT NULL,
    model_ref     TEXT    NOT NULL,
    prompt_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    latency_ms    INTEGER DEFAULT 0,
    ok            INTEGER NOT NULL,
    failure_kind  TEXT,
    cost_usd      REAL    DEFAULT 0.0,
    attempt       INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_calls_model_ts ON calls(model_ref, ts);
CREATE INDEX IF NOT EXISTS idx_calls_build    ON calls(build_id);

CREATE TABLE IF NOT EXISTS breaker (
    model_ref     TEXT PRIMARY KEY,
    consecutive   INTEGER NOT NULL DEFAULT 0,
    open_until    REAL    NOT NULL DEFAULT 0,
    last_failure  TEXT,
    trips         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS quota (
    provider      TEXT PRIMARY KEY,
    observed_at   REAL NOT NULL,
    remaining     REAL,
    limit_value   REAL,
    resets_at     REAL,
    raw           TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    build_id  TEXT,
    kind      TEXT NOT NULL,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts);
"""

# A failure that means "this model is misbehaving" and should count toward the
# circuit breaker, as opposed to one that means "this request was bad", which
# should not. Getting this distinction wrong makes the breaker either useless or
# trigger-happy.
BREAKER_FAILURES = frozenset(
    {
        "malformed_tool_call",
        "schema_violation",
        "timeout",
        "connection_error",
        "empty_response",
        "truncated_response",
        "rate_limited",
        "server_error",
    }
)


@dataclass(frozen=True)
class ModelStats:
    """Rolling statistics for one model over the scoring window."""

    model_ref: str
    calls: int
    successes: int
    p50_latency_ms: float
    failure_kinds: dict[str, int]

    @property
    def success_rate(self) -> float:
        """Laplace-smoothed success rate.

        Smoothing matters here: a model with one lucky success must not outrank a
        model with 400 calls at 97%, and a newly added model must not be locked
        out by a single early failure.
        """
        return (self.successes + 1.0) / (self.calls + 2.0)


class Ledger:
    """Thread-safe SQLite ledger."""

    def __init__(self, path: str | Path = "var/ctswarm.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            # WAL keeps the router readable while a build writes heavily, and
            # survives the mid-build stack restart that probe 5 performs.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- writes ------------------------------------------------------------

    def record_call(
        self,
        *,
        backend: str,
        model_ref: str,
        ok: bool,
        build_id: str | None = None,
        role: str | None = None,
        tier: str | None = None,
        virtual_model: str | None = None,
        prompt_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int = 0,
        failure_kind: str | None = None,
        cost_usd: float = 0.0,
        attempt: int = 1,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO calls (ts, build_id, role, tier, virtual_model, backend,"
                " model_ref, prompt_tokens, output_tokens, latency_ms, ok,"
                " failure_kind, cost_usd, attempt)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    build_id,
                    role,
                    tier,
                    virtual_model,
                    backend,
                    model_ref,
                    prompt_tokens,
                    output_tokens,
                    latency_ms,
                    1 if ok else 0,
                    failure_kind,
                    cost_usd,
                    attempt,
                ),
            )
            self._update_breaker(conn, model_ref, ok, failure_kind)

    def _update_breaker(
        self,
        conn: sqlite3.Connection,
        model_ref: str,
        ok: bool,
        failure_kind: str | None,
    ) -> None:
        """Advance circuit-breaker state for a model.

        Backoff is exponential in the number of historical trips, so a model that
        keeps coming back broken is sidelined for progressively longer instead of
        being retried at a fixed interval forever.
        """
        if ok:
            conn.execute(
                "INSERT INTO breaker (model_ref, consecutive, open_until) VALUES (?,0,0)"
                " ON CONFLICT(model_ref) DO UPDATE SET consecutive=0, open_until=0",
                (model_ref,),
            )
            return

        if failure_kind not in BREAKER_FAILURES:
            return

        row = conn.execute(
            "SELECT consecutive, trips FROM breaker WHERE model_ref=?", (model_ref,)
        ).fetchone()
        consecutive = (row["consecutive"] if row else 0) + 1
        trips = row["trips"] if row else 0
        open_until = 0.0

        if consecutive >= 3:
            trips += 1
            backoff = min(60.0 * (2 ** (trips - 1)), 3600.0)
            open_until = time.time() + backoff
            consecutive = 0

        conn.execute(
            "INSERT INTO breaker (model_ref, consecutive, open_until, last_failure, trips)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(model_ref) DO UPDATE SET consecutive=excluded.consecutive,"
            " open_until=excluded.open_until, last_failure=excluded.last_failure,"
            " trips=excluded.trips",
            (model_ref, consecutive, open_until, failure_kind, trips),
        )

    def record_event(
        self, kind: str, detail: Any = None, build_id: str | None = None
    ) -> None:
        payload = detail if isinstance(detail, str) else json.dumps(detail, default=str)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO events (ts, build_id, kind, detail) VALUES (?,?,?,?)",
                (time.time(), build_id, kind, payload),
            )

    def record_quota(
        self,
        provider: str,
        *,
        remaining: float | None = None,
        limit_value: float | None = None,
        resets_at: float | None = None,
        raw: Any = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO quota (provider, observed_at, remaining, limit_value,"
                " resets_at, raw) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(provider) DO UPDATE SET observed_at=excluded.observed_at,"
                " remaining=excluded.remaining, limit_value=excluded.limit_value,"
                " resets_at=excluded.resets_at, raw=excluded.raw",
                (
                    provider,
                    time.time(),
                    remaining,
                    limit_value,
                    resets_at,
                    json.dumps(raw, default=str) if raw is not None else None,
                ),
            )

    # -- reads -------------------------------------------------------------

    def is_open(self, model_ref: str) -> bool:
        """True when the breaker is open, meaning the model must not be routed to."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT open_until FROM breaker WHERE model_ref=?", (model_ref,)
            ).fetchone()
        return bool(row) and row["open_until"] > time.time()

    def stats(self, model_ref: str, window_s: float = 86400.0) -> ModelStats:
        since = time.time() - window_s
        with self._connect() as conn:
            agg = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(ok),0) AS ok FROM calls"
                " WHERE model_ref=? AND ts>=?",
                (model_ref, since),
            ).fetchone()
            latencies = [
                r["latency_ms"]
                for r in conn.execute(
                    "SELECT latency_ms FROM calls WHERE model_ref=? AND ts>=? AND ok=1"
                    " ORDER BY latency_ms",
                    (model_ref, since),
                )
            ]
            kinds = {
                r["failure_kind"]: r["n"]
                for r in conn.execute(
                    "SELECT failure_kind, COUNT(*) AS n FROM calls"
                    " WHERE model_ref=? AND ts>=? AND ok=0 AND failure_kind IS NOT NULL"
                    " GROUP BY failure_kind",
                    (model_ref, since),
                )
            }
        p50 = latencies[len(latencies) // 2] if latencies else 0.0
        return ModelStats(
            model_ref=model_ref,
            calls=agg["n"],
            successes=agg["ok"],
            p50_latency_ms=float(p50),
            failure_kinds=kinds,
        )

    def quota(self, provider: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM quota WHERE provider=?", (provider,)
            ).fetchone()
        return dict(row) if row else None

    def events(self, kind: str | None = None, build_id: str | None = None) -> list[dict]:
        clauses, params = [], []
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if build_id:
            clauses.append("build_id=?")
            params.append(build_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM events {where} ORDER BY ts", params
            ).fetchall()
        return [dict(r) for r in rows]

    def usage_summary(self, build_id: str | None = None) -> dict:
        """Per-backend rollup. Feeds the pilot criterion that at least 80% of
        routine agent calls are served locally."""
        where, params = ("WHERE build_id=?", [build_id]) if build_id else ("", [])
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT backend, COUNT(*) AS calls, COALESCE(SUM(ok),0) AS ok,"
                f" COALESCE(SUM(cost_usd),0) AS cost,"
                f" COALESCE(SUM(prompt_tokens+output_tokens),0) AS tokens"
                f" FROM calls {where} GROUP BY backend",
                params,
            ).fetchall()
        by_backend = {r["backend"]: dict(r) for r in rows}
        total = sum(r["calls"] for r in rows) or 1
        local = sum(
            r["calls"] for r in rows if r["backend"] in ("ollama", "mlx", "lmstudio")
        )
        return {
            "by_backend": by_backend,
            "total_calls": sum(r["calls"] for r in rows),
            "local_fraction": local / total,
            "total_cost_usd": sum(r["cost"] for r in rows),
        }
