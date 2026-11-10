<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# bridge_sim

A test harness that runs the real VectorBridge Python code of this checkout with WAV files in place of the radio, so an FT8 client can be tested end to end with no Pi and no radio. It is used and tested on macOS; nothing in it is macOS-specific, but Linux has not been tried.

**This is a test tool. The bridge service never imports it, and client apps never ship it or import it.** It imports the GPL-3.0-or-later bridge, so every file here is GPL-3.0-or-later too.

## Same checkout

The sim lives in the vector-bridge repository at `tools/bridge_sim/` and runs the bridge code from the same checkout: `run.sh` puts `<repo>/src` on `PYTHONPATH` (the repository root is found from where `run.sh` sits), and `sim.py` refuses to start if `vector_bridge` loads from anywhere else. Nothing is installed or pinned. To test against another bridge version, check that version out.

The ready line says which code ran: `bridge_commit` is the checkout's `git rev-parse --short HEAD`, with `-dirty` added when tracked files differ from it (untracked files do not count), or null when git is not available; `bridge_version` is `vector_bridge.__version__`.

**Client projects** (for example the Vector Connect and VectorComm apps) use the sim by pointing at a vector-bridge checkout and starting `<checkout>/tools/bridge_sim/run.sh`. Record the ready line's `bridge_commit` (and `bridge_version`) with every result, so a result can be traced to the exact bridge code; a `-dirty` commit cannot be reproduced from git alone.

Changes to the sim are made in this repository, coordinated with the bridge maintainer, and `tests/test_bridge_sim.py` runs the self-test with the rest of the bridge's tests.

### First run

