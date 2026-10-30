"""Management CLI against the running fake service: status table and gain persistence."""

import pytest

from vector_bridge.cli import main
from vector_bridge.config import Config, load
from vector_bridge.service import BridgeService


@pytest.fixture
def running(tmp_path):
    cfg_path = tmp_path / "config.toml"
    svc = BridgeService(Config(audio_port=0, control_port=0), audio="fake", bind="127.0.0.1", config_path=cfg_path)
    svc.start()
    yield svc, cfg_path
    svc.stop()


def test_status_table_and_gain(running, capsys):
    svc, cfg_path = running
    port = str(svc.control_port)
    assert main(["--config", str(cfg_path), "status", "--port", port]) == 0
    out = capsys.readouterr().out
    assert "rx_packets_sent" in out and "alsa_device" in out and "fake:1kHz-tone" in out

    assert main(["--config", str(cfg_path), "audio", "gain", "--port", port, "--tx", "33", "--rx", "44"]) == 0
    assert "tx=33 rx=44" in capsys.readouterr().out
    assert (load(cfg_path).tx_gain, load(cfg_path).rx_gain) == (33, 44)

    assert main(["--config", str(cfg_path), "status", "--port", "1"]) == 1  # nothing listening
