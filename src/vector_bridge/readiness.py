"""Readiness check: could I pass traffic right now?

This is the home screen, not a gate. Five checks run in a fixed order and nothing here
ever keys the radio, so the screen is safe to open while someone is listening. Every
non-green result carries its likely fix in plain language. A monitor re-runs the checks
in the background and raises an alert only when a check that was green turns red.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from .cat import CatError

log = logging.getLogger(__name__)

OK, WARN, FAIL = "ok", "warn", "fail"
READY, CAVEATS, NOT_READY = "ready", "ready_with_caveats", "not_ready"
VERDICT_TEXT = {READY: "Ready", CAVEATS: "Ready with caveats", NOT_READY: "Not ready"}

RX_FLOOR_RMS = 30  # below this (of 32767) the input is digital silence: nothing is plugged in
HISTORY = 20  # status samples kept for the "visibly moving" and "stream flowing" checks

# Most urgent first: which fault owns the alert and the top of the home page when several are red.
# "battery" comes from /api/battery and "wifi_open" from /api/wifi; the rest are check keys here.
FAULT_PRIORITY = ("ptt", "cat", "gates", "rx_audio", "battery", "client", "wifi_open")


UNEXPECTED_TX = "The radio is transmitting and the bridge did not ask it to."


def by_priority(key: str) -> int:
    return FAULT_PRIORITY.index(key) if key in FAULT_PRIORITY else len(FAULT_PRIORITY)


def rank(c: Check) -> float:
    """A PTT that cannot be read follows lost radio control; only a real unexpected transmit ranks first."""
    if c.key == "ptt" and c.detail != UNEXPECTED_TX:
        return by_priority("cat") + 0.5
    return by_priority(c.key)


@dataclass
class Check:
    key: str
    title: str
    state: str
    detail: str
    fix: str = ""  # plain-language likely fix; empty only when state is ok


@dataclass
class Inputs:
    """Everything the checks look at, gathered without transmitting."""

    statuses: list[dict] = field(default_factory=list)  # recent service status replies, oldest first
    service_up: bool = False
    cat_alive: bool = False
    ptt_state: bool | None = None  # radio's PTT read back over CAT; None when it cannot be read
    tx_owner: str | None = None  # who holds the TX lock, if anyone
    tx_just_released: bool = False  # the bridge unkeyed moments ago; a PTT read-back may not show it yet
    ptt_proven: bool = False  # set once the transmit ladder has keyed and released the rig
    gates: dict | None = None  # {"passed": bool, "bypassed": [names]} from the ladder; None before it exists
    vox: bool = False  # the kit keys by VOX: no radio control, and keying cannot be read back
    no_cat: bool = False  # no radio control, keyed on an RTS or DTR line through rigctld's dummy rig


def check_cat(i: Inputs) -> Check:
    if i.vox:
        return Check("cat", "Radio control (CAT)", OK, "Not used: this kit keys by VOX.")
    if i.no_cat:
        if i.cat_alive:
            return Check("cat", "Radio control (CAT)", OK, "Not used: this kit has no radio control and keys on a serial line.")
        return Check("cat", "Radio control (CAT)", FAIL, "The serial line that keys the radio is not open.",
                     "Check the interface's USB cable at the Pi. If more than one USB serial device is "
                     "plugged in, choose the interface's port in setup.")
    if i.cat_alive:
        return Check("cat", "Radio control (CAT)", OK, "The radio answers on CAT.")
    return Check("cat", "Radio control (CAT)", FAIL, "The radio is not answering on CAT.",
                 "Check the cable is in the radio's data or USB jack, not the microphone jack. "
                 "Then check the radio is switched on and its CAT baud rate matches the kit.")


def check_rx_audio(i: Inputs) -> Check:
    title = "Receive audio"
    if not i.service_up or not i.statuses:
        return Check("rx_audio", title, FAIL, "The audio service is not running.",
                     "Wait a few seconds after power-up. If it stays red, the USB audio interface "
                     "is probably unplugged: reseat its USB cable at the Pi.")
    levels = [s.get("rx_rms", 0) for s in i.statuses]
    if max(levels) < RX_FLOOR_RMS:
        return Check("rx_audio", title, FAIL, "No receive audio is arriving from the radio.",
                     "Check the audio cable is in the radio's data or headphone jack, not the "
                     "microphone jack, and turn the radio's volume or data output level up.")
    if len(levels) < 3:
        return Check("rx_audio", title, WARN, "Receive audio is present; checking that the level moves.",
                     "Still measuring: this settles within a few seconds of the audio service starting.")
    if max(levels[-5:]) == min(levels[-5:]):
        return Check("rx_audio", title, WARN, "Receive audio is present but the level is not moving.",
                     "A frozen level usually means a stuck interface. Unplug and replug the USB "
                     "audio interface, and check the radio is not muted or squelched.")
    return Check("rx_audio", title, OK, "Receive audio is present and the level is moving.")


def check_ptt(i: Inputs) -> Check:
    title = "Transmit keying (PTT)"
    if i.vox:
        # The home page reads the word "transmitting" as "keyed now" and pauses receive, so it is
        # said only while the bridge holds transmit.
        if i.tx_owner is not None:
            return Check("ptt", title, OK, f"Transmitting now for {i.tx_owner}.")
        return Check("ptt", title, WARN, "Keying is by VOX, so the bridge cannot read the radio's transmit state.",
                     "Turn VOX on at the radio, and watch its transmit light on the first transmission.")
    if i.ptt_state is None:
        return Check("ptt", title, FAIL, "The radio's transmit state cannot be read.",
                     "PTT is checked over CAT, so fix radio control first. If CAT is green, this "
                     "radio may need a separate PTT cable: run setup and pick the PTT method.")
    if i.ptt_state and i.tx_owner is None and i.tx_just_released:
        # PTT was read while the bridge was keyed, or rigctld has not caught up with the unkey.
        return Check("ptt", title, OK, "The bridge has just released transmit.")
    if i.ptt_state and i.tx_owner is None:
        return Check("ptt", title, FAIL, UNEXPECTED_TX,
                     "Something else is keying the radio. Check VOX is off, and that no hand "
                     "microphone or footswitch is pressed or shorted.")
    if i.ptt_state:
        return Check("ptt", title, OK, f"Transmitting now for {i.tx_owner}.")
    if not i.ptt_proven:
        return Check("ptt", title, WARN, "The radio reports receive, but keying has not been tested this session.",
                     "Keying is only proven by the transmit checks in setup, which ask before "
                     "they key. This screen never transmits.")
    return Check("ptt", title, OK, "The radio keyed and released when last tested.")


def _names(gates: list[str]) -> str:
    return ", ".join("ham band check" if x == "band" else x for x in gates)


def check_gates(i: Inputs) -> Check:
    title = "Transmit safety checks"
    g = i.gates
    if g is None or g.get("tx_allowed") is False or not (g.get("passed") or g.get("bypassed")):
        was = " Earlier this session you bypassed " + _names(g["bypassed"]) + "." if g and g.get("bypassed") else ""
        return Check("gates", title, WARN, "Transmit is locked: the rig has not been proven yet." + was,
                     "Run the transmit checks in setup: confirm the antenna or dummy load, set the "
                     "audio drive, then a brief low-power carrier.")
    if g.get("bypassed"):
        return Check("gates", title, WARN, " ".join(["Rig unverified: bypassed " + _names(g["bypassed"]) + ".",
                                                     *g.get("caveats", [])]),
                     "You chose to skip these checks. Run them from setup when you can; the rig "
                     "stays marked unverified for the rest of this session.")
    if g.get("caveats"):
        return Check("gates", title, WARN, "Transmit checks passed, with a limit. " + " ".join(g["caveats"]),
                     "Use a radio or meter that reports SWR if you can. Until then keep power low "
                     "and transmissions short.")
    return Check("gates", title, OK, "All transmit checks passed.")


def check_client(i: Inputs) -> Check:
    title = "Client audio stream"
    fix = ("Open the client app on the iPad or laptop, check it is joined to this bridge's "
           "Wi-Fi, and start its audio connection.")
    if not i.service_up or not i.statuses or not i.statuses[-1].get("peer"):
        return Check("client", title, FAIL, "No client is connected.", fix)
    sent = [s.get("rx_packets_sent", 0) for s in i.statuses][-3:]
    if len(sent) < 2 or sent[-1] < sent[0]:  # too few samples, or the counter reset on a service restart
        return Check("client", title, WARN, "A client is connected; checking that audio is flowing.",
                     "Still measuring: this settles within a few seconds of the audio service starting.")
    if sent[-1] == sent[0]:
        return Check("client", title, FAIL, "A client is connected but no audio is flowing to it.", fix)
    return Check("client", title, OK, f"Audio is reaching {i.statuses[-1]['peer']}.")


CHECKS: tuple[Callable[[Inputs], Check], ...] = (check_cat, check_rx_audio, check_ptt, check_gates, check_client)


def run_checks(i: Inputs) -> list[Check]:
    return [c(i) for c in CHECKS]


def verdict(checks: list[Check]) -> str:
    states = {c.state for c in checks}
    return NOT_READY if FAIL in states else CAVEATS if WARN in states else READY


def regressions(green: set[str], after: list[Check]) -> list[Check]:
    """Checks that had been green and are now red. Only these interrupt the operator.

    `green` is sticky: a check stays in it through amber, so green, amber, red still
    interrupts. It is updated in place: red leaves the set, green (re)joins it.
    """
    newly_red = [c for c in after if c.state == FAIL and c.key in green]
    green.difference_update(c.key for c in after if c.state == FAIL)
    green.update(c.key for c in after if c.state == OK)
    return newly_red


class Monitor:
    """Re-runs the checks quietly; `alert_seq` bumps only on a green-to-red change."""

    def __init__(self, gather: Callable[[deque], Inputs], interval_s: float = 2.0) -> None:
        self._gather = gather
        self._interval = interval_s
        self._statuses: deque = deque(maxlen=HISTORY)
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()  # one poll at a time: HTTP threads may call poll()
        self._green: set[str] = set()
        self._checks: list[Check] = []
        self._alert_seq = 0
        self._alert: list[Check] = []
        self._fault: set[str] = set()  # regressed checks that are still red: drives the fault LED
        self._stop = threading.Event()
        self.on_alert: Callable[[list[Check]], None] | None = None  # runs in its own thread on a new alert

    def poll(self) -> None:
        with self._poll_lock:
            self._poll()

    def _poll(self) -> None:
        checks = run_checks(self._gather(self._statuses))
        with self._lock:
            newly_red = regressions(self._green, checks)
            if newly_red:
                self._alert_seq += 1
                self._alert = newly_red
                if self.on_alert is not None:  # a slow journal read must not hold up the next poll
                    threading.Thread(target=self.on_alert, args=(newly_red,), daemon=True).start()
            self._fault.update(c.key for c in newly_red)
            self._fault.intersection_update(c.key for c in checks if c.state == FAIL)
            self._checks = checks

    def report(self) -> dict:
        with self._lock:
            checks, seq = list(self._checks), self._alert_seq
            alert = sorted(self._alert, key=rank)
            now = {c.key: c for c in self._checks}
            fault = sorted(self._fault, key=lambda k: rank(now[k]) if k in now else by_priority(k))
        v = verdict(checks) if checks else NOT_READY
        return {"ok": True, "verdict": v, "verdict_text": VERDICT_TEXT[v],
                "checks": [asdict(c) for c in checks],
                "alert_seq": seq, "alert": [asdict(c) for c in alert], "fault": fault}

    def state_of(self, key: str) -> str | None:
        """The latest state of one check, or None before the first poll."""
        with self._lock:
            return next((c.state for c in self._checks if c.key == key), None)

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.poll()
                except Exception:
                    log.exception("readiness poll failed")
                self._stop.wait(self._interval)
        threading.Thread(target=loop, name="readiness", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def make_gather(control_fn: Callable[[dict], dict | None], cat=None, ladder: Callable[[], dict] | None = None,
                vox_fn: Callable[[], bool] | None = None, no_cat_fn: Callable[[], bool] | None = None):
    """Build the production gather: service status over the control channel, CAT via rigctld.

    Only reads are issued on CAT (`alive`, `get_ptt`); set_ptt is never called from here.
    `ladder` is the area 5 hook returning {"gates": ..., "ptt_proven": ..., "tx_owner": ...}.
    """

    def gather(history: deque) -> Inputs:
        status = control_fn({"cmd": "status"})
        if status is not None:  # a dropped request must not wipe the history: that turned red into green
            history.append(status)
        vox = bool(vox_fn and vox_fn())
        alive = bool(cat and not vox and cat.alive())  # a VOX kit has no rigctld to ask
        ptt = None
        if alive:
            try:
                ptt = cat.get_ptt()
            except (CatError, KeyError, ValueError):
                ptt = None
        extra = ladder() if ladder else {}
        return Inputs(statuses=list(history), service_up=status is not None, cat_alive=alive,
                      ptt_state=ptt, tx_owner=extra.get("tx_owner"),
                      tx_just_released=bool(extra.get("tx_just_released")),
                      ptt_proven=bool(extra.get("ptt_proven")), gates=extra.get("gates"), vox=vox,
                      no_cat=not vox and bool(no_cat_fn and no_cat_fn()))

    return gather
