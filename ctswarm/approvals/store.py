"""Persistence for approval requests and decisions.

Separate from the ledger because approvals have a different durability contract:
the ledger is telemetry that may be pruned, while an approval record is an audit
artifact that must survive everything, including a stack restart mid-decision.

The plan is explicit that agents may never modify approval thresholds or the
audit log. Nothing in this module offers an update-in-place or delete path for a
recorded decision; decisions are append-only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .rules import ApprovalRequest, Decision, Risk

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    dedupe_key   TEXT PRIMARY KEY,
    build_id     TEXT NOT NULL,
    action       TEXT NOT NULL,
    rule_name    TEXT NOT NULL,
    risk         TEXT NOT NULL,
    reversible   INTEGER NOT NULL,
    payload      TEXT NOT NULL,
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    notified_at  REAL,
    channel      TEXT,
    message_ref  TEXT
);
CREATE INDEX IF NOT EXISTS idx_appr_build ON approvals(build_id);

-- Append-only. One row per decision event; the current state of a request is
-- its latest row. There is deliberately no UPDATE path.
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key  TEXT NOT NULL,
    decision    TEXT NOT NULL,
    decided_by  TEXT,
    decided_at  REAL NOT NULL,
    note        TEXT,
    source      TEXT
);
CREATE INDEX IF NOT EXISTS idx_dec_key ON decisions(dedupe_key, decided_at);
"""


class ApprovalStore:
    """Append-only approval record."""

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
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def create(
        self, request: ApprovalRequest, *, ttl_s: float = 86400.0
    ) -> tuple[bool, dict]:
        """Record a request. Returns (is_new, row).

        Deduplication is the whole point. Retries, replans, and the middle
        escalation loop re-surface the same action many times over a build; the
        pilot criterion is exactly one actionable card per high-risk action. A
        conflict on the content-derived key is the normal case, not an error.
        """
        now = time.time()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM approvals WHERE dedupe_key=?", (request.dedupe_key,)
            ).fetchone()
            if existing:
                return False, dict(existing)

            conn.execute(
                "INSERT INTO approvals (dedupe_key, build_id, action, rule_name, risk,"
                " reversible, payload, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    request.dedupe_key,
                    request.build_id,
                    request.action,
                    request.rule_name,
                    request.risk.value,
                    1 if request.reversible else 0,
                    json.dumps(request.to_dict()),
                    now,
                    now + ttl_s,
                ),
            )
            row = conn.execute(
                "SELECT * FROM approvals WHERE dedupe_key=?", (request.dedupe_key,)
            ).fetchone()
            return True, dict(row)

    def mark_notified(
        self, dedupe_key: str, *, channel: str, message_ref: str = ""
    ) -> None:
        """Record that a card was delivered.

        Used to guarantee at-most-one notification per request even if the
        service restarts between creating and sending.
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE approvals SET notified_at=?, channel=?, message_ref=?"
                " WHERE dedupe_key=? AND notified_at IS NULL",
                (time.time(), channel, message_ref, dedupe_key),
            )

    def decide(
        self,
        dedupe_key: str,
        decision: Decision,
        *,
        decided_by: str = "",
        note: str = "",
        source: str = "",
    ) -> dict:
        """Append a decision. Never overwrites a prior one."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO decisions (dedupe_key, decision, decided_by, decided_at,"
                " note, source) VALUES (?,?,?,?,?,?)",
                (dedupe_key, decision.value, decided_by, time.time(), note, source),
            )
            row = conn.execute(
                "SELECT * FROM decisions WHERE dedupe_key=? ORDER BY decided_at DESC"
                " LIMIT 1",
                (dedupe_key,),
            ).fetchone()
        return dict(row)

    def current_decision(self, dedupe_key: str) -> Optional[dict]:
        """Latest decision for a request, or None if still open."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM decisions WHERE dedupe_key=? ORDER BY decided_at DESC"
                " LIMIT 1",
                (dedupe_key,),
            ).fetchone()
        return dict(row) if row else None

    def get(self, dedupe_key: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
        if not row:
            return None
        record = dict(row)
        record["payload"] = json.loads(record["payload"])
        record["decision"] = self.current_decision(dedupe_key)
        return record

    def pending(self) -> list[dict]:
        """Open requests, newest first.

        Expiry is evaluated on read rather than by a background sweeper. A
        sweeper that dies would silently leave requests looking open forever;
        computing it from stored timestamps cannot drift.
        """
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.* FROM approvals a"
                " LEFT JOIN decisions d ON d.dedupe_key = a.dedupe_key"
                " WHERE d.id IS NULL ORDER BY a.created_at DESC"
            ).fetchall()

        pending = []
        for row in rows:
            record = dict(row)
            record["payload"] = json.loads(record["payload"])
            record["expired"] = record["expires_at"] < now
            pending.append(record)
        return pending

    def all_for_build(self, build_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE build_id=? ORDER BY created_at",
                (build_id,),
            ).fetchall()
        result = []
        for row in rows:
            record = dict(row)
            record["payload"] = json.loads(record["payload"])
            record["decision"] = self.current_decision(record["dedupe_key"])
            result.append(record)
        return result

    def notification_count(self, dedupe_key: str) -> int:
        """How many times a card was delivered.

        The pilot requires that a denied approval does not produce repeated
        notifications, so this is asserted directly by the probe suite.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT notified_at FROM approvals WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
        return 1 if row and row["notified_at"] is not None else 0
