"""ALSA backend without hardware: card selection by USB id, stereo fallback, error mapping."""

import sys
import types
from typing import ClassVar

import numpy as np
import pytest

from vector_bridge.audio import AudioDeviceError
from vector_bridge.audio.alsa import (
    DEAD_READS_LIMIT,
    AlsaBackend,
    list_cards,
    select_card,
)
from vector_bridge.config import Config
from vector_bridge.protocol import FRAME_SAMPLES


@pytest.fixture
def proc_asound(tmp_path):
    root = tmp_path / "asound"
    root.mkdir()
    (root / "cards").write_text(
        " 0 [vc4hdmi        ]: vc4-hdmi - vc4-hdmi\n"
        "                      vc4-hdmi\n"
        " 1 [Device         ]: USB-Audio - USB Audio Device\n"
        "                      C-Media Electronics Inc. USB Audio Device at usb-3f980000.usb-1, full speed\n"
    )
    (root / "card0").mkdir()
    (root / "card1").mkdir()
    (root / "card1" / "usbid").write_text("0d8c:0014\n")
    return root


class StubPCM:
    """Stereo-only card. Mono open either raises (many dongles) or, like the C-Media
    0d8c:0014, silently negotiates 2 channels and admits it only in info().
    Scripted read results."""

    mono_silently_stereo = False
    format_name = None  # None: info() has no format fields, as before

    def __init__(self, type, mode, rate, channels, format, periodsize, periods, device):
        if channels == 1 and not self.mono_silently_stereo:
            raise sys.modules["alsaaudio"].ALSAAudioError("Invalid argument")
        self.type, self.channels, self.periodsize = type, 2, periodsize

        self.reads: list = []
        self.written: list[bytes] = []
        self.closed = False

    def read(self):
        item = self.reads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def write(self, data):
        self.written.append(data)
        return -32 if data == b"UNDERRUN" else len(data) // (2 * self.channels)

    def close(self):
        self.closed = True

    def info(self):
        info = {"channels": self.channels, "rate": 48000, "period_size": self.periodsize}
        if self.format_name is not None:
            info["format_name"] = self.format_name
        return info


class StubMixer:
    levels: ClassVar[dict[str, int]] = {}

    def __init__(self, control, cardindex):
        if control not in ("PCM", "Mic"):
            raise sys.modules["alsaaudio"].ALSAAudioError("no such control")
        self.control = control

    def getvolume(self, pcmtype):
        if (self.control, pcmtype) not in (("PCM", 0), ("Mic", 1)):
            raise sys.modules["alsaaudio"].ALSAAudioError("wrong capability")
        return [StubMixer.levels.get(self.control, 0)]

    def getrange(self, pcmtype):
        return [0, 100]

    def volumecap(self):
        return {"PCM": ["Playback Volume"], "Mic": ["Capture Volume"]}.get(self.control, [])

    def setvolume(self, v, pcmtype):
        StubMixer.levels[self.control] = v

    def setrec(self, on):
        pass

    def close(self):
        pass


@pytest.fixture
def alsaaudio(monkeypatch):
    mod = types.ModuleType("alsaaudio")
    mod.ALSAAudioError = type("ALSAAudioError", (Exception,), {})
    mod.PCM_PLAYBACK, mod.PCM_CAPTURE, mod.PCM_NORMAL, mod.PCM_FORMAT_S16_LE = 0, 1, 0, 2
    mod.PCM = StubPCM
    mod.Mixer = StubMixer
    mod.mixers = lambda cardindex: ["Speaker", "PCM", "Mic", "Auto Gain Control"]
    StubMixer.levels = {}
    monkeypatch.setitem(sys.modules, "alsaaudio", mod)
    return mod


def test_list_and_select_by_usb_id(proc_asound):
    cards = list_cards(proc_asound)
    assert [c.index for c in cards] == [0, 1]
    assert not cards[0].is_usb
    assert cards[1].usb_id == "0d8c:0014"
    assert select_card(cards).index == 1  # empty config: first USB card
    assert select_card(cards, "0d8c", "0014").index == 1
    assert select_card(cards, "1234", "") is None


