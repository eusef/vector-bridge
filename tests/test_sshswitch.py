"""SSH on or off (R-D4): the password check, the request the web side leaves (a hash, never the
password), the root side for on, off and a bad request, the boot re-apply, and the routes."""
import json
import os
import pwd
import shutil
import threading

import pytest
from test_portal import get
from test_wifijoin import post

from vector_bridge import apsetup, sshswitch
from vector_bridge.tuner import TunerApp, serve

HASH = "$6$saltsalt$" + "a" * 86


class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


@pytest.mark.parametrize(("password", "ok"), [
    ("correct horse", True),
    ("12345678", True),
    ("1234567", False),
    ("x" * 65, False),
    ("tab\there", False),
    (None, False),
])
def test_check_password(password, ok):
    assert (not sshswitch.check_password(password)) == ok


@pytest.mark.parametrize(("body", "ok"), [
    ({"action": "on", "hash": HASH}, True),
    ({"action": "off"}, True),
    ({"action": "off", "hash": HASH}, True),                  # STATE keeps the password while off
    ({"action": "on"}, False),
    ({"action": "on", "hash": "$1$md5$abc"}, False),
    ({"action": "on", "hash": HASH + "\nroot:x"}, False),     # one line, one user
    ({"action": "on", "password": "correct horse"}, False),   # root never takes a plain password
    ({"action": "reboot"}, False),
    ("junk", False),
])
def test_validate(body, ok):
    assert (not sshswitch.validate(body)[1]) == ok


@pytest.mark.skipif(not shutil.which("openssl"), reason="no openssl")
def test_hash_password_is_sha512_crypt():
    h = sshswitch.hash_password("correct horse")
    assert h.startswith("$6$") and not sshswitch.validate({"action": "on", "hash": h})[1]


def test_handle_leaves_a_private_hash_and_never_the_password(tmp_path, monkeypatch):
    pending = tmp_path / "ssh-request.json"
    monkeypatch.setattr(sshswitch, "hash_password", lambda pw: HASH)
    assert sshswitch.handle({"action": "on", "password": "short"}, pending)[0] == 400
    assert not pending.exists()
    assert sshswitch.handle({"action": "on", "password": "correct horse"}, pending)[0] == 200
    assert json.loads(pending.read_text()) == {"action": "on", "hash": HASH}
    assert "correct horse" not in pending.read_text() and pending.stat().st_mode & 0o777 == 0o600
    assert sshswitch.handle({"action": "off"}, pending) == (200, {"ok": True, "action": "off"})
    assert json.loads(pending.read_text()) == {"action": "off"}


@pytest.fixture
def root_side(tmp_path, monkeypatch):
    """apply() and boot() with every system command recorded instead of run."""
    calls = []
    for name in ("PENDING", "RESULT", "STATE"):
        monkeypatch.setattr(sshswitch, name, tmp_path / getattr(sshswitch, name).name)
    monkeypatch.setattr(sshswitch, "login_user", lambda: "vectorbridge")
    monkeypatch.setattr(sshswitch.subprocess, "run", lambda argv, **k: calls.append(
        " ".join(argv) + (f" <{k['input'].strip()}" if k.get("input") else "")) or R())
    return calls


def request(body):
    sshswitch.PENDING.write_text(json.dumps(body))


def state():
    return json.loads(sshswitch.STATE.read_text())


def test_on_sets_the_password_saves_the_setting_and_starts_ssh(root_side):
    request({"action": "on", "hash": HASH})
    assert sshswitch.apply() == 0
    assert root_side == [f"chpasswd -e <vectorbridge:{HASH}", "systemctl start ssh.service"]
    assert state() == {"action": "on", "hash": HASH} and sshswitch.STATE.stat().st_mode & 0o777 == 0o600
    assert json.loads(sshswitch.RESULT.read_text())["ok"] and not sshswitch.PENDING.exists()


def test_off_stops_ssh_and_keeps_the_password(root_side):
    sshswitch.STATE.write_text(json.dumps({"action": "on", "hash": HASH}))
    request({"action": "off"})
    assert sshswitch.apply() == 0
    assert root_side == ["systemctl stop ssh.service"]
    assert state() == {"action": "off", "hash": HASH}


def test_a_bad_request_is_removed_without_touching_the_login(root_side):
    request({"action": "on", "hash": "plain text"})
    assert sshswitch.apply() == 2
    assert root_side == [] and not sshswitch.PENDING.exists() and not sshswitch.STATE.exists()
    assert not json.loads(sshswitch.RESULT.read_text())["ok"]


