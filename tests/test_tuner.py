"""Browser tuner: dBFS conversion, amixer parsing, and the status/gain/mixers routes."""

import http.client
import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from vector_bridge.cat import CatError
from vector_bridge.tuner import (
    SAVE_TIMEOUT_S,
    TunerApp,
    card_index,
    control_timeout,
    dbfs,
    parse_mixers,
    serve,
    vox_aware_ptt,
)

# Real `amixer -c 0` output from the bench Pi with the KT USB Audio adapter.
AMIXER = """Simple mixer control 'Headphone',0
  Capabilities: pvolume pswitch pswitch-joined
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 100
  Mono:
  Front Left: Playback 70 [70%] [-15.00dB] [on]
  Front Right: Playback 70 [70%] [-15.00dB] [on]
Simple mixer control 'Mic',0
  Capabilities: cvolume cvolume-joined cswitch cswitch-joined
  Capture channels: Mono
  Limits: Capture 0 - 100
  Mono: Capture 85 [85%] [-7.50dB] [on]
"""

DEVICE = "alsa:hw:0 KT USB Audio usb=12d1:0010 cap1ch/play1ch period=480x4"


def app(reply, mixers=None):
    """A TunerApp whose control channel returns `reply` (or None to mean unreachable)."""
    sent = []

    def control(req):
        sent.append(req)
        return reply

    a = TunerApp(control, mixer_fn=lambda card: mixers or [])
    return a, sent


@pytest.mark.parametrize("peak,expected", [
    (32767, 0.0),       # full scale
    (16384, -6.0),      # half amplitude is one bit down
    (8000, -12.2),      # bottom of the healthy receive window
    (16000, -6.2),      # top of it
    (0, -90.0),         # silence floors instead of going infinite
    (99999, 0.0),       # a count above full scale still reads 0, never positive
])
def test_dbfs_converts_int16_counts(peak, expected):
    assert dbfs(peak) == expected


def test_card_index_reads_the_telemetry_device_string():
    assert card_index(DEVICE) == 0
    assert card_index("fake:1kHz-tone") is None
    assert card_index("") is None


def test_parse_mixers_keeps_name_direction_percent_and_db():
    controls = parse_mixers(AMIXER)
    assert controls == [
        {"name": "Headphone", "index": 0, "direction": "playback", "percent": 70, "db": -15.0},
        {"name": "Mic", "index": 0, "direction": "capture", "percent": 85, "db": -7.5},
    ]


def test_parse_mixers_ignores_the_limits_line():
    """`Limits: Playback 0 - 100` carries no percent and must not become the reading."""
    controls = parse_mixers(AMIXER)
    assert controls[0]["percent"] == 70


def test_status_adds_dbfs_and_the_target_window():
    a, _ = app({"rx_peak": 16384, "tx_peak": 0, "rx_rms": 8000, "tx_rms": 0, "clip_count": 0})
    code, body = a.status()
    assert code == 200
    assert body["rx_dbfs"] == -6.0
    assert body["rx_rms_dbfs"] == -12.2
    assert body["tx_dbfs"] == -90.0
    assert (body["target_low_dbfs"], body["target_high_dbfs"]) == (-12.2, -6.2)


def test_status_reports_503_when_the_service_does_not_answer():
    a, _ = app(None)
    code, body = a.status()
    assert code == 503
    assert body == {"ok": False, "error": "service_unreachable", "device_id": a.device_id}


def test_set_gain_proxies_to_the_control_channel():
    a, sent = app({"ok": True, "tx": 70, "rx": 40})
    code, body = a.set_gain({"rx": 40})
    assert code == 200
    assert body["rx"] == 40
    assert sent == [{"cmd": "set_gain", "rx": 40}]


@pytest.mark.parametrize("body", [{"rx": 101}, {"rx": -1}, {"tx": "loud"}, {"rx": True}])
def test_set_gain_rejects_out_of_range_and_non_numeric(body):
    a, sent = app({"ok": True})
    code, _ = a.set_gain(body)
    assert code == 400
    assert sent == []


def test_set_gain_rejects_a_request_naming_neither_direction():
    a, sent = app({"ok": True})
    code, body = a.set_gain({})
    assert (code, body["error"]) == (400, "no_gain_given")
    assert sent == []


