"""SSH on or off (R-D4): the password check, the request the web side leaves (a hash, never the
password), the root side for on, off and a bad request, the boot re-apply, and the routes."""
import json
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