def test_a_symlinked_request_is_not_followed(root_side, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"action": "on", "hash": HASH}))
    sshswitch.PENDING.symlink_to(target)
    assert sshswitch.apply() == 2 and root_side == []


def test_a_failed_command_is_reported(root_side, monkeypatch):
    def run(argv, **k):
        raise sshswitch.subprocess.CalledProcessError(1, argv)
    monkeypatch.setattr(sshswitch.subprocess, "run", run)
    request({"action": "on", "hash": HASH})
    assert sshswitch.apply() == 1
    assert not json.loads(sshswitch.RESULT.read_text())["ok"] and not sshswitch.PENDING.exists()


@pytest.mark.parametrize(("saved", "calls"), [
    (None, []),                                                     # the bench: left as it is
    ({"action": "on", "hash": HASH}, [f"chpasswd -e <vectorbridge:{HASH}", "systemctl start ssh.service"]),
    ({"action": "off", "hash": HASH}, [f"chpasswd -e <vectorbridge:{HASH}", "systemctl stop ssh.service"]),
    ({"action": "off"}, ["systemctl stop ssh.service"]),
])
def test_boot_puts_back_what_the_overlay_forgot(root_side, saved, calls):
    if saved is not None:
        sshswitch.STATE.write_text(json.dumps(saved))
    assert sshswitch.boot() == 0 and root_side == calls


def test_boot_refuses_a_bad_state(root_side):
    sshswitch.STATE.write_text(json.dumps({"action": "on", "hash": "nope"}))
    assert sshswitch.boot() == 1 and root_side == []



# ---- SSH keys (roadmap 4.11) ---------------------------------------------------------------


