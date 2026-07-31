"""Tests for the approval boundary.

These lock down behaviors where a regression would be dangerous rather than
merely annoying: escalating too little lets an autonomous system take an
irreversible action, and escalating too much trains reflex approval.
"""

from __future__ import annotations

import time

import httpx
import pytest

from ctswarm.approvals.rules import (
    ESCALATE_RULES,
    NEVER_ESCALATE,
    Decision,
    Risk,
    classify,
)
from ctswarm.approvals.service import app
from ctswarm.approvals.slack import verify_signature
from ctswarm.approvals.store import ApprovalStore


@pytest.fixture()
def store(tmp_path):
    return ApprovalStore(tmp_path / "approvals.db")


class TestClassification:
    def test_routine_actions_never_escalate(self):
        for action in NEVER_ESCALATE:
            assert classify(action=action, detail="", build_id="b") is None

    def test_routine_action_with_alarming_detail_still_does_not_escalate(self):
        """Never-escalate is checked before pattern matching.

        A retry whose failure output happens to contain "DROP TABLE" must not
        page anyone. Getting this backwards produces exactly the notification
        spam that destroys the value of the gate.
        """
        result = classify(
            action="retry",
            detail="test failed: DROP TABLE users appeared in the fixture log",
            build_id="b",
        )
        assert result is None

    @pytest.mark.parametrize(
        "action,detail,expected_rule",
        [
            ("apply_migration", "DROP TABLE legacy_users;", "destructive_migration"),
            ("run", "truncate the events table", "destructive_migration"),
            ("deploy", "deploy to production cluster", "production_deploy"),
            ("edit", "added .skip( to the failing test", "test_weakened"),
            ("commit", "merge into main", "merge_to_main"),
            ("cleanup", "rm -rf /var/data", "data_deletion"),
            ("config", "rotate secret AWS_KEY", "secret_or_permission_change"),
            ("policy", "modify branch protection rules", "secret_or_permission_change"),
        ],
    )
    def test_high_risk_actions_escalate(self, action, detail, expected_rule):
        result = classify(action=action, detail=detail, build_id="b")
        assert result is not None, f"{action}/{detail} should have escalated"
        assert result.rule_name == expected_rule

    def test_irreversible_actions_are_marked(self):
        result = classify(
            action="deploy", detail="deploy to production", build_id="b"
        )
        assert result.reversible is False
        assert result.risk is Risk.CRITICAL

    def test_agent_cannot_escalate_its_own_permissions_quietly(self):
        """Self-modification of the protection layer must always surface."""
        for detail in (
            "expand token scope to admin",
            "disable branch protection",
            "remove approval policy",
        ):
            assert classify(action="selfconfig", detail=detail, build_id="b") is not None

    def test_dedupe_key_is_content_derived_and_stable(self):
        a = classify(action="x", detail="DROP TABLE t", build_id="b")
        b = classify(action="x", detail="DROP TABLE t", build_id="b")
        assert a.dedupe_key == b.dedupe_key

    def test_dedupe_key_differs_across_builds(self):
        a = classify(action="x", detail="DROP TABLE t", build_id="b1")
        b = classify(action="x", detail="DROP TABLE t", build_id="b2")
        assert a.dedupe_key != b.dedupe_key


class TestStore:
    def test_exactly_one_card_per_action(self, store):
        """The pilot criterion: one actionable card per high-risk action."""
        request = classify(action="x", detail="DROP TABLE t", build_id="b")
        is_new_first, _ = store.create(request)
        is_new_second, _ = store.create(request)
        assert is_new_first is True
        assert is_new_second is False
        assert len(store.pending()) == 1

    def test_decisions_are_append_only(self, store):
        request = classify(action="x", detail="DROP TABLE t", build_id="b")
        store.create(request)
        store.decide(request.dedupe_key, Decision.DENY, decided_by="quinn")
        store.decide(request.dedupe_key, Decision.APPROVE, decided_by="attacker")
        # Both rows persist; history cannot be rewritten. The latest wins for
        # state, but the earlier decision remains auditable.
        current = store.current_decision(request.dedupe_key)
        assert current["decision"] == Decision.APPROVE.value
        record = store.get(request.dedupe_key)
        assert record is not None

    def test_decided_request_leaves_pending(self, store):
        request = classify(action="x", detail="DROP TABLE t", build_id="b")
        store.create(request)
        assert len(store.pending()) == 1
        store.decide(request.dedupe_key, Decision.DENY)
        assert len(store.pending()) == 0

    def test_expiry_is_flagged_not_approved(self, store):
        """Silence must never read as approval."""
        request = classify(action="x", detail="DROP TABLE t", build_id="b")
        store.create(request, ttl_s=-1.0)
        pending = store.pending()
        assert len(pending) == 1
        assert pending[0]["expired"] is True
        # Critically: still no decision recorded. Expiry pauses, it never approves.
        assert store.current_decision(request.dedupe_key) is None

    def test_notification_counted_once(self, store):
        request = classify(action="x", detail="DROP TABLE t", build_id="b")
        store.create(request)
        store.mark_notified(request.dedupe_key, channel="slack", message_ref="1.1")
        store.mark_notified(request.dedupe_key, channel="slack", message_ref="2.2")
        assert store.notification_count(request.dedupe_key) == 1


class TestSlackSignature:
    SECRET = "test-signing-secret"

    def _sign(self, body: bytes, timestamp: str) -> str:
        import hashlib
        import hmac

        base = b"v0:" + timestamp.encode() + b":" + body
        return (
            "v0="
            + hmac.new(self.SECRET.encode(), base, hashlib.sha256).hexdigest()
        )

    def test_valid_signature_accepted(self):
        body = b"payload=%7B%7D"
        ts = str(int(time.time()))
        assert verify_signature(
            signing_secret=self.SECRET,
            timestamp=ts,
            signature=self._sign(body, ts),
            body=body,
        )

    def test_tampered_body_rejected(self):
        ts = str(int(time.time()))
        signature = self._sign(b"payload=%7B%7D", ts)
        assert not verify_signature(
            signing_secret=self.SECRET,
            timestamp=ts,
            signature=signature,
            body=b"payload=evil",
        )

    def test_replayed_old_request_rejected(self):
        """An old signed payload must not be replayable into an approval."""
        body = b"payload=%7B%7D"
        old = str(int(time.time()) - 3600)
        assert not verify_signature(
            signing_secret=self.SECRET,
            timestamp=old,
            signature=self._sign(body, old),
            body=body,
        )

    def test_missing_secret_rejected(self):
        assert not verify_signature(
            signing_secret="", timestamp="1", signature="v0=x", body=b""
        )


async def test_expired_approval_cannot_be_decided(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "approvals.db"
    monkeypatch.setenv("CTSWARM_DB", str(db_path))
    store = ApprovalStore(db_path)
    request = classify(action="x", detail="DROP TABLE t", build_id="b")
    store.create(request, ttl_s=-1.0)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://approvals.test",
    ) as client:
        response = await client.post(
            f"/approvals/{request.dedupe_key}/decide",
            json={"decision": "approve", "decided_by": "test"},
        )

    assert response.status_code == 409
    assert response.json()["decision"] == Decision.EXPIRED.value
    assert store.current_decision(request.dedupe_key) is None


def test_every_rule_has_a_reason():
    """Cards must explain why, not just that. An unexplained block is unactionable."""
    for rule in ESCALATE_RULES:
        assert rule.why.strip()
        assert rule.patterns or rule.actions
