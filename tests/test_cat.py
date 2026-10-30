"""rigctld client against a fake rigctld socket, plus the launch argv."""

import io
import os
import socket
import threading
import time
from dataclasses import replace

import pytest

from vector_bridge import cat, profiles


class FakeRigctld:
    """Answers extended-protocol commands the way rigctld 4.5 does."""

    def __init__(self, replies: dict[str, str]):
        self.replies = replies
        self.seen: list[str] = []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen()
        self.port = self.srv.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                line = conn.recv(1024).decode().strip()
                self.seen.append(line)
                cmd = line.lstrip("+\\").split()[0]
                conn.sendall(self.replies.get(cmd, f"{cmd}:\nRPRT -1\n").encode())

    def close(self):
        # On Linux close() alone does not wake a thread blocked in accept(): the port keeps listening.
        try:
            self.srv.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.srv.close()
        if self.thread:
            self.thread.join(timeout=2)


@pytest.fixture
def rig():
    f = FakeRigctld({
        "get_freq": "get_freq:\nFrequency: 14074000\nRPRT 0\n",
        "get_ptt": "get_ptt:\nPTT: 1\nRPRT 0\n",
        "set_ptt": "set_ptt: 0\nRPRT 0\n",
        "get_level": "get_level: SWR\nLevel Value: 1.4\nRPRT 0\n",
    })
    yield f
    f.close()


def test_reads_freq_ptt_and_levels(rig):
    c = cat.RigctldClient(port=rig.port)
    assert c.get_freq() == 14074000
    assert c.get_ptt() is True
    assert c.get_level("SWR") == pytest.approx(1.4)
    assert c.alive() is True


def test_tunes_frequency_and_data_mode():
    f = FakeRigctld({"set_freq": "set_freq: 7074000\nRPRT 0\n", "set_mode": "set_mode: PKTUSB 0\nRPRT 0\n",
                     "get_mode": "get_mode:\nMode: PKTUSB\nPassband: 2400\nRPRT 0\n"})
    try:
        c = cat.RigctldClient(port=f.port)
        c.set_freq(7074000)
        c.set_mode("PKTUSB")
        assert c.get_mode() == "PKTUSB"
        assert f.seen[:2] == ["+\\set_freq 7074000", "+\\set_mode PKTUSB 0"]
    finally:
        f.close()


def test_reads_the_bare_level_line_real_rigctld_sends():
    # Captured from rigctld 4.6.2 with an IC-705, 2026-09-20: no "Level Value:" in front of the number.
    f = FakeRigctld({"get_level": "get_level: ALC\n0.37\nRPRT 0\n"})
    try:
        assert cat.RigctldClient(port=f.port).get_level("ALC") == pytest.approx(0.37)
    finally:
        f.close()


def test_set_ptt_sends_extended_command(rig):
    cat.RigctldClient(port=rig.port).set_ptt(False)
    assert rig.seen[-1] == "+\\set_ptt 0"


def test_nonzero_rprt_and_unreachable_raise(rig):
    c = cat.RigctldClient(port=rig.port)
    with pytest.raises(cat.CatError, match="RPRT -1"):
        c.command("get_vfo")
    rig.close()
    with pytest.raises(cat.CatError, match="unreachable"):
        cat.RigctldClient(port=rig.port).get_freq()
    assert cat.RigctldClient(port=rig.port).alive() is False


def test_rigctld_argv_from_profile():
    p = profiles.get("ft-891")
    p.cat_device = "/dev/serial/by-id/usb-FTDI-if00"
    argv = cat.rigctld_argv(p, port=4532)
    assert argv[:3] == ["rigctld", "-m", "1036"]
    assert "-r" in argv and "/dev/serial/by-id/usb-FTDI-if00" in argv and "38400" in argv
    with pytest.raises(ValueError):
        cat.rigctld_argv(profiles.get("digirig-generic"))


def test_rigctld_argv_keys_a_line_with_the_dummy_rig_when_there_is_no_cat():
    p = replace(profiles.get("digirig-generic"), cat_device="/dev/serial/by-id/usb-Silicon_Labs-if00-port0")
    argv = cat.rigctld_argv(p)
    assert argv[:3] == ["rigctld", "-m", str(cat.DUMMY_RIG_MODEL)]
    assert argv[argv.index("-P") + 1] == "RTS" and argv[argv.index("-p") + 1] == p.cat_device
    assert "-r" not in argv and "-s" not in argv  # the dummy rig opens no CAT port
    assert "-P DTR" in " ".join(cat.rigctld_argv(replace(p, ptt_method="dtr")))
    with pytest.raises(ValueError):
        cat.rigctld_argv(profiles.get("signalink-generic"))  # VOX: no rigctld at all


