# KX2 and adapter settings that work

Measured on the bench from 2026-09-15 to 2026-09-19 with an Elecraft KX2. These are measured
values, not guesses. The bench history behind them (the decode test, the
soak and the clipping investigation) is in the repository's `DOCS/archive/kx2-bench-2026-09.md`,
which is not on the card.

The bridge's hang-PTT watchdog covers transmit only when the bridge keys the radio (CAT, RTS or DTR),
not when the radio keys itself on VOX.

## Wiring

The adapter's **output** goes to the radio's **input**, and vice versa. That crossover is
the thing people get wrong, including an earlier bench setup checklist (since deleted).

| Adapter side | Radio side | Direction |
|---|---|---|
| Headphone / line output (green on a two-jack adapter) | KX2 **MIC** | TX, keys the radio through VOX |
| Microphone input (pink on a two-jack adapter) | KX2 **PHONES** | RX |

Cables: 3.5 mm TRS, shielded. A TRS plug in the KX2's four-conductor MIC jack puts the
right audio channel on the PTT/UP/DN contact, which is why MIC BTN must be OFF.

Proof the wiring is right, without trusting jack labels: with no tone playing, turn AF
GAIN from zero to three quarters. The Pi's `rx_peak` should follow the knob. Measured
4 counts at zero, about 330 at three quarters, about 25 at one third.

## KX2 settings

| Setting | Value | Why |
|---|---|---|
| Mode | `DATA A` | Disables compression and TX EQ, uses USB. Manual p.27 |
| MENU:VOX MD | `ON` | Must be set **while in DATA A**; the KX2 stores VOX per mode and defaults it OFF for data |
| MENU:VOX GN | `030` | Factory default, keys promptly at the levels below |
| MENU:VOX INH | `000` | Factory default, no anti-VOX needed with headphones out to the adapter |
| MENU:MIC BTN | `OFF` | Required for any mic that isn't the Elecraft MH3, and for a TRS plug |
| MENU:MIC BIAS | `OFF` | Avoids DC on a line-level source |
| Mic gain (KYR-SPT/MIC knob) | `14` (Pi `tx_gain` 70); `11` with the iPad app through the bridge | See the pairing note below |
| PWR | `1.0` W | Bench value, into a dummy load |
| AF GAIN | `14` | Sets the level into the adapter's mic input. Do not move it after RX calibration. At `30` on 40 m the adapter's ADC overloads: `rx_peak` pins at exactly 12316 (full scale after -8.5 dB) with RMS at 75 % of peak, and `clip_count` stayed 0 until the flat-top detector was added on 2026-09-19 |
| FIL / FC (RX filter) | `3.2` kHz, center `1.72` kHz | Puts both filter skirts outside the 500 to 3000 Hz data window; see the passband note below |

Bench frequency used: 14.071.81 into a 50 W dummy load.

## Adapter and Pi

| Item | Value |
|---|---|
| Adapter | UGreen, USB id `12d1:0010`, reports as `KT USB Audio` |
| ALSA | `hw:0`, capture 1 channel, playback 2 channels, period 480 x 4 |
| Mixer controls | `Headphone` for TX, `Mic` for RX (not `Speaker`, which is what a C-Media adapter exposes) |
| `tx_gain` | `70`, which is -15 dB on this adapter |
| `rx_gain` | `83`, which is -8.5 dB (was 85 on 2026-09-15; see "If the receive audio clips" below) |
| Volume scale | Both controls map 0 to 100 linearly onto -50 dB to 0 dB, so one step is 0.5 dB. To change level by X dB, change the number by 2X |
| AGC | This adapter has no Auto Gain Control, so no `alsactl store` step is needed |

### No receive audio: the jack-detect trap (found 2026-09-23)

