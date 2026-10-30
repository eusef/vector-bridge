"""The owner's Wi-Fi name and passphrase: validation, the pending request, the root-side apply,
and the open-AP gate on the web UI and on transmit."""
import json
import subprocess
import threading
import urllib.request

import pytest
from test_ladder import FakeRig, make
from test_portal import get

from vector_bridge import apsetup
from vector_bridge.manage import ap_connection_argv
from vector_bridge.tuner import TunerApp, serve


@pytest.mark.parametrize(("suffix", "passphrase", "ok"), [
    ("K7ABC_go-kit", "correct horse", True),
    ("", "correct horse", False),
    ("has space", "correct horse", False),
    ("x" * 20, "correct horse", False),          # VectorBridge- plus 20 is over 32 bytes
    ("K7ABC", "short", False),
    ("K7ABC", "x" * 64, False),
    ("K7ABC", "café-passphrase", False),
    ("K7ABC", None, False),
])
def test_validate(suffix, passphrase, ok):
    ssid, _, errors = apsetup.validate(suffix, passphrase)
    assert (not errors) == ok
    assert ssid.startswith("VectorBridge-")


def test_an_open_access_point_has_no_security_arguments():
    assert not [a for a in ap_connection_argv("VectorBridge-EF12", None) if a.startswith("wifi-sec")]
    assert "wifi-sec.psk" in ap_connection_argv("VectorBridge-EF12", "correct horse")


def test_handle_writes_a_private_request_and_rejects_a_bad_one(tmp_path):
    pending = tmp_path / "ap-pending.json"
    assert apsetup.handle({"suffix": "bad name", "passphrase": "correct horse"}, pending)[0] == 400
    assert not pending.exists()
    code, out = apsetup.handle({"suffix": "K7ABC", "passphrase": "correct horse"}, pending)
    assert code == 200 and out["ssid"] == "VectorBridge-K7ABC"
    assert json.loads(pending.read_text()) == {"ssid": "VectorBridge-K7ABC", "passphrase": "correct horse"}
    assert pending.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(("is_open", "suffix", "code"), [(True, "8282", 400), (True, "chicken", 200), (False, "8282", 200)])
def test_securing_the_open_network_needs_a_new_name(tmp_path, monkeypatch, is_open, suffix, code):
    # Phones that joined the open network keep its profile and never join it once it has a passphrase.
    monkeypatch.setattr(apsetup, "unsecured", lambda: is_open)
    monkeypatch.setattr(apsetup, "current_ssid", lambda: "VectorBridge-8282")
    pending = tmp_path / "ap-pending.json"
    got, out = apsetup.handle({"suffix": suffix, "passphrase": "correct horse"}, pending)
    assert got == code and pending.exists() == (code == 200)
    if code == 400:
        assert "new name" in out["errors"][0]


@pytest.fixture
def root_side(tmp_path, monkeypatch):
    """apply() with every system effect recorded instead of run."""
    calls = []
    monkeypatch.setattr(apsetup, "PENDING", tmp_path / "ap-pending.json")
    monkeypatch.setattr(apsetup, "UNSECURED", tmp_path / "ap-unsecured")
    monkeypatch.setattr(apsetup, "WIFI_TXT", tmp_path / "vector-bridge-wifi.txt")
    monkeypatch.setattr(apsetup, "_run", lambda *cmd: calls.append(" ".join(cmd)))
    monkeypatch.setattr(apsetup, "lock_down", lambda: calls.append("lockdown"))
    monkeypatch.setattr(apsetup, "write_wifi_txt", lambda s, p: apsetup.WIFI_TXT.write_text(f"{s}\n{p}\n"))
    monkeypatch.setattr(apsetup, "_current", lambda: ("VectorBridge-OLD", "old passphrase"))
    monkeypatch.setattr(apsetup, "current_ssid", lambda: "VectorBridge-OLD")
    monkeypatch.setattr(apsetup, "FIRSTBOOT_FLAG", tmp_path / "firstboot")
    monkeypatch.setattr(apsetup, "ap_profile_is_open", lambda: False)
    monkeypatch.setattr(apsetup.manage, "_ap_create", lambda s, p, **k: calls.append(f"ap {s} {p}") or (s, p))
    monkeypatch.setattr(apsetup.manage, "_nmcli", lambda *a, **k: calls.append("nmcli " + " ".join(a)))
    apsetup.UNSECURED.touch()
    return calls