@pytest.fixture
def keys(tmp_path, monkeypatch):
    """save_keys() and restore_keys() with the login's home and KEYS under tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    user = pwd.struct_passwd(("vectorbridge", "x", os.getuid(), os.getgid(), "", str(home), "/bin/bash"))
    monkeypatch.setattr(sshswitch, "_login", lambda: user)
    monkeypatch.setattr(sshswitch, "KEYS", tmp_path / "persist" / "ssh" / "authorized_keys")
    monkeypatch.setattr(sshswitch, "STATE", tmp_path / "persist" / "ssh.json")
    sshswitch.KEYS.parent.parent.mkdir()
    monkeypatch.setattr(sshswitch.time, "sleep", lambda s: None)
    saves = []
    real_write = sshswitch.persist.atomic_write
    monkeypatch.setattr(sshswitch.persist, "atomic_write", lambda p, d, **k: saves.append(p) or real_write(p, d, **k))
    return home / ".ssh" / "authorized_keys", saves


def saved_keys(text, dir_mode=0o700):
    sshswitch.KEYS.parent.mkdir(mode=dir_mode)
    sshswitch.KEYS.parent.chmod(dir_mode)
    sshswitch.KEYS.write_text(text)
    sshswitch.KEYS.chmod(0o600)


def add_key(path, text, mode=0o600):
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def test_a_changed_key_file_is_saved_and_an_unchanged_one_is_not(keys):
    path, saves = keys
    add_key(path, "ssh-ed25519 AAAA mac\n")
    assert sshswitch.save_keys() == 0
    assert sshswitch.KEYS.read_text() == "ssh-ed25519 AAAA mac\n"
    assert sshswitch.KEYS.stat().st_mode & 0o777 == 0o600
    assert sshswitch.KEYS.parent.stat().st_mode & 0o777 == 0o700          # root only: not the web user's
    assert sshswitch.save_keys() == 0 and len(saves) == 1     # the same keys: /persist untouched
    add_key(path, "ssh-ed25519 AAAA mac\nssh-ed25519 BBBB laptop\n")
    assert sshswitch.save_keys() == 0 and len(saves) == 2
    assert "laptop" in sshswitch.KEYS.read_text()


def test_a_key_added_while_a_save_runs_is_saved_too(keys, monkeypatch):
    path, saves = keys
    add_key(path, "ssh-ed25519 AAAA mac\n")
    second = iter(["ssh-ed25519 AAAA mac\nssh-ed25519 BBBB laptop\n"])
    monkeypatch.setattr(sshswitch.time, "sleep", lambda s: add_key(path, next(second, path.read_text())))
    assert sshswitch.save_keys() == 0
    assert "laptop" in sshswitch.KEYS.read_text() and len(saves) == 2


def test_deleting_the_key_file_deletes_the_saved_copy(keys):
    path, _ = keys
    add_key(path, "ssh-ed25519 AAAA mac\n")
    assert sshswitch.save_keys() == 0
    path.unlink()
    assert sshswitch.save_keys() == 0 and not sshswitch.KEYS.exists()
    path.parent.rmdir()
    assert sshswitch.save_keys() == 0 and not sshswitch.KEYS.exists()


@pytest.mark.parametrize("bad", ["symlink", "dir_symlink", "fifo", "writable", "too_big"])
def test_root_saves_nothing_it_should_not_read(keys, tmp_path, bad):
    path, saves = keys
    secret = tmp_path / "secret"
    secret.write_text("not a key")
    if bad == "symlink":
        path.parent.mkdir(mode=0o700)
        path.symlink_to(secret)
    elif bad == "dir_symlink":
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "elsewhere" / "authorized_keys").write_text("not a key")
        path.parent.symlink_to(tmp_path / "elsewhere")
    elif bad == "fifo":
        path.parent.mkdir(mode=0o700)
        os.mkfifo(path)
    elif bad == "writable":
        add_key(path, "ssh-ed25519 AAAA mac\n", mode=0o666)
    else:
        add_key(path, "x" * (sshswitch.KEYS_MAX + 1))
    assert sshswitch.save_keys() == 1
    assert saves == [] and not sshswitch.KEYS.exists()


def test_boot_puts_the_saved_keys_back(keys, root_side):
    path, _ = keys
    saved_keys("ssh-ed25519 AAAA mac\n")
    assert sshswitch.boot() == 0                                   # no STATE: SSH left as it is
    assert path.read_text() == "ssh-ed25519 AAAA mac\n" and root_side == []
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_uid == os.getuid() and sorted(os.listdir(path.parent)) == ["authorized_keys"]
    add_key(path, "ssh-ed25519 OLD stale\n")                     # overwritten, not appended
    assert sshswitch.boot() == 0 and path.read_text() == "ssh-ed25519 AAAA mac\n"


def test_boot_leaves_the_keys_alone_when_none_were_saved(keys, root_side):
    path, _ = keys
    add_key(path, "ssh-ed25519 AAAA installed-in-maintenance\n")
    assert sshswitch.boot() == 0 and path.read_text() == "ssh-ed25519 AAAA installed-in-maintenance\n"


def test_boot_refuses_saved_keys_root_did_not_write(keys, root_side, tmp_path):
    path, _ = keys
    planted = tmp_path / "planted"
    planted.write_text("ssh-ed25519 EVIL web-user\n")
    sshswitch.KEYS.parent.mkdir(mode=0o700)
    sshswitch.KEYS.symlink_to(planted)
    sshswitch.STATE.write_text(json.dumps({"action": "on", "hash": HASH}))
    assert sshswitch.boot() == 1 and not path.exists()
    assert root_side == [f"chpasswd -e <vectorbridge:{HASH}", "systemctl start ssh.service"]   # SSH still applied


def test_boot_refuses_saved_keys_in_a_directory_others_can_open(keys, root_side):
    path, _ = keys
    saved_keys("ssh-ed25519 AAAA mac\n", dir_mode=0o755)
    assert sshswitch.boot() == 1 and not path.exists()

@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(apsetup, "UNSECURED", tmp_path / "ap-unsecured")
    monkeypatch.setattr(apsetup, "ap_profile_is_open", lambda: False)
    monkeypatch.setattr(sshswitch, "PENDING", tmp_path / "ssh-request.json")
    monkeypatch.setattr(sshswitch, "report", lambda: {"ok": True, "on": False})
    monkeypatch.setattr(sshswitch, "hash_password", lambda pw: HASH)
    monkeypatch.setattr(sshswitch.persist, "save", lambda p, d, **k: p.write_text(d))
    httpd = serve(TunerApp(lambda req: {"ok": True}), bind="127.0.0.1", port=0, docs_dir=tmp_path)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_ssh_routes(server):
    assert get(server + "/api/ssh")[0] == 200
    assert post(server + "/api/ssh", {"action": "on", "password": "correct horse"})[0] == 200
    assert json.loads(sshswitch.PENDING.read_text()) == {"action": "on", "hash": HASH}
    assert post(server + "/api/ssh", {"action": "on", "password": "short"})[0] == 400
    apsetup.UNSECURED.touch()                 # a stranger on the open AP cannot turn SSH on
    assert post(server + "/api/ssh", {"action": "off"})[0] == 403
    assert get(server + "/api/ssh")[0] == 302
