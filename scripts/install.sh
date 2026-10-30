#!/usr/bin/env bash
# VectorBridge installer. Run once as root on the Pi from the repo checkout; idempotent.
#   sudo bash scripts/install.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "install.sh: run as root (sudo)"; exit 1; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/vector-bridge
VENV=$PREFIX/venv
CONFIG_DIR=/etc/vector-bridge
# After scripts/readonly/setup-readonly-root.sh the config lives on the persist partition.
PERSIST=${VECTOR_BRIDGE_PERSIST:-/persist}
if findmnt -no SOURCE "$PERSIST" >/dev/null 2>&1; then
  CONFIG_DIR=$PERSIST/vector-bridge
  mount -o remount,rw "$PERSIST"
  trap 'mount -o remount,ro "$PERSIST" 2>/dev/null || true' EXIT
fi
CONFIG=$CONFIG_DIR/config.toml
SVC_USER=vector-bridge

export DEBIAN_FRONTEND=noninteractive
NEED=()
# python3-dev and gcc are what pyalsaaudio needs to build; rsync is for tools/deploy.sh.
# dnsmasq-base is what NetworkManager runs for DHCP and DNS on the shared (AP) connection.
# avahi-daemon answers vectorbridge.local by mDNS (REQ-NET-008); the base OS is not relied on for it.
for p in python3-venv libasound2-dev alsa-utils python3-dev gcc rsync dnsmasq-base avahi-daemon; do
  dpkg -s "$p" >/dev/null 2>&1 || NEED+=("$p")
