#!/usr/bin/env python3
"""Mac-side stand-in for the iPad. Talks the VectorBridge protocol (DOCS/protocol.md).

    python tools/bridge_client.py --host vectorbridge.local            # hello, play RX audio, print stats
    python tools/bridge_client.py --host ... --tx tone --amplitude 0.25 # also send a 1 kHz tone as TX
    python tools/bridge_client.py --host ... --tx wav file.wav          # stream a 48 kHz mono int16 WAV
    python tools/bridge_client.py --host ... --tx mic                   # stream the Mac microphone
    python tools/bridge_client.py --host ... --record rx.wav            # save RX audio
    python tools/bridge_client.py --host ... --soak 10                  # both directions, final report
    python tools/bridge_client.py --host ... --monitor --out-device VectorBridge  # listen-only beside the iPad

Run from tools/.venv (sounddevice + numpy). Standalone: it does not import the vector_bridge package.
"""

from __future__ import annotations

import argparse
import collections
import json
import signal
import socket
import struct
import sys
import threading
import time
import wave
from datetime import UTC, datetime

import numpy as np

MAGIC = b"VBRG"
VERSION = 1
DIR_RX, DIR_TX = 0, 1
HEADER = struct.Struct("<4sBBIQH")
RATE = 48_000
FRAME = 480
FRAME_S = FRAME / RATE
SEQ_MOD = 1 << 32
# A Pi that restarts soon after connecting jumps back by less than 1000; no reordering on
# the link holds back this many packets in a row (0.5 s), so a run this long is a restart.
LATE_RUN_RESYNC = 50


def seq_delta(a: int, b: int) -> int:
    d = (a - b) % SEQ_MOD
    return d - SEQ_MOD if d >= SEQ_MOD // 2 else d


def encode(direction: int, seq: int, pcm: np.ndarray) -> bytes:
    pcm = np.ascontiguousarray(pcm, dtype="<i2")
    return HEADER.pack(MAGIC, VERSION, direction, seq % SEQ_MOD,
                       int(time.monotonic() * 1e6), len(pcm)) + pcm.tobytes()


def decode(data: bytes):
    if len(data) < HEADER.size:  # a runt datagram must not raise struct.error and kill rx_loop
        return None
    magic, ver, direction, seq, ts, n = HEADER.unpack_from(data)
    if magic != MAGIC or ver != VERSION or len(data) != HEADER.size + 2 * n:
        return None
    return direction, seq, ts, np.frombuffer(data, dtype="<i2", offset=HEADER.size, count=n)