Python 3.11 or newer with numpy (the bridge's only runtime dependency) is needed. If the repository has a `.venv` (the usual development setup), `run.sh` uses its Python. Otherwise the first run creates `tools/bridge_sim/.venv` (gitignored) from `python3`, or from `BRIDGE_SIM_PYTHON` if set, and installs numpy into it, which needs network access once.

**Caveat: valid for protocol and sequencing tests, not for latency budgets.** The sound card is simulated with FakeBackend-style pacing (sleeps, not a codec clock), and TX playout adds the bridge's 100 ms jitter prime.

## What is real and what is simulated

| Part | Real bridge code | Simulated |
|---|---|---|
| UDP audio and control (5100/5101 by default) | `BridgeService`: peer, monitors, jitter buffer, TX cap, telemetry, device reopen | |
| Web API | `TunerApp` and its HTTP server: login, PIN, status, battery, readiness, ladder | `/api/wizard` answers with a fixed stub |
| Transmit | `Ladder`, `PttController`, readiness `Monitor`, `Admin` | `SimRig` behind CAT |
| Sound card | | `WavBackend`: RX from WAV files, TX to WAV files |
| Battery, device ID | | `SimBattery` (80 %, on battery), device ID `5e5e5e5e5e5e5e5e` |

Not present: the rigctl proxy on 4532, rigctld, Wi-Fi, SSH and join pages, the LED and beeper.

## Launch

```
tools/bridge_sim/run.sh --rx-dir DIR --tx-dir DIR [--http-port 8080] [--bind 127.0.0.1] \
    [--fault-port 8081] [--audio-port 5100] [--control-port 5101] [--gates-locked] [--no-rig] [--loop] \
    [--noise-rms 40]
```

See "First run" above for the Python it uses. Stop it with SIGTERM or Ctrl-C; it unkeys, finishes any open TX recording and frees its ports.

| Flag | Effect |
|---|---|
| `--rx-dir` | Mono 16-bit PCM WAVs at 12 kHz (upsampled 4x with a FIR) or 48 kHz. Read once at start; files added later are not seen. A file longer than 15 s is cut at the slot end |
| `--tx-dir` | Created if missing. TX recordings and `slots.jsonl` go here |
| `--loop` | Files in sorted filename order, one per UTC 15 s slot, sample 0 on the slot boundary, from the first boundary after the ready line. **The list wraps** and plays again from the first file |
| no `--loop` | A file named `<YYMMDD_HHMMSS>.wav` (UTC slot start; the name and suffix match in any case) plays in that slot; starting mid-slot plays from mid-file. A file whose name is not a date, or whose seconds are not on a 15 s boundary, never plays, and a warning on stderr at startup says so |
| `--gates-locked` | Skip the gate pre-pass: transmit stays locked until the client (or a test) runs the ladder |
| `--no-rig` | rig_model 0 (VOX, no CAT): `/api/ladder` answers 503; a transmission is detected from the audio |
| `--noise-rms` | Band noise where no file plays, default 40 (about -58 dBFS). See "Silence" below |
| `--bind` | Address for HTTP and UDP. The fault endpoint is always 127.0.0.1 |

### Ready line

When everything listens, one JSON line on stdout:

```
{"ready":true,"bridge_commit":"963c893","bridge_version":"0.2.2","http":"http://127.0.0.1:8080","audio_port":5100,"control_port":5101,"fault":"http://127.0.0.1:8081","pin":"2468","device_id":"5e5e5e5e5e5e5e5e","max_tx_s":15,"rig":"sim"}
```

Ports are the actual ones. Under `--no-rig`: `"rig":"none"` and `"max_tx_s":null`. Under `--gates-locked`: `"max_tx_s":null`. That is the real ladder's answer, not a gap: it reports a cap only once the carrier gate has passed without an SWR reading, so after a client runs the gates `/api/ladder` shows 15.

stdout carries only JSON lines: the ready line first, then one line per slot (slot lines from before the ready line follow it in order). The bridge's own log goes to stderr.

Output never holds up the simulated radio. The capture thread only queues each slot line; a writer thread appends it to `slots.jsonl` and passes it to stdout. stdout and stderr each have their own writer thread, and every other thread only queues text for them. If a launcher closes stdout after the ready line, the sim stops writing stdout and carries on: RX, TX and `slots.jsonl` are unaffected. If nobody reads stdout or stderr, the queues fill and further lines are dropped, never waited on. At shutdown, stderr reports any dropped lines.

By default the harness passes the gates through the real ladder API before the ready line (`confirm_load` dummy, `drive`, `carrier`, `meter` ok), so `tx_allowed` is true. `SimRig` reports no SWR, so the real ladder caps each transmission at 15 s and then rests 15 s, as for a radio whose antenna cannot be verified.

The admin PIN is seeded as `2468`. Log in as an app with `POST /api/login {"pin":"2468","app":"<name>"}` and send `Authorization: Bearer <token>` on every POST.

## Files

### `<tx-dir>/slots.jsonl` (also on stdout)

One line per UTC slot the sound card captured, including the partial slot at start:

```
{"slot_utc":"2026-11-05T12:00:15Z","rx_file":"003.wav"}
{"slot_utc":"2026-11-05T12:00:30Z","rx_file":null}
```

A slot missed while the audio device is down (fault below) has no line.

### `<tx-dir>/tx-<YYMMDD_HHMMSS>.wav` and `.json`

One per transmission, named for the UTC slot of the key time; a second one in the same slot gets `-2`, then `-3`. The WAV is 48 kHz mono 16-bit: exactly what the bridge handed the sound card, after its TX gain and TX cap. The sidecar is written after the WAV, so once it exists the WAV is complete.

| Field | Meaning |
|---|---|
| `wav` | The WAV's file name |
| `key_utc`, `unkey_utc` | Wall time of PTT key and unkey (VOX: first and last non-silent frame played) |
| `first_nonsilent_arrival_utc` | Wall time the UDP packet carrying the first non-silent TX sample reached the service; null if none |
| `first_nonsilent_playout_utc` | Wall time the sound card write got that sample; null if none. If the audio burst began just before the key, both stamps name the burst's first sample and can precede `key_utc` |
| `packets`, `seq_gaps`, `late`, `underruns` | Deltas over the transmission of the bridge's own `tx_packets_received`, `tx_packet_loss`, `tx_late`, `tx_underruns`. One underrun is normal when the stream stops before the unkey. The bridge never resets these counters, not even when the peer changes (its jitter buffer reset keeps its stats, as of bridge 0.2.2), so a delta is never negative |
| `owner` | The ladder owner that keyed; null under `--no-rig` |
| `mode`, `samples`, `sample_rate` | `cat` or `vox`, WAV length, 48000 |

A transmission is SimRig PTT key to unkey (including the bridge's own drain tail of up to 300 ms). Under `--no-rig` (VOX) it starts with the first non-silent frame played and ends after a 1 s silent gap, which is trimmed from the WAV. Transmissions made by the startup gate pre-pass are not recorded.

## Faults

`POST http://127.0.0.1:<fault-port>/sim/fault` with a JSON body. Reply `{"ok": true}`, or `{"ok": false, "error": "..."}` with status 400 (bad request), 409 (key refused) or 500.

| `fault` | Body | Effect |
|---|---|---|
| `takeover` | `{"client": name, "seconds": n}` (seconds optional) | The harness says `hello` as `name` from its own socket and repeats it every second until `takeover_end` or `seconds`. It becomes the peer, so RX stops reaching the client. The bridge is last-sender-wins: the client's next hello or TX packet takes the peer back, and the two trade places each second |
| `takeover_end` | | Stops the takeover |
| `takeover-key` | `{"owner": name, "seconds": n}` (default 10) | A real ladder key under `name`, heartbeated until `takeover-key_end` or `seconds`, so the client's own key gets the real 409. Recorded like any transmission. The real 15 s cap still applies: past 15 s the ladder unkeys it and rests 15 s, and the heartbeats then do nothing |
| `takeover-key_end` | | Unkeys it |
| `battery_tx_locked` | `{"on": bool}` | The battery reads 5 % on battery: `/api/battery` shows `tx_locked`, and keys get 409 |
| `audio_device_down` | `{"seconds": n}` | The sound card read and write raise, so the real reopen path runs: `status` shows `audio_device.ok` false and RX pauses; once it is back, `device_reopens` counts one (the supervisor's backoff can add a few seconds) |
| `rig_cat_error` | `{"on": bool}` | SimRig raises CatError on every CAT call except PTT, so tune and radio get 502 and readiness shows CAT down. PTT keeps working so the sim never sticks in transmit |

`takeover-key` and `rig_cat_error` are refused under `--no-rig`. Send faults one at a time: two overlapping POSTs of the same takeover kind are not guarded against. The takeover socket binds to the same address as the service, so it works with any `--bind`.

## Silence

Where no file plays, the sound card returns low band noise (`--noise-rms`, default 40), not digital silence. The real readiness check turns receive audio red below rms 30 ("nothing is plugged in"), and the real ladder refuses every client key while it is red. A real radio always has band noise, so this keeps the sim like the field. `--noise-rms 0` gives true silence, to test that refusal.

## Self-test

```
tools/bridge_sim/selftest.sh
```

Exits 0 when everything works on this machine, non-zero on any failure. `tests/test_bridge_sim.py` runs it as part of the bridge's pytest suite. It starts the sim three times on free non-default ports (with `--loop`; with `--gates-locked` and its stdout closed after the ready line; with `--no-rig` and dated files), and checks the ready line, RX packets and slot timing, `slots.jsonl`, login, the ladder, a keyed 2 s transmission and its recording, every fault, VOX recording, the 503s, the `-2` file suffix, takeover expiry and clean shutdown. It takes about two minutes.

Slot timing is checked on the frame grid, not on one packet. The bridge stamps each RX packet when it starts the read for it, so a single stamp is late by the previous loop's processing plus however long it overslept, which reaches about 10 ms when the OS wakes the thread late; a stamp is never early. Packets are 10 ms apart by sequence number, so the self-test takes the minimum of (stamp minus sequence offset) over the 200 frames around the first loud one, and requires that frame to be the one starting at the slot boundary: within half a frame, 5 ms. A client measuring timing from the RX stamps should allow for that one-off lateness too.
