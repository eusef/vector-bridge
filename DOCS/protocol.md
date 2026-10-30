# VectorBridge wire protocol, version 1

The Pi defines this protocol. A client (today the Mac test client in `tools/bridge_client.py`, later the Vector-Comm iPad app) is written against this document, not against the Pi code. Everything is UDP; there is no TCP, HTTP, or mDNS.

| Item | Value |
|---|---|
| Audio port | 5100 (config `audio_port`) |
| Control port | 5101 (config `control_port`) |
| Sample format | 48 kHz, mono, signed 16-bit, little-endian |
| Packet cadence | 10 ms, 480 samples, 960 PCM bytes |
| Byte order | little-endian for every header field |
| Clients | one peer at a time, plus up to 2 listen-only monitors (see Monitors) |

## Audio packet

Header is `struct.Struct("<4sBBIQH")`, 20 bytes, followed by the PCM payload. Total packet 980 bytes.

| Offset | Field | Type | Meaning |
|---|---|---|---|
| 0 | magic | 4 bytes | `VBRG` |
| 4 | version | uint8 | 1 |
| 5 | direction | uint8 | 0 = RX (Pi to client, audio from the radio), 1 = TX (client to Pi, audio for the radio) |
| 6 | sequence | uint32 | per-direction counter, +1 per packet, wraps at 2^32. TX may start anywhere. RX starts at 0 when the service starts and is not reset for a new peer |
| 10 | timestamp_us | uint64 | sender's monotonic clock in microseconds at the first sample |
| 18 | sample_count | uint16 | 480, or 0 for a TX keepalive. Any other value is rejected |
| 20 | pcm | int16[sample_count] | signed 16-bit mono |

Rules:

- A packet with the wrong magic or version, a direction other than 0 or 1, a `sample_count` other than 0 or 480, or a payload length that does not match `sample_count * 2` is dropped and logged (rate-limited). It is not counted in `status`. A well-formed RX-direction packet sent to the Pi is ignored silently.
- The Pi sends RX packets only while a peer or a monitor is registered (see below). The client should send TX packets only when it has audio for the radio; silence is fine but not required.
- The client should keep its own jitter buffer for RX audio. 100 ms is a good starting depth; the Pi uses 100 ms (cap 400 ms) for TX. The proposal suggested 30 ms; measured on a Pi Zero 2 W on 2.4 GHz Wi-Fi, arrival stalls of 100 to 130 ms occur about once a minute even with power save off, and 30 ms underran on every stall.
- Timestamps are for jitter measurement only. Neither side synchronises clocks.

## Peer registration

The Pi remembers the most recent address (IP and port) that sent it either a TX audio packet on the audio port or a control `hello`, and sends RX packets to that address. A peer that has sent neither a TX packet nor a `hello` for 5 seconds is dropped and RX sending pauses until any peer speaks again. The client therefore needs no configured address on the Pi, and it survives the Pi rebooting or losing Wi-Fi by simply sending `hello` again.

The peer is one address. RX goes to the address that last sent a `hello` or a TX packet; `status`, `set_gain` and `tone` do not move it. A new peer address also resets the Pi's TX jitter buffer, dropping any TX audio still queued (about 100 ms), so a reconnecting client may start its TX sequence anywhere.

Either socket layout works if the peer stays put:

- **One unconnected UDP socket** (BSD sockets) that sends `hello` to 5101 and TX audio and keepalives to 5100. RX arrives on that socket.
- **Separate sockets**, where the audio socket sends only TX audio and 0-sample keepalives to 5100, and the control socket is used for `hello` only before the audio stream starts, then for `status`. RX arrives on the audio socket. This is the layout for Network.framework: an `NWConnection` to 5100 receives RX, since RX comes from 5100, and a `hello` is not required (5100 is the default audio port; `hello` reports it). `tools/bridge_client.py` works this way.

