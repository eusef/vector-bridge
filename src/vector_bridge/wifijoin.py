"""Join an existing Wi-Fi network from the field UI (the Wi-Fi page, below the access point).

One radio at a time carries the bridge's network: a USB Wi-Fi adapter when one is plugged in, else
the Pi's built-in radio (wifiradio picks it and moves the network when an adapter comes or goes).
While it is on someone else's network the access point is gone, and the UI is at
http://vectorbridge.local/ on that network instead. The access point always comes up
first at boot (apsetup keeps it at autoconnect priority 100), so a reboot is the way back, and so
is the "Back to the access point" button. The one exception is a home network: one remembered
network may be marked to rejoin at boot, and vb-wifi-home.service switches to it (see boot())
only when it is in range. Away from home the access point stays up; on an adapter that cannot
scan while it is the access point, it pauses once at boot for that scan.

Three requests, all written by the unprivileged web process to the persist partition and run as
root by vb-wifi-join.service when vb-wifi-join.path sees the file:

  join <ssid> <passphrase>  a new network: connect, keep the profile (autoconnect off) and stay
                            joined. The profile lands on /persist (NetworkManager's keyfile
                            directory, remounted rw for those seconds), so there is no reboot and
                            the root stays the overlay. A refused passphrase puts the access
                            point back.
  up <ssid>                 a remembered network.
  ap                        the access point again.

The outcome of the last request is left in a result file the page shows.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import apsetup, manage, persist, wifiradio

PENDING = persist.APP_DIR / "wifi-join.json"
RESULT = persist.APP_DIR / "wifi-join-result.json"
# The remembered network to rejoin at boot. Written by the web process; no root trip needed.
HOME = persist.APP_DIR / "home-network.json"
PATH_UNIT = "vb-wifi-join.path"
ACTIONS = ("join", "up", "ap")
_PASS = apsetup._PASS


def validate(body: dict) -> tuple[dict, list[str]]:
    """(request, errors). Root runs this again on the pending file, which it does not trust."""
    errors = []
    action = body.get("action") if isinstance(body, dict) else None
    if action not in ACTIONS:
        return {}, ["Unknown request."]
    req = {"action": action}
    if action in ("join", "up"):
        ssid = body.get("ssid")
        ssid = ssid.strip() if isinstance(ssid, str) else ""
        if not ssid or len(ssid.encode()) > apsetup.SSID_MAX or any(ord(c) < 0x20 for c in ssid):
            errors.append("Network name: 1 to 32 characters.")
        req["ssid"] = ssid
    if action == "join":
        passphrase = body.get("passphrase")
        passphrase = passphrase if isinstance(passphrase, str) else ""
        if passphrase and not _PASS.fullmatch(passphrase):
            errors.append("Passphrase: 8 to 63 characters, plain letters, digits and symbols; or none for an open network.")
        req["passphrase"] = passphrase
    return req, errors


def _device() -> dict:
    dev = wifiradio.active_device()
    r = manage._nmcli("-t", "-f", "GENERAL.CONNECTION,GENERAL.STATE,IP4.ADDRESS", "device", "show", dev, sudo=False)
    if r.returncode != 0:
        return {}
    return {"device": dev, **dict(line.split(":", 1) for line in r.stdout.splitlines() if ":" in line)}


def saved_networks() -> list[str]:
    """Remembered client profiles, by connection name. Names are not secrets."""
    r = manage._nmcli("-t", "-f", "NAME,TYPE", "connection", "show", sudo=False)
    if r.returncode != 0:
        return []
    names = [line.split(":", 1)[0] for line in r.stdout.splitlines()
             if line.endswith(":802-11-wireless") and line.split(":", 1)[0] != manage.AP_CON]
    return sorted(names)


def last_result() -> dict | None:
    try:
        return json.loads(RESULT.read_text())
    except (OSError, ValueError):
        return None


def home_network() -> str:
    """The remembered network marked to rejoin at boot, or "" for none."""
    # Root reads this at boot: no symlink, and a FIFO must not hang it (as in _read_request).
    try:
        fd = os.open(HOME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as f:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return ""
            name = json.loads(f.read(4096)).get("ssid", "")
    except (OSError, ValueError, AttributeError):
        return ""
    return name if isinstance(name, str) else ""


def report() -> dict:
    dev = _device()
    con = dev.get("GENERAL.CONNECTION", "")
    return {"ok": True, "on_ap": con == manage.AP_CON, "connection": con, "radio": wifiradio.label(dev.get("device", "")),
            "radio_usb": wifiradio.is_usb(dev.get("device", "")),
            "state": dev.get("GENERAL.STATE", ""), "ip": dev.get("IP4.ADDRESS[1]", "").split("/")[0],
            "saved": saved_networks(), "home": home_network(), "pending": PENDING.exists(), "result": last_result()}


def set_home(body: dict, home: Path | None = None) -> tuple[int, dict]:
    """{"action": "home", "ssid": name} marks a remembered network to rejoin at boot; an empty name clears it.
    Handled here, never queued: root's validate() does not know "home", so it cannot reach apply()."""
    name = body.get("ssid")
    name = name.strip() if isinstance(name, str) else ""
    if name and name not in saved_networks():
        return 400, {"ok": False, "errors": ["Only a remembered network can be the home network."]}
    try:
        persist.save(home or HOME, json.dumps({"ssid": name}), mode=0o644)
    except OSError as e:
        return 500, {"ok": False, "errors": [f"Could not write to the card: {e}"]}
    return 200, {"ok": True, "action": "home", "ssid": name}