UP = "nmcli connection up vector-bridge-ap"


def test_apply_in_field_mode_stores_everything_without_a_reboot(root_side, monkeypatch):
    monkeypatch.setattr(apsetup, "overlay_on", lambda: True)
    apsetup.handle({"suffix": "K7ABC", "passphrase": "correct horse"}, apsetup.PENDING)
    assert apsetup.apply() == 0
    assert root_side == ["ap VectorBridge-K7ABC correct horse", UP]      # no lockdown, no reboot
    assert not apsetup.PENDING.exists() and not apsetup.UNSECURED.exists()
    assert apsetup.WIFI_TXT.read_text() == "VectorBridge-K7ABC\ncorrect horse\n"


def test_apply_on_the_writable_root_stores_everything_then_locks_down(root_side, monkeypatch):
    monkeypatch.setattr(apsetup, "overlay_on", lambda: False)
    apsetup.handle({"suffix": "K7ABC", "passphrase": "correct horse"}, apsetup.PENDING)
    assert apsetup.apply() == 0
    assert root_side == ["ap VectorBridge-K7ABC correct horse", UP, "lockdown", "reboot"]
    text = apsetup.WIFI_TXT.read_text()
    assert "VectorBridge-K7ABC" in text and "correct horse" in text
    assert not apsetup.PENDING.exists() and not apsetup.UNSECURED.exists()


@pytest.mark.parametrize("request_text", [
    json.dumps({"ssid": "XectorBridge-K7ABC", "passphrase": "correct horse"}),   # only the prefix is wrong
    json.dumps({"ssid": "VectorBridge-bad name", "passphrase": "correct horse"}),  # only the suffix is wrong
    json.dumps({"ssid": "VectorBridge-K7ABC", "passphrase": "short"}),
    json.dumps({"ssid": ["VectorBridge-K7ABC"], "passphrase": "correct horse"}),
    "not json",
])
def test_apply_does_not_trust_the_request_file(root_side, monkeypatch, request_text):
    monkeypatch.setattr(apsetup, "overlay_on", lambda: False)
    apsetup.PENDING.write_text(request_text)
    assert apsetup.apply() == 2
    assert root_side == [] and not apsetup.PENDING.exists()
    assert apsetup.UNSECURED.exists()          # still open, still gated


def test_apply_refuses_a_symlinked_request(root_side, monkeypatch, tmp_path):
    monkeypatch.setattr(apsetup, "overlay_on", lambda: False)
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"ssid": "VectorBridge-K7ABC", "passphrase": "correct horse"}))
    apsetup.PENDING.symlink_to(real)
    assert apsetup.apply() == 2
    assert root_side == [] and not apsetup.PENDING.is_symlink() and real.exists()


def _refuse_first(made):
    def create(ssid, passphrase, **k):
        made.append((ssid, passphrase, k.get("open_ap", False)))
        return None if len(made) == 1 else (ssid, passphrase)
    return create


def test_a_refused_profile_restores_the_old_one_and_never_leaves_the_request(root_side, monkeypatch):
    """The path unit fires for as long as the request exists, and _ap_create deletes the old profile first."""
    monkeypatch.setattr(apsetup, "overlay_on", lambda: False)
    apsetup.UNSECURED.unlink()                 # a later change on a secured bridge
    made = []
    monkeypatch.setattr(apsetup.manage, "_ap_create", _refuse_first(made))
    apsetup.handle({"suffix": "K7ABC", "passphrase": "correct horse"}, apsetup.PENDING)
    assert apsetup.apply() == 1
    assert made == [("VectorBridge-K7ABC", "correct horse", False), ("VectorBridge-OLD", "old passphrase", False)]
    assert root_side == [UP, "lockdown", "reboot"] and not apsetup.PENDING.exists()
    assert "old passphrase" in apsetup.WIFI_TXT.read_text()    # the card's copy says what the radio does


