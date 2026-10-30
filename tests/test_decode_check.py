"""Decode harness: WAV loading, anti-aliased resampling, period slicing, jt9 parsing."""

import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from decode_check import (
    TARGET_RATE,
    callsigns,
    load_wav,
    parse_decodes,
    report,
    resample,
    run,
    slice_periods,
    write_wav,
)

# One period of real jt9 FT8 output.
JT9_OUTPUT = """\
000000  -8  0.2 1234 ~  CQ K1ABC FN42
000000 -15 -0.1  975 ~  W9XYZ K1ABC -12
000000  -3  0.4 1600 ~  CQ DX VE3ABC FN25
<DecodeFinished>
"""


def tone(freq, seconds, rate, amplitude=8000):
    t = np.arange(int(seconds * rate)) / rate
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def power_at(x, freq, rate):
    """Magnitude of one bin, via a direct correlation so no FFT binning games."""
    t = np.arange(len(x)) / rate
    ref = np.exp(-2j * np.pi * freq * t)
    return abs(np.dot(x.astype(np.float64), ref)) / len(x)


# ---- WAV round trip -----------------------------------------------------------


def test_load_wav_reads_mono_int16(tmp_path):
    path = tmp_path / "a.wav"
    x = tone(1000, 0.5, 48000)
    write_wav(path, x, 48000)
    back, rate = load_wav(path)
    assert rate == 48000
    assert np.array_equal(back, x)


def test_load_wav_averages_stereo_to_mono(tmp_path):
    path = tmp_path / "s.wav"
    left, right = np.full(100, 1000, np.int16), np.full(100, 2000, np.int16)
    interleaved = np.empty(200, np.int16)
    interleaved[0::2], interleaved[1::2] = left, right
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(interleaved.tobytes())
    back, _ = load_wav(path)
    assert len(back) == 100
    assert back[0] == 1500


def test_load_wav_rejects_8bit(tmp_path):
    path = tmp_path / "b.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(48000)
        w.writeframes(b"\x00" * 100)
    with pytest.raises(ValueError, match="16-bit"):
        load_wav(path)


# ---- resampling ---------------------------------------------------------------


def test_resample_changes_rate_by_the_integer_ratio():
    x = tone(1000, 1.0, 48000)
    y = resample(x, 48000)
    assert abs(len(y) - TARGET_RATE) <= 1


def test_resample_keeps_an_in_band_tone():
    """1 kHz sits inside the FT8 passband and must survive."""
    y = resample(tone(1000, 1.0, 48000), 48000)
    assert power_at(y, 1000, TARGET_RATE) > 1000


def test_resample_rejects_an_out_of_band_tone():
    """8 kHz is above the new Nyquist; without the low pass it would alias to 4 kHz.

    This is the test that matters: aliasing shows up as broadband noise that looks
    like a bad antenna rather than like a software bug.
    """
    y = resample(tone(8000, 1.0, 48000), 48000)
    assert power_at(y, 4000, TARGET_RATE) < 100


def test_resample_is_a_noop_at_the_target_rate():
    x = tone(1000, 0.1, TARGET_RATE)
    assert np.array_equal(resample(x, TARGET_RATE), x)


def test_resample_refuses_a_non_integer_ratio():
    with pytest.raises(ValueError, match="integer ratio"):
        resample(tone(1000, 0.1, 44100), 44100)


# ---- period slicing -----------------------------------------------------------


def test_slice_periods_cuts_whole_15s_chunks():
    x = np.zeros(TARGET_RATE * 47, np.int16)          # 47 s holds three whole periods
    chunks = slice_periods(x, TARGET_RATE, 0.0)
    assert len(chunks) == 3
    assert all(len(c) == TARGET_RATE * 15 for c in chunks)


def test_slice_periods_honours_the_offset():
    x = np.arange(TARGET_RATE * 40, dtype=np.int16)
    chunks = slice_periods(x, TARGET_RATE, 2.5)
    assert chunks[0][0] == np.int16(2.5 * TARGET_RATE)


def test_slice_periods_drops_a_short_tail():
    x = np.zeros(int(TARGET_RATE * 20), np.int16)     # one period plus 5 s
    assert len(slice_periods(x, TARGET_RATE, 0.0)) == 1


# ---- jt9 output ---------------------------------------------------------------


def test_parse_decodes_pulls_snr_dt_freq_and_message():
    decodes = parse_decodes(JT9_OUTPUT)
    assert len(decodes) == 3
    assert decodes[0] == {"snr": -8, "dt": 0.2, "freq": 1234, "message": "CQ K1ABC FN42"}


def test_parse_decodes_ignores_non_decode_lines():
    assert parse_decodes("<DecodeFinished>\nrandom noise\n") == []


def test_callsigns_extracts_stations_and_skips_prosigns():
    calls = callsigns(parse_decodes(JT9_OUTPUT))
    assert {"K1ABC", "W9XYZ", "VE3ABC"} <= calls
    assert "CQ" not in calls


# ---- end to end with a stub decoder -------------------------------------------


def test_run_picks_the_alignment_that_decoded_most(tmp_path):
    path = tmp_path / "rx.wav"
    write_wav(path, tone(1000, 40, 48000), 48000)
    winner = 5.0

    def fake_decoder(_binary, seg):
        if f"_{winner:g}_" in seg.name:
            return parse_decodes(JT9_OUTPUT), JT9_OUTPUT
        return [], ""

    rep = run(path, offsets=(0.0, winner), jt9="/bin/sh", decoder=fake_decoder)
    assert rep["best"]["offset"] == winner
    assert len(rep["best"]["calls"]) == 3
    assert "best alignment 5 s" in report(rep)


def test_run_reports_nothing_decoded_without_claiming_success(tmp_path):
    path = tmp_path / "quiet.wav"
    write_wav(path, np.zeros(48000 * 20, np.int16), 48000)
    rep = run(path, offsets=(0.0,), jt9="/bin/sh", decoder=lambda *_: ([], ""))
    assert rep["best"]["decodes"] == []
    out = report(rep)
    assert "RESULT: nothing decoded." in out
    assert "parser needs adjusting" not in out  # nothing came back, so do not blame the parser


def test_report_shows_raw_output_when_nothing_parsed(tmp_path):
    """An unparsed decode is a different fault from an empty band and must look different."""
    path = tmp_path / "odd.wav"
    write_wav(path, np.zeros(48000 * 20, np.int16), 48000)
    rep = run(path, offsets=(0.0,), jt9="/bin/sh",
              decoder=lambda *_: ([], "000000  -8  0.2 1234 @  SOME NEW FORMAT"))
    out = report(rep)
    assert "SOME NEW FORMAT" in out
    assert "parser" in out


def test_run_explains_the_apple_silicon_case_when_no_decoder_exists(tmp_path, monkeypatch):
    """Must not depend on what this machine happens to have installed."""
    import decode_check

    monkeypatch.setattr(decode_check, "JT9_CANDIDATES", ())
    path = tmp_path / "rx.wav"
    write_wav(path, tone(1000, 20, 48000), 48000)
    with pytest.raises(RuntimeError, match="Rosetta"):
        run(path, offsets=(0.0,), jt9="/nonexistent/jt9")
