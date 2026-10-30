"""Transmit safety ladder: gate one is unskippable, overrides are recorded, the watchdog never yields."""

import json

import pytest

from vector_bridge import ladder as L
from vector_bridge import persist, profiles, readiness, tx
from vector_bridge.cat import CatError
from vector_bridge.tx import PttController


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeRig:
    def __init__(self, alc=(0.05,), swr=(1.2,), levels=None, freq=14_074_000, mode="PKTUSB"):  # ALC just below onset
        self.keyed, self.key_log = False, []
        self.freq, self.mode = freq, mode
        self.alc, self.swr = list(alc), list(swr)
        self.levels = {"RFPOWER": 0.8, **(levels or {})}

    def set_ptt(self, on):
        self.keyed = on
        self.key_log.append(on)

    def get_level(self, name):
        if name in ("ALC", "SWR"):
            assert self.keyed, f"{name} read while not transmitting"
            seq = self.alc if name == "ALC" else self.swr
            return seq.pop(0) if len(seq) > 1 else seq[0]
        if name not in self.levels:
            raise CatError(f"no {name}")
        return self.levels[name]

    def set_level(self, name, value):
        self.levels[name] = value

    def get_freq(self):
        return self.freq

    def set_freq(self, hz):
        self.freq = hz

    def get_mode(self):
        return self.mode

    def set_mode(self, mode):
        self.mode = mode


class Service:
    def __init__(self, tx=60, tone_ok=True, queued_ms=0):
        self.tx, self.tone_ok, self.tone_on, self.tones = tx, tone_ok, False, []
        self.queued_ms = queued_ms  # TX audio still in the jitter buffer and the sound card

    def __call__(self, req):
        if req["cmd"] == "set_gain":
            self.tx = req["tx"]
        if req["cmd"] == "tone":
            self.tones.append(req["seconds"])
            if not self.tone_ok:
                return None  # the audio service did not answer
            self.tone_on = req["seconds"] > 0
            return {"ok": True}
        return {"tx_gain": self.tx, "tx_queued_ms": self.queued_ms}


def make(tmp_path, rig, profile="ic-705", svc=None, ident=None, **hooks):  # the bench rig that reports ALC and SWR
    clock = Clock()
    ptt = PttController(rig.set_ptt, watchdog_s=3.0, clock=clock)
    ident = ident if ident is not None else [profile, "internal-usb", ""]
    lad = L.Ladder(rig, ptt, svc or Service(), lambda: profiles.get(ident[0]), identity_fn=lambda: tuple(ident),
                   record_path=tmp_path / "overrides.json", sleep=clock.sleep, clock=clock, **hooks)
    return lad, ptt, clock


@pytest.fixture(autouse=True)
def _no_remount(monkeypatch):
    monkeypatch.setattr(persist, "_system_remount", lambda *a, **k: None, raising=False)


def test_gate_one_cannot_be_skipped(tmp_path):
    rig = FakeRig()
    lad, _, _ = make(tmp_path, rig)
    for attempt in (lad.run_drive, lad.run_carrier, lambda: lad.override(["drive", "carrier"])):
        with pytest.raises(L.LadderError, match="cannot be skipped"):
            attempt()
    with pytest.raises(L.LadderError, match="never be bypassed"):
        lad.override(["load", "drive", "carrier"])
    lad.confirm_load("antenna")
    with pytest.raises(L.LadderError, match="never be bypassed"):
        lad.override(["load"])
    assert not lad.tx_allowed() and not lad.key("client") and rig.key_log == []


def test_a_dummy_load_is_offered_but_an_antenna_is_accepted(tmp_path):
    lad, _, _ = make(tmp_path, FakeRig())
    lad.confirm_load("antenna")
    assert lad.report()["gates"][0]["state"] == "passed"
    with pytest.raises(L.LadderError):
        lad.confirm_load("nothing")


def test_full_ladder_unlocks_transmit_and_always_unkeys(tmp_path):
    rig = FakeRig(alc=(1.0, 1.0, 0.08), swr=(1.3,))
    svc = Service(tx=60)
    lad, ptt, _ = make(tmp_path, rig, svc=svc)
    lad.confirm_load("dummy")
    # The IC-705 search starts at the profile's 70, hears drive at once, then lowers twice to the ALC target
    assert lad.run_drive()["result"]["ok"] and svc.tx == 60
    assert lad.run_carrier()["result"]["ok"]
    assert not rig.keyed and ptt.lock.owner is None
    assert rig.levels["RFPOWER"] == 0.8  # power restored after the carrier
    hook = lad.hook()
    assert hook["gates"] == {"passed": True, "drive_passed": True, "bypassed": [], "caveats": [], "tx_allowed": True}
    assert hook["ptt_proven"]
    assert lad.tx_allowed() and lad.key("client") and rig.keyed


def test_no_tone_fails_the_drive_gate(tmp_path):
    lad, _, _ = make(tmp_path, FakeRig(alc=(0.0,)))
    lad.confirm_load("dummy")
    r = lad.run_drive()["result"]
    assert not r["ok"] and "No drive is reaching" in r["detail"] and "data mode" in r["detail"]
    assert "ham band" in r["detail"]  # an off-band radio keys with no output (IC-705 on 10 MHz WWV)


