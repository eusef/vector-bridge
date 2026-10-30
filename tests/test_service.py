"""Full pipeline on the fake backend over localhost UDP for 5 seconds: zero underruns."""

import itertools
import json
import socket
import threading
import time
from datetime import datetime

import numpy as np
import pytest

from vector_bridge.config import Config, load
from vector_bridge.protocol import (
    DIR_RX,
    DIR_TX,
    FRAME_MS,
    FRAME_SAMPLES,
    AudioPacket,
    decode,
)
from vector_bridge.service import BridgeService


@pytest.fixture
def service(tmp_path):
    cfg = Config(audio_port=0, control_port=0)  # ephemeral ports
    svc = BridgeService(cfg, audio="fake", bind="127.0.0.1", config_path=tmp_path / "config.toml")
    svc.start()
    yield svc
    svc.stop()


def control(svc, req: dict) -> dict:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    s.sendto(json.dumps(req).encode(), ("127.0.0.1", svc.control_port))
    return json.loads(s.recv(4096))


def test_pipeline_5s_localhost_zero_underruns(service, tmp_path):
    svc = service
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.settimeout(1)
    audio.bind(("127.0.0.1", 0))
    # register: TX packets alone are enough (hello is the other path, covered by test_control)
    stop = threading.Event()
    sent = {"n": 0}

    def tx_sender():
        t = time.monotonic()
        seq = 0
        tone = (6000 * np.sin(2 * np.pi * 1000 * np.arange(FRAME_SAMPLES) / 48000)).astype(np.int16)
        while not stop.is_set():
            pkt = AudioPacket(DIR_TX, seq, int(t * 1e6), tone)
            audio.sendto(pkt.encode(), ("127.0.0.1", svc.audio_port))
            seq += 1
            sent["n"] += 1
            t += FRAME_MS / 1000
            d = t - time.monotonic()
            if d > 0:
                time.sleep(d)

    th = threading.Thread(target=tx_sender, daemon=True)
    th.start()

    rx_seqs = []
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        data, _ = audio.recvfrom(4096)
        pkt = decode(data)
        assert pkt.direction == DIR_RX and len(pkt.pcm) == FRAME_SAMPLES
        rx_seqs.append(pkt.sequence)
    st = control(svc, {"cmd": "status"})  # while TX still flows: the drain after stop is not an underrun
    stop.set()
    th.join()

    assert st["ok"] and st["peer"] is not None
    assert st["tx_underruns"] == 0, st
    assert st["rx_overruns"] == 0, st
    assert st["tx_packet_loss"] == 0, st
    assert abs(st["tx_packets_received"] - sent["n"]) <= 2
    assert st["rx_packets_sent"] >= 450 and len(rx_seqs) >= 450  # ~500 expected in 5 s
    assert rx_seqs == list(range(rx_seqs[0], rx_seqs[0] + len(rx_seqs)))  # no RX loss on localhost
    assert st["rx_peak"] > 7000 and st["tx_peak"] > 5000  # levels measured on both directions
    assert svc.backend.frames_written >= 450


def test_set_gain_persists_to_config(service, tmp_path):
    reply = control(service, {"cmd": "set_gain", "tx": 33, "rx": 66})
    assert reply == {"ok": True, "tx": 33, "rx": 66}
    saved = load(tmp_path / "config.toml")
    assert (saved.tx_gain, saved.rx_gain) == (33, 66)


def test_status_reports_the_applied_gains(service):
    """The tuner shows the current levels from status; without these it cannot."""
    control(service, {"cmd": "set_gain", "tx": 41, "rx": 77})
    st = control(service, {"cmd": "status"})
    assert (st["tx_gain"], st["rx_gain"]) == (41, 77)


def test_status_reports_the_tx_audio_still_queued_for_the_ptt_tail(service, monkeypatch):
    monkeypatch.setattr("vector_bridge.audio.fake.FakeBackend.playback_buffer_ms", property(lambda self: 40))
    service.jitter.stats.depth = 10  # 100 ms of packets waiting
    assert service.status()["tx_queued_ms"] == 140


@pytest.mark.parametrize("fixed", [True, False])
def test_status_says_when_the_card_has_no_receive_level(tmp_path, monkeypatch, fixed):
    """IC-705 codec: no capture control, so the pages point at the radio instead of the slider."""
    monkeypatch.setattr("vector_bridge.audio.fake.FakeBackend.rx_fixed", property(lambda self: fixed))
    svc = BridgeService(Config(audio_port=0, control_port=0), audio="fake", bind="127.0.0.1",
                        config_path=tmp_path / "config.toml")
    svc.start()
    try:
        assert control(svc, {"cmd": "status"})["rx_gain_fixed"] is fixed
    finally:
        svc.stop()