Never send `hello` from a control socket while the audio socket is streaming: each one moves RX to the control socket and the next TX packet moves it back, costing about 100 ms of TX audio every time.

Practical client behaviour: send `hello`, then either stream TX or send a keepalive at least every 2 seconds while listening only. A keepalive is either another `hello` (one-socket layout only) or a TX packet with `sample_count` 0 and no payload; it refreshes registration and carries no audio. `status` and `set_gain` do not refresh registration. RX can pause for 10 s or more while the Pi reopens its audio device (`device_reopens` in `status` counts these); keep sending keepalives through the gap and the audio resumes.

## Monitors

A monitor is a listen-only client, for example `bridge_client.py --monitor` feeding JS8Call on a Mac so its decodes can be compared with the iPad's. It receives the same RX packets as the peer, with the same sequence numbers, and never takes the peer slot.

- Register by sending `monitor` to 5101 **from the socket that should receive RX**. Repeat it at least every 2 seconds; a monitor silent for 5 seconds is dropped.
- A monitor must not send TX packets. The Pi drops every TX packet from a monitor address, keepalives included, so a monitor can never reach playback or key a VOX rig. A TX packet from a monitor that has already been dropped registers it as the peer, as from any other address.
- At most 2 monitors at a time. A `hello` from a monitor address makes it the peer and removes it from the monitors.
- Each monitor is another 980-byte datagram every 10 ms (about 0.8 Mbit/s) on the Pi's Wi-Fi.

## Control channel

UDP, port 5101. One JSON object per datagram, no newline, one reply datagram per request, sent back to the requesting address. Requests always carry a string `cmd`. Replies always carry a boolean `ok`. New commands (PTT, CAT) are added as new `cmd` values and never change the audio packet.

The settings commands `set_gain`, `set_tx_audio_cap`, `select_audio` and `set_rigctl_access` are answered only from the Pi itself (the web server and the `vector-bridge` CLI send them from 127.0.0.1). From any other address they reply `{"ok":false,"error":"pin_required"}` and change nothing, because settings need the admin PIN (roadmap 1.2). An app changes them through the web server's PIN-gated twins (`DOCS/web-api.md`): `POST /api/gain`, `POST /api/tx_audio_cap`, `POST /api/rigctl`, and the sound card through `POST /api/wizard` (`update` with `audio_usb_id` and `audio_usb_path`).

### hello

Request: `{"cmd":"hello","client":"<name>"}`

Reply: `{"ok":true,"device":"VectorBridge","protocol":1,"sample_rate":48000,"channels":1,"audio_port":5100}`

Side effect: registers the sender as the audio peer.

### monitor

Request: `{"cmd":"monitor","client":"<name>"}`

Reply: the `hello` reply plus `"role":"monitor"`, or an error: `is_peer` when the sender is the current peer, `monitors_full` when 2 other monitors are registered.

Side effect: registers or refreshes the sender as a listen-only monitor (see Monitors).

### status

Request: `{"cmd":"status"}`

Reply: `{"ok":true, ...telemetry}` with these fields:

