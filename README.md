# VectorBridge

A Raspberry Pi Zero 2 W that presents an HF radio to a tablet or computer as a full-duplex network sound card over Wi-Fi. It was built for the Elecraft KX2 and the Vector-Comm iPad app; the audio path (USB sound card into the radio's audio jacks) is the one already proven with a wired splitter, and only the cable between the client and the radio is replaced by UDP.

On top of the audio transport the bridge is a field appliance: it boots as its own Wi-Fi network with a captive portal, keeps its card read-only so a power pull is safe, walks a rig to ready with a setup wizard, keeps transmit locked until a safety ladder has passed, and alerts a phone and the status LED when something that was working stops.

Status: 0.2.2 is published as a pre-release (2026-10-29); it replaces 0.2.1. Image vector-bridge-0.2.2 is the latest captured image; interim-fb72fbf (2026-10-23) was the last one through acceptance. The iPad client is VectorComm, in its own repo.

## Documents

| Document | For | What |
|---|---|---|
| `DOCS/known-issues.md` | operators | what does not work yet, or works with a catch, and what to do about it |
| `DOCS/protocol.md` | client authors | UDP audio and control protocol, version 1 |
| `DOCS/web-api.md` | client authors | HTTP pages and JSON endpoints on port 80, including client PTT |
| `DOCS/field-network.md` | operators | the access point, captive portal, Wi-Fi page and joining another network |
| `DOCS/readiness.md` | operators | the home screen checks |
| `DOCS/setup-wizard.md` | operators | the setup wizard and kits |
| `DOCS/write-the-image.md` | end users | writing the card with Raspberry Pi Imager, with screenshots |
| `DOCS/transmit-checks.md` | operators | the transmit safety ladder |
| `DOCS/alerts.md` | operators | phone alerts and the status LED |
| `DOCS/card-layout.md` | developers | partitions, `/persist`, maintenance mode |
| `DOCS/image-build.md` | developers | golden card, first boot, release image recipe |
| `DOCS/kx2-settings.md` | operators | KX2 and adapter settings: wiring, KX2 menu values, gains, the jack-detect and mic gain traps |
| `DOCS/app-listing.md` | client authors | how a client app describes itself to the bridge's Apps card |

Every `DOCS/*.md` is also served on the card at `http://vectorbridge/docs/`.

## Layout

| Path | What |
|---|---|
| `src/vector_bridge/` | the audio service (`serve`), the web server (`tuner`) and the management CLI |
| `src/vector_bridge/audio/` | `fake` backend (1 kHz tone, no hardware) and `alsa` backend (Pi) |
| `service.py`, `jitter.py`, `protocol.py`, `control.py`, `telemetry.py` | UDP audio transport, TX jitter buffer, control channel, counters |
| `tuner.py`, `portal.py` | web server on port 80, captive portal redirects, on-card docs |
| `readiness.py` | home screen checks |
| `wizard.py`, `wizard_page.py`, `profiles.py` | setup wizard, kits, radio profiles |
| `cat.py`, `tx.py`, `ladder.py`, `ladder_page.py` | rigctld supervisor and client, transmit lock and PTT watchdog, safety ladder |
| `rigproxy.py` | rigctl safety proxy on 4532 for desktop WSJT-X and JS8Call |
| `alerts.py`, `alerts_page.py` | alert settings, status LED and beeper outputs |
| `apsetup.py`, `wifijoin.py`, `wifi_page.py`, `qr.py` | Wi-Fi page: access point name and passphrase, joining another network, QR code (hand-written encoder) |
| `persist.py`, `config.py` | atomic saves to `/persist`, config file |
| `scripts/install.sh` | idempotent installer for the Pi, run as root |
| `scripts/vector-bridge.service`, `scripts/vector-bridge-tuner.service` | systemd units: audio service, and web server (also runs rigctld) |
| `scripts/readonly/` | `setup-readonly-root.sh` (one-time card layout) and `vb-maintenance` |
| `scripts/image/` | `prepare-golden.sh`, `vb-firstboot`, the `vb-ap-apply` and `vb-wifi-join` path units, vendored `pishrink.sh` |
| `scripts/ap/dnsmasq-shared.conf`, `scripts/udev/` | captive DNS for the access point, ACT LED rule |
| `tools/bridge_client.py` | Mac client standing in for the iPad |
| `tools/decode_check.py` | proves the receive chain by decoding FT8 with `jt9` |
| `tools/deploy.sh` | rsync to the Pi, install, restart |
| `config/vector-bridge.example.toml` | documented defaults for the config file |
| `release/` | local image build output (images, SHA-256 checksums, package lists), git-ignored; images are published outside the repo |
| `tests/` | pytest suite, no hardware needed |

## End user: write the image with Raspberry Pi Imager

You need a Raspberry Pi Zero 2 W, a microSD card of 16 GB or more (the image unpacks to about 9.4 GB), and a phone. A USB Wi-Fi adapter is recommended (`DOCS/field-network.md`, "USB Wi-Fi adapter"). Read `DOCS/known-issues.md` before you take the bridge to the field.

1. Download `vector-bridge-VERSION.img.xz` and `vector-bridge-VERSION.img.xz.sha256` from the release. To check the download, run `shasum -a 256 -c vector-bridge-VERSION.img.xz.sha256` in the same folder (`sha256sum -c` on Linux); it prints `OK`.
2. Open [Raspberry Pi Imager](https://www.raspberrypi.com/software/). Device: **Raspberry Pi Zero 2 W**. OS: **Use Custom**, then pick the `.img.xz`. Storage: your card. (Or skip the download: start Imager with `--repo https://github.com/eusef/vector-bridge/releases/download/vVERSION/os_list.json`, and VectorBridge appears in its OS list with the checksum verified for you. `DOCS/write-the-image.md` shows each step.)
3. If Imager offers customisation, choose **Skip customisation**. The bridge does not use Imager's hostname, user, Wi-Fi or SSH settings; it asks for what it needs on its own first-boot page.
4. Write the card, put it in the Pi and power it up. On its first boot the bridge makes an open Wi-Fi network named `VectorBridge-XXXX`.
5. Join that network from your phone. The sign-in page opens by itself (if it does not, browse to `http://vectorbridge/wifi`). Choose a new network name, a passphrase and your Wi-Fi country, then save. The new name must differ from the open one, so phones do not try the old, open entry. The bridge reboots once.
6. Join the new network with the new passphrase and open `http://vectorbridge/`. The first change you save asks you to create an admin PIN. Then set up your radio (`DOCS/setup-wizard.md`).

`vector-bridge-wifi.txt` on the card's boot partition always holds the current network name and passphrase; any computer can read it. A forgotten PIN or passphrase can also be reset from an HDMI monitor and USB keyboard (`DOCS/field-network.md`, "Admin PIN and the console").

## Mac: develop and test without hardware

```sh
cd ~/CODE/vector-bridge
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q

# fake service on localhost, then the client soak against it
VECTOR_BRIDGE_CONFIG=/tmp/vb.toml .venv/bin/vector-bridge serve --audio fake --bind 127.0.0.1
python3 -m venv tools/.venv && tools/.venv/bin/pip install -r tools/requirements.txt
tools/.venv/bin/python tools/bridge_client.py --host 127.0.0.1 --soak 1
```

The service reads `VECTOR_BRIDGE_CONFIG` (or `--config PATH`) so a Mac run never touches `/etc` or `/persist`. `VECTOR_BRIDGE_PERSIST` moves the persist root the same way.

## Pi: bench install

```sh
bash tools/deploy.sh                 # rsync + sudo bash scripts/install.sh + restart
ssh vectorbridge@vectorbridge.local
vector-bridge service status         # active
vector-bridge audio list             # * marks the USB card the config selects
vector-bridge status                 # telemetry table from the running service
```

A card in field mode (read-only root) takes updates only in maintenance mode: `DOCS/card-layout.md`. `install.sh` needs internet for apt and pip.

## Services and ports

| Unit | Runs | Ports |
|---|---|---|
| `vector-bridge` | `vector-bridge serve`: audio and control | UDP 5100 audio, UDP 5101 control |
| `vector-bridge-tuner` | `vector-bridge tuner --http-port 80`: web UI, readiness, wizard, ladder, alerts, LED, rigctld child | TCP 80; TCP 4532 rigctl safety proxy on loopback plus whichever Wi-Fi the Pi is on (default, never the tailnet), loopback plus the access point, loopback only, or every interface: `rigctl_access`, set under Radio, Advanced; rigctld itself on 127.0.0.1:4534 only |
| `vb-ap-apply.path`, `vb-wifi-join.path` | root helpers for the Wi-Fi page | none |
| `vb-firstboot` | first boot of a burned image, once | none |

Config file: `/persist/vector-bridge/config.toml` on a card with the persist partition, `/etc/vector-bridge/config.toml` otherwise.

## Management CLI

| Command | Does |
|---|---|
| `vector-bridge serve [--audio alsa\|fake] [--bind ADDR]` | run the bridge in the foreground (systemd runs this) |
| `vector-bridge service start\|stop\|restart\|status\|logs [-f] [-n N]` | systemctl and journalctl wrappers |
| `vector-bridge status [--json]` | telemetry over the control port |
| `vector-bridge audio list` | ALSA cards with USB ids, selected one marked |
| `vector-bridge audio select <card>` | persist a card's USB id and restart |
| `vector-bridge audio gain --tx N --rx N` | set and persist mixer levels (0 to 100) |
| `vector-bridge audio meter [--interval S]` | live RX and TX peak, RMS and clip readout, 250 ms refresh |
| `vector-bridge tuner [--bind ADDR] [--http-port N]` | the web server. systemd serves it on port 80; the CLI default is 8080 because a non-root shell cannot bind 80 |
| `vector-bridge wifi join <ssid>` / `wifi status` | bench tool: infrastructure Wi-Fi through nmcli, password prompted |
| `vector-bridge ap enable [--ssid S]` / `ap disable` | bench tool: bring the access point up or remove it |
| `vector-bridge ap ensure [--ssid S] [--passphrase P] [--reset]` | installer: make the access point the boot default without activating it |
| `vector-bridge ap show` | print the access point name and passphrase (needs sudo) |
| `vb-maintenance status\|enter\|apply [dir]\|exit` | field mode and maintenance mode round trip |

In the field, the Wi-Fi page (`http://vectorbridge/wifi`) replaces `wifi join` and `ap enable`.

## Proposal phases and the commands that run them

Phases 1 to 5b passed on hardware with the KX2 by 2026-09-19. Phase 6 passed by 2026-10-01: VectorComm decoded JS8 live on the iPad.

| Phase | Where | Command | Pass looks like |
|---|---|---|---|
| 1 USB audio locally | Pi | `arecord -l; aplay -l; vector-bridge audio list` | UGreen listed with its USB id and marked `*` |
| 2 local loopback | Pi | `vector-bridge serve` then `vector-bridge audio meter` with phones-to-mic loop cable | RX meter follows TX, no clip |
| 3 network RX | Mac | `tools/bridge_client.py --host vectorbridge.local --record rx.wav` | audible radio audio on the Mac, loss 0 |
| 4 network TX | Mac | `tools/bridge_client.py --host vectorbridge.local --tx tone --amplitude 0.25` | `vector-bridge audio meter` on the Pi shows TX level; KX2 VOX keys |
| 5 full duplex soak | Mac | `tools/bridge_client.py --host vectorbridge.local --soak 10` | report: loss below 0.1 percent, underruns 0, overruns 0, Pi CPU below 60 percent |
| 5b decode proof | Mac | `tools/decode_check.py rx.wav` on a `--record` capture from an FT8 frequency | stations decoded; a level reading alone does not prove the chain |
| 6 digital mode | iPad | JS8 decode through the iPad client (VectorComm) | decode on the iPad |

Grep the journal for failure events: `journalctl -u vector-bridge | grep -E 'event=(alsa_underrun|alsa_overrun|udp_loss|excessive_jitter|audio_clipping|device_disconnect|device_reconnect)'`.