def test_tx_audio_cap_persists_and_mutes_a_held_tone(service, tmp_path):
    """A VOX rig keys on audio, so audio that never pauses must be cut (txcap.py)."""
    assert control(service, {"cmd": "set_tx_audio_cap", "seconds": 1}) == {"ok": True, "tx_audio_cap_s": 1}
    assert load(tmp_path / "config.toml").tx_audio_cap_s == 1
    assert control(service, {"cmd": "tone", "hz": 1500, "seconds": 5})["ok"]
    # the 5 s tone is still playing, so silence here can only be the cap
    assert _wait_for(lambda: service.tx_cap.muted and int(np.abs(service.backend.last_written).max()) == 0,
                     timeout=3.0)
    st = control(service, {"cmd": "status"})
    assert (st["tx_audio_cap_s"], st["tx_audio_muted"], st["tx_audio_cap_trips"]) == (1, True, 1)
    control(service, {"cmd": "tone", "seconds": 0})


def test_peer_dropped_after_silence(service, monkeypatch):
    import vector_bridge.service as sm

    monkeypatch.setattr(sm, "PEER_TIMEOUT_S", 0.3)
    control(service, {"cmd": "hello", "client": "t"})
    assert control(service, {"cmd": "status"})["peer"] is not None
    time.sleep(0.8)
    assert control(service, {"cmd": "status"})["peer"] is None


def _reconnect_attempts(tmp_path, monkeypatch, healthy_s: float) -> list[int]:
    """Run a bridge whose capture fails on every read; return the reconnect attempt numbers."""
    import vector_bridge.service as sm
    from vector_bridge.audio import AudioDeviceError

    monkeypatch.setattr(sm, "REOPEN_BACKOFF", (0.01, 0.02, 0.03, 0.04))
    monkeypatch.setattr(sm, "HEALTHY_S", healthy_s)
    attempts: list[int] = []
    real_log = sm.log
    monkeypatch.setattr(sm, "log", lambda event, **kw: (attempts.append(kw["attempt"])
                                                         if event == "device_reconnect"
                                                         else real_log(event, **kw)))
    svc = BridgeService(Config(audio_port=0, control_port=0), audio="fake", bind="127.0.0.1",
                        config_path=tmp_path / "config.toml")

    def dead_read():
        time.sleep(0.001)
        raise AudioDeviceError("capture read: stream will not start")

    svc.backend.read = dead_read
    svc.start()
    deadline = time.monotonic() + 3
    while len(attempts) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    svc.stop()
    return attempts[:3]


def test_instant_refailure_climbs_the_backoff(tmp_path, monkeypatch):
    assert _reconnect_attempts(tmp_path, monkeypatch, healthy_s=10.0) == [1, 2, 3]


def test_backoff_starts_over_after_a_healthy_run(tmp_path, monkeypatch):
    assert _reconnect_attempts(tmp_path, monkeypatch, healthy_s=0.0) == [1, 1, 1]


class _SlowSendSock:
    """The service's audio socket, with one RX sendto() held for 60 ms."""

    def __init__(self, sock):
        self._sock, self.slow = sock, True

    def sendto(self, data, *args):
        if self.slow:
            self.slow = False
            time.sleep(0.06)
        return self._sock.sendto(data, *args)

    def __getattr__(self, name):
        return getattr(self._sock, name)


@pytest.mark.parametrize("step", ["read", "sendto"])
def test_a_capture_step_longer_than_the_buffer_is_named(tmp_path, monkeypatch, step):
    import vector_bridge.service as sm

    stalls: list[dict] = []
    real_warn = sm.warn
    monkeypatch.setattr(sm, "warn", lambda event, **kw: (stalls.append(kw) if event == "capture_stall"
                                                         else real_warn(event, **kw)))
    svc = BridgeService(Config(audio_port=0, control_port=0), audio="fake", bind="127.0.0.1",
                        config_path=tmp_path / "config.toml")
    if step == "read":
        real_read, slow = svc.backend.read, [True]

        def slow_read():
            if slow:
                slow.pop()
                time.sleep(0.06)
            return real_read()

        svc.backend.read = slow_read
    else:
        svc.audio_sock = _SlowSendSock(svc.audio_sock)
        svc._register_peer(("127.0.0.1", 9))
    svc.start()
    try:
        assert _wait_for(lambda: stalls)
        time.sleep(0.2)  # normal reads and sends after the one slow call stay quiet
    finally:
        svc.stop()
    assert [s["step"] for s in stalls] == [step], stalls
    assert stalls[0]["ms"] >= 60
    assert stalls[0]["every"] == 1.0  # a persistently slow device logs once a second, not 100 times