def test_a_refused_profile_on_first_boot_stays_open_and_writable(root_side, monkeypatch):
    monkeypatch.setattr(apsetup, "overlay_on", lambda: False)
    monkeypatch.setattr(apsetup, "_current", lambda: ("VectorBridge-EF12", ""))
    made = []
    monkeypatch.setattr(apsetup.manage, "_ap_create", _refuse_first(made))
    apsetup.handle({"suffix": "K7ABC", "passphrase": "correct horse"}, apsetup.PENDING)
    assert apsetup.apply() == 1
    assert made[1] == ("VectorBridge-EF12", None, True)
    # the restored open AP goes back on the air; no lockdown, no reboot
    assert root_side == [UP] and not apsetup.PENDING.exists() and apsetup.UNSECURED.exists()
    assert "VectorBridge-EF12" in apsetup.WIFI_TXT.read_text()


def test_the_gate_lifts_only_after_the_secured_profile_is_on_the_air(root_side, monkeypatch):
    """Until `connection up` the radio is still the open network."""
    monkeypatch.setattr(apsetup, "overlay_on", lambda: False)
    seen = []
    monkeypatch.setattr(apsetup.manage, "_nmcli", lambda *a, **k: seen.append(apsetup.UNSECURED.exists()))
    apsetup.handle({"suffix": "K7ABC", "passphrase": "correct horse"}, apsetup.PENDING)
    assert apsetup.apply() == 0
    assert seen == [True] and not apsetup.UNSECURED.exists()


def test_a_text_file_that_cannot_be_written_does_not_strand_the_card(root_side, monkeypatch):
    monkeypatch.setattr(apsetup, "overlay_on", lambda: False)

    def full(ssid, passphrase):
        raise OSError("No space left on device")
    monkeypatch.setattr(apsetup, "write_wifi_txt", full)
    apsetup.handle({"suffix": "K7ABC", "passphrase": "correct horse"}, apsetup.PENDING)
    assert apsetup.apply() == 3
    assert root_side[-2:] == ["lockdown", "reboot"]
    assert not apsetup.PENDING.exists() and not apsetup.UNSECURED.exists()


@pytest.mark.parametrize("broken", ["_current", "lock_down"])
def test_any_failure_on_the_writable_root_still_drops_the_request(root_side, monkeypatch, broken):
    monkeypatch.setattr(apsetup, "overlay_on", lambda: False)

    def boom():
        raise OSError("nmcli went away")
    monkeypatch.setattr(apsetup, broken, boom)
    apsetup.handle({"suffix": "K7ABC", "passphrase": "correct horse"}, apsetup.PENDING)
    assert apsetup.apply() == 4
    assert not apsetup.PENDING.exists() and "reboot" not in root_side


def test_a_refused_profile_with_unreadable_old_values_never_reopens_a_secured_bridge(root_side, monkeypatch):
    monkeypatch.setattr(apsetup, "overlay_on", lambda: False)
    monkeypatch.setattr(apsetup, "_current", lambda: ("", ""))
    apsetup.UNSECURED.unlink()
    made = []

    def create(ssid, passphrase, **k):
        made.append((ssid, passphrase, k.get("open_ap", False)))
        return None if len(made) == 1 else ("VectorBridge-EF12", "generated-by-manage")
    monkeypatch.setattr(apsetup.manage, "_ap_create", create)
    apsetup.handle({"suffix": "K7ABC", "passphrase": "correct horse"}, apsetup.PENDING)
    assert apsetup.apply() == 1
    assert made[1] == (None, None, False)
    assert "generated-by-manage" in apsetup.WIFI_TXT.read_text() and "lockdown" in root_side


def test_an_open_profile_gates_even_without_the_marker(tmp_path, monkeypatch):
    """/persist failing to mount, or the web user deleting the marker, must not open the UI."""
    monkeypatch.setattr(apsetup, "UNSECURED", tmp_path / "missing")
    monkeypatch.setattr(apsetup, "ap_profile_is_open", lambda: True)
    assert apsetup.unsecured()
    monkeypatch.setattr(apsetup, "ap_profile_is_open", lambda: False)
    assert not apsetup.unsecured()


def test_the_passphrase_is_not_announced_under_systemd(monkeypatch, capsys):
    ok = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    monkeypatch.setattr(apsetup.manage, "_nmcli", lambda *a, **k: ok)
    assert apsetup.manage._ap_create("VectorBridge-K7ABC", "correct horse", announce=False)
    assert "correct horse" not in capsys.readouterr().out


