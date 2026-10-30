"""Local alerting and the status LED. Nothing here leaves the device.

The phone alerts from its open tab (sound and vibration, see alerts_page.py). The Pi
side is a beeper and one LED. The hardware is not chosen yet, so both sit behind the
smallest interface that covers every candidate: an `Output` that can be switched on
or off. `SysfsOutput` drives anything the kernel exposes as an LED-class device, which
covers the Pi's onboard ACT LED, a GPIO LED and an active buzzer (dtoverlay=gpio-led).
`NullOutput` is the default when no path is configured or the path cannot be written.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import subprocess
import threading
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from . import persist

log = logging.getLogger(__name__)

# One alert event per readiness check: the event fires when that check goes from green to red.
EVENTS = {
    "cat": "Radio control lost",
    "rx_audio": "Receive audio lost",
    "ptt": "Unexpected transmit",
    "gates": "Transmit re-locked",
    "client": "Client disconnected",
}
CHANNELS = ("sound", "vibrate", "beep")  # phone sound, phone vibration, Pi beeper
CONFIG_PATH = persist.APP_DIR / "alerts.json"

# LED states, highest priority first when several apply.
FAULT, CLIENT, AP_UP, BOOTING = "fault", "client", "ap_up", "booting"
# Patterns are (on, seconds) steps, repeated. Distinct at a glance with no legend:
# fault is a frantic flash, booting an even blink, AP up a brief wink, client connected steady on.
PATTERNS: dict[str, list[tuple[bool, float]]] = {
    BOOTING: [(True, 0.5), (False, 0.5)],
    AP_UP: [(True, 0.1), (False, 1.9)],
    CLIENT: [(True, 1.0)],
    FAULT: [(True, 0.1), (False, 0.1)],
}
BEEP: list[tuple[bool, float]] = [(True, 0.15), (False, 0.1), (True, 0.15), (False, 0.1), (True, 0.4), (False, 0.0)]

LED_PATH = os.environ.get("VECTOR_BRIDGE_LED", "")  # e.g. /sys/class/leds/ACT/brightness
BEEPER_PATH = os.environ.get("VECTOR_BRIDGE_BEEPER", "")  # e.g. /sys/class/leds/beeper/brightness
SYS_NET = Path("/sys/class/net")  # any Wi-Fi radio: the built-in one, or a USB adapter carrying the network


class Output(Protocol):
    def set(self, on: bool) -> None: ...


class NullOutput:
    def set(self, on: bool) -> None:
        pass


class SysfsOutput:
    """Writes 1 or 0 to a sysfs brightness file. A write failure is logged once, then ignored."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._warned = False

    def set(self, on: bool) -> None:
        try:
            self.path.write_text("1" if on else "0")
        except OSError as e:
            if not self._warned:
                self._warned = True
                log.warning("cannot drive %s: %s", self.path, e)


def output_for(path: str) -> Output:
    return SysfsOutput(path) if path else NullOutput()


def default_config() -> dict[str, dict[str, bool]]:
    return {e: dict.fromkeys(CHANNELS, True) for e in EVENTS}


