"""USB topology: the sound card that shares a USB device or hub with the radio's serial port."""

import os
from types import SimpleNamespace

from vector_bridge import usbtopo

BUS = ("devices", "platform", "soc", "3f980000.usb", "usb1", "1-1")


def _iface(root, *path):
    d = root.joinpath(*BUS, *path)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _link(at, target):
    at.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, at)


def sysfs(tmp_path):
    """An IC-705 at 1-1.2 (serial + audio on one device), a Digirig hub at 1-1.4, a spare card at 1-1.3."""
    tty, snd = tmp_path / "class" / "tty", tmp_path / "class" / "sound"
    _link(tty / "ttyACM0" / "device", _iface(tmp_path, "1-1.2", "1-1.2:1.0"))
    _link(tty / "ttyUSB0" / "device", _iface(tmp_path, "1-1.4", "1-1.4.1", "1-1.4.1:1.0"))
    _link(tty / "ttyAMA0" / "device", tmp_path.joinpath(*BUS[:3], "3f201000.serial"))
    _link(snd / "card1" / "device", _iface(tmp_path, "1-1.2", "1-1.2:1.3"))
    _link(snd / "card2" / "device", _iface(tmp_path, "1-1.3", "1-1.3:1.0"))
    _link(snd / "card3" / "device", _iface(tmp_path, "1-1.4", "1-1.4.2", "1-1.4.2:1.0"))
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "ttyACM0").touch()
    _link(dev / "serial" / "by-id" / "usb-Icom_IC-705-if00", dev / "ttyACM0")
    return tty, snd, dev


def test_ports_and_cards_resolve_to_their_usb_device(tmp_path):
    tty, snd, dev = sysfs(tmp_path)
    assert usbtopo.serial_device(str(dev / "serial" / "by-id" / "usb-Icom_IC-705-if00"), tty) == "1-1.2"
    assert usbtopo.serial_device("/dev/ttyUSB0", tty) == "1-1.4.1"
    assert [usbtopo.card_device(i, snd) for i in (1, 2, 3)] == ["1-1.2", "1-1.3", "1-1.4.2"]


def test_ports_off_usb_or_missing_have_no_device(tmp_path):
    tty, snd, _ = sysfs(tmp_path)
    assert usbtopo.serial_device("/dev/ttyAMA0", tty) == ""
    assert usbtopo.serial_device("/dev/ttyUSB9", tty) == ""
    assert usbtopo.serial_device("", tty) == ""
    assert usbtopo.card_device(7, snd) == ""


def test_the_card_on_the_radios_own_usb_device_wins():
    cards = [SimpleNamespace(usb_path=p) for p in ("1-1.3", "1-1.2", "1-1.4.2")]
    assert usbtopo.best_match("1-1.2", cards) is cards[1]


def test_a_digirig_card_behind_the_same_hub_matches():
    cards = [SimpleNamespace(usb_path=p) for p in ("1-1.3", "1-1.4.2")]
    assert usbtopo.best_match("1-1.4.1", cards) is cards[1]


def test_no_close_card_or_a_tie_leaves_the_choice_to_the_service():
    far = [SimpleNamespace(usb_path=p) for p in ("1-1.3", "1-1.5")]
    assert usbtopo.best_match("1-1.4.1", far) is None
    assert usbtopo.best_match("1-1.2", far) is None  # both share the radio's hub: a tie
    tie = [SimpleNamespace(usb_path=p) for p in ("1-1.4.2", "1-1.4.3")]
    assert usbtopo.best_match("1-1.4.1", tie) is None
    assert usbtopo.best_match("", far) is None
    assert usbtopo.best_match("1-1.2", []) is None
    assert usbtopo.best_match("1-1.2", [SimpleNamespace(usb_path="")]) is None


def test_ports_on_the_root_hub_are_not_siblings():
    assert usbtopo.closeness("1-1", "1-2") == 0
    assert usbtopo.closeness("1-1.2", "1-1.2") == 2
    assert usbtopo.closeness("1-1.2", "1-1.3") == 1
