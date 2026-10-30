"""The Wi-Fi country (R-D7): what the page may save, what root reads back, and the first-boot form."""
import json
import os

import pytest

from vector_bridge import apsetup, wificountry


@pytest.mark.parametrize("code,expected", [
    ("US", "US"), (" de ", "DE"), ("", ""), ("00", ""), ("XX", ""), ("USA", ""), (None, ""), (["US"], ""),
])
def test_check(code, expected):
    assert wificountry.check(code) == expected


def test_saved_reads_only_a_regular_file_with_a_country(tmp_path):
    path = tmp_path / "wifi-country.json"
    assert wificountry.saved(path) == ""
    for bad in ("not json", '["US"]', '{"country": "XX"}', '{"country": "US; reboot"}'):
        path.write_text(bad)
        assert wificountry.saved(path) == ""
    path.write_text('{"country": "GB"}')
    assert wificountry.saved(path) == "GB"
    # Root reads this file from a directory the web user owns: a link to another file counts as nothing.
    target = tmp_path / "elsewhere.json"
    target.write_text('{"country": "GB"}')
    path.unlink()
    path.symlink_to(target)
    assert wificountry.saved(path) == ""
    path.unlink()
    os.mkfifo(path)
    assert wificountry.saved(path) == ""     # and a FIFO does not hang it


def test_the_kernel_command_line_counts_when_nothing_is_saved(tmp_path, monkeypatch):
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("console=tty1 rootwait ds=nocloud;i=rpi-imager-1 cfg80211.ieee80211_regdom=US\n")
    assert wificountry.cmdline(cmdline) == "US"
    monkeypatch.setattr(wificountry, "CMDLINE", cmdline)
    monkeypatch.setattr(wificountry, "SAVED", tmp_path / "wifi-country.json")
    assert wificountry.current() == "US"
    wificountry.SAVED.write_text('{"country": "DE"}')
    assert wificountry.current() == "DE"     # the owner's choice wins
    cmdline.write_text("console=tty1 rootwait\n")
    wificountry.SAVED.unlink()
    assert wificountry.current() == ""
    assert wificountry.cmdline(tmp_path / "missing") == ""


def test_handle_saves_a_country_and_refuses_anything_else(tmp_path):
    path = tmp_path / "wifi-country.json"
    assert wificountry.handle({"country": "Narnia"}, path)[0] == 400
    assert wificountry.handle({}, path)[0] == 400 and not path.exists()
    assert wificountry.handle({"country": "nz"}, path) == (200, {"ok": True, "country": "NZ"})
    assert json.loads(path.read_text()) == {"country": "NZ"}
    assert path.stat().st_mode & 0o777 == 0o644


def test_the_first_boot_form_saves_the_country_with_the_wifi(tmp_path):
    pending, country = tmp_path / "ap-pending.json", tmp_path / "wifi-country.json"
    ok = {"suffix": "K7ABC", "passphrase": "correct horse"}
    code, out = apsetup.handle({**ok, "country": "XX"}, pending, country)
    assert code == 400 and "Country" in " ".join(out["errors"])
    assert not pending.exists() and not country.exists()
    assert apsetup.handle({**ok, "country": ""}, pending, country)[0] == 200     # optional
    assert pending.exists() and not country.exists()
    assert apsetup.handle({**ok, "country": "CA"}, pending, country)[0] == 200
    assert json.loads(country.read_text()) == {"country": "CA"}