def test_drive_and_carrier_key_with_the_bridge_tone_and_stop_it(tmp_path):
    """No client app needs to be connected: the bridge plays its own tone under the key (HANDOFF D8)."""
    svc, heard = Service(), []
    rig = FakeRig(alc=(1.0, 0.08))
    read = rig.get_level
    rig.get_level = lambda name: (heard.append((name, svc.tone_on)), read(name))[1]
    lad, _, _ = make(tmp_path, rig, svc=svc)
    lad.confirm_load("dummy")
    assert lad.run_drive()["result"]["ok"] and not svc.tone_on
    assert lad.run_carrier()["result"]["ok"] and not svc.tone_on
    keyed_reads = [on for name, on in heard if name in ("ALC", "SWR")]
    assert keyed_reads and all(keyed_reads)


def test_the_tone_stops_before_the_unkey(tmp_path):
    """An FT-891 unkeyed over CAT with audio still going in takes over a second to leave transmit
    (bench, 2026-10-27), past the PTT timeout; with the tone already stopped it unkeys at once."""
    svc, order = Service(), []
    rig = FakeRig(alc=(1.0, 0.08))
    set_ptt = rig.set_ptt
    rig.set_ptt = lambda on: (order.append(("ptt", on, svc.tone_on)), set_ptt(on))[1]
    lad, _, _ = make(tmp_path, rig, svc=svc)
    lad.confirm_load("dummy")
    assert lad.run_drive()["result"]["ok"] and lad.run_carrier()["result"]["ok"]
    unkeys = [tone_on for what, on, tone_on in order if what == "ptt" and not on]
    assert unkeys and not any(unkeys)  # every unkey found the tone already stopped


def test_no_tone_from_the_audio_service_fails_and_unkeys_at_once(tmp_path):
    rig, svc = FakeRig(), Service(tone_ok=False)
    lad, ptt, _ = make(tmp_path, rig, svc=svc)
    lad.confirm_load("dummy")
    assert lad.run_drive()["result"]["detail"] == L.NO_TONE
    lad.override(["drive"])
    assert lad.run_carrier()["result"]["detail"] == L.NO_TONE
    assert rig.key_log == [True, False, True, False] and ptt.lock.owner is None
    assert svc.tones == [L.TONE_S, 0, L.TONE_S, 0]  # a stop follows each start whose reply was lost


def test_the_tone_is_renewed_with_each_heartbeat_and_stopped_after(tmp_path):
    """A short deadline renewed while keyed: a lost stop leaves at most TONE_S of tone."""
    svc = Service()
    lad, _, _ = make(tmp_path, FakeRig(alc=(1.0, 1.0, 0.08)), svc=svc)
    lad.confirm_load("dummy")
    assert lad.run_drive()["result"]["ok"]
    assert svc.tones == [L.TONE_S] * 4 + [0]  # start, one renewal per ALC sample, stop
    assert L.TONE_S < 3.0  # shorter than the PTT watchdog


def test_no_tone_plays_until_keying_is_allowed(tmp_path, monkeypatch):
    """On a VOX rig the tone keys the radio, so it must wait for the Wi-Fi check, the lock and low power."""
    svc = Service()
    lad, _, _ = make(tmp_path, FakeRig(), svc=svc)
    lad.confirm_load("dummy")
    lad.override(["drive"])
    monkeypatch.setattr(L.apsetup, "unsecured", lambda: True)
    assert not lad.run_carrier()["result"]["ok"]
    assert svc.tones == []
    monkeypatch.setattr(L.apsetup, "unsecured", lambda: False)
    rig_power = []
    svc_call = svc.__call__
    lad.control_fn = lambda req: (rig_power.append(lad.cat.levels["RFPOWER"]) if req["cmd"] == "tone" else None,
                                  svc_call(req))[1]
    assert lad.run_carrier()["result"]["ok"]
    assert rig_power[0] == L.LOW_POWER  # power was down before the tone started


def test_a_failing_tone_stop_still_restores_power(tmp_path):
    svc = Service()
    lad, _, _ = make(tmp_path, FakeRig(), svc=svc)
    lad.confirm_load("dummy")
    lad.override(["drive"])

    def flaky(req):
        if req["cmd"] == "tone" and req["seconds"] == 0:
            raise OSError("control socket gone")
        return svc(req)

    lad.control_fn = flaky
    assert lad.run_carrier()["result"]["ok"] and lad.cat.levels["RFPOWER"] == 0.8


def test_high_swr_aborts_the_carrier_at_once(tmp_path):
    rig = FakeRig(swr=(1.5, 4.2, 1.0))
    lad, _, clock = make(tmp_path, rig)
    lad.confirm_load("antenna")
    lad.override(["drive"])
    r = lad.run_carrier()["result"]
    assert not r["ok"] and "4.2" in r["detail"] and not rig.keyed
    assert clock.t < L.CARRIER_S  # it did not wait out the carrier
    assert not lad.tx_allowed()


