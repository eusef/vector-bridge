# Building the release image

How a working bench card becomes a `.img.xz` that anyone can write to a card with a raw copy. No pi-gen,
no CI: one golden card, captured with `dd`, shrunk with PiShrink.

Base (read from interim-fb72fbf, 2026-10-24): Raspberry Pi OS Lite, 64-bit only (every package
`arm64` or `all`), Debian 13.7 trixie, `/etc/rpi-issue` "Raspberry Pi reference 2026-10-06" (pi-gen
stage2, the Lite build), kernel 6.18.50. Start a new golden card from the same "Raspberry Pi OS Lite
(64-bit)", fresh, as in "Golden card from a fresh base" below. The image is built and tested on the Raspberry Pi Zero 2 W only. Imager lists Zero 2 W images under its
`pi3-64bit` tag, so they also show to Pi 3 owners; the listing description says "Pi Zero 2 W only".

## How each burned card becomes its own bridge

The AP name and passphrase live only in NetworkManager. Since P10 its profiles sit on `/persist`; the
root in field mode is a RAM overlay, so anything written there is gone at the next reboot, and the
host keys and machine-id must still differ per card. The image is therefore captured in **maintenance mode** (overlay off) with `vb-firstboot` armed.

First boot of a burned card (`vb-firstboot`, once, root still writable):

