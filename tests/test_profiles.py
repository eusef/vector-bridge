"""Radio profiles: built-ins load, user presets override, bad presets are rejected."""

import pytest

from vector_bridge import profiles


def test_builtins_cover_the_field_kit():
    names = profiles.builtin_names()
    for n in ("kx2", "qmx", "usdx", "ft-891", "ic-705", "digirig-generic"):
        assert n in names
    for p in profiles.BUILTINS:
        p.validate()


def test_kx2_does_not_claim_alc():
    # Hamlib 2044 answers "Invalid parameter" to get_level ALC (bench, 2026-09-20); SWR it does report.
    kx2 = {p.name: p for p in profiles.BUILTINS}["kx2"]
    assert kx2.can_report_alc is False and kx2.can_report_swr is True


def test_user_preset_overrides_builtin_and_adds_new(tmp_path):
    (tmp_path / "qmx.toml").write_text('display_name = "My QMX"\nrig_model = 2014\nptt_method = "vox"\n')
    (tmp_path / "loaner.toml").write_text('ptt_method = "rts"\ncat_device = "/dev/ttyUSB0"\n')
    all_ = profiles.load_all(tmp_path)
    assert all_["qmx"].display_name == "My QMX" and all_["qmx"].builtin is False
    assert all_["qmx"].ptt_method == "vox"
    assert all_["loaner"].display_name == "loaner" and all_["loaner"].cat_device == "/dev/ttyUSB0"
    assert all_["kx2"].builtin is True


def test_bad_ptt_method_is_rejected(tmp_path):
    (tmp_path / "bad.toml").write_text('ptt_method = "telepathy"\n')
    with pytest.raises(ValueError):
        profiles.load_all(tmp_path)


def test_dumps_round_trips_through_loader(tmp_path):
    orig = profiles.get("ft-891")
    (tmp_path / "ft-891.toml").write_text(profiles.dumps(orig))
    back = profiles.load_all(tmp_path)["ft-891"]
    assert back.as_dict() | {"builtin": True} == orig.as_dict()


def test_save_writes_preset_that_loader_reads(tmp_path):
    p = profiles.get("qmx")
    p.display_name, p.cat_device = "Field QMX", "/dev/serial/by-id/usb-QRP_Labs_QMX-if00"
    assert profiles.save(p, tmp_path) == tmp_path / "qmx.toml"
    back = profiles.load_all(tmp_path)["qmx"]
    assert back.display_name == "Field QMX" and back.cat_device.endswith("if00") and back.builtin is False
    assert [x.name for x in tmp_path.iterdir()] == ["qmx.toml"]


def test_signalink_is_a_vox_only_profile_with_no_radio_control():
    sl = {p.name: p for p in profiles.BUILTINS}["signalink-generic"]
    assert sl.rig_model == 0 and sl.ptt_method == "vox" and sl.interface == "other"
    assert sl.can_report_swr is False and sl.can_report_alc is False


@pytest.mark.parametrize("given,want", [("a4", "A4"), ("0xA4", "A4"), ("A4h", "A4"), (" 94 ", "94"), ("E", "0E"), ("", "")])
def test_civ_addresses_are_read_as_the_radios_menu_shows_them(given, want):
    assert profiles.normalize_civaddr(given) == want


@pytest.mark.parametrize("bad", ["00", "G1", "1A4", "0x", 164])
def test_a_civ_address_that_is_not_hex_01_to_ff_is_refused(bad):
    with pytest.raises(ValueError):
        profiles.normalize_civaddr(bad)


@pytest.mark.parametrize("name,start", [("ic-705", 70), ("kx2", 20), ("qmx", 60), ("ft-891", 50), ("usdx", 50),
                                        ("digirig-generic", 40), ("signalink-generic", 40)])
def test_each_profile_carries_its_tx_start_level(name, start):
    # REQ-AUDIO-009: the IC-705 makes no drive at the old fixed 20 (bench 2026-10-24); the others are the
    # section 9 assumptions. The ALC target is the shipped default on every built-in.
    p = profiles.get(name)
    assert p.tx_start_gain == start and p.alc_target == 0.10


def test_a_preset_may_set_its_own_tx_start_level_and_alc_target(tmp_path):
    (tmp_path / "hot.toml").write_text('display_name = "Hot rig"\nrig_model = 3085\ntx_start_gain = 35\nalc_target = 0.2\n')
    p = profiles.get("hot", tmp_path)
    assert p.tx_start_gain == 35 and p.alc_target == 0.2