class Stats:
    """Per-second and cumulative RX statistics, computed on the receive thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.received = self.lost = self.late = self.dup = 0
        self.max_gap = 0
        self.jitter_ms = 0.0
        self.max_dev_ms = 0.0
        self.peak = self.rms = self.clips = 0
        self.tx_sent = 0
        self.play_underruns = 0
        self._next = None
        self._last_arrival = None
        self._late_run = 0

    def on_packet(self, seq: int, pcm: np.ndarray) -> bool:
        """Return True if the packet is in order (or a gap was skipped), False if late/dup."""
        now = time.monotonic()
        with self.lock:
            self.received += 1
            if self._last_arrival is not None:
                dev = abs((now - self._last_arrival) * 1000 - 10.0)
                self.jitter_ms += (dev - self.jitter_ms) / 16
                self.max_dev_ms = max(self.max_dev_ms, dev)
            self._last_arrival = now
            x = pcm.astype(np.int32)
            self.peak = int(np.abs(x).max()) if x.size else 0
            self.rms = int(np.sqrt(np.mean(x * x))) if x.size else 0
            self.clips += int(np.count_nonzero(np.abs(x) >= 32700))
            if self._next is None:
                self._next = seq
            d = seq_delta(seq, self._next)
            if abs(d) > 1000:  # sender restarted (a Pi reboot starts near 0): resync, not late or lost
                d = 0
            if d < 0:
                self._late_run += 1
                if self._late_run < LATE_RUN_RESYNC:
                    self.late += 1
                    return False
                d = 0  # the sender restarted after all: resync on this packet
            self._late_run = 0
            if d > 0:
                self.lost += d
                self.max_gap = max(self.max_gap, d)
            self._next = (seq + 1) % SEQ_MOD
            return True

    def snapshot(self) -> dict:
        with self.lock:
            return {"received": self.received, "lost": self.lost, "late": self.late,
                    "max_gap": self.max_gap, "jitter_ms": self.jitter_ms,
                    "max_dev_ms": self.max_dev_ms, "peak": self.peak, "rms": self.rms,
                    "clips": self.clips, "tx_sent": self.tx_sent,
                    "play_underruns": self.play_underruns}


class Player:
    """Small priming jitter buffer feeding a sounddevice output callback."""

    def __init__(self, stats: Stats, target_frames: int = 3, enabled: bool = True,
                 device: str | int | None = None) -> None:
        self.stats = stats
        self.q: collections.deque[np.ndarray] = collections.deque(maxlen=12)
        self.target = target_frames
        self.primed = False
        self.lock = threading.Lock()
        self.stream = None
        if enabled:
            import sounddevice as sd

            self.stream = sd.OutputStream(samplerate=RATE, channels=1, dtype="int16",
                                          blocksize=FRAME, callback=self._cb, device=device)
            self.stream.start()

    def push(self, pcm: np.ndarray) -> None:
        with self.lock:
            self.q.append(pcm)
            if len(self.q) >= self.target:
                self.primed = True

    def _cb(self, out, frames, t, status):
        with self.lock:
            if self.primed and self.q:
                pcm = self.q.popleft()
                out[:, 0] = pcm[:frames] if len(pcm) >= frames else np.pad(pcm, (0, frames - len(pcm)))
                return
            if self.primed:
                self.primed = False
                self.stats.play_underruns += 1
        out.fill(0)

    def close(self) -> None:
        if self.stream:
            self.stream.stop()
            self.stream.close()


def control(sock: socket.socket, host: str, port: int, req: dict, timeout: float = 2.0,
            retries: int = 3) -> dict | None:
    """One request, one reply. Replies queued from an earlier attempt that timed out
    are drained before each send, which removes the common stale-reply case; a reply
    that lands between the drain and this attempt's reply can still be mismatched
    (the protocol has no request nonce). RX audio arriving on the same socket (a monitor
    asks from its audio socket) is skipped while waiting for the reply."""
    payload = json.dumps(req, separators=(",", ":")).encode()
    for _ in range(retries):
        sock.settimeout(0)
        try:
            while True:
                sock.recvfrom(65535)
        except (BlockingIOError, OSError):
            pass
        try:
            sock.sendto(payload, (host, port))
            deadline = time.monotonic() + timeout
            while True:
                sock.settimeout(max(0.001, deadline - time.monotonic()))
                data, _ = sock.recvfrom(65535)
                if not data.startswith(MAGIC):
                    return json.loads(data)
                if time.monotonic() >= deadline:
                    raise TimeoutError
        except (TimeoutError, ValueError):
            continue
        except OSError:
            return None
    return None


def resolve(host: str) -> list[str]:
    """Every IPv4 address for host, in resolver order. A stale DHCP lease can put a dead
    address first (vectorbridge.lan, 2026-09-30), so the caller tries each one."""
    infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def hello_any(sock: socket.socket, ips: list[str], port: int, client: str,
              cmd: str = "hello") -> tuple[str | None, dict | None, list[str]]:
    """Hello (or monitor) to each address in turn. Returns (ip, reply, errors) for the first
    that answers ok, or (None, None, errors) with one reason per address."""
    errors = []
    for ip in ips:
        reply = control(sock, ip, port, {"cmd": cmd, "client": client})
        if reply and reply.get("ok"):
            return ip, reply, errors
        errors.append(f"no reply from {ip}" if reply is None else f"{ip} answered {reply}")
    return None, None, errors


def tx_source(kind: str, path: str | None, amplitude: float):
    """Generator of 480-sample int16 blocks for tone or wav."""
    if kind == "tone":
        n = np.arange(FRAME)
        phase = 0
        while True:
            blk = (amplitude * 32767 * np.sin(2 * np.pi * 1000 * (n + phase) / RATE)).astype(np.int16)
            phase = (phase + FRAME) % RATE
            yield blk
    if kind == "wav":
        with wave.open(path, "rb") as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
                sys.exit(f"{path}: need 48000 Hz mono 16-bit, got {w.getframerate()} Hz "
                         f"{w.getnchannels()} ch {8 * w.getsampwidth()}-bit")
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        i = 0
        while True:
            blk = pcm[i:i + FRAME]
            if len(blk) < FRAME:
                blk = np.pad(blk, (0, FRAME - len(blk)))
                i = 0
            else:
                i += FRAME
            yield blk
    raise ValueError(kind)


def _utc(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="milliseconds")


class Recorder:
    """The --record WAV, kept on the RX timeline: each sequence gap is filled with silence and
    logged with its sample offset, so a replay can leave out the slots it touches. The audio's
    UTC is record_start + offset_s; the gap's utc= is when the packet after it arrived, which
    trails by up to a second in a Wi-Fi stall."""

    def __init__(self, path: str) -> None:
        self.w = wave.open(path, "wb")  # noqa: SIM115 - closed by close(), from main()
        self.w.setnchannels(1)
        self.w.setsampwidth(2)
        self.w.setframerate(RATE)
        self.samples = self.padded = 0

    def write(self, pcm: np.ndarray, gap: int) -> None:
        """Append one in-order packet, after `gap` packets of silence for those lost before it."""
        now = time.time()
        if self.samples == 0:
            print(f"record_start utc={_utc(now)}", flush=True)
        if gap > 0:
            self.padded += gap
            print(f"rx_gap utc={_utc(now)} offset_samples={self.samples} offset_s={self.samples / RATE:.3f} "
                  f"packets={gap} total_padded={self.padded}", flush=True)
            self.w.writeframes(bytes(2 * gap * len(pcm)))
            self.samples += gap * len(pcm)
        self.w.writeframes(pcm.tobytes())
        self.samples += len(pcm)

    def close(self) -> None:
        self.w.close()


def rx_loop(audio: socket.socket, stop: threading.Event, stats: Stats, player: Player,
            rec: Recorder | None) -> None:
    while not stop.is_set():
        try:
            data, _ = audio.recvfrom(4096)
        except TimeoutError:
            continue
        except OSError:
            # An ICMP error from a rebooting Pi or a dropped Wi-Fi hop surfaces here as
            # "Connection refused" or "No route to host"; keep listening, as send() does.
            stop.wait(0.1)
            continue
        p = decode(data)
        if p is None or p[0] != DIR_RX:
            continue
        _, seq, _, pcm = p
        lost = stats.lost  # only this thread changes it
        if stats.on_packet(seq, pcm):
            player.push(pcm)
            if rec:
                rec.write(pcm, stats.lost - lost)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="vectorbridge.local")
    ap.add_argument("--audio-port", type=int, default=5100)
    ap.add_argument("--control-port", type=int, default=5101)
    ap.add_argument("--tx", nargs="+", metavar=("MODE", "FILE"),
                    help="also send TX audio: tone | wav FILE (48 kHz mono int16) | mic")
    ap.add_argument("--amplitude", type=float, default=0.25, help="tone amplitude, 0..1 full scale")
    ap.add_argument("--record", metavar="FILE", help="write received RX audio to a WAV, lost packets as silence")
    ap.add_argument("--soak", type=float, metavar="MINUTES", help="run both directions, then print a report")
    ap.add_argument("--no-play", action="store_true", help="do not open the Mac output device")
    ap.add_argument("--out-device", metavar="NAME",
                    help="sounddevice output for RX playback, e.g. a Loopback device (default: system output)")
    ap.add_argument("--in-device", metavar="NAME",
                    help="sounddevice input for --tx mic, e.g. a Loopback device (default: system input)")
    ap.add_argument("--list-devices", action="store_true", help="print sounddevice's device table and exit")
    ap.add_argument("--client", default="mac-client", help="client name sent in hello")
    ap.add_argument("--monitor", action="store_true",
                    help="listen-only: receive RX beside the iPad without taking its slot; sends no TX")
    args = ap.parse_args()
    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return 0
    args.wav = None
    if args.tx:
        mode, rest = args.tx[0], args.tx[1:]
        if mode not in ("tone", "wav", "mic") or (mode == "wav") != (len(rest) == 1) or (mode != "wav" and rest):
            ap.error("--tx takes: tone | wav FILE | mic")
        args.tx = mode
        args.wav = rest[0] if rest else None
    if args.monitor and (args.tx or args.soak):
        ap.error("--monitor is listen-only: it cannot be used with --tx or --soak")
    if args.soak and not args.tx:
        args.tx = "tone"
    try:
        ips = resolve(args.host)  # resolve once, not per packet
    except OSError as e:
        print(f"cannot resolve {args.host}: {e}", file=sys.stderr)
        return 2

    # hello on the control socket (retried per address), then a keepalive from the audio
    # socket so the Pi registers the audio socket as the RX peer; status queries never re-register.
    # A monitor sends `monitor` from the audio socket instead: RX goes to the socket that asked,
    # and a TX keepalive from it would take the iPad's peer slot.
    ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("0.0.0.0", 0))
    cmd = "monitor" if args.monitor else "hello"
    host_ip, hello, errors = hello_any(audio if args.monitor else ctl, ips, args.control_port,
                                       args.client, cmd)
    if not hello:
        print(f"{cmd} to {args.host}:{args.control_port} failed: {'; '.join(errors)}", file=sys.stderr)
        return 2
    print(f"connected: device={hello['device']} protocol={hello['protocol']} "
          f"rate={hello['sample_rate']} port={hello['audio_port']}"
          + (" role=monitor" if args.monitor else ""))
    audio_port = hello["audio_port"]
    audio.settimeout(0.5)
    monitor_req = json.dumps({"cmd": "monitor", "client": args.client}, separators=(",", ":")).encode()
    keepalive = encode(DIR_TX, 0, np.zeros(0, dtype=np.int16))

    def send(pkt: bytes) -> None:
        # A rebooting Pi or a dropped Wi-Fi hop raises "No route to host" or "Host is down";
        # one lost packet must not kill the sending thread, or the Pi never re-registers us.
        try:
            audio.sendto(pkt, (host_ip, audio_port))
        except OSError:
            pass

    if not args.monitor:
        send(keepalive)
    baseline = control(ctl, host_ip, args.control_port, {"cmd": "status"}) or {}

    stats = Stats()
    stop = threading.Event()
    player = Player(stats, enabled=not args.no_play, device=args.out_device)
    # Not a context manager on purpose: the WAV must be closed explicitly on KeyboardInterrupt
    # (below) so its header is finalized, and the stream code spans two threads.
    rec = Recorder(args.record) if args.record else None

    def tx_loop():
        seq = 0
        if args.tx == "mic":
            import sounddevice as sd

            def cb(indata, frames, t, status):
                nonlocal seq
                send(encode(DIR_TX, seq, indata[:, 0]))
                seq += 1
                stats.tx_sent += 1

            with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME, callback=cb,
                                device=args.in_device):
                stop.wait()
            return
        src = tx_source(args.tx, args.wav, args.amplitude)
        t = time.monotonic()
        while not stop.is_set():
            send(encode(DIR_TX, seq, next(src)))
            seq += 1
            stats.tx_sent += 1
            t += FRAME_S
            d = t - time.monotonic()
            if d > 0:
                time.sleep(d)
            elif d < -0.5:
                t = time.monotonic()

    def keepalive_loop():
        # keep the peer registration alive when no TX audio is flowing; a monitor repeats
        # `monitor` instead (rx_loop ignores the JSON replies)
        while not stop.wait(2.0):
            if args.monitor:
                try:
                    audio.sendto(monitor_req, (host_ip, args.control_port))
                except OSError:
                    pass
            elif not args.tx:
                send(keepalive)

    threads = [threading.Thread(target=rx_loop, args=(audio, stop, stats, player, rec), daemon=True),
               threading.Thread(target=keepalive_loop, daemon=True)]
    if args.tx:
        threads.append(threading.Thread(target=tx_loop, daemon=True))
    for th in threads:
        th.start()

    deadline = time.monotonic() + args.soak * 60 if args.soak else None
    prev = stats.snapshot()
    # a background run is stopped with SIGTERM (background jobs ignore SIGINT): end it the same way
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        while deadline is None or time.monotonic() < deadline:
            time.sleep(1.0)
            s = stats.snapshot()
            print(f"rx {s['received'] - prev['received']:4d}/s lost {s['lost']} late {s['late']} "
                  f"jit {s['jitter_ms']:.2f}ms maxdev {s['max_dev_ms']:.1f}ms "
                  f"peak {s['peak']:5d} rms {s['rms']:5d} clips {s['clips']} "
                  f"underruns {s['play_underruns']} | tx {s['tx_sent'] - prev['tx_sent']:4d}/s", flush=True)
            prev = s
    except KeyboardInterrupt:
        pass
    # sample the Pi while the stream is still flowing, so the end-of-stream drain is not counted
    pi = control(ctl, host_ip, args.control_port, {"cmd": "status"})
    stop.set()
    for th in threads:
        th.join(timeout=2)
    player.close()
    if rec:
        rec.close()
        print(f"recorded {args.record}")

    s = stats.snapshot()
    if args.soak:
        loss_pct = 100.0 * s["lost"] / max(1, s["received"] + s["lost"])
        if pi:
            # Pi counters are cumulative since service start; report this run's deltas
            for k in ("tx_packets_received", "tx_packet_loss", "tx_underruns", "rx_overruns",
                      "clip_count", "device_reopens"):
                pi[k] = pi.get(k, 0) - baseline.get(k, 0)
            pi_loss = pi["tx_packet_loss"]
            pi_loss_pct = 100.0 * pi_loss / max(1, pi["tx_packets_received"] + pi_loss)
        print("\n=== soak report ===")
        print(f"duration_min          {args.soak}")
        print(f"rx_received           {s['received']}")
        print(f"rx_lost               {s['lost']} ({loss_pct:.3f}%)")
        print(f"rx_late               {s['late']}")
        print(f"rx_max_gap            {s['max_gap']}")
        print(f"rx_jitter_ms          {s['jitter_ms']:.2f} (max deviation {s['max_dev_ms']:.1f})")
        print(f"rx_clips              {s['clips']}")
        print(f"mac_play_underruns    {s['play_underruns']}")
        print(f"tx_sent               {s['tx_sent']}")
        if pi:
            print(f"pi_tx_received        {pi.get('tx_packets_received')}")
            print(f"pi_tx_loss            {pi_loss} ({pi_loss_pct:.3f}%)")
            print(f"pi_tx_underruns       {pi.get('tx_underruns')}")
            print(f"pi_rx_overruns        {pi.get('rx_overruns')}")
            print(f"pi_network_jitter_ms  {pi.get('network_jitter_ms')}")
            print(f"pi_clip_count         {pi.get('clip_count')}")
            print(f"pi_device             {pi.get('alsa_device')}")
            print(f"pi_device_reopens     {pi.get('device_reopens')}")
            print(f"pi_uptime_s           {pi.get('uptime_s')}")
        else:
            print("pi_status             unavailable")
        # proposal gate: loss below 0.1 percent in each direction, no underruns, no overruns
        ok = loss_pct < 0.1 and bool(pi) and pi_loss_pct < 0.1 \
            and pi["tx_underruns"] == 0 and pi["rx_overruns"] == 0
        print(f"result                {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
