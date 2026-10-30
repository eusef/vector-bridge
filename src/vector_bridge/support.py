"""Support bundle: one text file a user can attach to a bug report.

The journal lives in RAM (16 MB) and is gone after a power cycle, so the tuner also saves a
bundle to /persist whenever a readiness alert fires, keeping the last few.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from . import persist, version_text

log = logging.getLogger(__name__)

JOURNAL_LINES = 400
KERNEL_LINES = 100
KEEP_FAULTS = 5
UNITS = ("vector-bridge", "vector-bridge-tuner")

# Alerts save from their own threads: two at once would pick the same number, and one's
# remount read-only could land in the middle of the other's write.
_save_lock = threading.Lock()


def _run(argv: list[str]) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return f"unavailable: {e}"
    return r.stdout or r.stderr or f"(no output, exit {r.returncode})"


def journal() -> str:
    """This boot's lines from the two bridge units only, never the rest of the system."""
    units = [a for u in UNITS for a in ("-u", u)]
    return _run(["journalctl", "-b", "--no-pager", "-o", "short-monotonic", "-n", str(JOURNAL_LINES), *units])


def kernel() -> str:
    """USB plug events and audio driver errors land here."""
    return _run(["journalctl", "-b", "-k", "--no-pager", "-o", "short-monotonic", "-n", str(KERNEL_LINES)])


def uptime() -> str:
    try:
        return f"{float(Path('/proc/uptime').read_text().split()[0]):.0f} s since boot"
    except (OSError, ValueError, IndexError) as e:
        return f"unavailable: {e}"


def bundle(parts: list[tuple[str, Callable[[], object]]]) -> str:
    """Each part under its own heading; a part that fails says why instead of breaking the bundle."""
    out = [f"VectorBridge support bundle, version {version_text()}", ""]
    for title, fn in parts:
        try:
            v = fn()
            text = v if isinstance(v, str) else json.dumps(v, indent=1, default=str)
        except Exception as e:  # noqa: BLE001 - a broken part must not cost the rest of the bundle
            text = f"unavailable: {e!r}"
        out += [f"===== {title} =====", text.rstrip(), ""]
    return "\n".join(out)


def save_fault(text: str, logs_dir: Path | None = None, keep: int = KEEP_FAULTS) -> Path | None:
    """Write fault-NNNN.txt (there is no clock to name it by) and keep only the newest `keep`."""
    logs_dir = logs_dir or persist.LOGS_DIR
    with _save_lock:
        return _save_fault(text, logs_dir, keep)


def clear_faults(logs_dir: Path | None = None) -> bool:
    """Remove every saved fault bundle (a factory reset). False when the card refused."""
    logs_dir = logs_dir or persist.LOGS_DIR
    with _save_lock:
        try:
            with persist.writable(logs_dir):
                for p in logs_dir.glob("fault-*.txt"):
                    p.unlink(missing_ok=True)
            return True
        except OSError:
            log.exception("could not clear the fault bundles")
            return False


def _save_fault(text: str, logs_dir: Path, keep: int) -> Path | None:
    try:
        nums = sorted(int(m.group(1)) for p in logs_dir.glob("fault-*.txt") if (m := re.fullmatch(r"fault-(\d+)\.txt", p.name)))
        path = logs_dir / f"fault-{(nums[-1] + 1 if nums else 1):04d}.txt"
        with persist.writable(path):
            persist.atomic_write(path, text)
            for n in nums[:max(0, len(nums) + 1 - keep)]:
                (logs_dir / f"fault-{n:04d}.txt").unlink(missing_ok=True)
        return path
    except OSError:
        log.exception("could not save the fault bundle")
        return None
