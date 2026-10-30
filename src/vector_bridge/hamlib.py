"""The radios the installed Hamlib knows, from ``rigctl -l``.

The list comes from the card's own Hamlib, so it always matches the rigctld that will run.
It is read once and cached: about 300 rows, and Hamlib cannot change while the bridge runs.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading

log = logging.getLogger(__name__)

# Hamlib's own entries (dummy rig, NET rigctl) are not radios a field user would plug in.
SKIP_MFG = {"Hamlib"}

_cache: list[dict] | None = None
_mu = threading.Lock()


def parse_list(text: str) -> list[dict]:
    """``rigctl -l`` as [{"model", "mfg", "name", "status"}], sorted by maker then name.

    The table is fixed-width under a header naming the columns, so each field is cut at the
    header's offsets: model names have spaces in them, and some backends have no model name.
    """
    lines = text.splitlines()
    head = next((i for i, line in enumerate(lines) if line.lstrip().startswith("Rig #")), None)
    if head is None:
        return []
    h = lines[head]
    try:
        cols = [h.index(c) for c in ("Mfg", "Model", "Version", "Status", "Macro")]
    except ValueError:
        return []
    out = []
    for line in lines[head + 1:]:
        num = line[:cols[0]].strip()
        if not num.isdigit():
            continue
        mfg = line[cols[0]:cols[1]].strip()
        name = line[cols[1]:cols[2]].strip()
        if not mfg or mfg in SKIP_MFG:
            continue
        out.append({"model": int(num), "mfg": mfg, "name": name or mfg,
                    "status": line[cols[3]:cols[4]].strip()})
    out.sort(key=lambda m: (m["mfg"].lower(), m["name"].lower(), m["model"]))
    return out


def models() -> list[dict]:
    """The installed Hamlib's radios; empty when rigctl is missing or fails (then nothing is cached)."""
    global _cache
    with _mu:
        if _cache is not None:
            return _cache
        if shutil.which("rigctl") is None:
            return []
        try:
            done = subprocess.run(["rigctl", "-l"], capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            log.exception("rigctl -l failed")
            return []
        found = parse_list(done.stdout)
        if found:
            _cache = found
        return found


def find(model: int) -> dict | None:
    return next((m for m in models() if m["model"] == model), None)


# ---- a radio's own settings (rigctld -C), from ``rigctl -m N -L`` -------------------

# Settings the page already sets, or that open another device: never through the expert field.
OWNED_CONF = {"serial_speed": "the baud rate", "ptt_type": "the keying method", "civaddr": "the CI-V address"}
_CONF_VALUE = re.compile(r"[A-Za-z0-9._:+-]{1,64}")
MAX_CONF = 10
_conf_cache: dict[int, list[dict]] = {}


def parse_conf(text: str) -> list[dict]:
    """``rigctl -m N -L`` as [{"name", "desc", "default", "kind", "min", "max", "options"}].

    Each setting is a ``name: "description"`` line, then tab-indented Default and type lines.
    Names with a space in them cannot be given to -C, so they and their lines are left out.
    """
    out: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        if not line.startswith("\t"):
            m = re.fullmatch(r'([A-Za-z0-9_]+): "(.*)"', line)
            cur = {"name": m.group(1), "desc": m.group(2), "default": "", "kind": "string"} if m else None
            if cur is not None:
                out.append(cur)
            continue
        if cur is None:
            continue
        body = line.strip()
        if body.startswith("Default:"):
            cur["default"] = body[len("Default:"):].split(", Value:")[0].strip()
        elif body.startswith("Range:"):
            r = re.match(r"Range: (\S+)\.\.(\S+?),", body)
            if r:
                cur.update(kind="range", min=float(r.group(1)), max=float(r.group(2)))
        elif body.startswith("Combo:"):
            cur.update(kind="combo", options=[o.strip() for o in body[len("Combo:"):].split(",") if o.strip()])
        elif body.startswith("Check button"):
            cur.update(kind="check", options=["0", "1"])
    return out


def conf_params(rig_model: int) -> list[dict]:
    """The settings this Hamlib takes for a model; empty when rigctl is missing or fails (not cached)."""
    with _mu:
        if rig_model in _conf_cache:
            return _conf_cache[rig_model]
        if shutil.which("rigctl") is None:
            return []
        try:
            done = subprocess.run(["rigctl", "-m", str(int(rig_model)), "-L"], capture_output=True, text=True,
                                  timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            log.exception("rigctl -L failed")
            return []
        found = parse_conf(done.stdout)
        if found:
            _conf_cache[rig_model] = found
        return found


def check_conf(text: str, params: list[dict]) -> str:
    """Expert settings typed as "name=value, name=value", checked against this radio's own list.
    Returns them as rigctld's -C takes them ("a=1,b=2"); "" stays "". ValueError says what is wrong.
    With no list (rigctl unreadable) names and values are only checked for their shape."""
    if not isinstance(text, str):
        raise ValueError("expert settings must be text, like rts_state=OFF")  # noqa: TRY004 - callers catch ValueError
    pairs = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    if len(pairs) > MAX_CONF:
        raise ValueError(f"at most {MAX_CONF} expert settings")
    known = {p["name"]: p for p in params}
    out, seen = [], set()
    for pair in pairs:
        name, eq, value = (x.strip() for x in pair.partition("="))
        if not eq or not re.fullmatch(r"[A-Za-z0-9_]{1,40}", name) or not _CONF_VALUE.fullmatch(value):
            raise ValueError(f"{pair!r} is not name=value (letters, digits and . _ : + - only)")
        if name in OWNED_CONF:
            raise ValueError(f"{name} is set by {OWNED_CONF[name]} field, not here")
        if name.endswith("_pathname"):
            raise ValueError(f"{name} names another device; pick ports with the port fields")
        if name.startswith("multicast"):
            raise ValueError(f"{name} opens rig control to the network outside the safety proxy")
        if name in seen:
            raise ValueError(f"{name} is given twice")
        seen.add(name)
        p = known.get(name)
        if known and p is None:
            raise ValueError(f"this radio's Hamlib has no setting called {name}")
        if p is not None and p.get("options") and value not in p["options"]:
            raise ValueError(f"{name} must be one of {', '.join(p['options'])}")
        if p is not None and p["kind"] == "range":
            try:
                v = float(value)
            except ValueError:
                raise ValueError(f"{name} must be a number") from None
            if not p["min"] <= v <= p["max"]:
                raise ValueError(f"{name} must be from {p['min']:g} to {p['max']:g}")
        out.append(f"{name}={value}")
    return ",".join(out)
