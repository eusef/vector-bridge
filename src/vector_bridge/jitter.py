"""TX jitter buffer: reorders incoming packets by sequence, conceals gaps with silence."""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from .protocol import FRAME_MS, FRAME_SAMPLES, SEQ_MOD, AudioPacket, seq_delta


@dataclass
class JitterStats:
    received: int = 0
    lost: int = 0  # gaps filled with silence
    late: int = 0  # arrived after we already moved past them
    duplicate: int = 0
    reordered: int = 0
    underruns: int = 0  # pop() found nothing after the buffer was primed
    reset: int = 0  # sequence jumps that forced a restart
    depth: int = 0  # packets currently queued

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class JitterBuffer:
    """Thread-safe. push() from the network thread, pop() from the playback thread.

    Playback is held until `target_ms` of audio is queued (priming). After that,
    pop() always returns exactly FRAME_SAMPLES samples: the next expected packet,
    or silence when it is missing (counted as lost) or the buffer is empty
    (counted as an underrun, then re-primes).
    """

    def __init__(self, target_ms: int = 30, max_ms: int = 40) -> None:
        self.target = max(1, target_ms // FRAME_MS)
        self.max_depth = max(self.target, max_ms // FRAME_MS)
        self._lock = threading.Lock()
        self._pending: dict[int, np.ndarray] = {}
        self._next: int | None = None
        self._primed = False
        self.stats = JitterStats()
        self._silence = np.zeros(FRAME_SAMPLES, dtype=np.int16)

    def push(self, pkt: AudioPacket) -> None:
        with self._lock:
            self.stats.received += 1
            if self._next is None:
                self._next = pkt.sequence
            d = seq_delta(pkt.sequence, self._next)
            if abs(d) > self.max_depth * 4:
                # far from where we are, either direction: the sender restarted. Start over here.
                self.stats.reset += 1
                self._pending.clear()
                self._next = pkt.sequence
                self._primed = False
                d = 0
            if d < 0:
                self.stats.late += 1
                return
            if pkt.sequence in self._pending:
                self.stats.duplicate += 1
                return
            if self._pending and any(seq_delta(pkt.sequence, s) < 0 for s in self._pending):
                self.stats.reordered += 1
            self._pending[pkt.sequence] = pkt.pcm
            # never let the queue grow past max_depth: drop the oldest audio and skip ahead
            while len(self._pending) > self.max_depth:
                oldest = min(self._pending, key=lambda s: seq_delta(s, self._next))
                del self._pending[oldest]
                # the dropped packet plus every missing sequence we skipped over
                self.stats.lost += 1 + seq_delta(oldest, self._next)
                self._next = (oldest + 1) % SEQ_MOD
            if len(self._pending) >= self.target:
                self._primed = True
            self.stats.depth = len(self._pending)

    def reset(self) -> None:
        """Forget the previous sender's position: a new peer starts its own sequence."""
        with self._lock:
            self._pending.clear()
            self._next = None
            self._primed = False
            self.stats.depth = 0

    def pop(self) -> np.ndarray:
        with self._lock:
            if not self._primed or self._next is None:
                return self._silence
            pcm = self._pending.pop(self._next, None)
            if pcm is None:
                if not self._pending:
                    self.stats.underruns += 1
                    self._primed = False
                    return self._silence
                self.stats.lost += 1
                pcm = self._silence
            self._next = (self._next + 1) % SEQ_MOD
            self.stats.depth = len(self._pending)
            if len(pcm) != FRAME_SAMPLES:
                out = np.zeros(FRAME_SAMPLES, dtype=np.int16)
                n = min(len(pcm), FRAME_SAMPLES)
                out[:n] = pcm[:n]
                pcm = out
            return pcm

    @property
    def primed(self) -> bool:
        return self._primed
