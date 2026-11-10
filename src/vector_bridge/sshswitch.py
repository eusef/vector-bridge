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

The login's SSH keys (~/.ssh/authorized_keys, roadmap 4.11) are forgotten the same way. Whenever
the file changes, vb-ssh-keys.path has root copy it to KEYS (deleted when the file is), and
vb-ssh-boot puts it back at boot. No KEYS (never changed since install): the keys are left alone.
"""
from __future__ import annotations

import contextlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import apsetup, persist

PENDING = persist.APP_DIR / "ssh-request.json"
RESULT = persist.APP_DIR / "ssh-result.json"
STATE = persist.APP_DIR / "ssh.json"
# Root only (0700), not under APP_DIR: the web user owns that directory and could rename files in it.
KEYS = persist.PERSIST_MOUNT / "ssh" / "authorized_keys"
KEYS_MAX = 64 * 1024
# systemd does not watch the file while vb-ssh-keys runs, so a save waits this long and looks again.
KEYS_SETTLE_S = 1.0
KEYS_PASSES = 5
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


# ---- SSH keys (roadmap 4.11) ------------------------------------------------------------


def _login() -> pwd.struct_passwd:
    return pwd.getpwuid(1000)


def _open_ssh_dir(user: pwd.struct_passwd, create: bool = False) -> int | None:
    """An fd on the login's ~/.ssh, or None when it is missing. A symlink, or a directory that the
    login does not own, raises: root must not be steered to another directory."""
    path = Path(user.pw_dir) / ".ssh"
    made = False
    if create and not os.path.lexists(path):
        os.mkdir(path, 0o700)
        made = True
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    # Through the fd, never the path: the login may have swapped the new directory for a symlink.
    if made and os.fstat(fd).st_uid == os.geteuid() != user.pw_uid:
        os.fchown(fd, user.pw_uid, user.pw_gid)
    if os.fstat(fd).st_uid != user.pw_uid:
        os.close(fd)
        raise OSError(f"{path} is not owned by {user.pw_name}")
    return fd


def _read_keys(name: str, dir_fd: int | None, path: Path, owner: int) -> bytes | None:
    """The file's bytes, or None when it is missing. Not a regular file, owned by someone else,
    writable by others or too big: raises."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{path} is not a regular file")
        if st.st_uid != owner or st.st_mode & 0o022:
            raise OSError(f"{path} has the wrong owner or mode")
        data = f.read(KEYS_MAX + 1)
    if len(data) > KEYS_MAX:
        raise OSError(f"{path} is over {KEYS_MAX} bytes")
    return data


def _keys_dir_ok() -> None:
    """KEYS's directory must be root's own and closed to everyone else, or nothing in it is used."""
    st = os.lstat(KEYS.parent)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise OSError(f"{KEYS.parent} is not a private root directory")


def _saved_keys() -> bytes | None:
    if not os.path.lexists(KEYS.parent):
        return None
    _keys_dir_ok()
    return _read_keys(str(KEYS), None, KEYS, os.geteuid())


def _live_keys(user: pwd.struct_passwd) -> bytes | None:
    dfd = _open_ssh_dir(user)
    if dfd is None:
        return None
    try:
        return _read_keys("authorized_keys", dfd, Path(user.pw_dir) / ".ssh" / "authorized_keys", user.pw_uid)
    finally:
        os.close(dfd)


def save_keys() -> int:
    """Called by vb-ssh-keys.service when the login's authorized_keys changes: root copies it to
    KEYS, or deletes KEYS when the file is gone. An unchanged copy is not written again. systemd
    stops watching the file while this runs, so after a save it waits and looks again: a second
    key added straight after the first is saved too."""
    try:
        user = _login()
        path = Path(user.pw_dir) / ".ssh" / "authorized_keys"
        for _ in range(KEYS_PASSES):
            data = _live_keys(user)
            if data == _saved_keys():
                return 0
            with persist.writable(KEYS):
                if data is None:
                    KEYS.unlink(missing_ok=True)
                else:
                    KEYS.parent.mkdir(mode=0o700, exist_ok=True)
                    _keys_dir_ok()
                    persist.atomic_write(KEYS, data, mode=0o600)
            print(f"vb-ssh: {path} is gone, the saved keys are deleted" if data is None
                  else f"vb-ssh: saved {path} for the next boot")
            time.sleep(KEYS_SETTLE_S)
    except (OSError, KeyError, subprocess.CalledProcessError) as e:
        print(f"vb-ssh: could not save the SSH keys: {e}", file=sys.stderr)
        return 1
    print(f"vb-ssh: {path} kept changing; the next change is saved", file=sys.stderr)
    return 1


def restore_keys() -> bool:
    """At boot: put KEYS back as the login's authorized_keys (0600, owned by the login), since
    the overlay forgot it. Only a KEYS that root wrote, in root's own directory, is used."""
    try:
        data = _saved_keys()
        if data is None:
            return True
        user = _login()
        dfd = _open_ssh_dir(user, create=True)
        if dfd is None:
            raise OSError(f"{user.pw_dir}/.ssh could not be made")
        tmp = ".authorized_keys.vb-ssh"
        try:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=dfd)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fchown(fd, user.pw_uid, user.pw_gid)
                os.fsync(fd)
            os.replace(tmp, "authorized_keys", src_dir_fd=dfd, dst_dir_fd=dfd)
        finally:
            os.close(dfd)
    except (OSError, KeyError) as e:
        print(f"vb-ssh: could not put back the SSH keys: {e}", file=sys.stderr)
        return False
    print("vb-ssh: SSH keys put back at boot")
    return True


def boot() -> int:
    """Called once by vb-ssh-boot.service: the overlay has forgotten the password, the started
    ssh.service and the SSH keys, so put back what STATE and KEYS say. No STATE (the bench):
    leave SSH and the password as they are."""
    keys_ok = restore_keys()
    if not os.path.lexists(STATE):
        return 0 if keys_ok else 1
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
    return 0 if keys_ok else 1


if __name__ == "__main__":
    sys.exit({"apply": apply, "boot": boot, "keys": save_keys}[sys.argv[1]]())
