"""Control channel request handling: one JSON object in, one JSON object out.

Kept free of sockets so it is unit-testable; the service wires it to UDP.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Callable

from . import tone, txcap
from .config import RIGCTL_ACCESS
from .log import error
from .protocol import CHANNELS, SAMPLE_RATE, VERSION

_HEX4 = re.compile(r"[0-9A-Fa-f]{4}")
_USB_PATH = re.compile(r"\d+-\d+(\.\d+)*")
# They change settings, which need the admin PIN (roadmap 1.2, R-D3). Over UDP only the tuner and the CLI
# on the Pi may send them; a remote app uses their PIN-gated HTTP twins (DOCS/web-api.md).
LOCAL_ONLY_CMDS = frozenset({"set_gain", "set_tx_audio_cap", "select_audio", "set_rigctl_access"})


def _loopback(addr: tuple) -> bool:
    # Linux drops a packet from the network that claims a 127/8 source, so the address can be trusted.
    try:
        return ipaddress.ip_address(addr[0]).is_loopback
    except (ValueError, TypeError, IndexError):
        return False


def parse(data: bytes) -> dict | None:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


class ControlHandler:
    def __init__(
        self,
        device_name: str,
        audio_port: int,
        status_fn: Callable[[], dict],
        set_gain_fn: Callable[[int | None, int | None], dict],
        register_fn: Callable[[tuple, str], None],
        tone_fn: Callable[[float, float], dict] | None = None,
        set_tx_audio_cap_fn: Callable[[int], dict] | None = None,
        monitor_fn: Callable[[tuple, str], str | None] | None = None,
        select_audio_fn: Callable[[str, str, str, bool], dict] | None = None,
        set_rigctl_access_fn: Callable[[str], dict] | None = None,
    ) -> None:
        self.device_name = device_name
        self.audio_port = audio_port
        self.status_fn = status_fn
        self.set_gain_fn = set_gain_fn
        self.register_fn = register_fn
        self.tone_fn = tone_fn
        self.set_tx_audio_cap_fn = set_tx_audio_cap_fn
        self.monitor_fn = monitor_fn
        self.select_audio_fn = select_audio_fn
        self.set_rigctl_access_fn = set_rigctl_access_fn

    def _stream(self) -> dict:
        return {
            "ok": True,
            "device": self.device_name,
            "protocol": VERSION,
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "audio_port": self.audio_port,
        }

    def handle(self, req: dict | None, addr: tuple) -> dict:
        if not req or not isinstance(req.get("cmd"), str):
            return {"ok": False, "error": "bad_request"}
        cmd = req["cmd"]
        if cmd in LOCAL_ONLY_CMDS and not _loopback(addr):
            return {"ok": False, "error": "pin_required"}
        if cmd == "hello":
            self.register_fn(addr, str(req.get("client", "")))
            return self._stream()
        if cmd == "monitor" and self.monitor_fn is not None:
            # listen-only: RX audio without taking the peer slot; its TX audio is dropped
            err = self.monitor_fn(addr, str(req.get("client", "")))
            return {"ok": False, "error": err} if err else {**self._stream(), "role": "monitor"}
        if cmd == "status":
            return {"ok": True, **self.status_fn()}
        if cmd == "set_gain":
            tx, rx = req.get("tx"), req.get("rx")
            if tx is None and rx is None:
                return {"ok": False, "error": "bad_request"}
            for v in (tx, rx):
                if v is not None and not (isinstance(v, (int, float)) and 0 <= v <= 100):
                    return {"ok": False, "error": "bad_gain"}
            try:
                applied = self.set_gain_fn(None if tx is None else int(tx), None if rx is None else int(rx))
            except Exception as e:  # noqa: BLE001 - the client is owed a reply either way
                error("set_gain_failed", reason=repr(e))
                return {"ok": False, "error": "gain_failed"}
            return {"ok": True, **applied}
        if cmd == "tone" and self.tone_fn is not None:
            hz, seconds = req.get("hz", 1500), req.get("seconds")
            for v in (hz, seconds):
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    return {"ok": False, "error": "bad_request"}
            if not (tone.MIN_HZ <= hz <= tone.MAX_HZ and 0 <= seconds <= tone.MAX_S):
                return {"ok": False, "error": "bad_tone"}
            return {"ok": True, **self.tone_fn(float(hz), float(seconds))}
        if cmd == "set_tx_audio_cap" and self.set_tx_audio_cap_fn is not None:
            seconds = req.get("seconds")
            if isinstance(seconds, bool) or not isinstance(seconds, int) \
                    or not 0 <= seconds <= txcap.MAX_CAP_S:
                return {"ok": False, "error": "bad_tx_audio_cap"}
            return {"ok": True, **self.set_tx_audio_cap_fn(seconds)}
        if cmd == "select_audio" and self.select_audio_fn is not None:
            # Setup picks the sound card: USB ids (hex) and position; all empty = the first USB card.
            # strict: only the card at that position will do (it has an identical twin).
            vid, pid, path = (req.get(k, "") for k in ("vendor", "product", "usb_path"))
            strict = req.get("strict", False)
            if not all(isinstance(v, str) for v in (vid, pid, path)) or not isinstance(strict, bool) \
                    or not all(_HEX4.fullmatch(v) for v in (vid, pid) if v) or (path and not _USB_PATH.fullmatch(path)):
                return {"ok": False, "error": "bad_audio_card"}
            return {"ok": True, **self.select_audio_fn(vid.lower(), pid.lower(), path, strict and bool(path))}
        if cmd == "set_rigctl_access" and self.set_rigctl_access_fn is not None:
            # Saved here because this service owns the config file; the tuner moves the 4532 listeners.
            access = req.get("access")
            if access not in RIGCTL_ACCESS:
                return {"ok": False, "error": "bad_rigctl_access"}
            return {"ok": True, **self.set_rigctl_access_fn(access)}  # a failed save replies ok false
        return {"ok": False, "error": "unknown_cmd"}

    def handle_bytes(self, data: bytes, addr: tuple) -> bytes:
        return json.dumps(self.handle(parse(data), addr), separators=(",", ":")).encode()
