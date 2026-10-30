"""The rigctl safety proxy (M4): desktop keys go through the Ladder, one keyer at a time,
unkeyed on disconnect and after the cap; everything else passes through to rigctld."""

import socket
import threading
import time
from types import SimpleNamespace

import pytest

from vector_bridge import rigproxy as P
from vector_bridge.tx import PttController


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakeRigctld:
    """Line-at-a-time rigctld on a real socket: answers f, acknowledges anything else."""

    def __init__(self):
        self.lines = []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        f = c.makefile("rb")
        for raw in f:
            line = raw.decode().strip()
            self.lines.append(line)
            c.sendall(b"14074000\n" if line in ("f", "+\\get_freq") else b"RPRT 0\n")
        c.close()

    def close(self):
        self.srv.close()


class FakeLadder:
    """The Ladder's keying door: its gates collapse to one flag, the PTT controller is real."""

    def __init__(self, ptt):
        self.ptt, self.allowed = ptt, True
        self.unkeys = []  # (owner, drain) as the proxy asked

    def unkey(self, owner, drain=True):
        self.unkeys.append((owner, drain))
        return self.ptt.unkey(owner)

    def key(self, owner):
        return self.allowed and self.ptt.key(owner)

    def heartbeat(self, owner):
        self.ptt.heartbeat(owner)


class Rig:
    def __init__(self):
        self.log = []

    def set_ptt(self, on):
        self.log.append(on)


@pytest.fixture
def bench():
    clock, rig, up = Clock(), Rig(), FakeRigctld()
    ptt = PttController(rig.set_ptt, clock=clock)
    ladder = FakeLadder(ptt)
    proxy = P.RigctlProxy(ladder, port=0, upstream_port=up.port, tick_s=3600, clock=clock)
    proxy.start()
    yield SimpleNamespace(proxy=proxy, ladder=ladder, ptt=ptt, rig=rig, clock=clock, up=up)
    proxy.stop()
    up.close()


def connect(proxy):
    c = socket.create_connection(("127.0.0.1", proxy.port), timeout=2)
    deadline = time.monotonic() + 2
    while not proxy.sessions and time.monotonic() < deadline:
        time.sleep(0.01)
    return c


def ask(c, line: bytes) -> bytes:
    c.sendall(line)
    out = b""
    while not out.endswith(b"\n"):
        out += c.recv(4096)
    return out


def owner_of(c):
    host, port = c.getsockname()
    return f"rigctl {host}:{port}"


def wait_for(cond):
    deadline = time.monotonic() + 2
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.01)
    return cond()


def test_non_ptt_commands_pass_through_unchanged(bench):
    proxy, up = bench.proxy, bench.up
    c = connect(proxy)
    assert ask(c, b"f\n") == b"14074000\n"
    assert ask(c, b"+\\get_freq\n") == b"14074000\n"
    assert up.lines == ["f", "+\\get_freq"]


def test_t1_keys_through_the_ladder_and_t0_unkeys(bench):
    proxy, ptt, rig, up = bench.proxy, bench.ptt, bench.rig, bench.up
    c = connect(proxy)
    assert ask(c, b"T 1\n") == b"RPRT 0\n"
    assert ptt.keyed and ptt.lock.owner == owner_of(c)
    assert ask(c, b"T 0\n") == b"RPRT 0\n"
    assert not ptt.keyed and rig.log == [True, False]
    assert up.lines == []  # rigctld never sees PTT from a desktop app


@pytest.mark.parametrize("line,reply", [
    (b"\\set_ptt 1\n", b"RPRT 0\n"),
    (b"+\\set_ptt 1\n", b"set_ptt: 1\nRPRT 0\n"),
    (b"+T 1\n", b"set_ptt: 1\nRPRT 0\n"),
    (b";\\set_ptt 1\n", b"set_ptt: 1;RPRT 0\n"),
    (b"|T 1\n", b"set_ptt: 1|RPRT 0\n"),
    (b",T 1\n", b"set_ptt: 1,RPRT 0\n"),
    (b"T 9\n", b"RPRT -1\n"),
])
def test_ptt_replies_are_framed_like_rigctld(bench, line, reply):
    proxy = bench.proxy
    assert ask(connect(proxy), line) == reply


def test_one_keying_client_at_a_time_shared_with_the_web_api(bench):
    proxy, ptt = bench.proxy, bench.ptt
    a, b = connect(proxy), connect(proxy)
    assert ask(a, b"T 1\n") == b"RPRT 0\n"
    assert ask(b, b"T 1\n") == b"RPRT -9\n"
    assert ask(a, b"T 0\n") == b"RPRT 0\n"
    assert ptt.key("ipad")  # VectorComm through /api/ladder holds the same lock
    assert ask(b, b"T 1\n") == b"RPRT -9\n"
    assert ptt.lock.owner == "ipad"


