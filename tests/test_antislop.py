"""Regression coverage for the deterministic anti-slop gate."""

from __future__ import annotations

from ctswarm.evidence.antislop import check_repo, scan_text


def test_ctswarm_source_passes_its_own_antislop_gate() -> None:
    report = check_repo("ctswarm")

    assert report.blockers == []


def test_explicit_allowance_is_line_scoped() -> None:
    findings = scan_text(
        "page.tsx",
        "\n".join(
            [
                'const intentional = "coming soon"; // ctswarm:allow documented state',
                'const accidental = "coming soon";',
            ]
        ),
    )

    assert [(finding.check, finding.line) for finding in findings] == [
        ("coming_soon", 2)
    ]