class _FullQueueSock:
    """The service's audio socket, with the send queue to one destination full: a blocking
    send waits 300 ms there, a non-blocking one is refused."""

    def __init__(self, sock, stuck_port):
        self._sock, self.stuck_port = sock, stuck_port

    def sendto(self, data, *args):
        flags, dest = args if len(args) == 2 else (0, args[0])
        if dest[1] == self.stuck_port:
            if flags & socket.MSG_DONTWAIT:
                raise BlockingIOError(11, "Resource temporarily unavailable")
            time.sleep(0.3)
        return self._sock.sendto(data, flags, dest)

    def __getattr__(self, name):
        return getattr(self._sock, name)


def test_a_full_send_queue_drops_that_clients_packet_instead_of_stalling_capture(tmp_path, monkeypatch):
    import vector_bridge.service as sm

    stalls: list[dict] = []
    real_warn = sm.warn
    monkeypatch.setattr(sm, "warn", lambda event, **kw: (stalls.append(kw) if event == "capture_stall"
                                                         else real_warn(event, **kw)))
    svc = BridgeService(Config(audio_port=0, control_port=0), audio="fake", bind="127.0.0.1",
                        config_path=tmp_path / "config.toml")
    peer, stuck = _udp(), _udp()
    svc.audio_sock = _FullQueueSock(svc.audio_sock, stuck.getsockname()[1])
    svc._register_peer(peer.getsockname())
    svc._monitors[stuck.getsockname()] = time.monotonic()
    svc.start()
    try:
        assert len(_rx_seqs(peer, 30)) == 30  # the healthy client keeps getting audio
        # every packet for the stuck client is dropped and counted, not waited on
        assert _wait_for(lambda: svc.status()["rx_packet_loss"] >= 30)
    finally:
        svc.stop()
    assert stalls == []


def _burst_service(tmp_path, monkeypatch):
    """A service whose audio socket refuses sends to one monitor until `sock.stuck_port` is cleared,
    and the rx_send_burst lines it logs."""
    import vector_bridge.service as sm

    bursts: list[dict] = []
    real_warn = sm.warn
    monkeypatch.setattr(sm, "warn", lambda event, **kw: (bursts.append(kw) if event == "rx_send_burst"
                                                         else real_warn(event, **kw)))
    svc = BridgeService(Config(audio_port=0, control_port=0), audio="fake", bind="127.0.0.1",
                        config_path=tmp_path / "config.toml")
    peer, stuck = _udp(), _udp()
    svc.audio_sock = _FullQueueSock(svc.audio_sock, stuck.getsockname()[1])
    svc._register_peer(peer.getsockname())
    svc._monitors[stuck.getsockname()] = time.monotonic()
    return svc, stuck, bursts


def test_a_run_of_failed_sends_is_logged_once_with_its_span_and_count(tmp_path, monkeypatch):
    svc, stuck, bursts = _burst_service(tmp_path, monkeypatch)
    t_start = time.time()
    svc.start()
    try:
        assert _wait_for(lambda: svc.status()["rx_packet_loss"] >= 20)
        assert bursts == []  # still failing: the run is open
        loss = svc.status()["rx_packet_loss"]
        svc.audio_sock.stuck_port = None  # the stall clears
        t_clear = time.time()
        assert _wait_for(lambda: bursts)
        time.sleep(0.1)  # later good sends add nothing
        loss_total = svc.status()["rx_packet_loss"]
    finally:
        svc.stop()
    assert len(bursts) == 1, bursts
    b = bursts[0]
    assert b["dest"] == f"127.0.0.1:{stuck.getsockname()[1]}"
    assert loss <= b["failed"] == loss_total  # the peer never failed, so every loss is this run
    first, last = (datetime.fromisoformat(b[k]).timestamp() for k in ("first", "last"))
    assert t_start - 0.01 <= first < last <= t_clear + 0.05


def test_a_flapping_link_logs_one_run_not_one_per_good_packet(tmp_path, monkeypatch):
    svc, _stuck, bursts = _burst_service(tmp_path, monkeypatch)
    port = svc.audio_sock.stuck_port
    svc.start()
    try:
        for _ in range(10):  # 50 ms refused, 50 ms through, ten times
            assert _wait_for(lambda: svc.status()["rx_packet_loss"] > 0)
            time.sleep(0.05)
            svc.audio_sock.stuck_port = None
            time.sleep(0.05)
            svc.audio_sock.stuck_port = port
        svc.audio_sock.stuck_port = None
        assert _wait_for(lambda: bursts)
    finally:
        svc.stop()
    assert len(bursts) == 1, bursts
    assert bursts[0]["failed"] == svc.status()["rx_packet_loss"]