class AlertConfig:
    """Which channels fire for which event. Unknown keys are dropped, missing ones default to on."""

    def __init__(self, path: Path = CONFIG_PATH, save: Callable[..., object] = persist.save) -> None:
        self.path, self._save = path, save
        self._mu = threading.Lock()
        self.events = default_config()
        self.save_error = ""
        try:
            self._merge(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            pass

    def _merge(self, data) -> None:
        if not isinstance(data, dict):
            return
        for event, chans in data.items():
            if event in self.events and isinstance(chans, dict):
                for ch, on in chans.items():
                    if ch in CHANNELS and isinstance(on, bool):
                        self.events[event][ch] = on

    def update(self, data) -> None:
        with self._mu:
            self._merge(data)
            try:
                self._save(self.path, json.dumps(self.events, indent=1))
                self.save_error = ""
            except (OSError, ValueError, subprocess.SubprocessError) as e:  # applies for this session even if the card refuses the write
                self.save_error = f"These settings could not be saved to the card: {e}"

    def reset(self) -> None:
        """Every channel on for every event again, and the saved choices removed."""
        with self._mu:
            self.events = default_config()
            try:
                with persist.writable(self.path):
                    self.path.unlink(missing_ok=True)
                self.save_error = ""
            except (OSError, subprocess.SubprocessError) as e:
                self.save_error = f"The old alert settings could not be removed from the card: {e}"

    def wants(self, event: str, channel: str) -> bool:
        with self._mu:
            return self.events.get(event, {}).get(channel, False)

    def report(self) -> dict:
        with self._mu:
            return {"ok": True, "events": {e: dict(c) for e, c in self.events.items()}, "titles": EVENTS,
                    "channels": list(CHANNELS), "save_error": self.save_error}


def led_state(report: dict | None, ap_up: bool) -> str:
    """Booting until the first readiness result; then fault beats client connected beats AP up."""
    if not report or not report.get("checks"):
        return BOOTING
    if report.get("fault"):
        return FAULT
    if any(c["key"] == "client" and c["state"] == "ok" for c in report["checks"]):
        return CLIENT
    return AP_UP if ap_up else BOOTING


def wlan_up(sys_net: Path = SYS_NET) -> bool:
    try:
        devs = list(sys_net.iterdir())
    except OSError:
        return False
    for dev in devs:
        try:
            if (dev / "wireless").is_dir() and (dev / "operstate").read_text().strip() == "up":
                return True
        except OSError:
            continue
    return False


class Player:
    """Plays an (on, seconds) pattern on one output from its own thread. `show` replaces the pattern."""

    def __init__(self, out: Output, name: str) -> None:
        self.out, self._name = out, name
        self._mu = threading.Lock()
        self._pattern: list[tuple[bool, float]] = []
        self._repeat = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def show(self, pattern: list[tuple[bool, float]], repeat: bool) -> None:
        with self._mu:
            if repeat and self._repeat and pattern == self._pattern:
                return  # same steady pattern: do not restart it on every poll
            self._pattern, self._repeat = list(pattern), repeat
            self._wake.set()  # under the lock, so _run never reads a pattern and then sees its own wake

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._mu:  # under the lock, so _run cannot clear this wake and then sleep through the stop
            self._stop.set()
            self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self.out.set(False)  # written here too: a light or buzzer must never be left on at exit

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._mu:
                if self._stop.is_set():
                    break
                self._wake.clear()
                pattern, repeat = self._pattern, self._repeat
                if not repeat:
                    self._pattern = []
            for on, seconds in pattern:
                self.out.set(on)
                if self._wake.wait(seconds):
                    break
            else:
                if not (repeat and pattern):
                    self.out.set(False)
                    self._wake.wait()
        self.out.set(False)


class Alerter:
    """Turns readiness reports into the LED state and beeps. `step` is the whole logic; `start` just loops it."""

    def __init__(self, report_fn: Callable[[], dict], config: AlertConfig, led: Output | None = None,
                 beeper: Output | None = None, ap_up_fn: Callable[[], bool] = wlan_up, interval_s: float = 1.0) -> None:
        self._report_fn, self.config, self._ap_up_fn, self._interval = report_fn, config, ap_up_fn, interval_s
        self.led = Player(led if led is not None else output_for(LED_PATH), "status-led")
        self.beeper = Player(beeper if beeper is not None else output_for(BEEPER_PATH), "beeper")
        self.state = BOOTING
        self._seen_seq: int | None = None
        self._stop = threading.Event()

    def step(self) -> None:
        try:
            report = self._report_fn()
        except Exception:  # noqa: BLE001 - the LED thread must outlive any report failure
            report = None
        self.state = led_state(report, self._ap_up_fn())
        self.led.show(PATTERNS[self.state], repeat=True)
        if not report:
            return
        seq = report.get("alert_seq", 0)
        if self._seen_seq is not None and seq > self._seen_seq and any(
                self.config.wants(c["key"], "beep") for c in report.get("alert", [])):
            self.beeper.show(BEEP, repeat=False)
        self._seen_seq = seq

    def start(self) -> None:
        self.led.start()
        self.beeper.start()

        def loop() -> None:
            while not self._stop.is_set():
                self.step()
                self._stop.wait(self._interval)
        threading.Thread(target=loop, name="alerter", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.led.stop()
        self.beeper.stop()


def icon_png(size: int = 180, rgb: tuple[int, int, int] = (10, 132, 255)) -> bytes:
    """A plain square home-screen icon, built here so no binary file ships in the repo."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = (b"\x00" + bytes(rgb) * size) * size
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


MANIFEST = {"name": "VectorBridge", "short_name": "VectorBridge", "start_url": "/", "display": "standalone",
            "background_color": "#12151a", "theme_color": "#12151a",
            "icons": [{"src": "/icon.png", "sizes": "180x180", "type": "image/png"}]}