def test_mixers_lists_the_cards_controls():
    a, _ = app({"alsa_device": DEVICE}, mixers=parse_mixers(AMIXER))
    code, body = a.mixers()
    assert code == 200
    assert body["card"] == 0
    assert [c["name"] for c in body["controls"]] == ["Headphone", "Mic"]


def test_mixers_is_empty_on_a_backend_with_no_card():
    a, _ = app({"alsa_device": "fake:1kHz-tone"})
    code, body = a.mixers()
    assert (code, body["card"], body["controls"]) == (200, None, [])


# ---- HTTP round trip ----------------------------------------------------------


@pytest.fixture
def server():
    """The real handler on an ephemeral loopback port, backed by a stub control channel."""
    a, sent = app({"ok": True, "tx": 70, "rx": 85, "rx_peak": 16384, "tx_peak": 0,
                   "rx_rms": 8000, "tx_rms": 0, "clip_count": 0, "uptime_s": 3,
                   "alsa_device": DEVICE})
    httpd = serve(a, bind="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", sent
    finally:
        httpd.shutdown()
        httpd.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def test_http_serves_the_page(server):
    base, _ = server
    status, body = get(base + "/tuner")  # the readiness check owns "/" now
    assert status == 200
    assert b"VectorBridge Tuner" in body


def test_http_status_returns_dbfs_json(server):
    base, _ = server
    status, body = get(base + "/api/status")
    assert status == 200
    assert json.loads(body)["rx_dbfs"] == -6.0


def test_http_post_gain_reaches_the_control_channel(server):
    base, sent = server
    req = urllib.request.Request(base + "/api/gain", data=b'{"rx": 40}',
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
    assert {"cmd": "set_gain", "rx": 40} in sent


def test_http_post_tx_audio_cap_reaches_the_control_channel(server):
    base, sent = server
    req = urllib.request.Request(base + "/api/tx_audio_cap", data=b'{"seconds": 120}',
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
    assert {"cmd": "set_tx_audio_cap", "seconds": 120} in sent


@pytest.mark.parametrize("body", [{"seconds": 601}, {"seconds": -1}, {"seconds": 1.5}, {"seconds": True}, {}])
def test_set_tx_audio_cap_rejects_bad_seconds(body):
    a, sent = app({"ok": True})
    assert a.set_tx_audio_cap(body) == (400, {"ok": False, "error": "bad_seconds"})
    assert sent == []


def test_http_unknown_path_is_404(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/nope")
    assert e.value.code == 404


# ---- cross-site POSTs (review item 1A) ------------------------------------------


def post_gain(base, headers):
    """POST a gain change with exactly these extra headers; (status, json body)."""
    port = int(base.rsplit(":", 1)[1])
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", "/api/gain", body=b'{"rx": 40}',
                     headers={"Content-Type": "text/plain", "Host": f"127.0.0.1:{port}", **headers})
        r = conn.getresponse()
        return r.status, json.loads(r.read())
    finally:
        conn.close()


@pytest.mark.parametrize("headers", [
    {"Host": "vectorbridge", "Origin": "http://vectorbridge.local"},  # an Origin that is not this Host
    {"Host": "evil.example"},  # DNS rebinding: the page's own origin, but not our name
    {"Origin": "http://evil.example"},  # a cross-site form or fetch with a good Host
    {"Origin": "null"},  # sandboxed iframe or data: URL
    {"Origin": "http://203.0.113.5"},  # an attacker's page at a bare address
    {"Host": "vectorbridge.evil.example", "Origin": "http://vectorbridge.evil.example"},  # rebinding
    {"Sec-Fetch-Site": "cross-site"},
])
def test_http_post_from_another_site_is_refused_before_the_handler(server, headers):
    base, sent = server
    assert post_gain(base, headers) == (403, {"ok": False, "error": "forbidden_origin"})
    assert sent == []  # the control channel was never asked


@pytest.mark.parametrize("headers", [
    {},  # curl, the iPad client: no Origin, no Sec-Fetch-Site
    {"Host": "vectorbridge.local:8080", "Origin": "http://vectorbridge.local:8080", "Sec-Fetch-Site": "same-origin"},
    {"Origin": "http://127.0.0.1:8080"},  # port aside, the same host as the Host header
    {"Host": "vectorbridge", "Origin": "http://vectorbridge"},
    {"Host": "10.42.0.1", "Origin": "http://10.42.0.1"},
])
def test_http_post_from_our_own_pages_is_served(server, headers):
    base, sent = server
    status, _ = post_gain(base, headers)
    assert status == 200
    assert {"cmd": "set_gain", "rx": 40} in sent


def test_http_battery_answers_even_with_no_i2c_bus(server):
    base, _ = server
    status, body = get(base + "/api/battery")
    assert status == 200
    assert json.loads(body)["ok"] is True


@pytest.mark.parametrize("mode", ["field", "maintenance", None])
def test_mode_route_reports_the_root_filesystem_mode(mode):
    a = TunerApp(lambda req: None, mixer_fn=lambda card: [], mode_fn=lambda: mode)
    assert a.mode() == (200, {"ok": True, "mode": mode})


def test_readiness_says_when_the_config_file_cannot_be_read():
    a = TunerApp(lambda req: None, mixer_fn=lambda card: [], config_problem_fn=lambda: "Invalid value (at line 2)")
    assert a.readiness()[1]["config_problem"] == "Invalid value (at line 2)"
    assert TunerApp(lambda req: None, mixer_fn=lambda card: []).readiness()[1]["config_problem"] is None


def test_device_mode_is_none_where_findmnt_is_missing(monkeypatch):
    from vector_bridge import apsetup, tuner

    def missing():
        raise FileNotFoundError("findmnt")
    monkeypatch.setattr(apsetup, "overlay_on", missing)
    assert tuner.device_mode() is None


def test_http_mode_answers_and_the_home_page_polls_it(server):
    base, _ = server
    status, body = get(base + "/api/mode")
    assert status == 200 and json.loads(body)["ok"] is True
    _, home = get(base + "/")
    assert b'<span id="mode" hidden>' in home and b"/api/mode" in home



class _Rigctld:
    """rigctld that fails every call with `error` as the cause, as RigctldClient does."""

    def __init__(self, error):
        self.error, self.calls = error, []

    def _fail(self, call):
        self.calls.append(call)
        raise CatError("rigctld unreachable") from self.error

    def set_ptt(self, on):
        self._fail(on)

    def get_ptt(self):
        self._fail("get")


def test_on_vox_a_key_sends_nothing_and_only_a_missing_rigctld_is_forgiven():
    vox = [True]
    cat = _Rigctld(ConnectionRefusedError())
    set_ptt, get_ptt = vox_aware_ptt(cat, lambda: vox[0])
    set_ptt(True)
    assert cat.calls == []  # the transmit audio keys a VOX rig
    set_ptt(False)  # the unkey is still sent, for a rig keyed over CAT before the switch to VOX
    assert get_ptt() is False and cat.calls == [False, "get"]
    stuck = _Rigctld(TimeoutError())  # rigctld is there but not answering: the key may be stuck
    set_ptt, get_ptt = vox_aware_ptt(stuck, lambda: vox[0])
    with pytest.raises(CatError):
        set_ptt(False)  # raised, so the controller keeps it keyed and retries
    with pytest.raises(CatError):
        get_ptt()
    vox[0] = False
    with pytest.raises(CatError):
        vox_aware_ptt(cat, lambda: vox[0])[0](True)  # on CAT a missing rigctld is still an error


class FakeProxy:
    port = 4532

    def __init__(self, failed=()):
        self.listening, self.failed, self.binds = ["127.0.0.1", "10.42.0.1"], list(failed), []

    def rebind(self, binds):
        self.binds.append(binds)
        self.listening = [b for b in binds if b not in (f.split(":")[0] for f in self.failed)]
        return self.failed


def test_rigctl_access_is_saved_by_the_service_then_the_proxy_moves():
    a, sent = app({"ok": True, "rigctl_access": "all"})
    a.proxy, a.rigctl_access = FakeProxy(), "ap"
    assert a.rigctl() == (200, {"ok": True, "access": "ap", "port": 4532, "listening": ["127.0.0.1", "10.42.0.1"],
                                "vb_auth": True, "login_required": True})
    assert a.set_rigctl({"access": "all"}) == (200, {"ok": True, "access": "all", "port": 4532, "listening": ["0.0.0.0"],
                                                     "vb_auth": True, "login_required": True})
    assert sent == [{"cmd": "set_rigctl_access", "access": "all"}] and a.proxy.binds == [("0.0.0.0",)]


def test_rigctl_access_reports_an_address_that_could_not_listen():
    a, _ = app({"ok": True})
    a.proxy = FakeProxy(failed=["10.42.0.1: Can't assign requested address"])
    code, out = a.set_rigctl({"access": "ap"})
    assert code == 200 and out["listening"] == ["127.0.0.1"] and out["failed"] == ["10.42.0.1: Can't assign requested address"]


@pytest.mark.parametrize("reply,code,error", [(None, 503, "service_unreachable"), ({"ok": False, "error": "x"}, 400, "x")])
def test_rigctl_access_unchanged_when_it_cannot_be_saved(reply, code, error):
    a, _ = app(reply)
    a.proxy, a.rigctl_access = FakeProxy(), "ap"
    assert a.set_rigctl({"access": "off"})[0] == code
    assert a.proxy.binds == [] and a.rigctl_access == "ap"


@pytest.mark.parametrize("body", [{}, {"access": "everywhere"}, {"access": 1}])
def test_rigctl_access_rejects_unknown_settings(body):
    a, sent = app({"ok": True})
    a.proxy = FakeProxy()
    assert a.set_rigctl(body) == (400, {"ok": False, "error": "bad_access"}) and sent == []


def test_rigctl_access_without_a_proxy_is_unavailable():
    a, sent = app({"ok": True})
    assert a.rigctl()[0] == 503 and a.set_rigctl({"access": "ap"})[0] == 503 and sent == []


def test_http_rigctl_routes(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/api/rigctl", timeout=5)
    assert e.value.code == 503  # this test server has no proxy
    req = urllib.request.Request(base + "/api/rigctl", data=b'{"access": "bogus"}',
                                 headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 400


def test_saving_the_transmit_limit_waits_for_the_card_write():
    # On the Pi the save took 0.29 s (two remounts); a 0.25 s wait reported a saved limit as not saved.
    assert control_timeout({"cmd": "set_tx_audio_cap", "seconds": 5}) == SAVE_TIMEOUT_S >= 1.0
    assert control_timeout({"cmd": "set_gain", "rx": 76}) == SAVE_TIMEOUT_S  # gains are saved the same way
    assert control_timeout({"cmd": "select_audio"}) == SAVE_TIMEOUT_S  # the chosen card is saved too
    assert control_timeout({"cmd": "set_rigctl_access", "access": "ap"}) == SAVE_TIMEOUT_S
    assert control_timeout({"cmd": "status"}) == 0.25  # polls stay quick


class FakeLadder:
    def __init__(self):
        self.ptt = SimpleNamespace(keyed=False, lock=SimpleNamespace(owner=None))
        self.cleared = 0

    def clear_record(self):
        self.cleared += 1
        return True


def reset_app(tmp_path, monkeypatch, reply):
    from vector_bridge import alerts, persist, wizard
    a, sent = app(reply)
    a.wizard = wizard.Wizard(wizard.Probes(control_fn=lambda req: None), state_path=tmp_path / "wizard.json",
                             kits_dir=tmp_path / "kits", presets_dir=tmp_path / "presets")
    a.wizard.state.profile = "kx2"
    a.alert_config = alerts.AlertConfig(path=tmp_path / "alerts.json")
    a.alert_config.update({"ptt": {"beep": False}})
    a.ladder, a.proxy, a.rigctl_access = FakeLadder(), FakeProxy(), "all"
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "fault-0001.txt").write_text("old fault")
    monkeypatch.setattr(persist, "LOGS_DIR", logs)
    return a, sent


@pytest.mark.parametrize("body,error", [({"scope": "radio"}, "Type RESET to confirm."),
                                        ({"scope": "radio", "confirm": "reset"}, "Type RESET to confirm."),
                                        ({"scope": "wifi", "confirm": "RESET"}, "bad_scope")])
def test_reset_needs_a_scope_and_the_typed_word(tmp_path, monkeypatch, body, error):
    a, sent = reset_app(tmp_path, monkeypatch, {"ok": True})
    assert a.reset(body) == (400, {"ok": False, "error": error})
    assert a.wizard.state.profile == "kx2" and sent == [] and a.ladder.cleared == 0


@pytest.mark.parametrize("keyed,owner", [(True, None), (False, "wsjtx")])
def test_reset_is_refused_while_the_radio_transmits(tmp_path, monkeypatch, keyed, owner):
    a, sent = reset_app(tmp_path, monkeypatch, {"ok": True})
    a.ladder.ptt.keyed, a.ladder.ptt.lock.owner = keyed, owner
    code, out = a.reset({"scope": "all", "confirm": "RESET"})
    assert code == 409 and "transmitting" in out["error"]
    assert a.wizard.state.profile == "kx2" and sent == []


def test_reset_radio_setup_leaves_the_settings(tmp_path, monkeypatch):
    a, sent = reset_app(tmp_path, monkeypatch, {"ok": True})
    code, out = a.reset({"scope": "radio", "confirm": "RESET"})
    assert code == 200 and out["ok"] and out["wizard"]["profile"] is None
    assert a.ladder.cleared == 1 and sent == [] and a.proxy.binds == []
    assert (tmp_path / "logs" / "fault-0001.txt").exists() and (tmp_path / "alerts.json").exists()


def test_reset_everything_puts_the_settings_back_but_not_wifi(tmp_path, monkeypatch):
    a, sent = reset_app(tmp_path, monkeypatch, {"ok": True})
    code, out = a.reset({"scope": "all", "confirm": "RESET"})
    assert code == 200 and out == out | {"ok": True, "problems": []}
    assert sent == [{"cmd": "set_gain", "tx": 20, "rx": 50}, {"cmd": "set_tx_audio_cap", "seconds": 120},
                    {"cmd": "select_audio", "vendor": "", "product": "", "usb_path": ""},
                    {"cmd": "set_rigctl_access", "access": "wifi"}]
    assert a.rigctl_access == "wifi" and a.proxy.binds
    assert a.alert_config.wants("ptt", "beep") and not (tmp_path / "alerts.json").exists()
    assert not list((tmp_path / "logs").iterdir())


def test_reset_everything_names_what_the_service_did_not_take(tmp_path, monkeypatch):
    a, _ = reset_app(tmp_path, monkeypatch, None)  # the audio service is not answering
    code, out = a.reset({"scope": "all", "confirm": "RESET"})
    assert code == 200 and not out["ok"] and out["wizard"]["profile"] is None
    assert any("audio levels" in p for p in out["problems"]) and any("rig control" in p for p in out["problems"])


def test_http_reset_route(server):
    base, _ = server
    req = urllib.request.Request(base + "/api/reset", data=b'{"scope": "radio"}',
                                 headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 400


def test_the_kit_in_use_is_not_renamed_or_deleted_while_transmitting(tmp_path, monkeypatch):
    a, _ = reset_app(tmp_path, monkeypatch, {"ok": True})
    a.wizard.state.kit = "Field One"
    a.ladder.ptt.keyed = True
    for body in ({"action": "rename_kit", "name": "Field One", "new_name": "Park"},
                 {"action": "delete_kit", "name": "Field One"}):
        code, out = a.wizard_action(body)
        assert code == 409 and "transmitting" in out["error"]
    code, out = a.wizard_action({"action": "delete_kit", "name": "Another"})  # not the kit in use: the wizard answers
    assert code == 400 and "no saved kit" in out["error"]


def test_http_hamlib_conf_needs_a_model_number(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/api/hamlib/conf?model=abc", timeout=5)
    assert e.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/api/hamlib/conf?model=%C2%B2", timeout=5)  # a digit, but not 0-9
    assert e.value.code == 400


def test_transmit_time_limit_sheet_updates_the_rigctl_key_cap_without_a_restart():
    a, sent = app({"ok": True, "tx_audio_cap_s": 240})
    a.proxy = SimpleNamespace(max_key_s=120.0)
    assert a.set_tx_audio_cap({"seconds": 240}) == (200, {"ok": True, "tx_audio_cap_s": 240})
    assert a.proxy.max_key_s == 240.0
    a2, _ = app(None)  # audio service unreachable: the key cap is left as it was
    a2.proxy = SimpleNamespace(max_key_s=120.0)
    assert a2.set_tx_audio_cap({"seconds": 10})[0] == 503
    assert a2.proxy.max_key_s == 120.0
