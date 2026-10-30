"""SSH on or off (R-D4). A fresh image ships with SSH off and the login password locked
(scripts/image/prepare-golden.sh). Two switches turn it on, and each sets a new login password:

  the Wi-Fi page    POST /api/ssh, behind an admin PIN session (never an app token). The web
                    process hashes the password, so only the hash is ever written to the card,
                    and leaves a request on /persist; vb-ssh-apply.service applies it as root
                    when vb-ssh-apply.path sees it.
  the HDMI console  menu item 4, `vector-bridge ssh on|off`, already root.

Off stops SSH and keeps the password, which sudo at the console's command line needs. Sessions
that are already open stay open (ssh.service kills only the listener).

In field mode the root is an overlay, so a new password and a started ssh.service are both gone
at the next boot. The setting therefore lives on /persist (STATE, root only) and
vb-ssh-boot.service applies it at every boot. A card with no STATE (the bench) is left alone.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import stat
import subprocess
import sys
from pathlib import Path

from . import apsetup, persist

PENDING = persist.APP_DIR / "ssh-request.json"
RESULT = persist.APP_DIR / "ssh-result.json"
STATE = persist.APP_DIR / "ssh.json"
PATH_UNIT = "vb-ssh-apply.path"
UNIT = "ssh.service"
PASS_MIN, PASS_MAX = 8, 64
# sha512-crypt, as `openssl passwd -6` writes it. Root accepts nothing else.
_HASH = re.compile(r"\$6\$[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{86}")


def login_user() -> str:
    """The card's own account (uid 1000), the one the console's command line logs in."""
    return pwd.getpwuid(1000).pw_name


def check_password(password) -> list[str]:
    if not isinstance(password, str) or not PASS_MIN <= len(password) <= PASS_MAX:
        return [f"Password: {PASS_MIN} to {PASS_MAX} characters."]
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in password):
        return ["Password: letters, digits, spaces and symbols only."]
    return []


def hash_password(password: str) -> str:
    """sha512-crypt through openssl: Python's crypt module is gone from 3.13. The password goes
    in on stdin, never in ps."""
    r = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password + "\n", text=True,
                       capture_output=True, check=False)
    h = r.stdout.strip()
    if r.returncode != 0 or not _HASH.fullmatch(h):
        raise OSError(f"openssl passwd failed: {(r.stderr or r.stdout).strip()}")
    return h


def validate(body) -> tuple[dict, list[str]]:
    """(request, errors) for the request file and STATE. Root runs this on both: it trusts neither.
    "on" carries the new password's hash; STATE also keeps it while SSH is off."""
    action = body.get("action") if isinstance(body, dict) else None
    if action not in ("on", "off"):
        return {}, ["Unknown request."]
    req = {"action": action}
    h = body.get("hash")
    if h is not None or action == "on":
        if not isinstance(h, str) or not _HASH.fullmatch(h):
            return {}, ["Bad password hash."]
        req["hash"] = h
    return req, []


def is_on() -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", UNIT], check=False).returncode == 0


def last_result() -> dict | None:
    try:
        r = json.loads(RESULT.read_text())
    except (OSError, ValueError):
        return None
    return r if isinstance(r, dict) else None


def report() -> dict:
    """GET /api/ssh."""
    try:
        user = login_user()
    except KeyError:
        user = ""
    return {"ok": True, "on": is_on(), "user": user, "pending": PENDING.exists(), "result": last_result()}


def handle(body: dict, pending: Path | None = None) -> tuple[int, dict]:
    """POST /api/ssh from the Wi-Fi page: {"action": "on", "password": ...} or {"action": "off"}."""
    action = body.get("action") if isinstance(body, dict) else None
    if action == "on":
        errors = check_password(body.get("password"))
        if errors:
            return 400, {"ok": False, "errors": errors}
        try:
            req = {"action": "on", "hash": hash_password(body["password"])}
        except OSError as e:
            return 500, {"ok": False, "errors": [f"Could not set the password: {e}"]}
    elif action == "off":
        req = {"action": "off"}
    else:
        return 400, {"ok": False, "errors": ["Unknown request."]}
    try:
        persist.save(pending or PENDING, json.dumps(req), mode=0o600)
    except OSError as e:
        return 500, {"ok": False, "errors": [f"Could not write to the card: {e}"]}
    return 200, {"ok": True, "action": action}