def test_ladder_gates_refuse_a_desktop_key(bench):
    proxy, ladder, ptt = bench.proxy, bench.ladder, bench.ptt
    ladder.allowed = False  # transmit checks not passed, open Wi-Fi, flat battery, no receive audio
    assert ask(connect(proxy), b"T 1\n") == b"RPRT -9\n"
    assert not ptt.keyed


def test_disconnect_while_keyed_unkeys(bench):
    proxy, ptt, rig = bench.proxy, bench.ptt, bench.rig
    c = connect(proxy)
    ask(c, b"T 1\n")
    c.close()
    assert wait_for(lambda: not ptt.keyed)
    assert rig.log == [True, False] and ptt.lock.owner is None


def test_a_clients_t0_lets_the_audio_play_out_but_cap_and_disconnect_drop_ptt_at_once(bench):
    proxy, ladder, clock = bench.proxy, bench.ladder, bench.clock
    c = connect(proxy)
    ask(c, b"T 1\n")
    assert ask(c, b"T 0\n") == b"RPRT 0\n"
    ask(c, b"T 1\n")
    clock.t += P.MAX_KEY_S
    proxy.tick()
    ask(c, b"T 0\n")  # clears the latch; nothing keyed, so no drain
    ask(c, b"T 1\n")
    c.close()
    assert wait_for(lambda: len(ladder.unkeys) == 3)
    assert [d for _, d in ladder.unkeys] == [True, False, False]


def test_heartbeats_keep_a_long_slot_alive_under_the_watchdog(bench):
    proxy, ptt, clock = bench.proxy, bench.ptt, bench.clock
    c = connect(proxy)
    ask(c, b"T 1\n")
    for _ in range(6):  # 15 s of FT8 slot, no traffic from the app
        clock.t += 2.5
        proxy.tick()
        assert not ptt.check()
    assert ptt.keyed


def test_cap_unkeys_and_refuses_until_the_client_unkeys(bench):
    proxy, ptt, clock = bench.proxy, bench.ptt, bench.clock
    c = connect(proxy)
    ask(c, b"T 1\n")
    clock.t += P.MAX_KEY_S
    proxy.tick()
    assert not ptt.keyed
    assert ask(c, b"T 1\n") == b"RPRT -9\n"  # a stuck app does not re-key straight away
    assert ask(c, b"T 0\n") == b"RPRT 0\n"
    assert ask(c, b"T 1\n") == b"RPRT 0\n"


def test_key_dropped_by_the_bridge_latches_too(bench):
    proxy, ptt = bench.proxy, bench.ptt
    c = connect(proxy)
    ask(c, b"T 1\n")
    ptt.unkey(owner_of(c))  # watchdog, re-lock or the capped-rig rule
    proxy.tick()
    assert ask(c, b"T 1\n") == b"RPRT -9\n"


@pytest.mark.parametrize("line", [b"f T 1\n", b"f +\\set_ptt 1\n", b"T1\n", b"f T1\n", b"\\send_morse CQ\n",
                                  b"b CQ\n", b"w FE FE\n", b"\\send_raw icom FE\n", b"T\n"])
def test_hidden_or_other_transmit_commands_never_reach_rigctld(bench, line):
    proxy, ptt, up = bench.proxy, bench.ptt, bench.up
    c = connect(proxy)
    assert ask(c, line).startswith(b"RPRT -")
    assert ask(c, b"f\n") == b"14074000\n"
    assert up.lines == ["f"] and not ptt.keyed


def test_stop_unkeys_a_keyed_client(bench):
    proxy, ptt, rig = bench.proxy, bench.ptt, bench.rig
    c = connect(proxy)
    ask(c, b"T 1\n")
    proxy.stop()
    assert not ptt.keyed and rig.log == [True, False]
    c.close()


@pytest.mark.parametrize("error", [RuntimeError("can't start new thread"), MemoryError()])
@pytest.mark.parametrize("which", ["session", "replies"])
def test_a_thread_that_cannot_start_costs_one_connection_not_the_port(bench, monkeypatch, which, error):
    """2026-10-04: "can't start new thread" ended the accept loop and wedged 4532 until a restart."""
    proxy, real, failures = bench.proxy, threading.Thread, [error]

    class Flaky(real):
        def start(self):
            mine = self.name.startswith("rigctl 127.") and self.name.endswith(" up") == (which == "replies")
            if mine and failures:
                raise failures.pop()
            super().start()

    monkeypatch.setattr(P.threading, "Thread", Flaky)
    lost = socket.create_connection(("127.0.0.1", proxy.port), timeout=2)
    assert lost.recv(1) == b""  # closed, not left hanging
    assert wait_for(lambda: not proxy.sessions)  # its slot is free again
    c = connect(proxy)
    assert ask(c, b"f\n") == b"14074000\n"


