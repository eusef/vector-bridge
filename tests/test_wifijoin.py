"""Joining an existing Wi-Fi from the field UI: validation, the pending request, the root-side
apply for each of the three requests, and the routes."""
import json
import threading
from contextlib import contextmanager

import pytest
from test_portal import get

from vector_bridge import apsetup, wifijoin, wifiradio
from vector_bridge.tuner import TunerApp, serve


@pytest.mark.parametrize(("body", "ok"), [
    ({"action": "join", "ssid": "Home Wi-Fi", "passphrase": "correct horse"}, True),
    ({"action": "join", "ssid": "Café", "passphrase": ""}, True),               # open network
    ({"action": "join", "ssid": "", "passphrase": "correct horse"}, False),
    ({"action": "join", "ssid": "x" * 33, "passphrase": "correct horse"}, False),
    ({"action": "join", "ssid": "Home", "passphrase": "short"}, False),
    ({"action": "join", "ssid": "Ho\x01me", "passphrase": "correct horse"}, False),
    ({"action": "up", "ssid": "Home"}, True),
    ({"action": "ap"}, True),
    ({"action": "reboot"}, False),
    ("junk", False),
])
def test_validate(body, ok):
    assert (not wifijoin.validate(body)[1]) == ok


def test_handle_writes_a_private_request_and_rejects_a_bad_one(tmp_path):
    pending = tmp_path / "wifi-join.json"
    assert wifijoin.handle({"action": "join", "ssid": "", "passphrase": "x"}, pending)[0] == 400
    assert not pending.exists()
    code, out = wifijoin.handle({"action": "join", "ssid": " Home ", "passphrase": "correct horse"}, pending)
    assert code == 200 and out["ssid"] == "Home"
    assert json.loads(pending.read_text()) == {"action": "join", "ssid": "Home", "passphrase": "correct horse"}
    assert pending.stat().st_mode & 0o777 == 0o600


class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


@pytest.fixture(autouse=True)
def built_in_radio(tmp_path, monkeypatch):
    """Only the Pi's own radio."""
    monkeypatch.setattr(wifiradio, "pick", lambda: wifiradio.Radio("wlan0", usb=False))
    monkeypatch.setattr(wifiradio, "active_device", lambda: "wlan0")
    monkeypatch.setattr(wifiradio, "built_in", lambda: wifiradio.Radio("wlan0", usb=False))
    monkeypatch.setattr(wifiradio, "label", lambda dev: "Built-in, 2.4 GHz")


AP_TO_WLAN0 = ("nmcli connection modify --temporary vector-bridge-ap connection.interface-name wlan0 "
               "802-11-wireless.band bg 802-11-wireless.channel 6")
AP_SECURITY = "nmcli -g 802-11-wireless-security.key-mgmt connection show vector-bridge-ap"   # these fakes read it empty


@pytest.fixture
def root_side(tmp_path, monkeypatch):
    """apply() with every system effect recorded instead of run."""
    calls = []
    monkeypatch.setattr(wifijoin, "PENDING", tmp_path / "wifi-join.json")
    monkeypatch.setattr(wifijoin, "RESULT", tmp_path / "wifi-join-result.json")
    monkeypatch.setattr(wifijoin.apsetup, "_run", lambda *cmd: calls.append(" ".join(cmd)))
    monkeypatch.setattr(wifijoin.apsetup, "lock_down", lambda: calls.append("lockdown"))
    monkeypatch.setattr(wifijoin.apsetup, "overlay_on", lambda: False)
    monkeypatch.setattr(wifijoin, "_connect", lambda s, p, radio: calls.append(f"connect {s} {radio}") or R())
    monkeypatch.setattr(wifijoin, "saved_networks", lambda: ["Known"])
    monkeypatch.setattr(wifijoin.time, "sleep", lambda s: None)
    monkeypatch.setattr(wifijoin.manage, "_nmcli", lambda *a, **k: calls.append("nmcli " + " ".join(a)) or R(out="Home"))
    return calls


def request(body):
    wifijoin.PENDING.write_text(json.dumps(body))


def result():
    return json.loads(wifijoin.RESULT.read_text())


