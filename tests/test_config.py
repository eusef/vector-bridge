import tomllib
from pathlib import Path

from vector_bridge import config as c


def test_defaults_are_conservative():
    cfg = c.Config()
    assert cfg.tx_gain == 20 and cfg.rx_gain == 50
    assert (cfg.audio_port, cfg.control_port) == (5100, 5101)


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "config.toml"
    cfg = c.Config(device_name='My "Bridge"', usb_vendor_id="0d8c", tx_gain=35)
    c.save(cfg, path)
    assert c.load(path) == cfg
    assert "# Playback" in path.read_text()


def test_missing_file_gives_defaults(tmp_path):
    assert c.load(tmp_path / "nope.toml") == c.Config()


def test_rigctl_proxy_reaches_the_bridge_wifi_by_default(tmp_path):
    assert c.Config().rigctl_access == "wifi"  # R-D9
    path = tmp_path / "config.toml"
    c.save(c.Config(rigctl_access="all"), path)
    assert 'rigctl_access = "all"' in path.read_text()
    assert c.load(path).rigctl_access == "all"


def test_old_rigctld_network_setting_carries_over(tmp_path):
    """Every card wrote rigctld_network = false (the old default); true meant every interface."""
    path = tmp_path / "config.toml"
    for old, new in (("true", "all"), ("false", "ap")):
        path.write_text(f"rigctld_network = {old}\n")
        assert c.load(path).rigctl_access == new
    path.write_text('rigctld_network = true\nrigctl_access = "off"\n')
    assert c.load(path).rigctl_access == "off"  # the new setting wins once it is saved
    c.save(c.load(path), path)
    assert "rigctld_network" not in path.read_text()


def test_example_config_matches_defaults():
    example = tomllib.loads((Path(__file__).parent.parent / "config" / "vector-bridge.example.toml").read_text())
    assert example == c.Config().as_dict()


def test_damaged_file_gives_defaults_and_save_keeps_a_copy(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("tx_gain = 35\nrx_gain = = 9\n")
    assert c.problem(path) and c.load(path) == c.Config()
    c.save(c.Config(tx_gain=40), path)
    assert c.load(path).tx_gain == 40 and c.problem(path) is None
    assert (tmp_path / "config.toml.bad").read_text() == "tx_gain = 35\nrx_gain = = 9\n"


def test_transmit_time_limit_defaults_to_120_s():
    # REQ-AUDIO-021: one setting for the TX audio mute and the 4532 key cap, default 120 s (covers WSPR)
    from vector_bridge import rigproxy, txcap
    assert c.Config().tx_audio_cap_s == 120
    assert txcap.DEFAULT_CAP_S == 120 and rigproxy.MAX_KEY_S == 120.0
