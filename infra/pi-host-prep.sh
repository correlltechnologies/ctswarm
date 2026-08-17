#!/usr/bin/env bash
#
# Raspberry Pi host preparation for ctswarm.
#
#   sudo bash infra/pi-host-prep.sh
#
# Everything here is a host-level change that ctswarm cannot make for itself:
# cgroup memory accounting, swap, Docker, log bounds, and the DNS interaction
# between Tailscale and a locally hosted resolver. Each step is idempotent, so
# re-running after a failure is safe.
#
# See docs/RASPBERRY_PI.md for why each of these matters.

set -uo pipefail

TARGET_USER="${SUDO_USER:-${TARGET_USER:-}}"

say() { printf '\n=== %s ===\n' "$1"; }
ok()  { printf '  ok: %s\n' "$1"; }
bad() { printf '  FAILED: %s\n' "$1"; }

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run this with sudo: sudo bash infra/pi-host-prep.sh" >&2
  exit 1
fi

if [[ -z "$TARGET_USER" || "$TARGET_USER" == "root" ]]; then
  echo "cannot determine the non-root account to configure." >&2
  echo "re-run as: sudo TARGET_USER=<youruser> bash infra/pi-host-prep.sh" >&2
  exit 1
fi

reboot_needed=0
failures=0
fail() { bad "$1"; failures=$((failures + 1)); }

# --------------------------------------------------------------------- DNS
say "1/7  DNS"
# If this Pi is also the tailnet's DNS server, Tailscale points resolv.conf at
# MagicDNS on 100.100.100.100, which forwards back to this same machine. On
# Raspberry Pi OS /usr/sbin/resolvconf is a symlink to resolvectl, so
# Tailscale's restore path fails and leaves a resolver that never answers.
# The symptom is that ping 8.8.8.8 works while every hostname fails.
if command -v tailscale >/dev/null 2>&1; then
  if tailscale status >/dev/null 2>&1; then
    tailscale set --accept-dns=false 2>/dev/null \
      && ok "tailscale no longer manages resolv.conf" \
      || ok "tailscale is present but --accept-dns=false was not applied"
  fi
fi

if ! getent hosts github.com >/dev/null 2>&1; then
  # Ordering matters. 127.0.0.1 is the local resolver (Pi-hole or similar).
  # When it is down the kernel refuses the connection immediately rather than
  # timing out, so glibc reaches the public fallback in well under a second.
  # That keeps this machine resolving through a local-resolver outage, which
  # is exactly the failure that otherwise takes the whole box offline.
  [[ -L /etc/resolv.conf ]] && rm -f /etc/resolv.conf
  {
    echo "# Written by ctswarm infra/pi-host-prep.sh."
    echo "# Local resolver first, public fallback second, so an outage of the"
    echo "# local resolver cannot take this host's own name resolution with it."
    echo "nameserver 127.0.0.1"
    echo "nameserver 1.1.1.1"
    echo "nameserver 8.8.8.8"
  } > /etc/resolv.conf
  chmod 644 /etc/resolv.conf
fi

if getent hosts github.com >/dev/null 2>&1; then
  ok "github.com resolves"
else
  fail "cannot resolve github.com; every remaining step needs the network"
  exit 1
fi

# ------------------------------------------------------------------ Docker
say "2/7  Docker"
# Canonical's Docker snap is strictly confined: bind mounts work only from
# $HOME, and its socket is root-owned rather than group-owned, so the usual
# usermod does nothing. The compose overlay assumes the official packages.
if command -v docker >/dev/null 2>&1 && ! readlink -f "$(command -v docker)" | grep -q '^/snap/'; then
  ok "already installed: $(docker --version)"
else
  if command -v snap >/dev/null 2>&1 && snap list docker >/dev/null 2>&1; then
    ok "removing the docker snap first"
    snap remove docker || fail "snap remove docker"
  fi
  curl -fsSL https://get.docker.com | sh || { fail "docker install"; exit 1; }
  ok "installed $(docker --version)"
fi

usermod -aG docker "$TARGET_USER" \
  && ok "$TARGET_USER is in the docker group (log out and back in to use it)" \
  || fail "usermod -aG docker"

if docker compose version >/dev/null 2>&1; then
  ok "$(docker compose version)"
else
  fail "docker compose plugin missing; the Pi overlay needs compose >= 2.24"
fi

# -------------------------------------------------------------------- Logs
say "3/7  Bound the logs"
# A factory running 24/7 fills a disk with container logs and journal entries
# long before it fills it with anything useful.
mkdir -p /etc/docker
if [[ -f /etc/docker/daemon.json ]] && ! grep -q '"max-size"' /etc/docker/daemon.json; then
  cp /etc/docker/daemon.json /etc/docker/daemon.json.pre-ctswarm
  ok "backed up the existing daemon.json"
