"""Readiness check: ordered checks, plain-language fixes, one verdict, quiet re-runs."""

import json
import re
import threading
import urllib.request
from collections import deque

import pytest

from vector_bridge import readiness as rd
from vector_bridge.tuner import TunerApp, serve


def statuses(n=4, peer="10.42.0.7:5000", rms=(900, 1400, 700, 1100)):
    return [{"ok": True, "rx_rms": rms[k % len(rms)], "rx_packets_sent": 100 * (k + 1), "peer": peer}
            for k in range(n)]


def good(**over):
    base = {"statuses": statuses(), "service_up": True, "cat_alive": True, "ptt_state": False,
            "ptt_proven": True, "gates": {"passed": True, "bypassed": []}}
    base.update(over)
    return rd.Inputs(**base)


def test_checks_run_in_the_specified_order_and_all_green_is_ready():
    checks = rd.run_checks(good())
    assert [c.key for c in checks] == ["cat", "rx_audio", "ptt", "gates", "client"]
    assert rd.verdict(checks) == rd.READY


def test_every_non_green_result_carries_a_plain_language_fix():
    bad = rd.Inputs()  # nothing works
    frozen = good(statuses=statuses(rms=(900,)), ptt_proven=False, gates=None)
    for c in rd.run_checks(bad) + rd.run_checks(frozen):
        assert c.state == rd.OK or len(c.fix) > 20, c
    assert "not the microphone jack" in rd.check_cat(bad).fix


def test_verdicts_not_ready_caveats_and_bypass_reports_unverified():
    assert rd.verdict(rd.run_checks(good(cat_alive=False))) == rd.NOT_READY
    assert rd.verdict(rd.run_checks(good(gates=None))) == rd.CAVEATS
    bypass = rd.check_gates(good(gates={"passed": False, "bypassed": ["audio drive", "SWR"]}))
    assert bypass.state == rd.WARN and "unverified" in bypass.detail and "SWR" in bypass.detail


def test_rx_audio_needs_a_present_and_moving_level():
    assert rd.check_rx_audio(good(statuses=statuses(rms=(0,)))).state == rd.FAIL
    assert rd.check_rx_audio(good(statuses=statuses(rms=(900,)))).state == rd.WARN
    assert rd.check_rx_audio(good()).state == rd.OK


def test_client_needs_a_peer_and_packets_actually_flowing():
    assert rd.check_client(good(statuses=statuses(peer=None))).state == rd.FAIL
    stalled = [dict(s, rx_packets_sent=500) for s in statuses()]
    assert rd.check_client(good(statuses=stalled)).state == rd.FAIL


class SpyCat:
    def __init__(self):
        self.calls = []

    def alive(self):
        self.calls.append("alive")
        return True

    def get_ptt(self):
        self.calls.append("get_ptt")
        return False

    def set_ptt(self, on):
        self.calls.append(f"set_ptt({on})")


def test_gathering_never_keys_the_radio():
    cat = SpyCat()
    mon = rd.Monitor(rd.make_gather(lambda req: statuses()[0], cat=cat))
    for _ in range(5):
        mon.poll()
    assert cat.calls and not [c for c in cat.calls if c.startswith("set_ptt")]
    assert rd.check_ptt(good(ptt_state=True, tx_owner=None)).state == rd.FAIL  # keyed by something else


def test_a_vox_kit_asks_rigctld_nothing_and_shows_cat_unused_and_keying_unreadable():
    cat = SpyCat()
    gather = rd.make_gather(lambda req: statuses()[0], cat=cat, vox_fn=lambda: True)
    checks = {c.key: c for c in rd.run_checks(gather(deque()))}
    assert cat.calls == []
    assert checks["cat"].state == rd.OK and "VOX" in checks["cat"].detail
    assert checks["ptt"].state == rd.WARN and checks["ptt"].fix
    assert not re.search(r"\btransmitting\b", checks["ptt"].detail, re.IGNORECASE)  # the home page's keyedNow() test
    keyed = rd.check_ptt(good(vox=True, cat_alive=False, ptt_state=None, tx_owner="client"))
    assert re.search(r"\btransmitting\b", keyed.detail, re.IGNORECASE)  # the bridge keying a VOX rig still pauses receive


def test_monitor_alerts_only_when_green_turns_red():
    state = {"i": good(gates=None)}  # gates amber from the start
    mon = rd.Monitor(lambda history: state["i"])
    mon.poll()
    assert mon.report()["alert_seq"] == 0
    state["i"] = good(gates=None, ptt_proven=False)  # green to amber: quiet
    mon.poll()
    assert mon.report()["alert_seq"] == 0
    state["i"] = good(gates=None, cat_alive=False, ptt_state=None)  # cat green to red: interrupt
    mon.poll()
    r = mon.report()
    assert r["alert_seq"] == 1 and "cat" in [c["key"] for c in r["alert"]]
    mon.poll()  # still red: no repeat
    assert mon.report()["alert_seq"] == 1


