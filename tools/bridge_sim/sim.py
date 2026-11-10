# SPDX-License-Identifier: GPL-3.0-or-later
"""bridge_sim: the real VectorBridge audio service and web API on a developer machine, with WAV files for
the radio.

Test harness only. The bridge service never imports it, and client apps never ship it. Start it with
run.sh, which imports the bridge from this checkout's src/. See README.md.

What is real: BridgeService (UDP audio and control, jitter buffer, TX cap, telemetry, device reopen),
TunerApp and its HTTP server (login, PIN, status, battery, readiness, ladder), Ladder, PttController,
the readiness Monitor and Admin. What is simulated: the sound card (WAV files in, WAV files out),
the rig behind CAT (SimRig), the wizard (a stub), the battery, and the device ID.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import wave
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __name__ != "__main__":
    raise ImportError("bridge_sim is a program for tests, not a module: start it with run.sh")

# The bridge reads these at import: keep its PIN, records and config in a throwaway directory.
STATE_DIR = Path(tempfile.mkdtemp(prefix="bridge_sim."))
(STATE_DIR / "persist" / "vector-bridge").mkdir(parents=True)
os.environ["VECTOR_BRIDGE_PERSIST"] = str(STATE_DIR / "persist")
os.environ["VECTOR_BRIDGE_CONFIG"] = str(STATE_DIR / "config.toml")

import numpy as np

import vector_bridge
import vector_bridge.log as bridge_log
from vector_bridge import admin as admin_mod
from vector_bridge import apsetup, readiness, tuner, wifiradio
from vector_bridge import config as configmod
from vector_bridge import ladder as ladder_mod
from vector_bridge import service as service_mod
from vector_bridge.audio import AudioBackend, AudioDeviceError
from vector_bridge.cat import CatError
from vector_bridge.manage import control_request
from vector_bridge.profiles import RadioProfile
from vector_bridge.protocol import (
    DIR_TX,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    ProtocolError,
    decode,
)
from vector_bridge.tx import PttController

SIM_PIN = "2468"
SIM_DEVICE_ID = "5e5e5e5e5e5e5e5e"
SLOT_S = 15
FRAME_S = 0.01
FRAMES_PER_SLOT = 1500
VOX_HANG_FRAMES = 100  # 1 s of silent playout ends a VOX transmission
BURST_GAP_S = 0.2  # a TX packet after this much quiet starts a new burst of audio
DEFAULT_NOISE_RMS = 40  # band noise between files: above readiness.RX_FLOOR_RMS (30), about -58 dBFS

class SimError(Exception):
    pass


class FdWriter:
    """Text to a file descriptor from its own thread. Callers never block and never see an error: a full
    queue drops the text (counted), and once the fd fails (a closed pipe) everything after it is dropped.
    Raw os.write, so no Python buffer is left to fail at exit. The capture, playback, PTT and fault
    threads only ever put() here, so a stalled or closed stdout or stderr cannot hold them up."""

    def __init__(self, fd: int, name: str, maxsize: int = 10000) -> None:
        self.fd, self.dropped, self.dead = fd, 0, False
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def put(self, text: str) -> None:
        if self.dead:
            return
        try:
            self._q.put_nowait(text)
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        while (text := self._q.get()) is not None:
            if self.dead:
                continue
            data = text.encode()
            try:
                while data:
                    data = data[os.write(self.fd, data):]
            except OSError:
                self.dead = True  # BrokenPipeError and friends: stop writing, keep draining

    def close(self, timeout: float = 2.0) -> None:
        try:
            self._q.put(None, timeout=timeout)
        except queue.Full:
            return
        self._thread.join(timeout=timeout)


class QueuedStream:
    """sys.stderr for this process: the bridge's log, logging, tracebacks and note() all go through it."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, out: FdWriter) -> None:
        self.out = out

    def write(self, text: str) -> int:
        self.out.put(text)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


STDOUT = FdWriter(1, "stdout-writer")
STDERR = FdWriter(2, "stderr-writer")


