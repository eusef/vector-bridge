"""Control channel: hello, status, set_gain, tone, unknown cmd, the loopback-only settings commands."""

import json

from vector_bridge.control import LOCAL_ONLY_CMDS, ControlHandler

LOCAL = ("127.0.0.1", 40000)  # the tuner and the CLI on the Pi


def make(registered, gains):
    def set_gain(tx, rx):
        if tx is not None:
            gains["tx"] = tx
        if rx is not None:
            gains["rx"] = rx
        return dict(gains)

    return ControlHandler("TestBridge", 5100, lambda: {"uptime_s": 1, "rx_packets_sent": 7},
                          set_gain, lambda addr, client: registered.append((addr, client)))


def test_hello_registers_peer_and_describes_stream():
    registered, gains = [], {"tx": 20, "rx": 50}
    h = make(registered, gains)
    reply = json.loads(h.handle_bytes(b'{"cmd":"hello","client":"mac"}', ("10.0.0.2", 4000)))
    assert reply == {"ok": True, "device": "TestBridge", "protocol": 1, "sample_rate": 48000,
                     "channels": 1, "audio_port": 5100}
    assert registered == [(("10.0.0.2", 4000), "mac")]


def test_status_returns_telemetry():
    h = make([], {"tx": 20, "rx": 50})
    reply = h.handle({"cmd": "status"}, ("10.0.0.2", 4000))
    assert reply["ok"] is True and reply["rx_packets_sent"] == 7 and reply["uptime_s"] == 1


def test_set_gain_applies_and_validates():
    gains = {"tx": 20, "rx": 50}
    h = make([], gains)
    assert h.handle({"cmd": "set_gain", "tx": 35}, LOCAL) == {"ok": True, "tx": 35, "rx": 50}
    assert h.handle({"cmd": "set_gain", "rx": 60}, LOCAL) == {"ok": True, "tx": 35, "rx": 60}
    assert h.handle({"cmd": "set_gain", "tx": 101}, LOCAL) == {"ok": False, "error": "bad_gain"}
    assert h.handle({"cmd": "set_gain"}, LOCAL) == {"ok": False, "error": "bad_request"}


def test_unknown_and_malformed():
    h = make([], {"tx": 20, "rx": 50})
    assert h.handle({"cmd": "ptt"}, LOCAL) == {"ok": False, "error": "unknown_cmd"}
    assert json.loads(h.handle_bytes(b"not json", LOCAL)) == {"ok": False, "error": "bad_request"}


def test_set_gain_replies_when_the_mixer_fails():
    def broken(tx, rx):
        raise OSError("mixer gone")

    h = ControlHandler("TestBridge", 5100, dict, broken, lambda addr, client: None)
    assert h.handle({"cmd": "set_gain", "tx": 30}, LOCAL) == {"ok": False, "error": "gain_failed"}


def test_tone_starts_stops_and_validates():
    calls = []

    def play(hz, seconds):
        calls.append((hz, seconds))
        return {"tone_hz": hz, "tone_s": seconds}

    h = ControlHandler("TestBridge", 5100, dict, lambda tx, rx: {}, lambda addr, client: None, play)
    assert h.handle({"cmd": "tone", "hz": 1500, "seconds": 30}, LOCAL) == {"ok": True, "tone_hz": 1500.0, "tone_s": 30.0}
    assert h.handle({"cmd": "tone", "seconds": 0}, LOCAL)["ok"]
    assert calls == [(1500.0, 30.0), (1500.0, 0.0)]
    for bad in ({"hz": 1500}, {"hz": "x", "seconds": 1}, {"seconds": True}):
        assert h.handle({"cmd": "tone", **bad}, LOCAL) == {"ok": False, "error": "bad_request"}
    for bad in ({"hz": 50, "seconds": 1}, {"hz": 1500, "seconds": 31}, {"seconds": -1}):
        assert h.handle({"cmd": "tone", **bad}, LOCAL) == {"ok": False, "error": "bad_tone"}
    assert len(calls) == 2


def test_tone_is_unknown_without_an_audio_path():
    h = ControlHandler("TestBridge", 5100, dict, lambda tx, rx: {}, lambda addr, client: None)
    assert h.handle({"cmd": "tone", "seconds": 1}, LOCAL) == {"ok": False, "error": "unknown_cmd"}


def test_set_tx_audio_cap_applies_and_validates():
    applied = []
    h = ControlHandler("TestBridge", 5100, dict, lambda tx, rx: {}, lambda a, c: None,
                       set_tx_audio_cap_fn=lambda s: applied.append(s) or {"tx_audio_cap_s": s})
    assert h.handle({"cmd": "set_tx_audio_cap", "seconds": 120}, LOCAL) == {"ok": True, "tx_audio_cap_s": 120}
    for bad in (601, -1, 1.5, True, "30", None):
        assert h.handle({"cmd": "set_tx_audio_cap", "seconds": bad}, LOCAL) == {"ok": False, "error": "bad_tx_audio_cap"}
    assert applied == [120]


