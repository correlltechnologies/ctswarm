"""Bench task suite: what a model must do to hold a SWE-AF agent role.

These are not general capability benchmarks. Each task targets a specific way a
model breaks an autonomous build, chosen because the failure stalls the DAG
rather than merely producing weaker output:

- **tool_call**: SWE-AF's 22 agents are defined by tool sets. A malformed call
  is not degraded output, it is a stuck agent.
- **schema**: agent results are parsed into typed schemas. Unparseable output
  fails the same way a crash does.
- **long_context**: agents read repo-scale context. A model that silently loses
  the middle of its window produces confidently wrong work.
- **instruction**: a model that invents a plausible answer rather than admitting
  it lacks information injects defects that survive review.
- **cancellation**: bounded retries and replanning require aborting in-flight
  work. A model that wedges on cancel breaks the control loop.

Every task is checkable programmatically. Nothing here is graded by another model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

# A deliberately unambiguous tool. If a model cannot call this correctly, no
# amount of prompt engineering will make the 22-agent tool surface work.
READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the full contents of a file at a given repository path.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repository-relative path to the file.",
                }
            },
            "required": ["path"],
        },
    },
}

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "grep",
        "description": "Search the repository for a regular expression.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex to search for."},
                "glob": {"type": "string", "description": "Optional file glob filter."},
            },
            "required": ["pattern"],
        },
    },
}

RUN_TESTS_TOOL = {
    "type": "function",
    "function": {
        "name": "run_tests",
        "description": "Run the test suite and return results.",
        "parameters": {
            "type": "object",
            "properties": {
                "suite": {
                    "type": "string",
                    "enum": ["unit", "integration", "e2e"],
                    "description": "Which suite to run.",
                },
                "pattern": {"type": "string"},
            },
            "required": ["suite"],
        },
    },
}


@dataclass(frozen=True)
class Task:
    """One measurable behavior."""

    name: str
    category: str  # tool_call | schema | long_context | instruction | cancel
    messages: list[dict]
    tools: list[dict] | None = None
    response_format: dict | None = None
    # Budgets throughout this suite clear reasoning overhead. Thinking models
    # spend their allowance on reasoning tokens before emitting any content, so a
    # budget sized for the visible answer scores them as broken rather than
    # measuring them. Observed here: qwen3.5:4b spends 181 reasoning tokens to
    # produce a 2-token answer, returning finish_reason=length and empty content
    # at max_tokens=64.
    max_tokens: int = 1536
    # Returns (passed, detail). Never calls a model.
    check: Callable[[dict], tuple[bool, str]] | None = None
    # Generous: the first call against a model includes cold-loading its weights
    # onto the accelerator, and reasoning models emit far more tokens than their
    # visible answer suggests.
    timeout_s: float = 180.0
    weight: float = 1.0
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# checkers
# ---------------------------------------------------------------------------


def _first_message(body: dict) -> dict:
    choices = body.get("choices") or []
    return (choices[0].get("message") or {}) if choices else {}


def _tool_calls(body: dict) -> list[dict]:
    return _first_message(body).get("tool_calls") or []


def _parse_args(call: dict) -> dict | None:
    """Decode tool-call arguments, tolerating the dict form some models emit."""
    import json

    function = call.get("function") or {}
    raw = function.get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def check_single_tool_call(
    expected_name: str, required_args: tuple[str, ...]
) -> Callable[[dict], tuple[bool, str]]:
    """Exactly one call to the expected tool, with required args present."""

    def _check(body: dict) -> tuple[bool, str]:
        calls = _tool_calls(body)
        if not calls:
            content = (_first_message(body).get("content") or "").strip()
            # Describing the call in prose instead of emitting one is the single
            # most common local-model failure, and it stalls an agent silently.
            return False, f"no tool call; answered in prose ({len(content)} chars)"
        if len(calls) != 1:
            return False, f"expected 1 call, got {len(calls)}"
        name = (calls[0].get("function") or {}).get("name")
        if name != expected_name:
            return False, f"called '{name}' instead of '{expected_name}'"
        args = _parse_args(calls[0])
        if args is None:
            return False, "arguments were not valid JSON"
        missing = [key for key in required_args if key not in args]
        if missing:
            return False, f"missing required args: {missing}"
        return True, "ok"

    return _check


def check_enum_arg(
    tool_name: str, arg: str, allowed: tuple[str, ...]
) -> Callable[[dict], tuple[bool, str]]:
    """Tool call whose enum-constrained argument is actually in the enum.

    Models that pass loose schemas often fail here by inventing an enum value,
    which a typed consumer rejects downstream.
    """

    def _check(body: dict) -> tuple[bool, str]:
        calls = _tool_calls(body)
        if not calls:
            return False, "no tool call"
        args = _parse_args(calls[0])
        if args is None:
            return False, "arguments were not valid JSON"
        value = args.get(arg)
        if value not in allowed:
            return False, f"{arg}={value!r} not in {allowed}"
        return True, "ok"

    return _check


def check_no_tool_call(body: dict) -> tuple[bool, str]:
    """Model must answer directly and NOT call a tool.

    Over-eager tool calling is as damaging as under-calling: an agent that
    invokes BASH when asked a question wastes a turn and can cause side effects.
    """
    if _tool_calls(body):
        return False, "called a tool when none was warranted"
    content = (_first_message(body).get("content") or "").strip()
    if not content:
        return False, "empty response"
    return True, "ok"


def check_json_schema(required_keys: tuple[str, ...]) -> Callable[[dict], tuple[bool, str]]:
    """Content parses as JSON and carries the required keys."""
    import json
    import re

    def _check(body: dict) -> tuple[bool, str]:
        content = (_first_message(body).get("content") or "").strip()
        if not content:
            return False, "empty response"
        candidate = content
        # Tolerate a fenced block, which many models emit despite instructions.
        fence = re.search(r"```(?:json)?\s*(.+?)\s*```", content, re.DOTALL)
        if fence:
            candidate = fence.group(1)
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError) as exc:
            return False, f"not valid JSON: {exc}"
        if not isinstance(parsed, dict):
            return False, f"expected object, got {type(parsed).__name__}"
        missing = [key for key in required_keys if key not in parsed]
        if missing:
            return False, f"missing keys: {missing}"
        return True, "ok"

    return _check


def check_contains(needle: str) -> Callable[[dict], tuple[bool, str]]:
    """Exact retrieval of an identifier from a large context.

    Deliberately requires an *exact* match, and distinguishes three outcomes,
    because they mean different things about a model's fitness for agent work:

    - exact          retrieval and transcription both sound
    - near miss      found the right region, corrupted the token
    - not found      retrieval failed, or the context was silently truncated

    The near-miss case is the one worth naming. Observed here: granite4.1:8b
    retrieved the needle from 16k tokens but answered "CTSARM-NEEDLE-8F31A2",
    dropping a character. Reporting that as "missing expected token" would imply
    a retrieval failure and hide the real defect. For a coder agent, transcribing
    an identifier with one character wrong produces code that does not compile,
    so this stays a failure, but it is recorded as the specific failure it is.
    """
    import difflib
    import re

    def _check(body: dict) -> tuple[bool, str]:
        content = _first_message(body).get("content") or ""
        if needle.lower() in content.lower():
            return True, "ok"
        if not content.strip():
            return False, "empty response"

        # Compare against the most token-shaped span in the answer.
        candidates = re.findall(r"[A-Za-z0-9\-]{6,}", content)
        best, ratio = "", 0.0
        for candidate in candidates:
            score = difflib.SequenceMatcher(
                None, candidate.upper(), needle.upper()
            ).ratio()
            if score > ratio:
                best, ratio = candidate, score

        if ratio >= 0.8:
            return False, (
                f"NEAR MISS: retrieved the right region but transcribed "
                f"{best!r} instead of {needle!r} ({ratio:.0%} similar). "
                "Retrieval works; exact transcription does not."
            )
        return False, f"not found: expected {needle!r}, best candidate {best!r}"

    return _check


def check_admits_unknown(body: dict) -> tuple[bool, str]:
    """Model must decline to invent a value it was not given.

    A model that fabricates here will fabricate acceptance-criteria evidence,
    which is the failure the whole evidence layer exists to prevent.

    Checked by looking for a **fabricated value**, not for an admission phrase.
    An earlier version matched a list of phrases like "not specified", which
    failed correct answers phrased any other way: "the provided configuration
    file does not contain a pool size setting" was scored as a fabrication. A
    checker that penalizes correct behavior is worse than no checker, because it
    gates good models out of the routing table on a false signal.

    Testing for absence of an invented number is both narrower and far more
    robust: the config supplies only PORT=8080, so any *other* integer in the
    answer is a value the model made up.
    """
    import re

    content = (_first_message(body).get("content") or "").strip()
    if not content:
        return False, "empty response"

    # Values genuinely present in the supplied config, which may be quoted back.
    supplied = {"8080"}
    numbers = set(re.findall(r"\b\d+\b", content))
    invented = numbers - supplied

    if invented:
        return False, f"invented value(s) {sorted(invented)}: {content[:110]!r}"
    return True, "ok"


# ---------------------------------------------------------------------------
# long-context fixture
# ---------------------------------------------------------------------------

NEEDLE_TOKEN = "CTSWARM-NEEDLE-8F31A2"


def build_haystack(target_chars: int, needle_position: float = 0.5) -> str:
    """Repo-shaped filler with one retrievable fact buried in it.

    Filler is written to look like source code rather than generic prose, because
    the retrieval behavior that matters is over code, and models attend to
    natural text differently than to structured code.
    """
    block = (
        "export function handler_{i}(req: Request, res: Response) {{\n"
        "  const cfg = loadConfig('module_{i}');\n"
        "  if (!cfg.enabled) return res.status(404).end();\n"
        "  return res.json({{ module: 'module_{i}', ok: true }});\n"
        "}}\n\n"
    )
    parts: list[str] = []
    length = 0
    index = 0
    while length < target_chars:
        chunk = block.format(i=index)
        parts.append(chunk)
        length += len(chunk)
        index += 1

    insert_at = max(0, min(len(parts) - 1, int(len(parts) * needle_position)))
    parts.insert(
        insert_at,
        f"\n// DEPLOYMENT NOTE: the production rollout token is {NEEDLE_TOKEN}.\n"
        f"// Do not change it without approval.\n\n",
    )
    return "".join(parts)


# ---------------------------------------------------------------------------
# the suite
# ---------------------------------------------------------------------------


def build_suite(*, long_context_chars: int = 60_000) -> tuple[Task, ...]:
    """The full task list. Deterministic, so runs are comparable across models."""
    haystack = build_haystack(long_context_chars)

    return (
        # -- tool_call ---------------------------------------------------
        Task(
            name="tool_basic_read",
            category="tool_call",
            messages=[
                {
                    "role": "system",
                    "content": "You are a coding agent. Use the provided tools to act. "
                    "Do not describe what you would do; call the tool.",
                },
                {
                    "role": "user",
                    "content": "I need to see what is in src/server.ts. Get it for me.",
                },
            ],
            tools=[READ_FILE_TOOL],
            check=check_single_tool_call("read_file", ("path",)),
        ),
        Task(
            name="tool_choose_among_three",
            category="tool_call",
            messages=[
                {
                    "role": "system",
                    "content": "You are a coding agent. Use the provided tools to act. "
                    "Call exactly the one tool that fits the request.",
                },
                {
                    "role": "user",
                    "content": "Find every place in the codebase where we call "
                    "`processPayment`. Search for it.",
                },
            ],
            tools=[READ_FILE_TOOL, SEARCH_TOOL, RUN_TESTS_TOOL],
            check=check_single_tool_call("grep", ("pattern",)),
        ),
        Task(
            name="tool_enum_argument",
            category="tool_call",
            messages=[
                {
                    "role": "system",
                    "content": "You are a QA agent. Use the provided tools.",
                },
                {
                    "role": "user",
                    "content": "Run only the integration tests for the checkout flow.",
                },
            ],
            tools=[READ_FILE_TOOL, SEARCH_TOOL, RUN_TESTS_TOOL],
            check=check_enum_arg("run_tests", "suite", ("integration",)),
        ),
        Task(
            name="tool_restraint",
            category="tool_call",
            messages=[
                {
                    "role": "system",
                    "content": "You are a coding agent with tools available. "
                    "Only call a tool when the request requires acting on the repository.",
                },
                {
                    "role": "user",
                    "content": "In one sentence, what is the difference between an "
                    "integration test and a unit test?",
                },
            ],
            tools=[READ_FILE_TOOL, SEARCH_TOOL, RUN_TESTS_TOOL],
            check=check_no_tool_call,
        ),
        # -- schema ------------------------------------------------------
        Task(
            name="schema_review_result",
            category="schema",
            messages=[
                {
                    "role": "system",
                    "content": "You are a code reviewer. Respond with ONLY a JSON object, "
                    "no prose and no code fence, matching exactly: "
                    '{"approved": boolean, "severity": "none"|"low"|"high", '
                    '"findings": [string], "summary": string}',
                },
                {
                    "role": "user",
                    "content": "Review this diff:\n\n"
                    "+ app.get('/users/:id', (req, res) => {\n"
                    "+   const q = `SELECT * FROM users WHERE id = ${req.params.id}`;\n"
                    "+   return res.json(db.query(q));\n"
                    "+ });",
                },
            ],
            check=check_json_schema(("approved", "severity", "findings", "summary")),
            max_tokens=2048,
        ),
        Task(
            name="schema_issue_plan",
            category="schema",
            messages=[
                {
                    "role": "system",
                    "content": "You are a sprint planner. Respond with ONLY a JSON object: "
                    '{"issues": [{"name": string, "depends_on": [string], '
                    '"acceptance_criteria": [string]}], "rationale": string}',
                },
                {
                    "role": "user",
                    "content": "Decompose: add a /healthz endpoint with tests and "
                    "an updated OpenAPI spec.",
                },
            ],
            check=check_json_schema(("issues", "rationale")),
            max_tokens=2560,
        ),
        # -- long_context -------------------------------------------------
        Task(
            name="long_context_needle",
            category="long_context",
            messages=[
                {
                    "role": "system",
                    "content": "You are a repository analyst. Answer only from the "
                    "provided source. Quote exact identifiers.",
                },
                {
                    "role": "user",
                    "content": f"{haystack}\n\n---\n\nWhat is the production rollout "
                    "token recorded in the deployment note above? Reply with the token only.",
                },
            ],
            check=check_contains(NEEDLE_TOKEN),
            max_tokens=1024,
            timeout_s=180.0,
            metadata={"context_chars": long_context_chars},
        ),
        # -- instruction ---------------------------------------------------
        Task(
            name="instruction_admit_unknown",
            category="instruction",
            messages=[
                {
                    "role": "system",
                    "content": "You are a repository analyst. Answer only from provided "
                    "context. If the context does not contain the answer, say so plainly. "
                    "Never guess.",
                },
                {
                    "role": "user",
                    "content": "Here is the config file:\n\n"
                    "PORT=8080\nLOG_LEVEL=info\nNODE_ENV=production\n\n"
                    "What database connection pool size is configured?",
                },
            ],
            check=check_admits_unknown,
            max_tokens=1024,
        ),
    )
