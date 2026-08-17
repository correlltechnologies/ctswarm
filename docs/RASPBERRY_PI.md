# Running ctswarm on a Raspberry Pi

Target: **Raspberry Pi 4 Model B, 4GB, arm64**, running continuously until a
swarm finishes and opens its pull request, reachable from a phone over Tailscale.

The Pi does no inference. Every agent role runs on the Claude Code and Codex
CLIs, so the thinking happens at Anthropic and OpenAI and the Pi orchestrates.
That is what makes a board with no accelerator a viable host at all.

## Architecture support is confirmed, memory is the real constraint

Checked rather than assumed:

| Component | arm64 |
|---|---|
| `agentfield/control-plane:latest` | publishes a `linux/arm64` manifest |
| `@openai/codex` | ships `@openai/codex-linux-arm64` |
| `@anthropic-ai/claude-code` | ships `@anthropic-ai/claude-code-linux-arm64` |
| `agent-browser` | dependency-free JS, drives system Chromium |
| Chromium | in Debian arm64 |
| Python deps | pure-Python, arm64 wheels |
| Dashboard build | lockfile carries `@tailwindcss/oxide-linux-arm64-*`, `lightningcss-linux-arm64-*` |

Re-check the first one before a fresh install, since it is the only third-party
image and a published manifest can change:

```bash
docker manifest inspect agentfield/control-plane:latest | \
  grep -A3 '"platform"' | grep -c arm64      # must be at least 1
```

**Be realistic about 4GB.** The container limits total 3456 MiB of roughly
3.7GB usable. That is deliberate but tight, and it does not include the *target
repository's* own `npm install`, typecheck, and test run, which is the thing
that actually exhausts the box. Expect this to work for small and medium
targets and to struggle with a large Node application. If builds keep dying,
the answer is a remote runner (`docs/REMOTE_EXECUTION.md`), not more tuning.

## Host preparation

### 0. Budget the memory against what the Pi is *already* running

Do this first, because it decides whether the rest is worth doing:

```bash
free -h
ps -eo rss,comm --sort=-rss | head -12 | awk '{printf "%6.0f MB  %s\n", $1/1024, $2}'
```

A Pi that is also a Pi-hole, a Grafana/InfluxDB box, or a small app server can
easily have 1GB+ committed before ctswarm starts. ctswarm needs roughly
1.3-1.8GB resident in normal operation, and the *target repository's* own
`npm ci` and test run needs whatever is left. On a 4GB board that means the
existing workload plus ctswarm plus a real build does not fit unless the box is
close to idle.

If it does not fit, the honest options are to move the other services off, to
accept that only small targets will build, or to run the Pi as scheduler and
dashboard with a remote runner doing the work (`docs/REMOTE_EXECUTION.md`).
More tuning will not create memory that is not there.

### 1. Enable cgroup memory accounting (mandatory)

Raspberry Pi OS ships with this off, and **every `mem_limit` in the compose
file is silently ignored until it is on.** Append to `/boot/firmware/cmdline.txt`
— one single line, do not add a newline:

```
cgroup_enable=memory cgroup_memory=1
```

Reboot, then verify. If this prints a warning, the limits are not being applied:

```bash
docker info 2>&1 | grep -i "no memory limit" && echo "NOT APPLIED" || echo "ok"
```

### 2. Boot from a USB SSD, not the SD card

This is the highest-value hardware decision here. A 24/7 factory running
Postgres, Docker layer writes, git checkouts and `npm ci` will wear out an SD
card, and the failure mode is silent corruption rather than a clean error.

At minimum move Docker's storage:

```bash
sudo systemctl stop docker
sudo rsync -aHAX /var/lib/docker/ /mnt/ssd/docker/
sudo rm -rf /var/lib/docker
sudo ln -s /mnt/ssd/docker /var/lib/docker
sudo systemctl start docker
```

### 3. zram instead of SD swap

Compressed RAM swap is cheap; swapping to an SD card is not. `swappiness=100`
is correct **only** in combination with zram.

```bash
sudo systemctl disable --now dphys-swapfile
sudo apt install -y zram-tools
```

`/etc/default/zramswap`:

```
ALGO=zstd
PERCENT=50
PRIORITY=100
```

`/etc/sysctl.d/99-ctswarm.conf`:

```
vm.swappiness=100
vm.vfs_cache_pressure=50
vm.page-cluster=0
```

### 4. Do not use the Docker snap

Check what you have:

```bash
which docker            # /snap/bin/docker means the snap
```

Canonical's Docker snap is strictly confined. Bind mounts only work from `$HOME`
and removable media, so the repository, the projects root, and every mounted
credential file must live under your home directory or the containers start
with empty mounts. Its socket is also `root:root` rather than group-owned, so
the usual `usermod -aG docker` does nothing and every command needs `sudo`.

Prefer the official packages:

```bash
sudo snap remove docker              # exports first if you have data in it
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"      # log out and back in
docker compose version               # must be >= 2.24 for the Pi overlay
```

If you must keep the snap, at minimum connect the home interface and keep
everything under `$HOME`:

```bash
sudo snap connect docker:home
sudo addgroup --system docker && sudo adduser "$USER" docker
sudo snap disable docker && sudo snap enable docker
```

### 5. Tailscale and Pi-hole on the same box

If this Pi is *also* your tailnet DNS server, do not let it accept Tailscale
DNS. Tailscale rewrites `/etc/resolv.conf` to point at MagicDNS on
`100.100.100.100`, which then forwards back to this same machine. When the
handoff breaks you get the confusing symptom of `ping 8.8.8.8` working while
every hostname fails, and `dig @127.0.0.1` succeeding while `getent hosts`
does not.

