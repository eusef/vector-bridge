"""Transmit audio cap: a VOX rig keys on the audio itself, so audio that never pauses keys it forever.

The cap counts how long the played audio has run without a pause. Past the cap it plays
silence until the source has been quiet for QUIET_RESET_S, so a stuck client or a stuck
tone leaves the rig unkeyed instead of keying it again. A short gap does not reset the
count: the rig's VOX hang holds it keyed through one. 0 turns the cap off.
"""

from __future__ import annotations

import numpy as np

from .protocol import SAMPLE_RATE

DEFAULT_CAP_S = 120  # covers FT8, FT4, every JS8 speed, JT65/JT9 (about 50 s) and WSPR (about 110 s); shared with the rigctl key cap
MAX_CAP_S = 600
LOUD_PEAK = 100  # about -50 dBFS; a frame below this counts as a pause
QUIET_RESET_S = 1.0


class TxAudioCap:
    def __init__(self, cap_s: int = DEFAULT_CAP_S) -> None:
        self.cap_s = cap_s
        self.muted = False
        self.trips = 0
        self._run = 0.0
        self._quiet = 0.0

    def apply(self, pcm: np.ndarray, peak: int) -> tuple[np.ndarray, bool]:
        """The frame to play, and True on the frame where the cap trips."""
        dt = len(pcm) / SAMPLE_RATE
        self._quiet = 0.0 if peak >= LOUD_PEAK else self._quiet + dt
        if self._quiet >= QUIET_RESET_S:
            self._run = 0.0
            self.muted = False
        else:
            self._run += dt
        if self.cap_s <= 0:
            self.muted = False
        tripped = False
        if not self.muted and self.cap_s > 0 and self._run >= self.cap_s:
            self.muted = True
            self.trips += 1
            tripped = True
        return (np.zeros_like(pcm) if self.muted else pcm), tripped
