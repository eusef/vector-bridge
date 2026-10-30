"""Backend interface: blocking read/write of one 10 ms frame at a time."""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class AudioDeviceError(RuntimeError):
    """Raised when the device is gone or ALSA fails; the supervisor reopens on this."""


class AudioBackend(ABC):
    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def read(self) -> np.ndarray:
        """Block until one frame (FRAME_SAMPLES int16) is captured and return it."""

    @abstractmethod
    def write(self, pcm: np.ndarray) -> None:
        """Block until one frame is queued for playback."""

    @abstractmethod
    def set_gain(self, tx: int | None = None, rx: int | None = None) -> dict:
        """Apply mixer levels (0-100). Returns {'tx': .., 'rx': ..} as applied."""

    @abstractmethod
    def describe(self) -> str:
        """Human-readable device identity for telemetry."""

    @property
    def tx_soft(self) -> bool:
        """True when the card has no playback volume, so tx_gain must scale the samples."""
        return False

    @property
    def rx_fixed(self) -> bool:
        """True when the card has no capture volume, so rx_gain changes nothing: the radio sets the level."""
        return False

    @property
    def playback_buffer_ms(self) -> int:
        """Audio the device still holds after the last write(): what an unkey must wait for."""
        return 0

    @property
    def overruns(self) -> int:
        return 0

    @property
    def underruns(self) -> int:
        return 0
