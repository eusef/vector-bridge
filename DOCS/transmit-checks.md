# Transmit checks: the safety ladder

Transmit stays locked until this rig has been proven. Tap **Unlock Transmit** on the home
page: the checks open as a sheet in three steps, Load, Drive and Antenna.
`http://vectorbridge/transmit` still works and opens the same sheet. The lock comes back
whenever the radio, the interface or the kit changes, and after every restart of the bridge.

The sheet will not start while receive audio is red ("Fix receive first") or while the
bridge's own battery is below 6%.

## The three checks

| Check | What happens | Can it be skipped |
|---|---|---|
| 1. Antenna or dummy load | You say what is on the antenna jack. A dummy load is offered, never required. | Never |
| 2. Audio drive | The bridge plays its own test tone, keys, reads ALC over CAT, and lowers the transmit level until ALC is just off the peg. On a radio that cannot report ALC (KX2, QMX, uSDX, no-CAT setups) the sheet says so and offers to skip it; set drive by the radio's own ALC meter. | Yes, recorded |
| 3. Antenna: low-power carrier | The bridge sets the radio's lowest power (its profile's minimum, shown on the sheet), keys for two seconds and reads SWR over CAT. SWR of 3.0 or more stops the carrier at once. | Yes, recorded |

Checks 2 and 3 use the bridge's own test tone: a 1500 Hz sine at 0.9 of full scale, played
by the audio service in place of client audio (the `tone` control command). No client app
needs to be connected, so a desktop WSJT-X or JS8Call station can finish setup on its own.
Both checks need the radio tuned inside a ham band and in its data mode. Most radios will not
transmit anywhere else (an IC-705 left on 10 MHz WWV in AM failed Drive with no ALC), so the
sheet says so before Drive and again on a "No tone" result.
The tone starts only once the bridge holds the transmit lock, and runs on a 2 s deadline renewed every 0.2 s
while keyed, so a lost stop leaves at most 2 s of tone. On a VOX rig the tone is what keys it.
Drive is set for this tone's level, about what VectorComm's tune tone measured on the bench
(`tx_rms` 20852). Quieter client audio drives the radio less. Client audio at full scale
drives it at most about 1 dB more.

## Radios that cannot report SWR

The screen always says that the antenna cannot be verified. In order of preference:

- Watch the radio's own SWR meter, or watch for the power folding back, during the carrier. Then tell the page what you saw.
- Failing that, the bridge watches power output and supply voltage during the carrier and stops if the supply sags. Power stays at the minimum.

Unless the bridge read SWR over CAT, it ends any transmission that reaches 15 seconds and then asks for 15 seconds of receive before the next one. That also applies after you confirm the radio's own meter, and after **Transmit Without Testing**.

## Skipping checks

**Transmit Without Testing** on the Load step skips checks 2 and 3 after the load is
confirmed. The bridge writes what was skipped to `ladder-overrides.json` on the persist
partition, and the home page shows the rig as unverified until the bridge restarts. Passing
the checks later does not clear that. If the record cannot be written, the home page says so.
The skip does not survive a restart: transmit locks again and asks for the load.

After any antenna result where SWR was not read over CAT, the radio stays at test power. The
home page says so on the Transmit card; set your operating power on the radio.

## What can never be turned off

- Check 1, the load confirmation.
- The transmit lock: one client keys at a time.
- The hang-PTT watchdog: the radio unkeys if the keying client goes quiet for 3 seconds.

Every keying, including the checks themselves, goes through the lock and the watchdog.
If transmit re-locks while a client is keyed, the bridge takes the radio off the air.

A carrier that puts out no power proves nothing, so check 3 fails if the test tone does not reach the radio.

## Client apps keying the radio

A client app keys through the same door: `POST /api/ladder` with `key`, `heartbeat` and
`unkey` (`DOCS/web-api.md`). A key is refused until the checks have passed or been
skipped, while receive audio is red, and while the bridge battery is at 6% or below on
battery power. The watchdog unkeys a client that stops sending heartbeats.

A radio on VOX has none of this: it keys on the audio. Its time limit is the transmit audio
cap. Audio that runs 120 s without a 1 s pause is muted until the app pauses, so a stuck app
cannot hold the radio on the air. The default covers WSPR (about 110 s) and JT65 or JT9
(about 50 s); long fldigi messages need a higher limit: set it on the home page's Transmit
Time Limit sheet (0 to 600 s, 0 turns it off). The cap applies to every radio, not only VOX
ones, and the same number caps a desktop app's key on port 4532.

## Verified on hardware

| Radio | Result |
|---|---|
| Icom IC-705 | All three checks pass on a dummy load. An SWR abort was proven with an unscrewed load: the carrier stopped in about 0.3 s. |
| Elecraft KX2 | Not run. The first attempt had no receive audio, since explained as the USB adapter's jack detect (`DOCS/kx2-settings.md`). The KX2 cannot report ALC over CAT, so check 2 is always skipped on it. |

Evidence: `DOCS/archive/bringup-log.md`, P5.