def test_a_destination_that_leaves_mid_run_closes_its_run(tmp_path, monkeypatch):
    svc, stuck, bursts = _burst_service(tmp_path, monkeypatch)
    svc.start()
    try:
        assert _wait_for(lambda: svc.status()["rx_packet_loss"] >= 5)
        with svc._peer_lock:
            del svc._monitors[stuck.getsockname()]  # still stuck, but no longer sent to
        assert _wait_for(lambda: bursts)
    finally:
        svc.stop()
    assert [b["dest"] for b in bursts] == [f"127.0.0.1:{stuck.getsockname()[1]}"]


def test_capture_clock_counts_the_frames_an_overrun_lost():
    from vector_bridge.service import CaptureClock

    clock, t = CaptureClock(), 100.0
    for i in range(300):  # reads return on the 10 ms grid, a few ms late at random
        assert clock.frame(t + i * 0.010 + (i % 7) * 0.0005, overran=False) == 0
    t += 300 * 0.010
    assert clock.frame(t + 0.200, overran=True) == 20  # 200 ms of audio thrown away
    assert clock.frame(t + 0.210, overran=False) == 0  # and back on the grid after it
    assert clock.frame(t + 0.220, overran=True) == 0  # an overrun that lost nothing skips nothing


def test_capture_clock_ignores_drift_between_the_codec_and_the_pi():
    from vector_bridge.service import CaptureClock

    clock, period = CaptureClock(), 0.010 * (1 + 50e-6)  # codec 50 ppm slow: 30 ms over 10 min
    n = 60_000
    for i in range(n):
        clock.frame(i * period, overran=False)
    assert clock.frame(n * period, overran=True) == 0


def test_an_overrun_skips_the_lost_sequence_numbers(tmp_path, monkeypatch):
    svc = BridgeService(Config(audio_port=0, control_port=0), audio="fake", bind="127.0.0.1",
                        config_path=tmp_path / "config.toml")
    fake, overruns, reads = svc.backend, [0], [0]
    monkeypatch.setattr(type(fake), "overruns", property(lambda self: overruns[0]))
    real_read = fake.read

    def read():
        reads[0] += 1
        if reads[0] == 50:  # stall 200 ms; the overrun throws that audio away, so no catch-up burst
            time.sleep(0.2)
            fake._next["read"] = time.monotonic() - FRAME_MS / 1000
            overruns[0] += 1
        return real_read()

    fake.read = read
    peer = _udp()
    svc._register_peer(peer.getsockname())
    svc.start()
    got: list[tuple[int, int]] = []
    try:
        while len(got) < 90:
            data, _ = peer.recvfrom(4096)
            p = decode(data)
            got.append((p.sequence, p.timestamp_us))
    finally:
        svc.stop()
    jumps = [(b[0] - a[0], b[1] - a[1]) for a, b in itertools.pairwise(got) if b[0] - a[0] > 1]
    assert len(jumps) == 1, jumps
    seq_jump, us = jumps[0]
    assert 17 <= seq_jump <= 23  # about 19 frames lost, plus the one that arrived
    assert abs(us / 1000 - seq_jump * FRAME_MS) <= 15  # the timestamp moved as far as the sequence


def test_a_device_reopen_gap_is_not_counted_as_overrun_loss(tmp_path, monkeypatch):
    import vector_bridge.service as sm
    from vector_bridge.audio import AudioDeviceError

    monkeypatch.setattr(sm, "REOPEN_BACKOFF", (0.6,))  # over the fake's 0.5 s catch-up limit: no burst
    svc = BridgeService(Config(audio_port=0, control_port=0), audio="fake", bind="127.0.0.1",
                        config_path=tmp_path / "config.toml")
    fake, overruns, reads = svc.backend, [0], [0]
    monkeypatch.setattr(type(fake), "overruns", property(lambda self: overruns[0]))
    real_read = fake.read

    def read():
        reads[0] += 1
        if reads[0] == 30:
            raise AudioDeviceError("capture read: card node gone")
        if reads[0] == 50:  # soon after the reopen, an overrun that lost nothing
            overruns[0] += 1
        return real_read()

    fake.read = read
    peer = _udp()
    svc._register_peer(peer.getsockname())
    svc.start()
    seqs: list[int] = []
    try:
        while len(seqs) < 70:
            data, _ = peer.recvfrom(4096)
            seqs.append(decode(data).sequence)
    finally:
        svc.stop()
    assert overruns[0] == 1 and reads[0] > 50
    assert [b - a for a, b in itertools.pairwise(seqs)] == [1] * 69  # the 0.6 s gap skipped nothing