def test_green_then_amber_then_red_still_interrupts():
    state = {"i": good()}
    mon = rd.Monitor(lambda history: state["i"])
    mon.poll()
    state["i"] = good(ptt_proven=False)  # ptt amber
    mon.poll()
    state["i"] = good(ptt_state=None)  # ptt red
    mon.poll()
    r = mon.report()
    assert r["alert_seq"] == 1 and [c["key"] for c in r["alert"]] == ["ptt"]


def test_a_dropped_status_request_does_not_turn_red_into_green():
    replies = [dict(s, rx_rms=900, rx_packets_sent=500) for s in statuses(6)] + [None]
    it = iter(replies)
    mon = rd.Monitor(rd.make_gather(lambda req: next(it), cat=SpyCat()))
    for _ in replies:
        mon.poll()
    assert {c["key"]: c["state"] for c in mon.report()["checks"]}["rx_audio"] == rd.FAIL  # service down
    mon2 = rd.Monitor(rd.make_gather(lambda req: dict(statuses()[0], rx_packets_sent=500), cat=SpyCat()))
    for _ in range(4):
        mon2.poll()
    states = {c["key"]: c["state"] for c in mon2.report()["checks"]}
    assert states["rx_audio"] == rd.WARN and states["client"] == rd.FAIL  # frozen level, stalled stream


def test_too_few_samples_is_amber_not_green_and_keyed_by_us_is_truthful():
    one = good(statuses=statuses(1))
    assert rd.check_rx_audio(one).state == rd.WARN and rd.check_client(one).state == rd.WARN
    assert "Transmitting now for ft8" in rd.check_ptt(good(ptt_state=True, tx_owner="ft8")).detail


@pytest.fixture
def server():
    httpd = serve(TunerApp(lambda req: statuses()[0], mixer_fn=lambda card: []), bind="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_home_screen_is_the_readiness_check_and_tuner_moved(server):
    home = urllib.request.urlopen(server + "/", timeout=5).read()
    assert b"Could I pass traffic right now?" in home and b"/api/readiness" in home
    assert b"rxgain" not in home
    assert b"rxgain" in urllib.request.urlopen(server + "/tuner", timeout=5).read()
    r = json.loads(urllib.request.urlopen(server + "/api/readiness", timeout=5).read())
    assert r["verdict"] == rd.NOT_READY and len(r["checks"]) == 5  # no rigctld in the test


def test_faults_are_ordered_most_urgent_first():
    state = {"i": good()}
    mon = rd.Monitor(lambda history: state["i"])
    mon.poll()
    state["i"] = good(statuses=statuses(rms=(0,), peer=None), cat_alive=False, ptt_state=True)
    mon.poll()
    r = mon.report()
    assert r["fault"] == ["ptt", "cat", "rx_audio", "client"]
    assert [c["key"] for c in r["alert"]] == r["fault"] and mon.state_of("rx_audio") == rd.FAIL
    state["i"] = good()
    mon.poll()
    state["i"] = good(cat_alive=False, ptt_state=None)  # lost CAT also makes PTT unreadable: not an unexpected transmit
    mon.poll()
    assert mon.report()["fault"] == ["cat", "ptt"]


def test_a_ptt_read_back_just_after_the_bridge_unkeyed_is_not_an_unexpected_transmit():
    """The read races the ladder's unkey, and rigctld lags it by about 0.8 s (bench, IC-705)."""
    c = rd.check_ptt(good(ptt_state=True, tx_owner=None, tx_just_released=True))
    assert c.state == rd.OK and c.detail != rd.UNEXPECTED_TX
    assert rd.check_ptt(good(ptt_state=True, tx_owner=None, tx_just_released=False)).detail == rd.UNEXPECTED_TX


def test_a_kit_keyed_on_a_serial_line_shows_radio_control_as_not_used():
    ok = rd.check_cat(good(no_cat=True))
    assert ok.state == rd.OK and "Not used" in ok.detail
    down = rd.check_cat(good(no_cat=True, cat_alive=False))
    assert down.state == rd.FAIL and "serial line" in down.detail


def test_gather_marks_no_cat_only_when_the_kit_is_not_vox():
    class Cat:
        def alive(self):
            return True

        def get_ptt(self):
            return False

    gather = rd.make_gather(lambda req: {"rx_rms": 900}, cat=Cat(), no_cat_fn=lambda: True)
    assert gather(deque()).no_cat is True
    gather = rd.make_gather(lambda req: {"rx_rms": 900}, cat=Cat(), vox_fn=lambda: True, no_cat_fn=lambda: True)
    assert gather(deque()).no_cat is False
