# ctswarm handoff

**As of 2026-07-30, 02:10.** Everything is committed and pushed to
`correlltechnologies/ctswarm` (private). The stack is running in Docker.

Read this top to bottom once; it is ordered by what you most need to know.

---

## 1. Where things actually stand

**The infrastructure works. The factory does not yet produce working code.**

Those are two different claims and the distinction matters.

What is verified working, by execution and not by assumption:

- All six containers run; both SWE-AF nodes register with the AgentField control plane
- The full inference chain: SWE-AF agent → opencode → ctswarm router → Ollama / OpenRouter
- A complete 22-agent build ran for **23 minutes**, made **112 model calls at 100% success**,
  routing live across three models and two backends
- Planning ran on `deepseek-v4-pro` (OpenRouter), coding on `qwen3.5:9b` (local), with
  automatic failover to `minimax-m2.5` inside the high tier

What did **not** work:

- The build reported `4/4 issues completed` but **wrote no code**. All four worktrees are
  clean at the initial commit. No `healthz` anywhere, no branches, no commits, no PR.
- The Verifier agent failed to produce a valid result, so the build correctly ended as
  `success: False`.

**The gates behaved exactly as designed.** A system claiming four completed issues with
zero code written is precisely the failure the evidence layer exists to catch, and it
caught it: the verifier refused, and no pull request was opened. "The agent says it is
done" carried no authority, which is the whole thesis.

So the honest summary: **plumbing proven, output not yet proven.**

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

## 5. The next thing to debug

**Why the coders wrote no files.** This is the single blocker between "infrastructure
works" and "system works".

The 112 successful router calls only mean the models returned *valid completions*. It does
not mean they invoked write tools correctly. Most likely causes, in order:

1. The coder agents produced plausible prose instead of tool calls under real agent
   pressure. Bench measures tool fidelity on short tasks; a 150-turn coding loop is a much
   harsher test.
2. Worktree writes happened but the merge step failed silently, and Workspace Cleanup
   removed the evidence.
3. opencode's tool-execution path needs configuration ctswarm is not supplying.

Where to look:

```bash
docker logs ctswarm-swe-agent-1 --tail 400 2>&1 | grep -iE "coder|merge|worktree|tool"
docker exec ctswarm-swe-agent-1 ls -la /workspaces/
```

A useful experiment: rerun the same goal with `SWE_DEFAULT_RUNTIME=claude_code` in `.env`.
If Claude writes files and local models do not, the problem is model capability under
agent load, not ctswarm's plumbing. That single test cleanly separates the two hypotheses
and is worth doing before anything else.

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
three times, each requiring `sudo systemctl restart ollama`. It also passed one clean bench
run in between, so a good score does not clear it. It harms the host, not just itself.

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

1. **Fix the no-code-written problem** (section 5). Nothing else matters until this works.
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
repo        correlltechnologies/ctswarm (private, 18 commits)
sandbox     correlltechnologies/ctswarm-sandbox (private, no branch protection)
stack       6 containers up
tests       43 passing, ruff clean, sandbox 18/18
verify      5 passed, 0 failed, 1 skipped (isolation needs a live build target)
models      14 measured, 9 eligible, 5 families
last build  build-5506756dae — ran 23 min, 112 calls, 100% success,
            4/4 issues "completed", NO code written, verifier failed, no PR
```