This adapter chip (`12d1:0010`, both the KT dongle and the UGreen) offers its microphone
input only if a plug is pressing the pink jack's detect switch when the adapter powers up
on USB. The plug on the KX2 PHONES cable does not press it, so the card comes up with
playback only and the wizard stops at Audio in ("No receive audio is arriving from the
radio"). `arecord -l` lists no capture device.

Workaround: plug the adapter in with any ordinary 3.5 mm headphone plug in the pink jack,
wait for it to come up, then swap the KX2 PHONES cable in without unplugging USB. Capture
stays. The lasting fix is a PHONES cable whose plug seats fully.

On 2026-09-23 RX clipped at `rx_gain` `83` until the KX2 AF GAIN was turned down; keep
`rx_gain` at `83` and set AF to match.

### CAT cable (KXUSB)

In the setup wizard, leave the port empty ("automatic") when the KXUSB is the only USB serial
adapter plugged in. With another one plugged in, type the path, for example
`/dev/serial/by-id/usb-FTDI_FT231X_USB_UART_<serial>-if00-port0` (list them with
`ls /dev/serial/by-id/`). Without the KXUSB cable the wizard cannot get past the CAT step with
the KX2 profile, even when the radio keys on VOX; that is step 2.6 in `ROADMAP.md`.

The service persists both gains in the config file (`/etc/vector-bridge/config.toml`, or
`/persist/vector-bridge/config.toml` on a read-only card) and reapplies them at
startup, and it reopened the new adapter automatically when it was swapped in.

Verified after an unplanned power cycle: the Pi came back with the service active, the
adapter again as `hw:0`, and both gains reapplied (Mic 85, Headphone 70) with no manual
step. The clip counter resets to 0 on reboot, so re-read the baseline before judging it.

## The mic gain trap

Mic gain and the Pi's mixer level must be set as a **pair**. Mic gain is far more
sensitive than it looks, and too much gain reads as too little signal:

| Source level | Mic gain | ALC |
|---|---|---|
| Pi mixer 70 (-15 dB), tone at 0.25 | 14 | 4 bars, correct |
| iPad VectorComm through the bridge, `tx_gain` 56 (read after the session, INFERRED to be the level used), 2026-10-01 | 11 | 4 bars, correct |
| Pi mixer 100 (0 dB), tone at 0.25 or 0.9 | 80 (max) | **0 bars**, overloaded |
| Mac headphone output at full volume | 1 (min) | 4 bars |

Elecraft anticipates this: "You may need an attenuator if the drive levels are too high"
(manual p.27). If the ALC reads zero, try *lowering* mic gain before raising drive.

A second trap: with the two cables on each other's jacks, the radio still showed ALC bars
and still keyed, because transmit audio leaked in through the PHONES output. The RX level
was dead (peak 4 of 32767) which is how to spot it.

## RX calibration, measured

On 14.074 USB in DATA A, magnetic loop antenna, AGC-S on, S3-S4 signals with an S1-S2
noise floor. Each step of the capture control is 0.5 dB, so the setting can be calculated
from one measurement instead of hunted for.

| AF volume | `rx_gain` | Max peak | Mean peak | Verdict |
|---|---|---|---|---|
| 20 | 51 (-24.5 dB) | 662 | 304 | Far too low |
| 20 | 100 (0 dB) | 10027 | 5107 | In target, but no gain left on the Pi |
| 30 | 85 (-7.5 dB) | 12700 | 5715 | **Superseded.** Looked right on 20m, but clips on strong signals. See below |

Target is 8000 to 16000 peak counts on strong signals, never flagging CLIP. Full scale is
32767. Prefer the setting that leaves headroom on both the radio and the Pi, so either can
be trimmed in the field.

AF volume is part of the calibration, not a comfort control. Moving it invalidates
`rx_gain`. The signature is unmistakable: with AF lowered, peaks on a busy band fell from
12700 to 1535 at an unchanged `rx_gain` of 85. If levels collapse, check the AF knob before
suspecting the adapter, the cable or the band.

Also note that the S meter is a poor predictor of audio level. With AGC-S on, S9 signals
do not produce proportionally louder audio than S3, so always measure rather than
extrapolate from the S reading when changing bands.

## Confirmed behavior at these settings

- VOX keys immediately when the tone starts and drops cleanly after it stops.
- ALC holds steady at 4 bars, inside Elecraft's 4 to 5 bar limit for data modes.
- 0 audio clips in both directions, 0 Pi RX overruns, 0 Mac playback underruns.
- RX peaks reach 12700 on band signals with no clipping during reception.
- The waterfall shows bright noise bands at both edges of the KX2 passband (at FIL 2.8:
  roughly 250 to 850 Hz and 2100 to 2900 Hz; at FIL 0.95 they move to 1000 to 1300 Hz and
  1900 to 2200 Hz). They track the FIL setting, so this is the radio's DSP filter skirt,
  not the bridge. Seen 2026-09-19 on 40 m and 20 m in WSJT-X and JS8Call. Fix, verified
  the same day: FIL `3.2` with center (FC) `1.72` kHz gives a flat waterfall from about
  500 Hz to past 3000 Hz. A 1.5 kHz center leaves the upper skirt inside the data window.
- Two faint birdies (about 1279 and 2673 Hz at 14.078 MHz on 2026-09-19) move with the
  VFO, so they are RF at the antenna or inside the KX2, not the audio chain. Ignore them.

## If the receive audio clips

A `rx_peak` that never changes by even one count, while `rx_rms` moves, always means clipping. Do not
calibrate against a pinned peak.

Cause, found on 30 m with S9+ signals: the adapter's input is a **mic** input, roughly 20 dB more
sensitive than the line input Elecraft says to feed, which is the 20 dB measured on the bench. The KX2 has no line-level output; PHONES is the
only receive tap (manual p.27). The `Mic` capture control tops out at 0 dB (attenuation only, after the
ADC), so it can never rescue an input that already clipped: turning it down moves the ceiling without
unpinning it.

At AF GAIN `14` and `rx_gain` `83` the peak varies (22 distinct values in 25 one-second samples, range
6410 to 12316, 0 clips) and the audio decodes at FT8's floor (96 decodes from 53 stations in five
minutes on 40 m, 2026-09-19). No pad was fitted: the clipping is absent at this operating point, not fixed in hardware.

If AF must run higher, or a hotter radio is connected, fit about 20 dB of attenuation between KX2
PHONES and the adapter's mic input: a commercial inline 3.5 mm attenuator, or a resistive divider (for
example 10k in series with 1k to ground, values unverified on this hardware), or use an adapter with a
true line input. Then redo the RX calibration from scratch: AF back to a normal setting, then set
`rx_gain` for peaks of 8000 to 16000 with the peak reading *varying*. The browser tuner flags this
case: a card whose capture control tops out at 0 dB gets a headroom warning in its mixer table.