def test_a_rigctld_connection_that_fails_setup_is_closed_too(bench, monkeypatch):
    proxy, real, made = bench.proxy, socket.create_connection, []

    class Fragile:
        def __init__(self, s):
            self.s, self.closed = s, False

        def settimeout(self, t):
            self.s.settimeout(t)

        def setsockopt(self, *a):
            raise OSError("setsockopt failed")

        def close(self):
            self.closed = True
            self.s.close()

    def create_connection(addr, *a, **kw):
        s = real(addr, *a, **kw)
        if addr[1] != proxy.upstream_port or made:
            return s
        made.append(Fragile(s))
        return made[-1]

    monkeypatch.setattr(P.socket, "create_connection", create_connection)
    lost = real(("127.0.0.1", proxy.port), timeout=2)
    assert lost.recv(1) == b""
    assert made and made[0].closed  # not leaked
    c = connect(proxy)
    assert ask(c, b"f\n") == b"14074000\n"


# -- who can connect (rigctl_access) --

def test_each_access_setting_listens_where_it_says():
    assert P.binds_for("off") == ("127.0.0.1",)
    assert P.binds_for("ap") == ("127.0.0.1", "10.42.0.1")
    assert P.binds_for("wifi") == ("wifi",)
    assert P.binds_for("all") == ("0.0.0.0",)
    assert P.binds_for("bogus") == ("127.0.0.1",)  # unknown: loopback only


def test_wifi_admits_loopback_and_wifi_addresses_only_and_follows_a_switch(bench):
    proxy, addrs = bench.proxy, {"10.42.0.1"}
    proxy.wifi_addrs_fn = lambda: addrs
    assert proxy.rebind(("wifi",)) == [] and proxy.listening == ["0.0.0.0"]
    assert proxy._allows("127.0.0.1") and proxy._allows("127.0.1.1") and proxy._allows("10.42.0.1")
    assert not proxy._allows("100.64.0.10") and not proxy._allows("192.0.2.15")  # tailnet; a LAN not joined
    addrs = {"192.0.2.15"}  # joined home Wi-Fi in place of the AP: no rebind
    assert proxy._allows("192.0.2.15") and not proxy._allows("10.42.0.1")
    assert ask(connect(proxy), b"f\n") == b"14074000\n"  # the Pi's own programs still connect


def test_switching_to_wifi_closes_and_unkeys_a_tailnet_client(bench):
    proxy, ptt = bench.proxy, bench.ptt
    proxy.wifi_addrs_fn = lambda: {"10.42.0.1"}
    proxy.rebind(("0.0.0.0",))  # access all
    c = connect(proxy)
    assert ask(c, b"T 1\n") == b"RPRT 0\n"
    proxy.sessions[0].local = "100.64.0.10"  # as if it had come in over the tailnet
    proxy.rebind(("wifi",))
    assert c.recv(1) == b""
    assert wait_for(lambda: not proxy.sessions)
    assert not ptt.keyed and ptt.lock.owner is None


def test_wifi_addresses_reads_only_wifi_interfaces(tmp_path):
    (tmp_path / "eth0").mkdir()
    (tmp_path / "tailscale0").mkdir()
    (tmp_path / "wlan9").mkdir()
    (tmp_path / "wlan9" / "wireless").mkdir()  # Wi-Fi, but no such interface to hold an address
    assert P.wifi_addresses(tmp_path) == set()
    assert P.wifi_addresses(tmp_path / "missing") == set()


def test_rebind_moves_the_listeners_and_keeps_allowed_clients(bench):
    proxy = bench.proxy
    c = connect(proxy)
    assert proxy.rebind(("0.0.0.0",)) == []
    assert proxy.listening == ["0.0.0.0"]
    assert ask(c, b"f\n") == b"14074000\n"  # came in on loopback, still allowed
    d = socket.create_connection(("127.0.0.1", proxy.port), timeout=2)
    assert wait_for(lambda: len(proxy.sessions) == 2)
    assert ask(d, b"f\n") == b"14074000\n"
    assert proxy.rebind(("127.0.0.1",)) == []
    assert proxy.listening == ["127.0.0.1"] and len(proxy.sessions) == 2
    d.close()


