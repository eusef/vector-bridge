"""Wire protocol for VectorBridge audio packets. See DOCS/protocol.md."""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

MAGIC = b"VBRG"
VERSION = 1
DIR_RX = 0  # Pi -> client: radio audio
DIR_TX = 1  # client -> Pi: audio for the radio

SAMPLE_RATE = 48_000
CHANNELS = 1
FRAME_SAMPLES = 480  # 10 ms at 48 kHz
FRAME_MS = 10
DEFAULT_AUDIO_PORT = 5100
DEFAULT_CONTROL_PORT = 5101

HEADER = struct.Struct("<4sBBIQH")
HEADER_SIZE = HEADER.size  # 20
SEQ_MOD = 1 << 32


class ProtocolError(ValueError):
    pass


@dataclass(slots=True)
class AudioPacket:
    direction: int
    sequence: int
    timestamp_us: int
    pcm: np.ndarray  # int16, shape (n,)

    def encode(self) -> bytes:
        pcm = np.ascontiguousarray(self.pcm, dtype="<i2")
        header = HEADER.pack(
            MAGIC, VERSION, self.direction, self.sequence % SEQ_MOD,
            self.timestamp_us, len(pcm),
        )
        return header + pcm.tobytes()


def decode(data: bytes) -> AudioPacket:
    if len(data) < HEADER_SIZE:
        raise ProtocolError("short packet")
    magic, version, direction, seq, ts, count = HEADER.unpack_from(data)
    if magic != MAGIC:
        raise ProtocolError("bad magic")
    if version != VERSION:
        raise ProtocolError(f"unsupported version {version}")
    if direction not in (DIR_RX, DIR_TX):
        raise ProtocolError(f"bad direction {direction}")
    if len(data) != HEADER_SIZE + 2 * count:
        raise ProtocolError("length mismatch")
    if count not in (0, FRAME_SAMPLES):
        raise ProtocolError(f"bad sample count {count}")  # 0 is a keepalive; audio is always 10 ms
    pcm = np.frombuffer(data, dtype="<i2", offset=HEADER_SIZE, count=count)
    return AudioPacket(direction, seq, ts, pcm)


def seq_delta(a: int, b: int) -> int:
    """Signed distance from b to a, modulo 2**32 (positive means a is newer)."""
    d = (a - b) % SEQ_MOD
    return d - SEQ_MOD if d >= SEQ_MOD // 2 else d