fi
{
  echo '{'
  echo '  "log-driver": "json-file",'
  echo '  "log-opts": {"max-size": "5m", "max-file": "3"},'
  echo '  "storage-driver": "overlay2"'
  echo '}'
} > /etc/docker/daemon.json
systemctl restart docker && ok "docker restarted with bounded logs" || fail "docker restart"

if ! grep -q '^SystemMaxUse=100M' /etc/systemd/journald.conf 2>/dev/null; then
  sed -i 's/^#\?SystemMaxUse=.*/SystemMaxUse=100M/' /etc/systemd/journald.conf
  grep -q '^SystemMaxUse=' /etc/systemd/journald.conf \
    || echo 'SystemMaxUse=100M' >> /etc/systemd/journald.conf
  systemctl restart systemd-journald
fi
ok "journald capped at 100M"

# ----------------------------------------------------------------- cgroups
say "4/7  cgroup memory accounting"
# Raspberry Pi OS ships with this off, and every mem_limit in the Pi compose
# overlay is silently ignored until it is on. Silently: no warning, no error,
# the container simply grows until the OOM killer takes something.
CMDLINE=/boot/firmware/cmdline.txt
[[ -f "$CMDLINE" ]] || CMDLINE=/boot/cmdline.txt
if [[ ! -f "$CMDLINE" ]]; then
  fail "no cmdline.txt found; mem_limit will be silently ignored"
elif grep -q 'cgroup_enable=memory' "$CMDLINE"; then
  ok "already enabled"
else
  cp "$CMDLINE" "$CMDLINE.pre-ctswarm"
  # This file must remain exactly one line; a newline in the middle silently
  # truncates the kernel command line at that point.
  printf '%s cgroup_enable=memory cgroup_memory=1\n' "$(tr -d '\n' < "$CMDLINE")" > "$CMDLINE"
  ok "appended to $CMDLINE (backup at $CMDLINE.pre-ctswarm)"
  reboot_needed=1
fi

# -------------------------------------------------------------------- Swap
say "5/7  zram swap"
# Compressed RAM swap is cheap. Swapping to an SD card is not, and it is the
# fastest way to wear one out. swappiness=100 is correct only with zram.
systemctl disable --now dphys-swapfile 2>/dev/null && ok "disabled the SD-card swapfile" || true
if ! dpkg -s zram-tools >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq zram-tools || fail "zram-tools install"
fi
{
  echo 'ALGO=zstd'
  echo 'PERCENT=50'
  echo 'PRIORITY=100'
} > /etc/default/zramswap
systemctl restart zramswap 2>/dev/null && ok "zram active" || fail "zramswap service"

{
  echo 'vm.swappiness=100'
  echo 'vm.vfs_cache_pressure=50'
  echo 'vm.page-cluster=0'
} > /etc/sysctl.d/99-ctswarm.conf
sysctl -p /etc/sysctl.d/99-ctswarm.conf >/dev/null && ok "sysctl applied" || fail "sysctl"

# ------------------------------------------------------------- Boot on power
say "6/7  Start on boot"
UNIT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ctswarm.service"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$UNIT_SRC" ]]; then
  sed -e "s|^User=.*|User=$TARGET_USER|" \
      -e "s|^WorkingDirectory=.*|WorkingDirectory=$REPO_DIR|" \
      -e "s|^ExecStart=.*|ExecStart=$REPO_DIR/stack.sh start|" \
      -e "s|^ExecStop=.*|ExecStop=$REPO_DIR/stack.sh down|" \
      "$UNIT_SRC" > /etc/systemd/system/ctswarm.service
  systemctl daemon-reload
  ok "installed ctswarm.service for $TARGET_USER at $REPO_DIR"
  ok "not enabled yet; run 'sudo systemctl enable --now ctswarm' after the first successful ./stack.sh up"
else
  fail "infra/ctswarm.service not found next to this script"
fi

# ------------------------------------------------------------------ Report
say "7/7  Result"
free -h
echo
if [[ "$failures" -gt 0 ]]; then
  echo ">> $failures step(s) failed. Read the FAILED lines above before continuing."
fi
if [[ "$reboot_needed" == "1" ]]; then
  echo ">> REBOOT REQUIRED before mem_limit takes effect: sudo reboot"
else
  echo ">> No reboot required."
fi
echo ">> Next: cd $REPO_DIR && ./bootstrap.sh"
exit "$(( failures > 0 ? 1 : 0 ))"
