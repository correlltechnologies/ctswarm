"""Anti-slop gates.

Section 7 of the plan: completion must be machine-readable evidence, and "the
agent says it is done" carries no authority. These checks target the specific
ways autonomous output looks finished without being finished.

Every check here is deterministic and runs against the integrated diff. None of
them asks a model whether the work is good, because a model that produced the
slop is not a credible judge of it and a second model of the same family shares
the failure mode.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    """One anti-slop violation."""

    check: str
    severity: str  # blocker | warning
    file: str
    line: int
    excerpt: str
    why: str

    def to_dict(self) -> dict:
        return {
            "check": self.check,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "excerpt": self.excerpt[:200],
            "why": self.why,
        }


# Source extensions worth scanning. Scanning lockfiles and minified bundles
# produces noise that trains people to ignore the report.
SOURCE_SUFFIXES = frozenset(
    {
        ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
        ".py", ".go", ".rs", ".java", ".rb", ".php",
        ".vue", ".svelte", ".astro",
        ".css", ".scss", ".html",
        ".sql", ".sh",
        ".md",
    }
)

SKIP_DIR_PARTS = frozenset(
    {
        "node_modules", ".git", "dist", "build", ".next", "vendor",
        "__pycache__", ".venv", "venv", "coverage", ".pytest_cache",
    }
)


@dataclass(frozen=True)
class Check:
    name: str
    severity: str
    why: str
    pattern: re.Pattern
    # Restrict to these suffixes, or None for all source files.
    suffixes: frozenset | None = None
    # A line matching this is exempt, used to allow deliberate, declared cases.
    exempt: re.Pattern | None = None


# An explicit, greppable opt-out. Anything genuinely intended stays in the
# codebase with a visible marker rather than being silently tolerated.
EXEMPT_MARKER = re.compile(r"ctswarm:allow\b", re.IGNORECASE)


CHECKS: tuple[Check, ...] = (
    Check(
        name="placeholder_copy",
        severity="blocker",
        why="Placeholder text shipped as if it were real content.",  # ctswarm:allow detector
        pattern=re.compile(
            r"\b(lorem ipsum|dolor sit amet|TODO:? *fill|your (name|company|text) here"  # ctswarm:allow detector
            r"|placeholder text|replace this|sample text|foo ?bar ?baz)\b",  # ctswarm:allow detector
            re.IGNORECASE,
        ),
    ),
    Check(
        name="coming_soon",
        severity="blocker",
        why="A control that announces future functionality instead of working.",  # ctswarm:allow detector
        pattern=re.compile(
            r"\b(coming soon|not implemented yet|under construction|todo: implement"  # ctswarm:allow detector
            r"|feature coming|stay tuned)\b",  # ctswarm:allow detector
            re.IGNORECASE,
        ),
    ),
    Check(
        name="dead_control",
        severity="blocker",
        why="An interactive control wired to nothing, which looks functional but is not.",
        pattern=re.compile(
            r"onClick=\{\s*\(\s*\)\s*=>\s*\{\s*\}\s*\}"
            r"|onClick=\{\s*\(\s*\)\s*=>\s*(null|undefined)\s*\}"
            r"|href=[\"']#[\"']"
            r"|onClick=\{\s*noop\s*\}",
            re.IGNORECASE,
        ),
        suffixes=frozenset({".tsx", ".jsx", ".vue", ".svelte", ".html", ".astro"}),
    ),
    Check(
        name="fabricated_metric",
        severity="blocker",
        why="A hardcoded statistic presented as if measured.",
        pattern=re.compile(
            r"\b(99\.9+% uptime|10,?000\+? (users|customers)|trusted by [\d,]+"
            r"|[\d,]+\+? happy customers|4\.9/5 stars)\b",
            re.IGNORECASE,
        ),
    ),
    Check(
        name="weakened_test",
        severity="blocker",
        why="A test disabled or emptied rather than made to pass honestly.",
        pattern=re.compile(
            r"\b(it|test|describe)\.(skip|todo)\s*\("
            r"|\bx(it|describe)\s*\("
            r"|@pytest\.mark\.skip"
            r"|t\.Skip\(\)"
            r"|// *@ts-nocheck",
        ),
    ),
    Check(
        name="silenced_error",
        severity="blocker",
        why="An error swallowed without handling, which hides failure from every gate.",
        pattern=re.compile(
            r"catch\s*\([^)]*\)\s*\{\s*\}"
            r"|except\s+\w*(Exception)?\s*:\s*\n\s*pass\b"
            r"|\.catch\(\s*\(\s*\)\s*=>\s*\{\s*\}\s*\)",
            re.MULTILINE,
        ),
    ),
    Check(
        name="ai_filler_prose",
        severity="warning",
        why="Generic model-generated filler that carries no project-specific meaning.",
        pattern=re.compile(
            r"\b(in today's fast-paced|revolutionize the way|seamlessly integrate"
            r"|unlock the power of|take your \w+ to the next level"
            r"|delve into|it's important to note that)\b",
            re.IGNORECASE,
        ),
    ),
    Check(
        name="hardcoded_secret_shape",
        severity="blocker",
        why="A credential-shaped literal committed to source.",
        pattern=re.compile(
            r"(sk-ant-[A-Za-z0-9_\-]{12,}|sk-or-v1-[A-Za-z0-9]{12,}"
            r"|ghp_[A-Za-z0-9]{20,}|xoxb-[A-Za-z0-9\-]{20,}"
            r"|AKIA[0-9A-Z]{16})",
        ),
    ),
)


def _should_scan(path: Path) -> bool:
    if any(part in SKIP_DIR_PARTS for part in path.parts):
        return False
    return path.suffix.lower() in SOURCE_SUFFIXES


def scan_text(path: str, text: str) -> list[Finding]:
    """Run every applicable check against one file's contents."""
    suffix = Path(path).suffix.lower()
    findings: list[Finding] = []
    lines = text.splitlines()

    for check in CHECKS:
        if check.suffixes is not None and suffix not in check.suffixes:
            continue
        for match in check.pattern.finditer(text):
            line_number = text.count("\n", 0, match.start()) + 1
            excerpt = (
                lines[line_number - 1].strip() if line_number <= len(lines) else ""
            )
            # An explicitly marked allowance is respected, on that line only.
            if EXEMPT_MARKER.search(excerpt):
                continue
            findings.append(
                Finding(
                    check=check.name,
                    severity=check.severity,
                    file=path,
                    line=line_number,
                    excerpt=excerpt,
                    why=check.why,
                )
            )
    return findings