1. deletes and regenerates the SSH host keys and `/etc/machine-id`
2. grows the `persist` partition to the end of the card
3. makes an **open** access point `VectorBridge-XXXX` (XXXX from the Pi's serial), writes the
   marker `/persist/vector-bridge/ap-unsecured`, and writes `vector-bridge-wifi.txt` to the boot
   partition saying the network is open

While the marker exists the web UI serves only the Wi-Fi page (every other page redirects to
`/wifi`, every other POST is refused) and `Ladder.key` refuses to key the transmitter, so a
stranger who joins the open network can do nothing but name it.

The owner joins, the captive portal opens `/wifi`, they choose a name (always prefixed
`VectorBridge-`) and a passphrase. The web process has no root (its only sudo is the `/persist` remount), so it writes
`/persist/vector-bridge/ap-pending.json`. `vb-ap-apply.path` sees the file and runs
`python -m vector_bridge.apsetup apply` as root, which validates the request again, then:

- root writable (first boot): writes the WPA2 profile, rewrites `vector-bridge-wifi.txt` with the
  name and passphrase, puts the secured profile on the air, enables read-only boot and the
  overlay for the next boot, and only then removes the marker and the request and reboots into
  field mode. One reboot. A power cut before the request is removed repeats the change; after
  it, the card boots into field mode. If the text file cannot be written the card still locks
  down (the radio is right, the copy on the card is stale, the journal says so). If the lockdown
  itself fails the request is dropped and the card stays writable. The radio is secured by then
  but the marker is still there, so the UI stays on `/wifi` and transmit stays refused until the
  owner saves again, which retries the lockdown.
- root is the overlay (a later change from the same `/wifi` page): writes the profile and the
  text file, puts the new name on the air, removes the marker and the request. No reboot, and
  the root is never made writable (P10).

### Wi-Fi profiles on `/persist` (P10)

`install.sh` points NetworkManager's keyfile directory at `/persist/vector-bridge/networks`
(`[keyfile] path=` in `/etc/NetworkManager/conf.d/vector-bridge-keyfile.conf`, root-only
directory, and a `NetworkManager.service` drop-in with `Wants=persist.mount`, `After=persist.mount`, and
`ProtectSystem=no`, `ProtectHome=no`, `ReadWritePaths=` so NetworkManager shares the host mount namespace:
sandboxed, it keeps its own read-only copy of the `/persist` mount and every keyfile write fails with
EROFS however the host remounts). Every
profile write (`manage._ap_create`, `apsetup`, `wifijoin`, the bench `wifi join`) runs inside
`persist.writable(persist.NETWORKS_DIR)`: `/persist` is rw for those seconds and NetworkManager
writes each keyfile through a temp file and rename, so a power cut leaves the old profile or the
new one. `nmcli connection up` writes nothing. Profiles that were under
`/etc/NetworkManager/system-connections` before the change are moved over once by `install.sh`
and NetworkManager is restarted (the active connection stays up).

A request must not survive a failed attempt, because the path unit would fire on it for ever: a
request that fails validation is dropped, and if NetworkManager refuses the new profile the
previous one is restored (open again on a first boot, the old name and passphrase otherwise) and
put back on the air. If the request cannot be removed at all (`/persist` will not remount), the
helper stops `vb-ap-apply.path`: no more Wi-Fi changes until a reboot. Securing the bridge also
removes the first-boot flag, so a first boot that failed half way cannot run again and reopen it.
The gate does not rest on the marker alone: an AP profile with no key management in
NetworkManager also counts as open, so a `/persist` that fails to mount cannot open the UI.

`vector-bridge-wifi.txt` on the boot partition (FAT, readable in any computer) always holds the
current name and passphrase, unless the write failed (see above; the journal says so).

### Joining another network (P9)

The same page's "Join another network" section uses the same shape: the web process writes
`/persist/vector-bridge/wifi-join.json`, `vb-wifi-join.path` runs `python -m vector_bridge.wifijoin
apply` as root, and every way out removes the request or reboots. Three requests:

- `join` (new network): `nmcli --ask device wifi connect` with the passphrase on stdin and
  autoconnect off on the new profile, both inside the `/persist` rw window, then stay joined.
  No reboot; the root stays the overlay (P10). A refused passphrase deletes the half profile and
  puts the AP back.
- `up` (remembered network) and `ap`: `nmcli connection up`; a failed `up` falls back to the AP.

The outcome is written to `/persist/vector-bridge/wifi-join-result.json` (0644) and shown on the
page. Both `/api/join` routes are refused while the AP is open. Design in
`src/vector_bridge/wifijoin.py`.

Accepted risks (maintainer, 2026-09-21): the first passphrase is typed over an open, unencrypted
network. SSH is off on the image and the login password is locked (R-D4, roadmap 1.5): the owner
turns SSH on from the Wi-Fi page (behind the admin PIN) or the HDMI console, and either one sets a new
password. Until then the console's command line has no password for `sudo`.

## Steps

1. Golden card in maintenance mode, current code installed with `scripts/install.sh`, everything
   verified: "Golden card from a fresh base" below. On an AP-only Pi use the by-hand enter steps
   (`DOCS/archive/HANDOFF.md`).
2. On the Pi: `sudo bash scripts/image/prepare-golden.sh`. It removes the host keys, every
   `authorized_keys`, shell history, journal, the maintenance flag, the bench helpers (`~/vb-enter.sh`,
   `~/vb-golden.sh`, `~/vb-golden.log`, `~/vb-deploy`, `~/.ssh`) and the `~/vector-bridge` checkout, the
   bench access from `scripts/dev` (the Tailscale node state on `/persist/tailscale`, with `tailscaled`
   disabled, and its live copy under `/var/lib/tailscale`, removed again at the end once `tailscaled` is
   stopped, because it rewrites it on the way down; the agent-deploy sudo rule), NetworkManager's
   leases, seen BSSIDs, timestamps and secret key (also at the end, after stopping it), the bench state on
   `/persist/vector-bridge` (wizard, ladder overrides, alerts, Wi-Fi join files, SSH files, admin PIN,
   app tokens, the Wi-Fi country, logs, last-good; `config.toml`, presets and kits stay), the Wi-Fi country
   on the kernel command line (`cfg80211.ieee80211_regdom=`, which Raspberry Pi Imager's OS settings write;
   R-D7), Raspberry Pi Imager's cloud-init seed (`user-data`, `meta-data` and `network-config` on the boot
   partition, which hold the bench login's password hash and SSH key; the `ds=` pointer on the command
   line; `/var/lib/cloud` and cloud-init's logs) with cloud-init turned off (`/etc/cloud/cloud-init.disabled`;
   `vb-firstboot` does the per-card setup), and **every saved Wi-Fi network**,
   including keyfiles NetworkManager left behind (seen on the 0.1.4 loop). It disables SSH (it does not
   stop it, so an SSH session survives that step) and replaces the login password's hash with `!` (locked,
   with no bench hash left in `/etc/shadow` or its `shadow-` backup), empties the login records (`wtmp`,
   `wtmp.db`, `btmp`, `lastlog`), removes `~/*.log` (the install logs print the golden access point's
   passphrase) and sudo's first-use notice, sets the time zone to UTC, arms first boot and
   powers off. It refuses to finish if a Wi-Fi profile or file is still left, or Raspberry Pi Imager's
   first-run files on the boot partition hold a Wi-Fi passphrase.
   An SSH session over Wi-Fi drops part way; the script carries on. Do not boot the card again.
