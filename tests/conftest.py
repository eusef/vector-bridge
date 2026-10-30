import pytest

from vector_bridge import admin, wifiradio


@pytest.fixture(autouse=True)
def _radio_lock_off_the_system(tmp_path, monkeypatch):
    """wifiradio's lock and skip list live in /run on the Pi; tests keep them in tmp."""
    monkeypatch.setattr(wifiradio, "LOCK", tmp_path / "vb-wifi-radio.lock")
    monkeypatch.setattr(wifiradio, "SKIP", tmp_path / "vb-wifi-radio.skip")


@pytest.fixture(autouse=True)
def _admin_pin(request, tmp_path, monkeypatch):
    """The PIN hash lives in tmp. Tests of the handlers behind the PIN hold a session, so they
    stay about those handlers; tests marked `admin_pin` exercise the real check (test_admin.py)."""
    monkeypatch.setattr(admin, "PIN_PATH", tmp_path / "admin-pin.json")
    if "admin_pin" not in request.keywords:
        monkeypatch.setattr(admin.Admin, "authorized", lambda self, headers: True)
