#!/usr/bin/env bash
# Last step on the golden card before it is captured. Run as root in maintenance mode:
#   sudo bash scripts/image/prepare-golden.sh
# Removes everything that must differ per device or must not ship, arms vb-firstboot, and
# powers off. The card is not usable as a bridge again until it has been through a first
# boot. See DOCS/image-build.md.
set -euo pipefail
trap '' HUP     # deleting the Wi-Fi profiles drops an SSH session over Wi-Fi; finish anyway
PATH=$PATH:/usr/sbin:/sbin
ROOT=${VB_GOLDEN_ROOT:-}   # tests only: a fake root in front of every file this script removes
[ "$(id -u)" -eq 0 ] || { echo "prepare-golden.sh: run as root (sudo)"; exit 1; }
if findmnt -no FSTYPE / | grep -q overlay; then
  echo "refused: root overlay is active. Enter maintenance mode first." >&2
  exit 3
fi
MOUNT=${VECTOR_BRIDGE_PERSIST:-/persist}
# Without vb-ap-apply.path a burned card's open access point could never be secured.
# is-enabled with two units passes when either one is enabled, so ask one at a time.
# Without the two vb-ssh units the owner could never turn SSH back on (R-D4).
# Without avahi-daemon vectorbridge.local never resolves (REQ-NET-008).
for u in vb-firstboot vb-ap-apply.path vb-wifi-join.path vb-ssh-apply.path vb-ssh-boot.service avahi-daemon.service; do
  systemctl is-enabled "$u" >/dev/null && continue
  echo "$u is not installed; run scripts/install.sh" >&2; exit 1; done

# The maintenance flag would make the burned card look like it is mid-maintenance; a stale
# Wi-Fi request or open-AP marker would confuse first boot, and a stale join result would show
# "Joined <bench network>" on a fresh card (seen on the 0.1.2 burn).
mount -o remount,rw "$MOUNT"
rm -f "$MOUNT"/vector-bridge/maintenance "$MOUNT"/vector-bridge/ap-pending.json "$MOUNT"/vector-bridge/ap-unsecured \
  "$MOUNT"/vector-bridge/wifi-join.json "$MOUNT"/vector-bridge/wifi-join-result.json "$MOUNT"/vector-bridge/home-network.json \
  "$MOUNT"/vector-bridge/ssh.json "$MOUNT"/vector-bridge/ssh-request.json "$MOUNT"/vector-bridge/ssh-result.json \
  "$MOUNT"/vector-bridge/wifi-country.json
# The bench admin PIN and the apps' tokens: kept, every owner's bridge would ask for a PIN they never set.
rm -f "$MOUNT"/vector-bridge/admin-pin.json "$MOUNT"/vector-bridge/app-tokens.json
# Bench state must not ship either: the wizard's progress, the transmit ladder's recorded overrides,
# alert settings, logs and last-good copies (0.1.1 and 0.1.2 shipped them all). config.toml stays:
# its gains are the bench-proven values (maintainer, 2026-09-22). presets/ and kits/ stay as they are.
rm -f "$MOUNT"/vector-bridge/wizard.json "$MOUNT"/vector-bridge/ladder-overrides.json "$MOUNT"/vector-bridge/alerts.json
for d in "$MOUNT"/vector-bridge/logs "$MOUNT"/vector-bridge/last-good; do [ -d "$d" ] && find "$d" -mindepth 1 -delete; done
# Bench access (scripts/dev): the tailnet node's keys, both the saved copy and tailscaled's live one
# on this (maintenance mode, real) root, so no owner's card joins the bench tailnet as the same node;
# and the password-free sudo rule for agent deploys.
rm -rf "$MOUNT"/tailscale "$ROOT"/var/lib/tailscale
systemctl disable tailscaled.service >/dev/null 2>&1 || true
rm -f "$ROOT"/etc/sudoers.d/vector-bridge-agent-deploy "$ROOT"/usr/local/sbin/vb-agent-install "$ROOT"/usr/local/sbin/vb-agent-enter
# $MOUNT stays rw until the Wi-Fi profiles below are deleted: NetworkManager keeps them there (P10).
# The golden card's own name and passphrase. /boot/firmware is often still read-only in
# maintenance mode (only `vb-maintenance apply` remounts it), and a silent failure would ship them.
mount -o remount,rw /boot/firmware
rm -f "$ROOT"/boot/firmware/vector-bridge-wifi.txt "$ROOT"/boot/firmware/prepare-golden-refused.txt
# Every installed package with its source package and version, for the GPL source offer in
# THIRD-PARTY-NOTICES.md: it ships on the card and is published next to the image (DOCS/image-build.md).
dpkg-query -W -f='${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n' \
  > "$ROOT"/boot/firmware/vector-bridge-packages.txt
