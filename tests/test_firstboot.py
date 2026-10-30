"""scripts/image/vb-firstboot against stub system commands: what runs, in what order, and
that a failure leaves the card armed. Lockdown is not its job: vb-ap-apply does that."""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "image" / "vb-firstboot"

STUB = """#!/bin/bash
echo "$(basename "$0") $*" >> "$STUB_LOG"
[ "$(basename "$0")" = "${STUB_FAIL:-}" ] && exit 1
case "$(basename "$0") $*" in
  "findmnt -no FSTYPE /") echo "${STUB_ROOT_FS:-ext4}" ;;
  "findmnt -no SOURCE "*) echo /dev/mmcblk0p3 ;;
  "lsblk "*) echo mmcblk0 ;;
esac
"""
COMMANDS = ["findmnt", "lsblk", "ssh-keygen", "systemd-machine-id-setup", "sfdisk", "partx", "mount",
            "resize2fs", "raspi-config", "reboot", "python"]


def run(tmp_path, armed=True, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in COMMANDS:
        (bin_dir / name).write_text(STUB)
        (bin_dir / name).chmod(0o755)
    etc = tmp_path / "etc"
    (etc / "ssh").mkdir(parents=True)
    (etc / "ssh" / "ssh_host_ed25519_key").write_text("golden")
    (etc / "machine-id").write_text("golden\n")
    flag = tmp_path / "flag"
    if armed:
        flag.touch()
    log = tmp_path / "log"
    log.touch()
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, check=False, env={
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_LOG": str(log), "VB_FIRSTBOOT_FLAG": str(flag),
        "VB_FIRSTBOOT_ETC": str(etc), "VB_FIRSTBOOT_DBUS_ID": str(tmp_path / "dbus-id"), "VB_FIRSTBOOT_PYTHON": str(bin_dir / "python"), **env})
    return r, log.read_text().splitlines(), flag, etc


def test_unarmed_does_nothing(tmp_path):
    r, calls, _, etc = run(tmp_path, armed=False)
    assert r.returncode == 0 and calls == []
    assert (etc / "ssh" / "ssh_host_ed25519_key").exists()


def test_refuses_under_the_overlay(tmp_path):
    r, calls, flag, _ = run(tmp_path, STUB_ROOT_FS="overlay")
    assert r.returncode == 3 and flag.exists()
    assert "reboot " not in calls and "ssh-keygen -A" not in calls


AP = "python -m vector_bridge.apsetup first-boot-open"


def test_regenerates_opens_the_access_point_and_leaves_the_card_writable(tmp_path):
    r, calls, flag, etc = run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert not flag.exists()
    assert not (etc / "ssh" / "ssh_host_ed25519_key").exists() and not (etc / "machine-id").exists()
    order = ["ssh-keygen -A", "systemd-machine-id-setup ", "sfdisk --no-reread --force -N 3 /dev/mmcblk0",
             "resize2fs /dev/mmcblk0p3", AP]
    assert [calls.index(c) for c in order] == sorted(calls.index(c) for c in order)
    assert not [c for c in calls if c.startswith(("raspi-config", "reboot"))]


def test_access_point_failure_leaves_the_card_armed(tmp_path):
    r, _, flag, _ = run(tmp_path, STUB_FAIL="python")
    assert r.returncode != 0 and flag.exists()


def test_host_key_failure_stops_before_the_access_point(tmp_path):
    r, calls, flag, _ = run(tmp_path, STUB_FAIL="ssh-keygen")
    assert r.returncode != 0 and flag.exists() and AP not in calls


def test_grow_failure_is_reported_and_does_not_block_the_access_point(tmp_path):
    r, calls, flag, _ = run(tmp_path, STUB_FAIL="sfdisk")
    assert r.returncode == 0 and not flag.exists()
    assert "could not grow" in r.stderr and "resize2fs /dev/mmcblk0p3" not in calls and AP in calls
