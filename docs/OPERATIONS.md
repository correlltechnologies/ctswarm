# Operating ctswarm

ctswarm runs as a seven-service Docker Compose stack. The scheduler is the only
supported build entry point: it owns the durable queue, runtime selection,
concurrency limit, restart recovery, and build controls.

## Service map

| Service | Host endpoint | Purpose |
|---|---|---|
| Router | `http://127.0.0.1:8090` | OpenAI-compatible model routing to local and hosted backends |
| Approvals | `http://127.0.0.1:8091` | Local approval UI and optional Slack bridge |
| Scheduler | `http://127.0.0.1:8092` | Durable queue and build-control API |
| Control plane | `http://127.0.0.1:18080` | AgentField execution API |
| Build database | internal only | SWE-AF build state |
| `swe-agent` | internal only | Planning and orchestration agents |
| `swe-fast` | internal only | Issue coding and review agents |

All published ports bind to loopback. Ollama also remains host-local; containers
reach it through Docker's host gateway.

## Start, stop, and inspect

Always use `stack.sh`. It applies the pinned SWE-AF patches and supplies the
Compose project directory that the overlay requires.

```bash
./stack.sh up
./stack.sh ps
./stack.sh logs ctswarm-scheduler
./stack.sh down
```

`./stack.sh up` fails nonzero unless the router, control plane, and scheduler
become ready. Every service uses `restart: unless-stopped`.

After changing one service:

```bash
./stack.sh build ctswarm-scheduler
./stack.sh recreate ctswarm-scheduler
```

`recreate` replaces only the named service and does not restart its
dependencies. `./stack.sh restart` restarts the two SWE-AF agent services while
the scheduler keeps monitoring.

`./stack.sh nuke` deletes the Compose volumes, including the queue and audit
ledger. It is intentionally the only normal command that destroys build state.

## Submit and control builds

```bash
./.venv/bin/ctswarm build "add the requested feature" \
  --repo https://github.com/OWNER/REPOSITORY

./.venv/bin/ctswarm status
./.venv/bin/ctswarm status <build-id>
./.venv/bin/ctswarm pause <build-id>
./.venv/bin/ctswarm resume <build-id>
./.venv/bin/ctswarm stop <build-id>
```

The CLI refuses to submit directly to AgentField. This prevents a caller from
bypassing the shared-resource limit. Use `--no-watch` to enqueue and detach.

Pause and stop take effect at a phase boundary because AgentField cannot
interrupt a reasoner that is already running. A queued build can be stopped
before it is dispatched.

## Durability and capacity

Queue entries, control signals, execution IDs, and terminal results live in the
shared SQLite ledger. When the scheduler restarts it reconstructs an in-flight
build from the ledger and resumes polling the same AgentField execution; it does
not submit a duplicate.

The default limit is one active build:

```dotenv
CTSWARM_MAX_CONCURRENT_BUILDS=1
CTSWARM_SCHEDULER_POLL_SECONDS=10
```

Raise the limit only when the AgentField database and model backends have
equivalent additional capacity. The scheduler health endpoint exposes the
current queue:

```bash
curl -fsS http://127.0.0.1:8092/health
# {"ok":true,"queued":0,"active":1,"max_concurrent":1}
```

## Logs and disk use

Docker's `json-file` logs rotate by size and count for every service:

```dotenv
CTSWARM_LOG_MAX_SIZE=10m
CTSWARM_LOG_MAX_FILES=5
```

Inspect only the relevant service when diagnosing a live build:

```bash
./stack.sh logs ctswarm-scheduler
./stack.sh logs swe-agent swe-fast
```

The durable ledger is in the `ctswarm-state` volume. Back up that volume before
host maintenance if preserving queued and historical build records matters.

## Failure handling

- If `./stack.sh up` fails readiness, run `./stack.sh ps` and the logs for the
  unhealthy service. Do not submit by calling the control plane directly.
- If the scheduler is restarted during a build, wait for `/health`, then run
  `ctswarm status <build-id>` and confirm the execution ID is unchanged.
- If all local inference requests stall while router health remains HTTP 200,
  inspect `curl -fsS http://127.0.0.1:8090/health`. A listed wedged model must be
  stopped in Ollama before more work is queued.
- A failed, blocked, stopped, or scanner-unavailable result is not a success.
  Work remains on its branch for diagnosis.

## Repository governance boundary

The factory opens branches and draft pull requests; it does not deploy
production. Branch protection remains the deterministic backstop for a target
repository when the GitHub plan supports it.

The private `ctswarm-sandbox` repository cannot enable the required branch
protection rules on its current GitHub plan. For that disposable test repository,
the owner has explicitly accepted direct-main administration as a governance
exception. This does not extend to production repositories.

Slack is optional. The local approval UI on port 8091 provides the same
decisions without external configuration; see `docs/SLACK.md` to add Slack.
Browser evidence is required only for builds that change a user interface.