def test_rebind_closes_and_unkeys_a_client_whose_address_is_no_longer_listened_on(bench):
    proxy, ptt = bench.proxy, bench.ptt
    c = connect(proxy)
    assert ask(c, b"T 1\n") == b"RPRT 0\n"
    proxy.sessions[0].local = "10.42.0.1"  # as if it had come in over the access point
    proxy.rebind(("127.0.0.1",))  # access set to off
    assert c.recv(1) == b""
    assert wait_for(lambda: not proxy.sessions)
    assert not ptt.keyed and ptt.lock.owner is None


def test_an_address_that_cannot_be_bound_is_reported_and_the_rest_still_listen(bench, monkeypatch):
    monkeypatch.setattr(P, "IP_FREEBIND", None)  # a Mac, or the AP address with freebind unavailable
    failed = bench.proxy.rebind(("127.0.0.1", "192.0.2.1"))
    assert len(failed) == 1 and failed[0].startswith("192.0.2.1: ")
    assert bench.proxy.listening == ["127.0.0.1"]
    assert ask(connect(bench.proxy), b"f\n") == b"14074000\n"


def test_a_start_that_cannot_listen_can_still_be_rebound_with_its_rules(monkeypatch):
    monkeypatch.setattr(P, "IP_FREEBIND", None)
    clock, up = Clock(), FakeRigctld()
    ptt = PttController(Rig().set_ptt, clock=clock)
    proxy = P.RigctlProxy(FakeLadder(ptt), binds=("192.0.2.1",), port=0, upstream_port=up.port, tick_s=3600,
                          clock=clock, max_key_s=20)
    try:
        with pytest.raises(OSError):
            proxy.start()
        assert proxy.listening == [] and proxy.rebind(("127.0.0.1",)) == []
        c = connect(proxy)
        assert ask(c, b"T 1\n") == b"RPRT 0\n"
        clock.t += 21
        proxy.tick()  # the cap still applies: the tick runs even though the first listen failed
        assert not ptt.keyed
    finally:
        proxy.stop()
        up.close()


@pytest.mark.skipif(P.IP_FREEBIND is None, reason="IP_FREEBIND is Linux only")
def test_the_ap_address_listens_before_the_access_point_is_up(bench):
    assert bench.proxy.rebind(("127.0.0.1", "192.0.2.1")) == []  # not on any interface, like 10.42.0.1 in join mode
    assert bench.proxy.listening == ["127.0.0.1", "192.0.2.1"]


# -- an app token on 4532 under all (roadmap 1.10) --

def test_every_client_but_loopback_needs_a_token_in_every_mode(bench):
    """The maintainer ruled 2026-10-28: the admin PIN guards CAT and PTT for Wi-Fi peers too."""
    proxy = bench.proxy
    proxy.wifi_addrs_fn = lambda: {"192.0.2.15", "10.42.0.1"}
    for binds in (("0.0.0.0",), ("wifi",), ("127.0.0.1", "10.42.0.1"), ("127.0.0.1",)):
        proxy.rebind(binds)
        assert proxy.needs_token("100.64.0.10")  # the tailnet
        assert proxy.needs_token("192.0.2.15") and proxy.needs_token("10.42.0.1", "10.42.0.7")  # Wi-Fi peers
        assert not proxy.needs_token("127.0.0.1") and not proxy.needs_token("127.0.1.1")


@pytest.fixture
def tailnet(bench):
    """Access all, with every client treated as a remote peer (the tests connect over loopback)."""
    bench.tokens = {"good"}
    bench.proxy.token_ok = lambda t: t in bench.tokens
    bench.proxy.needs_token = lambda local, peer="": True
    bench.proxy.rebind(("0.0.0.0",))
    return bench


@pytest.mark.parametrize("first", [b"f\n", b"\\vb_auth bad\n", b"\\vb_auth\n", b"\\vb_auth good extra\n"])
def test_a_tailnet_client_without_a_good_token_is_refused_before_rigctld_sees_anything(tailnet, first):
    c = connect(tailnet.proxy)
    assert ask(c, first) == b"RPRT -9\n"
    assert c.recv(1) == b""
    assert wait_for(lambda: not tailnet.proxy.sessions) and tailnet.up.lines == []


def test_a_tailnet_client_with_an_app_token_tunes_and_keys(tailnet):
    c = connect(tailnet.proxy)
    assert ask(c, b"\\vb_auth good\n") == b"RPRT 0\n"
    assert ask(c, b"f\n") == b"14074000\n" and tailnet.up.lines == ["f"]  # the login line never reaches rigctld
    assert ask(c, b"T 1\n") == b"RPRT 0\n" and tailnet.ptt.keyed


