"""Fake backend: paced 1 kHz tone on capture, paced discard on playback. For tests and Mac runs."""

from __future__ import annotations

import time

import numpy as np

from ..protocol import FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE
from .base import AudioBackend


class FakeBackend(AudioBackend):
    TONE_HZ = 1000
    AMPLITUDE = 8000

    def __init__(self, config, paced: bool = True) -> None:
        self.paced = paced
        self._gain = {"tx": config.tx_gain, "rx": config.rx_gain}
        self._phase = 0
        self._next = {"read": 0.0, "write": 0.0}
        self._open = False
        self.frames_written = 0
        self.last_written: np.ndarray | None = None

    def open(self) -> None:
        self._open = True
        now = time.monotonic()
        self._next = {"read": now, "write": now}

    def close(self) -> None:
        self._open = False

    def _pace(self, key: str) -> None:
        t = self._next[key] + FRAME_MS / 1000
        delay = t - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -0.5:
            t = time.monotonic()
        self._next[key] = t

    def read(self) -> np.ndarray:
        if self.paced:
            self._pace("read")
        n = np.arange(self._phase, self._phase + FRAME_SAMPLES)
        self._phase = (self._phase + FRAME_SAMPLES) % SAMPLE_RATE
        tone = self.AMPLITUDE * np.sin(2 * np.pi * self.TONE_HZ * n / SAMPLE_RATE)
        return tone.astype(np.int16)

    def write(self, pcm: np.ndarray) -> None:
        if self.paced:
            self._pace("write")
        self.frames_written += 1
        self.last_written = pcm

    def set_gain(self, tx=None, rx=None) -> dict:
        if tx is not None:
            self._gain["tx"] = int(max(0, min(100, tx)))
        if rx is not None:
            self._gain["rx"] = int(max(0, min(100, rx)))
        return dict(self._gain)

    def describe(self) -> str:
        return "fake:1kHz-tone"
