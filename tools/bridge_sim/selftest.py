# SPDX-License-Identifier: GPL-3.0-or-later
"""bridge_sim self-test: drives run.sh as a client would, over UDP and HTTP only. Run by selftest.sh.

Exits 0 when every check passes, 1 on the first failure. Test harness only, never shipped in the app.
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
RUN = HERE / "run.sh"
REPO = HERE.parents[1]
HEADER = struct.Struct("<4sBBIQH")  # DOCS/protocol.md, written out here so the test does not lean on the bridge
RATE, FRAME = 48000, 480
PIN, DEVICE_ID = "2468", "5e5e5e5e5e5e5e5e"
CHECKS = 0


class Fail(Exception):
    pass


def expected_commit() -> str | None:
    """What the ready line's bridge_commit must say, worked out here without the sim's code."""
    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=10,
                                 check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None
    head, changes = git("rev-parse", "--short", "HEAD"), git("status", "--porcelain", "--untracked-files=no")
    if head is None or changes is None:
        return None
    return head + ("-dirty" if changes else "")


def expected_version() -> str:
    text = (REPO / "src" / "vector_bridge" / "__init__.py").read_text()
    return re.search(r'^__version__ = "([^"]+)"', text, re.MULTILINE).group(1)


def check(cond, what: str) -> None:
    global CHECKS
    if not cond:
        raise Fail(what)
    CHECKS += 1
    print(f"PASS {what}", flush=True)


def free_ports(n: int) -> list[int]:
    socks = []
    for _ in range(n):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            t.bind(("127.0.0.1", s.getsockname()[1]))  # free for both TCP (HTTP) and UDP
        except OSError:
            s.close()
            t.close()
            continue
        socks += [s, t]
    ports = sorted({s.getsockname()[1] for s in socks})[:n]
    for s in socks:
        s.close()
    if len(ports) < n or 5100 in ports or 8080 in ports:
        return free_ports(n)
    return ports


def write_wav(path: Path, rate: int, seconds: float, hz: float, amp: int = 8000) -> None:
    t = np.arange(int(rate * seconds)) / rate
    x = (amp * np.sin(2 * np.pi * hz * t)).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(x.tobytes())


def rms(x) -> float:
    x = np.asarray(x, dtype=np.float64)
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


def utc(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).timestamp()


def utc_slot(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def rx_timeline(client, since: float) -> list[tuple[float, np.ndarray]]:
    """RX frames with the wall time of their first sample. timestamp_us is the bridge's time.monotonic(),
    the same clock as this process's on the same machine, so the mapping is exact."""
    offset = time.time() - time.monotonic()
    pkts = [d for w, d in list(client.rx) if w >= since]
    return [(HEADER.unpack_from(d)[4] / 1e6 + offset, np.frombuffer(d, "<i2", offset=20)) for d in pkts]


def rx_onset(client, since: float, level: float) -> float | None:
    """Wall time of the first RX frame louder than `level`, read from the frame grid rather than that one
    frame's stamp. The bridge stamps a frame when it starts the read, so a single stamp is late by however
    long the previous loop overslept (about 10 ms when the OS wakes it late), and never early: that read
    starts after the previous one returned at this frame's start. Frames are 10 ms apart by sequence
    number, so the minimum over the frames around the onset of (stamp - sequence offset) is the grid
    with the least of that lateness."""
    offset = time.time() - time.monotonic()
    pkts = [HEADER.unpack_from(d)[3:5] + (np.frombuffer(d, "<i2", offset=20),) for w, d in list(client.rx) if w >= since]
    loud = next((i for i, (_, _, pcm) in enumerate(pkts) if rms(pcm) > level), None)
    if loud is None:
        return None
    seq0 = pkts[loud][0]
    grid = [ts / 1e6 - (seq - seq0) * FRAME / RATE for seq, ts, _ in pkts[max(0, loud - 100):loud + 100]]
    return min(grid) + offset


