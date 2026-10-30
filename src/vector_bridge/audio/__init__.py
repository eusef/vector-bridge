"""Audio backends. `fake` needs no hardware; `alsa` is the Pi backend."""

from .base import AudioBackend, AudioDeviceError


def open_backend(name: str, config) -> AudioBackend:
    if name == "fake":
        from .fake import FakeBackend

        return FakeBackend(config)
    if name == "alsa":
        from .alsa import AlsaBackend

        return AlsaBackend(config)
    raise ValueError(f"unknown audio backend {name!r}")


__all__ = ["AudioBackend", "AudioDeviceError", "open_backend"]