def _two_identical_dongles(tmp_path):
    root = tmp_path / "asound"
    root.mkdir()
    (root / "cards").write_text(
        " 1 [Device         ]: USB-Audio - USB Audio Device\n"
        "                      C-Media Electronics Inc. USB Audio Device at usb-3f980000.usb-1.2, full speed\n"
        " 2 [Device_1       ]: USB-Audio - USB Audio Device\n"
        "                      C-Media Electronics Inc. USB Audio Device at usb-3f980000.usb-1.3, full speed\n"
    )
    snd = tmp_path / "sound"
    for i, port in ((1, "1-1.2"), (2, "1-1.3")):
        (root / f"card{i}").mkdir()
        (root / f"card{i}" / "usbid").write_text("0D8C:0014\n")
        iface = tmp_path / "devices" / "usb1" / "1-1" / port / f"{port}:1.0"
        iface.mkdir(parents=True)
        (snd / f"card{i}").mkdir(parents=True)
        (snd / f"card{i}" / "device").symlink_to(iface)
    return list_cards(root, snd)


def test_two_identical_cards_are_told_apart_by_usb_position(tmp_path):
    cards = _two_identical_dongles(tmp_path)
    assert [(c.usb_id, c.usb_path) for c in cards] == [("0d8c:0014", "1-1.2"), ("0d8c:0014", "1-1.3")]
    assert select_card(cards, "0d8c", "0014", "1-1.3").index == 2
    assert select_card(cards, "0d8c", "0014").index == 1  # no position: the first of that id


def test_a_card_moved_to_another_socket_is_found_by_its_id(tmp_path):
    cards = _two_identical_dongles(tmp_path)
    assert select_card(cards, "0d8c", "0014", "1-1.9").index == 1
    assert select_card(cards, "1234", "5678", "1-1.3") is None  # a position never overrides the id


def test_a_strict_choice_is_found_only_at_its_position(tmp_path):
    cards = _two_identical_dongles(tmp_path)
    assert select_card(cards, "0d8c", "0014", "1-1.3", strict=True).index == 2
    assert select_card(cards, "0d8c", "0014", "1-1.9", strict=True) is None  # gone: not its twin