def test_join_in_field_mode_remembers_the_network_and_stays_joined(root_side, monkeypatch):
    monkeypatch.setattr(wifijoin.apsetup, "overlay_on", lambda: True)
    request({"action": "join", "ssid": "Home", "passphrase": "correct horse"})
    assert wifijoin.apply() == 0
    assert root_side == ["nmcli device wifi rescan ifname wlan0", "nmcli -e no -t -f SSID device wifi list ifname wlan0",
                         "connect Home wlan0",
                         "nmcli -g GENERAL.CONNECTION device show wlan0",
                         # no trip, no lockdown, no reboot; the saved profile is tied to no radio
                         "nmcli connection modify Home connection.autoconnect no connection.interface-name "]
    assert result()["ok"] and "vectorbridge.local" in result()["text"]
    assert not wifijoin.PENDING.exists()


def test_the_profile_is_written_inside_the_persist_rw_window(root_side, monkeypatch):
    windows = []

    @contextmanager
    def fake_writable(path, remount=None):
        windows.append(f"rw {path.name}")
        yield
        windows.append("ro")
    monkeypatch.setattr(wifijoin.persist, "writable", fake_writable)
    monkeypatch.setattr(wifijoin, "_connect", lambda s, p, radio: windows.append(f"connect {s}") or R())
    request({"action": "join", "ssid": "Home", "passphrase": "correct horse"})
    assert wifijoin.apply() == 0
    assert windows[:3] == ["rw networks", "connect Home", "ro"]


def test_a_refused_join_restores_the_access_point_without_a_reboot(root_side, monkeypatch):
    monkeypatch.setattr(wifijoin, "_connect", lambda s, p, radio: R(1, err="Error: Connection activation failed: (7) Secrets were required"))
    request({"action": "join", "ssid": "Home", "passphrase": "wrong horse"})
    assert wifijoin.apply() == 1
    assert "nmcli connection delete Home" in root_side
    assert root_side[-1] == "nmcli connection up vector-bridge-ap"
    assert "lockdown" not in root_side and "reboot" not in root_side
    assert not result()["ok"] and "Secrets were required" in result()["text"]
    assert not wifijoin.PENDING.exists()
    root_side.clear()                          # a remembered network keeps its profile after a wrong retype
    request({"action": "join", "ssid": "Known", "passphrase": "wrong horse"})
    assert wifijoin.apply() == 1
    assert "nmcli connection delete Known" not in root_side


def test_up_and_ap_need_no_trip(root_side):
    request({"action": "up", "ssid": "Home"})
    assert wifijoin.apply() == 0
    assert root_side == ["nmcli connection modify --temporary Home connection.interface-name wlan0",
                         "nmcli connection up Home"] and result()["ok"]
    root_side.clear()
    request({"action": "ap"})
    assert wifijoin.apply() == 0
    assert root_side == [AP_SECURITY, AP_TO_WLAN0, "nmcli connection up vector-bridge-ap"] and result()["ok"]
    assert not wifijoin.PENDING.exists()


def test_a_failed_up_falls_back_to_the_access_point(root_side, monkeypatch):
    # A deleted profile: nmcli refuses to re-point it, so it is never brought up.
    monkeypatch.setattr(wifijoin.manage, "_nmcli", lambda *a, **k: root_side.append("nmcli " + " ".join(a))
                        or (R(10, err="Error: unknown connection 'Gone'.") if "Gone" in a else R()))
    request({"action": "up", "ssid": "Gone"})
    assert wifijoin.apply() == 1
    assert root_side == ["nmcli connection modify --temporary Gone connection.interface-name wlan0",
                         AP_SECURITY, AP_TO_WLAN0, "nmcli connection up vector-bridge-ap"]
    assert not result()["ok"]


def test_a_bad_request_is_removed_without_touching_the_radio(root_side):
    request({"action": "join", "ssid": "Home", "passphrase": "short"})
    assert wifijoin.apply() == 2
    assert root_side == [] and not wifijoin.PENDING.exists()


def test_saved_networks_leave_out_the_access_point(monkeypatch):
    out = "vector-bridge-ap:802-11-wireless\nHome:802-11-wireless\nWired connection 1:802-3-ethernet\nCafé:802-11-wireless\n"
    monkeypatch.setattr(wifijoin.manage, "_nmcli", lambda *a, **k: R(out=out))
    assert wifijoin.saved_networks() == ["Café", "Home"]


