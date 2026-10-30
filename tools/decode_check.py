#!/usr/bin/env python3
"""Prove the bridge's receive audio decodes, which a level reading cannot.

`bridge_client.py --record` writes one continuous 48 kHz mono WAV. FT8 decoders want
12 kHz and one file per 15 s transmit period aligned to UTC :00/:15/:30/:45, and the
recording carries no timestamp. So this resamples, cuts the recording at several
alignments, decodes every slice, and keeps whichever alignment decoded best.

    tools/decode_check.py ~/rx-30m.wav

Exit status is 0 when anything decoded, 1 when nothing did, so it works as an
acceptance test for a new radio or adapter.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

TARGET_RATE = 12_000
PERIOD_S = 15.0
# FT8 tolerates roughly +-2.5 s of sync error, so alignments 2.5 s apart leave at most
# 1.25 s of error at the worst offset. Six passes is cheap next to guessing wrong.
DEFAULT_OFFSETS = (0.0, 2.5, 5.0, 7.5, 10.0, 12.5)
JT9_CANDIDATES = (
    "jt9",
    str(Path.home() / ".local/jt9-arm64/jt9"),
    "/Applications/wsjtx.app/Contents/MacOS/jt9",
    "/usr/local/bin/jt9",
    "/opt/homebrew/bin/jt9",
)
# jt9 prints e.g. "000000  -8  0.2 1234 ~  CQ K1ABC FN42"
_DECODE_RE = re.compile(r"^\s*\d{4,6}\s+(-?\d+)\s+(-?[\d.]+)\s+(\d+)\s+[~`?*+]\s+(.+?)\s*$")
_CALL_RE = re.compile(r"\b([A-Z0-9]{1,3}[0-9][A-Z0-9]{0,3}[A-Z])\b")


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    """Mono int16 samples and the sample rate, rejecting anything the decoder cannot use."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: need 16-bit samples, got {w.getsampwidth() * 8}-bit")
        channels, rate, frames = w.getnchannels(), w.getframerate(), w.getnframes()
        raw = w.readframes(frames)
    x = np.frombuffer(raw, dtype="<i2")
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return x, rate


def lowpass_taps(cutoff_hz: float, rate: int, taps: int = 129) -> np.ndarray:
    """Windowed-sinc low pass, normalized to unity gain at DC."""
    n = np.arange(taps) - (taps - 1) / 2
    fc = cutoff_hz / rate
    h = 2 * fc * np.sinc(2 * fc * n) * np.hamming(taps)
    return h / h.sum()


def resample(x: np.ndarray, rate: int, target: int = TARGET_RATE) -> np.ndarray:
    """Down to `target` by integer decimation, low-passed first.

    Without the filter, everything above the new Nyquist folds back into the FT8
    passband as noise that looks exactly like a bad antenna.
    """
    if rate == target:
        return x.astype(np.int16)
    if rate % target:
        raise ValueError(f"cannot decimate {rate} Hz to {target} Hz: not an integer ratio")
    factor = rate // target
    h = lowpass_taps(target * 0.45, rate)
    y = np.convolve(x.astype(np.float64), h, mode="same")[::factor]
    return np.clip(y, -32768, 32767).astype(np.int16)


def slice_periods(x: np.ndarray, rate: int, offset_s: float,
                  period_s: float = PERIOD_S) -> list[np.ndarray]:
    """Whole `period_s` chunks starting `offset_s` in; a short tail is dropped."""
    step = round(period_s * rate)
    start = round(offset_s * rate)
    if step <= 0:
        raise ValueError("period must be positive")
    return [x[i:i + step] for i in range(start, len(x) - step + 1, step)]


