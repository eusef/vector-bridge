"""The transmit audio cap: the only time limit a VOX rig has (txcap.py)."""
import numpy as np

from vector_bridge.protocol import FRAME_SAMPLES
from vector_bridge.txcap import QUIET_RESET_S, TxAudioCap

LOUD = np.full(FRAME_SAMPLES, 8000, dtype=np.int16)
QUIET = np.zeros(FRAME_SAMPLES, dtype=np.int16)
FRAMES_PER_S = 100


def play(cap, frame, seconds):
    """Peaks of what the cap passes, frame by frame."""
    return [int(np.abs(cap.apply(frame, int(np.abs(frame).max()))[0]).max())
            for _ in range(round(seconds * FRAMES_PER_S))]


def test_a_stuck_source_is_muted_at_the_cap_and_stays_muted_until_it_pauses():
    cap = TxAudioCap(30)
    out = play(cap, LOUD, 40)
    assert all(out[:30 * FRAMES_PER_S - 1]) and not any(out[30 * FRAMES_PER_S:])
    assert cap.muted and cap.trips == 1
    play(cap, QUIET, 0.5)  # a pause shorter than the reset keeps it muted
    assert not any(play(cap, LOUD, 1))
    play(cap, QUIET, QUIET_RESET_S)
    assert not cap.muted and all(play(cap, LOUD, 1))


def test_ft8_slots_with_their_pauses_are_never_cut():
    cap = TxAudioCap(30)
    for _ in range(4):  # 12.6 s of audio, then the 2.4 s gap to the next slot
        assert all(play(cap, LOUD, 12.6))
        play(cap, QUIET, 2.4)
    assert cap.trips == 0


def test_zero_turns_the_cap_off_and_releases_a_mute():
    cap = TxAudioCap(1)
    play(cap, LOUD, 2)
    assert cap.muted
    cap.cap_s = 0
    assert all(play(cap, LOUD, 60))


def test_default_cap_covers_a_wspr_transmission():
    cap = TxAudioCap()  # the default Transmit Time Limit
    assert cap.cap_s == 120
    assert all(play(cap, LOUD, 111))  # WSPR is 110.6 s of continuous audio: none of it is muted
