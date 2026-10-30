import numpy as np

from vector_bridge.jitter import JitterBuffer
from vector_bridge.protocol import DIR_TX, FRAME_SAMPLES, SEQ_MOD, AudioPacket


def pkt(seq):
    return AudioPacket(DIR_TX, seq, 0, np.full(FRAME_SAMPLES, seq % 32000, dtype=np.int16))


def val(frame):
    return int(frame[0])


def test_primes_then_plays_in_order():
    jb = JitterBuffer(target_ms=30, max_ms=40)
    jb.push(pkt(10))
    jb.push(pkt(11))
    assert not jb.primed and val(jb.pop()) == 0
    jb.push(pkt(12))
    assert jb.primed
    assert [val(jb.pop()) for _ in range(3)] == [10, 11, 12]


def test_reorders_out_of_order_packets():
    jb = JitterBuffer(30, 40)
    for s in (1, 3, 2):
        jb.push(pkt(s))
    assert [val(jb.pop()) for _ in range(3)] == [1, 2, 3]
    assert jb.stats.reordered == 1


def test_gap_becomes_silence_and_counts_loss():
    jb = JitterBuffer(30, 40)
    for s in (1, 2, 4, 5):
        jb.push(pkt(s))
    assert [val(jb.pop()) for _ in range(4)] == [1, 2, 0, 4]
    assert jb.stats.lost == 1
    assert jb.stats.received == 4


def test_empty_after_primed_is_underrun_and_reprimes():
    jb = JitterBuffer(30, 40)
    for s in range(3):
        jb.push(pkt(s))
    for _ in range(3):
        jb.pop()
    assert jb.stats.underruns == 0
    jb.pop()
    assert jb.stats.underruns == 1 and not jb.primed


def test_late_and_duplicate_packets_are_dropped():
    jb = JitterBuffer(30, 40)
    for s in (5, 6, 7):
        jb.push(pkt(s))
    jb.pop()
    jb.push(pkt(5))  # late
    jb.push(pkt(6))  # duplicate
    assert (jb.stats.late, jb.stats.duplicate) == (1, 1)


def test_sequence_wrap_is_continuous():
    jb = JitterBuffer(30, 40)
    for s in (SEQ_MOD - 2, SEQ_MOD - 1, 0, 1):
        jb.push(pkt(s))
    got = [val(jb.pop()) for _ in range(4)]
    assert got == [(SEQ_MOD - 2) % 32000, (SEQ_MOD - 1) % 32000, 0, 1]
    assert jb.stats.lost == 0


def test_overfull_buffer_drops_oldest():
    jb = JitterBuffer(30, 40)
    for s in range(12):
        jb.push(pkt(s))
    jb.pop()
    assert jb.stats.depth <= jb.max_depth
    assert jb.stats.lost > 0


def test_sender_restart_at_lower_sequence_resets():
    jb = JitterBuffer(30, 40)
    for s in (1_000_000, 1_000_001, 1_000_002):
        jb.push(pkt(s))
    jb.pop()
    for s in (0, 1, 2):
        jb.push(pkt(s))
    assert jb.stats.reset == 1 and jb.stats.late == 0
    assert [val(jb.pop()) for _ in range(3)] == [0, 1, 2]


def test_overflow_trim_counts_skipped_gap_as_lost():
    jb = JitterBuffer(30, 40)
    jb.push(pkt(0))
    jb.push(pkt(1))
    jb.push(pkt(2))
    for _ in range(3):
        jb.pop()          # _next is now 3
    for s in range(4, 10):  # seq 3 never arrives; 6 packets exceed max_depth 4
        jb.push(pkt(s))
    # dropped packets 4 and 5, plus the missing 3
    assert jb.stats.lost == 3
    assert val(jb.pop()) == 6


def test_reset_lets_a_new_sender_start_behind_the_old_one():
    jb = JitterBuffer(30, 40)
    for s in range(100, 104):
        jb.push(pkt(s))
    jb.reset()
    for s in range(50, 53):  # 50 behind the old position: "late" without the reset
        jb.push(pkt(s))
    assert jb.stats.late == 0
    assert val(jb.pop()) == 50
