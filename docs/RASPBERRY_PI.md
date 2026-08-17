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

### 1. Run the host prep script

Steps 2 through 6 below are all things ctswarm cannot do for itself, and all of
them are in one idempotent script. Read it first, then:

```bash
sudo bash infra/pi-host-prep.sh
```

It repairs DNS if Tailscale broke it, replaces the Docker snap with the
official packages, bounds the container and journal logs, enables cgroup memory
accounting, switches swap to zram, and installs the systemd unit with your
username and repository path filled in. It does not enable that unit; do that
after the first successful `./stack.sh up`.

The rest of this section explains what each step is for, because when one of
them fails the symptom is rarely obvious.

### 2. cgroup memory accounting (mandatory)

Raspberry Pi OS ships with this off, and **every `mem_limit` in the compose
file is silently ignored until it is on.** No warning, no error: the container
simply grows until the OOM killer takes something else. The script appends
`cgroup_enable=memory cgroup_memory=1` to `/boot/firmware/cmdline.txt`, which
must stay a single line. **This one needs a reboot.** Verify afterwards; if
this prints a warning, the limits are not being applied:

```bash
docker info 2>&1 | grep -i "no memory limit" && echo "NOT APPLIED" || echo "ok"
```

### 3. Boot from a USB SSD, not the SD card

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

### 4. zram instead of SD swap

Compressed RAM swap is cheap; swapping to an SD card is not, and it is the
fastest way to wear one out. `swappiness=100` is correct **only** in
combination with zram, which is why the script sets both together.

### 5. Do not use the Docker snap

Check what you have:

```bash
readlink -f "$(command -v docker)"    # a /snap/ path means the snap
```

Canonical's Docker snap is strictly confined. Bind mounts only work from `$HOME`
and removable media, so the repository, the projects root, and every mounted
credential file must live under your home directory or the containers start
with empty mounts. Its socket is also `root:root` rather than group-owned, so
the usual `usermod -aG docker` does nothing and every command needs `sudo`.

The script removes it and installs the official packages. **Export anything you
care about first**: `snap remove docker` takes its containers and volumes with
it, and if this box is also your Pi-hole, that means your network loses DNS
until you bring it back. Bind-mounted data under `$HOME` survives; named
volumes do not.

The Pi overlay needs `docker compose` >= 2.24 for `!reset` and `!override`.

### 6. Tailscale and Pi-hole on the same box

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
`tailscale status`, whose health section reports it.

```bash
sudo tailscale set --accept-dns=false
```

That is *supposed* to restore the pre-Tailscale `resolv.conf`. When resolvconf
is the broken symlink above it cannot, and you are left with a machine that
still cannot resolve anything even though you ran the documented fix. The
script therefore writes `/etc/resolv.conf` directly:

```
nameserver 127.0.0.1
nameserver 1.1.1.1
nameserver 8.8.8.8
```

The ordering is the point. `127.0.0.1` is Pi-hole, so normal resolution stays
filtered. When Pi-hole is down the kernel refuses the connection *immediately*
rather than timing out, so glibc reaches the public fallback in well under a
second. Without that fallback, stopping Pi-hole for thirty seconds also stops
this host from resolving, which is how a routine container restart turns into
an outage you cannot debug because `apt`, `git`, and `curl` have all gone dark
at the same moment.

The cost of `--accept-dns=false` is that this node stops resolving `*.ts.net`
names. It still reaches tailnet peers by IP, and every other device keeps using
Pi-hole normally.

### 7. Bound the logs

Container logs are capped at 5MB × 3 in `/etc/docker/daemon.json` and the
journal at 100M in `/etc/systemd/journald.conf`. A factory running for weeks
fills a disk with logs long before it fills one with anything useful.

## Install

```bash
git clone git@github.com:correlltechnologies/ctswarm.git ~/ctswarm
cd ~/ctswarm
sudo bash infra/pi-host-prep.sh    # see "Host preparation" above; may ask for a reboot
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
| Claude | `claude setup-token`, paste into `.env` as `CLAUDE_CODE_OAUTH_TOKEN` | **Yes**, prints a token with no browser redirect |
| GitHub | `gh auth login --web` | **Yes**, device-code flow that prints a code |
| Codex | `codex login` | **No**, needs a loopback browser callback |

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
*disabled by subscriptions-only mode*, which is a different thing from
"none detected", and the wording is deliberate.

Building the images natively on a Pi takes a while. If you would rather not
have the Pi compile a React app, cross-build on a faster machine and push to a
registry:

```bash
docker buildx build --platform linux/arm64 -f infra/Dockerfile.router -t <registry>/ctswarm-router:arm64 --push .
```

### Boot on power-up

`infra/pi-host-prep.sh` already installed the unit with your username and
repository path substituted in. It deliberately left it disabled, because a
unit that starts a stack you have never successfully started by hand turns a
first-boot problem into a reboot loop. Once `./stack.sh up` has worked once:

```bash
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