def test_a_revoked_token_closes_and_unkeys_its_client(tailnet):
    proxy, ptt, clock = tailnet.proxy, tailnet.ptt, tailnet.clock
    c = connect(proxy)
    assert ask(c, b"\\vb_auth good\n") == b"RPRT 0\n" and ask(c, b"T 1\n") == b"RPRT 0\n"
    tailnet.tokens.clear()  # revoked, or the PIN changed
    clock.t += P.AUTH_RECHECK_S - 1
    proxy.tick()
    assert ptt.keyed  # not re-checked yet
    clock.t += 1
    proxy.tick()
    assert c.recv(1) == b""
    assert wait_for(lambda: not proxy.sessions) and not ptt.keyed and ptt.lock.owner is None


def test_a_tailnet_client_that_never_logs_in_is_closed(tailnet):
    proxy, clock = tailnet.proxy, tailnet.clock
    c = connect(proxy)
    clock.t += P.AUTH_TIMEOUT_S
    proxy.tick()
    assert c.recv(16) == b"RPRT -9\n" and c.recv(1) == b""
    assert wait_for(lambda: not proxy.sessions)


def test_a_tailnet_peer_reaching_a_wifi_address_is_not_treated_as_wifi(bench):
    proxy = bench.proxy  # the Pi as a Tailscale subnet router: Linux takes the packet on any interface
    proxy.wifi_addrs_fn = lambda: {"10.42.0.1"}
    proxy.rebind(("0.0.0.0",))
    assert proxy.needs_token("10.42.0.1", "100.101.102.103")
    proxy.rebind(("wifi",))
    assert not proxy._allows("10.42.0.1", "100.101.102.103") and proxy._allows("10.42.0.1", "10.42.0.7")
    proxy.rebind(("127.0.0.1", "10.42.0.1"))  # ap
    assert not proxy._allows("10.42.0.1", "100.101.102.103") and proxy._allows("127.0.0.1", "127.0.0.1")


def test_clients_waiting_to_log_in_hold_no_rigctld_connection_and_have_their_own_cap(tailnet):
    proxy = tailnet.proxy
    waiting = [connect(proxy)]
    waiting.append(socket.create_connection(("127.0.0.1", proxy.port), timeout=2))
    assert wait_for(lambda: len(proxy.sessions) == P.MAX_PENDING)
    assert all(s.upstream is None for s in proxy.sessions) and proxy.open_sessions() == 0
    extra = socket.create_connection(("127.0.0.1", proxy.port), timeout=2)
    assert extra.recv(1) == b""  # a third is turned away; the open slots stay free for Wi-Fi apps
    assert ask(waiting[0], b"\\vb_auth good\n") == b"RPRT 0\n" and proxy.open_sessions() == 1
    assert ask(waiting[0], b"f\n") == b"14074000\n"


def test_a_closed_client_cannot_key_again():
    s = P.Session.__new__(P.Session)  # just the PTT state: a pipelined T 1 that arrives behind close()
    s._mu, s._closed, s.keyed = threading.Lock(), True, False
    assert s.ptt(True) == P.ERJCTED and not s.keyed


def test_a_login_line_from_a_client_that_needs_none_never_reaches_rigctld(bench):
    c = connect(bench.proxy)
    assert ask(c, b"\\vb_auth anything\n") == b"RPRT 0\n"
    assert ask(c, b"+\\vb_auth anything\n") == b"RPRT 0\n"
    assert ask(c, b"f\n") == b"14074000\n" and bench.up.lines == ["f"]


def test_key_cap_follows_the_transmit_time_limit_live(bench):
    proxy, ptt, clock = bench.proxy, bench.ptt, bench.clock
    c = connect(proxy)
    ask(c, b"T 1\n")
    proxy.max_key_s = 5.0  # the Transmit Time Limit sheet changed it while this client was keyed
    clock.t += 5
    proxy.tick()
    assert not ptt.keyed
    ask(c, b"T 0\n")
    proxy.max_key_s = 300.0  # raised for WSPR: a 111 s hold is left alone
    ask(c, b"T 1\n")
    clock.t += 111
    proxy.tick()
    assert ptt.keyed


def test_zero_transmit_time_limit_means_no_key_cap(bench):
    proxy, ptt, clock = bench.proxy, bench.ptt, bench.clock
    proxy.max_key_s = 0.0
    c = connect(proxy)
    ask(c, b"T 1\n")
    clock.t += 3600
    proxy.tick()
    assert ptt.keyed