| Field | Meaning |
|---|---|
| rx_packets_sent | RX datagrams sent since start, counting one per peer and one per monitor |
| tx_packets_received | TX packets accepted from the client |
| rx_packet_loss | RX packets the Pi failed to send |
| tx_packet_loss | TX packets missing at playback time, filled with silence |
| tx_late | TX packets that arrived after their slot was played |
| tx_reordered | TX packets that arrived out of order and were resequenced |
| rx_overruns | ALSA capture overruns |
| tx_underruns | playback underruns (jitter buffer empty or ALSA underrun) |
| network_jitter_ms | smoothed absolute deviation of TX inter-arrival from 10 ms |
| jitter_depth | TX jitter buffer depth in packets at the last playback |
| rx_peak, rx_rms | last captured block, int16 counts (32767 is full scale) |
| tx_peak, tx_rms | last played block, int16 counts |
| clip_count | samples at full scale in either direction, cumulative |
| sample_rate | 48000 |
| alsa_device | opened device description, or `fake:1kHz-tone` |
| peer | `"ip:port"` of the registered client, or null |
| monitors | `["ip:port", ...]` of the registered monitors, empty when none |
| clients | `[{"addr": "ip:port", "role": "peer" or "monitor", "client": str}, ...]`, one per live stream. `client` is the string the stream's own `hello` or `monitor` sent, else the newest one from any port on its IP, else `""`. The home page's Apps card is built from it (DOCS/app-listing.md) |
| device_reopens | times the supervisor reopened the audio device |
| audio_device | `{"ok": bool, "error": str or null, "since": int}`. `ok` is false while the audio device is closed (missing at boot, or lost and being reopened); no RX is sent then. `error` is the latest open or read failure, null when ok. `since` is Unix seconds of the last change of `ok` |
| uptime_s | seconds since the service started |
| tx_gain, rx_gain | mixer levels currently applied, as `set_gain` reports them |
| rx_gain_fixed | true when the sound card has no capture level control (the IC-705's built-in codec), so `rx_gain` changes nothing and the receive level is set on the radio. False when the card has one, or when the check could not tell |
| tx_audio_cap_s | the transmit audio cap in seconds, 0 when off (`set_tx_audio_cap`) |
| tx_audio_muted | true while the cap is muting TX audio |
| tx_audio_cap_trips | times the cap has muted TX audio since the service started |

### set_gain

Request: `{"cmd":"set_gain","tx":<0..100>}` or `{"cmd":"set_gain","rx":<0..100>}`, or both keys.

Reply: `{"ok":true,"tx":<applied>,"rx":<applied>}`. `tx` is the playback mixer level toward the KX2 microphone input; `rx` is the capture mixer level from the KX2 headphone output. Values are persisted to the Pi's config file.

### set_tx_audio_cap

Request: `{"cmd":"set_tx_audio_cap","seconds":<0..600>}`, a whole number.

Reply: `{"ok":true,"tx_audio_cap_s":<seconds>}`. Persisted to the Pi's config file (`tx_audio_cap_s`, default 120). The same number is the key cap for desktop apps on port 4532. When TX audio, client audio or the test tone, has run this long without a 1 s pause below about -50 dBFS, the Pi plays silence in its place until the source pauses for 1 s. A VOX rig keys on the audio, so this is its only time limit. 0 turns the cap off. The home page sets it on its Transmit Time Limit sheet.

### select_audio

Request: `{"cmd":"select_audio","vendor":"0d8c","product":"0014","usb_path":"1-1.4.2","strict":false}`. Every key is optional; all empty means the first USB sound card. `vendor` and `product` are 4 hex digits, and `usb_path` is the card's USB position (`1-1.2` style). With `strict` true, only the card at `usb_path` will do. Setup sends true when another card with the same ids is plugged in.

Reply: `{"ok":true,"changed":<bool>}`. Saved to the Pi's config file (`usb_vendor_id`, `usb_product_id`, `audio_usb_path`, `audio_usb_strict`). When `changed` is true, the service closes the current card and opens the chosen one, in about half a second. Audio stops meanwhile, and `audio_device` in `status` reads not ok ("switching sound card"). The setup wizard sends it at its cabling step; a client has no reason to send it.

### set_rigctl_access

Request: `{"cmd":"set_rigctl_access","access":"off"|"ap"|"wifi"|"all"}`.

Reply: `{"ok":true,"rigctl_access":"<access>"}`, or `{"ok":false,"error":"config_save_failed"}` when the file could not be written (the setting is then unchanged). Saved to the Pi's config file (`rigctl_access`, default `wifi`) for the tuner, which runs the rigctl safety proxy on TCP 4532 and reads the setting when it starts. The tuner's `POST /api/rigctl` sends it and then moves its own listeners (`DOCS/web-api.md`); a client has no reason to send it.

### tone

Request: `{"cmd":"tone","hz":<200..3000>,"seconds":<0..30>}`. `hz` defaults to 1500. `seconds` 0 stops the tone.

Reply: `{"ok":true,"tone_hz":<hz>,"tone_s":<seconds>}`. The Pi plays a sine at 0.9 of full scale in place of client TX audio until `seconds` run out or a stop arrives. Client TX packets that arrive meanwhile are dropped, not queued. The transmit checks use it (`DOCS/transmit-checks.md`); a client has no reason to send it. It does not move the peer or refresh registration.

### Errors

| Reply | When |
|---|---|
| `{"ok":false,"error":"unknown_cmd"}` | `cmd` is a string the Pi does not know |
| `{"ok":false,"error":"pin_required"}` | a settings command came from an address other than the Pi's own (see the start of this section); it is refused before its values are checked |
| `{"ok":false,"error":"bad_request"}` | datagram is not a JSON object with a string `cmd`, or `set_gain` has neither `tx` nor `rx`, or `tone` has no numeric `seconds` or a non-numeric `hz` |
| `{"ok":false,"error":"bad_gain"}` | a `set_gain` value is not a number in 0 to 100 |
| `{"ok":false,"error":"bad_tone"}` | a `tone` value is out of range |
| `{"ok":false,"error":"bad_tx_audio_cap"}` | `set_tx_audio_cap` `seconds` is not a whole number in 0 to 600 |
| `{"ok":false,"error":"bad_rigctl_access"}` | `set_rigctl_access` `access` is not `off`, `ap`, `wifi` or `all` |
| `{"ok":false,"error":"bad_audio_card"}` | a `select_audio` id is not 4 hex digits, `usb_path` is not a USB position, or `strict` is not true or false |
| `{"ok":false,"error":"is_peer"}` | `monitor` came from the current peer's address |
| `{"ok":false,"error":"monitors_full"}` | `monitor` came from a new address while 2 monitors are registered |
| `{"ok":false,"error":"gain_failed"}` | the Pi's mixer refused the change. It may be partly applied (`tx` is set before `rx`), and `status` may still show the old levels: send `set_gain` again |

## Out of scope in version 1

No codec, no encryption, no authentication, no CAT, no PTT on this channel (a VOX rig keys from the audio itself), no Bonjour service record. The Pi is reachable as `vectorbridge.local` through the Pi's own mDNS responder, which is independent of this protocol.

CAT and PTT for rigs set up through the wizard are on the Pi's web server, not in this protocol: a client keys with `POST /api/ladder` (`key`, `heartbeat`, `unkey`), which goes through the transmit lock, the safety checks and the hang-PTT watchdog. See `DOCS/web-api.md`.

A rig with no CAT (VOX only) has nothing for `/api/ladder` to key: the client sends TX audio only, and the radio's VOX keys on it. The client should offer this as a per-station PTT setting. When the client stops sending (quit, backgrounded, Wi-Fi lost) the Pi plays silence and VOX drops. A client that keeps sending audio with no 1 s pause is muted after the transmit audio cap (`set_tx_audio_cap`, 120 s by default) and stays muted until it pauses.

Desktop apps (WSJT-X, JS8Call) key over Hamlib NET rigctl on port 4532 instead. That port is the bridge's safety proxy (milestone M4, `DOCS/archive/HANDOFF.md`): `T 1` and `T 0` go through the same transmit checks and lock as `/api/ladder`, with a key cap equal to the Transmit Time Limit (`set_tx_audio_cap`, 120 s by default, 0 = off) and an unkey when the client disconnects; every other command passes through to `rigctld`, which listens on loopback only. Every client that is not on the Pi itself must first log in with an app token (`DOCS/web-api.md`, "The same token on port 4532"), so the desktop apps go through VectorCable, which logs in for them.