def test_report_reads_the_device(monkeypatch, tmp_path):
    monkeypatch.setattr(wifijoin, "RESULT", tmp_path / "r.json")
    monkeypatch.setattr(wifijoin, "saved_networks", lambda: ["Home"])
    monkeypatch.setattr(wifijoin, "PENDING", tmp_path / "p.json")
    monkeypatch.setattr(wifijoin.manage, "_nmcli", lambda *a, **k: R(out="GENERAL.CONNECTION:Home\nGENERAL.STATE:100 (connected)\nIP4.ADDRESS[1]:192.0.2.7/24\n"))
    rep = wifijoin.report()
    assert (rep["on_ap"], rep["connection"], rep["ip"], rep["result"]) == (False, "Home", "192.0.2.7", None)
    assert rep["radio"] == "Built-in, 2.4 GHz" and rep["radio_usb"] is False   # the home page's adapter notice keys on this


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(apsetup, "UNSECURED", tmp_path / "ap-unsecured")
    monkeypatch.setattr(apsetup, "ap_profile_is_open", lambda: False)
    monkeypatch.setattr(wifijoin, "PENDING", tmp_path / "wifi-join.json")
    monkeypatch.setattr(wifijoin, "report", lambda: {"ok": True, "on_ap": True})
    monkeypatch.setattr(wifijoin.persist, "save", lambda p, d, **k: p.write_text(d))
    httpd = serve(TunerApp(lambda req: {"ok": True}), bind="127.0.0.1", port=0, docs_dir=tmp_path)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def post(url, body):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_join_routes(server):
    assert get(server + "/api/join")[0] == 200
    code, out = post(server + "/api/join", {"action": "ap"})
    assert (code, out["ok"]) == (200, True) and json.loads(wifijoin.PENDING.read_text()) == {"action": "ap"}
    assert post(server + "/api/join", {"action": "join", "ssid": "", "passphrase": ""})[0] == 400
    apsetup.UNSECURED.touch()                 # a stranger on the open AP cannot move the bridge
    assert post(server + "/api/join", {"action": "ap"})[0] == 403
    assert get(server + "/api/join")[0] == 302


# ---- home network: rejoined at boot when in range ------------------------------------


def test_home_is_set_and_cleared_only_for_a_remembered_network(tmp_path, monkeypatch):
    home = tmp_path / "home-network.json"
    monkeypatch.setattr(wifijoin, "HOME", home)
    monkeypatch.setattr(wifijoin, "saved_networks", lambda: ["HomeNet"])
    monkeypatch.setattr(wifijoin.persist, "save", lambda p, d, **k: p.write_text(d))
    assert wifijoin.handle({"action": "home", "ssid": "Stranger"})[0] == 400 and not home.exists()
    assert wifijoin.handle({"action": "home", "ssid": "HomeNet"}) == (200, {"ok": True, "action": "home", "ssid": "HomeNet"})
    assert wifijoin.home_network() == "HomeNet"
    assert wifijoin.handle({"action": "home", "ssid": ""})[0] == 200 and wifijoin.home_network() == ""


def test_home_never_reaches_the_root_queue():
    assert wifijoin.validate({"action": "home", "ssid": "HomeNet"})[1]  # root rejects it if found in the pending file


@pytest.fixture
def at_boot(tmp_path, monkeypatch):
    """boot() on the access point, home marked; `scan` is what each rescan finds, `up_rc` the join result."""
    state = {"scan": [["HomeNet"]], "up_rc": 0, "rescan_rc": 0, "calls": []}
    (tmp_path / "home.json").write_text(json.dumps({"ssid": "HomeNet"}))
    monkeypatch.setattr(wifijoin, "HOME", tmp_path / "home.json")
    monkeypatch.setattr(wifijoin, "RESULT", tmp_path / "result.json")
    monkeypatch.setattr(wifijoin, "saved_networks", lambda: ["HomeNet"])
    monkeypatch.setattr(wifijoin.persist, "save", lambda p, d, **k: p.write_text(d))
    monkeypatch.setattr(wifijoin.time, "sleep", lambda s: None)
    scans = iter(range(100))

    def nmcli(*a, **k):
        cmd = " ".join(a)
        state["calls"].append(cmd)
        if "device show" in cmd:
            return R(out="GENERAL.CONNECTION:vector-bridge-ap\n")
        if "802-11-wireless.ssid" in cmd:
            return R(out="VectorBridge-B08a\n" if "vector-bridge-ap" in cmd else "HomeNet\n")
        if "wifi list" in cmd:
            found = state["scan"][min(next(scans), len(state["scan"]) - 1)]
            return R(out="\n".join(found) + "\n")
        if "connection up HomeNet" in cmd:
            return R(state["up_rc"], err="Error: Connection activation failed")
        if "rescan" in cmd:
            return R(state["rescan_rc"], err="Error: Scanning not allowed while in AP mode")
        return R()
    monkeypatch.setattr(wifijoin.manage, "_nmcli", nmcli)
    return state