class Sim:
    """run.sh in a subprocess; stdout JSON lines collected in the background."""

    def __init__(self, work: Path, name: str, extra: list[str], rx_dir: Path, close_stdout: bool = False) -> None:
        self.close_stdout = close_stdout  # read the ready line, then close the pipe as a crashed launcher would
        self.tx_dir = work / f"tx-{name}"
        self.ports = free_ports(4)
        http, fault, audio, ctl = self.ports
        self.err_path = work / f"{name}.stderr.log"
        self.lines: queue.Queue = queue.Queue()
        self.proc = subprocess.Popen(
            ["bash", str(RUN), "--rx-dir", str(rx_dir), "--tx-dir", str(self.tx_dir), "--http-port", str(http),
             "--fault-port", str(fault), "--audio-port", str(audio), "--control-port", str(ctl), *extra],
            stdout=subprocess.PIPE, stderr=self.err_path.open("w"), text=True)
        threading.Thread(target=self._read, daemon=True).start()
        self.ready = self._wait_ready()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
            if self.close_stdout and json.loads(line).get("ready"):
                self.proc.stdout.close()
                return

    def _wait_ready(self) -> dict:
        deadline = time.monotonic() + 180  # the first run builds the venv
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=0.5)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise Fail(f"run.sh exited {self.proc.returncode} before ready; see {self.err_path}") from None
                continue
            obj = json.loads(line)
            if not hasattr(self, "first_line"):
                self.first_line = obj
            if obj.get("ready"):
                return obj
        raise Fail("no ready line within 180 s")

    def http(self, method: str, path: str, body: dict | None = None, token: str | None = None, base: str | None = None):
        req = urllib.request.Request((base or self.ready["http"]) + path, method=method,
                                     data=None if body is None else json.dumps(body).encode())
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def fault(self, body: dict):
        return self.http("POST", "/sim/fault", body, base=self.ready["fault"])

    def slots(self) -> list[dict]:
        p = self.tx_dir / "slots.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def stop(self) -> int:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                return self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
                return -9
        return self.proc.returncode


