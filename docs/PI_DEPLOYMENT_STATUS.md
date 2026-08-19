# Raspberry Pi deployment: where this stands

**As of 2026-08-18.** A resume point, not a runbook. `docs/RASPBERRY_PI.md` is
the runbook; this records what is done, what is not, and what to do next.

The goal driving this work: run the whole factory unattended on a Raspberry Pi,
using only Claude and Codex subscriptions with no local models, reachable from a
phone over Tailscale, running until a swarm finishes and opens its PR.

## Where to pick up

**The stack is installed and running on the board.** All six services are up and
healthy. What is missing is credentials, and only the operator can supply them,
because all three need either a browser or a token that belongs to their
accounts:

```bash
ssh -i ~/.ssh/ctswarm_pi quinn@100.118.93.5

# 1. Claude. Prints a token; paste it into ~/ctswarm/.env as
#    CLAUDE_CODE_OAUTH_TOKEN=...
claude setup-token

# 2. Codex. Opens a local callback on 1455, so SSH must forward it:
#    ssh -L 1455:localhost:1455 -i ~/.ssh/ctswarm_pi quinn@100.118.93.5
codex login

# 3. GitHub. Either `gh auth login` on the board, or copy a token from the
#    laptop into ~/ctswarm/.env as GH_TOKEN=...
gh auth login
```

Then confirm and restart so the agent containers pick up the new `.env`:

```bash
cd ~/ctswarm && ./.venv/bin/ctswarm doctor && CTSWARM_PROFILE=pi ./stack.sh up
```

`doctor` currently reports all three as `no` and reports local backends as
*disabled by subscriptions-only mode*, which is the intended wording and is
deliberately different from "none detected".

## Pi state, verified 2026-08-18

Host: `raspberrypi`, 192.168.0.155 on the LAN, 100.118.93.5 on the tailnet.
User `quinn`. **Ubuntu Server 24.04.3 LTS**, not Raspberry Pi OS. Pi 4B, 4GB,
Python 3.12.3. Root on the SD card (`/dev/mmcblk0p2`, 116G). No USB SSD.

Host preparation, by `infra/pi-host-prep.sh`:

| Item | State |
|---|---|
| DNS | `/etc/resolv.conf` written by hand, resolves |
| Docker | 29.7.2, official packages, `quinn` in the `docker` group |
| Compose | v5.5.0 (the Pi overlay needs >= 2.24) |
| Log bounds | container 5m x 3, journal 100M |
| cgroup memory | already active under cgroup v2; Ubuntu needs no kernel change |
| zram | 1.8G at priority 100 |
| `ctswarm.service` | installed, **deliberately disabled** |
| Repo | `~/ctswarm` on `main` |

Application, by `bootstrap.sh` and `CTSWARM_PROFILE=pi ./stack.sh up`:

| Item | State |
|---|---|
| `.venv`, `.env` | created |
| `vendor/SWE-AF` | vendored at the pinned commit, all fourteen patches applied on arm64 |
| Images | built natively on the board |
| Services | `build-db`, `control-plane`, `ctswarm-scheduler`, `ctswarm-approvals`, `swe-agent`, `swe-fast`, all up, uptime 8h at last check |
| Published ports | every one on `127.0.0.1`: 8092 scheduler, 8091 approvals, 18080 control plane, 8003 swe-agent, 8004 swe-fast |
| MCP registry | materializes to `var/mcp/claude.json` and `var/mcp/codex.toml`, written by uid 10001 at mode 0600 |
| Memory | 914Mi of 3.7Gi used idle, 2.8Gi available, swap untouched |

Not done:

- **No credentials.** None of `claude setup-token`, `codex login`, or
  `gh auth login` has been done on this box, so no build can be launched yet.
  This is the only thing between the board and a first real build.
- **`/etc/resolv.conf` still lists `nameserver 127.0.0.1` first.** Pi-hole is
  gone and nothing listens on 53, so that line is a refused connection on every
  lookup before the public fallback answers. Harmless, worth removing, needs a
  password-gated sudo:
  ```bash
  printf '# No local resolver on this host.\nnameserver 1.1.1.1\nnameserver 8.8.8.8\n' | sudo tee /etc/resolv.conf
  ```
- **Leftover directories**, 1.4GB of disk and no memory: `~/pihole-backup`
  (647M), `~/docker-services/pihole-data` (645M), `~/dht-influx` (94M). The
  `pihole:` service is still in `~/docker-services/docker-compose.yml`, so a
  `docker compose up -d` there would resurrect it and take port 53. Backup of
  that file at `docker-compose.yml.pre-ctswarm`.
