# ctswarm

A governance, routing, and evidence layer that turns [SWE-AF](https://github.com/Agent-Field/SWE-AF)
into an autonomous software engineering factory you can actually leave running.

ctswarm is **not** a fork of SWE-AF. SWE-AF is the factory: it turns a natural-language
goal into a PRD, an architecture, a dependency-sorted issue DAG, isolated git worktrees,
coder/reviewer/QA loops, integration tests, and a draft pull request. ctswarm is the
plant management around it: deciding *which* model and runtime does *which* job based on
capability and remaining quota, enforcing gates that models cannot talk their way past,
collecting machine-readable evidence that the work is genuinely done, and escalating to a
human only when continuing would cross a real authority boundary.

```
                Owner (Slack / local approval UI)
                          │  only genuine authority boundaries
                          ▼
      ┌───────────────────────────────────────────┐
      │  ctswarm  approvals · policy · evidence   │
      └───────────────────────────────────────────┘
                          │
                          ▼
      ┌───────────────────────────────────────────┐
      │  SWE-AF + AgentField  (22 agents)         │
      │  pinned, vendored, never forked           │
      └───────────────────────────────────────────┘
             │ runtime choice          │ model choice
             ▼                         ▼
      capacity manager          ctswarm router (OpenAI-compatible)
      claude_code │ codex        ollama · mlx · openrouter · openai
             │                         │
             ▼                         ▼
      isolated worktrees ──▶ integration branch ──▶ protected PR ──▶ main
```

## Quick start

```bash
git clone git@github.com:correlltechnologies/ctswarm.git
cd ctswarm
./bootstrap.sh
```

`bootstrap.sh` is idempotent. It detects your platform and accelerator, picks a model
candidate set that actually fits your hardware, pulls what is missing, vendors SWE-AF at a
pinned commit, writes a `.env` from whatever credentials it can find, and starts the stack.
It never overwrites an existing `.env` and it tells you exactly which capabilities are
unavailable and the one command needed to enable each.

Then:

```bash
ctswarm doctor      # what is wired up, what is missing
ctswarm bench       # qualify local models, write the routing table
ctswarm capacity    # remaining headroom per runtime, and which one gets picked
ctswarm route       # explain what the router would choose for a role, and why
ctswarm serve       # start the router
ctswarm committee   # put a judgement call to an independent multi-model vote
ctswarm usage       # model usage, cost, and the local-inference fraction
ctswarm verify      # run the self-verification probe suite
```

## Verification committees

Section 2 of the plan calls for committees rather than one model's judgment, and
section 9 requires "multiple independent models **and** deterministic scanners"
for security, because "committee agreement alone cannot establish security".

Two rules make that real rather than decorative:

**Independence is by model family, not model count.** Three Qwen models agreeing
is one opinion sampled three times: they share training data, tokenizer, and
failure modes, so they are wrong together and confidently. Quorum requires
distinct families, same-family votes collapse to one, and a split family resolves
to its *reject*.

**Scanners are authoritative; models are advisory.** A unanimous panel cannot
vote away a secret-scanner hit or a failing test. Models may add findings, never
subtract them.

Unparseable votes abstain rather than approve, ties escalate to a human, and only
bench-eligible models may sit: a reviewer that cannot emit parseable output is
not a reviewer.

## The two-tier switching model

This is the most important thing to understand, and the thing most easily gotten wrong.

SWE-AF supports three runtimes: `claude_code`, `codex`, and `open_code`. The first two are
**CLI harnesses** driven by subscription logins (`claude setup-token`, `codex login`). They
are not OpenAI-compatible HTTP endpoints, so **no HTTP proxy can route across them**.
Only `open_code` speaks to an arbitrary OpenAI-compatible base URL.

So switching happens at two levels, and conflating them produces a design that cannot work:

| Level | What switches | Mechanism | Covers |
|---|---|---|---|
| **Runtime** | Which harness runs the build | `capacity manager` picks the `runtime` field in the SWE-AF build request | Claude subscription, ChatGPT/Codex subscription, open models |
| **Model** | Which model serves a request inside `open_code` | `ctswarm router`, an OpenAI-compatible gateway | Ollama, MLX, OpenRouter, OpenAI-compatible endpoints |

The capacity manager tracks remaining subscription headroom for Claude and Codex and
remaining quota/budget for OpenRouter, and picks the runtime per build. Inside an
`open_code` build, the router picks the model per request. Both write to the same ledger,
so routing decisions improve from real build outcomes rather than guesses.

### Virtual models

SWE-AF assigns models per role across three tiers (`high` planning, `med` coding/review,
`low` mechanical). Rather than pinning concrete model IDs in SWE-AF config, ctswarm
exposes **virtual models** that the router resolves at request time:

```
ctswarm/high    ctswarm/med    ctswarm/low
```

Point SWE-AF at those and the routing policy becomes a ctswarm concern, changeable without
touching factory config. Concrete pins (`ctswarm/ollama:qwen3.5:9b`) still work when you
want to force a specific backend, which the bench and probes rely on.

## Hardware and platform support

The model catalog is platform-aware. The same repo picks different models depending on
where it is cloned.

| Platform | Backend | Notes |
|---|---|---|
| Linux + NVIDIA | Ollama (CUDA) | Catalog filtered by detected VRAM. Models that would spill past VRAM are marked `partial_offload` and scored with a throughput penalty rather than being silently chosen. |
| macOS + Apple Silicon | MLX (`mlx_lm.server`) or LM Studio, Ollama fallback | Catalog filtered by unified memory. MLX quants preferred over GGUF on Metal. |
| Any | OpenRouter / OpenAI-compatible | Overflow capacity, needs a key. |

Detection is in `ctswarm/platform_detect.py` and is the only place that knows about
hardware. Adding a backend means adding one file under `ctswarm/backends/`.

## Why models are measured, not chosen by reputation

A SWE-AF build makes **400 to 500+ agent invocations**, and nearly every agent is defined
by a tool set (`READ`/`WRITE`/`EDIT`/`BASH`/`GLOB`/`GREP`) plus a typed output schema. A
model that writes good prose but emits malformed tool calls 5% of the time will stall the
DAG, not merely degrade it. Parameter count and benchmark scores do not predict this well.

`ctswarm bench` therefore measures what actually matters for this workload:

- **Tool-call fidelity** — well-formed calls, correct argument types, no hallucinated tools
- **Schema adherence** — output parses against the typed schema SWE-AF expects
- **Long-context recall** — retrieval from a repo-sized context
- **Instruction-following under constraint** — refusing to invent, admitting incompleteness
- **Cancellation and timeout behavior** — clean abort, no wedged generations
- **Latency and throughput** — under real concurrency, not single-shot

Results land in `bench/results/` and generate `routing.toml`. A model that fails the
tool-call gate is not eligible for agent roles regardless of how good it looks otherwise.

## Repository protection

Models are a quality control mechanism, not a security boundary. Everything that actually
protects the repo is deterministic and enforced outside the model layer:

- The factory gets no push permission to `main` or release branches
- Every issue gets a unique branch and worktree; no two agents share a checkout
- Only the integration service merges into the build branch, after deterministic checks
- Branch protection requires CI, and during the pilot, human approval
- Destructive migrations, deploys, secret rotation, permission changes, and spend above
  budget always require explicit approval
- Agents may never modify protection policy, branch rules, approval thresholds, credential
  scopes, or the audit log

`ctswarm policy apply` configures branch protection from `policy/protection.toml` so the
rules are version-controlled rather than clicked into a settings page.

## Definition of Done

"The agent says it is done" carries no authority. A build is complete only when
`ctswarm evidence check` passes, which requires linked machine-readable evidence for every
must-have acceptance criterion, passing tests with no test weakened/skipped/deleted without
justification, browser evidence for UI work, security and dependency scans, and the
anti-slop gates (no placeholder copy, dead controls, fabricated metrics, or "coming soon"
behavior outside declared scope).

## Self-verification

The point of `ctswarm verify` is to test the *system*, not the generated code. It runs a
real build against `sandbox/` and asserts six probes:

1. **Anti-slop trap** — sandbox ships a test a lazy implementation breaks; weakening or
   deleting it must fail the build
2. **Provider failover** — kill the local backend mid-build; the router must fail over and
   the build must continue
3. **Approval trigger** — a high-risk action in the goal must produce exactly one approval
   card and pause
4. **Denial handling** — denying it must pause or redirect cleanly with no repeat pings
5. **Crash resume** — restart the stack mid-build; checkpoint recovery must resume
6. **Worktree isolation** — `main` never pushed to, PR exists, worktree count equals issue
   count, no cross-worktree contamination

`ctswarm verify` prints a pass/fail scoreboard asserted against git history, the GitHub
API, the router ledger, and build logs.

## Status

Pilot. SWE-AF is public beta with no tagged release, so it is vendored at a pinned commit
(`infra/versions.env`) rather than tracking `main`. The intended first version is fully
autonomous **branch-and-PR creation under strict deterministic gates**, not autonomous
production deployment. Authority expands by repository and risk class only after the system
repeatedly produces correct, reviewed, evidence-backed PRs without intervention.

See `docs/VERIFIED.md` for the separation between what has been verified against live
sources and what remains an assumption.

## License

Private. Copyright Correll Technologies LLC.
