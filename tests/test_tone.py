"""The bridge's own test tone: a steady sine that ends at its deadline on its own."""

import numpy as np

from vector_bridge import tone
from vector_bridge.protocol import FRAME_SAMPLES


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_no_tone_until_started():
    assert tone.ToneSource(Clock()).frame() is None


def test_tone_is_a_continuous_sine_at_the_client_level():
    src = tone.ToneSource(Clock())
    src.start(1500, 5)
    pcm = np.concatenate([src.frame() for _ in range(10)])
    assert len(pcm) == 10 * FRAME_SAMPLES and pcm.dtype == np.int16
    assert tone.PEAK - 50 <= np.abs(pcm).max() <= tone.PEAK
    step = np.abs(np.diff(pcm.astype(int))).max()
    assert step < 2 * np.pi * 1500 / 48_000 * tone.PEAK * 1.01  # no jump at the frame joins


def test_tone_ends_at_its_deadline_without_a_stop():
    clock = Clock()
    src = tone.ToneSource(clock)
    src.start(1500, 2)
    clock.t = 1.99
    assert src.frame() is not None
    clock.t = 2.0
    assert src.frame() is None


def test_stop_silences_at_once():
    src = tone.ToneSource(Clock())
    src.start(1500, 5)
    src.stop()
    assert src.frame() is None
