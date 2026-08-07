# ctswarm handoff

**As of 2026-08-06.** The seven-service stack is running, Mission Control is
available at `http://127.0.0.1:8092/dashboard`, and the scheduler now preserves
an immutable model/harness policy at enqueue time. A final Pokémon-game closure
build is exercising Claude planning/review with local OpenCode/Ollama
implementation; its terminal outcome is recorded in the 2026-08-06 section of
`docs/VERIFIED.md`.

This document is the operational handoff. `docs/VERIFIED.md` contains the
evidence history and `docs/OPERATIONS.md` contains the runbook.

---

## 1. Current state

The core branch-and-PR factory works end to end:

- Seven Docker services are up: control plane, build database, router,
  approvals, scheduler, `swe-agent`, and `swe-fast`.
- Every service uses `restart: unless-stopped` and rotated Docker logs
  (`10m`, five files by default).
- The scheduler owns all submissions. Its SQLite-backed queue, controls,
  execution IDs, and terminal results survive container restarts.
- Routing policy is captured when a request is enqueued, so changing global
  defaults cannot race with or rewrite a queued build's launch assignments.
- Stop cancels the complete AgentField workflow tree when a workflow/run ID is
  available and falls back to per-execution cancellation for older records.
- Live build records retain a truthful inactivity timer rather than resetting
  the displayed stalled duration during scheduler polling.
- Mission Control generates a plain-language operator narrative for every build:
  delivery stage, checkout impact, recent milestones, and stop/block reason.
- Build details display the immutable planning, implementation, review, and
  maintenance harness/model assignments used at launch.
- Default concurrency is one build, matching the shared AgentField database and
  local GPU. A queued build cannot bypass that limit.
- Claude and local OpenCode both produced committed, tested, independently
  reviewed code under the fail-closed SWE-AF harness.
- A full five-issue Claude build completed planning, isolated worktrees,
  dependency-ordered parallel execution, deterministic merges, integration
  testing, final verification, and draft-PR creation.
- Root CI failures that predated the local-model work are fixed. Pull-request
  CI and the post-merge `main` run both pass Python, sandbox, and anti-slop jobs.

There is no required engineering gap left for the current autonomous
branch-and-draft-PR scope. Production deployment remains deliberately outside
that scope.

## 2. Full post-fix proof build

Target: private disposable repository
`correlltechnologies/ctswarm-sandbox`.

```text
ctswarm build       build-910573b23f
AgentField run      run_20260730_193009_cjmupetz
root execution      exec_20260730_193009_fmvuzw2d
runtime             claude_code
elapsed             2,627 seconds
result              success=true; scheduler state=complete
pull request        https://github.com/correlltechnologies/ctswarm-sandbox/pull/1
```

The planner produced five real issues:

1. route definition;
2. README documentation;
3. handler implementation;
4. OpenAPI documentation;
5. focused endpoint tests.

The DAG ran three dependency levels. Independent issues used separate worktrees
and ran in parallel; dependent issues started from the merged prior-level
commit. All issue coders committed changes and all five code reviews approved.
Merges used the deterministic no-conflict path.

The first intermediate integration run intentionally found the handler and
OpenAPI work still absent. After those dependencies merged, the next integration
run passed 71/71 tests. Final verification passed all 13 acceptance criteria,
including typecheck, the complete test suite, documentation, schema constraints,
and PR existence.

Repository finalization briefly swept five generated integration-test artifacts
into the PR, adding 969 lines and changing it to ready-for-review. Those
temporary artifacts were removed in follow-up commit `635b975`, leaving the
focused six-test endpoint suite. A fresh checkout then passed:

- TypeScript typecheck;
- 24/24 committed tests;
- coverage thresholds (90.84% statements);
- secret scan;
- anti-slop scan;
- production dependency audit.

The PR is draft, targets `main`, is limited to six files and 141 additions, and
remains unmerged as the disposable proof artifact.

## 3. Local-model path

The local path is not a configuration-only claim.

Issue-level proof:

```text
execution  exec_20260730_165029_oipllfbe
runtime    OpenCode -> ctswarm/med -> Ollama qwen3.5:9b
commit     87989566018e3d2187c5cc4d1c0fc6950a308bc6
result     21 tests passed; independent reviewer approved
```

The current running stack was also rechecked after the production-readiness
changes. Routing selected local `qwen3.5:9b`; a streaming completion emitted the
required structured `record_value({"value": 7})` tool call with
`finish_reason=tool_calls`; router health reported no wedged models.

The critical adapter fixes are:

- OpenCode streaming stays on Ollama's native `/api/chat` path.
- Native context sizing and reasoning controls are preserved.
- OpenAI stringified tool arguments are translated back to native Ollama
  objects on subsequent turns.
- Runtime-specific model names prevent OpenCode aliases from leaking into
  Claude or Codex.

Local Qwen remains slower and less schema-reliable than Claude, so Claude is the
safer default for complex builds. Local Qwen is nevertheless a working coder,
reviewer, and structured-tool runtime.

`ornith:9b` remains quarantined. It has repeatedly wedged the shared Ollama
queue, even after one clean benchmark run.

## 4. Operating the stack

```bash
cd ~/Desktop/Projects/ctswarm

./stack.sh up
./stack.sh ps
./.venv/bin/ctswarm doctor
./.venv/bin/ctswarm capacity
./.venv/bin/ctswarm status
```

Submit and control a build:

```bash
./.venv/bin/ctswarm build "your goal" \
  --repo https://github.com/OWNER/REPOSITORY

./.venv/bin/ctswarm status <build-id>
./.venv/bin/ctswarm pause <build-id>
./.venv/bin/ctswarm resume <build-id>
./.venv/bin/ctswarm stop <build-id>
```

The CLI refuses to bypass the scheduler. `--no-watch` enqueues and detaches.
Pause and stop take effect at a phase boundary; queued work can be stopped
before dispatch.

Always use `stack.sh`, never a hand-written Compose command. It supplies the
required project directory and applies the pinned SWE-AF patches before build or
startup.

For one changed service:

```bash
./stack.sh build ctswarm-scheduler
./stack.sh recreate ctswarm-scheduler
```

Endpoints:

| Port | Service |
|---|---|
| `8090` | Model router |
| `8091` | Approvals and local approval UI |
| `8092` | Durable scheduler |
| `18080` | AgentField control plane |

All published ports bind to loopback.

## 5. Scheduler recovery proof

During the full build, the scheduler container was recreated twice. After each
restart it recovered:

```text
build       build-910573b23f
execution   exec_20260730_193009_fmvuzw2d
state       executing
```

It did not submit a duplicate execution.

A second request (`build-f1f27e712a`) remained queued while the real build held
the only slot. Stopping it produced terminal state `stopped` with no execution
ID, proving it never reached AgentField. The regression suite covers both
restart recovery and stopped-while-queued behavior.

## 6. Validation snapshot

Current root validation:

```text
ruff                         clean
pytest                       61 passed
platform detection           RTX 5070 / CUDA / Ollama detected
anti-slop self-check         0 blockers
sandbox typecheck            passed
sandbox tests                18 passed
sandbox coverage             90.15% statements
stack                        7 services healthy/running
GitHub CI                    3/3 jobs passed on merged main
```

The sandbox draft PR's fresh-checkout validation is recorded in section 2.

## 7. Governance boundary

`ctswarm-sandbox` is private and its current GitHub plan does not support the
required branch-protection rules. The owner explicitly authorized direct-main
administration for this project and accepted the sandbox exception. The factory
still opens feature branches and draft PRs; it does not deploy.

Do not generalize that exception to production repositories. Apply
`ctswarm policy apply` where the repository plan supports protection, and keep
agents unable to edit workflows, secrets, rules, or audit history.

Slack is optional. The local approval UI on port 8091 implements the same
decision path with no external setup. UI-changing builds additionally require
browser evidence; the API-only sandbox proof does not.

## 8. Known operational cautions

- Editing source does not update a running container. Build and recreate the
  affected service.
- `.env` and copied credential files must remain untracked.
- A router HTTP 200 is not enough; inspect `wedged_models` if all local requests
  stall.
- Do not clear `ornith:9b` from quarantine based on one green run.
- A skipped scanner or committee is not a pass.
- The scheduler ledger is in the `ctswarm-state` volume. `./stack.sh nuke`
  destroys it.
- Dev-only dependency advisories in the sandbox are reported but do not block;
  production dependency advisories do.

## 9. Documentation map

| File | Purpose |
|---|---|
| `README.md` | Architecture and design principles |
| `docs/OPERATIONS.md` | Always-on runbook, recovery, controls, logs |
| `docs/VERIFIED.md` | Verified facts, live evidence, assumptions |
| `docs/REMOTE_EXECUTION.md` | VPS/local/hybrid execution and routing requirement |
| `docs/SLACK.md` | Optional Slack approval setup |
| `docs/OPENROUTER.md` | Hosted overflow setup and cost reasoning |
| `sandbox/README.md` | Local verification target and contract trap |

Future expansion items—not blockers for the current scope—are branch protection
on plans that support it, Slack delivery if desired, and browser evidence when a
target includes a user interface.