class SlotLog:
    """Slot lines from the capture thread: put() never blocks or does I/O. A writer thread appends each to
    slots.jsonl and passes it to stdout, holding the ones from before the ready line until it is out, so
    the ready line is always first. A dead stdout does not stop slots.jsonl."""

    def __init__(self, path: Path, maxsize: int = 1000) -> None:
        self.path, self.dropped = path, 0
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread = threading.Thread(target=self._run, name="slot-writer", daemon=True)
        self._thread.start()

    def put(self, line: dict) -> None:
        try:
            self._q.put_nowait(("slot", line))
        except queue.Full:
            self.dropped += 1

    def ready(self, obj: dict) -> None:
        self._q.put(("ready", obj))  # main thread: may wait, keeps the order

    def _run(self) -> None:
        held: list[str] | None = []
        while (item := self._q.get()) is not None:
            kind, obj = item
            text = json.dumps(obj, separators=(",", ":")) + "\n"
            if kind == "ready":
                STDOUT.put(text)
                for t in held or []:
                    STDOUT.put(t)
                held = None
                continue
            try:
                with self.path.open("a") as f:
                    f.write(text)
            except OSError as e:
                note(f"slots.jsonl write failed: {e!r}")
            if held is None:
                STDOUT.put(text)
            elif len(held) < 100:
                held.append(text)
            else:
                self.dropped += 1  # still in slots.jsonl, but never reaches stdout

    def close(self, timeout: float = 2.0) -> None:
        try:
            self._q.put(None, timeout=timeout)
        except queue.Full:
            return
        self._thread.join(timeout=timeout)


def note(msg: str) -> None:
    STDERR.put(f"bridge_sim: {msg}\n")


def utc_iso(t: float | None, ms: bool = True) -> str | None:
    if t is None:
        return None
    d = datetime.fromtimestamp(t, UTC)
    return d.strftime("%Y-%m-%dT%H:%M:%S") + (f".{d.microsecond // 1000:03d}Z" if ms else "Z")


def slot_stamp(slot_s: int) -> str:
    return datetime.fromtimestamp(slot_s, UTC).strftime("%y%m%d_%H%M%S")


# ---- RX: WAV files on a UTC slot schedule ---------------------------------------------------------

def _fir_4x() -> np.ndarray:
    """Interpolation low-pass for 12 kHz to 48 kHz: Kaiser-windowed sinc, 5.4 kHz cutoff, gain 4."""
    n = np.arange(-64, 65)
    fc = 5400 / SAMPLE_RATE
    h = 2 * fc * np.sinc(2 * fc * n) * np.kaiser(len(n), 8.0)
    return h * (4 / h.sum())


_FIR = _fir_4x()


def load_wav(path: Path) -> np.ndarray:
    """Mono int16 at 48 kHz. 12 kHz files are upsampled 4x; sample 0 stays sample 0."""
    with wave.open(str(path), "rb") as w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if ch != 1 or width != 2:
        raise SimError(f"{path.name}: needs mono 16-bit PCM, got {ch} channels of {8 * width}-bit")
    x = np.frombuffer(raw, dtype="<i2")
    if not len(x):
        raise SimError(f"{path.name}: no samples")
    if rate == SAMPLE_RATE:
        y = x.astype(np.int16)
    elif rate == 12000:
        up = np.zeros(len(x) * 4)
        up[::4] = x
        delay = (len(_FIR) - 1) // 2
        y = np.clip(np.rint(np.convolve(up, _FIR)[delay:delay + len(up)]), -32768, 32767).astype(np.int16)
    else:
        raise SimError(f"{path.name}: sample rate {rate} Hz; use 12000 or 48000")
    return y[:SLOT_S * SAMPLE_RATE]  # one slot at most