def write_wav(path: Path, x: np.ndarray, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(x.astype("<i2").tobytes())


def can_exec(path: str) -> bool:
    """Whether this machine can actually run the binary.

    An Intel jt9 on Apple Silicon fails at exec, not at decode. Without this check the
    run would report "nothing decoded", which points at the antenna instead of the tool.
    """
    try:
        # jt9 drops timer.out, decoded.txt and jt9_wisdom.dat into its working
        # directory, so never run it from the caller's.
        subprocess.run([path, "-8", "/nonexistent.wav"], check=False,
                       capture_output=True, timeout=10, cwd=tempfile.gettempdir())
    except OSError:
        return False
    except subprocess.SubprocessError:
        return True  # it started; slow or noisy is fine
    return True


def find_jt9(explicit: str | None = None) -> tuple[str | None, list[str]]:
    """The first jt9 that runs here, plus a note for each one that exists but cannot."""
    notes: list[str] = []
    for candidate in ([explicit] if explicit else []) + list(JT9_CANDIDATES):
        found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if not found:
            continue
        if can_exec(found):
            return found, notes
        notes.append(f"{found} exists but will not run on this machine")
    return None, notes


def parse_decodes(text: str) -> list[dict]:
    """jt9 stdout to a list of decodes; lines that are not decodes are ignored."""
    out = []
    for line in text.splitlines():
        m = _DECODE_RE.match(line)
        if m:
            out.append({"snr": int(m.group(1)), "dt": float(m.group(2)),
                        "freq": int(m.group(3)), "message": m.group(4)})
    return out


def callsigns(decodes: list[dict]) -> set[str]:
    found = set()
    for d in decodes:
        for token in _CALL_RE.findall(d["message"].upper()):
            if token not in ("CQ", "RR73", "73"):
                found.add(token)
    return found


def decode_file(jt9: str, path: Path, timeout: float = 60.0) -> tuple[list[dict], str]:
    """Parsed decodes and the raw stdout, so a parser mismatch can be seen rather than
    silently reported as an empty band."""
    try:
        proc = subprocess.run([jt9, "-8", path.name], check=False, cwd=path.parent,
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return [], ""
    return parse_decodes(proc.stdout), proc.stdout


def run(wav: Path, offsets=DEFAULT_OFFSETS, jt9: str | None = None,
        decoder=decode_file) -> dict:
    """Try each alignment, keep the best. Returns a report dict."""
    x, rate = load_wav(wav)
    duration = len(x) / rate
    y = resample(x, rate)
    binary, notes = find_jt9(jt9)
    if binary is None:
        hint = ("WSJT-X ships jt9, but an Intel build will not run on Apple Silicon: "
                "install Rosetta (sudo softwareupdate --install-rosetta), or reinstall "
                "WSJT-X as the Apple Silicon build.")
        raise RuntimeError("\n".join(["no usable jt9 decoder found.", *notes, hint]))

    results, raw_samples = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for offset in offsets:
            chunks = slice_periods(y, TARGET_RATE, offset)
            decodes: list[dict] = []
            for i, chunk in enumerate(chunks):
                seg = Path(tmp) / f"seg_{offset:g}_{i:03d}.wav"
                write_wav(seg, chunk, TARGET_RATE)
                found, raw = decoder(binary, seg)
                decodes.extend(found)
                # <DecodeFinished> is jt9's normal end marker, so a segment that produced
                # only that decoded nothing and is not evidence of a parser problem.
                leftover = "\n".join(line for line in raw.splitlines()
                                     if line.strip() and "<DecodeFinished>" not in line)
                if leftover and len(raw_samples) < 3:
                    raw_samples.append(leftover)
                seg.unlink(missing_ok=True)
            results.append({"offset": offset, "periods": len(chunks),
                            "decodes": decodes, "calls": callsigns(decodes)})

    best = max(results, key=lambda r: len(r["decodes"]), default=None)
    return {"file": str(wav), "duration_s": round(duration, 1), "source_rate": rate,
            "jt9": binary, "results": results, "best": best, "raw_samples": raw_samples}


def report(rep: dict) -> str:
    best = rep["best"]
    lines = [
        f"file        {rep['file']}",
        f"duration    {rep['duration_s']} s at {rep['source_rate']} Hz",
        f"decoder     {rep['jt9']}",
        "",
        "alignment   periods  decodes  unique calls",
    ]
    for r in rep["results"]:
        mark = " <-- best" if best and r["offset"] == best["offset"] else ""
        lines.append(f"  {r['offset']:>5.1f} s   {r['periods']:>6}  {len(r['decodes']):>7}"
                     f"  {len(r['calls']):>12}{mark}")
    lines.append("")
    if not best or not best["decodes"]:
        lines.append("RESULT: nothing decoded.")
        lines.append("Levels can look right and still not decode. Check that the radio was on an")
        lines.append("active FT8 frequency (10.136 MHz on 30 m), in USB, with the recording long")
        lines.append("enough to span several 15 s periods.")
        raw = rep.get("raw_samples") or []
        if raw:
            # The decoder said something we could not parse, which is a different fault
            # from an empty band and should not look the same in the report.
            lines.append("")
            lines.append("The decoder did produce output. If this looks like decodes, the parser")
            lines.append("needs adjusting rather than the radio:")
            for sample in raw[:2]:
                for line in sample.splitlines()[:6]:
                    lines.append(f"  | {line}")
        return "\n".join(lines)
    lines.append(f"RESULT: {len(best['decodes'])} decodes, "
                 f"{len(best['calls'])} unique stations, best alignment {best['offset']:g} s.")
    snrs = sorted(d["snr"] for d in best["decodes"])
    lines.append(f"SNR range   {snrs[0]} to {snrs[-1]} dB (median {snrs[len(snrs) // 2]})")
    lines.append("")
    lines.append("Sample decodes:")
    for d in best["decodes"][:8]:
        lines.append(f"  {d['snr']:>4} dB  {d['freq']:>5} Hz  {d['message']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Decode a bridge recording to prove the RX chain works")
    ap.add_argument("wav", type=Path, help="WAV from bridge_client.py --record")
    ap.add_argument("--jt9", default=None, help="path to the jt9 binary")
    ap.add_argument("--offsets", type=float, nargs="+", default=list(DEFAULT_OFFSETS),
                    help="alignments in seconds to try")
    args = ap.parse_args(argv)
    if not args.wav.is_file():
        print(f"error: no such file: {args.wav}", file=sys.stderr)
        return 2
    try:
        rep = run(args.wav, offsets=tuple(args.offsets), jt9=args.jt9)
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(report(rep))
    return 0 if rep["best"] and rep["best"]["decodes"] else 1


if __name__ == "__main__":
    sys.exit(main())
