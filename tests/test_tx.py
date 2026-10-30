"""TX lock is exclusive; the hang-PTT watchdog unkeys a silent owner and cannot be disarmed."""

import time

import pytest

from vector_bridge.tx import PttController, TxLock


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(watchdog_s=3.0):
    clk, ptt_log, hangs = Clock(), [], []
    ctl = PttController(ptt_log.append, TxLock(), watchdog_s=watchdog_s, clock=clk, on_hang=hangs.append)
    return ctl, clk, ptt_log, hangs


def test_lock_is_exclusive_until_released():
    lock = TxLock()
    assert lock.acquire("ipad") and lock.acquire("ipad")
    assert not lock.acquire("laptop")
    assert not lock.release("laptop") and lock.owner == "ipad"
    assert lock.release("ipad") and lock.acquire("laptop")


def test_second_client_cannot_key_while_first_holds_ptt():
    ctl, _clk, ptt_log, _ = make()
    assert ctl.key("ipad") is True and ptt_log == [True]
    assert ctl.key("laptop") is False and ptt_log == [True]
    assert ctl.unkey("laptop") is False and ctl.keyed
    assert ctl.unkey("ipad") is True and ptt_log == [True, False] and ctl.lock.owner is None


def test_watchdog_unkeys_when_heartbeats_stop_and_stays_armed():
    ctl, clk, ptt_log, hangs = make(watchdog_s=3.0)
    ctl.key("ipad")
    clk.t += 2.5
    ctl.heartbeat("ipad")
    clk.t += 2.5
    assert ctl.check() is False and ctl.keyed  # 2.5 s since last beat, still fine
    assert ctl.watchdog_armed is True
    clk.t += 1.0
    assert ctl.check() is True and not ctl.keyed  # 3.5 s silence: unkeyed
    assert ptt_log == [True, False] and hangs == ["ipad"] and ctl.hang_count == 1
    assert ctl.lock.owner is None
    assert not hasattr(ctl, "disarm")


def test_heartbeat_from_non_owner_does_not_feed_watchdog():
    ctl, clk, ptt_log, _ = make(watchdog_s=3.0)
    ctl.key("ipad")
    clk.t += 4
    ctl.heartbeat("laptop")
    assert ctl.check() is True and ptt_log == [True, False]


def test_a_failed_key_frees_the_lock_and_leaves_the_rig_unkeyed():
    calls, fail = [], [True]

    def set_ptt(on):
        calls.append(on)
        if on and fail[0]:
            raise OSError("rigctld went away")

    ctl = PttController(set_ptt, TxLock(), clock=Clock())
    with pytest.raises(OSError):
        ctl.key("ipad")
    assert calls == [True, False] and not ctl.keyed and ctl.lock.owner is None
    fail[0] = False
    assert ctl.key("laptop") is True  # nobody is locked out by the failure


def test_watchdog_thread_survives_a_failing_check():
    ptt_log, fail = [], [True]

    def set_ptt(on):
        if not on and fail[0]:
            raise OSError("rigctld went away")
        ptt_log.append(on)

    ctl = PttController(set_ptt, TxLock(), watchdog_s=0.04)
    ctl.key("ipad")
    ctl.start()
    try:
        time.sleep(0.15)  # several failed unkeys
        assert ctl._thread.is_alive() and ctl.keyed
        fail[0] = False
        time.sleep(0.15)
        assert ptt_log == [True, False] and not ctl.keyed and ctl.lock.owner is None
    finally:
        ctl.stop()


def test_a_key_that_cannot_be_undone_stays_armed_for_the_watchdog():
    clk, calls, fail = Clock(), [], [True]

    def set_ptt(on):
        calls.append(on)
        if fail[0]:
            raise OSError("rigctld went away")

    ctl = PttController(set_ptt, TxLock(), watchdog_s=3.0, clock=clk)
    with pytest.raises(OSError):
        ctl.key("ipad")
    assert ctl.keyed and ctl.lock.owner == "ipad"  # RX not proven: nobody else may key
    fail[0] = False
    clk.t += 3.5
    assert ctl.check() is True and calls[-1] is False and ctl.lock.owner is None


