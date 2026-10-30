"""Transmit safety ladder: transmit stays locked until this rig has been proven.

Three gates, in order. Gate one (load confirmation) can never be skipped. Gates two
(audio drive) and three (brief low-power carrier with SWR) can be bypassed by one explicit
action that records what was bypassed; the rig is then reported unverified for the rest of
the session. The TX lock and the hang-PTT watchdog live in tx.py and have no override:
every keying here goes through the same PttController, bypass or not.

The ladder re-locks whenever the radio, interface or preset changes. Drive and carrier key
with the bridge's own test tone (tone.py, a control command under HANDOFF D8), so they need
no client app connected.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import apsetup, bands, battery, persist
from .cat import CatError
from .profiles import RadioProfile
from .tx import PttController

log = logging.getLogger(__name__)

GATES = ("load", "drive", "carrier")
TITLES = {"load": "Antenna or dummy load", "drive": "Audio drive (ALC)", "carrier": "Low-power carrier (SWR)"}
OVERRIDABLE = ("drive", "carrier")  # never "load"; the lock and watchdog are not gates at all
LOADS = ("antenna", "dummy")
OWNER = "ladder"

ALC_NO_DRIVE = 0.01  # hamlib normalises ALC to 0..1; below this no tone is reaching the radio
ALC_TARGET = 0.10  # Drive passes at or below this: just below the ALC onset, where digital modes belong (REQ-AUDIO-019)
GAIN_STEP = 5
MAX_DRIVE_STEPS = 20
SWR_ABORT = 3.0
CARRIER_S = 2.0
SAMPLE_GAP_S = 0.2
TONE_HZ = 1500  # mid-passband for a data mode, where FT8 audio sits
TONE_S = 2.0  # renewed with every heartbeat, so a lost stop leaves at most this much tone
LOW_POWER = 0.05  # RFPOWER is 0..1; the rig clamps this up to its own minimum
SAG_ABORT = 0.15  # supply voltage falling by this fraction under carrier means trouble
RF_FLOOR_W = 0.05  # less forward power than this under carrier means no tone reached the radio
CAPPED_REST_S = 15.0  # and as long again in receive before the next one
PTT_TAIL_MAX_S = 0.3  # a deliberate unkey holds PTT for the queued TX audio, never longer than this (REQ-AUDIO-022)
CAPPED_MAX_TX_S = 15.0  # when the antenna cannot be verified: short transmissions only
NO_TONE = "The audio service is not answering, so the bridge cannot play its test tone."
CHECK_AUDIO = ("Check the radio is tuned inside a ham band and set to its data mode, and the audio cable goes "
               "to its data or line input, then run this check again.")
DATA_MODES = ("PKTUSB", "PKTLSB", "PKTFM")  # Hamlib's data modes; PKTUSB covers HF digital
ANY_BAND = "band"  # in the unverified list: the operator allowed transmit outside the amateur bands
OUT_OF_BAND = ("The radio is on {f}, outside every amateur band. Tune it into a ham band, or, if this frequency "
               "is allowed where you are, choose Transmit Outside the Ham Bands on Unlock Transmit's first step.")
TUNE_OUT_OF_BAND = ("{f} is outside every amateur band. Pick a frequency inside a ham band, or, if this one "
                    "is allowed where you are, choose Transmit Outside the Ham Bands first.")
MANUAL_DRIVE = ("This radio does not report ALC over radio control, so the bridge cannot set the transmit level "
                "for it. Set the level with the slider by the radio's own ALC meter, then skip this check.")
ANTENNA_UNVERIFIED = ("The antenna cannot be verified: this radio does not report SWR. "
                      "Keep power at the minimum and transmissions short.")


class LadderError(ValueError):
    def __init__(self, msg: str, code: str | None = None) -> None:
        super().__init__(msg)
        self.code = code  # machine-readable, for an app that must not parse the sentence


class _NoTone(Exception):
    """The audio service did not take the test tone, so there is nothing to measure."""


class Ladder:
    def __init__(
        self,
        cat,
        ptt: PttController,
        control_fn: Callable[[dict], dict | None],
        profile_fn: Callable[[], RadioProfile | None],
        identity_fn: Callable[[], tuple] | None = None,
        record_path: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rx_ok_fn: Callable[[], bool] | None = None,
        battery_fn: Callable[[], dict] | None = None,
    ) -> None:
        self.cat, self.ptt, self.control_fn = cat, ptt, control_fn
        self._profile_fn = profile_fn
        self._rx_ok_fn = rx_ok_fn  # False while the readiness receive check is red: receive gates transmit
        self._battery_fn = battery_fn  # the /api/battery reply; transmit locks at battery.LOCK_PCT
        self._identity_fn = identity_fn or (lambda: ())
        self._record_path = record_path or persist.APP_DIR / "ladder-overrides.json"
        self._sleep, self._clock = sleep, clock
        self._mu = threading.RLock()
        self._identity = self._identity_fn()
        self._unverified: list[str] = []  # sticky for the session, survives a re-lock
        self._keyed_at = 0.0
        self._rest_until = 0.0
        self.record_error = ""
        self._reset()

    def _reset(self) -> None:
        self.load: str | None = None
        self.passed: list[str] = []
        self.bypassed: list[str] = []
        self.details: dict[str, str] = {}
        self.swr_method: str | None = None  # "cat", "operator" or "capped"
        self.any_band = False  # the operator allowed transmit outside the amateur bands, for this rig
        self.ptt_proven = False
        # The operator's power before a carrier lowered it, and what the rig read once lowered:
        # kept until a run restores it, so a run after a failure does not take the test setting as theirs.
        self._power_before: float | None = None
        self._power_left: float | None = None

    def _sync(self) -> None:
        """Re-lock when the radio, interface or preset is no longer the one that was proven."""
        now = self._identity_fn()
        if now != self._identity:
            log.warning("transmit re-locked: rig changed from %s to %s", self._identity, now)
            self._identity = now
            self._drop_anyone()
            self._reset()

    def _drop_anyone(self) -> None:
        """Transmit just became locked: whoever is keyed, client or ladder, comes off the air."""
        owner = self.ptt.lock.owner
        if owner is not None:
            self.ptt.unkey(owner)

    # -- state --

    def _done(self, gate: str) -> bool:
        return gate in self.passed or gate in self.bypassed

    def tx_allowed(self) -> bool:
        with self._mu:
            self._sync()
            return all(self._done(g) for g in GATES)

    def antenna_verified(self) -> bool:
        return self.swr_method == "cat" and "carrier" in self.passed

    def hook(self) -> dict:
        """The shape readiness and the wizard consume."""
        with self._mu:
            self._sync()
            caveats = [ANTENNA_UNVERIFIED] if self.tx_allowed() and not self.antenna_verified() else []
            return {"gates": {"passed": all(g in self.passed for g in GATES),
                              "drive_passed": "drive" in self.passed,  # a kit stores levels only after this (REQ-AUDIO-033)
                              "bypassed": list(self._unverified), "caveats": caveats,
                              "tx_allowed": self.tx_allowed()},
                    "ptt_proven": self.ptt_proven, "tx_owner": self.ptt.lock.owner,
                    "tx_just_released": self.ptt.just_released()}

    def report(self) -> dict:
        with self._mu:
            self._sync()
            prof = self._profile_fn()
            nxt = next((g for g in GATES if not self._done(g)), None)
            return {"ok": True, "tx_allowed": self.tx_allowed(), "next": nxt, "load": self.load,
                    "gates": [{"key": g, "title": TITLES[g], "overridable": g in OVERRIDABLE,
                               "state": "passed" if g in self.passed else "bypassed" if g in self.bypassed else "locked",
                               "detail": self.details.get(g, "")} for g in GATES],
                    "unverified": list(self._unverified), "record_error": self.record_error,
                    "antenna_verified": self.antenna_verified(),
                    "antenna_note": "" if self.antenna_verified() or not self._done("carrier") else ANTENNA_UNVERIFIED,
                    "can_report_swr": bool(prof and prof.can_report_swr),
                    "can_report_alc": bool(prof and prof.can_report_alc),
                    "tx_start_gain": _start_gain(prof) if prof else None,
                    "min_power_w": prof.min_power_w if prof else None,
                    "max_tx_s": CAPPED_MAX_TX_S if self._capped() else None,
                    "watchdog_s": self.ptt.watchdog_s, "never_overridable": ["load", "tx_lock", "watchdog"],
                    "any_band": self.any_band, "data_modes": list(DATA_MODES),
                    "can_tune": bool(prof and prof.rig_model != 0)}

    def _capped(self) -> bool:
        return self._done("carrier") and not self.antenna_verified()

    # -- gate one: never skippable --

    def confirm_load(self, load: str) -> None:
        if load not in LOADS:
            raise LadderError("say what is connected: antenna or dummy")
        with self._mu:
            self._sync()
            self._check_band(self._profile_fn())  # every way to unlock confirms the load first
            self._drop_anyone()
            self.load = load
            self.passed = ["load"]  # a new load means the later gates prove nothing
            self.bypassed = []
            self.details = {"load": "Dummy load connected." if load == "dummy" else "Antenna connected."}
            self.swr_method = None

    def _require(self, gate: str) -> RadioProfile:
        self._sync()
        if "load" not in self.passed:
            raise LadderError("confirm the antenna or dummy load first; this step cannot be skipped")
        for g in GATES[:GATES.index(gate)]:
            if not self._done(g):
                raise LadderError(f"finish or bypass '{TITLES[g]}' first")
        prof = self._profile_fn()
        if prof is None:
            raise LadderError("pick a radio in setup first")
        if self.ptt.lock.owner is not None:
            raise LadderError(f"transmit is in use by {self.ptt.lock.owner}; wait for it to finish")
        self._check_band(prof)  # the radio may have been retuned since the load was confirmed
        return prof

    # -- the radio's frequency: Unlock refuses outside the amateur bands --

    def _band_problem(self, prof: RadioProfile | None) -> str | None:
        """Why the radio's frequency rules out transmit, or None."""
        if prof is None or prof.rig_model == 0 or self.any_band:
            return None  # no radio control: the frequency cannot be read
        try:
            hz = self.cat.get_freq()
        except (CatError, KeyError, ValueError):
            return None  # a radio that does not answer fails its own step or key; this only stops a known-bad frequency
        return OUT_OF_BAND.format(f=bands.mhz(hz)) if bands.band_of(hz) is None else None

    def _check_band(self, prof: RadioProfile | None) -> None:
        problem = self._band_problem(prof)
        if problem:
            raise LadderError(problem)

    def radio(self) -> dict:
        """The radio's frequency and mode, read over CAT, for the Unlock page."""
        prof = self._profile_fn()
        if prof is None or prof.rig_model == 0:
            raise LadderError("this radio has no radio control, so the bridge cannot read its frequency")
        hz, mode = self._read_radio()  # outside the lock: a slow rig must not hold up a live transmission's heartbeats
        return {**self.report(), "radio": {"freq_hz": hz, "mode": mode,
                                           "band": bands.band_of(hz), "any_band": self.any_band}}

    def tune(self, hz, mode, listen_only: bool = False) -> dict:
        """Set the frequency and data mode the operator picked, then read them back.
        `listen_only` tunes outside the ham bands (WWV, say) without allowing transmit there:
        it sets no any_band and records no bypass, and every fresh key still refuses off band."""
        if isinstance(hz, bool) or not isinstance(hz, int) or hz <= 0:
            raise LadderError("give the frequency in hertz")
        if mode not in DATA_MODES:
            raise LadderError("pick a data mode: " + ", ".join(DATA_MODES))
        with self._mu:
            self._sync()
            prof = self._profile_fn()
            if prof is None or prof.rig_model == 0:
                raise LadderError("this radio has no radio control: tune it by hand")
            if self.ptt.lock.owner is not None:
                raise LadderError(f"transmit is in use by {self.ptt.lock.owner}; wait for it to finish")
            if bands.band_of(hz) is None and not self.any_band and not listen_only:
                raise LadderError(TUNE_OUT_OF_BAND.format(f=bands.mhz(hz)), code="out_of_band")
            self.cat.set_freq(hz)
            self.cat.set_mode(mode)
            got_hz, got_mode = self._read_radio()
            ok = got_mode == mode and abs(got_hz - hz) < 10
            detail = (f"The radio is on {bands.mhz(got_hz)} {got_mode}." if ok else
                      f"The radio reads back {bands.mhz(got_hz)} {got_mode}, not {bands.mhz(hz)} {mode}. "
                      "It may not support that frequency or mode: check it on the radio.")
            return {**self.report(), "radio": {"freq_hz": got_hz, "mode": got_mode, "band": bands.band_of(got_hz),
                                               "any_band": self.any_band},
                    "result": {"gate": "tune", "ok": ok, "detail": detail}}

    def _read_radio(self) -> tuple[int, str]:
        try:
            return self.cat.get_freq(), self.cat.get_mode()
        except (KeyError, ValueError) as e:
            raise CatError(f"the radio's frequency or mode reply could not be read: {e}") from e

    def allow_any_band(self) -> dict:
        """The operator's defeat for an allocation the table does not know. Recorded, and the rig is unverified."""
        with self._mu:
            self._sync()
            if not self.any_band:
                self.any_band = True
                if ANY_BAND not in self._unverified:
                    self._unverified.append(ANY_BAND)
                self._record([ANY_BAND])
            return self.report()

    # -- gate two: audio drive --

    def run_drive(self) -> dict:
        """Key with the test tone playing and search the TX level in both directions until ALC sits
        just below its onset (REQ-AUDIO-019).

        From the profile's start level (or the level the operator raised it to) the level goes up by
        GAIN_STEP until the radio first reports drive (ALC >= ALC_NO_DRIVE) or level 100, then down by
        GAIN_STEP until ALC is at or below the profile's target. The level it settles at is the one the
        audio service keeps (set_gain writes the config), so a kit saved afterwards stores it.
        """
        with self._mu:
            prof = self._require("drive")
            if not prof.can_report_alc or prof.rig_model == 0:
                return self._fail("drive", MANUAL_DRIVE)
            status = self.control_fn({"cmd": "status"}) or {}
            current = status.get("tx_gain")
            if not isinstance(current, int):
                return self._fail("drive", "The audio service is not answering, so the transmit level cannot be set.")
            start = _start_gain(prof)
            # Raise Level on the failure card moves the slider above the start: honour it, do not undo it.
            gain = current if current > start else start
            target = prof.alc_target if isinstance(prof.alc_target, (int, float)) and 0 < prof.alc_target < 1 else ALC_TARGET
            try:
                with self._keyed():
                    if gain != current:
                        self.control_fn({"cmd": "set_gain", "tx": gain})
                    steps = 0
                    alc = self._sample_alc()
                    while alc < ALC_NO_DRIVE:  # raise until the radio first sees the tone
                        if gain >= 100:
                            return self._fail("drive", "No drive is reaching the radio even at transmit level 100. " + CHECK_AUDIO)
                        if steps >= MAX_DRIVE_STEPS:
                            return self._fail("drive", f"No drive is reaching the radio after {MAX_DRIVE_STEPS} level steps. " + CHECK_AUDIO)
                        gain, steps = min(100, gain + GAIN_STEP), steps + 1
                        self.control_fn({"cmd": "set_gain", "tx": gain})
                        alc = self._sample_alc()
                    while alc > target:  # then lower until ALC is just below its onset
                        if gain <= 0:
                            return self._fail("drive", "ALC stays pegged even at the lowest transmit level. Turn down the "
                                              "radio's own data or mic input gain.")
                        if steps >= MAX_DRIVE_STEPS:
                            return self._fail("drive", f"ALC is still above its target after {MAX_DRIVE_STEPS} level steps. "
                                              "Turn down the radio's own data or mic input gain, then try again.")
                        gain, steps = max(0, gain - GAIN_STEP), steps + 1
                        self.control_fn({"cmd": "set_gain", "tx": gain})
                        alc = self._sample_alc()
                return self._pass("drive", f"ALC is just below its onset ({alc:.2f}) at transmit level {gain}.")
            except _NoTone:
                return self._fail("drive", NO_TONE)
            except (CatError, KeyError, ValueError) as e:
                return self._fail("drive", f"The radio stopped answering on CAT: {e}")

    def _sample_alc(self) -> float:
        """One ALC reading, SAMPLE_GAP_S after the last level change, with the key kept alive."""
        self._sleep(SAMPLE_GAP_S)
        self._heartbeat()
        return float(self.cat.get_level("ALC"))

    # -- gate three: brief carrier, SWR --

    def run_carrier(self, power_by_hand: bool = False) -> dict:
        """Brief carrier at the lowest power. Aborts the moment SWR is high.

        Where the rig cannot report SWR: the operator watches the rig's own meter (or for
        power fold-back) during the carrier and then answers through `report_meter`. Failing
        that, the ladder watches power output and supply voltage and caps the session.
        Either way the antenna is reported as unverifiable.
        """
        with self._mu:
            prof = self._require("carrier")
            self.swr_method = None  # an earlier run proves nothing about this one
            restore = None
            # A kit with no radio control may still run rigctld to key RTS or DTR. That is Hamlib's
            # dummy rig, whose levels and meters are made up, so none of them is read here.
            cat_levels = prof.rig_model != 0
            try:
                try:
                    if not cat_levels:
                        raise CatError("no radio control")
                    restore = self.cat.get_level("RFPOWER")
                    if self._power_before is not None and self._power_left is not None \
                            and abs(restore - self._power_left) < 1e-3:
                        restore = self._power_before  # still at the test setting an earlier run left
                    self.cat.set_level("RFPOWER", LOW_POWER)
                    self._power_before = restore
                    try:
                        self._power_left = self.cat.get_level("RFPOWER")  # the rig may clamp LOW_POWER up
                    except (CatError, KeyError, ValueError):
                        self._power_left = LOW_POWER
                except (CatError, KeyError, ValueError, AttributeError):
                    restore = None
                    if not power_by_hand:
                        return self._fail("carrier", "Power cannot be set over CAT. Set the radio to its lowest "
                                          f"power ({prof.min_power_w:g} W) by hand, confirm that, and run again.")
                use_cat = prof.can_report_swr and cat_levels
                worst, volts, watts = 0.0, [], []
                with self._keyed():
                    t0 = self._clock()
                    while self._clock() - t0 < CARRIER_S:
                        self._sleep(SAMPLE_GAP_S)
                        self._heartbeat()
                        if use_cat:
                            try:
                                swr = self.cat.get_level("SWR")
                            except (CatError, KeyError, ValueError):
                                use_cat = False
                                continue
                            worst = max(worst, swr)
                            try:
                                watts.append(self.cat.get_level("RFPOWER_METER_WATTS"))
                            except (CatError, KeyError, ValueError):
                                pass
                            if swr >= SWR_ABORT:
                                self.ptt.unkey(OWNER)
                                return self._fail("carrier", f"Aborted: SWR {swr:.1f} is too high. Check the antenna "
                                                  "connection, feed line and tuner before transmitting.")
                        elif cat_levels:
                            for name, out in (("VD_METER", volts), ("RFPOWER_METER_WATTS", watts)):
                                try:
                                    out.append(self.cat.get_level(name))
                                except (CatError, KeyError, ValueError):
                                    pass
                            if volts and volts[0] > 0 and (volts[0] - volts[-1]) / volts[0] >= SAG_ABORT:
                                self.ptt.unkey(OWNER)
                                return self._fail("carrier", "Aborted: the supply voltage sagged under carrier. "
                                                  "Check the battery and the antenna before transmitting.")
                if (watts and max(watts) < RF_FLOOR_W) or (use_cat and worst < 1.0):
                    return self._fail("carrier", "No power left the radio, so nothing was measured. " + CHECK_AUDIO)
                if use_cat:
                    self.swr_method = "cat"
                    return self._pass("carrier", f"SWR {worst:.1f} at the lowest power.")
                self.swr_method = "capped"
                watched = " and ".join(n for n, got in (("supply voltage", volts), ("power output", watts)) if got)
                return self._pass("carrier", "SWR cannot be read over CAT. "
                                  + (f"The {watched} held steady. " if watched else "Nothing could be measured. ")
                                  + f"If you watched the radio's own meter, say what you saw. {ANTENNA_UNVERIFIED}")
            except _NoTone:
                return self._fail("carrier", NO_TONE)
            except (CatError, KeyError, ValueError) as e:
                return self._fail("carrier", f"The radio stopped answering on CAT: {e}")
            finally:
                # Power goes back up only once SWR was really measured: otherwise it stays capped.
                if restore is not None and self.swr_method == "cat":
                    try:
                        self.cat.set_level("RFPOWER", restore)
                        self._power_before = self._power_left = None
                    except (CatError, KeyError, ValueError):
                        log.warning("could not restore RF power to %s", restore)

    def report_meter(self, ok: bool) -> dict:
        """The operator watched the rig's own SWR meter, or for fold-back, during the carrier."""
        with self._mu:
            self._sync()
            if self.swr_method not in ("capped", "operator") or "carrier" not in self.passed:
                raise LadderError("run the carrier check first")
            if not ok:
                self.swr_method = None
                return self._fail("carrier", "You saw high SWR or power fold-back on the radio's meter. "
                                  "Check the antenna connection, feed line and tuner.")
            self.swr_method = "operator"
            return self._pass("carrier", f"You read the radio's own meter and it looked right. {ANTENNA_UNVERIFIED}")

    # -- the one override --

    def override(self, gates: list[str]) -> dict:
        """Bypass drive and/or carrier in one explicit action, and record what was bypassed."""
        with self._mu:
            self._sync()
            if not isinstance(gates, list) or not gates or any(g not in GATES for g in gates):
                raise LadderError("name the checks to bypass: drive, carrier")
            if any(g not in OVERRIDABLE for g in gates):
                raise LadderError("load confirmation can never be bypassed")
            if "load" not in self.passed:
                raise LadderError("confirm the antenna or dummy load first; this step cannot be skipped")
            self._check_band(self._profile_fn())  # skipping the tests never skips the band
            new = [g for g in OVERRIDABLE if g in gates and g not in self.passed]
            for g in new:
                if g not in self.bypassed:
                    self.bypassed.append(g)
                if g not in self._unverified:
                    self._unverified.append(g)
                self.details[g] = "Bypassed by the operator."
            if new:
                self._record(new)
            return self.report()

    def clear_record(self) -> bool:
        """A factory reset: forget the session's bypasses, which otherwise outlive a re-lock, and
        remove their record on the card. False when the card refused."""
        with self._mu:
            self._unverified.clear()
            try:
                with persist.writable(self._record_path):
                    self._record_path.unlink(missing_ok=True)
                return True
            except OSError:
                log.exception("could not remove the bypass record")
                return False

    def _record(self, gates: list[str]) -> None:
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "bypassed": gates, "load": self.load,
                 "rig": [str(x) for x in self._identity]}
        log.warning("transmit checks bypassed: %s", entry)
        self.record_error = ""
        try:
            old = json.loads(self._record_path.read_text()) if self._record_path.exists() else []
            persist.save(self._record_path, json.dumps((old if isinstance(old, list) else [])[-99:] + [entry], indent=1))
        except (OSError, ValueError):
            log.exception("could not record the bypass on the card")
            self.record_error = "The bypass could not be written to the card; it is in the log only."

    # -- keying: the only door to PTT, locked until the ladder is done --

    def key(self, owner: str) -> bool:
        # Read over I2C before taking the lock, so a slow bus cannot hold up heartbeats.
        flat = self._battery_fn is not None and battery.tx_locked(self._battery_fn())
        # An app can retune over 4532, or the VFO can turn, after Unlock: a fresh key checks the band again.
        # Read before the lock (a CAT read must not hold up heartbeats), and for every key, so an unkey in between
        # cannot let a fresh key through unchecked.
        off_band = self._band_problem(self._profile_fn())
        with self._mu:
            if owner == OWNER or not self.tx_allowed():
                return False
            if apsetup.unsecured():
                return False  # open access point: a stranger in range must not key the rig
            if self._capped() and not self.ptt.keyed and self._clock() < self._rest_until:
                return False  # antenna unverifiable: a rest between short transmissions
            if not self.ptt.keyed and self._rx_ok_fn is not None and not self._rx_ok_fn():
                return False  # no receive audio almost always means a cabling fault that breaks TX audio too
            if not self.ptt.keyed and flat:
                return False  # the bridge's own battery is nearly empty
            if not self.ptt.keyed and off_band:
                log.warning("%s: key refused: %s", owner, off_band)
                return False
            fresh = not self.ptt.keyed
            ok = self.ptt.key(owner)
            if ok and fresh:  # a repeat key must not restart the capped-transmission clock
                self._keyed_at = self._clock()
            return ok

    def heartbeat(self, owner: str) -> None:
        with self._mu:
            if owner != OWNER and not self.tx_allowed():
                self._drop_anyone()  # re-locked mid-transmission
                return
            if self._capped() and self.ptt.keyed and self._clock() - self._keyed_at >= CAPPED_MAX_TX_S:
                self.ptt.unkey(owner)  # antenna unverifiable: transmissions stay short
                self._rest_until = self._clock() + CAPPED_REST_S
                return
        self.ptt.heartbeat(owner)

    def unkey(self, owner: str, drain: bool = True) -> bool:
        """A client's own unkey (ladder `unkey`, proxy `T 0`): hold PTT until the TX audio still queued in the
        jitter buffer and the sound card has played out, at most PTT_TAIL_MAX_S, then drop it (REQ-AUDIO-022).
        Safety unkeys pass drain=False and drop PTT at once. Only this calling thread waits."""
        if drain and self.ptt.keyed and self.ptt.lock.owner == owner:
            self._sleep(self._tail_s())
        return self.ptt.unkey(owner)

    def _tail_s(self) -> float:
        """How long the queued TX audio needs, from the audio service; the cap when it cannot say."""
        started = self._clock()
        try:
            status = self.control_fn({"cmd": "status"}) or {}
            queued = float(status["tx_queued_ms"]) / 1000.0
        except Exception as e:  # noqa: BLE001 - no answer, old service: the bound is still a safe tail
            log.warning("tx queue unknown at unkey, holding PTT %.0f ms: %r", PTT_TAIL_MAX_S * 1000, e)
            queued = PTT_TAIL_MAX_S
        return max(0.0, min(queued, PTT_TAIL_MAX_S) - (self._clock() - started))

    # -- helpers --

    def _tone(self, on: bool) -> bool:
        """Start or stop the test tone; False when the audio service did not take the command."""
        try:
            reply = self.control_fn({"cmd": "tone", "hz": TONE_HZ, "seconds": TONE_S if on else 0})
        except Exception as e:  # noqa: BLE001 - a failed tone must never stop an unkey or a power restore
            log.warning("tone command failed: %r", e)
            return False
        return bool(reply and reply.get("ok"))

    def _heartbeat(self) -> None:
        self.ptt.heartbeat(OWNER)
        self._tone(True)  # a lost renewal only shortens the tone; the next read shows it

    def _keyed(self):
        ladder = self

        class _Key:
            def __enter__(self):
                if apsetup.unsecured():
                    raise CatError("the Wi-Fi is still open; set its passphrase first")
                if not ladder.ptt.key(OWNER):
                    raise CatError(f"transmit is in use by {ladder.ptt.lock.owner}")
                # Only once keying is allowed: on a VOX rig the tone itself keys the radio.
                if not ladder._tone(True):
                    ladder.ptt.unkey(OWNER)
                    ladder._tone(False)  # the start may have landed with only its reply lost
                    raise _NoTone

            def __exit__(self, *exc):
                # The tone stops first: a Yaesu unkeyed over CAT while audio is still going in (the
                # FT-891) takes over a second to drop out of transmit, past the PTT timeout, and the
                # bench then reported "stopped answering" after a carrier that measured fine. On a VOX
                # rig the tone is the key, so stopping it first is the unkey.
                try:
                    ladder._tone(False)
                finally:
                    ladder.ptt.unkey(OWNER)  # always, whatever happened inside
                if exc[0] is None:
                    ladder.ptt_proven = True

        return _Key()

    def _pass(self, gate: str, detail: str) -> dict:
        if gate not in self.passed:
            self.passed.append(gate)
        if gate in self.bypassed:
            self.bypassed.remove(gate)
        if gate in self._unverified:  # a bypass later proven on a re-run is no longer a caveat (REQ-WEB-034)
            self._unverified.remove(gate)
        self.details[gate] = detail
        return {**self.report(), "result": {"gate": gate, "ok": True, "detail": detail}}

    def _fail(self, gate: str, detail: str) -> dict:
        if gate in self.passed:
            self.passed.remove(gate)
        if gate == "carrier":
            self.swr_method = None
        self._drop_anyone()
        self.details[gate] = detail
        return {**self.report(), "result": {"gate": gate, "ok": False, "detail": detail}}