def test_monitor_registers_listen_only_and_reports_refusals():
    calls, answer = [], {"err": None}
    h = ControlHandler("TestBridge", 5100, dict, lambda tx, rx: {}, lambda a, c: None,
                       monitor_fn=lambda a, c: calls.append((a, c)) or answer["err"])
    assert h.handle({"cmd": "monitor", "client": "js8"}, ("10.0.0.3", 4001)) == {
        "ok": True, "device": "TestBridge", "protocol": 1, "sample_rate": 48000,
        "channels": 1, "audio_port": 5100, "role": "monitor"}
    answer["err"] = "monitors_full"
    assert h.handle({"cmd": "monitor"}, ("10.0.0.4", 4001)) == {"ok": False, "error": "monitors_full"}
    assert calls == [(("10.0.0.3", 4001), "js8"), (("10.0.0.4", 4001), "")]
    no_fn = ControlHandler("TestBridge", 5100, dict, lambda tx, rx: {}, lambda a, c: None)
    assert no_fn.handle({"cmd": "monitor"}, LOCAL) == {"ok": False, "error": "unknown_cmd"}


def test_select_audio_passes_ids_and_position_and_refuses_bad_ones():
    chosen = []
    h = ControlHandler("TestBridge", 5100, dict, lambda tx, rx: {}, lambda a, c: None,
                       select_audio_fn=lambda v, p, u, s: chosen.append((v, p, u, s)) or {"changed": True})
    req = {"cmd": "select_audio", "vendor": "0D8C", "product": "0014", "usb_path": "1-1.4.2", "strict": True}
    assert h.handle(req, LOCAL) == {"ok": True, "changed": True}
    assert h.handle({"cmd": "select_audio"}, LOCAL) == {"ok": True, "changed": True}  # all empty: automatic
    assert h.handle({"cmd": "select_audio", "strict": True}, LOCAL)["ok"]  # strict needs a position
    for bad in ({"vendor": "0d8"}, {"product": "zzzz"}, {"usb_path": "1-1/../x"}, {"vendor": 3468},
                {"usb_path": "usb1"}, {"strict": "yes"}):
        assert h.handle({"cmd": "select_audio", **bad}, LOCAL) == {"ok": False, "error": "bad_audio_card"}
    assert chosen == [("0d8c", "0014", "1-1.4.2", True), ("", "", "", False), ("", "", "", False)]
    no_fn = ControlHandler("TestBridge", 5100, dict, lambda tx, rx: {}, lambda a, c: None)
    assert no_fn.handle(req, LOCAL) == {"ok": False, "error": "unknown_cmd"}


def test_set_rigctl_access_passes_a_known_setting_and_refuses_others():
    chosen = []
    h = ControlHandler("TestBridge", 5100, dict, lambda tx, rx: {}, lambda a, c: None,
                       set_rigctl_access_fn=lambda a: chosen.append(a) or {"rigctl_access": a})
    assert h.handle({"cmd": "set_rigctl_access", "access": "all"}, LOCAL) == {"ok": True, "rigctl_access": "all"}
    for bad in ({}, {"access": "AP"}, {"access": True}, {"access": "everywhere"}):
        assert h.handle({"cmd": "set_rigctl_access", **bad}, LOCAL) == {"ok": False, "error": "bad_rigctl_access"}
    assert chosen == ["all"]


def test_settings_commands_are_refused_from_off_the_pi():
    applied = []
    h = ControlHandler("TestBridge", 5100, dict, lambda tx, rx: applied.append("gain") or {}, lambda a, c: None,
                       set_tx_audio_cap_fn=lambda s: applied.append("cap") or {},
                       select_audio_fn=lambda v, p, u, s: applied.append("card") or {},
                       set_rigctl_access_fn=lambda a: applied.append("rigctl") or {})
    reqs = [{"cmd": "set_gain", "tx": 30}, {"cmd": "set_tx_audio_cap", "seconds": 0},
            {"cmd": "select_audio"}, {"cmd": "set_rigctl_access", "access": "all"}]
    assert {r["cmd"] for r in reqs} == LOCAL_ONLY_CMDS
    for addr in (("10.42.0.5", 5000), ("192.0.2.20", 5000), ("100.96.1.2", 5000), ("a", 1)):
        for req in reqs:
            assert h.handle(req, addr) == {"ok": False, "error": "pin_required"}
    assert h.handle({"cmd": "set_gain", "tx": 500}, ("10.42.0.5", 5000))["error"] == "pin_required"  # before validation
    assert applied == []
    assert h.handle({"cmd": "status"}, ("10.42.0.5", 5000))["ok"]  # reading stays open
    for req in reqs:
        assert h.handle(req, ("127.0.0.1", 40000))["ok"]
    assert applied == ["gain", "cap", "card", "rigctl"]
