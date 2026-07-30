# Verified facts vs assumptions

The implementation plan requires that agents research current documentation
before framework-specific decisions, and that unsupported assumptions are treated
as defects. This document holds ctswarm to the same standard.

Everything below is either **verified** (checked against a live source or executed
on this machine, with the method recorded) or **assumed** (plausible but not yet
confirmed). Assumptions are not load-bearing without a note saying what breaks if
they are wrong.

Research date: **2026-07-30**. Provider behavior changes; re-check before relying
on any of this in a new environment.

---

## Verified against live sources

| Fact | Method | Result |
|---|---|---|
| SWE-AF repository is real and active | `gh api repos/Agent-Field/SWE-AF` | Apache-2.0, Go+Python, 956 stars, last push 2026-07-24 |
| SWE-AF has **no tagged release** | `gh api .../releases` | `0` releases. This is why `infra/versions.env` pins a commit. |
| SWE-AF supports 3 runtimes | Read `swe_af/runtime/providers.py` at pinned commit | `claude_code`, `open_code`, `codex` |
| `claude_code` runtime accepts a **subscription** token | Read `.env.example` upstream | `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, uses Pro/Max credits rather than API billing |
| `codex` runtime accepts a **ChatGPT** login | Read `.env.example` + `docker-compose.yml` upstream | `SWE_CODEX_AUTH_MODE=chatgpt`, `~/.codex` mounted into both agent containers |
| Model tiers are `high` / `med` / `low` | Read upstream `.env.example` | 17 roles mapped across 3 tiers; mirrored in `ctswarm/catalog.py::ROLE_TIERS` |
| A build makes 400 to 500+ agent invocations | `docs/ARCHITECTURE.md` upstream | Drives the whole routing-economics argument |
| SWE-AF has a built-in HITL path | Read `swe_af/hitl/ask_user.py` | Posts approval requests, waits on `/api/v1/webhooks/approval-response`. ctswarm bridges this rather than reimplementing it. |
| opencode supports custom OpenAI-compatible providers | opencode.ai/docs/providers | `npm: "@ai-sdk/openai-compatible"` + `options.baseURL`. This is what makes the router reachable as `ctswarm/*`. |
| agentfield SDK is published and current | PyPI JSON API | Latest `0.1.117`; SWE-AF requires `>=0.1.67` for the opencode v1.4+ fix (upstream issue #45) |
| Ollama exposes an OpenAI-compatible API | `curl localhost:11434/v1/models` | Confirmed on this host |

## Verified by execution on this machine

| Fact | Method | Result |
|---|---|---|
| Host profile detection | `python -m ctswarm.platform_detect` | RTX 5070, 11.9GB usable VRAM, 46.7GB RAM, backend `ollama` |
| Backend discovery and warm-model detection | live call against `:11434` | 7 models listed, `/api/ps` residency read correctly |
| Router health, model listing, routing explanation | `curl :8090/...` | All correct; `/routing/explain` returns scored candidates and exclusion reasons |
| **A wedged model runner blocks the whole queue** | observed live | `ornith:9b` entered a runaway generation, never terminated, pinned the GPU at 94%. `GET /v1/models` returned 200 the entire time. Every other model's requests queued behind it forever. |
| Wedge detection works | `OllamaBackend.wedged_models()` | Correctly identified `ornith:9b` via `expires_at` in the past while still loaded |
| Generation probe distinguishes up from working | `probe_generation()` | Returned `False` while `health()` returned `True` |
| Approval flow end to end | live HTTP against `:8091` | Routine action not escalated; high-risk escalated; duplicate deduped; deny recorded; status resolved |
| Unsigned Slack callbacks rejected | `curl` without signature | HTTP 503, refuses to process |
| Agent-supplied text is HTML-escaped in the approval UI | injected `<script>` into a card | Rendered escaped, `0` unescaped occurrences |
| Sandbox suite passes on clean checkout | `npm test` | 18/18 |
| **The contract trap fires** | added a route without updating `openapi.yaml` | Contract test failed with an actionable message; restored cleanly to 18/18 |
| Anti-slop gates fire | `scan_text` against crafted samples | All 8 checks fire; **zero false positives** on the clean sandbox |
| Approval rule invariants | `pytest tests/` | 24/24 passing |
| Empty coder claims fail closed | Live OpenCode issue build plus regression tests | Two iterations produced no git changes; outcome was `failed_unrecoverable`, reviewer was never called |
| Claude can write under the real harness | Live `swe-fast.implement_issue` with `runtime=claude_code`, native `sonnet` model | Wrote two files, passed 19 sandbox tests, committed `9849f03`, independent reviewer approved |
| Native Ollama tools survive OpenCode streaming | Direct three-turn OpenCode run through `ctswarm/med` | Qwen emitted structured `write`, then structured `read`, then a final answer; exact file bytes verified |
| Local OpenCode can complete the fail-closed issue workflow | Live `swe-fast.implement_issue`, execution `exec_20260730_165029_oipllfbe` | Qwen wrote two files, self-corrected a test, committed `8798956`, passed 21 tests, and the independent local reviewer approved |
| Runtime model isolation | Live failed/successful A/B plus root tests | `ctswarm/med` caused the Claude CLI to hang; explicit native model overrides fixed it. All runtimes now receive model ids valid for their harness. |
| Narrow Claude credential mount | Docker mount inspection | Container sees one 942-byte credential instead of the 653 MB / 7,631-file host Claude profile |
| Patched SWE-AF regression suite | Ephemeral production image with local source mounted | 71 focused tests passed (git fast path, coding loop, issue build) |
| ctswarm suite after fixes | `pytest -q`; `ruff check ctswarm tests` | 52/52 passing; lint clean |

## Bench results (2026-07-29, RTX 5070 / 11.9GB VRAM)

Produced by `ctswarm bench`. Eligibility requires tool-call >= 90%, schema >= 85%,
and clean cancellation, because those are the behaviors that stall a DAG rather
than merely degrade output.

| Model | Tools | Schema | Long ctx | Instr | tok/s | Verdict |
|---|---|---|---|---|---|---|
| `qwen3.5:9b` | 100% | 100% | 100% | 100% | 94 | **ELIGIBLE** |
| `granite4.1:8b` | 100% | 100% | near-miss | 100% | 96 | **ELIGIBLE** |
| `qwen3.5:4b` | 100% | 50% | 100% | 100% | 141 | no: schema |
| `qwen3.6` | 75% | 50% | 100% | 100% | 19 | no: too slow to be reliable |
| `qwen2.5-coder:7b` | **25%** | 100% | 100% | 100% | 2 | no: answers in prose instead of calling tools |
| `ornith:9b` | — | — | — | — | — | **QUARANTINED** |
| `granite4.1:3b` | — | — | — | — | — | unmeasured (blocked by ornith's wedge) |

Notes that matter more than the numbers:

- **`qwen2.5-coder:7b` is the cautionary result.** It is nominally the "coder"
  model and it is the least suitable for agent work in the set, failing 3 of 4
  tool tasks by describing the call in prose instead of emitting one. Reputation
  and name are not predictive of agent fitness; this is the entire argument for
  measuring.
- **`granite4.1:8b`'s long-context "failure" is a near miss.** It retrieved the
  needle from a 16k-token context but transcribed `CTSARM-NEEDLE-8F31A2`,
  dropping a character. Retrieval works; exact transcription does not. Still
  disqualifying for a coder role, since an agent that corrupts an identifier
  emits code that does not compile, but it is not a retrieval failure.
- **The high tier is empty.** No installed model qualifies for planning roles.
  The router degrades to the med tier and labels the decision
  `DEGRADED from high tier` rather than returning nothing. Resolving this
  properly means either upgrading Ollama to pull `laguna-xs-2.1`, or routing
  planning roles to the `claude_code` / `codex` runtime, which is what the
  original plan recommends for architecture work anyway.

## Known-bad findings

| Finding | Evidence | Consequence |
|---|---|---|
| **`ornith:9b` wedges the entire inference queue** | Repeated live events: runaway generation, stale loaded process, and all other models queued behind it. Earlier events required `sudo systemctl restart ollama`; on 2026-07-30 `ollama stop ornith:9b` cleared it. | **Quarantined in the catalog**, excluded from routing regardless of score. It passed one complete bench run cleanly between wedges, so a green result does not clear it. This failure class damages the shared host queue rather than only its own request. |
| `ollama 0.31.1` cannot pull `laguna-xs-2.1` | Pull fails with a download prompt rather than a version error | The best local high-tier candidate is unavailable until Ollama is upgraded. `qwen3.6` covers the tier meanwhile. |
| Ollama loaded `ornith:9b` with a 4096 context | `/api/ps` reported `context_length: 4096` despite the model advertising 262144 | Advertised context is not effective context. The bench measures real retrieval rather than trusting metadata. |

## First full build (2026-07-30)

`build-5506756dae`, goal: add `/healthz` with tests and an OpenAPI update.

| Fact | Result |
|---|---|
| Stack runs, both SWE-AF nodes register | verified |
| Full chain: agent -> opencode -> router -> ollama/openrouter | verified |
| Build ran to completion | 23 min, 112 model calls, **100% success** |
| Live multi-backend routing | high -> deepseek-v4-pro (OpenRouter), med/low -> qwen3.5:9b (local), failover to minimax-m2.5 inside the high tier |
| **Code actually written** | **NO.** All worktrees clean at the initial commit; no `healthz`, no branches, no commits |
| Build outcome | `success: False` — 4/4 issues reported complete, Verifier failed, no PR opened |

The final verifier prevented a false PR, but deeper inspection found unsafe inner
gates: git init returned a false success with an empty SHA, workspace setup
returned no worktrees and silently fell back to the shared root, empty coder
claims were accepted, and reviewer failures defaulted to approval.

The local patch set now makes each boundary fail closed:

- deterministic, validated git init;
- exact worktree coverage with no shared-root fallback;
- git-derived change evidence, including committed changes;
- empty-output rejection before review;
- blocking reviewer-error fallbacks;
- `CoderResult.complete=False` by default.

The patches are stored under `infra/patches/` and applied idempotently by
`bootstrap.sh` and `stack.sh`. The patcher compares the vendor tree to a complete
expected worktree and refuses to overwrite divergent local changes.

## Runtime A/B after the first build (2026-07-30)

| Runtime / execution | Result |
|---|---|
| OpenCode `ctswarm/med`, `exec_20260730_160932_9cpa1bep` | Two coder attempts, zero git changes; correctly rejected before review and ended `failed_unrecoverable` |
| Claude with leaked `ctswarm/med` alias | CLI hung. Root cause: global container tier variables applied to every runtime. |
| Claude `sonnet` with full host profile mounted | Correct model, but startup scanned 653 MB / 7,631 host-profile files. |
| Claude `sonnet`, credential-only mount, before committed-change fix | Wrote, tested, and committed two files; exposed that issue-level builds did not propagate their base branch to the change gate. |
| Claude `sonnet`, final patched image, `exec_20260730_162640_n8dhesy5` | **SUCCESS** in 134 s: branch `issue/0690a662-prove-claude-end-to-end`, commit `9849f03`, two files, 19 tests passed, reviewer approved |
| OpenCode after native streaming + tool-history translation, `exec_20260730_165029_oipllfbe` | **SUCCESS** in 434 s: branch `issue/4ca4a0aa-prove-local-native-end-to-end`, commit `8798956`, two files, 21 tests passed, reviewer approved |

This separates the concerns cleanly. The original local failure was an adapter
failure, not proof that Qwen could not code: OpenCode's streaming requests
inherited the OpenAI-compatible Ollama path and bypassed native tool/context/
reasoning handling. After routing streaming through native `/api/chat` and
translating OpenAI string arguments back to native objects between turns, the
same local model completed the real coder→commit→reviewer path. It remains
slower and less schema-reliable than Claude, so benchmark eligibility alone is
still not a reason to make it the default for complex work.

## Assumptions not yet verified

| Assumption | Why it is not yet verified | What breaks if wrong |
|---|---|---|
| MLX model references in `catalog.py` resolve | No Apple Silicon machine in this session | `ctswarm doctor` on the Mac reports them as unresolvable. They are marked `verified_ref=False` and are **not** trusted by the router. Low blast radius by design. |
| A full multi-issue Claude build merges and opens a valid PR | Only the issue-level coder→reviewer path has been rerun after the fixes | Integration merge, final verification, and PR creation remain unproven on the patched image |
| OpenRouter provider-side quota is observable accurately | Live calls and routing work, but the account's authoritative quota semantics were not fully exercised | Capacity can still fall back to failure-driven switching |
| Claude subscription quota is observable before exhaustion | Authentication and calls work, but the CLI does not expose a reliable remaining-credit endpoint here | Capacity must use configured rolling budgets plus failure-driven switching |

## Things deliberately NOT assumed

- **Model quality from reputation or parameter count.** Nothing is routed to on the
  basis of a benchmark score published elsewhere. `ctswarm bench` measures
  tool-call fidelity, schema adherence, long-context retrieval, and cancellation
  on this hardware, and a model absent from the routing table is treated as
  unmeasured rather than acceptable.
- **That HTTP 200 means success.** `inspect_completion` validates the body:
  empty content, truncation, and malformed tool-call arguments are all failures
  even with a 200 status.
- **That a reachable backend is a working backend.** See the wedged-runner
  finding above.
- **Pricing.** Fetched from the provider's own endpoint, never hardcoded.
