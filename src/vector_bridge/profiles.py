"""Radio profiles: what a rig is, how it is wired, how CAT and PTT reach it.

Built-ins cover the field kit. User presets are TOML files in a presets directory and
override a built-in with the same name. Autodetection elsewhere may *suggest* a
profile; it never picks one, because cheap USB interfaces share identical ids.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DEFAULT_PRESETS_DIR = Path(os.environ.get("VECTOR_BRIDGE_PRESETS", "/etc/vector-bridge/presets"))

PTT_METHODS = ("cat", "rts", "dtr", "vox")


@dataclass
class RadioProfile:
    name: str
    display_name: str
    rig_model: int = 0  # Hamlib model number; 0 means no CAT
    interface: str = "internal-usb"  # internal-usb | digirig | other
    cat_device: str = ""  # serial device path; empty = none or pick at setup
    cat_baud: int = 0
    ptt_method: str = "vox"
    audio_usb_id: str = ""  # "vendor:product" hint, e.g. "0d8c:0012"; a hint only
    civaddr: str = ""  # Icom CI-V address in hex as the radio's menu shows it, e.g. "A4"; empty = Hamlib's default
    set_conf: str = ""  # expert Hamlib settings for rigctld -C, "name=value,name=value"; checked by hamlib.check_conf
    ptt_device: str = ""  # RTS or DTR keying on another serial port than radio control; empty = the same port
    assert_lines: str = ""  # serial lines to hold ON at port open, "rts", "dtr" or "rts,dtr", for cables powered from one; empty = held OFF
    can_report_swr: bool = True
    can_report_alc: bool = True
    min_power_w: float = 5.0
    tx_start_gain: int = 40  # TX level (0..100) set when the profile is chosen and where the Drive search starts
    alc_target: float = 0.10  # Drive passes at or below this on Hamlib's 0..1 ALC scale (just below ALC onset)
    quirks: dict = field(default_factory=dict)
    notes: str = ""
    builtin: bool = True

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def needs_rigctld(self) -> bool:
        """rigctld runs for radio control, and also to key an RTS or DTR line when there is none."""
        return self.rig_model != 0 or self.ptt_method in ("rts", "dtr")

    def asserted_lines(self) -> list[str]:
        return [x.strip().lower() for x in self.assert_lines.split(",") if x.strip()]

    def validate(self) -> None:
        if self.ptt_method not in PTT_METHODS:
            raise ValueError(f"ptt_method must be one of {PTT_METHODS}, got {self.ptt_method!r}")
        if self.ptt_method == "cat" and self.rig_model == 0:
            raise ValueError("ptt_method 'cat' needs a rig_model")
        if not set(self.asserted_lines()) <= {"rts", "dtr"}:
            raise ValueError(f"assert_lines must name rts and/or dtr, got {self.assert_lines!r}")
        self.civaddr = normalize_civaddr(self.civaddr)


# Hamlib model numbers as of Hamlib 4.5: Kenwood TS-480 2014 (QMX and uSDX emulate it),
# Elecraft KX2 2044, Yaesu FT-891 1036, Icom IC-705 3085.
BUILTINS: tuple[RadioProfile, ...] = (
    RadioProfile("kx2", "Elecraft KX2", rig_model=2044, cat_baud=38400, ptt_method="cat",
                 min_power_w=0.1, tx_start_gain=20, can_report_alc=False, quirks={"data_mode": "DATA A"},
                 notes="Audio via KX2 phones and mic jacks through the USB sound card; CAT via the KXUSB cable."),
    RadioProfile("qmx", "QRP Labs QMX", rig_model=2014, cat_baud=38400, ptt_method="cat",
                 min_power_w=0.5, tx_start_gain=60, can_report_swr=False, can_report_alc=False,
                 notes="Single USB cable carries audio and CAT. QMX emulates a Kenwood TS-480 for CAT."),
    RadioProfile("usdx", "uSDX-style transceiver", rig_model=2014, cat_baud=38400, ptt_method="cat",
                 min_power_w=0.5, tx_start_gain=50, can_report_swr=False, can_report_alc=False, interface="digirig",
                 notes="Kenwood TS-480 emulation is common but varies by firmware; CAT may fail. Falls back to VOX."),
    RadioProfile("ft-891", "Yaesu FT-891", rig_model=1036, cat_baud=38400, ptt_method="cat",
                 interface="digirig", audio_usb_id="0d8c:0012", min_power_w=5.0, tx_start_gain=50,
                 notes="Audio through the rear DATA jack via a Digirig, never the front mic jack. CAT on the CAT/LINEAR jack."),
    RadioProfile("ic-705", "Icom IC-705", rig_model=3085, cat_baud=115200, ptt_method="cat",
                 min_power_w=0.5, tx_start_gain=70,
                 notes="Single USB cable. Set DATA MOD to USB and USB SEND to RTS or CI-V on the rig."),
    RadioProfile("digirig-generic", "Unknown rig on a Digirig Mobile", rig_model=0, ptt_method="rts",
                 interface="digirig", audio_usb_id="0d8c:0012", can_report_swr=False, can_report_alc=False,
                 notes="PTT on the serial RTS line. No CAT until a rig is chosen."),
    RadioProfile("signalink-generic", "Unknown rig on a SignaLink", rig_model=0, ptt_method="vox",
                 interface="other", can_report_swr=False, can_report_alc=False,
                 notes="The SignaLink keys the radio from the audio itself. No CAT: frequency, mode, SWR and power "
                       "cannot be read."),
)


def normalize_civaddr(value: str) -> str:
    """A CI-V address as two upper-case hex digits ("A4"), from "a4", "0xA4" or "A4h"; "" stays "".

    Icom menus show the address in hex, so a bare number is read as hex too.
    """
    if not isinstance(value, str):
        raise ValueError("the CI-V address must be text, for example A4")  # noqa: TRY004 - callers catch ValueError for any bad value
    if not value.strip():
        return ""
    v = value.strip().upper().removeprefix("0X").removesuffix("H")
    if not re.fullmatch(r"[0-9A-F]{1,2}", v) or int(v, 16) == 0:
        raise ValueError(f"the CI-V address must be hex from 01 to FF, as the radio's menu shows it, not {value!r}")
    return f"{int(v, 16):02X}"


def builtin_names() -> list[str]:
    return [p.name for p in BUILTINS]


def _from_dict(data: dict, *, builtin: bool) -> RadioProfile:
    known = {f.name for f in fields(RadioProfile)}
    kwargs = {k: v for k, v in data.items() if k in known and k != "builtin"}
    prof = RadioProfile(builtin=builtin, **kwargs)
    prof.validate()
    return prof


def load_all(presets_dir: Path | None = None) -> dict[str, RadioProfile]:
    """Built-ins, then user presets on top. A user preset with a built-in name replaces it."""
    presets_dir = presets_dir or DEFAULT_PRESETS_DIR
    out = {p.name: p for p in BUILTINS}
    if presets_dir.is_dir():
        for path in sorted(presets_dir.glob("*.toml")):
            data = tomllib.loads(path.read_text())
            data.setdefault("name", path.stem)
            data.setdefault("display_name", data["name"])
            out[data["name"]] = _from_dict(data, builtin=False)
    return out


def get(name: str, presets_dir: Path | None = None) -> RadioProfile:
    profiles = load_all(presets_dir)
    if name not in profiles:
        raise KeyError(name)
    return profiles[name]


def save(prof: RadioProfile, presets_dir: Path | None = None) -> Path:
    """Write a user preset atomically onto the persist partition."""
    from . import persist

    prof.validate()
    presets_dir = presets_dir or DEFAULT_PRESETS_DIR
    return persist.save(presets_dir / f"{prof.name}.toml", dumps(prof))


def dumps(prof: RadioProfile) -> str:
    lines = [f"# VectorBridge radio profile: {prof.display_name}", ""]
    for f in fields(RadioProfile):
        if f.name in ("builtin", "quirks"):
            continue
        lines.append(f"{f.name} = {_toml_value(getattr(prof, f.name))}")
    # The table goes last: in TOML every key after a [table] header belongs to it.
    lines += ["", "[quirks]"] + [f"{k} = {_toml_value(v)}" for k, v in prof.quirks.items()]
    return "\n".join(lines) + "\n"


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