done
if [ ${#NEED[@]} -gt 0 ]; then
  apt-get update -qq
  apt-get install -y -qq "${NEED[@]}"
fi
# libhamlib-utils provides rigctld, which the tuner runs as a child process for CAT. It comes
# from trixie-backports (4.7.2): trixie's 4.6.2 has no IC-705 WFM and ignores "FM 0" (bench
# 2026-10-04). apt then follows backports for it on later upgrades.
HAMLIB_MIN=4.7
hamlib_version() { dpkg-query -W -f='${Version}' libhamlib-utils 2>/dev/null || echo 0; }
if grep -q '^VERSION_CODENAME=trixie' /etc/os-release; then
  BPO=/etc/apt/sources.list.d/trixie-backports.sources
  # An enabled line only: a commented-out one or a *.save backup must not stop the source being added.
  if ! grep -rqsE --include='*.list' --include='*.sources' '^[^#]*trixie-backports' /etc/apt/sources.list /etc/apt/sources.list.d/; then
    printf 'Types: deb\nURIs: http://deb.debian.org/debian/\nSuites: trixie-backports\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.pgp\n' > "$BPO"
  fi
  if ! dpkg --compare-versions "$(hamlib_version)" ge "$HAMLIB_MIN"; then
    if ! { apt-get update -qq && apt-get install -y -qq -t trixie-backports libhamlib-utils; }; then
      # A card with no internet (AP only) keeps the Hamlib it has; with none at all, nothing can do CAT.
      dpkg -s libhamlib-utils >/dev/null 2>&1 || { echo "install.sh: could not install libhamlib-utils" >&2; exit 1; }
      echo "install.sh: could not reach trixie-backports, so Hamlib stays at $(hamlib_version); IC-705 WFM needs $HAMLIB_MIN" >&2
    fi
  fi
else
  dpkg -s libhamlib-utils >/dev/null 2>&1 || { apt-get update -qq; apt-get install -y -qq libhamlib-utils; }
  dpkg --compare-versions "$(hamlib_version)" ge "$HAMLIB_MIN" ||
    echo "install.sh: Hamlib $(hamlib_version) from this release; IC-705 WFM needs $HAMLIB_MIN or later" >&2
fi

id -u $SVC_USER >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin $SVC_USER
# dialout: rigctld opens the CAT serial port (/dev/ttyUSB*, root:dialout 0660) as this user.
usermod -aG audio,dialout $SVC_USER
# i2c: the home page reads a PiSugar battery board on /dev/i2c-1 (root:i2c 0660), if one is fitted.
# do_i2c 0 turns the bus on (dtparam=i2c_arm=on plus i2c-dev); the dtparam applies at the next reboot.
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_i2c 0 || true
  # do_i2c leaves the dtparam commented out when /boot/firmware is read-only, and the bus never appears.
  BOOT_CFG=/boot/firmware/config.txt; [ -f "$BOOT_CFG" ] || BOOT_CFG=/boot/config.txt
  grep -qE '^dtparam=([^,]*,)*i2c_arm=on' "$BOOT_CFG" 2>/dev/null || echo "install.sh: I2C is not on in $BOOT_CFG, so no PiSugar battery will show; remount $(dirname "$BOOT_CFG") rw, set dtparam=i2c_arm=on and reboot" >&2
fi
if getent group i2c >/dev/null; then usermod -aG i2c $SVC_USER; fi

mkdir -p $PREFIX
[ -x $VENV/bin/python ] || python3 -m venv $VENV
$VENV/bin/pip install -q --upgrade pip
# Install from a root-owned copy so no root-owned build artefacts land in the user checkout
# (they would break the next rsync --delete from tools/deploy.sh).
rsync -a --delete --exclude .git --exclude .venv --exclude '__pycache__' --exclude '*.egg-info' "$REPO/" $PREFIX/src/
# The footer and `vector-bridge --version` show the commit; "+" marks uncommitted changes.
if commit=$(git -c safe.directory="$REPO" -C "$REPO" rev-parse --short HEAD 2>/dev/null); then
  [ -z "$(git -c safe.directory="$REPO" -C "$REPO" status --porcelain --untracked-files=no 2>/dev/null)" ] || commit="$commit+"
  printf 'COMMIT = "%s"\n' "$commit" > $PREFIX/src/src/vector_bridge/_build.py
fi
$VENV/bin/pip install -q "$PREFIX/src[pi]"
# The on-card docs index lists every DOCS/*.md; DOCS/archive holds developer records.
rm -rf $PREFIX/src/DOCS/archive

mkdir -p $CONFIG_DIR
if [ ! -f $CONFIG ]; then
  cp "$REPO/config/vector-bridge.example.toml" $CONFIG
fi
# The service persists set_gain here, and the CLI (run by an admin user in the
# audio group) writes it too, so the directory and file are group-writable.
chown -R $SVC_USER:audio $CONFIG_DIR
chmod 775 $CONFIG_DIR
chmod 664 $CONFIG
# The saved login password hash (vector_bridge.sshswitch) is root only; the chown -R above undid that.
if [ -f $CONFIG_DIR/ssh.json ]; then chown root:root $CONFIG_DIR/ssh.json; chmod 600 $CONFIG_DIR/ssh.json; fi

# brcmfmac Wi-Fi power save is on by default and produces 20 to 200 ms packet
# bursts (ping stddev 30 ms measured); off, ping stddev is under 3 ms. This is a
# NetworkManager-wide setting, it does not edit any connection profile.
mkdir -p /etc/NetworkManager/conf.d
printf '[connection]\nwifi.powersave = 2\n' > /etc/NetworkManager/conf.d/vector-bridge-wifi-powersave.conf
# reapply is enough: measured ping stddev to the Pi fell from 30 ms to 2.6 ms
# immediately, and it stayed under 2 ms after a reboot.
if nmcli general reload conf 2>/dev/null; then
  for dev in $(nmcli -t -f DEVICE,TYPE device 2>/dev/null | awk -F: '$2 == "wifi" {print $1}'); do
    nmcli device reapply "$dev" >/dev/null 2>&1 || true
  done
fi

# ---- Wi-Fi profiles on the persist partition (P10) ---------------------------------
# NetworkManager keeps its keyfiles under $PERSIST/vector-bridge/networks, so a name, passphrase
# or joined-network change never needs the writable root: the callers (manage._ap_create,
# apsetup, wifijoin) remount $PERSIST rw for the seconds NetworkManager writes and put it back.
# Root only: the passphrases live here. Nothing to do on a Pi without the partition.
if [ "$CONFIG_DIR" != /etc/vector-bridge ]; then
  NETWORKS=$CONFIG_DIR/networks
  mkdir -p "$NETWORKS"; chown root:root "$NETWORKS"; chmod 700 "$NETWORKS"
  # The chown -R of $CONFIG_DIR above also hands any keyfiles already here to $SVC_USER, and
  # NetworkManager refuses a keyfile not owned by root ("File owner (999) is insecure"): after
  # a re-install the AP and every remembered network are gone at the next boot (2026-09-30).
  find "$NETWORKS" -name '*.nmconnection' -exec chown root:root {} + -exec chmod 600 {} +
  KEYFILE_CONF=/etc/NetworkManager/conf.d/vector-bridge-keyfile.conf
  NM_DROPIN=/etc/systemd/system/NetworkManager.service.d/persist.conf
  changed=0
  if [ "$(cat "$KEYFILE_CONF" 2>/dev/null)" != "$(printf '[keyfile]\npath=%s' "$NETWORKS")" ]; then
    printf '[keyfile]\npath=%s\n' "$NETWORKS" > "$KEYFILE_CONF"; changed=1
  fi
  # The keyfile directory must be mounted before NetworkManager reads it. Wants, not Requires: a
  # broken /persist must not keep NetworkManager (and any way in) down.
  # Debian's unit sandboxes NetworkManager (ProtectSystem, ProtectHome, ReadWritePaths), which
  # gives it a private mount namespace: a `remount,rw` of $PERSIST from outside never reaches
  # its copy of the mount and every keyfile write fails with EROFS (seen 2026-09-22). The
  # callers remount from the host, so NetworkManager must share the host namespace.
  mkdir -p "$(dirname "$NM_DROPIN")"
  printf '[Unit]\nWants=persist.mount\nAfter=persist.mount\n\n[Service]\nProtectSystem=no\nProtectHome=no\nReadWritePaths=\n' > "$NM_DROPIN"
  systemctl daemon-reload
  # Profiles written before this change move over once; NetworkManager reads only the new path.
  for f in /etc/NetworkManager/system-connections/*.nmconnection; do
    [ -e "$f" ] || continue
    mv "$f" "$NETWORKS"/; changed=1
  done
  # `nmcli general reload` does not take a new keyfile path, so restart. NetworkManager re-reads
  # the moved profiles and lets the AP (autoconnect priority 100) win, which drops a bench
  # session on another network (seen 2026-09-22): put the connection that was up back up. The
  # session still pauses for a few seconds, so this script must not die on SIGHUP.
  if [ "$changed" = 1 ]; then
    active=$(nmcli -t -f GENERAL.CONNECTION device show wlan0 2>/dev/null | cut -d: -f2- || true)
    trap '' HUP
    systemctl restart NetworkManager
    sleep 5
    if [ -n "$active" ] && [ "$(nmcli -t -f GENERAL.CONNECTION device show wlan0 2>/dev/null | cut -d: -f2-)" != "$active" ]; then
      nmcli connection up "$active" >/dev/null 2>&1 || echo "install.sh: could not put $active back up" >&2
    fi
  fi
fi

# The login account ships in the image (maintainer, 2026-09-21) so an owner can SSH in with its password.
# Raspberry Pi Imager turns password login off when it installs a key; sshd keeps the FIRST value
# it reads, so this file sorts before Imager's in sshd_config.d.
mkdir -p /etc/ssh/sshd_config.d
printf 'PasswordAuthentication yes\n' > /etc/ssh/sshd_config.d/00-vector-bridge.conf
systemctl reload ssh 2>/dev/null || true

# ---- field access point and captive portal ---------------------------------------
# Memorable hostname: http://vectorbridge/ on the AP (captive DNS), vectorbridge.local via mDNS.
AP_HOST=vectorbridge
if [ "$(hostname)" != "$AP_HOST" ]; then
  hostnamectl set-hostname "$AP_HOST" 2>/dev/null || echo "$AP_HOST" > /etc/hostname
  sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$AP_HOST/" /etc/hosts
  grep -q "^127\.0\.1\.1" /etc/hosts || printf '127.0.1.1\t%s\n' "$AP_HOST" >> /etc/hosts
fi
# Every name resolves to the Pi on the AP: this is the captive portal's DNS half.
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
install -m 644 "$REPO/scripts/ap/dnsmasq-shared.conf" /etc/NetworkManager/dnsmasq-shared.d/vector-bridge.conf
# The AP is the boot default. `ensure` only writes the profile (an existing one keeps
# its passphrase) and does not activate it, so an SSH session over bench Wi-Fi survives
# the install; the AP is up at the next reboot. The passphrase lives only in
# NetworkManager (root-only); read it back with `sudo vector-bridge ap show`.
# `ensure` brackets its own rw window on $PERSIST, so this script lets go of it first.
[ "$CONFIG_DIR" = /etc/vector-bridge ] || mount -o remount,ro "$PERSIST"
$VENV/bin/vector-bridge ap ensure || true

install -m 644 "$REPO/scripts/vector-bridge.service" /etc/systemd/system/vector-bridge.service
install -m 644 "$REPO/scripts/vector-bridge-tuner.service" /etc/systemd/system/vector-bridge-tuner.service
install -m 755 "$REPO/scripts/readonly/vb-maintenance" /usr/local/bin/vb-maintenance
# First boot of a burned image; inert until scripts/image/prepare-golden.sh arms it.
install -m 755 "$REPO/scripts/image/vb-firstboot" /usr/local/sbin/vb-firstboot
install -m 644 "$REPO/scripts/image/vb-firstboot.service" /etc/systemd/system/vb-firstboot.service
# HDMI console: a menu (reset the admin PIN, show the Wi-Fi, command line) in place of the login prompt,
# from the next boot (restarting getty now would cut off an install run at the console).
install -m 755 "$REPO/scripts/console/vb-console" /usr/local/sbin/vb-console
install -D -m 644 "$REPO/scripts/console/vb-console.conf" /etc/systemd/system/getty@tty1.service.d/vb-console.conf
# Root side of the Wi-Fi page: applies a name and passphrase change the web UI left on /persist.
install -m 644 "$REPO/scripts/image/vb-ap-apply.path" /etc/systemd/system/vb-ap-apply.path
install -m 644 "$REPO/scripts/image/vb-ap-apply.service" /etc/systemd/system/vb-ap-apply.service
# Root side of "join another network" on the same page.
install -m 644 "$REPO/scripts/image/vb-wifi-join.path" /etc/systemd/system/vb-wifi-join.path
install -m 644 "$REPO/scripts/image/vb-wifi-join.service" /etc/systemd/system/vb-wifi-join.service
# At boot, move from the access point to the home network if one is marked and in range.
install -m 644 "$REPO/scripts/image/vb-wifi-home.service" /etc/systemd/system/vb-wifi-home.service
# SSH on or off from the Wi-Fi page or the console (R-D4); the boot unit puts the setting back
# because field mode's overlay forgets the password and the started ssh.service.
install -m 644 "$REPO/scripts/image/vb-ssh-apply.path" /etc/systemd/system/vb-ssh-apply.path
install -m 644 "$REPO/scripts/image/vb-ssh-apply.service" /etc/systemd/system/vb-ssh-apply.service
install -m 644 "$REPO/scripts/image/vb-ssh-boot.service" /etc/systemd/system/vb-ssh-boot.service
# A USB Wi-Fi adapter in any port takes over the bridge's network; unplugged, the built-in radio does.
install -m 644 "$REPO/scripts/image/vb-wifi-radio.service" /etc/systemd/system/vb-wifi-radio.service
# Status light: the rule runs again at every boot; the trigger applies it now.
install -m 644 "$REPO/scripts/udev/99-vector-bridge-led.rules" /etc/udev/rules.d/99-vector-bridge-led.rules
udevadm control --reload && udevadm trigger --action=change /sys/class/leds/ACT || true
ln -sfn $VENV/bin/vector-bridge /usr/local/bin/vector-bridge
systemctl daemon-reload
systemctl enable vb-firstboot >/dev/null 2>&1
systemctl enable --now vb-ap-apply.path >/dev/null 2>&1
systemctl enable --now vb-wifi-join.path >/dev/null 2>&1
systemctl enable vb-wifi-home.service >/dev/null 2>&1
systemctl enable --now vb-ssh-apply.path >/dev/null 2>&1
systemctl enable vb-ssh-boot.service >/dev/null 2>&1
# Not --now: moving the network mid-install would drop a Wi-Fi SSH session. It starts at the next boot.
systemctl enable vb-wifi-radio.service >/dev/null 2>&1
systemctl enable --now avahi-daemon.service >/dev/null 2>&1
# A field box is offline and read-only, so the daily apt jobs and the man-db index only
# compete with audio capture: apt-daily-upgrade overlapped a 134 ms capture stall and an
# RX overrun on 2026-10-02. Masking stops the timers; apt by hand still works.
systemctl mask --now apt-daily.timer apt-daily-upgrade.timer man-db.timer >/dev/null 2>&1 || true
for unit in vector-bridge vector-bridge-tuner; do
  systemctl enable $unit >/dev/null 2>&1
  # restart, not start: a changed unit file (Environment= lines) must take on a running Pi.
  systemctl restart $unit || true
done
echo "installed: service=$(systemctl is-active vector-bridge) tuner=$(systemctl is-active vector-bridge-tuner) venv=$VENV config=$CONFIG"
echo "tuner: http://$(hostname -I | awk '{print $1}')/  (the first save asks for an admin PIN; reset it with sudo vector-bridge pin reset)"
echo "field: after reboot join the VectorBridge-xxxx Wi-Fi (sudo vector-bridge ap show prints name and passphrase); portal opens http://$AP_HOST/"