def test_boot_rejoins_the_home_network_when_it_is_in_range(at_boot):
    assert wifijoin.boot() == 0
    assert "connection up HomeNet" in at_boot["calls"]
    assert "connection up vector-bridge-ap" not in at_boot["calls"]
    assert json.loads(wifijoin.RESULT.read_text())["text"] == "Rejoined HomeNet at boot."


def test_boot_keeps_the_access_point_when_home_is_out_of_range(at_boot):
    at_boot["scan"] = [["Neighbour"]]  # in the field: the home network never shows up
    assert wifijoin.boot() == 0
    assert not any(c.startswith("connection up") for c in at_boot["calls"])
    assert sum("rescan" in c for c in at_boot["calls"]) == 3


def test_boot_retries_an_empty_first_scan(at_boot):
    at_boot["scan"] = [[], ["HomeNet"]]
    assert wifijoin.boot() == 0 and "connection up HomeNet" in at_boot["calls"]


def test_a_failed_rejoin_at_boot_brings_the_access_point_back(at_boot):
    at_boot["up_rc"] = 4
    assert wifijoin.boot() == 1
    assert at_boot["calls"][-1] == "connection up vector-bridge-ap"
    assert json.loads(wifijoin.RESULT.read_text())["ok"] is False


def test_boot_does_nothing_without_a_home_network(at_boot):
    wifijoin.HOME.write_text(json.dumps({"ssid": ""}))
    assert wifijoin.boot() == 0 and at_boot["calls"] == []


def test_when_the_access_point_cannot_scan_boot_pauses_it_to_look(at_boot):
    # NetworkManager refuses to scan in AP mode; with the access point down the fourth scan works.
    at_boot["rescan_rc"], at_boot["scan"] = 1, [[], [], [], ["HomeNet"]]
    assert wifijoin.boot() == 0
    calls = at_boot["calls"]
    assert "device disconnect wlan0" in calls
    assert calls.index("device disconnect wlan0") < calls.index("connection up HomeNet")
    assert any(c.endswith("ifname wlan0 --rescan yes") for c in calls)
    assert not any("--wait 30" in c for c in calls)


def test_an_adapter_that_accepts_the_scan_but_finds_nothing_pauses_too(at_boot):
    # rtw88_8821au (TP-Link Archer T2U Nano): the rescan returns 0, the driver refuses it, the list stays empty.
    at_boot["scan"] = [[], [], [], ["HomeNet"]]
    assert wifijoin.boot() == 0
    assert "device disconnect wlan0" in at_boot["calls"]
    assert "connection up HomeNet" in at_boot["calls"]


def test_the_bridges_own_network_left_in_the_list_does_not_count_as_a_scan(at_boot):
    # 2026-10-10 bench: every scan failed, and the list still held the access point's own name.
    at_boot["scan"] = [["VectorBridge-B08a"]] * 3 + [["VectorBridge-B08a", "HomeNet"]]
    assert wifijoin.boot() == 0
    assert "device disconnect wlan0" in at_boot["calls"]
    assert "connection up HomeNet" in at_boot["calls"]


def test_a_radio_that_can_scan_never_pauses_the_access_point(at_boot):
    at_boot["scan"] = [["Neighbour"]]
    assert wifijoin.boot() == 0
    assert not any("disconnect" in c for c in at_boot["calls"])


def test_one_scan_that_found_networks_is_enough_to_skip_the_pause(at_boot):
    at_boot["scan"] = [["Neighbour"], []]  # a later scan came back empty: the radio can still scan
    assert wifijoin.boot() == 0
    assert not any("disconnect" in c for c in at_boot["calls"])


