"""Configuration: /etc/vector-bridge/config.toml with documented defaults."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import persist
from .protocol import DEFAULT_AUDIO_PORT, DEFAULT_CONTROL_PORT

DEFAULT_PATH = Path(os.environ.get("VECTOR_BRIDGE_CONFIG", "/etc/vector-bridge/config.toml"))
log = logging.getLogger(__name__)
RIGCTL_ACCESS = ("off", "ap", "wifi", "all")  # who reaches the rigctl safety proxy on 4532: this Pi, + the AP, + any Wi-Fi, + every network


@dataclass
class Config:
    device_name: str = "VectorBridge"
    audio_port: int = DEFAULT_AUDIO_PORT
    control_port: int = DEFAULT_CONTROL_PORT
    usb_vendor_id: str = ""  # hex, e.g. "0d8c"; empty = first USB audio card found
    usb_product_id: str = ""
    audio_usb_path: str = ""  # USB position of the card, e.g. "1-1.2", set by setup; empty = by id alone
    audio_usb_strict: bool = False  # true: the card must be at audio_usb_path (it had an identical twin)
    tx_gain: int = 20  # percent, conservative so the KX2 mic input is never slammed at boot
    rx_gain: int = 50  # percent
    jitter_target_ms: int = 100
    jitter_max_ms: int = 400
    rigctl_access: str = "wifi"  # one of RIGCTL_ACCESS; R-D9
    tx_audio_cap_s: int = 120  # the Transmit Time Limit: seconds of TX audio without a pause before it is muted, and the 4532 key cap; 0 = off

    def as_dict(self) -> dict:
        return asdict(self)


COMMENTS = {
    "device_name": "Name reported to clients in the hello reply.",
    "audio_port": "UDP port for audio packets.",
    "control_port": "UDP port for JSON control requests.",
    "usb_vendor_id": "USB vendor id (hex) of the sound card; empty picks the first USB audio card.",
    "usb_product_id": "USB product id (hex) of the sound card.",
    "audio_usb_path": "USB position of the sound card (e.g. 1-1.2), set by setup to tell identical cards apart; empty = by id.",
    "audio_usb_strict": "true when the sound card had an identical twin when chosen: only the card at audio_usb_path is used. false: a card with the same id in another socket is used too.",
    "tx_gain": "Playback (to KX2 mic) mixer level, 0-100. Starts low on purpose; raise during calibration.",
    "rx_gain": "Capture (from KX2 phones) mixer level, 0-100.",
    "jitter_target_ms": "TX jitter buffer priming depth in milliseconds. 100 rides out the 100 to 130 ms stalls seen on Pi Zero 2 W Wi-Fi; 30 was measured to underrun.",
    "jitter_max_ms": "TX jitter buffer maximum depth before old audio is dropped.",
    "rigctl_access": "Where port 4532 answers; there an app logged in with the admin PIN (VectorCable, relaying WSJT-X and JS8Call) tunes the radio and keys it through the bridge's safety proxy (keying passes the transmit checks, the transmit lock and the Transmit Time Limit as a key cap; a client that disconnects is unkeyed). \"off\": only programs on the Pi. \"ap\": also devices on the bridge's own Wi-Fi. \"wifi\": devices on whichever Wi-Fi the Pi is on, its own or a joined one, following a switch; never the tailnet. \"all\": every network the Pi is on. Every client but the Pi itself must first send \"\\vb_auth <app token>\" (VectorCable does it for WSJT-X and JS8Call). Settable live under Radio, Advanced.",
    "tx_audio_cap_s": "Seconds of transmit audio without a 1 s pause before the bridge mutes it, 0 to 600; 0 turns the limit off. The same number caps how long a desktop app may hold a key through port 4532. A VOX rig keys on the audio, so this is its only time limit. 120 fits FT8, FT4, every JS8 speed, JT65 and JT9 (about 50 s) and WSPR (about 110 s); long fldigi messages may need more. Settable from the home page's Transmit Time Limit sheet.",
}


UNREADABLE = (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError)


def problem(path: Path | None = None) -> str | None:
    """Why the config file cannot be read, or None when it reads or does not exist."""
    path = path or DEFAULT_PATH
    try:
        if path.exists():
            tomllib.loads(path.read_text())
    except UNREADABLE as e:
        return str(e)
    return None


def load(path: Path | None = None) -> Config:
    path = path or DEFAULT_PATH
    cfg = Config()
    try:
        if not path.exists():
            return cfg
        data = tomllib.loads(path.read_text())
    except UNREADABLE as e:
        # A damaged file must not crash-loop the audio service and the web UI: run on the
        # defaults (low transmit level, rig control on this bridge's Wi-Fi only) and say so.
        log.warning("config %s cannot be read, using the defaults: %s", path, e)
        return cfg
    if "rigctl_access" not in data and "rigctld_network" in data:
        # Before 2026-10: a bool, every card wrote false (loopback, the only safe choice then) and true meant every interface.
        data["rigctl_access"] = "all" if data["rigctld_network"] is True else "ap"
    for f in fields(Config):
        if f.name in data:
            setattr(cfg, f.name, data[f.name])
    return cfg


def dumps(cfg: Config) -> str:
    lines = ["# VectorBridge configuration", ""]
    for f in fields(Config):
        v = getattr(cfg, f.name)
        lines.append(f"# {COMMENTS[f.name]}")
        lines.append(f"{f.name} = {_toml_value(v)}")
        lines.append("")
    return "\n".join(lines)


def save(cfg: Config, path: Path | None = None) -> Path:
    path = path or DEFAULT_PATH
    mode = gid = None
    if path.exists():
        # Keep the installed mode and group (664, audio) so the service user and the
        # admin user can both keep writing this file after either one saves it.
        st = path.stat()
        mode, gid = st.st_mode & 0o777, st.st_gid
        if problem(path) is not None:
            try:  # keep the damaged file beside the fresh one for a bug report
                persist.save(path.with_name(path.name + ".bad"), path.read_bytes(), mode=mode, gid=gid)
            except OSError as e:
                log.warning("could not keep a copy of the unreadable config %s: %s", path, e)
    return persist.save(path, dumps(cfg), mode=mode, gid=gid)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