@pytest.fixture
def open_server(tmp_path, monkeypatch):
    monkeypatch.setattr(apsetup, "UNSECURED", tmp_path / "ap-unsecured")
    monkeypatch.setattr(apsetup, "current_ssid", lambda: "VectorBridge-EF12")
    monkeypatch.setattr(apsetup, "ap_profile_is_open", lambda: False)
    apsetup.UNSECURED.touch()
    httpd = serve(TunerApp(lambda req: {"ok": True}), bind="127.0.0.1", port=0, docs_dir=tmp_path)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_while_the_access_point_is_open_only_the_wifi_page_is_served(open_server):
    for path in ("/", "/tuner", "/transmit", "/api/status"):
        code, headers, _ = get(open_server + path)
        assert (code, headers["Location"]) == (302, "/wifi"), path
    assert get(open_server + "/wifi")[0] == 200
    assert get(open_server + "/api/wifi")[0] == 200
    req = urllib.request.Request(open_server + "/api/gain", data=b"{}", headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 403
    apsetup.UNSECURED.unlink()
    assert get(open_server + "/")[0] == 200


def test_transmit_is_refused_while_the_access_point_is_open(tmp_path, monkeypatch):
    rig = FakeRig(levels={"VD_METER": 12.0})
    lad, _, _ = make(tmp_path, rig, profile="usdx")
    lad.confirm_load("antenna")
    lad.override(["drive"])
    assert lad.run_carrier()["result"]["ok"]
    monkeypatch.setattr(apsetup, "UNSECURED", tmp_path / "ap-unsecured")
    monkeypatch.setattr(apsetup, "ap_profile_is_open", lambda: False)
    apsetup.UNSECURED.touch()
    assert not lad.key("client")
    apsetup.UNSECURED.unlink()
    assert lad.key("client")


def test_the_ladders_own_carrier_is_refused_while_the_access_point_is_open(tmp_path, monkeypatch):
    lad, _, _ = make(tmp_path, FakeRig(levels={"VD_METER": 12.0}), profile="usdx")
    lad.confirm_load("antenna")
    lad.override(["drive"])
    monkeypatch.setattr(apsetup, "UNSECURED", tmp_path / "ap-unsecured")
    monkeypatch.setattr(apsetup, "ap_profile_is_open", lambda: False)
    apsetup.UNSECURED.touch()
    assert not lad.run_carrier()["result"]["ok"]
    apsetup.UNSECURED.unlink()
    assert lad.run_carrier()["result"]["ok"]


def test_a_request_that_cannot_be_removed_stops_the_path_unit(root_side, monkeypatch):
    """Otherwise vb-ap-apply.path starts the service again for ever."""
    stopped = []
    real = apsetup.subprocess.run
    monkeypatch.setattr(apsetup.subprocess, "run", lambda cmd, **k: stopped.append(cmd) if cmd[0] == "systemctl" else real(cmd, **k))

    def no_remount(path):
        raise OSError("remount failed")
    monkeypatch.setattr(apsetup.persist, "writable", no_remount)
    apsetup.PENDING.write_text("junk")
    assert apsetup.apply() == 2
    assert stopped == [["systemctl", "stop", "vb-ap-apply.path"]]


def test_a_request_that_is_a_directory_is_removed(root_side):
    apsetup.PENDING.mkdir()
    (apsetup.PENDING / "x").touch()
    assert apsetup.apply() == 2
    assert not apsetup.PENDING.exists()


def test_a_secured_bridge_disarms_a_half_finished_first_boot(root_side, monkeypatch):
    monkeypatch.setattr(apsetup, "overlay_on", lambda: False)
    apsetup.FIRSTBOOT_FLAG.touch()
    apsetup.PENDING.write_text(json.dumps({"ssid": "VectorBridge-Mine", "passphrase": "good passphrase"}))
    assert apsetup.apply() == 0
    assert not apsetup.FIRSTBOOT_FLAG.exists()


def test_a_missing_profile_is_not_remembered_as_secured(monkeypatch):
    """apply() deletes the profile for a moment; a poll in that window must not pin the gate open."""
    answers = [(10, "", "no such connection"), (0, "ap\n\n", "")]
    monkeypatch.setattr(apsetup, "_open_cache", [0.0, None])
    monkeypatch.setattr(apsetup.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, *answers.pop(0)))
    assert not apsetup.ap_profile_is_open()
    assert apsetup.ap_profile_is_open()
