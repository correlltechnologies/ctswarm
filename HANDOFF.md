# ctswarm handoff

**As of 2026-07-30, 16:58 EDT.** The stack is running in Docker. The fail-closed
SWE-AF fixes are pushed on `agent/fix-local-model-coding`; the final local-model
adapter fix is validated and ready for its follow-up commit.

Read this top to bottom once; it is ordered by what you most need to know.

---

## 1. Where things actually stand

**The infrastructure works, fail-closed execution is enforced, and both Claude
and local OpenCode have produced committed, tested, independently reviewed code
under the real harness.**

What is verified working, by execution and not by assumption:

- All six containers run; both SWE-AF nodes register with the AgentField control plane
- The full inference chain: SWE-AF agent → opencode → ctswarm router → Ollama / OpenRouter
- A complete 22-agent OpenCode build ran for **23 minutes**, made **112 model
  calls at 100% transport/schema success**, routing live across three models and
  two backends
- Planning ran on `deepseek-v4-pro` (OpenRouter), coding on `qwen3.5:9b` (local), with
  automatic failover to `minimax-m2.5` inside the high tier
- A live Claude issue build completed coder → commit → reviewer in 134 seconds:
  execution `exec_20260730_162640_n8dhesy5`, branch
  `issue/0690a662-prove-claude-end-to-end`, commit `9849f03`, two files changed,
  19 sandbox tests passed, review approved
- A live local Qwen/OpenCode issue build completed coder → commit → reviewer:
  execution `exec_20260730_165029_oipllfbe`, branch
  `issue/4ca4a0aa-prove-local-native-end-to-end`, commit `8798956`, two files
  changed, 21 sandbox tests passed, review approved
- The local coder caught and repaired its own bad newline assertion. The
  reviewer initially wrote the schema definition instead of an instance, then
  corrected it on the harness's schema retry. Both failures remained fail closed.

What was wrong in the first build:

- LLM-driven git init returned a false success with an empty SHA; workspace
  setup then returned zero worktrees; the DAG silently fell back to the shared
  repository.
- Coders returned `complete: true` with no files, and reviewer failures defaulted
  to approval. Four empty issues were therefore labeled complete.
- The final verifier still prevented a PR, but the inner execution gates were
  not safe enough.

What is fixed:

- Git init is deterministic and validated against the real branch/SHA.
- Worktree setup must succeed with an exact issue/worktree match; shared-root
  fallback is gone.
- Coder claims are replaced by git-observed changes. Empty output is rejected
  before review, including after the coder commits.
- Reviewer failures are blocking, never approvals; `CoderResult.complete`
  defaults false.
- Runtime switching now supplies native Claude/Codex model names rather than
  leaking `ctswarm/*` OpenCode aliases into those CLIs.
- OpenCode streaming now stays on Ollama's native `/api/chat` path, preserving
  structured tools, explicit context sizing, separated reasoning, and reasoning
  allowance. OpenAI-style string tool arguments are decoded back to the object
  shape Ollama requires on subsequent turns.
- Claude containers mount only the 942-byte credential file, not the 653 MB /
  7,631-file host profile that made each invocation scan unrelated skills.
- Three reproducible patches under `infra/patches/` are applied by bootstrap and
  every `stack.sh` operation. The patcher refuses to overwrite a divergent
  vendor tree.

Honest boundary: **Claude and local/OpenCode issue-level coding output are
proven. Local Qwen is materially slower and needed schema self-correction, so
Claude remains the safer production default for complex work. A new full
multi-issue build has not yet been run on the patched stack.**

---

## 2. The one decision waiting on you

Branch protection needs **GitHub Pro** for private repos, so `ctswarm-sandbox` has none.

- **Make `ctswarm-sandbox` public** — protection is free on public repos, and it is a
  throwaway test service with no secrets. Recommended.
- **Upgrade to Pro** — protects private repos too.
- **Accept no backstop on the sandbox** — workable for now, since SWE-AF opens PRs rather
  than pushing to main by design, but you lose the deterministic guarantee.

I did not make it public unilaterally because that is outward-facing and hard to fully
reverse.

Your GitHub token scopes are appropriately narrow (`repo`, `read:org`, `gist`,
`admin:public_key`) with no `workflow` or `admin:org`, which is the more important half.
Verify any time with:

