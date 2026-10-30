"""CAT through Hamlib's rigctld, which runs as a separate process.

Nothing here links libhamlib. The bridge talks to rigctld over its TCP protocol using
the extended response format (a leading '+'), which returns labelled lines and an
explicit ``RPRT n`` status, so failures are visible rather than guessed from silence.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .profiles import RadioProfile

log = logging.getLogger(__name__)

# rigctld runs about 4 threads plus one per open connection. When the rig stops answering, every
# connection a client gives up on keeps its thread queued on the rig for minutes (reproduced
# 2026-10-04: 60 abandoned connections, 59 threads, 1 gone 2 min later). rigctld shares the
# tuner unit's task limit (TasksMax 218), so the pile-up starved the tuner of threads that day.
MAX_RIGCTLD_THREADS = 48

# udev's stable names for USB serial ports. FTDI (KXUSB, Digirig) and ACM (IC-705, QMX) get one.
SERIAL_BY_ID = "/dev/serial/by-id"
# Hamlib's dummy rig: it opens no CAT port, but still keys the RTS or DTR line given with -P/-p.
# A kit with no radio control that keys by RTS or DTR runs rigctld as this.
DUMMY_RIG_MODEL = 1
# rigctld is private to the bridge: loopback, off the usual 4532. Desktop WSJT-X and JS8Call
# reach it only through the safety proxy on 4532 (rigproxy), which owns PTT.
DEFAULT_RIGCTLD_PORT = 4534
DEFAULT_RIGCTLD_BIND = "127.0.0.1"
# rigctld's own stderr lines kept for the CAT failure "Details": a wrong port, a busy port or a
# bad model all look the same from outside ("not answering"), but Hamlib says which it is.
# Hamlib 4.6.2 holds that text until rigctld exits (bench 2026-10-04: nothing while running
# through a pipe, a pty or stdbuf; 839 bytes on SIGTERM), so a failure stops rigctld to read it.
RIGCTLD_LOG_LINES = 20
BARE = ""  # key for a reply line that carries a value with no "Name:" in front


class CatError(Exception):
    """rigctld reported a non-zero RPRT or could not be reached."""


class RigctldClient:
    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_RIGCTLD_PORT, timeout: float = 2.0) -> None:
        self.host, self.port, self.timeout = host, port, timeout

    def command(self, cmd: str, *args: str) -> dict[str, str]:
        line = "+\\" + " ".join((cmd, *args)) + "\n"
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.sendall(line.encode())
                buf = b""
                while b"RPRT " not in buf:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
        except OSError as e:
            raise CatError(f"rigctld unreachable at {self.host}:{self.port}: {e} ({cmd})") from e
        return _parse_extended(buf.decode(errors="replace"))

    def get_freq(self) -> int:
        return int(float(self.command("get_freq")["Frequency"]))

    def set_freq(self, hz: int) -> None:
        self.command("set_freq", str(int(hz)))

    def get_mode(self) -> str:
        return self.command("get_mode")["Mode"]

    def set_mode(self, mode: str) -> None:
        self.command("set_mode", mode, "0")  # passband 0: the radio's normal filter for that mode

    def get_ptt(self) -> bool:
        return self.command("get_ptt")["PTT"].strip() != "0"

    def set_ptt(self, on: bool) -> None:
        self.command("set_ptt", "1" if on else "0")

    def get_level(self, name: str) -> float:
        """Levels: ALC (0..1, hamlib normalises), SWR (ratio), RFPOWER_METER_WATTS, VD_METER."""
        reply = self.command("get_level", name)
        # Hamlib 4.6.2 answers with a bare value line, not "Level Value: x" (bench, 2026-09-20).
        return float(reply["Level Value"] if "Level Value" in reply else reply[BARE])

    def set_level(self, name: str, value: float) -> None:
        self.command("set_level", name, f"{value:g}")  # a refusal comes back as CatError

    def alive(self) -> bool:
        try:
            self.get_freq()
            return True
        except (CatError, KeyError, ValueError):
            return False


def _parse_extended(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    rprt = None
    for raw in text.splitlines():
        if raw.startswith("RPRT "):
            rprt = int(raw.split()[1])
        elif ":" in raw:
            k, _, v = raw.partition(":")
            v = v.strip()
            if v:  # the first line echoes the command with an empty value
                out[k.strip()] = v
        elif raw.strip():
            out[BARE] = raw.strip()
    if rprt is None:
        raise CatError("no RPRT status from rigctld")
    if rprt != 0:
        raise CatError(f"rigctld error RPRT {rprt}")
    return out


def rigctld_argv(profile: RadioProfile, port: int = DEFAULT_RIGCTLD_PORT,
                 bind: str = DEFAULT_RIGCTLD_BIND) -> list[str]:
    if not profile.needs_rigctld:
        raise ValueError(f"profile {profile.name!r} has no CAT (rig_model 0)")
    if profile.rig_model == 0:
        if not profile.cat_device:
            raise ValueError(f"profile {profile.name!r} keys by {profile.ptt_method.upper()} and needs the serial port")
        argv = ["rigctld", "-m", str(DUMMY_RIG_MODEL), "-T", bind or DEFAULT_RIGCTLD_BIND, "-t", str(port),
                "-P", profile.ptt_method.upper(), "-p", profile.cat_device]
        return argv + (["-C", profile.set_conf] if profile.set_conf else []) + line_states_conf(profile)
    argv = ["rigctld", "-m", str(profile.rig_model), "-T", bind or DEFAULT_RIGCTLD_BIND, "-t", str(port)]
    if profile.cat_device:
        argv += ["-r", profile.cat_device]
    if profile.cat_baud:
        argv += ["-s", str(profile.cat_baud)]
    if profile.civaddr:
        argv += ["-c", str(int(profile.civaddr, 16))]  # rigctld takes the address in decimal
    ptt_port = profile.ptt_device or profile.cat_device  # a separate keying cable, or the control port's line
    if profile.ptt_method in ("rts", "dtr") and ptt_port:
        argv += ["-P", profile.ptt_method.upper(), "-p", ptt_port]
    if profile.set_conf:
        argv += ["-C", profile.set_conf]
    return argv + line_states_conf(profile)


def line_states_conf(profile: RadioProfile) -> list[str]:
    """Hold RTS and DTR OFF when rigctld opens the port so the radio does not key for a moment
    (many CAT cables and the Digirig wire one of them to PTT). The PTT line is left to -P; a
    profile names lines in assert_lines to hold ON instead (cables powered from a line).
    Goes last on the command line: a later -C pair wins over one typed in expert settings."""
    held = [f"{line}_state={'ON' if line in profile.asserted_lines() else 'OFF'}"
            for line in ("rts", "dtr") if profile.ptt_method != line]
    if not held:
        return []
    # Backends that default to hardware handshake (the FT-891) refuse rig_open with a fixed
    # RTS state ("cannot set RTS with hardware handshake"); none of these cables flow-control.
    return ["-C", ",".join(["serial_handshake=None", *held])]


def serial_ports(by_id: str | None = None) -> list[str]:
    """One path per USB serial device: its lowest interface.

    A radio with a built-in USB port can show several interfaces (the IC-705 has if00, its
    CI-V port, and if02). They share a name up to ``-ifNN``, so they count as one device.
    """
    root = Path(by_id or SERIAL_BY_ID)
    if not root.is_dir():
        return []
    devices: dict[str, str] = {}
    for p in sorted(root.iterdir()):
        devices.setdefault(re.sub(r"-if\d+(-port\d+)?$", "", p.name), str(p))
    return sorted(devices.values())


def port_label(path: str) -> str:
    """A readable name from a by-id path: usb-Icom_Inc._IC-705_IC-705_12011136-if00 -> Icom Inc. IC-705."""
    name = re.sub(r"-if\d+(-port\d+)?$", "", Path(path).name)
    if not name.startswith("usb-"):
        return Path(path).name
    words = name[4:].split("_")
    if len(words) > 1 and re.fullmatch(r"(?=.*\d)[A-Za-z0-9]{4,}", words[-1]):
        words = words[:-1]  # the device's serial number; a CH340 (usb-1a86_USB_Serial) has none
    out: list[str] = []
    for w in words:
        if w and (not out or out[-1] != w):  # the IC-705 repeats its model name
            out.append(w)
    return " ".join(out) or Path(path).name


def auto_port(by_id: str | None = None) -> str:
    """The serial port when exactly one USB serial device is plugged in, else ""."""
    ports = serial_ports(by_id)
    return ports[0] if len(ports) == 1 else ""


def with_port(profile: RadioProfile) -> RadioProfile:
    """The profile with an empty ("automatic") port filled in, when one device makes it certain."""
    if not profile.needs_rigctld or profile.cat_device:
        return profile
    port = auto_port()
    return replace(profile, cat_device=port) if port else profile


def start_rigctld(profile: RadioProfile, port: int = DEFAULT_RIGCTLD_PORT,
                  bind: str = DEFAULT_RIGCTLD_BIND) -> subprocess.Popen:
    """Launch rigctld as a child process. It is a system package, never built here."""
    if shutil.which("rigctld") is None:
        raise CatError("rigctld not installed (apt install libhamlib-utils)")
    return subprocess.Popen(rigctld_argv(profile, port, bind), stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, errors="replace")


def _task_count(pid: int) -> int:
    """Threads in a process, from Linux /proc. 0 where there is none (macOS bench)."""
    try:
        return len(os.listdir(f"/proc/{pid}/task"))
    except OSError:
        return 0


class RigctldSupervisor:
    """Keeps one rigctld child running for the selected profile.

    Restarts it when the profile changes and, once ``start()`` has been called, when the
    child dies on its own (USB glitch, crash, kill). ``release_when`` returning true stops it
    and forgets the profile until the next ensure(), for a kit that no longer uses CAT.
    """

    def __init__(self, port: int = DEFAULT_RIGCTLD_PORT, settle_s: float = 2.0, check_s: float = 1.0,
                 bind: str = DEFAULT_RIGCTLD_BIND, release_when: Callable[[], bool] | None = None) -> None:
        self.port = port
        self.release_when = release_when
        self.bind = bind
        self.settle_s = settle_s
        self.check_s = check_s
        self.restarts = 0
        self._proc: subprocess.Popen | None = None
        self._argv: list[str] = []
        self._profile: RadioProfile | None = None
        self._mu = threading.RLock()  # the wizard's ensure() and the keep-alive thread both spawn
        self._log: deque[str] = deque(maxlen=RIGCTLD_LOG_LINES)
        self._log_mu = threading.Lock()
        self._log_gen = 0  # bumped when the log starts over, so a dying rigctld's drain cannot refill it
        self._drainer: threading.Thread | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def ensure(self, profile: RadioProfile) -> None:
        with self._mu:
            # Resolved on every spawn, so a keep-alive restart finds a radio plugged in later.
            resolved = with_port(profile)
            argv = rigctld_argv(resolved, self.port, self.bind)
            if self._proc is not None and self._proc.poll() is None and argv == self._argv:
                return
            self._kill()
            if argv != self._argv:  # a keep-alive restart of the same rigctld keeps its last words
                with self._log_mu:
                    self._log.clear()
                    self._log_gen += 1
            self._profile = profile
            self._proc, self._argv = start_rigctld(resolved, self.port, self.bind), argv
            stream = getattr(self._proc, "stderr", None)
            if stream is not None:  # drained by its own thread, or a chatty rigctld blocks on a full pipe
                drainer = threading.Thread(target=self._drain, args=(stream, self._log_gen),
                                           name="rigctld-stderr", daemon=True)
                self._drainer = None
                try:
                    drainer.start()
                except RuntimeError as e:  # "can't start new thread": an undrained rigctld would block later
                    self._kill()
                    stream.close()
                    raise CatError(f"rigctld could not start: {e}") from None
                self._drainer = drainer
            client = RigctldClient(port=self.port, timeout=0.5)
            deadline = time.monotonic() + self.settle_s
            while time.monotonic() < deadline and self._proc.poll() is None and not client.alive():
                time.sleep(0.1)

    def _drain(self, stream, gen: int) -> None:
        """Keep rigctld's last stderr lines. Ends when rigctld exits and the pipe closes."""
        try:
            for line in stream:
                line = line.strip()
                if line:
                    with self._log_mu:
                        if gen == self._log_gen:
                            self._log.append(line[:300])
        except (OSError, ValueError):
            pass  # the pipe closed under us: rigctld was killed
        finally:
            stream.close()

    def recent_log(self) -> list[str]:
        """rigctld's last stderr lines, oldest first, for a CAT failure's "Details"."""
        with self._log_mu:
            return list(self._log)

    def failure_log(self, profile: RadioProfile) -> list[str]:
        """Stop a rigctld that is not answering, so it writes out what it holds, and return its last
        lines. The keep-alive starts it again within check_s, and the next ensure() does too.

        Empty, and nothing stopped, unless the running rigctld is this profile's: a profile that
        could not start must not show, or kill, the previous one. One that answers on a second,
        slower try is left running: apps on the 4532 proxy share it.
        """
        with self._mu:
            try:
                argv = rigctld_argv(with_port(profile), self.port, self.bind)
            except ValueError:
                return []
            if argv != self._argv:
                return []
            if self._proc is not None and self._proc.poll() is None and RigctldClient(port=self.port, timeout=2.0).alive():
                return []  # it answers after all: older lines would describe an earlier run
            drainer = self._drainer
            self._kill()
        if drainer is not None:
            drainer.join(timeout=1.0)
        return self.recent_log()

    def check(self) -> bool:
        """Restart rigctld if it has exited since the last ensure(), or if its threads have piled
        up past MAX_RIGCTLD_THREADS. True when it restarted."""
        with self._mu:
            if self._profile is not None and self.release_when is not None and self.release_when():
                self._profile = None
                self._kill()
                return False
            if self._profile is None:
                return False
            if self._proc is not None and self._proc.poll() is None:
                n = _task_count(self._proc.pid)
                if n <= MAX_RIGCTLD_THREADS:
                    return False
                log.warning("rigctld has %d threads, over %d: the rig is not answering; restarting it",
                            n, MAX_RIGCTLD_THREADS)
                self._kill()
            try:
                self.ensure(self._profile)
            except Exception:  # noqa: BLE001 - anything escaping here would end the keep-alive thread for good
                return False  # rigctld missing or the port gone: try again next tick
            self.restarts += 1
            return True

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="rigctld-keepalive", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.check_s):
            try:
                self.check()
            except Exception:  # an escape here would end the keep-alive for good
                log.exception("rigctld keep-alive check failed")

    def release(self) -> None:
        """Stop rigctld and forget the profile, keeping the keep-alive running for the next ensure()."""
        with self._mu:
            self._profile = None
            self._kill()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        with self._mu:
            self._profile = None
            self._kill()

    def _kill(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