def test_rigctld_binds_loopback_unless_asked():
    p = profiles.get("kx2")
    argv = cat.rigctld_argv(p)
    assert argv[argv.index("-T") + 1] == "127.0.0.1"
    argv = cat.rigctld_argv(p, bind="0.0.0.0")
    assert argv[argv.index("-T") + 1] == "0.0.0.0"


def test_rigctld_holds_both_lines_off_at_port_open_for_cat_keying():
    argv = cat.rigctld_argv(replace(profiles.get("ic-705"), cat_device="/dev/ttyACM0"))
    assert argv[-2:] == ["-C", "serial_handshake=None,rts_state=OFF,dtr_state=OFF"]


def test_rigctld_holds_only_the_line_that_is_not_ptt_off():
    argv = cat.rigctld_argv(replace(profiles.get("kx2"), cat_device="/dev/ttyUSB0", ptt_method="rts"))
    assert argv[-2:] == ["-C", "serial_handshake=None,dtr_state=OFF"]
    dummy = cat.rigctld_argv(replace(profiles.get("digirig-generic"), cat_device="/dev/ttyUSB0", ptt_method="dtr"))
    assert dummy[-2:] == ["-C", "serial_handshake=None,rts_state=OFF"]


def test_a_profile_can_keep_a_line_on_for_a_cable_powered_from_it():
    p = replace(profiles.get("ft-891"), cat_device="/dev/ttyUSB0", assert_lines="dtr")
    argv = cat.rigctld_argv(p)
    assert argv[-2:] == ["-C", "serial_handshake=None,rts_state=OFF,dtr_state=ON"]
    with pytest.raises(ValueError, match="assert_lines"):
        replace(p, assert_lines="cts").validate()


IC705 = "usb-Icom_Inc._IC-705_IC-705_12011136"
KXUSB = "usb-FTDI_FT231X_USB_UART_D30A1B2C"


def _by_id(root, *names):
    root.mkdir(exist_ok=True)
    for n in names:
        (root / n).touch()
    return str(root)


def test_automatic_port_takes_the_one_usb_serial_device(tmp_path):
    kx2 = _by_id(tmp_path / "a", f"{KXUSB}-if00-port0")
    assert cat.auto_port(kx2) == f"{kx2}/{KXUSB}-if00-port0"
    # The IC-705's two interfaces are one device; if00 is its CI-V port.
    ic705 = _by_id(tmp_path / "b", f"{IC705}-if02", f"{IC705}-if00")
    assert cat.serial_ports(ic705) == [f"{ic705}/{IC705}-if00"]
    assert cat.auto_port(ic705) == f"{ic705}/{IC705}-if00"


def test_automatic_port_refuses_to_guess(tmp_path):
    both = _by_id(tmp_path, f"{IC705}-if00", f"{IC705}-if02", f"{KXUSB}-if00-port0")
    assert len(cat.serial_ports(both)) == 2 and cat.auto_port(both) == ""
    assert cat.serial_ports(str(tmp_path / "missing")) == [] and cat.auto_port(str(tmp_path / "missing")) == ""


def test_supervisor_fills_an_empty_port_but_never_overrides_a_typed_one(tmp_path, monkeypatch):
    monkeypatch.setattr(cat, "SERIAL_BY_ID", _by_id(tmp_path, f"{IC705}-if00", f"{IC705}-if02"))
    seen = []
    monkeypatch.setattr(cat, "start_rigctld", lambda prof, port, bind: seen.append(prof.cat_device) or _FakeProc())
    sup = cat.RigctldSupervisor(settle_s=0)
    sup.ensure(profiles.get("ic-705"))
    sup.ensure(replace(profiles.get("ic-705"), cat_device="/dev/ttyACM1"))
    assert seen == [f"{tmp_path}/{IC705}-if00", "/dev/ttyACM1"]
    # Keying by RTS with no CAT still needs the port; VOX needs nothing.
    assert cat.with_port(profiles.get("digirig-generic")).cat_device == f"{tmp_path}/{IC705}-if00"
    assert cat.with_port(profiles.get("signalink-generic")).cat_device == ""


def test_supervisor_passes_its_bind_to_rigctld(monkeypatch):
    seen = []
    monkeypatch.setattr(cat, "start_rigctld", lambda prof, port, bind: seen.append(bind) or _FakeProc())
    sup = cat.RigctldSupervisor(settle_s=0, bind="0.0.0.0")
    sup.ensure(profiles.get("kx2"))
    assert seen == ["0.0.0.0"]


class _FakeProc:
    pid = 0

    def __init__(self):
        self.code = None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = -15

    def wait(self, timeout=None):
        return self.code