- **Plan phase A step 10** (gate the OpenCode curl-installer behind a Docker
  build arg so the Pi image skips it) is still open. The image builds without
  it, so this is a size and build-time cost, not a blocker.

### What happened to the previous services

Pi-hole, homepage, Grafana, and InfluxDB were all running on this box. Removing
the Docker snap took every container and image with it. Grafana and InfluxDB
were removed with the operator's agreement. Pi-hole is not wanted back.

**If the router still hands out 192.168.0.155 as the DNS server over DHCP, the
LAN is pointed at a Pi-hole that no longer exists.** Repoint DHCP before
relying on it.

Homepage is still defined in `~/docker-services/docker-compose.yml` and is not
running. Bringing it back costs roughly 300MB resident, which is real against
the overlay's 3456 MiB of container limits on a 3.7GB board.

## Memory, which is the actual constraint

Idle with the full stack running is 914Mi of 3.7Gi, 2.8Gi available. That is
what the Pi overlay was budgeted against and it fits. What does not
automatically fit is the *target repository's* own `npm ci`, typecheck, and test
run, which happens on this box and is what will exhaust it. Watch
`docker stats` and `dmesg | grep -i oom` through the first few real builds
rather than once.

Root is still on the SD card. A 24/7 factory writing Postgres, Docker layers,
git checkouts and `npm ci` wears one out, and the failure is silent corruption
rather than a clean error. A USB SSD is the highest-value hardware change left.

## Access

SSH key `~/.ssh/ctswarm_pi`, `quinn@100.118.93.5`. Docker works without sudo.
**Sudo requires a password**, so privileged steps need the operator.

Two assistant-side limits are worth recording, because they shape what can be
automated from a session:

- Piping a GitHub token into a remote command to write the Pi's `.env` is
  refused by the permission classifier. The token has to be placed by the
  operator.
- Compound read-only SSH commands are sometimes refused where the same work
  split into simpler calls succeeds. Prefer one command per call.

## Repository state

Nineteen commits on `main` beyond `50714cc`, CI green:

```
653ee6c Stop doctor reporting a Codex login that does not exist
9f3c689 Stop publishing the agent control ports on every interface
92a485b Let the scheduler write the MCP config it is responsible for
bf7a5ae Stop listing host Node as something that blocks a build
5a8a2eb Stop bootstrap reporting a login it invented itself
4306903 Record where the Pi deployment stands, for resuming later
a150380 Do not name a local resolver that is not there
3b2b8bb Detect the memory cgroup instead of assuming Raspberry Pi OS
3d31738 Stop tests from reading the developer's real credentials
221c972 Correct the declared Python floor to the one that works
3fa3c4c Make MCP servers something the operator controls
35060a8 Remove em dashes from prose, comments, and UI strings
bf9d018 Fix the Claude CLI patch, which never applied
e976189 Fold the Pi host preparation into one idempotent script
e85a9c0 Correct Pi assumptions against a real board
869bdb3 Install the Claude Code CLI in the agent image
09e3111 Add Raspberry Pi profile: compose overlay, boot unit, bootstrap path
6d70201 Add a typed settings registry with provenance and audit
c1507ab Add subscriptions-only execution mode
```

### Behaviour that changed on `main`

Both of these affect any host, not just the Pi:

1. **Subscriptions-only is now the default execution mode.** A fresh clone on
   the GPU box will choose the harness path unless `CTSWARM_EXECUTION_MODE=hybrid`
   is set or the setting is changed in the ledger.
2. **MCP configuration now comes from the ctswarm registry** rather than being
   inherited from `~/.claude.json`. The first scheduler start imports whatever
   the host already has, so nothing disappears, but the registry is
   authoritative from then on.

### Plan phases

The approved plan is `~/.claude/plans/synchronous-snuggling-popcorn.md`.

| Phase | State |
|---|---|
| A: subscriptions-only execution | done, except step 10 (OpenCode installer build arg) |
| B: typed settings registry | done, backend only |
| C: real MCP support | done, backend only; see `docs/MCP.md` |
| D: document context | not started |
| E: browser evidence pipeline | not started |
| F: Pi hosting | done: stack builds, runs, and stays healthy on the board; credentials outstanding |
| G: UI reduction and styling | not started |