```bash
./.venv/bin/python -c "from ctswarm.policy.protection import token_scope_report; print(token_scope_report())"
```

---

## 3. Picking this back up

```bash
cd ~/Desktop/Projects/ctswarm

./stack.sh ps                      # is everything up
./stack.sh up                      # start / restart
./.venv/bin/ctswarm doctor         # full inventory
./.venv/bin/ctswarm capacity       # runtime headroom and spend
./.venv/bin/ctswarm status         # recent builds
```

**Never run `docker compose` by hand.** Use `./stack.sh`. Compose resolves relative paths
against the first `-f` file's directory, so without `--project-directory .` the build
context and opencode config silently point at the wrong places. `stack.sh` exists to make
that unrepresentable.

Ports: router `8090`, approvals + local approval UI `8091`, control plane **`18080`**
(moved off 8080, which your `correll-voice-crm` Supabase stack owns).

---

## 4. Running a build

```bash
./.venv/bin/ctswarm build "your goal here" \
  --repo https://github.com/correlltechnologies/ctswarm-sandbox
```

Controls, from the CLI or from Slack buttons once Slack is configured:

```bash
./.venv/bin/ctswarm pause  <build-id>
./.venv/bin/ctswarm resume <build-id>
./.venv/bin/ctswarm stop   <build-id>
./.venv/bin/ctswarm status <build-id>
```

**Pause is honoured at the next phase boundary, not instantly.** There is no way to
interrupt SWE-AF mid-reasoner. What is guaranteed is that no *new* work starts. Control
signals live in the ledger, so a pause survives a restart.

Approvals: `http://localhost:8091` works with zero setup. Slack needs
`docs/SLACK.md` (~10 min). Until then, approval cards still exist and are still actionable
locally, and expiry always resolves to **pause**, never approve.

---

## 5. The next build

Run a bounded full build with the Claude runtime and inspect the resulting
integration branch/PR. The issue-level discriminating experiments are complete:
both Claude and local OpenCode wrote, tested, committed, and passed review.

Do not switch by only changing `SWE_DEFAULT_RUNTIME` and assuming the container
tier variables are harmless. Submit through `ctswarm build`; the orchestrator
now sends runtime-native model overrides (`sonnet`/`haiku` for Claude,
auth-aware `gpt-5.5` or `gpt-5.3-codex` for Codex, and `ctswarm/*` for
OpenCode).

Useful live proof:

```text
runtime    Claude
execution  exec_20260730_162640_n8dhesy5
commit     9849f034c2210aad9f1f4c64ccec6262600d3937
tests      19 passed; reviewer approved

runtime    OpenCode → ctswarm/med → Ollama qwen3.5:9b
execution  exec_20260730_165029_oipllfbe
commit     87989566018e3d2187c5cc4d1c0fc6950a308bc6
tests      21 passed; reviewer approved
```

---

## 6. Models

Routing table: 14 measured, 9 eligible, 5 independent families. Regenerate any time with
`ctswarm bench` (add `--backend openrouter` for hosted; results now merge rather than
overwrite).

| Tier | Primary | Fallbacks |
|---|---|---|
| high (planning) | `deepseek/deepseek-v4-pro` | `qwen3.6`, `minimax-m2.5` |
| med (coding) | `qwen3.5:9b` (local, free) | `gpt-oss-120b`, `deepseek-v4-flash` |
| low (mechanical) | `qwen3.5:9b` | `deepseek-v4-flash`, `granite4.1:8b` |

**`ornith:9b` is quarantined and must stay that way.** It wedged the entire Ollama queue
repeatedly. Earlier events required `sudo systemctl restart ollama`; the latest
was cleared with `ollama stop ornith:9b`. It also passed one clean bench run in
between, so a good score does not clear it. It harms the host, not just itself.

`qwen2.5-coder:7b` fails the tool-call gate at 25%, answering in prose. The nominal "coder"
model is the least fit for agent work in the set.

If Ollama ever seems to hang everything at once, that is the wedge signature:

```bash
curl -s localhost:8090/health | python3 -m json.tool   # shows degraded + wedged_models
sudo systemctl restart ollama
```

---

## 7. Cost

