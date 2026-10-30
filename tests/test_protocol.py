import numpy as np
import pytest

from vector_bridge import protocol as p


def _pkt(seq, direction=p.DIR_TX, n=p.FRAME_SAMPLES):
    pcm = (np.arange(n) * 37 % 65536 - 32768).astype(np.int16)
    return p.AudioPacket(direction, seq, 123_456_789, pcm)


def test_round_trip_and_size():
    pkt = _pkt(7)
    data = pkt.encode()
    assert len(data) == p.HEADER_SIZE + 2 * p.FRAME_SAMPLES == 980
    out = p.decode(data)
    assert (out.direction, out.sequence, out.timestamp_us) == (p.DIR_TX, 7, 123_456_789)
    assert np.array_equal(out.pcm, pkt.pcm)


def test_sequence_wraps_at_32_bits():
    data = _pkt(p.SEQ_MOD + 5).encode()
    assert p.decode(data).sequence == 5
    assert p.seq_delta(0, p.SEQ_MOD - 1) == 1
    assert p.seq_delta(p.SEQ_MOD - 1, 0) == -1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d[:10],
        lambda d: b"XXXX" + d[4:],
        lambda d: d[:4] + bytes([9]) + d[5:],
        lambda d: d[:5] + bytes([2]) + d[6:],
        lambda d: d + b"\x00",
    ],
    ids=["short", "magic", "version", "direction", "length"],
)
def test_rejects_malformed(mutate):
    with pytest.raises(p.ProtocolError):
        p.decode(mutate(_pkt(1).encode()))


@pytest.mark.parametrize("n", [1, 479, 481, 960])
def test_rejects_sample_counts_other_than_keepalive_or_one_frame(n):
    with pytest.raises(p.ProtocolError):
        p.decode(_pkt(1, n=n).encode())
    assert len(p.decode(_pkt(1, n=0).encode()).pcm) == 0  # keepalive still decodes