class RxSchedule:
    """Which file plays in which UTC slot. --loop: sorted order from the first boundary after start,
    wrapping at the end of the list. Otherwise: <YYMMDD_HHMMSS>.wav plays in the slot it names."""

    def __init__(self, rx_dir: Path, loop: bool) -> None:
        files = sorted(p for p in rx_dir.iterdir() if p.is_file() and p.suffix.lower() == ".wav")
        self.audio = {p.name: load_wav(p) for p in files}
        self.order = [p.name for p in files]
        self.loop = loop
        self.dated: dict[str, str] = {}  # "YYMMDD_HHMMSS" -> file name, any case of the name or suffix
        if not loop:
            for name in self.order:
                stem = Path(name).stem
                try:
                    when = datetime.strptime(stem, "%y%m%d_%H%M%S").replace(tzinfo=UTC).timestamp()
                except ValueError:
                    note(f"warning: {name} is not named <YYMMDD_HHMMSS>.wav, so it never plays without --loop")
                    continue
                if int(when) % SLOT_S:
                    note(f"warning: {name} does not start on a 15 s slot boundary, so it never plays")
                    continue
                self.dated[stem] = name
        self.epoch = math.inf  # set by start(): nothing plays before the ready line

    def start(self, now: float) -> None:
        """--loop counts from the first slot boundary after the ready line, not after process start,
        which can be seconds earlier (the gate pre-pass), so a client that waits for ready hears 001 whole."""
        self.epoch = (math.floor(now / SLOT_S) + 1) * SLOT_S

    def file_for(self, slot_s: int) -> str | None:
        if self.loop:
            if not self.order or slot_s < self.epoch:
                return None
            return self.order[((slot_s - self.epoch) // SLOT_S) % len(self.order)]
        return self.dated.get(slot_stamp(slot_s))


# ---- TX: what the bridge played to the "radio" ----------------------------------------------------

class TxRecorder:
    """Writes each transmission to tx-<slot>.wav plus a .json sidecar.

    Rig mode: a transmission is SimRig PTT key to unkey. VOX (--no-rig): it starts with the first
    non-silent frame played and ends after VOX_HANG_FRAMES of silence (trimmed from the file)."""

    def __init__(self, tx_dir: Path, vox: bool, telemetry_fn, owner_fn) -> None:
        self.tx_dir, self.vox = tx_dir, vox
        self.telemetry_fn, self.owner_fn = telemetry_fn, owner_fn
        self.enabled = False  # off while the harness itself passes the gates at startup
        self._mu = threading.Lock()
        self._cur: dict | None = None
        self._burst_start: float | None = None  # wall time of the first packet of the current burst
        self._burst_tel0: dict = {}  # the bridge's telemetry just before that packet was taken in
        self._last_arrival = -math.inf  # monotonic time of the last non-silent TX packet
        self._play_start: float | None = None  # wall time the current burst's first loud frame played
        self._last_loud = -math.inf  # wall time of the last loud frame played
        self._names: set[str] = set()
        self._writers: list[threading.Thread] = []

    def on_arrival(self, pcm: np.ndarray, wall: float, mono: float) -> None:
        """A TX packet from the peer, called just before the service takes it in (so not yet counted)."""
        if not pcm.any():
            return
        with self._mu:
            if mono - self._last_arrival > BURST_GAP_S:
                self._burst_start = wall
                self._burst_tel0 = self.telemetry_fn()
            self._last_arrival = mono
            if self._cur is not None and self._cur["arrival"] is None:
                self._cur["arrival"] = self._burst_start

    def _open(self, wall: float, owner) -> None:
        """A burst of audio already under way at the key gives both stamps, so they always name the same
        (first) sample of the burst, even when it began just before the key."""
        live = time.monotonic() - self._last_arrival <= BURST_GAP_S
        arrival = self._burst_start if live else None
        playout = self._play_start if live and time.time() - self._last_loud <= BURST_GAP_S else None
        # VOX opens at the first loud playout, a jitter prime after the first packet: count from that packet.
        tel0 = self._burst_tel0 if self.vox and live and self._burst_tel0 else self.telemetry_fn()
        self._cur = {"key": wall, "owner": owner, "frames": [], "arrival": arrival, "playout": playout,
                     "tel0": tel0, "last_ns": 0, "last_ns_end": wall, "silent": 0}

    def key(self) -> None:
        with self._mu:
            if self.enabled and self._cur is None and not self.vox:
                self._open(time.time(), self.owner_fn())

    def unkey(self) -> None:
        with self._mu:
            cur, self._cur = self._cur, None
        if cur is not None:
            self._finish(cur, time.time())

    def on_frame(self, pcm: np.ndarray, wall: float) -> None:
        """One frame the playback loop handed the sound card; `wall` is when it played."""
        done = None
        with self._mu:
            loud = bool(pcm.any())
            if loud:
                if wall - self._last_loud > BURST_GAP_S:
                    self._play_start = wall
                self._last_loud = wall
            if self.vox and self.enabled and self._cur is None and loud:
                self._open(wall, None)
            cur = self._cur
            if cur is None:
                return
            cur["frames"].append(pcm.copy())
            if loud:
                if cur["playout"] is None:
                    cur["playout"] = wall
                cur["last_ns"], cur["last_ns_end"], cur["silent"] = len(cur["frames"]), wall + FRAME_S, 0
            else:
                cur["silent"] += 1
            if self.vox and cur["silent"] >= VOX_HANG_FRAMES:
                done, self._cur = cur, None
                done["frames"] = done["frames"][:done["last_ns"]]
        if done is not None:
            self._finish(done, done["last_ns_end"])

    def _finish(self, cur: dict, unkey_wall: float) -> None:
        tel1 = self.telemetry_fn()
        tel0 = cur["tel0"]

        def delta(k):
            return int(tel1.get(k, 0)) - int(tel0.get(k, 0))

        with self._mu:
            base = f"tx-{slot_stamp(int(cur['key'] // SLOT_S) * SLOT_S)}"
            name, n = base, 1
            while name in self._names or (self.tx_dir / f"{name}.wav").exists():
                n += 1
                name = f"{base}-{n}"
            self._names.add(name)
        pcm = np.concatenate(cur["frames"]) if cur["frames"] else np.zeros(0, np.int16)
        meta = {"wav": f"{name}.wav", "key_utc": utc_iso(cur["key"]), "unkey_utc": utc_iso(unkey_wall),
                "first_nonsilent_arrival_utc": utc_iso(cur["arrival"]),
                "first_nonsilent_playout_utc": utc_iso(cur["playout"]),
                "packets": delta("tx_packets_received"), "seq_gaps": delta("tx_packet_loss"),
                "late": delta("tx_late"), "underruns": delta("tx_underruns"), "owner": cur["owner"],
                "mode": "vox" if self.vox else "cat", "samples": len(pcm), "sample_rate": SAMPLE_RATE}
        # Written off the PTT path. The sidecar lands last, so a reader that sees it gets a whole WAV.
        t = threading.Thread(target=self._write, args=(name, pcm, meta), name="tx-writer", daemon=True)
        with self._mu:
            self._writers = [w for w in self._writers if w.is_alive()] + [t]
        t.start()

    def _write(self, name: str, pcm: np.ndarray, meta: dict) -> None:
        wav, side = self.tx_dir / f"{name}.wav", self.tx_dir / f"{name}.json"
        tmp = wav.with_suffix(".wav.tmp")
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(np.ascontiguousarray(pcm, dtype="<i2").tobytes())
        os.replace(tmp, wav)
        tmp = side.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=1) + "\n")
        os.replace(tmp, side)
        note(f"tx recorded {wav.name} ({meta['samples'] / SAMPLE_RATE:.2f} s, owner {meta['owner']})")

    def close(self) -> None:
        with self._mu:
            cur, self._cur = self._cur, None
        if cur is not None:
            if self.vox:
                cur["frames"] = cur["frames"][:cur["last_ns"]]
            self._finish(cur, cur["last_ns_end"] if self.vox else time.time())
        for w in list(self._writers):
            w.join(timeout=5)