Roughly **$0.20 to $0.50** of OpenRouter credit was used across benching and the build, out
of your $5.

The number that matters: a trivial Claude call measured **$0.345**, almost entirely fixed
system-prompt overhead rather than prompt size. Multiply by 400+ invocations per build.
That is why routing is local-first and why subscriptions are treated as scarce.

```bash
./.venv/bin/ctswarm usage       # calls, tokens, spend, local share
```

Budget cap is `CTSWARM_BUDGET_USD_PER_BUILD=2.00` in `.env`; exceeding it raises an approval
rather than silently continuing. Worth also setting a spend limit on the OpenRouter key
itself, since your key is currently uncapped and a provider-side limit cannot be bypassed
by a bug in ctswarm.

---

## 8. Still to build

In the order I would do them:

1. **Run a bounded full Claude build** and verify integration/PR behavior. The
   issue-level coding path is proven; the multi-issue path still needs this
   post-fix proof.
2. **Evidence bundle** mapping test results to PRD acceptance criteria. Scanners and
   committees exist; the artifact tying them to each must-have does not.
3. **Apply branch protection** once section 2 is decided (`ctswarm.policy.protection` is
   written and tested, just not applied).
4. **Scheduler** for genuine 24/7 operation: queue, concurrency limits, restart policies,
   log rotation.
5. **Browser/Playwright evidence** for UI work. Not needed for the sandbox, but the plan
   requires it before any UI project.

---

## 9. Things that will bite you

- **`docker compose` by hand** breaks paths. Use `./stack.sh`.
- **Editing code does not update containers.** Run `./stack.sh build <service>` then
  `./stack.sh up`. `up` alone reuses the old image, which cost me an hour.
- **`.env` is gitignored via `.env*`**, deliberately broad. A copied template once landed as
  `.env copy.example` and sat one `git add -A` away from committing a live token.
- **Token budgets must clear reasoning overhead.** Thinking models spend their allowance
  before emitting content: `qwen3.5:4b` used 181 reasoning tokens for a 2-token answer. The
  Ollama backend compensates automatically; remember it if you add a backend.
- **OpenCode always requests streaming.** An Ollama streaming implementation
  must preserve native `/api/chat` semantics. Falling back to `/v1/chat/completions`
  silently turns structured tool calls into prose and bypasses context sizing.
- **OpenAI and native Ollama tool histories differ.** OpenAI sends function
  arguments as a JSON string; native Ollama requires an object on the next turn.
- **Ollama silently truncates to a 4096 context** unless `num_ctx` is set on the native
  endpoint. `/v1` ignores the field. This is handled, but it is invisible when wrong: the
  model answers confidently from a truncated prompt.
- **A skipped verification probe is not a pass.** `ctswarm verify` exits non-zero on skips
  unless you pass `--allow-skips`.

---

## 10. Documentation map

| File | What it holds |
|---|---|
| `README.md` | Architecture, the two-tier switching model, committees |
| `docs/VERIFIED.md` | **Read this one.** What is verified vs assumed, with methods and known-bad findings |
| `docs/OPENROUTER.md` | OpenRouter setup and cost reasoning |
| `docs/SLACK.md` | Slack app setup, ~10 minutes |
| `sandbox/README.md` | The test project and why the contract test is the trap |
| `.claude-memory/` | Project memory notes for future sessions |

`docs/VERIFIED.md` is the one that will save you the most time. It records exactly which
claims were checked against live sources, which were executed here, and which remain
assumptions, with the blast radius of each.

---

## Current state at a glance

```
repo        correlltechnologies/ctswarm (private; current fix commit not pushed)
sandbox     correlltechnologies/ctswarm-sandbox (private, no branch protection)
stack       6 containers up
tests       48 root tests, ruff clean; 71 focused SWE-AF tests; sandbox 19/19 in live proof
verify      5 passed, 0 failed, 1 skipped (isolation needs a live build target)
models      14 measured, 9 eligible, 5 families
last build  build-5506756dae — ran 23 min, 112 calls, 100% success,
            4/4 issues "completed", NO code written, verifier failed, no PR
live proof  exec_20260730_162640_n8dhesy5 — Claude coder + reviewer,
            1 commit, 2 files, 19 tests, approved, success=true
```
