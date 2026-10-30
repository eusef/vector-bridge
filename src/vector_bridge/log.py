"""Structured single-line logging to stdout (journald picks it up under systemd).

Every line: `ts=<iso> level=<L> event=<name> key=value ...`. Failure modes from
proposal section 17 each have a fixed event name so they can be grepped:
alsa_underrun, alsa_overrun, udp_loss, excessive_jitter, audio_clipping,
device_disconnect, device_reconnect.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import UTC, datetime

_lock = threading.Lock()
_last: dict[str, float] = {}
_suppressed: dict[str, int] = {}


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.3f}"
    if isinstance(v, (int, bool)) or v is None:
        return str(v)
    s = str(v)
    return json.dumps(s) if (" " in s or "=" in s or not s) else s


def log(event: str, level: str = "INFO", *, every: float = 0.0, **fields) -> None:
    """Emit one line. `every=N` rate-limits the event to one line per N seconds,
    adding `suppressed=<count>` for lines dropped in between."""
    now = time.monotonic()
    with _lock:
        if every:
            key = event + ":" + str(next(iter(fields.values()), ""))
            last = _last.get(key)
            if last is not None and now - last < every:
                _suppressed[key] = _suppressed.get(key, 0) + 1
                return
            _last[key] = now
            n = _suppressed.pop(key, 0)
            if n:
                fields["suppressed"] = n
        ts = datetime.now(UTC).isoformat(timespec="milliseconds")
        parts = [f"ts={ts}", f"level={level}", f"event={event}"]
        parts += [f"{k}={_fmt(v)}" for k, v in fields.items()]
        sys.stdout.write(" ".join(parts) + "\n")
        sys.stdout.flush()


def warn(event: str, **fields) -> None:
    log(event, "WARN", **fields)


def error(event: str, **fields) -> None:
    log(event, "ERROR", **fields)
