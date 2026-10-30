# Changelog

What changed in each VectorBridge image, for the people who run one. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Numbers in parentheses are pull requests; short hashes are
commits that reached `main` without one.

## [Unreleased]

## [0.2.2] - 2026-10-29

Published as a pre-release, like 0.2.1, which it replaces.

### Fixed

- `/api/rigctl` reports `login_required: true` again, so VectorCable's check says that port 4532 asks for the
  PIN instead of the older "anyone on this Wi-Fi can tune and key" wording. 0.2.1 enforced the login but did
  not advertise it. (24c6667)

## [0.2.1] - 2026-10-29

Published as a pre-release, like 0.2.0: the client apps do not yet log in for the admin PIN, and the image has
not had a field session on the air yet. This release replaces 0.2.0, which is withdrawn.

### Changed

- **Breaking for desktop apps.** Port 4532 now asks every app for the admin PIN (as an app token) before it
  answers a command, in every "Rig Control for Apps" setting. That setting now only picks where the port
  answers. WSJT-X, JS8Call and fldigi cannot log in by themselves: run VectorCable 0.2.0 or later on the
  computer, log in once, and point the app's Hamlib NET rigctl at VectorCable's own address, 127.0.0.1:4532.
  Apps on the bridge itself (loopback) are unchanged. On 0.2.0 an app on the Wi-Fi could retune or key the
  radio with no PIN. (#89)
- VectorCable 0.2.0 adds a Linux client (x86_64 and arm64 tarballs; a desktop with PipeWire and glibc 2.31 or
  newer). Its installer puts it under `~/.local` with no root and checks the download against the release
  manifest first. It logs in to port 4532 for WSJT-X, JS8Call and fldigi, and asks for the PIN again after a
  revoke. On Linux the PIN is asked at every start (no token store yet); macOS keeps it in the Keychain.
  PulseAudio-only desktops are untested. macOS stays Apple silicon only, unsigned.
  Download: https://github.com/eusef/vector-cable/releases/tag/vectorcable-v0.2.0
- The design reference under `DOCS/design/` no longer names the tool it was exported from.

### Known issues

- The truSDX is not supported over its single USB cable yet. It sends audio and radio control over one USB serial link, and the bridge needs a USB sound card for audio. Support is planned for a later release. Until then, a truSDX needs a USB sound card for audio. Use a mono cable into the radio's MIC jack: a stereo plug can hold the radio's key line and lock its controls.

## [0.2.0] - 2026-10-28

Published as a pre-release: the client apps do not yet log in for the admin PIN (so a bridge with a PIN set
refuses their settings changes), and the image has not had a field session on the air yet.

### Added

- New home page that shows whether the bridge is ready to receive and transmit, with sheets for setup, Unlock Transmit, receive, power, Wi-Fi and display. The old `/setup` and `/transmit` pages open these sheets. (#34)
- You can set up a radio and run the transmit checks with no client app. The bridge sends its own 1500 Hz test tone for the drive and carrier checks, which helps desktop WSJT-X and JS8Call stations. (#34)
- Rig control for desktop WSJT-X and JS8Call on port 4532, through a safety proxy. Keying goes through the transmit lock and checks, has a 20 s cap, and unkeys if the app disconnects. A new "Rig Control for Apps" setting under Radio, Advanced picks where 4532 answers. The default, `wifi`, answers on the bridge's own Wi-Fi or a joined home Wi-Fi, follows a switch between them, and never answers on a tailnet. (#10, #35, b473f50, #66)
- Setup can pick any radio Hamlib knows, searched from the card's own list. It also takes an optional Icom CI-V address and an "Expert Hamlib settings" field. Each expert setting is checked against what Hamlib says that radio accepts. (adb2f7e, 55a2a37)
- Keying by RTS or DTR with no radio control (for example a Digirig), with an optional separate keying port. Digirig support is experimental. (adb2f7e, 55a2a37)
- The bridge picks the sound card on the radio's USB cable by itself, or you can pick one by hand under Advanced. (adb2f7e)
- The serial port is a list of USB serial devices by name, not a typed /dev path. Leaving it on "automatic" uses the one USB serial device. (4b5cbfe, #34)
- "Find It for Me" on a failed radio control check tries the common baud rates and saves the first one that answers. (2efddea)
- VOX-only setup: "Sound card on VOX" (SignaLink or a USB sound card) keys the radio from the audio, with no radio control cable. A failed radio control check offers "Key by VOX Instead". (#34, #50)
- Transmit Time Limit sheet: transmit audio that runs 30 s with no 1 s pause is muted until it pauses. You can set 0 (off) up to 600 s. It applies to every radio and is the only time limit on a VOX setup. (#49)
- Unlock Transmit refuses when the radio is tuned outside the amateur bands (IARU regions 1 to 3). You can override it, and the bridge records that you did. The Unlock page has a frequency and data mode box and a Tune Radio button. Apps can still tune outside the bands to listen, for example to WWV. (e2e8228, 0d1bb88, #57)
- "Kits and Reset" sheet: rename and delete saved kits, "Reset Radio Setup", and "Reset Everything Except Wi-Fi". (61af40a)
- A Support Bundle download in the footer. On every readiness alert the bridge saves a fault bundle to the card and keeps the newest 5. The footer and `vector-bridge --version` show the version and build. (a90c9a9, 93aab53)
- PiSugar battery level and plugged-in state on the home page. Tested on PiSugar 2. PiSugar 3 and 2 Pro are untested. (#15, #16, #17)
- A home page badge says "Field mode, safe to unplug" or "Maintenance, don't pull power". (#18)
- Rejoin home Wi-Fi at boot: mark one saved network, and the bridge joins it at power-up when it is in range. Out of range it keeps its own Wi-Fi. This also works with adapters that cannot scan while hosting the bridge's Wi-Fi, such as the TP-Link Archer T2U Nano. (#19, #20, #62)
- USB Wi-Fi adapter support: the bridge prefers a USB adapter, handles plugging and unplugging, and runs its own Wi-Fi on a quiet 5 GHz channel. The Panda AC600 is the recommended adapter. On the built-in radio the home page shows a notice recommending one. (#58)
- Wi-Fi country on the first-boot page and the Wi-Fi page. The bridge stays on 2.4 GHz until you set it. (#72)
- The home page Apps card lists connected apps with their name, icon and a launch link. (#57)
- A second, listen-only receive stream, so a desktop decoder such as JS8Call can listen beside the iPad app. (#51)
- HDMI console menu: reset the admin PIN, show the bridge Wi-Fi, turn SSH on or off, or open a command line. (#63, #71)

### Changed

- One-hour bench run on the release candidate (IC-705, FT8, 2026-10-27): decodes in every minute, no clipping, no errors. Over home 2.4 GHz Wi-Fi three transmissions each lost one 10 ms frame to network jitter; every period still completed. On the bridge's own Wi-Fi or a 5 GHz adapter no loss was seen. FT8 DT through the bridge is about +0.7 s with VectorCable at its 480 ms buffer, of which the bridge itself adds about 0.2 s.
- Securing the open first-boot Wi-Fi needs a new name. Phones that joined the open network do not rejoin it under the same name once it has a passphrase. (#75)
- Hamlib 4.7.2 from Debian trixie-backports. This adds IC-705 WFM and FM filter widths. (77487d7)
- A failed radio control check shows Hamlib's own error lines under "Details from Hamlib". (4b5cbfe)
- On sound cards with no receive volume control (the IC-705's built-in codec), the receive slider is disabled and the page points you to the radio's USB AF output level. (74aea84)
- The bridge's Wi-Fi uses WPA2 only on every radio, so Apple devices do not ask for the password again after an adapter is unplugged. (#58)
- For app developers: status reports whether the audio device is open, `GET /api/rigctl` reports `vb_auth`, and several protocol edge cases are fixed. (#23, #54, #70)
- The docs served on the card keep operator settings only. Bench history moved to an archive that is not on the card. (#59, #61, #73)
- The desktop audio client, `tools/bridge_client.py`, survives a Pi reboot, a stale address and socket errors. (#39, #47)
- VectorBridge is licensed GPL-3.0-or-later. `LICENSE` and `THIRD-PARTY-NOTICES.md` ship on the card. The card's boot partition lists every installed package and version (`vector-bridge-packages.txt`), with an offer of their source in the notices.
- Bench and build tooling: deploy script, tailnet join, I2C check and keyfile ownership in the installer, record gap padding, release gate. (#52, #44, #34, 0ff0784, bc82153, 811b810)

### Fixed

- Joining another Wi-Fi from the Wi-Fi page works on the first try: the bridge scans until the network shows up before it connects. A failed join keeps the passphrase you typed. (#77)
- The radio unkeys when the bridge restarts after a crash and when its service is stopped while keyed. (#9, #11)
- No false "Unexpected transmit" alert in the 2 s after the bridge unkeys. (#34)
- After a failed carrier check, the next passing check restores your own RF power, not the low test power. (#53)
- One client with a slow Wi-Fi link no longer freezes receive audio for every client. Audio lost to an overrun no longer shifts the decode timing. (#55, 8db8582)
- The audio service recovers when the sound card fails to start or drops off USB, instead of spinning or leaving receive dead until a restart. (#22, b47b655)
- QRP Labs QMX: 24-bit audio is read correctly, and transmit level is set in software on cards with no playback volume. QMX support is experimental. (32c914f, 9cfae2b, 058cfd7)
- Port 4532 no longer stops answering after a silent radio uses up the service's threads. (663361b, a791544)
- A damaged settings file no longer crash-loops the bridge. It falls back to defaults, keeps the bad file as `config.toml.bad`, and shows a band on the home page. (4762da8)
- Radio control starts after a reboot when its settings last passed, even if a later setup step was undone. (#69)
- Transmit Time Limit and gain saves no longer report "not answering" when they worked. (#50)

### Security

- Admin PIN (4 to 8 digits) for every settings change and every action that can key the radio. After 5 wrong PINs the wait grows, up to 1 hour. Reset a forgotten PIN from the HDMI console or with `sudo vector-bridge pin reset`. Reading pages stays open. (#63)
- The bridge refuses changes sent from other websites (cross-site requests and DNS rebinding). (#9)
- Apps log in once and keep their own token instead of storing the PIN. A PIN change or console reset ends the token, and you can revoke it. (#65)
- Settings commands on UDP port 5101 answer only from the Pi itself. Apps must use the PIN-protected web API. (#64)
- With Rig Control for Apps set to `all`, a client that is not on loopback or the bridge's Wi-Fi (for example on a tailnet) must log in with an app token on 4532. (#67, #70)
- SSH is off by default. Turn it on from the console or the Wi-Fi page, and set a new password when you do. (#71)
- The image ships with no bench SSH keys, password hash, admin PIN, app tokens, tailnet keys, Raspberry Pi Imager cloud-init seed, or NetworkManager and tailscaled state. cloud-init is turned off. (#71, #74, #76)
- The web service no longer holds the CAP_SYS_ADMIN privilege. (#68)
- App names, notes and links on the Apps card are escaped, link types are filtered, icons are checked, and request size is capped. (#57)

### Known issues

See `DOCS/known-issues.md`.

## [0.1.4] - 2026-09-23

Earlier releases are not recorded here.