class Client:
    """One-socket layout (DOCS/protocol.md): hello to the control port, RX and TX on the same socket."""

    def __init__(self, ctl_port: int, audio_port: int) -> None:
        self.ctl, self.audio = ("127.0.0.1", ctl_port), ("127.0.0.1", audio_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.rx: list[tuple[float, bytes]] = []
        self.replies: queue.Queue = queue.Queue()
        self.keepalive = threading.Event()
        self.keepalive.set()
        self._stop = threading.Event()
        self.seq = 1000
        threading.Thread(target=self._recv, daemon=True).start()
        threading.Thread(target=self._keepalive, daemon=True).start()

    def _recv(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except (TimeoutError, OSError):
                continue
            if addr == self.ctl:
                self.replies.put(json.loads(data))
            else:
                self.rx.append((time.time(), data))

    def _keepalive(self) -> None:
        while not self._stop.wait(1.0):
            if self.keepalive.is_set():
                self.sock.sendto(HEADER.pack(b"VBRG", 1, 1, self.seq, 0, 0), self.audio)

    def hello(self) -> dict:
        self.sock.sendto(json.dumps({"cmd": "hello", "client": "selftest"}).encode(), self.ctl)
        return self.replies.get(timeout=3)

    def send_tx(self, pcm: np.ndarray) -> None:
        self.sock.sendto(HEADER.pack(b"VBRG", 1, 1, self.seq, int(time.monotonic() * 1e6), FRAME)
                         + pcm.astype("<i2").tobytes(), self.audio)
        self.seq += 1

    def rx_since(self, t: float) -> list[bytes]:
        return [d for when, d in list(self.rx) if when >= t]

    def close(self) -> None:
        self._stop.set()
        self.sock.close()


def wait_for(fn, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise Fail(f"timed out waiting for {what}")


def tx_tone(client: Client, sim: Sim, token: str, seconds: float, owner: str | None) -> None:
    """About `seconds` of 1 kHz TX audio at 10 ms pacing, with a ladder heartbeat every 0.5 s when keyed."""
    n = int(seconds * 100)
    t0 = time.monotonic()
    for i in range(n):
        ph = np.arange(i * FRAME, (i + 1) * FRAME) / RATE
        client.send_tx(6000 * np.sin(2 * np.pi * 1000 * ph))
        if owner and i % 50 == 49:
            code, out = sim.http("POST", "/api/ladder", {"action": "heartbeat", "owner": owner}, token)
            if code != 200:
                raise Fail(f"heartbeat refused {code} {out}")
        delay = t0 + (i + 1) * 0.01 - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def tx_files(sim: Sim) -> list[Path]:
    return sorted(sim.tx_dir.glob("tx-*.json"))


def run_main(work: Path, rx_loop: Path) -> None:
    sim = Sim(work, "loop", ["--loop"], rx_loop)
    client = None
    try:
        r = sim.ready
        http, fault, audio, ctl = sim.ports
        check(r["bridge_commit"] == expected_commit() and r["bridge_version"] == expected_version()
              and r["pin"] == PIN and r["device_id"] == DEVICE_ID,
              f"ready line: commit {r['bridge_commit']}, version {r['bridge_version']}, pin, device_id")
        check(r["http"] == f"http://127.0.0.1:{http}" and r["fault"] == f"http://127.0.0.1:{fault}"
              and r["audio_port"] == audio and r["control_port"] == ctl, "ready line: the ports asked for")
        check(r["max_tx_s"] == 15 and r["rig"] == "sim", "ready line: max_tx_s 15, rig sim")
        check(sim.first_line is r, "ready line is the first stdout line")

        # RX over the real wire format
        client = Client(ctl, audio)
        rep = client.hello()
        check(rep.get("ok") and rep.get("audio_port") == audio and rep.get("sample_rate") == RATE, "hello reply")
        t = time.time()
        pkts = wait_for(lambda: len(client.rx_since(t)) >= 50 and client.rx_since(t), 5, "RX packets")
        heads = [HEADER.unpack_from(p) for p in pkts]
        check(all(h[0] == b"VBRG" and h[1] == 1 and h[2] == 0 and h[5] == FRAME and len(p) == 20 + 2 * FRAME
                  for h, p in zip(heads, pkts, strict=True)), f"RX packets: magic VBRG, RX direction, 480 samples ({len(pkts)} seen)")
        seqs = [h[3] for h in heads]
        check(all(b == a + 1 for a, b in pairwise(seqs)), "RX sequence numbers consecutive")
        check(len(sim.slots()) >= 1 and set(sim.slots()[0]) == {"slot_utc", "rx_file"}, "slots.jsonl gets lines")

        # --loop: the first file plays in the first whole slot after start, from its sample 0
        first = wait_for(lambda: next((s for s in sim.slots() if s["rx_file"]), None), 20, "the first file's slot")
        check(first["rx_file"] == "001.wav", "--loop plays 001.wav first")
        slot_t = datetime.strptime(first["slot_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
        check(slot_t % 15 == 0, f"slot_utc on a 15 s boundary ({first['slot_utc']})")
        wait_for(lambda: time.time() > slot_t + 15.6, 20, "the second file's slot")
        tl = rx_timeline(client, slot_t - 1.0)
        onset = rx_onset(client, slot_t - 1.0, 1000)
        # The loud frame must be the frame that starts at the boundary: nearer to it than half a 10 ms frame.
        check(onset is not None and abs(onset - slot_t) < FRAME / RATE / 2,
              f"001.wav audio starts in the frame at the slot boundary (grid offset "
              f"{None if onset is None else round(onset - slot_t, 4)} s, limit half a frame, 0.005 s)")
        in_file = [rms(pcm) for w, pcm in tl if slot_t + 0.1 < w < slot_t + 12.9]
        check(in_file and min(in_file) > 4000, "12 kHz file upsampled to full level at 48 kHz")
        gap = [rms(pcm) for w, pcm in tl if slot_t + 13.1 < w < slot_t + 14.9]
        check(gap and 20 < min(gap) and max(gap) < 200, f"band-noise floor after a 13 s file (rms {max(gap or [0]):.0f})")
        nxt = [s for s in sim.slots() if s["slot_utc"] == utc_slot(slot_t + 15)]
        check(nxt and nxt[0]["rx_file"] == "002.wav", "--loop plays 002.wav in the next slot")
        f2 = [rms(pcm) for w, pcm in tl if slot_t + 15.1 < w < slot_t + 15.5]
        check(f2 and min(f2) > 4000, "48 kHz file at full level")

        # HTTP: status, login, ladder
        code, st = sim.http("GET", "/api/status")
        check(code == 200 and st.get("device_id") == DEVICE_ID, "GET /api/status device_id")
        check(st["audio_device"]["ok"] is True, "status audio_device ok")
        code, out = sim.http("POST", "/api/ladder", {"action": "radio"})
        check(code == 401, "ladder POST without a token is 401")
        code, out = sim.http("POST", "/api/login", {"pin": PIN, "app": "bridge_sim selftest"})
        check(code == 200 and out.get("token") and out.get("device_id") == DEVICE_ID, "POST /api/login with pin + app")
        token = out["token"]
        code, lad = sim.http("GET", "/api/ladder", token=token)
        check(code == 200 and lad["tx_allowed"] is True and lad["max_tx_s"] == 15, "GET /api/ladder tx_allowed, max_tx_s 15")
        code, out = sim.http("POST", "/api/ladder", {"action": "tune", "freq_hz": 14074000, "mode": "PKTUSB"}, token)
        check(code == 200 and out["result"]["ok"] is True, "ladder tune 14074000 PKTUSB result.ok")
        code, out = sim.http("POST", "/api/ladder", {"action": "radio"}, token)
        check(code == 200 and out["radio"]["freq_hz"] == 14074000 and out["radio"]["mode"] == "PKTUSB", "ladder radio")
        for p in ("/api/battery", "/api/readiness", "/api/wizard"):
            code, _ = sim.http("GET", p)
            check(code == 200, f"GET {p}")

        # TX: key, ~2 s of tone with heartbeats, unkey; then the recording. Two in one slot: start early in it.
        if time.time() % 15 > 4:  # both keys need about 4 s, so leave 10 s of margin
            time.sleep(15 - time.time() % 15 + 0.2)
        code, out = sim.http("POST", "/api/ladder", {"action": "key", "owner": "selftest"}, token)
        check(code == 200 and out["ok"] is True, "ladder key")
        tx_tone(client, sim, token, 2.0, "selftest")
        code, out = sim.http("POST", "/api/ladder", {"action": "unkey", "owner": "selftest"}, token)
        check(code == 200 and out["ok"] is True, "ladder unkey")
        side = wait_for(lambda: tx_files(sim), 5, "the TX sidecar")[0]
        meta = json.loads(side.read_text())
        wav = side.with_suffix(".wav")
        check(side.name.startswith("tx-") and wav.exists(), f"TX recording {wav.name} + sidecar")
        check(meta["owner"] == "selftest" and meta["mode"] == "cat", "sidecar owner")
        k, u = utc(meta["key_utc"]), utc(meta["unkey_utc"])
        check(2.0 <= u - k < 3.0, f"key to unkey {u - k:.2f} s")
        a, p = utc(meta["first_nonsilent_arrival_utc"]), utc(meta["first_nonsilent_playout_utc"])
        check(k - 0.5 < a <= p < k + 0.5 and 0.05 <= p - a < 0.4, f"arrival then playout ({(p - a) * 1000:.0f} ms jitter prime)")
        check(195 <= meta["packets"] <= 200 and meta["seq_gaps"] == 0 and meta["late"] == 0,
              f"telemetry deltas packets {meta['packets']} gaps {meta['seq_gaps']} late {meta['late']} underruns {meta['underruns']}")
        with wave.open(str(wav)) as w:
            ok = (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, RATE)
            x = np.frombuffer(w.readframes(w.getnframes()), "<i2")
        check(ok and abs(len(x) / RATE - (u - k)) < 0.05 and rms(x[int(0.3 * RATE):int(1.8 * RATE)]) > 3000,
              f"TX WAV 48 kHz mono, {len(x) / RATE:.2f} s of tone")
        base = "tx-" + datetime.fromtimestamp(int(k // 15) * 15, UTC).strftime("%y%m%d_%H%M%S")
        check(slot_t <= k and wav.name == base + ".wav", f"TX file named for the key's UTC slot ({wav.name})")
        code, out = sim.http("POST", "/api/ladder", {"action": "key", "owner": "selftest"}, token)
        check(code == 200, "second key in the same slot")
        tx_tone(client, sim, token, 0.3, "selftest")
        sim.http("POST", "/api/ladder", {"action": "unkey", "owner": "selftest"}, token)
        second = wait_for(lambda: [m for m in tx_files(sim) if m != side], 5, "the second sidecar")
        check([m.name for m in second] == [base + "-2.json"] and (sim.tx_dir / (base + "-2.wav")).exists(),
              f"collision suffix: {second[0].name}")

        # Faults
        code, out = sim.fault({"fault": "nonsense"})
        check(code == 400 and out["ok"] is False and out["error"], "unknown fault refused")

        client.keepalive.clear()  # a keepalive would take the peer straight back
        code, out = sim.fault({"fault": "takeover", "client": "OtherApp"})
        check(code == 200 and out == {"ok": True}, "fault takeover")
        code, st = sim.http("GET", "/api/status")
        peers = [c for c in st["clients"] if c["role"] == "peer"]
        check(len(peers) == 1 and peers[0]["client"] == "OtherApp", "takeover: status peer is OtherApp")
        t = time.time() + 0.1
        time.sleep(0.5)
        check(len(client.rx_since(t)) == 0, "takeover: RX no longer reaches the client")
        check(sim.fault({"fault": "takeover_end"})[0] == 200, "fault takeover_end")
        client.hello()
        client.keepalive.set()
        code, st = sim.http("GET", "/api/status")
        check([c["client"] for c in st["clients"] if c["role"] == "peer"] == ["selftest"], "hello takes the peer back")

        code, out = sim.fault({"fault": "takeover-key", "owner": "OtherApp", "seconds": 30})
        check(code == 200 and out["ok"], "fault takeover-key")
        code, out = sim.http("POST", "/api/ladder", {"action": "key", "owner": "selftest"}, token)
        check(code == 409 and out["ok"] is False, "takeover-key: client key gets 409")
        check(sim.fault({"fault": "takeover-key_end"})[0] == 200, "fault takeover-key_end")
        other = wait_for(lambda: [m for m in tx_files(sim) if json.loads(m.read_text())["owner"] == "OtherApp"], 5,
                         "the takeover transmission's sidecar")
        check(len(other) == 1 and other[0] != side, f"takeover-key transmission recorded as {other[0].name}")

        check(sim.fault({"fault": "battery_tx_locked", "on": True})[0] == 200, "fault battery_tx_locked on")
        code, b = sim.http("GET", "/api/battery")
        check(b.get("tx_locked") is True, "battery reports tx_locked")
        code, out = sim.http("POST", "/api/ladder", {"action": "key", "owner": "selftest"}, token)
        check(code == 409, "battery_tx_locked: key gets 409")
        sim.fault({"fault": "battery_tx_locked", "on": False})
        code, b = sim.http("GET", "/api/battery")
        check(b.get("tx_locked") is False, "battery_tx_locked off")

        code, out = sim.fault({"fault": "audio_device_down", "seconds": 3})
        check(code == 200, "fault audio_device_down")
        wait_for(lambda: sim.http("GET", "/api/status")[1]["audio_device"]["ok"] is False, 3, "audio_device.ok false")
        check(True, "audio_device_down: status audio_device.ok false")
        wait_for(lambda: sim.http("GET", "/api/status")[1]["audio_device"]["ok"] is True, 20, "audio_device reopen")
        code, st = sim.http("GET", "/api/status")
        check(st.get("device_reopens", 0) >= 1, f"audio device reopened by the real supervisor (device_reopens {st.get('device_reopens')})")
        t = time.time()
        wait_for(lambda: len(client.rx_since(t)) > 20, 5, "RX after the reopen")
        check(True, "RX resumes after the reopen")

        check(sim.fault({"fault": "rig_cat_error", "on": True})[0] == 200, "fault rig_cat_error on")
        code, out = sim.http("POST", "/api/ladder", {"action": "tune", "freq_hz": 7074000, "mode": "PKTUSB"}, token)
        check(code == 502 and out["ok"] is False, "rig_cat_error: tune gets 502")
        code, out = sim.http("POST", "/api/ladder", {"action": "radio"}, token)
        check(code == 502, "rig_cat_error: radio gets 502")
        sim.fault({"fault": "rig_cat_error", "on": False})
        code, out = sim.http("POST", "/api/ladder", {"action": "radio"}, token)
        check(code == 200, "rig_cat_error off: radio answers")
    finally:
        if client:
            client.close()
        rc = sim.stop()
    check(rc == 0, "SIGTERM: clean exit 0")
    check(not tx_files(sim) or all(p.with_suffix(".wav").exists() for p in tx_files(sim)), "no half-written TX files")


def run_gates_locked(work: Path, rx_loop: Path) -> None:
    """--gates-locked, with stdout closed right after the ready line: the sim must keep capturing."""
    sim = Sim(work, "locked", ["--gates-locked", "--loop"], rx_loop, close_stdout=True)
    client = None
    try:
        r = sim.ready
        check(r["rig"] == "sim" and r["max_tx_s"] is None, "--gates-locked ready line: max_tx_s null")
        code, out = sim.http("POST", "/api/login", {"pin": PIN, "app": "bridge_sim selftest"})
        token = out["token"]
        code, lad = sim.http("GET", "/api/ladder", token=token)
        check(code == 200 and lad["tx_allowed"] is False and lad["max_tx_s"] is None and lad["next"] == "load",
              "--gates-locked: tx_allowed false, max_tx_s null, load gate next")
        code, out = sim.http("POST", "/api/ladder", {"action": "key", "owner": "selftest"}, token)
        check(code == 409 and out["ok"] is False, "--gates-locked: key gets 409")

        client = Client(sim.ports[3], sim.ports[2])
        client.hello()
        client.keepalive.clear()
        code, out = sim.fault({"fault": "takeover", "client": "Brief", "seconds": 1})
        check(code == 200, "fault takeover for 1 s")

        def peers():
            return [c["client"] for c in sim.http("GET", "/api/status")[1]["clients"] if c["role"] == "peer"]

        check(peers() == ["Brief"], "takeover for 1 s: Brief is the peer")
        t0 = time.monotonic()
        wait_for(lambda: peers() == [], 10, "the expired takeover's peer to time out")
        check(time.monotonic() - t0 > 4, f"takeover hellos stopped after its seconds; peer gone after {time.monotonic() - t0:.1f} s")
        client.hello()
        client.keepalive.set()

        n0 = len(sim.slots())
        boundary = (int(time.time() // 15) + 1) * 15
        wait_for(lambda: time.time() > boundary + 1.0, 17, "the next slot boundary")
        later = [s for s in sim.slots() if s["slot_utc"] >= utc_slot(boundary)]
        check(len(sim.slots()) > n0 and later, f"stdout closed: slots.jsonl keeps growing ({later[:1]})")
        check(len(client.rx_since(boundary + 0.2)) > 50, "stdout closed: RX packets still arrive after the boundary")
        code, st = sim.http("GET", "/api/status")
        check(st["audio_device"]["ok"] is True, "stdout closed: audio device still ok")
    finally:
        if client:
            client.close()
        rc = sim.stop()
    check(rc == 0, "stdout closed: SIGTERM, clean exit 0")


def run_no_rig(work: Path) -> None:
    rx = work / "rx-dated"
    rx.mkdir()
    now = time.time()
    if now % 15 > 11:  # too near the next boundary to be sure of the slot the sim starts in
        time.sleep(15 - now % 15 + 0.2)
        now = time.time()
    slot = int(now // 15) * 15
    name = datetime.fromtimestamp(slot, UTC).strftime("%y%m%d_%H%M%S") + ".WAV"  # upper case must match too
    write_wav(rx / name, 12000, 15, 1000)
    write_wav(rx / "260101_000007.wav", 12000, 1, 1000)  # not on a slot boundary: warned about, never played
    sim = Sim(work, "norig", ["--no-rig"], rx)
    client = None
    try:
        r = sim.ready
        check(r["rig"] == "none" and r["max_tx_s"] is None, "--no-rig ready line: rig none, max_tx_s null")
        wait_for(lambda: "260101_000007.wav does not start on a 15 s slot boundary" in sim.err_path.read_text(), 3,
                 "the misaligned-file warning on stderr")
        check(True, "misaligned dated file warned about on stderr")
        code, out = sim.http("GET", "/api/ladder")
        check(code == 503, "--no-rig: GET /api/ladder 503")
        code, out = sim.http("POST", "/api/login", {"pin": PIN, "app": "bridge_sim selftest"})
        token = out["token"]
        code, out = sim.http("POST", "/api/ladder", {"action": "radio"}, token)
        check(code == 503, "--no-rig: POST /api/ladder 503")
        first = sim.slots()[0]
        check(first["rx_file"] == name, f"dated file plays in its own slot, joined mid-slot ({first})")
        client = Client(sim.ports[3], sim.ports[2])
        client.hello()
        t = time.time()
        wait_for(lambda: len(client.rx_since(t)) > 20, 5, "RX")
        own = [rms(pcm) for w, pcm in rx_timeline(client, t) if slot + 0.05 < w < slot + 14.95]
        check(own and min(own) > 4000, f"mid-file audio, not silence, after a mid-slot start ({len(own)} frames)")
        tx_tone(client, sim, token, 1.0, None)
        side = wait_for(lambda: tx_files(sim), 5, "the VOX sidecar")[0]
        meta = json.loads(side.read_text())
        k, u = utc(meta["key_utc"]), utc(meta["unkey_utc"])
        check(meta["owner"] is None and meta["mode"] == "vox" and 0.9 < u - k < 1.2,
              f"VOX transmission recorded, owner null, {u - k:.2f} s after the 1 s hang is trimmed")
        code, out = sim.fault({"fault": "rig_cat_error", "on": True})
        check(code == 400 and out["ok"] is False, "--no-rig: rig_cat_error refused")
    finally:
        if client:
            client.close()
        rc = sim.stop()
    check(rc == 0, "--no-rig: SIGTERM, clean exit 0")


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="bridge_sim_selftest."))
    rx_loop = work / "rx-loop"
    rx_loop.mkdir()
    write_wav(rx_loop / "001.wav", 12000, 13, 1500)  # FT8-length at 12 kHz
    write_wav(rx_loop / "002.wav", 48000, 13, 1200)
    try:
        run_main(work, rx_loop)
        run_gates_locked(work, rx_loop)
        run_no_rig(work)
    except Fail as e:
        print(f"FAIL {e}", flush=True)
        print(f"selftest: FAILED after {CHECKS} checks; logs in {work}", flush=True)
        return 1
    print(f"selftest: all {CHECKS} checks passed (logs in {work})", flush=True)
    return 0


if __name__ == "__main__":
    os.environ.pop("PYTHONPATH", None)
    sys.exit(main())
