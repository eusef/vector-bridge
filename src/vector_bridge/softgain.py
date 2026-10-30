"""TX soft gain: the transmit level for a card with no playback volume (QRP Labs QMX: PCM switch only).

The slider (tx_gain, 0-100) then scales the samples. 0 is silent; 1-100 run 0.4 dB a step with
75 at unity, so 100 is +10 dB. The QMX keys RF on the audio alone once it crosses its Rise
threshold (80 % of full scale), and the app sends 0.5 FS, so it needs a boost: 88 gives about
0.9 FS. The test tone already plays at 0.9 FS, so it is only ever attenuated.
"""

from __future__ import annotations

import numpy as np

UNITY_STEP = 75
DB_PER_STEP = 0.4


def gain(step: int) -> float:
    """Linear sample gain for a 0-100 slider step; 0 (or less) is silent."""
    if step <= 0:
        return 0.0
    return 10 ** ((min(step, 100) - UNITY_STEP) * DB_PER_STEP / 20)


def gain_db(step: int) -> float | None:
    """The same gain in dB, None when silent."""
    return None if step <= 0 else (min(step, 100) - UNITY_STEP) * DB_PER_STEP


def apply(pcm: np.ndarray, g: float) -> np.ndarray:
    """Scale int16 samples by g, saturating at full scale instead of wrapping."""
    if g == 1.0:
        return pcm
    return np.clip(np.rint(pcm.astype(np.float32) * g), -32768, 32767).astype(np.int16)