def scan_paths(root: str | Path, paths: Iterable[str] | None = None) -> list[Finding]:
    """Scan a repository, or a specific set of files within it."""
    root = Path(root)
    targets: list[Path]
    if paths is not None:
        targets = [root / p for p in paths]
    else:
        targets = [p for p in root.rglob("*") if p.is_file()]

    findings: list[Finding] = []
    for target in targets:
        if not target.is_file():
            continue
        relative = target.relative_to(root) if target.is_relative_to(root) else target
        if not _should_scan(relative):
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        findings.extend(scan_text(str(relative), text))
    return findings


def changed_files(repo: str | Path, base_ref: str) -> list[str]:
    """Files changed against a base ref.

    Scanning only the diff is deliberate. A pre-existing violation elsewhere in
    the repository is not this build's fault, and blocking on it would make the
    gate impossible to adopt on a real codebase.
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


@dataclass
class AntiSlopReport:
    findings: list[Finding] = field(default_factory=list)
    scanned_files: int = 0

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocker"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def passed(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "scanned_files": self.scanned_files,
            "blocker_count": len(self.blockers),
            "warning_count": len(self.warnings),
            "findings": [f.to_dict() for f in self.findings],
        }


def check_repo(
    repo: str | Path, *, base_ref: str | None = None
) -> AntiSlopReport:
    """Run the anti-slop gate over a repository or its diff."""
    paths = changed_files(repo, base_ref) if base_ref else None
    findings = scan_paths(repo, paths)
    scanned = len(paths) if paths is not None else len(findings)
    return AntiSlopReport(findings=findings, scanned_files=scanned)
