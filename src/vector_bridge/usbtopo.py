"""Where a serial port or sound card sits on the USB bus, from Linux sysfs.

A radio with its own USB socket (IC-705, QMX) puts its serial port and its sound card on
one USB device; a Digirig puts both behind its own small hub. So the sound card that
shares a USB device, or failing that a hub, with the radio's serial port is the radio's.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# A USB device in sysfs: bus-port(.port)*, e.g. "1-1" or "1-1.3.2". Interfaces add ":1.0".
_DEVICE = re.compile(r"\d+-\d+(\.\d+)*")
SYS_TTY = Path("/sys/class/tty")
SYS_SOUND = Path("/sys/class/sound")


def device_of(link: Path) -> str:
    """The USB device a sysfs ``device`` link resolves under, or "" when it is not on USB."""
    try:
        resolved = Path(os.path.realpath(link))
    except OSError:
        return ""
    return next((p for p in reversed(resolved.parts) if _DEVICE.fullmatch(p)), "")


def serial_device(dev: str, sys_tty: Path = SYS_TTY) -> str:
    """The USB device of a serial port such as /dev/serial/by-id/... or /dev/ttyACM0."""
    if not dev:
        return ""
    return device_of(sys_tty / Path(os.path.realpath(dev)).name / "device")


def card_device(index: int, sys_sound: Path = SYS_SOUND) -> str:
    """The USB device of ALSA card ``index``."""
    return device_of(sys_sound / f"card{index}" / "device")


def _parent(dev: str) -> str:
    return dev.rsplit(".", 1)[0] if "." in dev else ""


def closeness(a: str, b: str) -> int:
    """2 on the same USB device, 1 behind the same hub, else 0."""
    if not a or not b:
        return 0
    if a == b:
        return 2
    return 1 if _parent(a) and _parent(a) == _parent(b) else 0


def best_match(serial: str, cards: list, path_of=lambda c: c.usb_path):
    """The one card closest to the serial device, or None when none is close or two tie."""
    scored = sorted(((closeness(serial, path_of(c)), i) for i, c in enumerate(cards)), reverse=True)
    if not scored or scored[0][0] == 0 or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None
    return cards[scored[0][1]]