def test_home_out_of_range_after_the_pause_brings_the_access_point_back(at_boot):
    at_boot["rescan_rc"], at_boot["scan"] = 1, [[]]  # in the field: nothing in range, no scan in AP mode
    assert wifijoin.boot() == 0
    assert at_boot["calls"][-1] == "connection up vector-bridge-ap"
    assert "connection up HomeNet" not in at_boot["calls"]


def test_an_access_point_that_will_not_come_back_after_the_pause_fails(at_boot, monkeypatch):
    at_boot["scan"] = [[]]
    monkeypatch.setattr(wifijoin, "_up", lambda name: R(4))
    assert wifijoin.boot() == 1


def test_a_symlinked_home_marker_is_ignored(tmp_path, monkeypatch):
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"ssid": "HomeNet"}))
    (tmp_path / "home.json").symlink_to(real)
    monkeypatch.setattr(wifijoin, "HOME", tmp_path / "home.json")
    assert wifijoin.home_network() == ""


def test_join_goes_on_the_usb_adapter_and_reuses_a_profile_saved_for_the_built_in_radio(root_side, monkeypatch):
    monkeypatch.setattr(wifiradio, "pick", lambda: wifiradio.Radio("wlan1", usb=True, channels=(149,)))
    monkeypatch.setattr(wifijoin, "_visible", lambda radio, rescan=False: {"Known"})
    request({"action": "join", "ssid": "Known", "passphrase": "correct horse"})
    assert wifijoin.apply() == 0
    # Re-pointed first: a profile tied to wlan0 would otherwise be copied as "Known 1".
    assert root_side[:3] == ["nmcli connection modify --temporary Known connection.interface-name wlan1",
                             "nmcli device wifi rescan ifname wlan1", "connect Known wlan1"]


def test_join_scans_until_the_network_shows_up(root_side, monkeypatch):
    # The access point's radio: the first scan after the rescan does not hold the network yet.
    lists = iter([set(), {"Home"}])
    monkeypatch.setattr(wifijoin, "_visible", lambda radio, rescan=False: next(lists))
    request({"action": "join", "ssid": "Home", "passphrase": "correct horse"})
    assert wifijoin.apply() == 0
    assert root_side[:3] == ["nmcli device wifi rescan ifname wlan0", "nmcli device wifi rescan ifname wlan0", "connect Home wlan0"]
    assert not any("disconnect" in c for c in root_side)


def test_join_pauses_the_access_point_when_no_scan_shows_the_network(root_side, monkeypatch):
    # The home network, 2.0 card, 2026-10-15: the scans on the access point never held it, and nmcli
    # refused it unseen. Other networks in the list do not make the join give up early.
    monkeypatch.setattr(wifijoin, "_visible", lambda radio, rescan=False: {"Home"} if rescan else {"Neighbour"})
    request({"action": "join", "ssid": "Home", "passphrase": "correct horse"})
    assert wifijoin.apply() == 0
    assert sum("rescan ifname" in c for c in root_side) == 3
    assert root_side.index("nmcli device disconnect wlan0") < root_side.index("connect Home wlan0")


def test_apply_waits_for_the_radio_lock(root_side):
    request({"action": "ap"})
    seen = []
    with wifiradio.locked():
        t = threading.Thread(target=lambda: seen.append(wifijoin.apply()))
        t.start()
        t.join(0.3)
        assert t.is_alive() and root_side == []   # the watcher is moving the radio: wait
    t.join(5)
    assert seen == [0] and root_side[-1] == "nmcli connection up vector-bridge-ap"


def test_an_adapter_that_refuses_the_access_point_leaves_it_to_the_built_in_radio(root_side, monkeypatch):
    monkeypatch.setattr(wifiradio, "pick", lambda: wifiradio.Radio("wlan1", usb=True))
    tried = []

    def bring_up(con, radio, is_ap=None):
        tried.append(radio.name)
        return R(1 if radio.name == "wlan1" else 0, err="Error: Connection activation failed")
    monkeypatch.setattr(wifiradio, "bring_up", bring_up)
    request({"action": "ap"})
    assert wifijoin.apply() == 0 and tried == ["wlan1", "wlan0"]
