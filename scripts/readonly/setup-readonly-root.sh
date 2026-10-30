#!/usr/bin/env bash
# One-time card layout for field use. Run as root on the bench, then reboot.
#   sudo bash scripts/readonly/setup-readonly-root.sh
#
# Result:
#   p1  /boot/firmware  read-only (raspi-config enable_bootro)
#   p2  /               read-only with a RAM overlay (raspi-config enable_overlayfs)
#   p3  /persist        ext4, mounted read-only; remounted rw only while saving
#
# Needs free space after the root partition. Raspberry Pi OS (trixie) grows root to fill
# the card on first boot, so root has to be shrunk in place first: the recipe is in
# DOCS/image-build.md, "Shrinking root". If root fills the card, this script refuses.
# Needs network once: raspi-config enable_overlayfs apt-installs overlayroot.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

ROOT_DEV=$(findmnt -no SOURCE /)                       # /dev/mmcblk0p2 or /dev/sda2
DISK=$(lsblk -no PKNAME "$ROOT_DEV" | head -1); DISK=/dev/$DISK
case "$DISK" in *[0-9]) P=p ;; *) P="" ;; esac           # mmcblk0p3 vs sda3
PART3=${DISK}${P}3
PERSIST_MIN_MB=${PERSIST_MIN_MB:-256}
MOUNT=/persist
SVC_USER=vector-bridge
OLD_CONFIG=/etc/vector-bridge/config.toml

step() { echo "==> $*"; }

if ! findmnt -no SOURCE "$MOUNT" >/dev/null 2>&1; then
  if [ -e "$PART3" ]; then
    step "persist partition $PART3 already exists"
  else
    ROOT_END=$(parted -sm "$DISK" unit MiB print | awk -F: '$1=="2"{print $3}' | sed 's/MiB//'); ROOT_END=${ROOT_END%.*}
    DISK_END=$(parted -sm "$DISK" unit MiB print | awk -F: 'NR==2{print $2}' | sed 's/MiB//'); DISK_END=${DISK_END%.*}
    START=$(( ROOT_END + 1 ))                                # parted prints root's last MiB; start after it
    FREE=$(( DISK_END - START ))
    if [ "$FREE" -lt "$PERSIST_MIN_MB" ]; then
      cat <<EOF
setup-readonly-root: only ${FREE} MiB free after root, need ${PERSIST_MIN_MB}.
Root fills the card. Shrink it in place with the recipe in DOCS/image-build.md
("Shrinking root"), then run this script again.
EOF
      exit 2
    fi
    step "creating $PART3 (${FREE} MiB) for /persist"
    parted -s "$DISK" mkpart primary ext4 "${START}MiB" 100%
    partprobe "$DISK"; sleep 1
    mkfs.ext4 -q -L persist "$PART3"; udevadm settle
  fi
fi

# A mount unit, not fstab: overlayroot wraps every fstab mount in the RAM overlay (recurse=1,
# and raspi-config's cmdline toggle cannot say otherwise), so saves would vanish at power-off.
step "mount unit for $MOUNT"
mkdir -p "$MOUNT"
sed -i '/^LABEL=persist[[:space:]]/d' /etc/fstab
cat > /etc/systemd/system/persist.mount <<EOF
[Unit]
Description=Vector Bridge persistent storage

[Mount]
What=/dev/disk/by-label/persist
Where=$MOUNT
Type=ext4
Options=ro,noatime

[Install]
WantedBy=local-fs.target
EOF
systemctl daemon-reload
systemctl enable persist.mount >/dev/null
findmnt -no SOURCE "$MOUNT" >/dev/null 2>&1 || systemctl start persist.mount

step "laying out $MOUNT/vector-bridge"
mount -o remount,rw "$MOUNT"
APP=$MOUNT/vector-bridge
mkdir -p "$APP"/{presets,kits,last-good,logs}
if [ -f "$OLD_CONFIG" ] && [ ! -f "$APP/config.toml" ]; then
  cp "$OLD_CONFIG" "$APP/config.toml"
fi
chown -R $SVC_USER:audio "$APP"
find "$APP" -type d -exec chmod 2775 {} +
find "$APP" -type f -exec chmod 664 {} +
mount -o remount,ro "$MOUNT"

step "letting the service remount $MOUNT without a password (only that, only rw/ro)"
SUDOERS_TMP=$(mktemp)
cat > "$SUDOERS_TMP" <<EOF
$SVC_USER ALL=(root) NOPASSWD: /usr/bin/mount -o remount\,rw $MOUNT, /usr/bin/mount -o remount\,ro $MOUNT
EOF
visudo -cf "$SUDOERS_TMP" >/dev/null   # validate before it can break sudo
install -m 440 "$SUDOERS_TMP" /etc/sudoers.d/vector-bridge-persist; rm -f "$SUDOERS_TMP"

step "pointing the services at $MOUNT"
for unit in vector-bridge vector-bridge-tuner; do
  mkdir -p /etc/systemd/system/$unit.service.d
  cat > /etc/systemd/system/$unit.service.d/persist.conf <<EOF
[Service]
Environment=VECTOR_BRIDGE_PERSIST=$MOUNT
Environment=VECTOR_BRIDGE_CONFIG=$APP/config.toml
Environment=VECTOR_BRIDGE_PRESETS=$APP/presets
# The tuner unit has ProtectSystem=full; the persist tree must stay writable for saves.
ReadWritePaths=$MOUNT
EOF
done
systemctl daemon-reload

step "journald to RAM (nothing else writes to the card)"
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=16M\n' > /etc/systemd/journald.conf.d/vector-bridge-volatile.conf

step "enabling read-only boot and overlay root"
# enable_bootro appends ",ro" every time it runs, so only call it when fstab lacks it.
[ "$(raspi-config nonint get_bootro_conf)" -eq 0 ] || raspi-config nonint enable_bootro
raspi-config nonint enable_overlayfs

install -m 755 "$(dirname "$0")/vb-maintenance" /usr/local/bin/vb-maintenance
echo "done. Reboot now: sudo reboot. Afterwards 'findmnt /' shows overlay and 'vb-maintenance status' shows field."
