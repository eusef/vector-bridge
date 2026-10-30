"""The bridge's own test tone, played in place of client audio while the ladder keys (HANDOFF D8).

It lets the drive and carrier checks run with no client app connected. It ends at its
deadline even if the stop never arrives, so a lost control datagram cannot leave it playing.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

import numpy as np

from .protocol import FRAME_SAMPLES, SAMPLE_RATE

PEAK = 29490  # 0.9 of full scale: the level of the client's own tune tone (tx_rms 20852 on the bench)
MIN_HZ, MAX_HZ = 200, 3000
MAX_S = 30.0


class ToneSource:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._mu = threading.Lock()
        self._hz = 0.0
        self._until = 0.0
        self._phase = 0.0

    def start(self, hz: float, seconds: float) -> None:
        with self._mu:
            if self._clock() >= self._until:
                self._phase = 0.0  # a fresh tone; a running one keeps its phase so a refresh does not click
            self._hz, self._until = float(hz), self._clock() + seconds

    def stop(self) -> None:
        with self._mu:
            self._until = 0.0

    def frame(self) -> np.ndarray | None:
        """The next 10 ms of tone, or None when no tone is playing."""
        with self._mu:
            if self._clock() >= self._until:
                return None
            step = 2 * math.pi * self._hz / SAMPLE_RATE
            out = (PEAK * np.sin(self._phase + step * np.arange(FRAME_SAMPLES))).astype(np.int16)
            self._phase = (self._phase + step * FRAME_SAMPLES) % (2 * math.pi)
            return out