# No Wi-Fi country either (R-D7): the owner picks one on the first-boot page, 2.4 GHz until then.
# Raspberry Pi Imager's OS settings put the bench card's on the kernel command line, next to its
# cloud-init seed pointer (`ds=nocloud;i=rpi-imager-...`).
cmdline="$ROOT"/boot/firmware/cmdline.txt
if [ -f "$cmdline" ]; then
  sed -E -e 's/[[:space:]]*cfg80211\.ieee80211_regdom=[^[:space:]]*//g' \
    -e 's/(^|[[:space:]]+)ds=[^[:space:]]*//g' "$cmdline" > "$cmdline.new"
  mv "$cmdline.new" "$cmdline"
fi
# Imager's cloud-init seed holds the bench login's password hash and SSH key (user-data), and
# cloud-init keeps a copy of it under /var/lib/cloud and in its logs. vb-firstboot does the
# per-card setup, so cloud-init is turned off rather than left to run on a card with no seed.
rm -f "$ROOT"/boot/firmware/user-data "$ROOT"/boot/firmware/meta-data "$ROOT"/boot/firmware/network-config
rm -rf "$ROOT"/var/lib/cloud "$ROOT"/var/log/cloud-init*
if [ -d "$ROOT"/etc/cloud ]; then touch "$ROOT"/etc/cloud/cloud-init.disabled; fi