G is last on purpose: the settings, MCP, document, and evidence screens are all
new UI, so styling before they exist means styling twice. It needs
`/impeccable init` to write `PRODUCT.md` and `DESIGN.md` first, and there is an
Impeccable update available (v3.8.0 to v4.1.1) that the skill asks be raised
with the operator once.

## Decisions worth not relitigating

- **The Pi does no inference.** Every role runs on the Claude Code or Codex CLI.
  That is what makes a board with no accelerator viable at all.
- **Review runs on a different vendor from implementation** (Codex reviews what
  Claude implements). The README's independence rule is by model family, and
  Anthropic and OpenAI are genuinely distinct in a way two Qwen variants are not.
- **`stack.sh start` never rebuilds**, which is why the systemd unit uses it. A
  rebuild at boot turns a power cut into a 45-minute outage.
- **The boot unit ships disabled.** It can be enabled now that `./stack.sh up`
  has succeeded on the board, but only after credentials are in place, so the
  first unattended boot is not also the first credentialed run.
- **Tailscale serve, not Funnel**, and every published port stays on loopback.
  Tailnet membership is the entire authorization boundary: the scheduler has no
  authentication, and `POST /api/swarms` spends your subscriptions.

## Traps found the hard way

Each of these was green until something external disagreed. The last four came
out of the first real run on the board, and none of them could have been found
on the development machine.

- `swe-af-claude-cli.patch` never applied. It was written against SWE-AF at
  HEAD, but it edits a line an earlier patch rewrites first. Nothing caught it
  because `vendor/` is gitignored and was absent, so the patch set had never
  once been applied end to end.
- Two tests passed locally and failed in CI because the credential check falls
  back to the macOS Keychain, so a logged-in laptop answered them. One asserted
  that `{}` counts as a Claude login while another in the same file asserted the
  opposite about the same bytes, and both passed. `tests/conftest.py` now cuts
  off the Keychain, `Path.home()`, and the credential environment for every
  test, autouse.
- `pyproject.toml` claimed Python 3.10 support and shipped a `tomli` fallback
  for it, but `tomllib` is imported unconditionally and is 3.11+. A 3.10 install
  resolved cleanly and then failed at startup.
- The Pi overlay's `volumes: !override` replaces the base list instead of
  merging, so mounts added to `infra/docker-compose.ctswarm.yml` are silently
  absent from the merged Pi config until restated. Always check with
  `CTSWARM_PROFILE=pi ./stack.sh config` after touching either file.
- Host prep assumed Raspberry Pi OS and told an Ubuntu box to reboot for a
  kernel parameter that changes nothing there.
- **`bootstrap.sh` reported a Claude login it had fabricated itself.** It writes
  a `{}` placeholder so the agent containers' bind mount stays a file, then
  tested for that same path with `[[ -f ]]`, so every run after the first
  announced the runtime as configured and dropped the token step from the list
  of things blocking a build. Now shares `capacity.py`'s predicate: the file
  counts only if it parses as a non-empty JSON object.
- **Docker materializes a missing bind mount source as a root-owned directory.**
  This bit three times: `~/.codex/auth.json` became a directory before
  `codex login` ever ran, so the login could not write there either; `~/projects`
  became root-owned, so the operator could not write their own projects
  directory on a box where sudo wants a password. `stack.sh` now creates the
  stubs first and reclaims Docker's leftovers with `rmdir`, which refuses
  anything non-empty, so real content is never touched.
- **The scheduler crash-looped with `Permission denied: /mcp-config/claude.json.tmp`.**
  The image runs as uid 10001 and the host directory belongs to uid 1000.
  Invisible on macOS, where Docker Desktop virtualizes bind-mount ownership, and
  fatal on Linux. Fixed with `group_add` and a group-writable directory rather
  than 0777 or running as root.
- **Both agent services published their control ports on 0.0.0.0**, inherited
  from `vendor/SWE-AF/docker-compose.yml` and never overridden, contradicting the
  README's loopback-only claim. Nothing outside the compose network calls them.
  On a board that is on the LAN as well as the tailnet, that handed the agent
  control API to the whole LAN while tailnet membership was supposed to be the
  entire authorization boundary. `ports: !override` was required, because compose
  *merges* port sequences rather than replacing them.
- **`ctswarm doctor` and the launch gate disagreed about Codex.** `cli.py` used
  `.exists()` where `capacity.py` used a parse-and-check, so the root-owned
  directory above passed the weaker test: doctor said Codex was ready and the
  launch was then refused. Tests now assert the two agree rather than testing
  each in isolation.