def test_override_records_what_it_bypassed_and_the_session_stays_unverified(tmp_path):
    ident = ["kx2", "internal-usb", ""]
    lad, _, _ = make(tmp_path, FakeRig(), ident=ident)
    lad.confirm_load("antenna")
    lad.override(["drive", "carrier"])
    rec = json.loads((tmp_path / "overrides.json").read_text())
    assert rec[-1]["bypassed"] == ["drive", "carrier"] and rec[-1]["load"] == "antenna"
    assert lad.tx_allowed()
    check = readiness.check_gates(readiness.Inputs(gates=lad.hook()["gates"]))
    assert check.state == readiness.WARN and "unverified" in check.detail and "drive" in check.detail
    # A gate proven later on a re-run drops out of the caveat (REQ-WEB-034); one still unproven stays
    # unverified for the rest of the session. The KX2 reports no ALC, so Drive cannot pass here.
    assert not lad.run_drive()["result"]["ok"]
    assert lad.run_carrier()["result"]["ok"]
    assert lad.hook()["gates"]["bypassed"] == ["drive"]
    ident[0] = "ic-705"  # changing the rig does not erase it either
    assert lad.hook()["gates"]["bypassed"] == ["drive"]


def test_watchdog_stays_armed_under_full_override(tmp_path):
    rig = FakeRig()
    lad, ptt, clock = make(tmp_path, rig)
    lad.confirm_load("antenna")
    lad.override(["drive", "carrier"])
    assert lad.key("client") and ptt.watchdog_armed and rig.keyed
    clock.t += 2.0
    lad.heartbeat("client")
    assert not ptt.check() and rig.keyed
    clock.t += 3.1  # the client went quiet
    assert ptt.check() and not rig.keyed and ptt.lock.owner is None
    assert not hasattr(lad, "disable_watchdog") and "watchdog" in lad.report()["never_overridable"]


def test_tx_lock_holds_under_full_override(tmp_path):
    lad, _, _ = make(tmp_path, FakeRig())
    lad.confirm_load("antenna")
    lad.override(["drive", "carrier"])
    assert lad.key("first") and not lad.key("second")
    with pytest.raises(L.LadderError, match="in use by first"):  # the ladder cannot take the rig from a client either
        lad.run_carrier()
    assert lad.tx_allowed()  # and being refused changed nothing


@pytest.mark.parametrize("field,value", [(0, "kx2"), (1, "digirig"), (2, "field-kit")])
def test_relocks_when_radio_interface_or_preset_changes(tmp_path, field, value):
    ident = ["ic-705", "internal-usb", ""]
    rig = FakeRig()
    lad, _, _ = make(tmp_path, rig, ident=ident)
    lad.confirm_load("dummy")
    lad.run_drive()
    lad.run_carrier()
    assert lad.key("client")
    lad.unkey("client")
    ident[field] = value
    assert not lad.tx_allowed() and not lad.key("client")
    assert lad.report()["next"] == "load"


def test_rig_without_swr_always_says_the_antenna_cannot_be_verified(tmp_path):
    rig = FakeRig(levels={"VD_METER": 12.0})
    lad, _, clock = make(tmp_path, rig, profile="usdx")
    assert not profiles.get("usdx").can_report_swr
    lad.confirm_load("antenna")
    lad.override(["drive"])
    assert lad.run_carrier()["result"]["ok"]
    rep = lad.report()
    assert "cannot be verified" in rep["antenna_note"] and rep["max_tx_s"] == L.CAPPED_MAX_TX_S
    lad.report_meter(True)  # the operator read the rig's own meter: still stated
    assert "cannot be verified" in lad.report()["antenna_note"]
    check = readiness.check_gates(readiness.Inputs(gates=lad.hook()["gates"]))
    assert check.state == readiness.WARN and "cannot be verified" in check.detail
    # Capped: a long transmission is ended.
    assert lad.key("client")
    clock.t += L.CAPPED_MAX_TX_S
    lad.heartbeat("client")
    assert not rig.keyed


def test_operator_seeing_high_swr_on_the_rig_meter_fails_the_gate(tmp_path):
    lad, _, _ = make(tmp_path, FakeRig(), profile="usdx")
    lad.confirm_load("antenna")
    lad.override(["drive"])
    lad.run_carrier()
    assert not lad.report_meter(False)["result"]["ok"] and not lad.tx_allowed()


def test_supply_sag_aborts_when_swr_cannot_be_read(tmp_path):
    rig = FakeRig()
    volts = iter([12.0, 9.5, 9.5])
    real = rig.get_level
    rig.get_level = lambda n: next(volts) if n == "VD_METER" else real(n)
    lad, _, _ = make(tmp_path, rig, profile="usdx")
    lad.confirm_load("antenna")
    lad.override(["drive"])
    r = lad.run_carrier()["result"]
    assert not r["ok"] and "sagged" in r["detail"] and not rig.keyed