On Raspberry Pi OS the usual cause is that `/usr/sbin/resolvconf` is a symlink
to `resolvectl`, so Tailscale's resolvconf call fails with
`Failed to resolve interface "tailscale": No such device` and leaves a
`resolv.conf` pointing at a resolver that never answers. Check with
`tailscale status` — the health section reports it.

```bash
sudo tailscale set --accept-dns=false
```

That restores the pre-Tailscale `resolv.conf` (`nameserver 127.0.0.1`, i.e.
Pi-hole). The cost is that this node stops resolving `*.ts.net` names; it can
still reach tailnet peers by IP, and every other device keeps using Pi-hole
normally.

### 6. Bound the logs

`/etc/docker/daemon.json`:

```json
{
  "log-driver": "json-file",
  "log-opts": {"max-size": "5m", "max-file": "3"},
  "storage-driver": "overlay2"
}
```

And in `/etc/systemd/journald.conf`: `SystemMaxUse=100M`.

## Install

```bash
git clone git@github.com:correlltechnologies/ctswarm.git ~/ctswarm
cd ~/ctswarm
./bootstrap.sh
```

On an arm64 host with no accelerator, `bootstrap.sh` selects subscriptions-only
mode automatically: it skips the local-backend probe and the model downloads,
pins `CTSWARM_EXECUTION_MODE=subscription_only` in `.env`, and reports which
logins are still missing. Force it anywhere with `--subscription-only`.

### Credentials

Nothing is acquired automatically; each of these is an interactive login.

| Credential | Command | Headless? |
|---|---|---|
| Claude | `claude setup-token`, paste into `.env` as `CLAUDE_CODE_OAUTH_TOKEN` | **Yes** — prints a token, no browser redirect |
| GitHub | `gh auth login --web` | **Yes** — device-code flow, prints a code |
| Codex | `codex login` | **No** — needs a loopback browser callback |

`codex login` cannot complete over plain SSH. Forward the port from a machine
that has a browser, then run the login inside that session:

```bash
ssh -L 1455:localhost:1455 pi@raspberrypi
# in that session:
codex login
```

`bootstrap.sh` detects an SSH session with no display and prints this command
with your actual host filled in.

### Start it

```bash
CTSWARM_PROFILE=pi ./stack.sh up      # first run: builds images, slow
./.venv/bin/ctswarm doctor
```

`doctor` should report both harnesses available and local backends
*disabled by subscriptions-only mode* — which is a different thing from
"none detected", and the wording is deliberate.

Building the images natively on a Pi takes a while. If you would rather not
have the Pi compile a React app, cross-build on a faster machine and push to a
registry:

```bash
docker buildx build --platform linux/arm64 -f infra/Dockerfile.router -t <registry>/ctswarm-router:arm64 --push .
```

### Boot on power-up

```bash
sudo cp infra/ctswarm.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ctswarm
```

The unit runs `./stack.sh start`, which never rebuilds. `up` rebuilds; a
rebuild at boot turns a power cut into a 45-minute outage, or into no factory
at all if the network is flaky while it runs.

## Reaching it from a phone

Install Tailscale on the **host**, not in a container:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
sudo tailscale serve --bg --https=443 http://127.0.0.1:8092
```

This is the recommended exposure because it changes nothing about the stack:
every published port stays bound to `127.0.0.1`, so a firewall mistake cannot
expose them. You get HTTPS with a real certificate and a stable
`https://<host>.<tailnet>.ts.net` name that works from a phone.

**Never use Tailscale Funnel here**, and never bind the router to a public
interface.

### The scheduler has no authentication

Say this out loud, because it decides how much care the next step deserves:
`POST /api/swarms` starts builds against your repositories using your
subscription credentials, and `PUT /api/settings` changes how they are spent.
**Tailnet membership is the entire authorization boundary.** If you share the
tailnet with anyone, add an ACL restricting port 8092 to your own user.

## Operating notes

- Concurrency stays at 1. The agents are network-bound, but the target repo's
  build is not, and two at once is what pushes a 4GB box into swap.
- Watch the first real build: `docker stats`, then `dmesg | grep -i oom`
  afterwards. Do this for the first few builds rather than once.
- Timeouts are raised on this profile (`CTSWARM_NO_PROGRESS_TIMEOUT_S=5400`,
  `CTSWARM_AGENT_TIMEOUT_SECONDS=2700`) because a healthy build on a Pi can sit
  inside a dependency install longer than the desktop defaults tolerate. Being
  killed as stalled is the failure that looks like a bug in the factory.
- Chromium runs with `--disable-dev-shm-usage` and a 384MB JS heap cap, one
  browser at a time.
- The stack survives restarts: the scheduler re-attaches to an in-flight
  AgentField execution rather than resubmitting it. Reboot mid-build once,
  deliberately, and confirm no duplicate execution appears in
  `ctswarm status`.

## Verifying the configuration before you trust it

A `depends_on` left pointing at the profile-disabled router is a silent boot
hang, so check the merged result rather than the source files:

```bash
CTSWARM_PROFILE=pi ./stack.sh config > /tmp/merged.yml

grep -c 'ctswarm-router:' /tmp/merged.yml          # expect 0 services
grep -A4 depends_on /tmp/merged.yml                # must not mention the router
grep -E 'ANTHROPIC_API_KEY|OPENAI_API_KEY' /tmp/merged.yml   # must all be ""
grep mem_limit /tmp/merged.yml                     # expect 6
```

The API keys matter: both CLIs prefer an API key over a subscription login when
one is present, so a key that survives into the merged config is a live paid
path on a host whose whole purpose is that it bills to a subscription.
