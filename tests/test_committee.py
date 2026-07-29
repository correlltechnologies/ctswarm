"""Tests for committee independence and scanner authority.

These encode the two claims the committee design rests on. If either regresses,
the committee still returns confident verdicts, it just stops meaning anything,
which is the dangerous kind of failure.
"""

from __future__ import annotations

import asyncio

import pytest

from ctswarm.committee import (
    CommitteeResult,
    Rule,
    ScannerResult,
    Verdict,
    Vote,
    _parse_vote,
    convene,
    eligible_members,
    family_of,
)
from ctswarm.router.policy import BenchScore, RoutingTable


def _vote(model: str, verdict: Verdict, confidence: float = 0.9) -> Vote:
    return Vote(model, family_of(model), verdict, confidence, "because")


class TestFamilyIndependence:
    @pytest.mark.parametrize(
        "model,family",
        [
            ("qwen3.5:9b", "qwen"),
            ("qwen2.5-coder:7b", "qwen"),
            ("ornith:9b", "qwen"),  # qwen35 architecture per `ollama show`
            ("granite4.1:8b", "granite"),
            ("claude-sonnet-4", "anthropic"),
            ("gpt-5.3-codex", "openai"),
        ],
    )
    def test_family_detection(self, model, family):
        assert family_of(model) == family

    def test_same_family_votes_collapse_to_one(self):
        """Three Qwen models are one opinion sampled three times.

        Counting them as three independent votes manufactures a consensus, which
        is the precise failure this module exists to prevent.
        """
        result = CommitteeResult(question="q")
        result.votes = [
            _vote("qwen3.5:9b", Verdict.APPROVE),
            _vote("qwen3.5:4b", Verdict.APPROVE),
            _vote("qwen2.5-coder:7b", Verdict.APPROVE),
        ]
        assert len(result.independent_votes) == 1
        assert result.families == {"qwen"}

    def test_disagreement_within_a_family_takes_the_reject(self):
        """A split family has not agreed; taking the optimistic vote would
        launder uncertainty into approval."""
        result = CommitteeResult(question="q")
        result.votes = [
            _vote("qwen3.5:9b", Verdict.APPROVE),
            _vote("qwen3.5:4b", Verdict.REJECT),
        ]
        assert len(result.independent_votes) == 1
        assert result.independent_votes[0].verdict is Verdict.REJECT

    def test_insufficient_families_escalates_rather_than_approving(self):
        result = asyncio.run(
            convene(
                question="q",
                context="",
                backend=None,
                members=[],
                rule=Rule.MAJORITY,
                min_families=2,
            )
        )
        assert result.approved is False
        assert result.needs_human is True


class TestScannerAuthority:
    def test_scanner_failure_overrides_unanimous_approval(self):
        """Deterministic evidence beats model opinion, always."""
        result = asyncio.run(
            convene(
                question="is this safe",
                context="",
                backend=None,
                members=[],
                scanners=[
                    ScannerResult("secret-scan", False, "found ghp_ token", ("cfg.ts:3",))
                ],
                rule=Rule.MAJORITY,
            )
        )
        assert result.approved is False
        assert "scanner" in result.reason.lower()

    def test_passing_scanners_do_not_by_themselves_approve(self):
        """Scanners can block but cannot substitute for a quorum."""
        result = asyncio.run(
            convene(
                question="is this safe",
                context="",
                backend=None,
                members=[],
                scanners=[ScannerResult("dep-audit", True, "0 advisories")],
                rule=Rule.MAJORITY,
                min_families=2,
            )
        )
        assert result.approved is False
        assert result.needs_human is True


