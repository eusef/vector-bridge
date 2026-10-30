#!/usr/bin/env bash
# Build this checkout's wheel and install it on a bench Pi with nobody at the keyboard:
# maintenance mode, copy, install, back to field mode (two reboots, about 3 minutes).
# Connected clients are dropped, not waited for. Needs the one-time bench setup in
# scripts/dev/setup-agent-deploy.sh. tools/deploy.sh is the older rsync + install.sh path.
#
#   tools/deploy_pi.sh [host]     (default vectorbridge.lan; each address it resolves to is tried)
set -euo pipefail
HOST=${1:-vectorbridge.lan}
KEY=${VB_SSH_KEY:-$HOME/.ssh/id_ed25519}
REPO=$(cd "$(dirname "$0")/.." && pwd)
# The card's host key is trusted under its name; an IP passed as HOST is checked against it too.
SSH_OPTS=(-i "$KEY" -o BatchMode=yes -o ConnectTimeout=5 -o HostKeyAlias="${VB_HOSTKEY_ALIAS:-vectorbridge.lan}"
          -o ServerAliveInterval=5 -o ServerAliveCountMax=2)
SITE='/opt/vector-bridge/venv/lib/python3*/site-packages'   # the glob expands on the Pi
# One hash over every module, the same on both ends: proves the Pi runs what was built here.
TREE_SUM="find vector_bridge -name '*.py' | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256 | cut -d' ' -f1"
IP=""

say() { printf '== %s\n' "$*"; }
die() { printf 'deploy_pi: %s\n' "$*" >&2; exit 1; }
on_pi() { ssh "${SSH_OPTS[@]}" "vectorbridge@$IP" "$@"; }

pick_ip() {  # the first address for HOST that answers ssh; a stale DHCP lease can come first
  local ip
  for ip in $(dscacheutil -q host -a name "$HOST" | awk '/ip_address/{print $2}') "$HOST"; do
    if ssh "${SSH_OPTS[@]}" "vectorbridge@$ip" true 2>/dev/null; then IP=$ip; return 0; fi
  done
  return 1
}

root_fs() { on_pi 'findmnt -no FSTYPE /' 2>/dev/null || true; }   # overlay = field mode

wait_for() {  # wait_for field|maintenance, across the reboot the previous step started
  local want=$1 end=$((SECONDS + 300)) fs
  sleep 15
  while [ $SECONDS -lt $end ]; do
    if pick_ip; then
      fs=$(root_fs)
      [ "$want" = field ] && [ "$fs" = overlay ] && return 0
      [ "$want" = maintenance ] && [ -n "$fs" ] && [ "$fs" != overlay ] && return 0
    fi
    sleep 5
  done
  die "the Pi did not come back in $want mode within 5 minutes"
}

reboot_step() {  # run a step that ends in a reboot; a dropped ssh session (255) is expected
  local rc=0
  on_pi "$1" || rc=$?
  [ $rc -eq 0 ] || [ $rc -eq 255 ] || die "'$1' failed (exit $rc)"
}

cd "$REPO"
commit=$(git rev-parse --short HEAD)
dirty=$(git status --porcelain --untracked-files=no)
say "building wheel from $commit${dirty:+ plus uncommitted changes}"
OUT=$(mktemp -d)
# The footer and `vector-bridge --version` show this commit; "+" marks uncommitted changes.
STAMP=src/vector_bridge/_build.py
trap 'rm -f "$REPO/$STAMP"' EXIT  # also when the wheel build fails
printf 'COMMIT = "%s"\n' "$commit${dirty:++}" > "$STAMP"
.venv/bin/python -m pip wheel -q --disable-pip-version-check --no-deps -w "$OUT" .
rm -f "$STAMP"  # a source checkout shows no commit rather than a stale one
WHL=$(ls "$OUT"/vector_bridge-*-py3-none-any.whl)
mkdir "$OUT/tree" && unzip -q "$WHL" -d "$OUT/tree"
LOCAL_SUM=$(cd "$OUT/tree" && eval "$TREE_SUM")

pick_ip || die "cannot reach $HOST over ssh with $KEY"
say "Pi at $IP"
on_pi 'sudo -n -l /usr/local/sbin/vb-agent-install >/dev/null 2>&1' \
  || die "password-free deploy is not set up on this card: see scripts/dev/setup-agent-deploy.sh"

fs=$(root_fs)
[ -n "$fs" ] || die "could not read the Pi's root filesystem type"
if [ "$fs" = overlay ]; then
  say "entering maintenance mode (reboot; clients are dropped)"
  reboot_step 'sudo -n /usr/local/sbin/vb-agent-enter'
  wait_for maintenance
  say "Pi back at $IP in maintenance mode"
fi
on_pi '[ -f /persist/vector-bridge/maintenance ]' \
  || die "root is writable but the maintenance flag is missing; stopping rather than guessing"

say "copying $(basename "$WHL")"
on_pi 'mkdir -p vb-deploy && find vb-deploy -name "vector_bridge-*.whl" -delete'
scp -q "${SSH_OPTS[@]}" "$WHL" "vectorbridge@$IP:vb-deploy/"
[ "$(on_pi "shasum -a 256 vb-deploy/$(basename "$WHL")" | cut -d' ' -f1)" = "$(shasum -a 256 "$WHL" | cut -d' ' -f1)" ] \
  || die "the wheel's checksum changed in transit"

say "installing"
on_pi 'sudo -n /usr/local/sbin/vb-agent-install'
say "back to field mode (vb-maintenance exit verifies, then reboots)"
reboot_step 'sudo -n /usr/local/bin/vb-maintenance exit'
wait_for field

say "verifying"
for _ in $(seq 1 15); do
  on_pi 'systemctl is-active -q vector-bridge vector-bridge-tuner' && break
  sleep 2
done
on_pi 'systemctl is-active -q vector-bridge vector-bridge-tuner' \
  || die "a service is not active: $(on_pi 'systemctl is-active vector-bridge vector-bridge-tuner' | tr '\n' ' ')"
on_pi '[ ! -f /persist/vector-bridge/maintenance ]' || die "the maintenance flag is still set"
PI_SUM=$(on_pi "cd $SITE && $TREE_SUM")
[ "$PI_SUM" = "$LOCAL_SUM" ] || die "the Pi's installed code does not match the wheel built here"
say "deployed $commit${dirty:+ (with uncommitted changes)} to $IP: field mode, services active, code matches"
