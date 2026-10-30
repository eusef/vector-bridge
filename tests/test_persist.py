"""Persist writes are atomic and bracketed by a rw/ro remount when on the partition."""

import os

import pytest

from vector_bridge import persist, profiles


def test_interrupted_preset_save_keeps_previous_good_version(tmp_path, monkeypatch):
    path = tmp_path / "presets" / "kx2.toml"
    good = profiles.dumps(profiles.get("kx2"))
    persist.save(path, good, remount=lambda m: None)
    assert profiles.load_all(tmp_path / "presets")["kx2"].display_name == "Elecraft KX2"

    def power_cut(src, dst):  # the cut lands after the temp file is written, before the rename
        raise OSError("power lost")

    monkeypatch.setattr(os, "replace", power_cut)
    changed = good.replace("Elecraft KX2", "Renamed KX2")
    with pytest.raises(OSError):
        persist.save(path, changed, remount=lambda m: None)
    monkeypatch.undo()

    assert path.read_text() == good
    assert profiles.load_all(tmp_path / "presets")["kx2"].display_name == "Elecraft KX2"
    assert [p.name for p in path.parent.iterdir()] == ["kx2.toml"], "no temp file left behind"


def test_remount_brackets_write_only_on_persist_partition(tmp_path, monkeypatch):
    calls = []
    path = tmp_path / "vector-bridge" / "config.toml"
    persist.save(path, "a = 1\n", remount=calls.append)
    assert calls == [], "not on the partition: no remount"

    monkeypatch.setattr(persist, "PERSIST_MOUNT", tmp_path)
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p) == str(tmp_path))
    persist.save(path, "a = 2\n", remount=calls.append)
    assert calls == ["rw", "ro"] and path.read_text() == "a = 2\n"

    calls.clear()
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("cut")))
    with pytest.raises(OSError):
        persist.save(path, "a = 3\n", remount=calls.append)
    assert calls == ["rw", "ro"], "goes back to read-only even when the write fails"


def test_system_remount_uses_mount_syscall_and_falls_back_to_sudo(monkeypatch):
    ran = []
    monkeypatch.setattr(persist.subprocess, "run", lambda argv, **kw: ran.append(argv))
    monkeypatch.setattr(os, "geteuid", lambda: 999)

    monkeypatch.setattr(persist, "_syscall_remount", lambda mode: True)
    persist._system_remount("rw")
    assert ran == [], "a process that may call mount(2) never reaches sudo (root)"

    monkeypatch.setattr(persist, "_syscall_remount", lambda mode: False)
    persist._system_remount("rw")
    assert ran == [["sudo", "-n", "/usr/bin/mount", "-o", "remount,rw", str(persist.PERSIST_MOUNT)]]


def test_a_failed_sudo_remount_raises_oserror_for_the_save_handlers(monkeypatch):
    def refused(argv, **kw):
        raise persist.subprocess.CalledProcessError(1, argv, stderr=b"sudo: a password is required\n")

    monkeypatch.setattr(persist.subprocess, "run", refused)
    monkeypatch.setattr(os, "geteuid", lambda: 999)
    monkeypatch.setattr(persist, "_syscall_remount", lambda mode: False)
    with pytest.raises(OSError, match="a password is required"):
        persist._system_remount("rw")
