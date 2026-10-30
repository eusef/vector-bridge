"""Runtime telemetry (proposal section 16) plus per-block level measurement."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .protocol import FRAME_MS, SAMPLE_RATE

CLIP_LEVEL = 32700  # samples at or beyond this magnitude count as clipping
# An ADC that overloads and is then attenuated by the mixer arrives below CLIP_LEVEL with a
# flat top: many samples at exactly the block peak. Live audio, even a pure tone, lands on
# its exact peak value only a few times per block. Count such a block as clipped too.
FLAT_TOP_MIN_PEAK = 1000  # ignore silence and noise floors
FLAT_TOP_FRACTION = 0.05  # 5 % of samples (24 of a 480-sample frame) sitting on the peak


@dataclass
class Levels:
    peak: int = 0
    rms: int = 0
    clips: int = 0


def measure(pcm: np.ndarray) -> Levels:
    """Peak, RMS and clip count of one int16 block, vectorized."""
    if pcm.size == 0:
        return Levels()
    x = pcm.astype(np.int32)
    ax = np.abs(x)
    peak = int(ax.max())
    rms = int(np.sqrt(np.mean(x * x)))
    clips = int(np.count_nonzero(ax >= CLIP_LEVEL))
    if clips == 0 and peak >= FLAT_TOP_MIN_PEAK:
        flat = int(np.count_nonzero(ax == peak))
        if flat >= FLAT_TOP_FRACTION * ax.size:
            clips = flat
    return Levels(peak, rms, clips)


class SendMeter:
    """One transmit send (client audio or the test tone) until 1 s without either, so the
    level the card got can be read from the journal without keying a radio. A jitter
    underrun mid-send does not split it."""

    QUIET_FRAMES = 1000 // FRAME_MS

    def __init__(self) -> None:
        self.frames = self.peak = self.clips = self._quiet = 0
        self._sq = 0.0

    def add(self, lv: Levels) -> None:
        self._quiet = 0
        self.frames += 1
        self.peak = max(self.peak, lv.peak)
        self.clips += lv.clips
        self._sq += float(lv.rms) ** 2

    def idle(self) -> dict | None:
        """A frame with nothing sent. Returns the send's summary once it has been quiet for 1 s."""
        if not self.frames:
            return None
        self._quiet += 1
        if self._quiet < self.QUIET_FRAMES:
            return None
        rms = (self._sq / self.frames) ** 0.5
        out = {"frames": self.frames, "seconds": round(self.frames * FRAME_MS / 1000, 2), "peak": self.peak,
               "peak_pct_fs": round(100 * self.peak / 32767, 1),
               "rms_dbfs": round(20 * np.log10(rms / 32768), 1) if rms else None, "clips": self.clips}
        self.__init__()
        return out


@dataclass
class Telemetry:
    """Counters shared between threads; plain ints written under one lock."""

    rx_packets_sent: int = 0
    tx_packets_received: int = 0
    rx_packet_loss: int = 0  # RX packets we failed to send (the client counts what it missed)
    tx_packet_loss: int = 0  # jitter buffer gaps filled with silence
    tx_late: int = 0
    tx_reordered: int = 0
    rx_overruns: int = 0  # ALSA capture overruns
    tx_underruns: int = 0  # jitter buffer starved + ALSA playback underruns
    network_jitter_ms: float = 0.0  # EWMA of |inter-arrival - 10 ms| on TX packets
    rx_peak: int = 0
    tx_peak: int = 0
    rx_rms: int = 0
    tx_rms: int = 0
    clip_count: int = 0
    sample_rate: int = SAMPLE_RATE
    alsa_device: str = ""
    peer: str | None = None
    device_reopens: int = 0
    audio_device_ok: bool = False
    audio_device_error: str | None = "not opened yet"
    audio_device_since: int = field(default_factory=lambda: int(time.time()))  # Unix s of the last ok change
    jitter_depth: int = 0
    started: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_tx_arrival: float | None = field(default=None, repr=False)

    def note_tx_arrival(self, now: float) -> float:
        """Update network jitter from a TX packet arrival; returns this packet's deviation in ms."""
        with self._lock:
            dev = 0.0
            if self._last_tx_arrival is not None:
                dev = abs((now - self._last_tx_arrival) * 1000 - FRAME_MS)
                self.network_jitter_ms += (dev - self.network_jitter_ms) / 16
            self._last_tx_arrival = now
            return dev

    def forget_tx_arrival(self) -> None:
        with self._lock:
            self._last_tx_arrival = None

    def update(self, **deltas: int) -> None:
        with self._lock:
            for k, v in deltas.items():
                setattr(self, k, getattr(self, k) + v)

    def set(self, **values) -> None:
        with self._lock:
            for k, v in values.items():
                setattr(self, k, v)

    def set_device(self, ok: bool, error: str | None = None) -> None:
        """Audio device health for status: `since` moves only when ok flips, the error tracks the latest try."""
        with self._lock:
            if ok != self.audio_device_ok:
                self.audio_device_since = int(time.time())
            self.audio_device_ok, self.audio_device_error = ok, None if ok else error

    @property
    def uptime_s(self) -> int:
        return int(time.monotonic() - self.started)

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "rx_packets_sent": self.rx_packets_sent,
                "tx_packets_received": self.tx_packets_received,
                "rx_packet_loss": self.rx_packet_loss,
                "tx_packet_loss": self.tx_packet_loss,
                "tx_late": self.tx_late,
                "tx_reordered": self.tx_reordered,
                "rx_overruns": self.rx_overruns,
                "tx_underruns": self.tx_underruns,
                "network_jitter_ms": round(self.network_jitter_ms, 2),
                "jitter_depth": self.jitter_depth,
                "rx_peak": self.rx_peak,
                "tx_peak": self.tx_peak,
                "rx_rms": self.rx_rms,
                "tx_rms": self.tx_rms,
                "clip_count": self.clip_count,
                "sample_rate": self.sample_rate,
                "alsa_device": self.alsa_device,
                "peer": self.peer,
                "device_reopens": self.device_reopens,
                "audio_device": {"ok": self.audio_device_ok, "error": self.audio_device_error,
                                 "since": self.audio_device_since},
                "uptime_s": self.uptime_s,
            }