# ---- the sound card -------------------------------------------------------------------------------

class WavBackend(AudioBackend):
    """Drop-in for the bridge's FakeBackend: the same paced read()/write() of 10 ms frames, but capture
    plays the RX schedule on the wall clock's 10 ms grid, so a slot's sample 0 starts its frame."""

    def __init__(self, cfg, schedule: RxSchedule, slots: SlotLog, noise_rms: float, recorder: TxRecorder) -> None:
        self._gain = {"tx": cfg.tx_gain, "rx": cfg.rx_gain}
        self.schedule, self.slots, self.noise_rms, self.recorder = schedule, slots, noise_rms, recorder
        self._k: int | None = None  # next capture frame, as an index on the UTC 10 ms grid
        self._last_slot: int | None = None
        self._slot_file: str | None = None
        self._write_next = 0.0
        self._down_until = 0.0
        self._rng = np.random.default_rng()

    def fail_for(self, seconds: float) -> None:
        self._down_until = time.monotonic() + seconds

    def _check_up(self) -> None:
        if time.monotonic() < self._down_until:
            raise AudioDeviceError("simulated: the USB audio device is unplugged")

    def open(self) -> None:
        self._check_up()
        self._k = None
        self._write_next = time.monotonic()

    def close(self) -> None:
        pass

    def read(self) -> np.ndarray:
        self._check_up()
        now = time.time()
        if self._k is None or now - (self._k + 1) * FRAME_S > 0.5:
            self._k = int(now // FRAME_S)  # (re)join the grid at the frame now being captured
        delay = (self._k + 1) * FRAME_S - time.time()
        if delay > 0:
            time.sleep(delay)
        k, self._k = self._k, self._k + 1
        return self._frame(k)

    def _noise(self) -> np.ndarray:
        if self.noise_rms <= 0:
            return np.zeros(FRAME_SAMPLES, np.int16)
        return np.clip(np.rint(self._rng.normal(0, self.noise_rms, FRAME_SAMPLES)), -32768, 32767).astype(np.int16)

    def _frame(self, k: int) -> np.ndarray:
        slot_s = (k // FRAMES_PER_SLOT) * SLOT_S
        if slot_s != self._last_slot:
            self._last_slot = slot_s
            self._slot_file = self.schedule.file_for(slot_s)
            self.slots.put({"slot_utc": utc_iso(slot_s, ms=False), "rx_file": self._slot_file})  # no I/O here
        data = self.schedule.audio.get(self._slot_file) if self._slot_file else None
        off = (k % FRAMES_PER_SLOT) * FRAME_SAMPLES
        if data is None or off >= len(data):
            return self._noise()
        seg = data[off:off + FRAME_SAMPLES]
        if len(seg) == FRAME_SAMPLES:
            return seg.copy()
        pcm = self._noise()
        pcm[:len(seg)] = seg
        return pcm

    def write(self, pcm: np.ndarray) -> None:
        self._check_up()
        t = self._write_next + FRAME_S  # FakeBackend's pacing
        delay = t - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -0.5:
            t = time.monotonic()
        self._write_next = t
        self.recorder.on_frame(np.asarray(pcm, dtype=np.int16), time.time())

    def set_gain(self, tx=None, rx=None) -> dict:
        if tx is not None:
            self._gain["tx"] = int(max(0, min(100, tx)))
        if rx is not None:
            self._gain["rx"] = int(max(0, min(100, rx)))
        return dict(self._gain)

    def describe(self) -> str:
        return "bridge_sim:wav"


# ---- the rig, the wizard, the battery -------------------------------------------------------------

class SimRig:
    """CAT for the Ladder, PttController and readiness, after tests/test_ladder.py FakeRig. ALC sits just
    below its onset when keyed, so Drive passes at once; there is no SWR, so the session is capped."""

    def __init__(self, recorder: TxRecorder) -> None:
        self.recorder = recorder
        self.keyed = False
        self.freq, self.mode = 14_074_000, "PKTUSB"
        self.levels = {"RFPOWER": 0.8}
        self.cat_error = False
        self._mu = threading.Lock()

    def _check(self) -> None:
        if self.cat_error:
            raise CatError("simulated: the radio stopped answering on CAT")

    def set_ptt(self, on: bool) -> None:
        # PTT keeps working under rig_cat_error, so a fault can never leave the sim stuck in transmit.
        with self._mu:
            was, self.keyed = self.keyed, bool(on)
        if on and not was:
            self.recorder.key()
        elif was and not on:
            self.recorder.unkey()

    def get_ptt(self) -> bool:
        return self.keyed

    def alive(self) -> bool:
        return not self.cat_error

    def get_level(self, name: str) -> float:
        self._check()
        if name == "ALC":
            return 0.05 if self.keyed else 0.0
        if name == "VD_METER":
            return 13.6 if self.keyed else 13.8
        if name == "RFPOWER_METER_WATTS":
            return 10.0 * self.levels["RFPOWER"] if self.keyed else 0.0
        if name not in self.levels:
            raise CatError(f"no {name}")
        return self.levels[name]

    def set_level(self, name: str, value: float) -> None:
        self._check()
        self.levels[name] = value

    def get_freq(self) -> int:
        self._check()
        return self.freq

    def set_freq(self, hz: int) -> None:
        self._check()
        self.freq = int(hz)

    def get_mode(self) -> str:
        self._check()
        return self.mode

    def set_mode(self, mode: str) -> None:
        self._check()
        self.mode = mode


def sim_profile(no_rig: bool) -> RadioProfile:
    if no_rig:
        return RadioProfile(name="sim-vox", display_name="Simulated radio (VOX, no CAT)", rig_model=0,
                            ptt_method="vox", can_report_swr=False, can_report_alc=False, builtin=False)
    # rig_model 1 is Hamlib's dummy rig; no SWR over CAT, so the ladder caps each transmission at 15 s.
    return RadioProfile(name="sim", display_name="Simulated radio (CAT)", rig_model=1, ptt_method="cat",
                        can_report_swr=False, can_report_alc=True, min_power_w=0.5, builtin=False)


class SimWizard:
    """Just enough of wizard.Wizard for the Ladder, TunerApp and GET /api/wizard."""

    def __init__(self, prof: RadioProfile) -> None:
        self.prof = prof
        self.state = types.SimpleNamespace(profile=prof.name, interface=prof.interface,
                                           ptt_method=prof.ptt_method, kit="")

    def profile(self) -> RadioProfile:
        return self.prof

    def report(self) -> dict:
        return {"ok": True, "sim": True, "note": "bridge_sim wizard stub: setup is fixed",
                "profile": self.prof.as_dict(), "state": vars(self.state), "done": True}

    def reset(self) -> None:
        pass


class SimTunerApp(tuner.TunerApp):
    def wizard_action(self, body: dict) -> tuple[int, dict]:
        return 200, {**self.wizard.report(), "ignored": body.get("action")}


class SimBattery:
    def __init__(self) -> None:
        self.locked = False

    def report(self) -> dict:
        return {"ok": True, "present": True, "model": "bridge_sim", "percent": 5 if self.locked else 80,
                "plugged": False}


# ---- faults ---------------------------------------------------------------------------------------

class Takeover:
    """Another client: a hello under its name from the harness's own socket, kept alive every second."""

    def __init__(self, ctl_addr: tuple) -> None:
        self.ctl_addr = ctl_addr  # (address the service is bound to, or 127.0.0.1, control port)
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self, client: str, seconds: float | None) -> None:
        self.stop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.ctl_addr[0], 0))  # same address as the service: loopback cannot reach a LAN bind
        hello = json.dumps({"cmd": "hello", "client": client}).encode()
        sock.settimeout(2)
        try:
            sock.sendto(hello, self.ctl_addr)
            while True:  # RX may already be arriving; wait for the control reply
                _, addr = sock.recvfrom(4096)
                if addr[1] == self.ctl_addr[1]:
                    break
        except OSError as e:
            sock.close()
            raise SimError(f"the audio service did not answer the takeover hello: {e!r}") from e
        stop = threading.Event()
        deadline = time.monotonic() + seconds if seconds else math.inf

        def run():
            sock.setblocking(False)
            next_hello = time.monotonic() + 1
            try:
                while not stop.is_set() and time.monotonic() < deadline:
                    if time.monotonic() >= next_hello:
                        sock.sendto(hello, self.ctl_addr)
                        next_hello += 1
                    r, _, _ = select.select([sock], [], [], 0.1)
                    while r:
                        try:
                            sock.recvfrom(4096)  # drain RX and replies
                        except BlockingIOError:
                            break
            finally:
                sock.close()

        self._stop, self._thread = stop, threading.Thread(target=run, name="takeover", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
            self._thread.join(timeout=2)
            self._stop = self._thread = None


class TakeoverKey:
    """A key through the real Ladder under another owner, heartbeated until it ends."""

    def __init__(self, lad) -> None:
        self.lad = lad
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self.owner = ""

    def start(self, owner: str, seconds: float) -> tuple[bool, str]:
        self.stop()
        code, out = ladder_mod.handle(self.lad, {"action": "key", "owner": owner})
        if code != 200:
            return False, out.get("error") or f"key refused ({code}); tx owner {self.lad.ptt.lock.owner}"
        stop, deadline = threading.Event(), time.monotonic() + seconds
        self.owner = owner

        def run():
            while not stop.wait(0.5) and time.monotonic() < deadline:
                ladder_mod.handle(self.lad, {"action": "heartbeat", "owner": owner})
            ladder_mod.handle(self.lad, {"action": "unkey", "owner": owner})

        self._stop, self._thread = stop, threading.Thread(target=run, name="takeover-key", daemon=True)
        self._thread.start()
        return True, ""

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
            self._thread.join(timeout=3)
            self._stop = self._thread = None


class Faults:
    def __init__(self, backend: WavBackend, battery: SimBattery, rig: SimRig | None, takeover: Takeover,
                 takeover_key: TakeoverKey | None) -> None:
        self.backend, self.battery, self.rig = backend, battery, rig
        self.takeover, self.takeover_key = takeover, takeover_key

    def apply(self, body: dict) -> tuple[int, dict]:
        fault = body.get("fault")

        def num(key, default=None):
            v = body.get(key, default)
            if v is None:
                return None
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise SimError(f"{key} must be a number of seconds")
            return float(v)

        def flag():
            if not isinstance(body.get("on"), bool):
                raise SimError("give \"on\": true or false")
            return body["on"]

        try:
            if fault == "takeover":
                client = body.get("client")
                if not isinstance(client, str) or not client:
                    raise SimError("give \"client\": the name the other client says hello with")
                self.takeover.start(client, num("seconds"))
            elif fault == "takeover_end":
                self.takeover.stop()
            elif fault in ("takeover-key", "takeover-key_end"):
                if self.takeover_key is None:
                    raise SimError("no rig control under --no-rig")
                if fault == "takeover-key_end":
                    self.takeover_key.stop()
                else:
                    owner = body.get("owner")
                    if not isinstance(owner, str) or not owner:
                        raise SimError("give \"owner\": the other client's ladder owner name")
                    ok, err = self.takeover_key.start(owner, num("seconds", 10.0))
                    if not ok:
                        return 409, {"ok": False, "error": err}
            elif fault == "battery_tx_locked":
                self.battery.locked = flag()
            elif fault == "audio_device_down":
                secs = num("seconds")
                if not secs:
                    raise SimError("give \"seconds\" greater than 0")
                self.backend.fail_for(secs)
            elif fault == "rig_cat_error":
                if self.rig is None:
                    raise SimError("no rig control under --no-rig")
                self.rig.cat_error = flag()
            else:
                raise SimError(f"unknown fault {fault!r}")
        except SimError as e:
            return 400, {"ok": False, "error": str(e)}
        except OSError as e:
            return 500, {"ok": False, "error": repr(e)}
        note(f"fault {json.dumps(body)}")
        return 200, {"ok": True}


def serve_faults(faults: Faults, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path != "/sim/fault":
                return self._send(404, {"ok": False, "error": "not_found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._send(400, {"ok": False, "error": "bad_json"})
            if not isinstance(body, dict):
                return self._send(400, {"ok": False, "error": "bad_json"})
            self._send(*faults.apply(body))

        def do_GET(self):
            self._send(404, {"ok": False, "error": "POST /sim/fault"})

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # localhost only, whatever --bind says
    httpd.daemon_threads = True
    return httpd


# ---- startup --------------------------------------------------------------------------------------

def pass_gates(lad) -> None:
    """The gates through the real ladder API, as an operator with a dummy load would."""
    for body in ({"action": "confirm_load", "load": "dummy"}, {"action": "drive"}, {"action": "carrier"},
                 {"action": "meter", "ok": True}):
        code, out = ladder_mod.handle(lad, body)
        res = out.get("result")
        if code != 200 or (res is not None and not res.get("ok")):
            raise SimError(f"gate pre-pass {body['action']} failed ({code}): {(res or {}).get('detail') or out.get('error')}")
    if not lad.tx_allowed():
        raise SimError("gate pre-pass finished but transmit is still locked")


REPO = Path(__file__).resolve().parents[2]  # this file is <repo>/tools/bridge_sim/sim.py


def git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", "--no-optional-locks", "-C", str(REPO), *args],
                             capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def checkout_commit() -> str | None:
    """HEAD of this checkout, short, with "-dirty" when tracked files differ from it; None without git."""
    head = git("rev-parse", "--short", "HEAD")
    if head is None:
        return None
    changes = git("status", "--porcelain", "--untracked-files=no")
    if changes is None:
        return None
    return head + ("-dirty" if changes else "")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="run.sh", description="VectorBridge simulator for client tests (see README.md)")
    p.add_argument("--rx-dir", required=True, type=Path, help="mono int16 WAVs, 12 or 48 kHz")
    p.add_argument("--tx-dir", required=True, type=Path, help="TX recordings and slots.jsonl go here")
    p.add_argument("--http-port", type=int, default=8080)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--fault-port", type=int, default=8081)
    p.add_argument("--audio-port", type=int, default=5100)
    p.add_argument("--control-port", type=int, default=5101)
    p.add_argument("--gates-locked", action="store_true", help="leave the transmit gates locked")
    p.add_argument("--no-rig", action="store_true", help="no CAT (rig_model 0): VOX, /api/ladder answers 503")
    p.add_argument("--loop", action="store_true", help="play the RX files in sorted order, one per slot, wrapping")
    p.add_argument("--noise-rms", type=float, default=DEFAULT_NOISE_RMS,
                   help="band noise where no file plays (0 = digital silence, which turns readiness rx_audio red)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    src = Path(vector_bridge.__file__).resolve()
    if not src.is_relative_to(REPO / "src"):
        raise SimError(f"vector_bridge loaded from {src}, not from {REPO / 'src'}; start the sim with run.sh")
    commit = checkout_commit()
    if not args.rx_dir.is_dir():
        raise SimError(f"--rx-dir {args.rx_dir} is not a directory")
    args.tx_dir.mkdir(parents=True, exist_ok=True)

    # The bridge logs to stdout; here stdout carries only JSON lines, so its log goes to stderr. Every
    # stderr write is queued (QueuedStream): a slow or closed stderr can never block a bridge thread.
    sys.stderr = QueuedStream(STDERR)
    bridge_log.sys = types.SimpleNamespace(stdout=sys.stderr)
    import logging
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(name)s %(levelname)s %(message)s")
    # Off a Pi: no NetworkManager to ask whether the access point is open, no /run.
    apsetup.ap_profile_is_open = lambda: False
    wifiradio.LOCK, wifiradio.SKIP = STATE_DIR / "vb-wifi-radio.lock", STATE_DIR / "vb-wifi-radio.skip"

    schedule = RxSchedule(args.rx_dir, args.loop)
    cfg_path = Path(os.environ["VECTOR_BRIDGE_CONFIG"])
    cfg = configmod.Config(audio_port=args.audio_port, control_port=args.control_port)
    configmod.save(cfg, cfg_path)
    svc = service_mod.BridgeService(cfg, audio="fake", bind=args.bind, config_path=cfg_path)
    ptt: PttController | None = None
    recorder = TxRecorder(args.tx_dir, vox=args.no_rig, telemetry_fn=svc.telemetry.as_dict,
                          owner_fn=lambda: ptt.lock.owner if ptt else None)
    slots = SlotLog(args.tx_dir / "slots.jsonl")
    backend = WavBackend(cfg, schedule, slots, args.noise_rms, recorder)
    svc.backend = backend  # before start(): the service opens and reads only this one

    on_audio = svc._on_audio

    def tapped_on_audio(data: bytes, addr: tuple) -> None:  # the service's UDP TX intake
        wall, mono = time.time(), time.monotonic()
        try:
            pkt = decode(data)
        except ProtocolError:
            pkt = None
        if pkt is not None and pkt.direction == DIR_TX and len(pkt.pcm) and not svc._is_monitor(addr):
            recorder.on_arrival(pkt.pcm, wall, mono)
        on_audio(data, addr)

    svc._on_audio = tapped_on_audio
    svc.start()
    ctl_host = "127.0.0.1" if args.bind in ("0.0.0.0", "", "127.0.0.1") else args.bind

    def control(req):
        return control_request(req, host=ctl_host, port=svc.control_port, timeout=tuner.control_timeout(req))

    prof = sim_profile(args.no_rig)
    wiz = SimWizard(prof)
    battery = SimBattery()
    rig = lad = monitor = None
    if not args.no_rig:
        rig = SimRig(recorder)
        ptt = PttController(rig.set_ptt, get_ptt=rig.get_ptt)
        ptt.start()
        lad = ladder_mod.Ladder(rig, ptt, control, wiz.profile,
                                identity_fn=lambda: (wiz.state.profile, wiz.state.interface, wiz.state.ptt_method, wiz.state.kit),
                                rx_ok_fn=lambda: monitor.state_of("rx_audio") != readiness.FAIL,
                                battery_fn=battery.report)
        monitor = readiness.Monitor(readiness.make_gather(control, cat=rig, ladder=lad.hook, vox_fn=lambda: False,
                                                          no_cat_fn=lambda: False))
    else:
        monitor = readiness.Monitor(readiness.make_gather(control, cat=None, vox_fn=lambda: True,
                                                          no_cat_fn=lambda: True))
    monitor.start()
    if lad is not None and not args.gates_locked:
        pass_gates(lad)

    admin = admin_mod.Admin(path=STATE_DIR / "persist" / "vector-bridge" / "admin-pin.json")
    code, out = admin.set_pin({"pin": SIM_PIN}, False)
    if code != 200:
        raise SimError(f"could not seed the admin PIN: {out}")
    app = SimTunerApp(control, mixer_fn=lambda card: [], monitor=monitor, wiz=wiz, ladder=lad,
                      battery_fn=battery.report, mode_fn=lambda: None,
                      config_problem_fn=lambda: configmod.problem(cfg_path), device_id_fn=lambda: SIM_DEVICE_ID)
    httpd = tuner.serve(app, bind=args.bind, port=args.http_port, admin=admin)
    takeover = Takeover((ctl_host, svc.control_port))
    takeover_key = TakeoverKey(lad) if lad is not None else None
    faultd = serve_faults(Faults(backend, battery, rig, takeover, takeover_key), args.fault_port)
    for srv, name in ((httpd, "http"), (faultd, "fault")):
        threading.Thread(target=srv.serve_forever, name=name, daemon=True).start()
    recorder.enabled = True

    max_tx = lad.report()["max_tx_s"] if lad is not None else None
    if isinstance(max_tx, float) and max_tx.is_integer():
        max_tx = int(max_tx)
    schedule.start(time.time())
    shown = "127.0.0.1" if args.bind in ("0.0.0.0", "") else args.bind
    slots.ready({"ready": True, "bridge_commit": commit, "bridge_version": vector_bridge.__version__,
          "http": f"http://{shown}:{httpd.server_address[1]}",
          "audio_port": svc.audio_port, "control_port": svc.control_port,
          "fault": f"http://127.0.0.1:{faultd.server_address[1]}", "pin": SIM_PIN, "device_id": SIM_DEVICE_ID,
          "max_tx_s": max_tx, "rig": "none" if args.no_rig else "sim"})

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.wait(0.5):
        pass

    note("stopping")
    steps = [httpd.shutdown, httpd.server_close, faultd.shutdown, faultd.server_close, takeover.stop]
    if takeover_key is not None:
        steps.append(takeover_key.stop)
    if ptt is not None:
        steps.append(ptt.stop)  # unkeys whoever holds PTT, which closes their recording
    steps += [recorder.close, monitor.stop, svc.stop, slots.close]
    for step in steps:
        try:
            step()
        except Exception as e:  # noqa: BLE001 - every step runs, so the ports are always freed
            note(f"shutdown step {getattr(step, '__qualname__', step)} failed: {e!r}")
    if slots.dropped or STDOUT.dropped or STDERR.dropped or STDOUT.dead:
        note(f"output dropped: slot lines {slots.dropped}, stdout {STDOUT.dropped}"
             f"{' (stdout closed)' if STDOUT.dead else ''}, stderr {STDERR.dropped}")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except SimError as e:
        note(f"error: {e}")
        rc = 2
    except SystemExit:  # argparse --help (0) and usage errors (2) keep their own exit codes
        raise
    except BaseException:  # noqa: BLE001 - print it through the queued stderr before the writers stop
        import traceback
        traceback.print_exc()
        rc = 1
    finally:
        shutil.rmtree(STATE_DIR, ignore_errors=True)
        STDOUT.close()
        STDERR.close()
    sys.exit(rc)
