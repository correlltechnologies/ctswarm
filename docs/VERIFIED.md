# Verified facts vs assumptions

The implementation plan requires that agents research current documentation
before framework-specific decisions, and that unsupported assumptions are treated
as defects. This document holds ctswarm to the same standard.

Everything below is either **verified** (checked against a live source or executed
on this machine, with the method recorded) or **assumed** (plausible but not yet
confirmed). Assumptions are not load-bearing without a note saying what breaks if
they are wrong.

Research date: **2026-07-29**. Provider behavior changes; re-check before relying
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
| **`ornith:9b` wedges the entire inference queue** | Three separate events on 2026-07-29: a tool-calling request, a trivial "Say OK", and once mid-bench. Each entered a runaway generation that never terminated, pinned the GPU at ~92%, and required `sudo systemctl restart ollama`. `ollama stop` does not clear it. | **Quarantined in the catalog**, excluded from routing regardless of score. Note it passed one complete bench run cleanly *in between* wedges, so a single green result does not clear it. The third wedge also blocked `granite4.1:3b` from being measured at all. This is the one failure class that damages the host rather than just the model, which is why quarantine overrides measurement. |
| `ollama 0.31.1` cannot pull `laguna-xs-2.1` | Pull fails with a download prompt rather than a version error | The best local high-tier candidate is unavailable until Ollama is upgraded. `qwen3.6` covers the tier meanwhile. |
| Ollama loaded `ornith:9b` with a 4096 context | `/api/ps` reported `context_length: 4096` despite the model advertising 262144 | Advertised context is not effective context. The bench measures real retrieval rather than trusting metadata. |

## Assumptions not yet verified

| Assumption | Why it is not yet verified | What breaks if wrong |
|---|---|---|
| MLX model references in `catalog.py` resolve | No Apple Silicon machine in this session | `ctswarm doctor` on the Mac reports them as unresolvable. They are marked `verified_ref=False` and are **not** trusted by the router. Low blast radius by design. |
| SWE-AF's `open_code` runtime drives the ctswarm router correctly end to end | Requires a full stack run, blocked on credentials and a healthy backend | The core integration. This is the single biggest untested assumption in the repo. |
| SWE-AF honors `SWE_MODEL_HIGH/MED/LOW` as documented | Read from `.env.example`, not executed | Falls back to `models.default`; per-tier routing would silently collapse to one model |
| The docker overlay composes cleanly with upstream's compose file | Not yet run | Service names or volume mounts may need adjustment |
| OpenRouter quota and pricing endpoints have the assumed shape | No API key available | Cost estimates read zero and the budget guard treats spend as unknown rather than free |
| Claude subscription quota is observable | No token available | The capacity manager cannot measure Claude headroom and must fall back to failure-driven switching |

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
