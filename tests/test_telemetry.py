import numpy as np

from vector_bridge.telemetry import CLIP_LEVEL, measure

N = 480  # one 10 ms frame at 48 kHz
T = np.arange(N) / 48_000


def tone(peak: float, hz: float = 1000.0) -> np.ndarray:
    """int16 tone; a peak above full scale clips like an ADC, it never wraps."""
    return np.clip(np.round(peak * np.sin(2 * np.pi * hz * T)), -32768, 32767).astype(np.int16)


def test_clean_tone_near_the_calibrated_level_is_not_clipping():
    lv = measure(tone(12316))
    assert lv.peak == 12316
    assert lv.clips == 0


def test_full_scale_clipping_is_counted():
    lv = measure(tone(60000))
    assert lv.peak >= CLIP_LEVEL
    assert lv.clips > 0


def test_adc_overload_attenuated_by_the_mixer_is_still_clipping():
    # Overdriven input clipped at the ADC, then -8.5 dB of post-ADC attenuation: the peak
    # is well below CLIP_LEVEL but sits on a flat top.
    overloaded = tone(60000).astype(np.float64)
    attenuated = np.round(overloaded * 10 ** (-8.5 / 20)).astype(np.int16)
    lv = measure(attenuated)
    assert lv.peak < CLIP_LEVEL
    assert lv.clips > 0


def test_quiet_flat_signal_is_ignored():
    lv = measure(np.full(N, 200, dtype=np.int16))
    assert lv.clips == 0


def test_band_noise_is_not_clipping():
    rng = np.random.default_rng(1)
    lv = measure((rng.standard_normal(N) * 3000).astype(np.int16))
    assert lv.clips == 0