def test_open_falls_back_to_stereo_and_reads_left_channel(proc_asound, alsaaudio):
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    assert b.card.index == 1 and b._cap_channels == 2 and b._play_channels == 2
    left = np.arange(FRAME_SAMPLES, dtype=np.int16)
    stereo = np.empty(FRAME_SAMPLES * 2, dtype=np.int16)
    stereo[0::2], stereo[1::2] = left, -1
    b._cap.reads = [(-32, b""), (FRAME_SAMPLES // 2, stereo[: FRAME_SAMPLES].tobytes()),
                    (FRAME_SAMPLES // 2, stereo[FRAME_SAMPLES:].tobytes())]
    out = b.read()
    assert np.array_equal(out, left)
    assert b.overruns == 1
    b.write(np.ones(FRAME_SAMPLES, dtype=np.int16))
    assert len(b._play.written[0]) == FRAME_SAMPLES * 4  # duplicated to stereo
    assert "usb=0d8c:0014" in b.describe()


def test_open_detects_silently_negotiated_stereo(proc_asound, alsaaudio, monkeypatch):
    """C-Media 0d8c:0014: mono open succeeds but info() says 2 channels. The
    backend must not believe the mono request, or every packet plays at 2x."""
    monkeypatch.setattr(StubPCM, "mono_silently_stereo", True)
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    assert b._cap_channels == 2 and b._play_channels == 2
    assert "play2ch" in b.describe()
    b.write(np.ones(FRAME_SAMPLES, dtype=np.int16))
    assert len(b._play.written[0]) == FRAME_SAMPLES * 2 * 2


def test_alsa_error_maps_to_device_error(proc_asound, alsaaudio):
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    b._cap.reads = [alsaaudio.ALSAAudioError("No such device [hw:1,0]")]
    (proc_asound / "card1" / "usbid").unlink()
    (proc_asound / "card1").rmdir()
    with pytest.raises(AudioDeviceError, match="card node gone"):
        b.read()
    b.close()
    assert b._cap is None


def test_set_gain_uses_discovered_mixers(proc_asound, alsaaudio):
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    assert b.set_gain(tx=35, rx=70) == {"tx": 35, "rx": 70}
    assert b.mixer_names() == {"tx": "PCM", "rx": "Mic"}
    assert StubMixer.levels == {"PCM": 35, "Mic": 70}


def test_a_card_with_no_capture_volume_still_opens(proc_asound, alsaaudio, monkeypatch):
    # IC-705 codec (08bb:2901), bench 2026-09-20: one control, PCM; getvolume(capture) answers [] and does not raise.
    monkeypatch.setattr(alsaaudio, "mixers", lambda cardindex: ["PCM"])
    real = StubMixer.getvolume
    monkeypatch.setattr(StubMixer, "getvolume", lambda self, pcmtype: [] if pcmtype == 1 else real(self, pcmtype))
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    assert b.set_gain(tx=35, rx=70)["tx"] == 35
    assert b.mixer_names() == {"tx": "PCM", "rx": None}


def test_a_card_with_only_a_playback_control_has_a_fixed_receive_level(proc_asound, alsaaudio, monkeypatch):
    """IC-705 codec, bench 2026-10-05: 'PCM' lists only ['Playback Volume'], so rx_gain changes nothing."""
    monkeypatch.setattr(alsaaudio, "mixers", lambda cardindex: ["PCM"])
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    assert b.set_gain(tx=35, rx=70) == {"tx": 35, "rx": 70}
    assert b.rx_fixed and not b.tx_soft


def test_a_card_with_a_capture_control_has_an_adjustable_receive_level(proc_asound, alsaaudio, monkeypatch):
    monkeypatch.setattr(alsaaudio, "mixers", lambda cardindex: ["PCM", "Mic"])
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    b.set_gain(rx=70)
    assert not b.rx_fixed and StubMixer.levels["Mic"] == 70


def test_a_mixer_lookup_error_keeps_the_receive_level_adjustable(proc_asound, alsaaudio, monkeypatch):
    """A transient ALSA error proves nothing: keep the slider rather than send the user to the radio."""
    def flaky(cardindex):
        raise alsaaudio.ALSAAudioError("Resource temporarily unavailable")
    monkeypatch.setattr(alsaaudio, "mixers", flaky)
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    b.set_gain(rx=70)
    assert not b.rx_fixed



def test_a_stream_that_will_not_start_raises_instead_of_spinning(proc_asound, alsaaudio):
    """IC-705 codec wedged in the kernel: every read fails at once. read() must give up."""
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    b._cap.reads = [(-32, b"")] * DEAD_READS_LIMIT
    with pytest.raises(AudioDeviceError, match="will not start"):
        b.read()


def test_overruns_between_audio_do_not_count_as_dead(proc_asound, alsaaudio):
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    half = np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()  # stereo: half a frame per read
    b._cap.reads = ([(-32, b"")] * (DEAD_READS_LIMIT - 1) + [(FRAME_SAMPLES // 2, half)]
                    + [(0, b"")] * (DEAD_READS_LIMIT - 1) + [(FRAME_SAMPLES // 2, half)])
    assert b.read().size == FRAME_SAMPLES


def _s24_stereo(left: np.ndarray, right: np.ndarray, low: int = 0x5A) -> bytes:
    """Interleaved S24_3LE frames whose top 16 bits are left/right, with a nonzero low byte."""
    words = np.empty(left.size * 2, dtype="<i2")
    words[0::2], words[1::2] = left, right
    raw = np.full((words.size, 3), low, dtype=np.uint8)
    raw[:, 1:] = words.view(np.uint8).reshape(-1, 2)
    return raw.tobytes()


def test_s24_card_reads_top_16_bits_of_left_channel_one_packet_per_period(proc_asound, alsaaudio, monkeypatch):
    """QMX 0483:a34c only does S24_3LE. Read as int16, 480 frames became 720 samples (1.5x rate)."""
    monkeypatch.setattr(StubPCM, "format_name", "S24_3LE")
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    assert b._cap_format == "S24_3LE" and "fmt=cap:S24_3LE/play:S24_3LE" in b.describe()
    left = np.arange(-240, 240, dtype=np.int16) * 64
    period = _s24_stereo(left, np.full(FRAME_SAMPLES, -7, dtype=np.int16))
    b._cap.reads = [(FRAME_SAMPLES, period)]
    assert np.array_equal(b.read(), left)
    assert b._pending.size == 0  # one 480-frame period is exactly one packet


def test_s24_write_round_trips(proc_asound, alsaaudio, monkeypatch):
    monkeypatch.setattr(StubPCM, "format_name", "S24_3LE")
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    pcm = np.array([0, 1, -1, 32767, -32768] * 96, dtype=np.int16)
    b.write(pcm)
    raw = np.frombuffer(b._play.written[0], dtype=np.uint8).reshape(-1, 3)
    assert raw.shape == (FRAME_SAMPLES * 2, 3) and not raw[:, 0].any()  # stereo, zero low byte
    back = np.ascontiguousarray(raw[:, 1:]).view("<i2").reshape(-1)
    assert np.array_equal(back[0::2], pcm) and np.array_equal(back[1::2], pcm)


def test_s16_card_is_unchanged_when_info_names_the_format(proc_asound, alsaaudio, monkeypatch):
    monkeypatch.setattr(StubPCM, "format_name", "S16_LE")
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    left = np.arange(FRAME_SAMPLES, dtype=np.int16)
    stereo = np.empty(FRAME_SAMPLES * 2, dtype=np.int16)
    stereo[0::2], stereo[1::2] = left, -1
    b._cap.reads = [(FRAME_SAMPLES, stereo.tobytes())]
    assert np.array_equal(b.read(), left)
    b.write(left)
    assert b._play.written[0] == np.repeat(left, 2).tobytes()
    assert "fmt=" not in b.describe()


def test_an_unsupported_sample_format_refuses_to_open(proc_asound, alsaaudio, monkeypatch):
    monkeypatch.setattr(StubPCM, "format_name", "S32_LE")
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    with pytest.raises(AudioDeviceError, match="sample format S32_LE"):
        b.open()
    assert b._cap is None and b._play is None


def test_a_switch_only_playback_control_is_not_a_mixer(proc_asound, alsaaudio, monkeypatch):
    """QMX 0483:a34c: 'PCM' is a mute switch; getvolume answers [0, 0] over a 0..0 range.
    Taken as the mixer, its gain read back as INT32 garbage; now tx_gain scales the samples."""
    monkeypatch.setattr(StubMixer, "getrange", lambda self, pcmtype: [0, 0] if self.control == "PCM" else [0, 100])
    monkeypatch.setattr(StubMixer, "volumecap", lambda self: [] if self.control == "PCM" else ["Capture Volume"])
    monkeypatch.setattr(alsaaudio, "mixers", lambda cardindex: ["PCM", "Mic"])  # every control opens, as on the QMX
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    assert b.set_gain(tx=88, rx=70) == {"tx": 88, "rx": 70}
    assert b.mixer_names() == {"tx": None, "rx": "Mic"}
    assert b.tx_soft and "PCM" not in StubMixer.levels


def test_a_card_with_playback_volume_keeps_its_hardware_mixer(proc_asound, alsaaudio):
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    b.set_gain(tx=70)
    assert not b.tx_soft and StubMixer.levels["PCM"] == 70


def test_a_mixer_lookup_error_fails_toward_unity_not_soft_gain(proc_asound, alsaaudio, monkeypatch):
    """KX2/IC-705: a transient ALSA error must not turn soft gain on, or it stacks on the hardware mixer."""
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    b.set_gain(tx=88)
    assert not b.tx_soft and StubMixer.levels["PCM"] == 88

    def flaky(cardindex):
        raise alsaaudio.ALSAAudioError("Resource temporarily unavailable")
    monkeypatch.setattr(alsaaudio, "mixers", flaky)
    assert b.set_gain(tx=88)["tx"] == 88
    assert not b.tx_soft


def test_a_card_that_lists_no_controls_is_not_taken_as_soft(proc_asound, alsaaudio, monkeypatch):
    """An empty control list (a card mid-enumeration) proves nothing: play at unity, not soft gain."""
    monkeypatch.setattr(alsaaudio, "mixers", lambda cardindex: [])
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    b.set_gain(tx=88)
    assert not b.tx_soft


def test_a_control_with_no_volume_in_volumecap_is_not_a_mixer(proc_asound, alsaaudio, monkeypatch):
    """Only volumecap() decides: a control that lists no volume is skipped even when getvolume and
    getrange look like a real 0..100 volume."""
    monkeypatch.setattr(StubMixer, "volumecap", lambda self: [] if self.control == "PCM" else ["Capture Volume"])
    monkeypatch.setattr(alsaaudio, "mixers", lambda cardindex: ["PCM", "Mic"])
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    assert b.set_gain(tx=88)["tx"] == 88
    assert b.mixer_names()["tx"] is None and b.tx_soft and "PCM" not in StubMixer.levels


def test_a_garbage_readback_keeps_the_requested_gain(proc_asound, alsaaudio, monkeypatch):
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    monkeypatch.setattr(StubMixer, "getvolume", lambda self, pcmtype: [2147483647])
    assert b.set_gain(tx=88)["tx"] == 88


def test_qmx_pcm_alternating_garbage_never_becomes_the_mixer(proc_asound, alsaaudio, monkeypatch):
    """QMX 2026-10-03: pyalsaaudio answers the mute-only 'PCM' clean ([0, 0] over 0..0) and garbage
    (INT32 volume, a wide range) on alternate opens. The garbage open passed the range check and
    stored tx_gain 2147483647."""
    pcm_opens = []
    real_init, real_getvolume = StubMixer.__init__, StubMixer.getvolume

    def init(self, control, cardindex):
        real_init(self, control, cardindex)
        if control == "PCM":
            pcm_opens.append(1)
        self.garbage = control == "PCM" and len(pcm_opens) % 2 == 1  # G . G . from the first PCM open

    def getvolume(self, pcmtype):
        if self.control != "PCM":
            return real_getvolume(self, pcmtype)  # Mic still raises for playback, as on hardware
        return [2147483647] if self.garbage else [0, 0]

    def getrange(self, pcmtype):
        if self.control != "PCM":
            return [0, 100]
        return [-233110271, 83954418] if self.garbage else [0, 0]

    monkeypatch.setattr(StubMixer, "volumecap", lambda self: [] if self.control == "PCM" else ["Capture Volume"])
    monkeypatch.setattr(alsaaudio, "mixers", lambda cardindex: ["PCM", "Mic"])
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    monkeypatch.setattr(StubMixer, "__init__", init)
    monkeypatch.setattr(StubMixer, "getvolume", getvolume)
    monkeypatch.setattr(StubMixer, "getrange", getrange)
    for _ in range(4):
        assert b.set_gain(tx=88)["tx"] == 88
        assert b.tx_soft and b.mixer_names()["tx"] is None
    assert "PCM" not in StubMixer.levels


@pytest.mark.parametrize("cap", [["Playback Volume"], ["Joined Playback Volume"], ["Volume"], ["Joined Volume"]])
def test_a_real_playback_volume_still_picks_its_mixer(proc_asound, alsaaudio, monkeypatch, cap):
    """KX2 / IC-705: a control that lists a playback volume stays the hardware mixer, at unity."""
    monkeypatch.setattr(StubMixer, "volumecap", lambda self: cap if self.control == "PCM" else ["Capture Volume"])
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    assert b.set_gain(tx=70)["tx"] == 70
    assert b.mixer_names()["tx"] == "PCM" and not b.tx_soft and StubMixer.levels["PCM"] == 70


def _close_mid_read(b):
    """The supervisor thread closes the device while capture is between reads (QMX USB drop)."""
    cap = b._cap

    def read():
        b.close()
        return 0, b""  # the card node is still there, so read() goes round again
    cap.read = read


def test_a_device_closed_mid_read_raises_a_device_error(proc_asound, alsaaudio):
    """2026-10-03: `self._cap.read()` on the closed device raised AttributeError and killed capture."""
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    _close_mid_read(b)
    with pytest.raises(AudioDeviceError, match="device closed"):
        b.read()


def test_capture_reads_again_after_the_reopen(proc_asound, alsaaudio):
    b = AlsaBackend(Config(), proc_asound=proc_asound)
    b.open()
    _close_mid_read(b)
    with pytest.raises(AudioDeviceError):
        b.read()
    b.close()
    b.open()  # what the supervisor does next
    left = np.arange(FRAME_SAMPLES, dtype=np.int16)
    stereo = np.repeat(left, 2)
    b._cap.reads = [(FRAME_SAMPLES, stereo.tobytes())]
    assert np.array_equal(b.read(), left)