# SSH off with no way in but the owner's own switch (R-D4, roadmap 1.5): no keys, a locked
# password, ssh.service disabled (not stopped: this session may be SSH). A boot-partition `ssh`
# file would turn it on again. The Wi-Fi page or the console sets a new password with SSH on.
rm -f "$ROOT"/root/.ssh/authorized_keys "$ROOT"/home/*/.ssh/authorized_keys
rm -f "$ROOT"/boot/firmware/ssh "$ROOT"/boot/firmware/ssh.txt
systemctl disable ssh.service >/dev/null
systemctl disable ssh.socket >/dev/null 2>&1 || true   # not on every release
user1000=$(getent passwd 1000 | cut -d: -f1)
home1000=$(getent passwd 1000 | cut -d: -f6)
# `passwd -l` would only prefix the bench hash with "!" and ship it; replace it outright, and drop the
# backups that still hold it. The owner's password arrives later as a whole hash (`chpasswd -e`).
usermod -p '!' "$user1000"
rm -f "$ROOT"/etc/shadow- "$ROOT"/etc/gshadow-
rm -f "$ROOT"/etc/ssh/ssh_host_*
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true
rm -f "$ROOT"/root/.bash_history "$ROOT"/home/*/.bash_history
# Every bench login and where it came from (wtmp.db is wtmpdb's, /var/lib/wtmpdb links to it).
for f in wtmp wtmp.db btmp lastlog; do [ -f "$ROOT"/var/log/$f ] && : > "$ROOT"/var/log/$f; done
# Ships in UTC (maintainer, 2026-10-22). timedatectl leaves /etc/timezone stale (seen on the 695f077 capture).
ln -sfn ../usr/share/zoneinfo/Etc/UTC "$ROOT"/etc/localtime
echo Etc/UTC > "$ROOT"/etc/timezone
# The bench helpers and the checkout this script may be running from (bash keeps it open; the
# product runs from /opt/vector-bridge/src, a copy). The install logs print the golden access
# point's passphrase (695f077 shipped them), and sudo leaves its first-use notice.
case "$home1000" in /home/?*)
  rm -rf "$ROOT$home1000"/vb-enter.sh "$ROOT$home1000"/vb-golden.sh "$ROOT$home1000"/vb-golden.log \
    "$ROOT$home1000"/vector-bridge "$ROOT$home1000"/vb-deploy "$ROOT$home1000"/.ssh \
    "$ROOT$home1000"/*.log "$ROOT$home1000"/.sudo_as_admin_successful ;;
esac

# From here on the terminal may be gone (SSH over the Wi-Fi being deleted). A failed write to
# it must not stop the script under set -e, so every message below is allowed to fail, and a
# refusal is also left on the boot partition where the Mac can read it.
# Every saved Wi-Fi network goes, the golden AP included (first boot makes a new one).
is_wifi='$NF == "802-11-wireless" || $NF == "wifi"'
nmcli -t -f UUID,TYPE connection show | awk -F: "$is_wifi {print \$1}" | while read -r uuid; do
  nmcli connection delete uuid "$uuid" >/dev/null 2>&1 || true
done
# NetworkManager has been seen to leave a deleted profile's keyfile behind (0.1.4): delete those too.
wifi_files() { grep -rlsi 'wifi\|802-11-wireless\|access-points' "$ROOT"/etc/netplan \
  "$ROOT"/etc/NetworkManager/system-connections "$MOUNT"/vector-bridge/networks || true; }
wifi_files | while read -r f; do rm -f "$f" || true; done
mount -o remount,ro "$MOUNT" || true
left=$(nmcli -t -f NAME,TYPE connection show | awk -F: "$is_wifi" || true)
leftfiles=$(wifi_files)
# Raspberry Pi Imager leaves the bench Wi-Fi passphrase in its first-run files on the boot partition.
bootfiles=$(grep -lsi 'ssid\|psk\|wifis\|access-points' "$ROOT"/boot/firmware/custom.toml "$ROOT"/boot/firmware/firstrun.sh \
  "$ROOT"/boot/firmware/network-config "$ROOT"/boot/firmware/user-data || true)
leftfiles="$leftfiles$bootfiles"
if [ -n "$left$leftfiles" ]; then
  msg=$(printf '%s\n' "refused to finish, first boot is NOT armed: saved Wi-Fi remains:" "$left" "$leftfiles" \
    "Remove the listed profiles or files by hand, then run this script again.")
  printf '%s\n' "$msg" > "$ROOT"/boot/firmware/prepare-golden-refused.txt || true
  printf '%s\n' "$msg" >&2 || true
  exit 4
fi
# Daemon state written while the bench ran, removed only now: stopping either daemon earlier would cut an
# SSH session over the tailnet while the messages above still had to reach it. Both rewrite their files on
# the way down, so they are stopped first (the 2.0 capture had a fresh /var/lib/tailscale). NetworkManager's
# are the DHCP leases (bench addresses, and the access point's client MACs), the BSSIDs of every network the
# bench joined, and the secret key behind stable IPv6 addresses, which every card would share; it makes a
# new key at its next start. Masked for this boot only, so an `nmcli` call (the tuner makes them) cannot
# start it again over D-Bus before the power goes.
systemctl stop tailscaled.service >/dev/null 2>&1 || true
systemctl mask --runtime NetworkManager.service >/dev/null 2>&1 || true
systemctl stop NetworkManager.service >/dev/null 2>&1 || true
rm -rf "$ROOT"/var/lib/tailscale
nm="$ROOT"/var/lib/NetworkManager
rm -f "$nm"/*.lease "$nm"/*.leases "$nm"/seen-bssids "$nm"/timestamps "$nm"/secret_key
# Armed last, so a card that failed the scrub above never looks ready to capture.
touch "$ROOT"/etc/vector-bridge-firstboot
sync
echo "golden card prepared; powering off. Capture it before it boots again." || true
poweroff