# ---- root side ---------------------------------------------------------------------


def _read(path: Path) -> tuple[dict, list[str]]:
    # The web user owns the directory: a symlink or a FIFO in its place must not move root.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as f:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return {}, ["not a regular file"]
            return validate(json.loads(f.read(4096)))
    except (OSError, ValueError) as e:
        return {}, [str(e)]


def _stored_hash() -> str:
    req, errors = _read(STATE)
    return "" if errors else req.get("hash", "")


def _result(ok: bool, text: str) -> None:
    try:
        persist.save(RESULT, json.dumps({"ok": ok, "text": text}), mode=0o644)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"vb-ssh: could not write the result: {e}", file=sys.stderr)


def _clear() -> None:
    apsetup._clear(PENDING)
    if os.path.lexists(PENDING):
        print(f"vb-ssh: request cannot be removed, stopping {PATH_UNIT}", file=sys.stderr)
        subprocess.run(["systemctl", "stop", PATH_UNIT], check=False)


def _set_password(h: str) -> None:
    # -e: the hash, read from stdin, so it is never in ps.
    subprocess.run(["chpasswd", "-e"], input=f"{login_user()}:{h}\n", text=True, capture_output=True, check=True)


def _systemctl(verb: str) -> None:
    subprocess.run(["systemctl", verb, UNIT], capture_output=True, check=True)


def turn(req: dict) -> tuple[bool, str]:
    """Root: save the setting to /persist first, so the next boot agrees, then apply it now."""
    on = req["action"] == "on"
    h = req.get("hash") or _stored_hash()
    state = {"action": "on" if on else "off", **({"hash": h} if h else {})}
    try:
        persist.save(STATE, json.dumps(state), mode=0o600)
        if on:
            _set_password(h)
            _systemctl("start")
        else:
            _systemctl("stop")
    except (OSError, subprocess.CalledProcessError, KeyError) as e:
        return False, f"Could not turn SSH {'on' if on else 'off'}: {e}"
    if on:
        return True, f"SSH is on. Log in as {login_user()} with the new password."
    return True, "SSH is off. Sessions already open stay open until they log out."


def apply() -> int:
    """Called by vb-ssh-apply.service while the request file exists; every way out removes it
    (the path unit fires again otherwise)."""
    try:
        req, errors = _read(PENDING)
        if errors:
            print(f"vb-ssh: bad request: {'; '.join(errors)}", file=sys.stderr)
            _result(False, "The request was not valid. Nothing changed.")
            return 2
        ok, text = turn(req)
        print(f"vb-ssh: {text}", file=sys.stdout if ok else sys.stderr)
        _result(ok, text)
        return 0 if ok else 1
    finally:
        _clear()


def boot() -> int:
    """Called once by vb-ssh-boot.service: the overlay has forgotten the password and the started
    ssh.service, so put back what STATE says. No STATE (the bench): leave the card as it is."""
    if not os.path.lexists(STATE):
        return 0
    req, errors = _read(STATE)
    if errors:
        print(f"vb-ssh: {STATE} is not valid ({'; '.join(errors)}); SSH stays off", file=sys.stderr)
        return 1
    try:
        if req.get("hash"):
            _set_password(req["hash"])
        _systemctl("start" if req["action"] == "on" else "stop")
    except (OSError, subprocess.CalledProcessError, KeyError) as e:
        print(f"vb-ssh: failed at boot: {e}", file=sys.stderr)
        return 1
    print(f"vb-ssh: SSH {req['action']} at boot")
    return 0


if __name__ == "__main__":
    sys.exit({"apply": apply, "boot": boot}[sys.argv[1]]())