def test_a_key_that_lands_late_is_unkeyed_by_the_watchdog():
    """Seen on the IC-705: rigctld stalled, the key timed out, then ran after the unkey."""
    clk, calls, rig, fail = Clock(), [], [False], [True]

    def set_ptt(on):
        calls.append(on)
        if fail[0] and on:
            raise OSError("timed out")
        rig[0] = on

    ctl = PttController(set_ptt, TxLock(), watchdog_s=3.0, clock=clk, get_ptt=lambda: rig[0])
    with pytest.raises(OSError):
        ctl.key("ladder")
    assert not ctl.keyed and ctl.lock.owner is None
    rig[0] = True  # the stalled key reaches the radio now
    clk.t += 1.0
    assert ctl.check() is False and calls == [True, False, False] and rig[0] is False
    rig[0] = True  # long after the failure a keyed rig is the operator's own microphone
    clk.t += 60.0
    ctl.check()
    assert rig[0] is True


def test_a_fresh_controller_unkeys_a_rig_left_keyed_by_a_crash():
    clk, rig = Clock(), [True]  # the previous process died while keyed

    def set_ptt(on):
        rig[0] = on

    ctl = PttController(set_ptt, TxLock(), clock=clk, get_ptt=lambda: rig[0])
    assert not ctl.keyed
    clk.t += 1.0
    assert ctl.check() is False and rig[0] is False


def test_stop_retries_the_unkey_through_transient_failures():
    calls, fails = [], [3]

    def set_ptt(on):
        calls.append(on)
        if not on and fails[0]:
            fails[0] -= 1
            raise OSError("rigctld busy")

    ctl = PttController(set_ptt, TxLock(), clock=Clock())
    ctl.stop_retry_s = 0
    ctl.key("ipad")
    ctl.stop()
    assert calls == [True, False, False, False, False]
    assert not ctl.keyed and ctl.lock.owner is None


def test_stop_does_not_raise_when_every_unkey_fails(caplog):
    calls = []

    def set_ptt(on):
        calls.append(on)
        if not on:
            raise OSError("rigctld went away")

    ctl = PttController(set_ptt, TxLock(), clock=Clock())
    ctl.stop_retry_s = 0
    ctl.key("ipad")
    ctl.stop()  # returns, so the rest of shutdown still runs
    assert calls == [True] + [False] * 5
    assert ctl.keyed and ctl.lock.owner == "ipad"
    assert "RIG MAY STILL BE KEYED" in caplog.text


def test_tuner_shutdown_stops_rigctld_when_a_step_raises(monkeypatch):
    from types import SimpleNamespace

    from vector_bridge import tuner

    stopped = []

    class Fake:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            return lambda *a, **k: None

    class Rig(Fake):
        def stop(self):
            stopped.append("rig")

    class Ptt(Fake):
        def stop(self):
            raise OSError("unkey failed")

    class Httpd(Fake):
        def serve_forever(self):
            raise KeyboardInterrupt

    class Proxy(Fake):
        port = 4532
        listening = ("127.0.0.1", "10.42.0.1")

        def stop(self):
            stopped.append("proxy")

    monkeypatch.setattr(tuner, "RigctldSupervisor", Rig)
    monkeypatch.setattr(tuner, "RigctldClient", Fake)
    monkeypatch.setattr(tuner, "PttController", Ptt)
    monkeypatch.setattr(tuner, "TunerApp", Fake)
    monkeypatch.setattr(tuner, "serve", lambda *a, **k: Httpd())
    monkeypatch.setattr(tuner.wizard, "Wizard", Fake)
    monkeypatch.setattr(tuner.wizard, "Probes", Fake)
    monkeypatch.setattr(tuner.ladder_mod, "Ladder", Fake)
    monkeypatch.setattr(tuner.readiness, "Monitor", Fake)
    monkeypatch.setattr(tuner.rigproxy, "RigctlProxy", Proxy)
    monkeypatch.setattr(tuner.readiness, "make_gather", lambda *a, **k: None)
    monkeypatch.setattr(tuner.alerts_mod, "AlertConfig", Fake)
    monkeypatch.setattr(tuner.alerts_mod, "Alerter", Fake)
    monkeypatch.setattr(tuner.signal, "signal", lambda *a: None)
    args = SimpleNamespace(host="127.0.0.1", port=0, config=None, bind="127.0.0.1", http_port=0)
    assert tuner.cmd_tuner(args) == 0
    assert stopped == ["proxy", "rig"]  # desktop clients are cut off before PTT and rigctld stop