def test_supervisor_restarts_a_dead_rigctld(monkeypatch):
    spawned = []
    monkeypatch.setattr(cat, "start_rigctld", lambda prof, port, bind: spawned.append(_FakeProc()) or spawned[-1])
    sup = cat.RigctldSupervisor(settle_s=0)
    assert sup.check() is False  # nothing chosen yet, nothing to revive
    sup.ensure(profiles.get("kx2"))
    assert sup.check() is False and len(spawned) == 1  # alive: left alone
    spawned[0].code = -9  # killed behind our back
    assert sup.check() is True and len(spawned) == 2 and sup.restarts == 1
    spawned[1].code = -9

    def boom(prof, port, bind):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cat, "start_rigctld", boom)
    assert sup.check() is False  # any failure is swallowed so the keep-alive thread survives
    sup.stop()
    assert sup.check() is False and len(spawned) == 2  # stop() means stay stopped


def test_supervisor_restarts_a_rigctld_whose_threads_pile_up(monkeypatch):
    """A rig that stops answering leaves one rigctld thread per abandoned connection (2026-10-04)."""
    spawned, threads = [], [4]
    monkeypatch.setattr(cat, "start_rigctld", lambda prof, port, bind: spawned.append(_FakeProc()) or spawned[-1])
    monkeypatch.setattr(cat, "_task_count", lambda pid: threads[0])
    sup = cat.RigctldSupervisor(settle_s=0)
    sup.ensure(profiles.get("ic-705"))
    threads[0] = cat.MAX_RIGCTLD_THREADS
    assert sup.check() is False and len(spawned) == 1  # at the limit: left alone
    threads[0] += 1
    assert sup.check() is True and spawned[0].code == -15 and len(spawned) == 2 and sup.restarts == 1


def test_keepalive_survives_an_unexpected_error(monkeypatch):
    monkeypatch.setattr(cat, "start_rigctld", lambda prof, port, bind: _FakeProc())
    calls = []

    def release_when():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("unexpected")
        return False

    sup = cat.RigctldSupervisor(settle_s=0, check_s=0.01, release_when=release_when)
    sup.ensure(profiles.get("kx2"))
    sup.start()
    deadline = time.monotonic() + 2
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    sup.stop()
    assert len(calls) >= 3  # still checking after the error


def test_supervisor_lets_rigctld_go_once_the_kit_stops_using_cat(monkeypatch):
    spawned, release = [], [False]
    monkeypatch.setattr(cat, "start_rigctld", lambda prof, port, bind: spawned.append(_FakeProc()) or spawned[-1])
    sup = cat.RigctldSupervisor(settle_s=0, release_when=lambda: release[0])
    sup.ensure(profiles.get("kx2"))
    release[0] = True  # switched to VOX with nothing keyed
    assert sup.check() is False and spawned[0].code == -15
    assert sup.check() is False and len(spawned) == 1  # stays stopped: not revived on VOX
    sup.ensure(profiles.get("kx2"))  # back on CAT, the CAT step starts it again
    assert len(spawned) == 2


def test_a_civ_address_goes_to_rigctld_in_decimal():
    argv = cat.rigctld_argv(replace(profiles.get("ic-705"), civaddr="A4"))
    assert argv[argv.index("-c") + 1] == "164"
    assert "-c" not in cat.rigctld_argv(profiles.get("ic-705"))  # empty: Hamlib's own default


def test_port_labels_come_from_the_usb_name_without_the_serial_number():
    assert cat.port_label("/dev/serial/by-id/usb-Icom_Inc._IC-705_IC-705_12011136-if00") == "Icom Inc. IC-705"
    assert cat.port_label("/dev/serial/by-id/usb-FTDI_FT231X_USB_UART_D30A1B2C-if00-port0") == "FTDI FT231X USB UART"
    assert cat.port_label("/dev/ttyAMA0") == "ttyAMA0"
    assert cat.port_label("/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0") == "1a86 USB Serial"  # no serial number


class _ChattyProc(_FakeProc):
    def __init__(self, text):
        super().__init__()
        self.stderr = io.StringIO(text)


def test_supervisor_keeps_rigctlds_last_stderr_lines_until_the_command_changes(monkeypatch):
    texts = iter(["serial_open: Unable to open /dev/ttyACM0\n\nIO error\n", "", "rig_open: timeout\n"])
    monkeypatch.setattr(cat, "start_rigctld", lambda prof, port, bind: _ChattyProc(next(texts)))
    sup = cat.RigctldSupervisor(settle_s=0)
    prof = replace(profiles.get("ic-705"), cat_device="/dev/ttyACM0")

    def log_settles():
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(t.name == "rigctld-stderr" for t in threading.enumerate()):
            time.sleep(0.01)
        return sup.recent_log()

    sup.ensure(prof)
    assert log_settles() == ["serial_open: Unable to open /dev/ttyACM0", "IO error"]
    sup._proc.code = 1  # rigctld exits; the keep-alive restarts the same command and keeps its last words
    assert sup.check() and log_settles() == ["serial_open: Unable to open /dev/ttyACM0", "IO error"]
    sup.ensure(replace(prof, cat_baud=9600))  # a new command starts a new log
    assert log_settles() == ["rig_open: timeout"]


