"""Area 6: per-event alert config, the LED state machine, and the beeper behind a small interface."""

import json
import threading
import time
import urllib.request

import pytest

from vector_bridge import alerts, readiness
from vector_bridge.tuner import TunerApp, serve


class FakeOut:
    def __init__(self):
        self.writes = []

    def set(self, on):
        self.writes.append(on)


def config(tmp_path, **kw):
    return alerts.AlertConfig(tmp_path / "alerts.json", save=lambda p, d: p.write_text(d), **kw)


def report(client="fail", fault=(), seq=0, alert=()):
    return {"checks": [{"key": "client", "state": client}], "fault": list(fault), "alert_seq": seq,
            "alert": [{"key": k} for k in alert]}


def test_every_event_defaults_to_every_channel(tmp_path):
    cfg = config(tmp_path)
    assert set(cfg.events) == set(alerts.EVENTS)
    assert all(cfg.wants(e, ch) for e in alerts.EVENTS for ch in alerts.CHANNELS)


def test_per_event_setting_survives_a_restart(tmp_path):
    config(tmp_path).update({"client": {"beep": False}})
    again = config(tmp_path)
    assert not again.wants("client", "beep")
    assert again.wants("client", "sound") and again.wants("cat", "beep")


@pytest.mark.parametrize("junk", [None, [], "x", {"nope": {"beep": False}}, {"cat": {"beep": "no", "siren": True}},
                                  {"cat": 7}])
def test_hostile_config_is_ignored(tmp_path, junk):
    cfg = config(tmp_path)
    cfg.update(junk)
    assert cfg.events == alerts.default_config()


def test_a_config_that_cannot_be_saved_says_so_but_still_applies(tmp_path):
    def refuse(path, data):
        raise OSError("read-only")
    cfg = alerts.AlertConfig(tmp_path / "alerts.json", save=refuse)
    cfg.update({"cat": {"sound": False}})
    assert not cfg.wants("cat", "sound")
    assert "could not be saved" in cfg.report()["save_error"]


@pytest.mark.parametrize("rep,ap,want", [
    (None, True, alerts.BOOTING),
    ({"checks": []}, True, alerts.BOOTING),
    (report(), False, alerts.BOOTING),
    (report(), True, alerts.AP_UP),
    (report(client="ok"), True, alerts.CLIENT),
    (report(client="ok", fault=["cat"]), True, alerts.FAULT),
])
def test_led_state(rep, ap, want):
    assert alerts.led_state(rep, ap) == want


def test_each_led_state_has_its_own_pattern():
    assert set(alerts.PATTERNS) == {alerts.BOOTING, alerts.AP_UP, alerts.CLIENT, alerts.FAULT}
    assert len({json.dumps(p) for p in alerts.PATTERNS.values()}) == 4


def test_beeps_only_for_a_new_alert_on_an_event_that_wants_it(tmp_path):
    cfg = config(tmp_path)
    cfg.update({"client": {"beep": False}})
    reports = [report(seq=3, alert=["cat"]),  # already in the past when the alerter started: silent
               report(seq=4, alert=["client"]),  # beep switched off for this event
               report(seq=5, alert=["cat"]),
               report(seq=5, alert=["cat"])]  # same alert seen again: no second beep
    a = alerts.Alerter(lambda: reports.pop(0), cfg, led=FakeOut(), beeper=FakeOut(), ap_up_fn=lambda: True)
    shown = []
    a.beeper.show = lambda pattern, repeat: shown.append(pattern)
    for want in (0, 0, 1, 1):
        a.step()
        assert len(shown) == want


def test_a_failing_report_shows_booting_not_a_crash(tmp_path):
    def boom():
        raise RuntimeError("monitor gone")
    a = alerts.Alerter(boom, config(tmp_path), led=FakeOut(), beeper=FakeOut())
    a.step()
    assert a.state == alerts.BOOTING


def test_player_drives_the_output_and_ends_a_beep_off():
    out = FakeOut()
    p = alerts.Player(out, "t")
    p.start()
    p.show([(True, 0.01), (False, 0.01), (True, 0.01)], repeat=False)
    deadline = time.monotonic() + 2
    while out.writes[-4:] != [True, False, True, False] and time.monotonic() < deadline:
        time.sleep(0.01)
    played = out.writes[-4:]
    p.stop()
    assert played == [True, False, True, False]  # the idle player writes one off first


def test_no_hardware_configured_is_a_no_op_and_a_bad_path_does_not_raise(tmp_path):
    assert isinstance(alerts.output_for(""), alerts.NullOutput)
    alerts.output_for(str(tmp_path / "missing" / "brightness")).set(True)
    led = tmp_path / "brightness"
    led.write_text("0")
    alerts.output_for(str(led)).set(True)
    assert led.read_text() == "1"


def test_monitor_reports_a_fault_until_the_regressed_check_recovers():
    state = {"cat": True}
    m = readiness.Monitor(lambda history: readiness.Inputs(cat_alive=state["cat"]))
    m.poll()
    assert m.report()["fault"] == []  # red from the start is not a fault: nothing regressed
    state["cat"] = False
    m.poll()
    assert m.report()["fault"] == ["cat"]
    state["cat"] = True
    m.poll()
    assert m.report()["fault"] == []


def test_http_alert_settings_and_web_app_files(tmp_path):
    app = TunerApp(lambda req: None, alert_config=config(tmp_path))
    httpd = serve(app, bind="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        def get(path):
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.headers.get("Content-Type"), r.read()
        req = urllib.request.Request(base + "/api/alerts", data=json.dumps({"events": {"ptt": {"vibrate": False}}}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read())["events"]["ptt"]["vibrate"] is False
        assert json.loads(get("/api/alerts")[1])["events"]["ptt"]["vibrate"] is False
        ctype, png = get("/icon.png")
        assert ctype == "image/png" and png.startswith(b"\x89PNG")
        assert json.loads(get("/manifest.json")[1])["display"] == "standalone"
        home = get("/")[1].decode()
        assert "apple-mobile-web-app-capable" in home and "notify(" in home and "armSound" in home
        assert "Bridge beep" in get("/alerts")[1].decode()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_stop_always_leaves_the_output_off():
    out = FakeOut()
    p = alerts.Player(out, "t")
    p.start()
    p.show([(True, 5.0)], repeat=True)  # steady on, as for a connected client
    deadline = time.monotonic() + 2
    while out.writes[-1:] != [True] and time.monotonic() < deadline:
        time.sleep(0.01)
    p.stop()
    assert out.writes[-1] is False
    assert not p._thread.is_alive()