class TestVoteParsing:
    def test_unparseable_response_abstains_never_approves(self):
        """An unreadable vote must never become silent consent."""
        vote = _parse_vote("qwen3.5:9b", "I think it's probably fine, honestly")
        assert vote.verdict is Verdict.ABSTAIN
        assert vote.counted is False

    def test_empty_response_abstains(self):
        assert _parse_vote("qwen3.5:9b", "").verdict is Verdict.ABSTAIN

    def test_fenced_json_is_accepted(self):
        vote = _parse_vote(
            "granite4.1:8b",
            '```json\n{"verdict":"reject","confidence":0.8,'
            '"reasoning":"sqli","findings":["injection"]}\n```',
        )
        assert vote.verdict is Verdict.REJECT
        assert vote.findings == ("injection",)

    def test_confidence_is_clamped(self):
        vote = _parse_vote(
            "qwen3.5:9b", '{"verdict":"approve","confidence":9.5,"reasoning":"x"}'
        )
        assert vote.confidence == 1.0


class TestQuorumRules:
    def _result(self, votes, rule):
        result = CommitteeResult(question="q", rule=rule, min_families=2)
        result.votes = votes
        return result

    def test_any_reject_blocks(self):
        result = asyncio.run(
            convene(
                question="q",
                context="",
                backend=_StubBackend(
                    {
                        "qwen3.5:9b": '{"verdict":"approve","confidence":0.9,"reasoning":"ok"}',
                        "granite4.1:8b": '{"verdict":"reject","confidence":0.9,"reasoning":"bug"}',
                    }
                ),
                members=["qwen3.5:9b", "granite4.1:8b"],
                rule=Rule.ANY_REJECT_BLOCKS,
                min_families=2,
            )
        )
        assert result.approved is False

    def test_tie_escalates_to_human(self):
        result = asyncio.run(
            convene(
                question="q",
                context="",
                backend=_StubBackend(
                    {
                        "qwen3.5:9b": '{"verdict":"approve","confidence":0.9,"reasoning":"ok"}',
                        "granite4.1:8b": '{"verdict":"reject","confidence":0.9,"reasoning":"no"}',
                    }
                ),
                members=["qwen3.5:9b", "granite4.1:8b"],
                rule=Rule.MAJORITY,
                min_families=2,
            )
        )
        assert result.approved is False
        assert result.needs_human is True


class TestMemberSelection:
    def test_ineligible_models_cannot_sit_on_a_committee(self):
        """A model that fails the tool-call or schema gate is not a reviewer."""
        table = RoutingTable(
            {
                "qwen3.5:9b": BenchScore(
                    "qwen3.5:9b", "ollama", 1.0, 1.0, 1.0, 1.0, True, 100, 90, 60000
                ),
                "qwen2.5-coder:7b": BenchScore(
                    "qwen2.5-coder:7b", "ollama", 0.25, 1.0, 1.0, 1.0, True, 100, 90, 60000
                ),
            }
        )
        assert eligible_members(table) == ["qwen3.5:9b"]

    def test_one_member_per_family(self):
        table = RoutingTable(
            {
                "qwen3.5:9b": BenchScore(
                    "qwen3.5:9b", "ollama", 1.0, 1.0, 1.0, 1.0, True, 100, 90, 60000
                ),
                "qwen3.5:4b": BenchScore(
                    "qwen3.5:4b", "ollama", 1.0, 1.0, 1.0, 1.0, True, 100, 140, 60000
                ),
                "granite4.1:8b": BenchScore(
                    "granite4.1:8b", "ollama", 1.0, 1.0, 1.0, 1.0, True, 100, 96, 60000
                ),
            }
        )
        members = eligible_members(table)
        assert len(members) == 2
        assert {family_of(m) for m in members} == {"qwen", "granite"}


class _StubBackend:
    """Returns canned content per model, with no network."""

    name = "stub"

    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses

    async def chat(self, request, model_ref):
        from ctswarm.backends.base import ChatResponse

        return ChatResponse(
            ok=True,
            body={
                "choices": [
                    {"message": {"content": self.responses.get(model_ref, "")}}
                ]
            },
            backend="stub",
            model_ref=model_ref,
            latency_ms=1,
        )
