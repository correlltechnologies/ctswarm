# Raspberry Pi deployment: where this stands

**As of 2026-08-17.** A resume point, not a runbook. `docs/RASPBERRY_PI.md` is
the runbook; this records what is done, what is not, and what to do next.

The goal driving this work: run the whole factory unattended on a Raspberry Pi,
using only Claude and Codex subscriptions with no local models, reachable from a
phone over Tailscale, running until a swarm finishes and opens its PR.

## Where to pick up

Nothing is installed on the Pi yet. Host preparation is done; the application
is not. Next command, on the Pi:

```bash
cd ~/ctswarm && git pull && ./bootstrap.sh
```

It should detect arm64 with no accelerator, select subscriptions-only, skip the
model downloads, vendor SWE-AF at the pinned commit, apply the fourteen
patches, create `.venv`, and report which logins are missing. Then:

```bash
CTSWARM_PROFILE=pi ./stack.sh up          # first run builds images, slow
./.venv/bin/ctswarm doctor
```

`doctor` should report Claude and Codex available and local backends *disabled
by subscriptions-only mode*, which is deliberately different wording from "none
detected".

## Pi state, verified 2026-08-17

Host: `raspberrypi`, 192.168.0.155 on the LAN, 100.118.93.5 on the tailnet.
User `quinn`. **Ubuntu Server 24.04.3 LTS**, not Raspberry Pi OS. Pi 4B, 4GB,
Python 3.12.3. Root on the SD card (`/dev/mmcblk0p2`, 116G, 7% used). No USB SSD.

Done by `infra/pi-host-prep.sh`:

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

Not done:

- **No `.venv`, no `.env`, no `vendor/SWE-AF`.** `bootstrap.sh` has not run.
- **No credentials.** None of `claude setup-token`, `codex login`, or
  `gh auth login` has been done on this box.
- **`/etc/resolv.conf` still lists `nameserver 127.0.0.1` first.** Pi-hole is
  gone and nothing listens on 53, so that line is a refused connection on every
  lookup before the public fallback answers. Harmless, worth removing:
  ```bash
  printf '# No local resolver on this host.\nnameserver 1.1.1.1\nnameserver 8.8.8.8\n' | sudo tee /etc/resolv.conf
  ```
- **A reboot was requested and is not needed.** The script appended
  `cgroup_enable=memory cgroup_memory=1` to `/boot/firmware/cmdline.txt` before
  it knew to check the live controller. The parameter is a no-op on Ubuntu.
  Original at `/boot/firmware/cmdline.txt.pre-ctswarm`. Fixed in `3b2b8bb`.
- **Leftover directories**, 1.4GB of disk and no memory: `~/pihole-backup`
  (647M), `~/docker-services/pihole-data` (645M), `~/dht-influx` (94M). The
  `pihole:` service is still in `~/docker-services/docker-compose.yml`, so a
  `docker compose up -d` there would resurrect it and take port 53. Backup of
  that file at `docker-compose.yml.pre-ctswarm`.

### What happened to the previous services

Pi-hole, homepage, Grafana, and InfluxDB were all running on this box. Removing
the Docker snap took every container and image with it: the box now has zero of
each. Grafana and InfluxDB were removed with the operator's agreement. Pi-hole
is not wanted back.

**If the router still hands out 192.168.0.155 as the DNS server over DHCP, the
LAN is pointed at a Pi-hole that no longer exists.** Repoint DHCP before
relying on it.

Homepage is still defined in `~/docker-services/docker-compose.yml` and is not
running. Bringing it back costs roughly 300MB resident, which is real against
the overlay's 3456 MiB of container limits on a 3.7GB board.

## Memory, which is the actual constraint

Idle baseline after the cleanup is 537Mi of 3.7Gi. That is what the Pi overlay
was budgeted against and it fits. What does not automatically fit is the
*target repository's* own `npm ci`, typecheck, and test run, which happens on
this box and is what will exhaust it. Watch `docker stats` and `dmesg | grep -i
oom` through the first few real builds rather than once.

Root is still on the SD card. A 24/7 factory writing Postgres, Docker layers,
git checkouts and `npm ci` wears one out, and the failure is silent corruption
rather than a clean error. A USB SSD is the highest-value hardware change left.

## Access

SSH key `~/.ssh/ctswarm_pi`, `quinn@100.118.93.5`. Docker works without sudo.
**Sudo requires a password**, so privileged steps need the operator. Separately,
the assistant's sandbox refused state-changing commands over SSH (`scp`,
`docker compose down`, `rm -rf`), so remote *reads* worked and remote *writes*
did not. Granting passwordless sudo alone would not have lifted that; it needs a
Bash permission rule for the ssh command.

## Repository state

Thirteen commits on `main` beyond `50714cc`, CI green:

```
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
| A: subscriptions-only execution | done |
| B: typed settings registry | done, backend only |
| C: real MCP support | done, backend only; see `docs/MCP.md` |
| D: document context | not started |
| E: browser evidence pipeline | not started |
| F: Pi hosting | compose overlay, boot unit, bootstrap path, host prep done; not yet run on the board |
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
- **The boot unit ships disabled.** Enabling it before `./stack.sh up` has
  succeeded once turns a first-boot problem into a reboot loop.
- **Tailscale serve, not Funnel**, and every published port stays on loopback.
  Tailnet membership is the entire authorization boundary: the scheduler has no
  authentication, and `POST /api/swarms` spends your subscriptions.

## Traps found the hard way

Each of these was green until something external disagreed:

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