def test_power_that_cannot_be_set_over_cat_needs_the_operator(tmp_path):
    rig = FakeRig()
    rig.levels.pop("RFPOWER")
    lad, _, _ = make(tmp_path, rig)
    lad.confirm_load("dummy")
    lad.override(["drive"])
    assert "by hand" in lad.run_carrier()["result"]["detail"] and rig.key_log == []
    assert lad.run_carrier(power_by_hand=True)["result"]["ok"]


def test_http_actions(tmp_path):
    lad, _, _ = make(tmp_path, FakeRig())
    assert L.handle(lad, {"action": "override", "gates": ["drive"]})[0] == 409
    assert L.handle(lad, {"action": "confirm_load", "load": "dummy"})[0] == 200
    assert L.handle(lad, {"action": "override", "gates": ["load"]})[0] == 409
    assert L.handle(lad, {"action": "nonsense"})[0] == 400
    code, rep = L.handle(lad, {"action": "override", "gates": ["drive", "carrier"]})
    assert code == 200 and rep["tx_allowed"] and rep["unverified"] == ["drive", "carrier"]


def test_http_transmit_page_and_ladder_api_through_the_web_server(tmp_path):
    import threading
    import urllib.error
    import urllib.request

    from vector_bridge import tuner

    lad, _, _ = make(tmp_path, FakeRig())
    app = tuner.TunerApp(Service(), mixer_fn=lambda c: [], ladder=lad)
    httpd = tuner.serve(app, bind="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(body):
        req = urllib.request.Request(base + "/api/ladder", data=json.dumps(body).encode(), method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    try:
        with urllib.request.urlopen(base + "/transmit") as r:
            assert "can never be skipped" in r.read().decode()
        assert post({"action": "override", "gates": ["drive", "carrier"]})[0] == 409
        assert post({"action": "confirm_load", "load": "dummy"})[1]["next"] == "drive"
        with urllib.request.urlopen(base + "/api/ladder") as r:
            assert json.load(r)["tx_allowed"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_failed_carrier_cannot_be_passed_by_a_meter_report(tmp_path):
    rig = FakeRig(levels={"VD_METER": 12.0})
    lad, _, _ = make(tmp_path, rig, profile="usdx")
    lad.confirm_load("antenna")
    lad.override(["drive"])
    assert lad.run_carrier()["result"]["ok"]
    rig.levels["VD_METER"] = 12.0
    volts = iter([12.0, 9.0])
    real = rig.get_level
    rig.get_level = lambda n: next(volts) if n == "VD_METER" else real(n)
    assert not lad.run_carrier()["result"]["ok"]
    with pytest.raises(L.LadderError, match="carrier check first"):
        lad.report_meter(True)
    assert not lad.tx_allowed()


@pytest.mark.parametrize("levels,swr", [({"RFPOWER_METER_WATTS": 0.0}, (1.0,)), ({}, (0.0,))])
def test_a_carrier_with_no_rf_proves_nothing(tmp_path, levels, swr):
    lad, _, _ = make(tmp_path, FakeRig(swr=swr, levels=levels))
    lad.confirm_load("dummy")
    lad.override(["drive"])
    r = lad.run_carrier()["result"]
    assert not r["ok"] and "No power left" in r["detail"] and "data mode" in r["detail"] and "ham band" in r["detail"]


def test_a_relock_takes_a_keyed_client_off_the_air(tmp_path):
    ident = ["kx2", "internal-usb", ""]
    rig = FakeRig()
    lad, ptt, _ = make(tmp_path, rig, ident=ident)
    lad.confirm_load("dummy")
    lad.override(["drive", "carrier"])
    assert lad.key("client") and rig.keyed
    ident[0] = "ic-705"
    lad.heartbeat("client")  # even a client that keeps its heartbeats going
    assert not rig.keyed and ptt.lock.owner is None


def _keyed_client(tmp_path, svc):
    rig = FakeRig()
    lad, ptt, clock = make(tmp_path, rig, svc=svc)
    lad.confirm_load("dummy")
    lad.override(["drive", "carrier"])
    assert lad.key("client") and rig.keyed
    return lad, ptt, clock, rig


@pytest.mark.parametrize("queued_ms,tail_s", [(140, 0.14), (900, 0.3), (0, 0.0)])
def test_a_deliberate_unkey_holds_ptt_until_the_queued_audio_has_played_bounded_at_300_ms(tmp_path, queued_ms, tail_s):
    lad, ptt, clock, rig = _keyed_client(tmp_path, Service(queued_ms=queued_ms))
    t0 = clock()
    assert lad.unkey("client")
    assert not rig.keyed and ptt.lock.owner is None
    assert clock() - t0 == pytest.approx(tail_s)


def test_an_unkey_with_no_answer_from_the_audio_service_holds_ptt_only_the_bound(tmp_path):
    lad, _ptt, clock, rig = _keyed_client(tmp_path, Service())
    lad.control_fn = lambda req: None  # the audio service is not answering
    t0 = clock()
    assert lad.unkey("client") and not rig.keyed
    assert clock() - t0 == pytest.approx(L.PTT_TAIL_MAX_S)


def test_a_safety_unkey_drops_ptt_at_once(tmp_path):
    lad, ptt, clock, rig = _keyed_client(tmp_path, Service(queued_ms=140))
    t0 = clock()
    assert lad.unkey("client", drain=False) and not rig.keyed  # the proxy's cap and disconnect path
    assert clock() == t0
    assert lad.key("client") and rig.keyed
    t0 = clock()
    clock.t += 3.0
    assert ptt.check() and not rig.keyed  # the watchdog never asks the ladder, so it never waits
    assert clock() - t0 == 3.0
    assert not lad.unkey("someone else")  # a non-owner's unkey does not even look at the queue
    assert clock() - t0 == 3.0


def test_power_stays_at_minimum_when_the_antenna_cannot_be_verified(tmp_path):
    rig = FakeRig(levels={"VD_METER": 12.0})
    lad, _, _ = make(tmp_path, rig, profile="usdx")
    lad.confirm_load("antenna")
    lad.override(["drive"])
    assert lad.run_carrier()["result"]["ok"]
    assert rig.levels["RFPOWER"] == L.LOW_POWER


def test_capped_transmissions_need_a_rest_before_the_next(tmp_path):
    rig = FakeRig(levels={"VD_METER": 12.0})
    lad, _, clock = make(tmp_path, rig, profile="usdx")
    lad.confirm_load("antenna")
    lad.override(["drive"])
    lad.run_carrier()
    assert lad.key("client")
    clock.t += L.CAPPED_MAX_TX_S
    lad.heartbeat("client")
    assert not lad.key("client")
    clock.t += L.CAPPED_REST_S
    assert lad.key("client")


def test_home_screen_says_locked_after_a_relock_even_with_an_earlier_bypass(tmp_path):
    ident = ["kx2", "internal-usb", ""]
    lad, _, _ = make(tmp_path, FakeRig(), ident=ident)
    lad.confirm_load("antenna")
    lad.override(["drive", "carrier"])
    ident[2] = "other-kit"
    check = readiness.check_gates(readiness.Inputs(gates=lad.hook()["gates"]))
    assert "locked" in check.detail and "bypassed drive, carrier" in check.detail


def test_hostile_http_bodies_cannot_weaken_the_lock_or_watchdog(tmp_path):
    rig = FakeRig()
    lad, ptt, clock = make(tmp_path, rig)
    body = {"action": "override", "gates": ["drive", "carrier"], "watchdog": False, "watchdog_s": 9999,
            "tx_lock": False, "load": "dummy"}
    assert L.handle(lad, body)[0] == 409 and lad.load is None  # still needs gate one; "load" here is ignored
    L.handle(lad, {"action": "confirm_load", "load": "antenna"})
    assert L.handle(lad, body)[0] == 200 and ptt.watchdog_s == 3.0
    assert L.handle(lad, {"action": "override", "gates": "drive"})[0] == 409
    assert lad.key("a") and not lad.key("b")
    clock.t += 3.1
    assert ptt.check() and not rig.keyed


def test_a_bypass_that_cannot_be_written_to_the_card_is_reported(tmp_path, monkeypatch):
    lad, _, _ = make(tmp_path, FakeRig())
    monkeypatch.setattr(persist, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    lad.confirm_load("antenna")
    assert "could not be written" in lad.override(["drive"])["record_error"]


def test_client_ptt_over_http_goes_through_the_ladder(tmp_path):
    rig = FakeRig()
    lad, ptt, _ = make(tmp_path, rig)
    code, rep = L.handle(lad, {"action": "key", "owner": "ipad"})
    assert code == 409 and rep["ok"] is False and not rig.keyed  # gates not passed: locked
    assert L.handle(lad, {"action": "key"})[0] == 400  # no owner
    lad.confirm_load("dummy")
    lad.override(["drive", "carrier"])
    for act in ("key", "heartbeat", "unkey"):  # the ladder's own name is not for clients
        assert L.handle(lad, {"action": act, "owner": L.OWNER})[0] == 409
    assert L.handle(lad, {"action": "key", "owner": "ipad"})[0] == 200 and rig.keyed
    assert L.handle(lad, {"action": "key", "owner": "laptop"})[0] == 409  # tx lock
    assert L.handle(lad, {"action": "heartbeat", "owner": "ipad"})[0] == 200
    assert L.handle(lad, {"action": "unkey", "owner": "laptop"})[0] == 409 and rig.keyed
    assert L.handle(lad, {"action": "unkey", "owner": "ipad"})[0] == 200
    assert not rig.keyed and ptt.lock.owner is None


def test_a_repeat_key_does_not_extend_a_capped_transmission(tmp_path):
    rig = FakeRig(levels={"VD_METER": 12.0})
    lad, _, clock = make(tmp_path, rig, profile="usdx")
    lad.confirm_load("antenna")
    lad.override(["drive"])
    assert lad.run_carrier()["result"]["ok"]
    assert lad.key("client")
    clock.t += L.CAPPED_MAX_TX_S - 1
    assert lad.key("client")  # still the owner, but the clock keeps running
    clock.t += 1
    lad.heartbeat("client")
    assert not rig.keyed


def test_receive_audio_and_a_flat_battery_gate_a_fresh_key(tmp_path):
    rig, rx, batt = FakeRig(), {"ok": False}, {"ok": True, "present": True, "percent": 50, "plugged": False}
    lad, _, _ = make(tmp_path, rig, rx_ok_fn=lambda: rx["ok"], battery_fn=lambda: batt)
    lad.confirm_load("dummy")
    lad.override(["drive", "carrier"])
    assert not lad.key("ipad") and not rig.keyed  # receive red: fix receive first
    rx["ok"] = True
    batt["percent"] = 6
    assert not lad.key("ipad") and not rig.keyed  # battery at the lock level
    batt["plugged"] = True
    assert lad.key("ipad") and rig.keyed
    rx["ok"] = False
    assert lad.key("ipad") and rig.keyed  # a repeat key by the owner is not a new transmission


def test_the_hook_reports_a_fresh_unkey_for_the_grace_period_only(tmp_path):
    lad, _, clock = make(tmp_path, FakeRig())
    assert not lad.hook()["tx_just_released"]
    lad.confirm_load("dummy")
    lad.override(["drive"])
    assert lad.run_carrier()["result"]["ok"]
    assert lad.hook()["tx_just_released"] and lad.hook()["tx_owner"] is None
    clock.t += tx.RELEASE_GRACE_S + 0.01  # the fake clock sums 0.2 s steps, so allow float error
    assert not lad.hook()["tx_just_released"]


class StuckRig(FakeRig):
    def set_ptt(self, on):
        super().set_ptt(on)
        if not on:
            raise CatError("rigctld timed out")


def test_a_failed_unkey_still_stops_the_tone_that_keys_a_vox_rig(tmp_path):
    svc = Service()
    lad, ptt, _ = make(tmp_path, StuckRig(), profile="signalink-generic", svc=svc)
    lad.confirm_load("dummy")
    lad.override(["drive"])
    with pytest.raises(CatError):  # the caller hears that the unkey failed
        lad.run_carrier(power_by_hand=True)
    assert svc.tones[-1] == 0 and not svc.tone_on
    assert ptt.keyed  # the unkey failed, so the watchdog keeps retrying it


def test_a_pass_after_a_failed_carrier_restores_the_power_from_before_the_ladder(tmp_path):
    """A failed run leaves the radio at the test setting; the next run must not take that as the operator's power."""
    rig = FakeRig(swr=(4.2,))
    lad, _, _ = make(tmp_path, rig)
    lad.confirm_load("antenna")
    lad.override(["drive"])
    assert not lad.run_carrier()["result"]["ok"]
    assert rig.levels["RFPOWER"] == L.LOW_POWER  # deliberately left low after a failure
    rig.swr = [1.2]
    assert lad.run_carrier()["result"]["ok"]
    assert rig.levels["RFPOWER"] == 0.8


def test_power_set_by_hand_after_a_failed_carrier_is_the_one_restored(tmp_path):
    rig = FakeRig(swr=(4.2,))
    lad, _, _ = make(tmp_path, rig)
    lad.confirm_load("antenna")
    lad.override(["drive"])
    lad.run_carrier()
    rig.levels["RFPOWER"] = 0.5  # the operator followed the fail card and set their power
    rig.swr = [1.2]
    assert lad.run_carrier()["result"]["ok"]
    assert rig.levels["RFPOWER"] == 0.5


def test_another_radio_does_not_inherit_the_remembered_power(tmp_path):
    rig, ident = FakeRig(swr=(4.2,)), ["ic-705", "internal-usb", ""]
    lad, _, _ = make(tmp_path, rig, ident=ident)
    lad.confirm_load("antenna")
    lad.override(["drive"])
    lad.run_carrier()
    ident[1] = "signalink"  # a different interface: the ladder starts over
    lad.confirm_load("antenna")
    lad.override(["drive"])
    rig.swr = [1.2]
    assert lad.run_carrier()["result"]["ok"]
    assert rig.levels["RFPOWER"] == L.LOW_POWER  # what this radio read, not the 0.8 of the one before


def test_a_kit_with_no_radio_control_never_trusts_the_dummy_rigs_levels(tmp_path):
    # RTS keying with no CAT runs Hamlib's dummy rig, which answers every level with a made-up value.
    rig = FakeRig(levels={"RFPOWER_METER_WATTS": 0.0, "VD_METER": 13.8})
    lad, _, _ = make(tmp_path, rig, profile="digirig-generic")
    lad.confirm_load("dummy")
    assert "does not report ALC" in lad.run_drive()["result"]["detail"]
    lad.override(["drive"])
    assert "by hand" in lad.run_carrier()["result"]["detail"] and rig.key_log == []
    r = lad.run_carrier(power_by_hand=True)["result"]
    assert r["ok"] and "Nothing could be measured" in r["detail"]  # not "No power left" from a fake meter
    assert rig.levels["RFPOWER"] == 0.8  # power was never set through the dummy rig


def test_unlock_is_refused_outside_every_ham_band_until_the_operator_allows_it(tmp_path):
    rig = FakeRig(freq=10_000_000, mode="AM")  # WWV: the 2026-09-30 IC-705 keyed here with no RF
    lad, _, _ = make(tmp_path, rig)
    with pytest.raises(L.LadderError, match="10.000 MHz, outside every amateur band"):
        lad.confirm_load("dummy")
    lad.allow_any_band()
    lad.confirm_load("dummy")
    assert lad.report()["any_band"] and "band" in lad.hook()["gates"]["bypassed"]
    assert json.loads((tmp_path / "overrides.json").read_text())[-1]["bypassed"] == ["band"]


def test_a_retune_out_of_band_after_the_load_stops_the_keyed_checks(tmp_path):
    rig = FakeRig()
    lad, _, _ = make(tmp_path, rig)
    lad.confirm_load("dummy")
    rig.freq = 1_130_000  # AM broadcast
    with pytest.raises(L.LadderError, match="outside every amateur band"):
        lad.run_drive()
    assert True not in rig.key_log


def test_tune_sets_the_frequency_and_data_mode_the_operator_picked(tmp_path):
    rig = FakeRig(freq=1_130_000, mode="AM")
    lad, _, _ = make(tmp_path, rig)
    r = lad.tune(144_390_000, "PKTFM")
    assert (rig.freq, rig.mode) == (144_390_000, "PKTFM")
    assert r["result"]["ok"] and r["radio"]["band"] == "2 m"
    with pytest.raises(L.LadderError, match="outside every amateur band"):
        lad.tune(10_000_000, "PKTUSB")
    with pytest.raises(L.LadderError, match="data mode"):
        lad.tune(14_074_000, "AM")
    assert rig.freq == 144_390_000


def test_tune_reports_a_radio_that_did_not_take_the_setting(tmp_path):
    class Stubborn(FakeRig):
        def set_mode(self, mode):
            pass  # an FM-only rig ignores PKTUSB
    lad, _, _ = make(tmp_path, Stubborn(mode="FM"))
    r = lad.tune(146_520_000, "PKTUSB")
    assert not r["result"]["ok"] and "not 146.520 MHz PKTUSB" in r["result"]["detail"]


def test_a_rig_with_no_radio_control_is_not_asked_its_frequency(tmp_path):
    class NoCat(FakeRig):
        def get_freq(self):
            raise AssertionError("no CAT on this rig")
    lad, _, _ = make(tmp_path, NoCat(), profile="digirig-generic")
    lad.confirm_load("antenna")
    assert lad.report()["can_tune"] is False
    with pytest.raises(L.LadderError):
        lad.tune(14_074_000, "PKTUSB")


def test_skipping_the_tests_never_skips_the_band(tmp_path):
    rig = FakeRig()
    lad, _, _ = make(tmp_path, rig)
    lad.confirm_load("antenna")
    rig.freq = 10_000_000
    with pytest.raises(L.LadderError, match="outside every amateur band"):
        lad.override(["drive", "carrier"])
    assert not lad.tx_allowed()


def test_a_client_key_after_unlock_is_refused_once_the_radio_leaves_the_bands(tmp_path):
    rig = FakeRig()
    lad, _, _ = make(tmp_path, rig)
    lad.confirm_load("dummy")
    lad.override(["drive", "carrier"])
    rig.freq = 10_000_000  # WSJT-X retuned over 4532, or the VFO turned
    assert not lad.key("wsjtx") and True not in rig.key_log
    lad.allow_any_band()
    assert lad.key("wsjtx") and rig.keyed


def test_a_listen_only_tune_leaves_the_bands_but_never_transmit(tmp_path):
    rig = FakeRig()
    lad, _, _ = make(tmp_path, rig)
    lad.confirm_load("dummy")  # unlocked in band first
    lad.override(["drive", "carrier"])
    bypassed = lad.hook()["gates"]["bypassed"]
    assert L.handle(lad, {"action": "tune", "freq_hz": 10_000_000, "mode": "PKTUSB"}) == (
        409, {"ok": False, "error": L.TUNE_OUT_OF_BAND.format(f="10.000 MHz"), "code": "out_of_band"})
    assert L.handle(lad, {"action": "tune", "freq_hz": 10_000_000, "mode": "PKTUSB", "listen_only": "yes"})[0] == 409
    code, r = L.handle(lad, {"action": "tune", "freq_hz": 10_000_000, "mode": "PKTUSB", "listen_only": True})
    assert code == 200 and r["result"]["ok"] and rig.freq == 10_000_000
    assert r["radio"]["band"] is None and not r["radio"]["any_band"]
    assert lad.hook()["gates"]["bypassed"] == bypassed  # no new bypass recorded
    assert not lad.key("ipad") and True not in rig.key_log  # off band: no transmit


def test_a_factory_reset_forgets_the_sessions_bypasses_and_their_record(tmp_path):
    lad, _, _ = make(tmp_path, FakeRig(), ident=["kx2", "internal-usb", ""])
    lad.confirm_load("antenna")
    lad.override(["drive"])
    assert (tmp_path / "overrides.json").exists() and lad.hook()["gates"]["bypassed"] == ["drive"]
    assert lad.clear_record()
    assert not (tmp_path / "overrides.json").exists() and lad.hook()["gates"]["bypassed"] == []


# -- REQ-AUDIO-019: the Drive search runs both ways from the profile's start level --

def test_drive_raises_from_the_profile_start_until_the_radio_hears_the_tone_then_lowers_to_target(tmp_path):
    # IC-705 at the config's 20 is silence; the search begins at the profile's 70, climbs until ALC >= 0.01,
    # then steps down until ALC <= the 0.10 target. Every level change is followed by one 0.2 s sample.
    rig = FakeRig(alc=(0.0, 0.0, 0.5, 0.3, 0.09))
    svc = Service(tx=20)
    lad, _, clock = make(tmp_path, rig, svc=svc)
    lad.confirm_load("dummy")
    levels = []
    lad.control_fn = lambda req: (levels.append(req["tx"]) if req["cmd"] == "set_gain" else None, svc(req))[1]
    r = lad.run_drive()["result"]
    assert r["ok"] and "transmit level 70" in r["detail"]
    assert levels == [70, 75, 80, 75, 70]  # start, two up, two down
    assert svc.tx == 70 and clock.t == pytest.approx(5 * L.SAMPLE_GAP_S)
    assert lad.report()["unverified"] == [] and "drive" in lad.passed


def test_drive_fails_when_no_alc_is_seen_at_level_100(tmp_path):
    svc = Service(tx=20)
    lad, _, _ = make(tmp_path, FakeRig(alc=(0.0,)), svc=svc)
    lad.confirm_load("dummy")
    r = lad.run_drive()["result"]
    assert not r["ok"] and "No drive is reaching the radio" in r["detail"] and "level 100" in r["detail"]
    assert svc.tx == 100 and "drive" not in lad.passed


def test_drive_fails_when_alc_stays_above_target_at_level_0(tmp_path):
    svc = Service(tx=70)
    lad, _, _ = make(tmp_path, FakeRig(alc=(0.9,)), svc=svc)
    lad.confirm_load("dummy")
    r = lad.run_drive()["result"]
    assert not r["ok"] and "lowest transmit level" in r["detail"] and svc.tx == 0


def test_drive_gives_up_after_twenty_steps(tmp_path):
    # Silent up to 100 (6 raises), then ALC stuck at 0.5 on the way down: the 20-step budget ends it at level 30.
    svc = Service(tx=70)
    lad, _, _ = make(tmp_path, FakeRig(alc=(0.0,) * 6 + (0.5,)), svc=svc)
    lad.confirm_load("dummy")
    r = lad.run_drive()["result"]
    assert not r["ok"] and "20 level steps" in r["detail"] and svc.tx == 30
    assert "drive" not in lad.passed


def test_drive_search_starts_from_a_level_the_operator_raised_above_the_profile_start(tmp_path):
    # Raise Level on the failure card moved the slider to 90: Retry must search from 90, not reset to 70.
    svc = Service(tx=90)
    lad, _, _ = make(tmp_path, FakeRig(alc=(0.05,)), svc=svc)
    lad.confirm_load("dummy")
    r = lad.run_drive()["result"]
    assert r["ok"] and svc.tx == 90 and "transmit level 90" in r["detail"]


def test_a_radio_with_no_alc_readout_gets_the_manual_slider_text_and_is_not_keyed(tmp_path):
    rig = FakeRig()
    lad, _, _ = make(tmp_path, rig, ident=["kx2", "internal-usb", ""])
    lad.confirm_load("dummy")
    r = lad.run_drive()["result"]
    assert not r["ok"] and r["detail"] == L.MANUAL_DRIVE and "slider" in r["detail"]
    assert rig.key_log == []
    assert lad.report()["tx_start_gain"] == 20  # the KX2's start, shown on the sheet


def test_a_bypassed_gate_that_later_passes_is_no_longer_unverified(tmp_path):
    # REQ-WEB-034: override drive, then run drive to a pass; `unverified` no longer contains drive.
    lad, _, _ = make(tmp_path, FakeRig(alc=(0.05,)), svc=Service(tx=70))
    lad.confirm_load("dummy")
    lad.override(["drive"])
    assert lad.report()["unverified"] == ["drive"] and lad.hook()["gates"]["bypassed"] == ["drive"]
    assert lad.run_drive()["result"]["ok"]
    assert lad.report()["unverified"] == [] and lad.hook()["gates"]["bypassed"] == []
    assert lad.hook()["gates"]["drive_passed"]
    # The any-band defeat is not a gate and is never cleared by a pass.
    lad.allow_any_band()
    lad.run_drive()
    assert lad.report()["unverified"] == [L.ANY_BAND]