def test_select_audio_saves_the_card_and_reopens_on_it(service, tmp_path):
    assert _wait_for(lambda: control(service, {"cmd": "status"})["audio_device"]["ok"])
    req = {"cmd": "select_audio", "vendor": "0d8c", "product": "0014", "usb_path": "1-1.4.2", "strict": True}
    assert control(service, req) == {"ok": True, "changed": True}
    saved = load(tmp_path / "config.toml")
    assert (saved.usb_vendor_id, saved.usb_product_id, saved.audio_usb_path) == ("0d8c", "0014", "1-1.4.2")
    assert saved.audio_usb_strict is True
    assert _wait_for(lambda: control(service, {"cmd": "status"})["device_reopens"] == 1, timeout=3)
    assert _wait_for(lambda: control(service, {"cmd": "status"})["audio_device"]["ok"])
    assert control(service, req) == {"ok": True, "changed": False}  # same card, already open: no reopen
    time.sleep(0.7)
    assert control(service, {"cmd": "status"})["device_reopens"] == 1


def test_rigctl_access_is_saved_for_the_tuner(service, tmp_path):
    assert control(service, {"cmd": "set_rigctl_access", "access": "off"}) == {"ok": True, "rigctl_access": "off"}
    assert load(tmp_path / "config.toml").rigctl_access == "off"
    control(service, {"cmd": "set_gain", "rx": 40})  # a later save of the whole file keeps it
    assert load(tmp_path / "config.toml").rigctl_access == "off"