3. Card in the Mac's reader. Copy the package list `prepare-golden.sh` wrote (it is published next to
   the image, for the source offer in `THIRD-PARTY-NOTICES.md`), find the card with `diskutil list`, then:

       cp /Volumes/bootfs/vector-bridge-packages.txt vector-bridge-VERSION-packages.txt
       diskutil unmountDisk /dev/diskN
       sudo dd if=/dev/rdiskN of=vector-bridge-golden.img bs=4m status=progress

4. PiShrink needs loop devices and `resize2fs`, so it runs in a privileged Docker container on the
   Mac (proven for 0.1.2 and 0.1.3; no second Pi or VM needed). The script is vendored at
   `scripts/image/pishrink.sh` (Drewsif/PiShrink v26.03.16, MIT), so nothing is downloaded. From `release/`:

       docker run --rm --privileged -v "$PWD":/work \
         -v "$PWD/../scripts/image/pishrink.sh":/usr/local/bin/pishrink.sh:ro -w /work \
         debian:stable-slim bash -c 'apt-get update -qq >/dev/null && \
           apt-get install -y -qq parted e2fsprogs util-linux >/dev/null && \
           pishrink.sh -s vector-bridge-golden.img vector-bridge-VERSION.img'
       xz -T0 -9 -k vector-bridge-VERSION.img
       shasum -a 256 vector-bridge-VERSION.img.xz > vector-bridge-VERSION.img.xz.sha256

   About a minute for the shrink (29G to 8.8G); `udevadm: not found` lines are harmless in the
   container. `-s` skips PiShrink's own auto-expand: it assumes the last partition is root, and here the
   last partition is `persist`, which `vb-firstboot` grows instead. PiShrink shrinks only that
   last partition; root stays at its 8 GiB.
   Keep the source behind the offer in `THIRD-PARTY-NOTICES.md`: for every source package and version in
   `vector-bridge-VERSION-packages.txt`, download it from snapshot.debian.org (Debian's packages) or
   archive.raspberrypi.com (Raspberry Pi's own, the kernel included), and keep that folder with the
   release for at least three years. Not scripted yet.
5. Write the `.img.xz` to a second card with Raspberry Pi Imager as the README's "End user" section says
   (Use Custom, Skip customisation; worked for interim-fb72fbf), then run "Acceptance" below.
   balenaEtcher failed twice on the Mac (2026-09-22: a zeroed `bootfs`, then an error opening the `.xz`).
   Without Imager: decompress with `xz -dk`, unmount the card with `diskutil unmountDisk`, and write the
   `.img` to the raw disk with the same copy command as step 3 with `if` and `of` swapped.
6. The golden card is now an armed, keyless card. To use it again, boot it: it goes through first
   boot like any other.
7. Publish (ROADMAP 4.1 and 4.2), on the public repository per `PUBLISHING.md`, after the export commit
   for this version is pushed there. `scripts/image/os-list.sh VERSION` writes `imager/os_list.json`
   (the sizes and checksums Raspberry Pi Imager verifies, `init_format: "none"`, `pi3-64bit`, and the
   fixed release download links); commit it to the dev repository with the version. Then, from `release/`:

       gh release create vVERSION --repo eusef/vector-bridge --prerelease \
         --title "VectorBridge VERSION" --notes-file <notes> \
         vector-bridge-VERSION.img.xz vector-bridge-VERSION.img.xz.sha256 \
         vector-bridge-VERSION-packages.txt ../imager/os_list.json ../imager/vector-bridge-icon.png

   Check it from the Mac (ROADMAP 4.3): `"/Applications/Raspberry Pi Imager.app/Contents/MacOS/rpi-imager"
   --repo https://github.com/eusef/vector-bridge/releases/download/vVERSION/os_list.json` lists
   VectorBridge with the image size; write a card from it and run "Acceptance" below.

### Rebuilding from an existing golden card

When `main` moves and the base has not changed, reuse the last golden card instead of "Golden card from
a fresh base": boot it (it runs first boot, so set a network name, passphrase and country from the phone
as an end user would, then turn SSH on from `/wifi`), join home Wi-Fi from `/wifi`, copy the new commit
to `~/vector-bridge` and run `sudo vb-maintenance enter`, `sudo bash scripts/install.sh`, check, then
step 2 above.

## Golden card from a fresh base

How the interim-fb72fbf golden card was made (2026-10-23), from
`2026-10-06-raspios-trixie-arm64-lite.img.xz`:

1. Prepare the base on the Mac, in a privileged Docker container (no Pi needed): grow the image so root
   (p2) is 8 GiB and remove ` resize` from `cmdline.txt`. That one word drives both the initramfs
   `resize_early` (root would grow to fill the card, leaving no room for `persist`) and `set_partuuid`,
   so without it root stays at 8 GiB and the disk ID stays the stock one. This replaces "Shrinking root"
   below. The offsets are the 2026-10-06 image's (`parted -sm base.img unit B print`); from the folder
   holding the decompressed `base.img`:

       docker run --rm --privileged -v "$PWD":/base -w /base debian:stable-slim bash -c '
         set -e; apt-get update -qq >/dev/null; apt-get install -y -qq parted e2fsprogs >/dev/null
         cp base.img golden-base.img
         B2=545259520; END=$((B2 + 8589934592))
         truncate -s $END golden-base.img
         parted -s golden-base.img unit B resizepart 2 $((END - 1))B
         L=$(losetup -f --show -o $B2 --sizelimit 8589934592 golden-base.img)
         e2fsck -fy $L || [ $? -le 1 ]; resize2fs $L; losetup -d $L
         mkdir /mb; mount -o loop,offset=8388608,sizelimit=536870912 golden-base.img /mb
         sed -i "s/ resize\b//" /mb/cmdline.txt; umount /mb'

2. Raspberry Pi Imager 2.0.12 greys out OS customisation for a custom image, so borrow it from the stock
   one: write the stock Lite image with customisation (user `vectorbridge`, home Wi-Fi, SSH, country),
   copy `user-data`, `network-config` and `meta-data` off its boot partition (they hold a password hash
   and the Wi-Fi passphrase: keep them private and delete them after acceptance), write
   `golden-base.img`, copy the three files back, and add Imager's `cmdline.txt` additions
   (`cfg80211.ieee80211_regdom=XX ds=nocloud;i=rpi-imager-NNN`) without ` resize`.
3. Boot on home Wi-Fi and copy the commit to `~/vector-bridge` (`git archive <commit>` over SSH). Run
   `sudo bash scripts/install.sh`, then `sudo bash scripts/readonly/setup-readonly-root.sh`, then
   `install.sh` **again**: the first run found no `/persist`, so NetworkManager's keyfiles stayed on root.
4. Write `/persist/vector-bridge/home-network.json` as `{"ssid": "<home profile>"}`, with the name
   `nmcli connection show` lists for home Wi-Fi, so the field boot rejoins it and SSH still reaches the
   card (`/persist` is read-only: `sudo mount -o remount,rw /persist` first, `ro` after).
   `sudo reboot`; `vb-maintenance status` says `mode: field`.
5. `sudo vb-maintenance enter`, check everything, then step 2 above (`prepare-golden.sh`).

## Acceptance (bring-up P8)

Last run: 0.2.1, written with Raspberry Pi Imager 2.0.12 from the local `.img.xz` (Use Custom), 2026-10-29,
steps 1 to 8 pass on a Zero 2 W (step 8: SSH still starts after the reboot), port 4532 answers `RPRT -9` to a
client with no token, and the served `DOCS/` carry no tool names; the hosted `os_list` was checked in Imager
afterwards (the Device page is empty with this list, pick OS directly). Before that: 0.2.0, written from the
hosted `os_list` (`--repo`), 2026-10-28, steps 1 to 8 pass on a Zero 2 W; the two HDMI console checks in step 8 (console `sudo`, menu item 4)
were not run. The step 5 channel check was read from the NetworkManager journal after the step 7 power
pull (the radio was on the home network when step 5 ran). Before that: interim-fb72fbf, 2026-10-23 and
24, steps 1 to 8 on a Zero 2 W, and step 6 plus a reboot on a second Zero 2 W (channel 6 with an `ssid`
line in both). Before that, interim-695f077 failed step 6: NetworkManager's automatic channel put the access point on 13, off
the air once the country was set to US (fixed in PR #82, channel 6).

On the second card, in order:

1. Pull the card after the first boot: `vector-bridge-wifi.txt` names the open network.
2. Phone joins the open network; the captive sheet shows the Wi-Fi page and nothing else
   (`/`, `/tuner`, `/transmit` all land on `/wifi`).
3. The Wi-Fi country list reads "Not set (2.4 GHz only)" (R-D7) and the name box is empty. Saving the
   open network's own name is refused (phones that joined it never rejoin it with a passphrase, seen
   on the 2.0 card). Save a new name, a passphrase and a country. The bridge reboots once. Leave SSH
   off for now: step 5 first checks that it is refused.
4. Phone joins the new name with the new passphrase; the home screen and the wizard open.
5. SSH is off (R-D4): `ssh vectorbridge@vectorbridge.local` is refused. Turn it on from `/wifi` (SSH
   login) with a new password, then over SSH (or at the console's command line, whose `sudo` takes the
   same password), compared with the golden card where marked:

       findmnt -no FSTYPE /                       # overlay
       cat /etc/machine-id                        # differs from golden
       for k in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf $k; done    # differ from golden
       nmcli -t -f NAME,TYPE connection show      # only vector-bridge-ap is Wi-Fi
       sudo vector-bridge ap show                 # the chosen name and passphrase
       cat /boot/firmware/vector-bridge-wifi.txt  # the same two values
       /usr/sbin/iw reg get | sed -n 2p           # country <the one chosen in step 3>
       /usr/sbin/iw dev wlan0 info                # AP on the built-in radio: channel 6 (2437 MHz), an ssid line
       grep -c regdom /boot/firmware/cmdline.txt  # 0
       grep -c 'ds=' /boot/firmware/cmdline.txt   # 0
       ls /boot/firmware/{user-data,meta-data,network-config}   # none of the three exists
       ls /etc/cloud/cloud-init.disabled /var/lib/cloud   # the first exists, the second does not
       ls /var/log/cloud-init*                    # no such file
       ls /persist/vector-bridge/                 # no ap-* marker or request, no wizard.json,
                                                  # ladder-overrides.json, alerts.json, wifi-join*.json, home-network.json,
                                                  # admin-pin.json or app-tokens.json (ssh.json: written by step 5)
       ls ~                                       # no vb-*.sh, vb-golden.log or vector-bridge checkout
       curl -s -o /dev/null -w '%{http_code}\n' http://localhost/wifi-qr.svg   # 200 (P9)
       df -h /persist                             # grown to the card

6. Change the name again from `/wifi` in field mode (P10): no reboot, the new name is on the air
   within seconds (if the bridge was joined to another network, applying the profile puts the access
   point back on the air and that network is dropped), new values in both places, `findmnt -no FSTYPE /` is still `overlay`,
   `findmnt -no OPTIONS /persist` starts with `ro`, and the profile is under
   `/persist/vector-bridge/networks/`.
7. Join a home network from `/wifi` (P9, P10): no reboot, `http://vectorbridge.local/wifi` on that
   network shows "Joined <name>", root is still the overlay; Back to the access point returns in
   seconds. Then pull the power while joined: the access point is back, root is the overlay, the
   joined profile is kept with `autoconnect no` and `up` rejoins it.
8. SSH across reboots (field mode forgets `/etc/shadow`; `vb-ssh-boot` puts it back): reboot, SSH still
   logs in with the step 5 password. Turn it off from `/wifi` and reboot: SSH is refused, and the
   console's `sudo` still takes the password. Console menu item 4 turns it on again with a new one.

## Shrinking root

Raspberry Pi OS (trixie) grows root to fill the card on first boot (`rpi-resize.service`,
INFERRED), which leaves no room for `persist`. Proven on the bench 2026-09-20 (bring-up log, P2);
the script below is reconstructed from that log entry and has not been re-run as written.

A one-shot initramfs `local-premount` script, because root cannot be shrunk while mounted:

    e2fsck -fy $DEV; rc=$?
    [ $rc -le 1 ] && resize2fs -f $DEV 8G

Plain `resize2fs` refused with "Please run e2fsck -f first" straight after a clean fsck (INFERRED:
no clock in the initramfs), hence `-f`, gated on the fsck result. Then, booted normally:

    echo ",16793600" | sudo sfdisk --no-reread --force -N 2 /dev/mmcblk0
    sudo partx -u /dev/mmcblk0
    sudo resize2fs /dev/mmcblk0p2

Remove the hook and rebuild the initramfs. Result on a 32 GB card: p2 8200 MiB, about 20 GiB free
for `persist`.