def test_a_rigctld_that_cannot_get_its_drain_thread_is_stopped_not_left_to_block(monkeypatch):
    proc = _ChattyProc("")
    monkeypatch.setattr(cat, "start_rigctld", lambda prof, port, bind: proc)

    class NoThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(cat.threading, "Thread", NoThread)
    sup = cat.RigctldSupervisor(settle_s=0)
    with pytest.raises(cat.CatError, match="can't start new thread"):
        sup.ensure(replace(profiles.get("ic-705"), cat_device="/dev/ttyACM0"))
    assert proc.code == -15 and proc.stderr.closed and sup._proc is None
    assert sup.failure_log(replace(profiles.get("ic-705"), cat_device="/dev/ttyACM0")) == []  # no thread to join


class _HoldingProc(_FakeProc):
    """Like Hamlib 4.6.2's rigctld: says nothing on stderr until it is stopped."""

    def __init__(self, text):
        super().__init__()
        r, self._w = os.pipe()
        self.stderr = os.fdopen(r, "r")
        self._text = text

    def terminate(self):
        super().terminate()
        os.write(self._w, self._text.encode())
        os.close(self._w)


def test_a_failure_stops_rigctld_so_it_writes_out_what_it_held(monkeypatch):
    procs = []
    monkeypatch.setattr(cat, "start_rigctld", lambda prof, port, bind: procs.append(
        _HoldingProc("serial_open: Unable to open /dev/ttyACM9 - No such file or directory\nIO error\n")) or procs[-1])
    sup = cat.RigctldSupervisor(settle_s=0)
    prof = replace(profiles.get("ic-705"), cat_device="/dev/ttyACM9")
    sup.ensure(prof)
    assert sup.recent_log() == []  # nothing while it runs
    monkeypatch.setattr(cat.RigctldClient, "alive", lambda self: False)
    assert sup.failure_log(replace(prof, cat_baud=9600)) == []  # not the running rigctld's profile: left alone
    assert procs[0].code is None
    assert sup.failure_log(prof) == ["serial_open: Unable to open /dev/ttyACM9 - No such file or directory", "IO error"]
    assert procs[0].code == -15 and sup.check() and len(procs) == 2  # the keep-alive starts it again
    assert sup.recent_log()[-1] == "IO error"  # same command: its last words stay for the next look


def test_a_rigctld_that_answers_on_a_slower_try_is_not_stopped(monkeypatch):
    procs = []
    monkeypatch.setattr(cat, "start_rigctld", lambda prof, port, bind: procs.append(_HoldingProc("")) or procs[-1])
    monkeypatch.setattr(cat.RigctldClient, "alive", lambda self: True)
    sup = cat.RigctldSupervisor(settle_s=0)
    prof = replace(profiles.get("ic-705"), cat_device="/dev/ttyACM0")
    sup.ensure(prof)
    assert sup.failure_log(prof) == [] and procs[0].code is None  # apps on 4532 keep their rigctld
    procs[0].terminate()


def test_supervisor_release_stops_rigctld_and_keeps_it_stopped(monkeypatch):
    spawned = []
    monkeypatch.setattr(cat, "start_rigctld", lambda prof, port, bind: spawned.append(_FakeProc()) or spawned[-1])
    sup = cat.RigctldSupervisor(settle_s=0)
    sup.ensure(profiles.get("kx2"))
    sup.release()  # a factory reset
    assert spawned[0].code == -15
    assert sup.check() is False and len(spawned) == 1  # the keep-alive does not bring it back
    sup.ensure(profiles.get("kx2"))
    assert len(spawned) == 2


def test_expert_settings_and_a_separate_keying_port_reach_rigctld():
    kx2 = replace(profiles.get("kx2"), cat_device="/dev/ttyUSB0", ptt_method="rts", ptt_device="/dev/ttyUSB1",
                  set_conf="rts_state=OFF")
    argv = cat.rigctld_argv(kx2)
    assert argv[argv.index("-r") + 1] == "/dev/ttyUSB0" and argv[argv.index("-p") + 1] == "/dev/ttyUSB1"
    assert argv[argv.index("-C") + 1] == "rts_state=OFF"
    same = cat.rigctld_argv(replace(kx2, ptt_device="", set_conf=""))
    assert same[same.index("-p") + 1] == "/dev/ttyUSB0"
    assert same[same.index("-C") + 1] == "serial_handshake=None,dtr_state=OFF" and same.count("-C") == 1  # only the held line, no expert pairs
    dummy = cat.rigctld_argv(replace(profiles.get("digirig-generic"), cat_device="/dev/ttyUSB0", set_conf="ptt_share=1"))
    assert dummy[dummy.index("-C") + 1] == "ptt_share=1"
