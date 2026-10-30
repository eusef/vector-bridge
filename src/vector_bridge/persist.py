"""The persist partition: the only place on the card that is written after boot.

Layout under the mount (default /persist):

    vector-bridge/config.toml     runtime config (gains, ports)
    vector-bridge/presets/        user radio profiles, override built-ins by name
    vector-bridge/kits/           named kits from the setup wizard
    vector-bridge/last-good/      last known good config and kit
    vector-bridge/logs/           small rotating logs
    vector-bridge/networks/       NetworkManager keyfiles (root only, `[keyfile] path`), so a
                                  Wi-Fi change never needs the writable root

The partition is mounted read-only and is remounted writable only for the seconds a
save takes. Every write is atomic: temp file in the same directory, fsync, rename,
fsync the directory. A power cut at any point leaves either the old file or the new
file, never a truncated one. On a machine without the partition (the bench, tests)
the remount is skipped and writes are still atomic.
"""

from __future__ import annotations

import ctypes
import errno
import os
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

PERSIST_MOUNT = Path(os.environ.get("VECTOR_BRIDGE_PERSIST", "/persist"))
APP_DIR = PERSIST_MOUNT / "vector-bridge"
PRESETS_DIR = APP_DIR / "presets"
KITS_DIR = APP_DIR / "kits"
LAST_GOOD_DIR = APP_DIR / "last-good"
LOGS_DIR = APP_DIR / "logs"
NETWORKS_DIR = APP_DIR / "networks"

Remount = Callable[[str], None]  # called with "rw" then "ro"


_MS_RDONLY, _MS_REMOUNT, _MS_NOATIME = 1, 32, 1024


def _syscall_remount(mode: str) -> bool:
    """Remount through mount(2). False when this process may not (no CAP_SYS_ADMIN)."""
    libc = ctypes.CDLL(None, use_errno=True)
    flags = _MS_REMOUNT | _MS_NOATIME | (_MS_RDONLY if mode == "ro" else 0)
    if libc.mount(None, str(PERSIST_MOUNT).encode(), None, flags, None) == 0:
        return True
    err = ctypes.get_errno()
    if err == errno.EPERM:
        return False
    raise OSError(err, os.strerror(err), str(PERSIST_MOUNT))


def _system_remount(mode: str) -> None:
    # A process with CAP_SYS_ADMIN (root, or a unit that grants it) remounts directly; the
    # services, which since roadmap 1.4 hold no such capability, fall through to sudo.
    if _syscall_remount(mode):
        return
    # Full path: the sudoers rule installed by setup-readonly-root.sh matches this exact command.
    argv = ["/usr/bin/mount", "-o", f"remount,{mode}", str(PERSIST_MOUNT)]
    if os.geteuid() != 0:
        argv = ["sudo", "-n", *argv]
    try:
        subprocess.run(argv, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        # Callers catch OSError for a failed save, as they did when the syscall path raised it.
        why = (e.stderr or b"").decode(errors="replace").strip() or f"exit {e.returncode}"
        raise OSError(errno.EIO, f"remount {mode} failed: {why}", str(PERSIST_MOUNT)) from e


def on_persist(path: Path) -> bool:
    """True when path lives on a real, separately mounted persist partition."""
    try:
        return os.path.ismount(PERSIST_MOUNT) and path.resolve().is_relative_to(PERSIST_MOUNT.resolve())
    except OSError:
        return False


@contextmanager
def writable(path: Path, remount: Remount = _system_remount):
    """Remount the persist partition rw for the block, then back to ro, even on error."""
    if not on_persist(path):
        yield
        return
    remount("rw")
    try:
        yield
    finally:
        remount("ro")


def atomic_write(path: Path, data: bytes | str, *, mode: int | None = None, gid: int | None = None) -> Path:
    """Write via temp file + fsync + rename in the same directory. Leaves no temp file on failure."""
    if isinstance(data, str):
        data = data.encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        if gid is not None:
            try:
                os.chown(tmp, -1, gid)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)
    return path


def save(path: Path, data: bytes | str, remount: Remount = _system_remount, **kw) -> Path:
    """The one call sites use: remount if needed, write atomically, remount back."""
    with writable(path, remount):
        return atomic_write(path, data, **kw)


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
