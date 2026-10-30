# SD card layout for field use

The card is read-only in normal operation, so pulling the power never corrupts it.

| Partition | Mount | Mode | Contents |
|---|---|---|---|
| p1 | /boot/firmware | read-only | kernel, firmware, cmdline.txt |
| p2 | / | read-only with RAM overlay | OS, /opt/vector-bridge, documentation |
| p3 | /persist | read-only, briefly rw during a save | config, presets, kits, Wi-Fi profiles, bridge state |

Anything written to / lands in RAM and is gone at reboot. That is intended. The only
things that survive are under `/persist/vector-bridge`:

| Path | Written by | Contents |
|---|---|---|
| `config.toml` | audio service, CLI | gains, ports, selected sound card |
| `presets/` | `vb-maintenance apply` | user radio profiles, overriding built-ins with the same name |
| `kits/` | setup wizard | named kits |
| `wizard.json` | setup wizard | where the wizard is, so it resumes after a power cut |
| `ladder-overrides.json` | transmit checks | which checks were skipped |
| `alerts.json` | Alerts page | which alert goes to which channel |
| `networks/` | NetworkManager (root only, 0700) | access point and remembered Wi-Fi profiles (P10) |
| `ap-unsecured` | `vb-firstboot` | marker: the access point is still open (first boot) |
| `ap-pending.json` | Wi-Fi page | a name and passphrase change waiting for `vb-ap-apply` |
| `wifi-join.json`, `wifi-join-result.json` | Wi-Fi page, `vb-wifi-join` | a join request and its outcome |
| `home-network.json` | Wi-Fi page, read by `vb-wifi-home` at boot | the remembered network rejoined at boot when in range |
| `wifi-country.json` | Wi-Fi page and first-boot sheet, read by `vb-wifi-radio` | the Wi-Fi country (R-D7), set in the kernel with `iw reg set` at every start; none: 2.4 GHz only |
| `ssh-request.json`, `ssh-result.json` | Wi-Fi page, `vb-ssh-apply` | an SSH on (with the new password's hash) or off request, and its outcome |
| `ssh.json` | `vb-ssh-apply` or the console (root only, 0600), read by `vb-ssh-boot` at boot | SSH on or off and the login password's hash: the overlay forgets both at every reboot |
| `maintenance` | `vb-maintenance` | flag: the card is in maintenance mode |
| `last-good/`, `logs/` | nothing yet | created by `setup-readonly-root.sh`, reserved |

The boot partition also holds `vector-bridge-wifi.txt` with the current access point name
and passphrase, written by `vb-firstboot` and `vb-ap-apply` with a brief rw remount of
`/boot/firmware`.

## How saves work

Every save goes through `vector_bridge.persist.save`: remount `/persist` writable, write
a temp file next to the target, fsync, rename over the target, fsync the directory,
remount read-only. A power cut at any instant leaves either the old file or the new
one. The test `tests/test_persist.py` interrupts a preset save before the rename and
checks the old preset is still readable.

The audio service user may run exactly two commands through sudo without a password:
remount `/persist` rw and remount it ro. The web server runs as the same user and uses the
same two rules; it holds no capability but `CAP_NET_BIND_SERVICE` for port 80 (roadmap 1.4
dropped the `CAP_SYS_ADMIN` it once held for this).
Wi-Fi profile writes happen in root helpers (`vb-ap-apply`, `vb-wifi-join`, the installer)
inside the same rw window.

## Provisioning a card

End users burn the release image instead: `DOCS/image-build.md`. From scratch, on the bench:

1. Flash Raspberry Pi OS Lite with Raspberry Pi Imager.
2. Boot. Raspberry Pi OS (trixie) grows root to fill the card, which leaves no room for
   `/persist`, so shrink root in place: `image-build`, "Shrinking root".
   The older advice to delete `init_resize.sh` from `cmdline.txt` does not apply to trixie.
3. Clone the repo, run `sudo bash scripts/install.sh`.
4. Run `sudo bash scripts/readonly/setup-readonly-root.sh`, then `sudo bash scripts/install.sh` again
   (the first run found no `/persist`, so it left NetworkManager's keyfiles on root), then `sudo reboot`.
5. Check: `findmnt /` shows `overlay`, and `vb-maintenance status` says `mode: field`.

## Maintenance mode

Updates and preset installs happen only through `vb-maintenance`, on the bench:

```
vb-maintenance status         # field or maintenance, plus mount state
vb-maintenance enter          # refuses if a phone is on the AP or a client is streaming; reboots rw
vb-maintenance apply [dir]    # runs the installer; copies *.toml from dir into presets
vb-maintenance exit           # verifies the install, re-enables read-only, reboots
```

It is refused during field operation on purpose. A bridge with a client connected is
never half-updated.

Known gaps: on a Pi that is only reachable through its own access point, the machine doing
the work is itself an AP station, so `enter` always refuses; bring-up ran its steps by hand.
`apply` runs `install.sh`, which needs internet for apt and pip: join a network from the
Wi-Fi page first. Both are open items in `ROADMAP.md`.

## Proxy check for power-cut safety

The real test is pulling the plug on a booted card and booting again. The quick check is:

```
findmnt -no FSTYPE,OPTIONS /            # overlay
findmnt -no OPTIONS /boot/firmware       # starts with ro
touch /opt/should-fail && echo BAD       # succeeds into RAM only; gone after reboot
sudo mount -o remount,rw /boot/firmware  # the only way to write boot; maintenance does this
```
