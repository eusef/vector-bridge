"""bridge_client is a standalone tool (tools/.venv); these tests stub sounddevice."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

SRC = Path(__file__).resolve().parents[1] / "tools" / "bridge_client.py"


@pytest.fixture
def client(monkeypatch):
    spec = importlib.util.spec_from_file_location("bridge_client", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_sd(monkeypatch):
    calls = {}

    class Stream:
        def __init__(self, **kw):
            calls.update(kw)

        def start(self):
            calls["started"] = True

    sd = types.SimpleNamespace(OutputStream=Stream, InputStream=Stream)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    return calls


def test_player_opens_the_requested_output_device(client, fake_sd):
    client.Player(client.Stats(), enabled=True, device="VectorBridge")
    assert fake_sd["device"] == "VectorBridge"
    assert fake_sd["started"] is True


def test_player_defaults_to_the_system_output(client, fake_sd):
    client.Player(client.Stats(), enabled=True)
    assert fake_sd["device"] is None


def test_disabled_player_opens_nothing(client, fake_sd):
    client.Player(client.Stats(), enabled=False, device="VectorBridge")
    assert fake_sd == {}


def test_backward_sequence_jump_is_a_sender_restart(client):
    stats = client.Stats()
    pcm = np.zeros(480, dtype=np.int16)
    for seq in range(30_000, 30_010):
        assert stats.on_packet(seq, pcm)
    assert not stats.on_packet(30_005, pcm)  # a reordered packet is still late
    assert stats.on_packet(3, pcm)  # the Pi rebooted and restarted its sequence
    assert stats.on_packet(4, pcm)
    s = stats.snapshot()
    assert (s["late"], s["lost"]) == (1, 0)


def test_hello_tries_each_address_and_names_the_silent_one(client, monkeypatch):
    replies = {"192.0.2.3": None, "192.0.2.199": {"ok": True, "device": "hw:1"}}
    monkeypatch.setattr(client, "control", lambda sock, ip, port, req: replies[ip])
    ip, hello, errors = client.hello_any(None, ["192.0.2.3", "192.0.2.199"], 5101, "mac")
    assert (ip, hello["device"], errors) == ("192.0.2.199", "hw:1", ["no reply from 192.0.2.3"])
    ip, hello, errors = client.hello_any(None, ["192.0.2.3"], 5101, "mac")
    assert (ip, hello, errors) == (None, None, ["no reply from 192.0.2.3"])


def test_rx_loop_keeps_listening_after_a_socket_error(client):
    stop = client.threading.Event()
    pcm = np.ones(client.FRAME, dtype=np.int16)
    pushed = []

    class Sock:
        def __init__(self):
            self.events = [OSError(65, "No route to host"), client.encode(client.DIR_RX, 7, pcm)]

        def recvfrom(self, n):
            if not self.events:
                stop.set()
                raise TimeoutError
            e = self.events.pop(0)
            if isinstance(e, Exception):
                raise e
            return e, ("192.0.2.199", 5102)

    player = types.SimpleNamespace(push=pushed.append)
    stats = client.Stats()
    client.rx_loop(Sock(), stop, stats, player, None)
    assert len(pushed) == 1 and stats.snapshot()["received"] == 1


def test_decode_skips_a_runt_datagram(client):
    assert client.decode(b"\x00" * (client.HEADER.size - 1)) is None


def test_a_run_of_late_packets_is_a_sender_restart(client):
    stats = client.Stats()
    pcm = np.zeros(client.FRAME, dtype=np.int16)
    for seq in range(500, 510):
        assert stats.on_packet(seq, pcm)
    # the Pi restarted 5 s in: its sequence starts again at 0, a backward jump under 1000
    played = [stats.on_packet(seq, pcm) for seq in range(client.LATE_RUN_RESYNC + 10)]
    assert not any(played[:client.LATE_RUN_RESYNC - 1])
    assert all(played[client.LATE_RUN_RESYNC - 1:])
    s = stats.snapshot()
    assert (s["late"], s["lost"]) == (client.LATE_RUN_RESYNC - 1, 0)


def test_monitor_asks_for_the_listen_only_slot(client, monkeypatch):
    sent = []
    monkeypatch.setattr(client, "control", lambda sock, ip, port, req: sent.append(req) or {"ok": True})
    client.hello_any(None, ["192.0.2.3"], 5101, "js8", "monitor")
    assert sent == [{"cmd": "monitor", "client": "js8"}]


@pytest.mark.parametrize("extra", [["--tx", "tone"], ["--soak", "1"]])
def test_monitor_refuses_to_transmit(client, monkeypatch, extra):
    monkeypatch.setattr(sys, "argv", ["bridge_client.py", "--monitor", *extra])
    with pytest.raises(SystemExit) as e:
        client.main()
    assert e.value.code == 2


def test_control_skips_rx_audio_that_arrives_before_the_reply(client):
    """A monitor asks from its audio socket, where RX can land ahead of the JSON reply."""
    rx = client.encode(client.DIR_RX, 1, np.zeros(client.FRAME, dtype=np.int16))

    class Sock:
        def __init__(self):
            self.queue = []

        def settimeout(self, t):
            self.blocking = t != 0

        def recvfrom(self, n):
            if not self.blocking or not self.queue:
                raise BlockingIOError if not self.blocking else TimeoutError
            return self.queue.pop(0), ("192.0.2.3", 5101)

        def sendto(self, data, addr):
            self.queue += [rx, rx, b'{"ok":true,"role":"monitor"}']

    assert client.control(Sock(), "192.0.2.3", 5101, {"cmd": "monitor"}) == {"ok": True, "role": "monitor"}


def test_record_fills_a_sequence_gap_with_silence_and_logs_where(client, tmp_path, capsys):
    import wave

    stop = client.threading.Event()
    packets = [client.encode(client.DIR_RX, seq, np.full(client.FRAME, seq, dtype=np.int16))
               for seq in (10, 11, 14, 15)]  # 12 and 13 lost on the way

    class Sock:
        def recvfrom(self, n):
            if not packets:
                stop.set()
                raise TimeoutError
            return packets.pop(0), ("192.0.2.3", 5100)

    rec = client.Recorder(str(tmp_path / "rx.wav"))
    client.rx_loop(Sock(), stop, client.Stats(), types.SimpleNamespace(push=lambda pcm: None), rec)
    rec.close()
    with wave.open(str(tmp_path / "rx.wav")) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, client.FRAME)
    assert [int(p[0]) for p in audio] == [10, 11, 0, 0, 14, 15]  # the timeline stays continuous
    gaps = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("rx_gap")]
    assert len(gaps) == 1
    assert f"offset_samples={2 * client.FRAME} " in gaps[0] and "packets=2 " in gaps[0]