def _start_gain(prof) -> int:
    """The profile's TX start level, 0..100; a bad preset value falls back to the generic default."""
    g = getattr(prof, "tx_start_gain", None)
    return g if isinstance(g, int) and not isinstance(g, bool) and 0 <= g <= 100 else RadioProfile.tx_start_gain


def handle(ladder: Ladder, body: dict) -> tuple[int, dict]:
    action = body.get("action")
    try:
        if action == "confirm_load":
            ladder.confirm_load(body.get("load", ""))
            return 200, ladder.report()
        if action == "drive":
            return 200, ladder.run_drive()
        if action == "carrier":
            return 200, ladder.run_carrier(power_by_hand=body.get("power_by_hand") is True)
        if action == "meter" and isinstance(body.get("ok"), bool):
            return 200, ladder.report_meter(body["ok"])
        if action == "override":
            return 200, ladder.override(body.get("gates"))
        if action == "radio":
            return 200, ladder.radio()
        if action == "tune":
            return 200, ladder.tune(body.get("freq_hz"), body.get("mode"), listen_only=body.get("listen_only") is True)
        if action == "any_band":
            return 200, ladder.allow_any_band()
        owner = body.get("owner")
        if action in ("key", "heartbeat", "unkey") and isinstance(owner, str) and owner:
            if owner == OWNER:  # the ladder's own name: a client could feed or stop a check
                return 409, {"ok": False, "error": "that owner name is reserved"}
            # Client PTT: Ladder.key is the only door, so the gates, cap and lock all apply.
            if action == "key":
                ok = ladder.key(owner)
            elif action == "unkey":
                ok = ladder.unkey(owner)
            else:
                ladder.heartbeat(owner)
                ok = ladder.ptt.keyed and ladder.ptt.lock.owner == owner
            return (200 if ok else 409), {**ladder.report(), "ok": ok}
    except LadderError as e:
        return 409, {"ok": False, "error": str(e), **({"code": e.code} if e.code else {})}
    except CatError as e:
        return 502, {"ok": False, "error": str(e)}
    return 400, {"ok": False, "error": "unknown_action"}