def handle(body: dict, pending: Path | None = None) -> tuple[int, dict]:
    """POST /api/join from the web UI."""
    if isinstance(body, dict) and body.get("action") == "home":
        return set_home(body)
    req, errors = validate(body)
    if errors:
        return 400, {"ok": False, "errors": errors}
    try:
        persist.save(pending or PENDING, json.dumps(req), mode=0o600)
    except OSError as e:
        return 500, {"ok": False, "errors": [f"Could not write to the card: {e}"]}
    return 200, {"ok": True, "action": req["action"], "ssid": req.get("ssid", "")}


# ---- root side ---------------------------------------------------------------------


def _read_request() -> tuple[dict, list[str]]:
    try:
        fd = os.open(PENDING, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)   # a FIFO must not hang root
        with os.fdopen(fd) as f:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return {}, ["not a regular file"]
            return validate(json.loads(f.read(4096)))
    except (OSError, ValueError) as e:
        return {}, [str(e)]


def _result(ok: bool, text: str) -> None:
    try:
        persist.save(RESULT, json.dumps({"ok": ok, "text": text}), mode=0o644)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"vb-wifi-join: could not write the result: {e}", file=sys.stderr)


def _clear() -> None:
    apsetup._clear(PENDING)
    if os.path.lexists(PENDING):
        print(f"vb-wifi-join: request cannot be removed, stopping {PATH_UNIT}", file=sys.stderr)
        subprocess.run(["systemctl", "stop", PATH_UNIT], check=False)


def _up(name: str) -> subprocess.CompletedProcess:
    """A saved profile, on the best radio there is now. An adapter that refuses the access point
    leaves it to the built-in radio."""
    radio = wifiradio.pick()
    r = wifiradio.bring_up(name, radio)
    if r.returncode != 0 and name == manage.AP_CON and radio.usb:
        r = wifiradio.bring_up(name, wifiradio.built_in())
    return r


def _connect(ssid: str, passphrase: str, radio: str) -> subprocess.CompletedProcess:
    """Creates the profile and activates it. --ask reads the secret from stdin: never in ps."""
    argv = ["nmcli", "--ask", "device", "wifi", "connect", ssid, "ifname", radio]
    return subprocess.run(manage._sudo(argv), input=passphrase + "\n", text=True, capture_output=True, check=False)


def _err(r: subprocess.CompletedProcess) -> str:
    lines = (r.stderr or r.stdout).strip().splitlines()
    return lines[-1] if lines else "nmcli failed"


def apply() -> int:
    """Called by vb-wifi-join.service while the request file exists; every way out removes it
    or reboots (the path unit fires again otherwise)."""
    with wifiradio.locked():
        return _apply()