def test_rigctl_access_that_cannot_be_saved_is_refused_and_unchanged(service, tmp_path, monkeypatch):
    import vector_bridge.service as sm

    def fail(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(sm.configmod, "save", fail)
    assert control(service, {"cmd": "set_rigctl_access", "access": "all"}) == {"ok": False, "error": "config_save_failed"}
    assert service.cfg.rigctl_access == "wifi"  # the default; a later gain save cannot write it either


def test_a_card_chosen_while_another_is_opening_is_opened_next(tmp_path):
    svc = BridgeService(Config(audio_port=0, control_port=0), audio="fake", bind="127.0.0.1",
                        config_path=tmp_path / "config.toml")
    real_open, opened = svc.backend.open, []

    def open_():  # setup picks a card while the first open is still running
        if not opened:
            svc.select_audio("0d8c", "0014", "1-1.3")
        opened.append(svc.cfg.audio_usb_path)
        real_open()

    svc.backend.open = open_
    svc.start()
    try:
        assert _wait_for(lambda: len(opened) >= 2 and svc._device_ok.is_set(), timeout=3)
    finally:
        svc.stop()
    assert opened[-1] == "1-1.3" and svc.telemetry.as_dict()["device_reopens"] >= 1


def test_new_peer_resets_the_tx_jitter_buffer(service):
    service.jitter.push(AudioPacket(DIR_TX, 100, 0, np.zeros(FRAME_SAMPLES, dtype=np.int16)))
    service._register_peer(("10.0.0.9", 4000))
    service.jitter.push(AudioPacket(DIR_TX, 50, 0, np.zeros(FRAME_SAMPLES, dtype=np.int16)))
    assert service.jitter.stats.late == 0


def test_keepalive_registers_the_peer_without_audio(service):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(AudioPacket(DIR_TX, 0, 0, np.zeros(0, dtype=np.int16)).encode(), ("127.0.0.1", service.audio_port))
    time.sleep(0.3)
    st = control(service, {"cmd": "status"})
    assert st["peer"] == f"127.0.0.1:{s.getsockname()[1]}"
    assert st["tx_packets_received"] == 0


def test_same_peer_again_keeps_the_tx_jitter_buffer(service):
    service._register_peer(("10.0.0.9", 4000))
    service.jitter.push(AudioPacket(DIR_TX, 100, 0, np.zeros(FRAME_SAMPLES, dtype=np.int16)))
    service._register_peer(("10.0.0.9", 4000))
    assert service.jitter.stats.depth == 1


def _wait_for(pred, timeout=2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_tone_plays_in_place_of_client_audio_and_stops(service):
    """The ladder's drive and carrier checks need a tone with no client connected (HANDOFF D8)."""
    def peak() -> int:
        pcm = service.backend.last_written
        return -1 if pcm is None else int(np.abs(pcm).max())

    assert control(service, {"cmd": "tone", "hz": 1500, "seconds": 5}) == {"ok": True, "tone_hz": 1500.0, "tone_s": 5.0}
    assert _wait_for(lambda: peak() > 20000)
    assert control(service, {"cmd": "tone", "seconds": 0})["ok"]
    assert _wait_for(lambda: peak() == 0)


def _udp() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(1)
    return s


def _ask(svc, sock: socket.socket, req: dict) -> dict:
    """A control request sent from `sock`, so the bridge sees that socket's address."""
    sock.sendto(json.dumps(req).encode(), ("127.0.0.1", svc.control_port))
    while True:
        data, addr = sock.recvfrom(4096)
        if addr[1] == svc.control_port:
            return json.loads(data)


def _rx_seqs(sock: socket.socket, n: int) -> set[int]:
    seqs = set()
    while len(seqs) < n:
        data, _ = sock.recvfrom(4096)
        if data[:4] == b"VBRG":
            seqs.add(decode(data).sequence)
    return seqs


def _keepalive(svc, sock: socket.socket) -> None:
    sock.sendto(AudioPacket(DIR_TX, 0, 0, np.zeros(0, dtype=np.int16)).encode(), ("127.0.0.1", svc.audio_port))


def test_monitor_gets_the_peers_rx_stream_without_taking_its_slot(service):
    peer, mon = _udp(), _udp()
    _keepalive(service, peer)
    assert _wait_for(lambda: service._current_peer() is not None)
    reply = _ask(service, mon, {"cmd": "monitor", "client": "js8call"})
    assert reply["ok"] and reply["role"] == "monitor"
    a, b = _rx_seqs(peer, 50), _rx_seqs(mon, 50)
    assert len(a & b) >= 25  # the same packets, same sequence numbers, reach both
    st = control(service, {"cmd": "status"})
    assert st["peer"] == f"127.0.0.1:{peer.getsockname()[1]}"
    assert st["monitors"] == [f"127.0.0.1:{mon.getsockname()[1]}"]


def test_monitor_tx_audio_never_reaches_playback(service):
    peer, mon = _udp(), _udp()
    _keepalive(service, peer)
    assert _ask(service, mon, {"cmd": "monitor"})["ok"]
    loud = np.full(FRAME_SAMPLES, 20000, dtype=np.int16)
    for seq in range(20):
        mon.sendto(AudioPacket(DIR_TX, seq, 0, loud).encode(), ("127.0.0.1", service.audio_port))
    _keepalive(service, mon)
    time.sleep(0.3)
    st = control(service, {"cmd": "status"})
    assert st["tx_packets_received"] == 0
    assert service.jitter.stats.depth == 0
    assert st["peer"] == f"127.0.0.1:{peer.getsockname()[1]}"  # nor did it take the peer slot
    assert int(np.abs(service.backend.last_written).max()) == 0


def test_monitor_refusals_and_hello_promotes_a_monitor(service, monkeypatch):
    import vector_bridge.service as sm

    peer, m1, m2, m3 = _udp(), _udp(), _udp(), _udp()
    assert _ask(service, peer, {"cmd": "hello"})["ok"]
    assert _ask(service, peer, {"cmd": "monitor"}) == {"ok": False, "error": "is_peer"}
    assert _ask(service, m1, {"cmd": "monitor"})["ok"]
    assert _ask(service, m2, {"cmd": "monitor"})["ok"]
    assert sm.MAX_MONITORS == 2
    assert _ask(service, m3, {"cmd": "monitor"}) == {"ok": False, "error": "monitors_full"}
    assert _ask(service, m1, {"cmd": "monitor"})["ok"]  # a refresh is not a new slot
    assert _ask(service, m1, {"cmd": "hello"})["ok"]
    st = control(service, {"cmd": "status"})
    assert st["peer"] == f"127.0.0.1:{m1.getsockname()[1]}"
    assert st["monitors"] == [f"127.0.0.1:{m2.getsockname()[1]}"]


def test_monitor_dropped_after_silence(service, monkeypatch):
    import vector_bridge.service as sm

    monkeypatch.setattr(sm, "PEER_TIMEOUT_S", 0.3)
    mon = _udp()
    assert _ask(service, mon, {"cmd": "monitor"})["ok"]
    assert control(service, {"cmd": "status"})["monitors"]
    assert _wait_for(lambda: control(service, {"cmd": "status"})["monitors"] == [])


def test_status_labels_each_live_stream_with_its_client(service):
    # rule 1: a monitor's own hello/monitor; rule 2: a peer whose hello came from another port on its IP
    mon, ctl, audio = _udp(), _udp(), _udp()
    assert _ask(service, mon, {"cmd": "monitor", "client": "vector-swl-radio"})["ok"]
    assert _ask(service, ctl, {"cmd": "hello", "client": "vector-comm"})["ok"]
    _keepalive(service, audio)  # the stream moves to the audio socket, as VectorComm does
    assert _wait_for(lambda: control(service, {"cmd": "status"})["peer"] == f"127.0.0.1:{audio.getsockname()[1]}")
    clients = control(service, {"cmd": "status"})["clients"]
    assert sorted(clients, key=lambda c: c["role"]) == [
        {"addr": f"127.0.0.1:{mon.getsockname()[1]}", "role": "monitor", "client": "vector-swl-radio"},
        {"addr": f"127.0.0.1:{audio.getsockname()[1]}", "role": "peer", "client": "vector-comm"}]


def test_a_refused_monitor_does_not_relabel_a_stream(service):
    m1, m2, m3, ctl, audio = _udp(), _udp(), _udp(), _udp(), _udp()
    assert _ask(service, m1, {"cmd": "monitor", "client": "mon"})["ok"]
    assert _ask(service, m2, {"cmd": "monitor", "client": "mon"})["ok"]
    assert _ask(service, ctl, {"cmd": "hello", "client": "vector-comm"})["ok"]
    assert _ask(service, m3, {"cmd": "monitor", "client": "refused"}) == {"ok": False, "error": "monitors_full"}
    _keepalive(service, audio)  # labelled by rule 2: the newest accepted name on its IP
    want = f"127.0.0.1:{audio.getsockname()[1]}"
    assert _wait_for(lambda: control(service, {"cmd": "status"})["peer"] == want)
    peer = [c for c in control(service, {"cmd": "status"})["clients"] if c["role"] == "peer"]
    assert peer == [{"addr": want, "role": "peer", "client": "vector-comm"}]


def test_status_clients_unnamed_without_hello_and_gone_after_silence(service, monkeypatch):
    import vector_bridge.service as sm

    service._register_peer(("10.0.0.9", 4000))  # TX audio alone: no hello from that IP
    assert control(service, {"cmd": "status"})["clients"] == [{"addr": "10.0.0.9:4000", "role": "peer", "client": ""}]
    monkeypatch.setattr(sm, "PEER_TIMEOUT_S", 0.2)
    time.sleep(0.3)
    assert control(service, {"cmd": "status"})["clients"] == []


def test_status_reports_a_healthy_audio_device(service):
    dev = control(service, {"cmd": "status"})["audio_device"]
    assert dev["ok"] is True and dev["error"] is None and dev["since"] <= time.time()


def test_status_reports_a_missing_audio_device_until_it_returns(tmp_path, monkeypatch):
    """A card that is not there at boot must not read as a quiet band: status names the failure."""
    import vector_bridge.service as sm
    from vector_bridge.audio import AudioDeviceError

    monkeypatch.setattr(sm, "REOPEN_BACKOFF", (0.05,))
    svc = BridgeService(Config(audio_port=0, control_port=0), audio="fake", bind="127.0.0.1",
                        config_path=tmp_path / "config.toml")
    real_open, present = svc.backend.open, threading.Event()

    def flaky_open():
        if not present.is_set():
            raise AudioDeviceError("open hw:0: No such file or directory")
        real_open()

    svc.backend.open = flaky_open
    svc.start()
    try:
        dev = control(svc, {"cmd": "status"})["audio_device"]
        assert dev["ok"] is False and "No such file" in dev["error"]
        present.set()
        assert _wait_for(lambda: control(svc, {"cmd": "status"})["audio_device"]["ok"])
        back = control(svc, {"cmd": "status"})["audio_device"]
        assert back["error"] is None and back["since"] >= dev["since"]
    finally:
        svc.stop()


def _soft_service(tmp_path, monkeypatch, tx_gain):
    """A service whose card has no playback volume (QMX), so tx_gain scales the samples."""
    monkeypatch.setattr("vector_bridge.audio.fake.FakeBackend.tx_soft", property(lambda self: True))
    svc = BridgeService(Config(audio_port=0, control_port=0, tx_gain=tx_gain), audio="fake", bind="127.0.0.1",
                        config_path=tmp_path / "config.toml")
    svc.start()
    return svc


def _send_tone(svc, peak: int, frames: int, s: socket.socket | None = None, seq0: int = 0) -> None:
    """The app's TX: a tone at `peak` (the app sends 0.5 FS, 16384), paced like the client. 1510 Hz, not
    1500: a 1500 Hz tone lands exactly on its peak 30 times a frame, which measure() calls a flat top."""
    s = s or _udp()
    t = np.arange(FRAME_SAMPLES * frames)
    pcm = np.rint(peak * np.sin(2 * np.pi * 1510 * t / 48000)).astype(np.int16)
    for seq in range(frames):
        s.sendto(AudioPacket(DIR_TX, seq0 + seq, 0, pcm[seq * FRAME_SAMPLES:(seq + 1) * FRAME_SAMPLES]).encode(),
                 ("127.0.0.1", svc.audio_port))
        time.sleep(FRAME_MS / 1000)


def _events(out: str, event: str) -> list[dict]:
    return [dict(f.split("=", 1) for f in ln.split() if "=" in f) for ln in out.splitlines() if f"event={event} " in ln]


@pytest.mark.parametrize("tx_gain,pct,clips", [(75, "50.0", "0"), (88, "91.0", "0"), (100, "100.0", None), (0, "0.0", "0")])
def test_soft_gain_scales_the_app_tone_and_logs_one_tx_send(tmp_path, monkeypatch, capsys, tx_gain, pct, clips):
    """The QMX keys RF only past 0.8 FS. 75 is unity, 88 is +5.2 dB (0.91 FS), 100 saturates, 0 is silent."""
    svc = _soft_service(tmp_path, monkeypatch, tx_gain)
    try:
        assert control(svc, {"cmd": "status"})["tx_soft_gain"] is True
        _send_tone(svc, 16384, 100)
        out = []
        assert _wait_for(lambda: out.append(capsys.readouterr().out) or "event=tx_send " in "".join(out), 3.0)
        time.sleep(0.3)
    finally:
        svc.stop()
    sends = _events("".join(out) + capsys.readouterr().out, "tx_send")
    assert len(sends) == 1 and sends[0]["soft"] == "True"
    assert abs(float(sends[0]["peak_pct_fs"]) - float(pct)) <= 0.1
    if clips is None:
        assert int(sends[0]["clips"]) > 0 and int(sends[0]["peak"]) >= 32767  # saturated, not wrapped
    else:
        assert sends[0]["clips"] == clips


@pytest.mark.parametrize("soft,tx_gain,peak", [(True, 88, 29490), (True, 50, 9326), (False, 88, 29490)])
def test_the_tune_tone_is_never_boosted(tmp_path, monkeypatch, soft, tx_gain, peak):
    """The tone already plays at 0.9 FS: soft gain may only attenuate it; hardware-mixer cards are untouched."""
    if soft:
        monkeypatch.setattr("vector_bridge.audio.fake.FakeBackend.tx_soft", property(lambda self: True))
    svc = BridgeService(Config(audio_port=0, control_port=0, tx_gain=tx_gain), audio="fake", bind="127.0.0.1",
                        config_path=tmp_path / "config.toml")
    svc.start()
    try:
        assert control(svc, {"cmd": "tone", "hz": 1500, "seconds": 2})["ok"]
        seen = [0]
        def loudest() -> bool:
            pcm = svc.backend.last_written
            seen.append(0 if pcm is None else int(np.abs(pcm).max()))
            return max(seen) >= peak - 1
        assert _wait_for(loudest)
        assert max(seen) <= peak
    finally:
        svc.stop()


def test_hardware_mixer_cards_play_client_audio_unscaled(service):
    peer = _udp()
    loud = np.full(FRAME_SAMPLES, 20000, dtype=np.int16)
    service.cfg.tx_gain = 0  # a hardware-mixer card: tx_gain sets the mixer, never the samples
    for seq in range(20):
        peer.sendto(AudioPacket(DIR_TX, seq, 0, loud).encode(), ("127.0.0.1", service.audio_port))
    assert _wait_for(lambda: service.backend.last_written is not None
                     and int(np.abs(service.backend.last_written).max()) == 20000)


@pytest.mark.parametrize("bad", [-2147483648, 2147483647, 101, True])
def test_an_out_of_range_tx_gain_falls_back_to_the_default(tmp_path, capsys, bad):
    """INT32 garbage from the old switch-only mixer read must not clamp to 100 (+10 dB on a soft card)."""
    svc = BridgeService(Config(audio_port=0, control_port=0, tx_gain=bad), audio="fake", bind="127.0.0.1",
                        config_path=tmp_path / "config.toml")
    assert svc.cfg.tx_gain == Config().tx_gain == 20
    assert svc.backend.set_gain(rx=40)["tx"] == 20  # the backend never saw the bad value
    assert _events(capsys.readouterr().out, "bad_tx_gain")


@pytest.mark.parametrize("tx_gain,muted", [(0, True), (88, False)])
def test_a_soft_card_says_so_at_open_and_warns_when_muted(tmp_path, monkeypatch, capsys, tx_gain, muted):
    svc = _soft_service(tmp_path, monkeypatch, tx_gain)
    svc.stop()
    out = capsys.readouterr().out
    on = _events(out, "tx_soft_gain")
    assert len(on) == 1 and on[0]["tx_gain"] == str(tx_gain)
    assert bool(_events(out, "tx_soft_gain_muted")) is muted


def test_a_gap_mid_send_does_not_split_the_tx_send(tmp_path, monkeypatch, capsys):
    """A 300 ms Wi-Fi gap drains the jitter buffer (an underrun) but is one send, not two."""
    svc = _soft_service(tmp_path, monkeypatch, 75)
    try:
        peer = _udp()
        _send_tone(svc, 16384, 50, peer)
        assert _wait_for(lambda: svc.jitter.stats.underruns >= 1, 2.0)
        time.sleep(0.3)
        _send_tone(svc, 16384, 50, peer, seq0=50)  # same sender, sequence carries on
        out = []
        assert _wait_for(lambda: out.append(capsys.readouterr().out) or "event=tx_send " in "".join(out), 3.0)
        time.sleep(0.3)
    finally:
        svc.stop()
    sends = _events("".join(out) + capsys.readouterr().out, "tx_send")
    assert len(sends) == 1 and int(sends[0]["frames"]) >= 100
