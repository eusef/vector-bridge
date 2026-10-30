#!/usr/bin/env bash
# One-time, BENCH CARDS ONLY: put the Pi on the maintainer's tailnet as `vectorbridge`, so
# vector-swl-radio (tsnet, default host `vectorbridge`) reaches UDP 5100-5101 and TCP 4532
# from off the AP. Not part of install.sh or the release image. Needs internet (home Wi-Fi),
# so not on an AP-only Pi; in the field the tailnet path does not exist.
#
#   sudo vb-maintenance enter                 # must persist, so not in field mode
#   sudo bash /home/vectorbridge/vb-deploy/setup-tailscale.sh          # prints a login URL once
#   sudo vb-maintenance exit
#
# Then turn key expiry off for `vectorbridge` in the Tailscale admin console: nothing writes a
# renewed key back once the root is the RAM overlay again.
#
# State: the root is a RAM overlay in field mode, so tailscaled's login is kept on /persist and
# copied into /var/lib/tailscale before tailscaled starts (maintainer, 2026-10-04). It sits in
# /persist/tailscale, root only, not under /persist/vector-bridge: install.sh chowns that tree to
# the service user, and this file holds the node's private keys. After any later `tailscale up`
# or re-login, save it again in maintenance mode: sudo bash .../setup-tailscale.sh --save
#
# Undo (in maintenance mode):
#   sudo tailscale logout; sudo apt-get purge -y tailscale
#   sudo rm /etc/systemd/system/tailscaled.service.d/vector-bridge.conf; sudo systemctl daemon-reload
#   sudo mount -o remount,rw /persist; sudo rm -r /persist/tailscale; sudo mount -o remount,ro /persist
#   sudo rm /etc/apt/sources.list.d/tailscale.list /usr/share/keyrings/tailscale-archive-keyring.gpg
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
if findmnt -no FSTYPE / | grep -q overlay; then
  echo "refused: field mode, this would vanish at the next reboot. Run 'sudo vb-maintenance enter' first." >&2; exit 3
fi
PERSIST=${VECTOR_BRIDGE_PERSIST:-/persist}
findmnt -no SOURCE "$PERSIST" >/dev/null || { echo "refused: $PERSIST is not mounted" >&2; exit 1; }
SAVED=$PERSIST/tailscale/tailscaled.state
LIVE=/var/lib/tailscale/tailscaled.state

save() {
  [ -s "$LIVE" ] || { echo "nothing to save: $LIVE is missing or empty" >&2; exit 1; }
  mount -o remount,rw "$PERSIST"
  trap 'mount -o remount,ro "$PERSIST" 2>/dev/null || true' EXIT
  install -d -o root -g root -m 700 "$PERSIST/tailscale"
  install -o root -g root -m 600 -p "$LIVE" "$SAVED"
  sync
  echo "saved $LIVE to $SAVED"
}

if [ "${1:-}" = --save ]; then save; exit 0; fi

# ---- package, from Tailscale's own apt repository ------------------------------------
if ! command -v tailscale >/dev/null; then
  . /etc/os-release
  curl -fsSL "https://pkgs.tailscale.com/stable/debian/$VERSION_CODENAME.noarmor.gpg" \
    -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL "https://pkgs.tailscale.com/stable/debian/$VERSION_CODENAME.tailscale-keyring.list" \
    -o /etc/apt/sources.list.d/tailscale.list
  apt-get update -q
  apt-get install -y -q tailscale
fi

# ---- seed the login from /persist before tailscaled starts ---------------------------
# Wants, not Requires: a broken /persist must not stop the unit, it just starts logged out.
# cp -u: only when the saved copy is newer, so a login made since on the ext4 root wins.
DROPIN=/etc/systemd/system/tailscaled.service.d/vector-bridge.conf
mkdir -p "$(dirname "$DROPIN")"
cat > "$DROPIN" <<EOF
[Unit]
Wants=persist.mount
After=persist.mount

[Service]
ExecStartPre=-/bin/sh -c 'if [ -s $SAVED ]; then cp -pu $SAVED $LIVE; fi'
EOF
systemctl daemon-reload
systemctl enable tailscaled
systemctl restart tailscaled

# ---- log in once, then keep the login on /persist ------------------------------------
# --accept-dns=false: the Pi's resolver and the AP's captive DNS stay as install.sh set them.
if ! tailscale status >/dev/null 2>&1; then
  echo "open the URL below and approve the device (it waits)"
  tailscale up --hostname=vectorbridge --accept-dns=false
fi
tailscale status | head -5
save
echo "done. Now: turn key expiry off for vectorbridge in the admin console, then sudo vb-maintenance exit"