def _apply() -> int:
    req, errors = _read_request()
    if errors:
        print(f"vb-wifi-join: request rejected: {errors}", file=sys.stderr)
        _clear()
        return 2
    action = req["action"]
    if action == "ap":
        r = _up(manage.AP_CON)
        _result(r.returncode == 0, "Back on the access point." if r.returncode == 0 else _err(r))
        _clear()
        return 0 if r.returncode == 0 else 1
    if action == "up":
        r = _up(req["ssid"])
        _result(r.returncode == 0, f"Joined {req['ssid']}." if r.returncode == 0 else _err(r))
        if r.returncode != 0:
            _up(manage.AP_CON)
        _clear()
        return 0 if r.returncode == 0 else 1
    rc = 0
    try:
        known = req["ssid"] in saved_networks()
        picked = wifiradio.pick()
        radio = picked.name
        if known:   # a profile tied to another radio would be copied, not reused
            manage._nmcli("connection", "modify", "--temporary", req["ssid"], "connection.interface-name", radio)
        # The radio is in AP mode: the scan cache may not hold the network yet, and nmcli refuses a
        # network it has not seen (the home network, 2.0 card, 2026-10-15). The join drops the AP anyway.
        _look(req["ssid"], picked, pause_if_missing=True)
        # NetworkManager writes the new profile to /persist: rw for these lines only.
        with persist.writable(persist.NETWORKS_DIR):
            r = _connect(req["ssid"], req["passphrase"], radio)
            if r.returncode == 0:
                # The AP must still win at boot; a remembered network is used on request only.
                # Tied to no radio on the card: each use picks the radio there is then.
                active = manage._nmcli("-g", "GENERAL.CONNECTION", "device", "show", radio, sudo=False).stdout.strip() or req["ssid"]
                manage._nmcli("connection", "modify", active, "connection.autoconnect", "no", "connection.interface-name", "")
            elif not known:             # a failed connect can leave a half profile; a remembered one stays
                manage._nmcli("connection", "delete", req["ssid"])
        if r.returncode == 0:
            _result(True, f"Joined {req['ssid']} and remembered it. Find the bridge at http://vectorbridge.local/ on that network.")
        else:
            rc = 1
            _result(False, f"Could not join {req['ssid']}: {_err(r)}")
            _up(manage.AP_CON)
    except (OSError, subprocess.CalledProcessError) as e:
        rc = rc or 4
        print(f"vb-wifi-join: failed: {e}", file=sys.stderr)
    finally:
        _clear()
    return rc


def _ssid_of(name: str) -> str:
    # -e no: the SSID exactly as broadcast, with no ":" or "\\" escaping to undo.
    return manage._nmcli("-e", "no", "-g", "802-11-wireless.ssid", "connection", "show", name, sudo=False).stdout.strip() or name


def _visible(radio: str, rescan: bool = False) -> set[str]:
    # --rescan yes waits for a fresh scan to finish; only root may ask for one.
    r = manage._nmcli("-e", "no", "-t", "-f", "SSID", "device", "wifi", "list", "ifname", radio,
                      *(("--rescan", "yes") if rescan else ()), sudo=rescan)
    return {line for line in r.stdout.splitlines() if line} if r.returncode == 0 else set()


def _look(ssid: str, radio: wifiradio.Radio, scans: int = 3, pause_if_missing: bool = False) -> tuple[set[str], bool]:
    """(the networks seen, whether the access point was paused to see them). A pause takes the
    access point down; the caller joins or brings it back."""
    seen: set[str] = set()
    for _ in range(scans):  # the radio has just come up in AP mode: the first scan can come back empty
        manage._nmcli("device", "wifi", "rescan", "ifname", radio.name)
        time.sleep(5)
        seen |= _visible(radio.name)
        if ssid in seen:
            return seen, False
    # The bridge's own network can sit in the scan list from before the move (seen 2026-10-10 at
    # signal 0 after every scan had failed), so it says nothing about whether a scan worked.
    if seen - {_ssid_of(manage.AP_CON)} and not pause_if_missing:
        return seen, False
    # Not one network: this radio cannot scan while it is the access point. NetworkManager may
    # refuse the rescan, or accept it while the driver refuses (TP-Link Archer T2U Nano,
    # rtw88_8821au: SCAN-FAILED -95, 2026-10-10). Pause the access point for one scan.
    print(f"vb-wifi: no {ssid} in the scan on the access point, pausing it to look")
    manage._nmcli("device", "disconnect", radio.name)
    return _visible(radio.name, rescan=True), True


def boot(scans: int = 3) -> int:
    """Called once by vb-wifi-home.service after boot. The access point is already up; move to the
    home network only if it is in range, so in the field nothing drops (a radio that cannot scan
    as an access point pauses it for one scan, a few seconds). A failed join (router
    down, passphrase changed) brings the access point back, as a failed "up" does."""
    with wifiradio.locked():
        return _boot(scans)


def _boot(scans: int) -> int:
    name = home_network()
    if not name or name not in saved_networks():
        return 0
    if _device().get("GENERAL.CONNECTION", "") == name:
        return 0
    ssid = _ssid_of(name)
    radio = wifiradio.pick()
    seen, paused = _look(ssid, radio, scans)
    if ssid not in seen:
        if paused:
            print(f"vb-wifi-home: {name} not in range, back to the access point")
            return 0 if _up(manage.AP_CON).returncode == 0 else 1
        print(f"vb-wifi-home: {name} not in range, staying on the access point")
        return 0
    r = wifiradio.bring_up(name, radio)
    _result(r.returncode == 0, f"Rejoined {name} at boot." if r.returncode == 0 else f"Could not rejoin {name} at boot: {_err(r)}")
    if r.returncode != 0:
        _up(manage.AP_CON)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit({"apply": apply, "boot": boot}[sys.argv[1]]())
