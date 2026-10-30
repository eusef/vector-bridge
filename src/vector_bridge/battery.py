"""PiSugar battery level over I2C, for the home page. Optional hardware: absent means hidden.

Reads the board directly rather than through pisugar-server: the server cannot tell whether a
board is fitted (its model comes from the install flag), and it wants to write /etc, which is
read-only here. Registers and voltage curves are from PiSugar's own pisugar-power-manager-rs.

PiSugar 3 (0x57) keeps its own percentage in a register. PiSugar 2 and 2 Pro (0x75) only
report voltage, which is mapped through the vendor's curve for that chip. PiSugar S has no
data connection and reads as absent.
"""

from __future__ import annotations

import ctypes
import fcntl
import logging
import os
import threading
from collections import deque

BUS = "/dev/i2c-1"

I2C_SLAVE = 0x0703
I2C_SMBUS = 0x0720
I2C_SMBUS_READ = 1
I2C_SMBUS_BYTE_DATA = 2

PISUGAR3 = 0x57
PISUGAR2 = 0x75

# (volts, percent), highest first. pisugar-core ip5209.rs and ip5312.rs BATTERY_CURVE.
IP5209_CURVE = [(4.16, 100.0), (4.05, 95.0), (4.00, 80.0), (3.92, 65.0), (3.86, 40.0),
                (3.79, 25.5), (3.66, 10.0), (3.52, 6.5), (3.49, 3.2), (3.1, 0.0)]
IP5312_CURVE = [(4.10, 100.0), (4.05, 95.0), (3.90, 88.0), (3.80, 77.0), (3.70, 65.0),
                (3.62, 55.0), (3.58, 49.0), (3.49, 25.6), (3.32, 4.5), (3.1, 0.0)]
# A reading outside this is not a lithium cell, so the registers belong to the other chip.
PLAUSIBLE_V = (3.0, 4.5)

ABSENT = {"ok": True, "present": False}

# On battery (not plugged in): warn at each of these, lock transmit at LOCK_PCT. SHUTDOWN_PCT is the
# decided shutdown level; nothing here shuts the bridge down yet (no shutdown hook exists).
WARN_PCT = (20, 10)
LOCK_PCT = 6
SHUTDOWN_PCT = 3
OK, LOW, CRITICAL, TX_LOCKED = "ok", "low", "critical", "tx_locked"

log = logging.getLogger(__name__)


class _SmbusData(ctypes.Union):
    _fields_ = [("byte", ctypes.c_uint8), ("block", ctypes.c_uint8 * 34)]


class _SmbusIoctl(ctypes.Structure):
    _fields_ = [("read_write", ctypes.c_uint8), ("command", ctypes.c_uint8),
                ("size", ctypes.c_uint32), ("data", ctypes.POINTER(_SmbusData))]


class SMBus:
    """Just SMBus read-byte-data through the kernel's i2c-dev, so no smbus2 dependency."""

    def __init__(self, path: str = BUS) -> None:
        self.fd = os.open(path, os.O_RDWR)

    def read_byte_data(self, addr: int, reg: int) -> int:
        fcntl.ioctl(self.fd, I2C_SLAVE, addr)
        data = _SmbusData()
        fcntl.ioctl(self.fd, I2C_SMBUS, _SmbusIoctl(I2C_SMBUS_READ, reg, I2C_SMBUS_BYTE_DATA, ctypes.pointer(data)))
        return data.byte

    def close(self) -> None:
        os.close(self.fd)


def level_from_voltage(volts: float, curve: list[tuple[float, float]]) -> float:
    """Linear between curve points, as pisugar-core convert_battery_voltage_to_level does."""
    for i, (v_low, l_low) in enumerate(curve):
        if volts >= v_low:
            if i == 0:
                return l_low
            v_high, l_high = curve[i - 1]
            return l_low + (volts - v_low) / (v_high - v_low) * (l_high - l_low)
    return 0.0


def _pisugar3(read_byte) -> dict | None:
    percent = read_byte(PISUGAR3, 0x2A)
    if percent > 100:
        return None
    plugged = bool(read_byte(PISUGAR3, 0x02) & 0x80)  # CTR1 bit 7: external power present
    return {"ok": True, "present": True, "model": "PiSugar 3", "percent": percent, "plugged": plugged}


def _pisugar2(read_byte) -> dict | None:
    # IP5209 (PiSugar 2): 14-bit ADC in 0xa2/0xa3; bit 13 set means negative, never a real cell.
    low, high = read_byte(PISUGAR2, 0xA2), read_byte(PISUGAR2, 0xA3)
    volts = (2600 + (((high & 0x1F) << 8) | low) * 0.26855) / 1000
    if not high & 0x20 and PLAUSIBLE_V[0] <= volts <= PLAUSIBLE_V[1]:
        model, curve = "PiSugar 2", IP5209_CURVE
    else:  # IP5312 (PiSugar 2 Pro): 0xd0/0xd1
        low, high = read_byte(PISUGAR2, 0xD0), read_byte(PISUGAR2, 0xD1)
        volts = (2600 + (((high & 0x3F) << 8) | low) * 0.26855) / 1000
        if (low, high) == (0, 0) or not PLAUSIBLE_V[0] <= volts <= PLAUSIBLE_V[1]:
            return None
        model, curve = "PiSugar 2 Pro", IP5312_CURVE
    # No plugged-in register without reconfiguring the chip's GPIO; PlugWatch infers it from voltage.
    return {"ok": True, "present": True, "model": model,
            "percent": round(level_from_voltage(volts, curve)), "plugged": None, "volts": round(volts, 3)}


def probe(read_byte) -> dict:
    """First board that answers with sane values, else ABSENT. `read_byte(addr, reg)` raises OSError on no ACK."""
    for board in (_pisugar3, _pisugar2):
        try:
            found = board(read_byte)
        except OSError:
            continue
        if found:
            return found
    return dict(ABSENT)


def read(path: str = BUS) -> dict:
    """The /api/battery reply. No I2C bus (disabled, no permission, not a Pi) reads as absent."""
    try:
        bus = SMBus(path)
    except OSError:
        return dict(ABSENT)
    try:
        return probe(bus.read_byte_data)
    finally:
        bus.close()


def level(report: dict) -> str:
    """Where the charge sits against the thresholds. External power, or no board, is always ok."""
    pct = report.get("percent")
    if not report.get("present") or report.get("plugged") or not isinstance(pct, (int, float)):
        return OK
    if pct <= LOCK_PCT:
        return TX_LOCKED
    if pct <= WARN_PCT[1]:
        return CRITICAL
    if pct <= WARN_PCT[0]:
        return LOW
    return OK


def tx_locked(report: dict) -> bool:
    return level(report) == TX_LOCKED


def assess(report: dict) -> dict:
    """The /api/battery reply with the threshold state added: `level` and `tx_locked`. No board: unchanged."""
    if not report.get("present"):
        return report
    lv = level(report)
    return {**report, "level": lv, "tx_locked": lv == TX_LOCKED}


class PlugWatch:
    """Plugged-in state for PiSugar 2 boards, inferred from steps in the cell voltage.

    Bench 2026-09-24 (PiSugar 2 on a Zero 2 W): USB power arriving stepped the voltage +70 mV
    within one second and leaving stepped it -71 mV, against about 3 mV of noise. With the cell
    full (4.18 V) the steps shrink to +35 and -28 mV, so the threshold sits at 15 mV. The battery
    current register is no help: it read negative both on USB power and on battery.
    Until the first step the state is unknown (None), so a tuner started on USB power says
    nothing about power until it changes. PiSugar 3 reports its own bit and is left alone.
    """

    STEP_MV = 15

    def __init__(self, read_fn=read, interval_s: float = 2.0, window: int = 5) -> None:
        self._read = read_fn
        self._interval = interval_s
        self._recent: deque = deque(maxlen=window)
        self.plugged: bool | None = None
        self._stop = threading.Event()

    def sample(self) -> None:
        volts = self._read().get("volts")
        if volts is None:
            self._recent.clear()
            return
        if len(self._recent) == self._recent.maxlen:
            step_mv = (volts - sum(self._recent) / len(self._recent)) * 1000
            if abs(step_mv) >= self.STEP_MV:
                self.plugged = step_mv > 0
        self._recent.append(volts)

    def report(self) -> dict:
        """The /api/battery reply, with the inferred state filled in where the board has none."""
        r = self._read()
        if r.get("present") and r.get("plugged") is None:
            r["plugged"] = self.plugged
        return r

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.sample()
                except Exception:
                    log.exception("battery sample failed")
                self._stop.wait(self._interval)
        threading.Thread(target=loop, name="battery", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
