"""Setup wizard: resumable, verifies before advancing, ends in a one-tap kit."""

import json
import threading
import urllib.request
from dataclasses import replace

import pytest

from vector_bridge import cat, tuner, wizard
from vector_bridge.audio.alsa import CardInfo


@pytest.fixture(autouse=True)
def _no_real_serial_ports(tmp_path, monkeypatch):
    """The port list reads /dev/serial/by-id; a test box's own USB devices must not leak in."""
    monkeypatch.setattr(cat, "SERIAL_BY_ID", str(tmp_path / "no-serial-by-id"))


class FakeRig:
    """Stand-in for the audio service and rigctld. Records every call so tests can prove nothing keys."""

    def __init__(self):
        self.service_up = True
        self.cat_up = True
        self.keyed = False
        self.n = 0
        self.gain = {"rx_gain": 83, "tx_gain": 40}
        self.calls = []
        self.alsa = "alsa:hw:1"

    def control(self, req):
        self.calls.append(req)
        if not self.service_up:
            return None
        if req["cmd"] == "set_gain":
            self.gain.update({f"{k}_gain": v for k, v in req.items() if k in ("rx", "tx")})
            return {"ok": True}
        if req["cmd"] == "select_audio":
            return {"ok": True, "changed": True}
        self.n += 1
        return {"rx_rms": 900 + 37 * (self.n % 5), "alsa_device": self.alsa,
                "audio_device": {"ok": True}, **self.gain}

    def alive(self):
        return self.cat_up

    def get_ptt(self):
        return self.keyed

    def set_ptt(self, on):  # pragma: no cover - must never run
        raise AssertionError("the wizard keyed the radio")


def make(tmp_path, rig, ladder=None, cards=(), ports=None):
    probes = wizard.Probes(control_fn=rig.control, cat=rig, ladder=ladder, sample_gap_s=0,
                           cards_fn=lambda: list(cards), serial_usb_fn=(ports or {}).get)
    return wizard.Wizard(probes, state_path=tmp_path / "wizard.json", kits_dir=tmp_path / "kits",
                         presets_dir=tmp_path / "presets")


def walk_to_ladder(w):
    w.choose("kx2")
    assert w.advance().ok
    w.update(cabling_confirmed=True)
    for _ in ("cabling", "audio_in", "cat", "ptt"):
        assert w.advance().ok
    assert w.state.step == "ladder"


def test_reopens_on_the_step_the_operator_left(tmp_path):
    rig = FakeRig()
    w = make(tmp_path, rig)
    w.choose("ft-891")
    assert w.advance().ok
    w.update(cabling_confirmed=True)
    assert w.advance().ok
    assert w.state.step == "audio_in"
    again = make(tmp_path, rig)  # a new process after a power cut
    assert again.state.step == "audio_in"
    assert again.state.profile == "ft-891"
    assert again.state.passed == ["identify", "cabling"]


def test_a_damaged_state_file_starts_over_instead_of_crashing(tmp_path):
    (tmp_path / "wizard.json").write_text("{not json")
    assert make(tmp_path, FakeRig()).state.step == "identify"


def test_each_step_refuses_to_advance_until_it_verifies(tmp_path):
    rig = FakeRig()
    w = make(tmp_path, rig)
    assert not w.advance().ok and w.state.step == "identify"  # no radio chosen
    w.choose("kx2")
    assert w.advance().ok
    assert not w.advance().ok and w.state.step == "cabling"  # not confirmed
    w.update(cabling_confirmed=True)
    rig.service_up = False
    assert not w.advance().ok and w.state.step == "cabling"  # no sound device
    rig.service_up = True
    assert w.advance().ok
    assert w.advance().ok  # audio in
    rig.cat_up = False
    assert not w.advance().ok and w.state.step == "cat"
    rig.cat_up = True
    assert w.advance().ok
    rig.keyed = True  # keyed by something else: PTT must not pass
    assert not w.advance().ok and w.state.step == "ptt"
    rig.keyed = False
    assert w.advance().ok
    assert not w.advance().ok and w.state.step == "ladder"  # locked until the ladder reports
    with pytest.raises(wizard.WizardError):
        make(tmp_path / "b", rig).goto("ptt")  # cannot jump past unverified steps


def test_silent_audio_does_not_pass(tmp_path):
    rig = FakeRig()
    w = make(tmp_path, rig)
    w.choose("kx2")
    w.advance()
    w.update(cabling_confirmed=True)
    w.advance()
    rig.control = lambda req: {"rx_rms": 0, "alsa_device": "alsa:hw:1"}
    w.probes.control_fn = rig.control
    res = w.advance()
    assert not res.ok and "microphone jack" in res.detail


def test_ladder_step_passes_on_the_ladder_hook_and_reports_bypass(tmp_path):
    rig = FakeRig()
    gates = {}
    w = make(tmp_path, rig, ladder=lambda: {"gates": gates})
    walk_to_ladder(w)
    assert not w.advance().ok
    gates.update(bypassed=["audio drive"])
    res = w.advance()
    assert res.ok and "unverified" in res.detail


def test_cat_step_says_why_an_automatic_port_failed(tmp_path, monkeypatch):
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    monkeypatch.setattr(cat, "SERIAL_BY_ID", str(by_id))
    rig = FakeRig()
    w = make(tmp_path, rig)
    w.choose("kx2")
    assert w.advance().ok
    w.update(cabling_confirmed=True)
    assert w.advance().ok and w.advance().ok and w.state.step == "cat"
    rig.cat_up = False
    assert "No USB serial port was found" in w.advance().detail
    for n in ("usb-FTDI_FT231X_USB_UART_D30A1B2C-if00-port0", "usb-Icom_Inc._IC-705_IC-705_1-if00"):
        (by_id / n).touch()
    r = w.advance()
    assert not r.ok and "2 USB serial devices" in r.detail and "usb-Icom_Inc._IC-705_IC-705_1-if00" in r.detail


def test_changing_a_setting_forces_its_step_to_verify_again(tmp_path):
    w = make(tmp_path, FakeRig())
    walk_to_ladder(w)
    w.update(cat_baud=9600)
    assert w.state.step == "cat"
    assert w.state.passed == ["identify", "cabling", "audio_in"]
    w.choose("qmx")  # a different radio clears everything
    assert w.state.step == "identify" and w.state.passed == []


def test_the_wizard_never_keys_the_radio(tmp_path):
    rig = FakeRig()
    w = make(tmp_path, rig)
    walk_to_ladder(w)  # FakeRig.set_ptt raises if touched
    w.save_kit("Field KX2")
    w.apply_kit("Field KX2")
    assert all(c["cmd"] in ("status", "set_gain") for c in rig.calls)


def test_interview_maps_answers_to_an_interface(tmp_path):
    assert wizard.interview({"box_label": "digirig"})["interface"] == "digirig"
    one = wizard.interview({"usb_count": "one", "box_label": "none"})
    assert one["interface"] == "internal-usb" and "ic-705" in one["profiles"]
    sl = wizard.interview({"usb_count": "box", "data_jack": "yes", "box_label": "signalink"})
    assert sl["interface"] == "other" and sl["ptt_method"] == "vox" and sl["profiles"]
    assert any("DATA" in a for a in sl["advice"])
    w = make(tmp_path, FakeRig())
    w.answer("usb_count", "box")
    assert make(tmp_path, FakeRig()).state.answers == {"usb_count": "box"}  # the interview resumes too
    with pytest.raises(wizard.WizardError):
        w.answer("usb_count", "seven")


def test_every_step_offers_two_or_three_causes_wrong_jack_first(tmp_path):
    assert set(wizard.HELP) == set(wizard.STEPS)
    for step, causes in wizard.HELP.items():
        assert 2 <= len(causes) <= 3, step
    for step in ("cabling", "audio_in", "cat", "ptt"):
        assert "wrong jack" in wizard.HELP[step][0]
    report = make(tmp_path, FakeRig()).report()
    assert [s["key"] for s in report["steps"]] == list(wizard.STEPS)
    assert all(s["help"] for s in report["steps"])


def test_kit_saves_everything_and_is_one_tap_next_time(tmp_path):
    rig = FakeRig()
    # The KX2 reports no ALC: its Drive check is skipped once the operator set the level by hand, and a kit
    # saved then keeps the levels (REQ-AUDIO-033).
    w = make(tmp_path, rig, ladder=lambda: {"gates": {"passed": False, "drive_passed": False, "bypassed": ["drive"]}})
    with pytest.raises(wizard.WizardError):
        w.save_kit("too early")
    walk_to_ladder(w)
    rig.gain = {"rx_gain": 83, "tx_gain": 40}  # the operator's hand-set levels (choose() applied the KX2 start of 20)
    w.update(ptt_method="vox")
    assert w.state.step == "cat"  # VOX turns radio control off, so CAT is checked again
    assert w.advance().ok and w.advance().ok
    path = w.save_kit("Field KX2")
    kit = json.loads(path.read_text())
    assert kit["radio"] == "kx2" and kit["interface"] == "internal-usb"
    assert kit["levels"] == {"rx_gain": 83, "tx_gain": 40}
    assert kit["cat"] == {"rig_model": 0, "device": "", "baud": 38400, "civaddr": "", "set_conf": ""}
    assert kit["ptt"] == {"method": "vox", "device": ""} and kit["cabling"]["confirmed"]
    assert not list(tmp_path.glob("kits/.*"))  # no temp file left behind
    with pytest.raises(wizard.WizardError):
        w.save_kit("../etc/passwd")

    rig.gain = {"rx_gain": 10, "tx_gain": 10}  # someone moved the gains since
    fresh = make(tmp_path / "other-session", rig)
    fresh.kits_dir = tmp_path / "kits"
    res = fresh.apply_kit("Field KX2")
    assert fresh.state.step == "ladder" and not res.ok  # everything up to the ladder re-verified in one tap
    assert fresh.state.passed == ["identify", "cabling", "audio_in", "cat", "ptt"]
    assert fresh.profile().ptt_method == "vox"
    assert rig.gain == {"rx_gain": 83, "tx_gain": 40}  # levels restored


def test_one_tap_stops_on_the_first_step_that_fails(tmp_path):
    rig = FakeRig()
    w = make(tmp_path, rig)
    walk_to_ladder(w)
    w.save_kit("bench")
    rig.cat_up = False
    res = w.apply_kit("bench")
    assert not res.ok and w.state.step == "cat"
    assert w.state.passed == ["identify", "cabling", "audio_in"]


def test_http_setup_page_and_wizard_api(tmp_path):
    rig = FakeRig()
    app = tuner.TunerApp(rig.control, mixer_fn=lambda c: [], wiz=make(tmp_path, rig))
    httpd = tuner.serve(app, bind="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(body):
        req = urllib.request.Request(base + "/api/wizard", data=json.dumps(body).encode(), method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    try:
        with urllib.request.urlopen(base + "/setup") as r:
            page = r.read().decode()
        assert "This isn't working" in page and "I'm not sure" in page
        with urllib.request.urlopen(base + "/") as r:
            assert 'href="/setup"' in r.read().decode()
        assert post({"action": "choose", "profile": "kx2"})[1]["profile"]["name"] == "kx2"
        code, rep = post({"action": "advance"})
        assert code == 200 and rep["step"] == "cabling" and rep["result"]["ok"]
        assert post({"action": "choose", "profile": "nope"})[0] == 400
        assert post({"action": "launch"})[0] == 400
        with urllib.request.urlopen(base + "/api/wizard") as r:
            assert json.load(r)["step"] == "cabling"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_step_that_fails_later_unproves_itself_and_blocks_the_kit(tmp_path):
    rig = FakeRig()
    w = make(tmp_path, rig)
    walk_to_ladder(w)
    w.goto("cabling")
    rig.service_up = False  # the operator unplugged the interface
    assert not w.advance().ok
    assert w.state.passed == ["identify"] and not w.kit_ready()
    with pytest.raises(wizard.WizardError):
        w.goto("ptt")
    with pytest.raises(wizard.WizardError):
        w.save_kit("stale")


def test_save_kit_verifies_again_so_old_passes_cannot_be_saved(tmp_path):
    rig = FakeRig()
    w = make(tmp_path, rig)
    walk_to_ladder(w)
    rig.cat_up = False  # passed days ago, not true now
    with pytest.raises(wizard.WizardError, match="no longer passes"):
        w.save_kit("old")
    assert w.state.step == "cat" and not list((tmp_path / "kits").glob("*.json"))


def test_bad_settings_and_damaged_state_are_refused(tmp_path):
    w = make(tmp_path, FakeRig())
    w.choose("kx2")
    for bad in ({"cat_device": {"a": 1}}, {"cat_device": "-m 1"}, {"cat_baud": -1}, {"cat_baud": "9600"},
                {"cabling_confirmed": "false"}, {"ptt_method": "magic"}):
        with pytest.raises(wizard.WizardError):
            w.update(**bad)
    w.update(cat_device="/dev/ttyUSB0", cat_baud=9600)
    (tmp_path / "wizard.json").write_text(json.dumps({"step": "ptt", "profile": "kx2", "passed": ["identify", "bogus", "cat"]}))
    again = make(tmp_path, FakeRig())
    assert again.state.passed == ["identify"] and again.state.step == "cabling"


def test_kit_names_that_collide_on_disk_are_refused(tmp_path):
    w = make(tmp_path, FakeRig())
    walk_to_ladder(w)
    w.save_kit("My Kit")
    w.save_kit("My Kit")  # saving over the same kit is fine
    with pytest.raises(wizard.WizardError, match="too close"):
        w.save_kit("my-kit")


def test_one_tap_with_rts_keying_never_keys_and_sets_no_gain_without_a_sound_device(tmp_path):
    rig = FakeRig()
    w = make(tmp_path, rig)
    w.choose("kx2")
    w.advance()
    w.update(cabling_confirmed=True)
    for _ in range(3):
        assert w.advance().ok
    port = tmp_path / "ttyFAKE"  # stands in for the serial device node
    port.write_text("")
    w.state.cat_device, w.state.ptt_method = str(port), "rts"
    assert w.advance().ok
    w.save_kit("rts kit")
    rig.calls.clear()
    rig.service_up = False
    assert w.apply_kit("rts kit").ok is False and w.state.step == "cabling"
    assert not [c for c in rig.calls if c["cmd"] == "set_gain"]


def test_a_rebooted_kit_restarts_radio_control_for_the_saved_rig(tmp_path):
    rig, started = FakeRig(), []
    walk_to_ladder(make(tmp_path, rig))
    w = make(tmp_path, rig)  # the reboot: same state file, new process
    assert w.resume_cat() is False  # no start_cat probe wired
    w.probes.start_cat = started.append
    assert w.resume_cat() is True and [p.name for p in started] == ["kx2"]
    fresh = make(tmp_path / "new", rig)
    fresh.choose("kx2")
    fresh.probes.start_cat = started.append
    assert fresh.resume_cat() is False and len(started) == 1  # CAT never passed: nothing to resume


def test_a_kit_loaded_while_the_audio_service_restarts_still_gets_radio_control_after_a_reboot(tmp_path):
    rig, started = FakeRig(), []
    w = make(tmp_path, rig)
    walk_to_ladder(w)
    w.save_kit("kx2 field")
    rig.service_up = False  # the cabling check fails, which un-passes CAT too
    assert w.apply_kit("kx2 field").ok is False and "cat" not in w.state.passed
    rebooted = make(tmp_path, rig)
    rebooted.probes.start_cat = started.append
    assert rebooted.resume_cat() is True and [p.name for p in started] == ["kx2"]  # the same settings passed CAT
    rebooted.update(cat_baud=4800)
    changed = make(tmp_path, rig)
    changed.probes.start_cat = started.append
    assert changed.resume_cat() is False and len(started) == 1  # a baud that never passed CAT is not started


def test_a_kx2_on_a_vox_sound_card_finishes_setup_with_no_radio_control(tmp_path):
    rig, started = FakeRig(), []
    rig.cat_up = False  # a plain USB dongle: no KXUSB cable, no rigctld
    w = make(tmp_path, rig)
    w.probes.start_cat = started.append
    w.choose("kx2", "other", "vox")
    assert w.advance().ok
    w.update(cabling_confirmed=True)
    for _ in ("cabling", "audio_in", "cat", "ptt"):
        assert w.advance().ok
    assert w.state.step == "ladder" and w.kit_ready() and not started
    prof = w.profile()
    assert prof.display_name == "Elecraft KX2" and prof.rig_model == 0
    assert not prof.can_report_swr and not prof.can_report_alc
    assert w.resume_cat() is False and not started  # a reboot starts no rigctld for a VOX kit


def test_an_interrupted_setup_reports_where_it_stopped_and_the_radio_list_carries_its_settings(tmp_path):
    rig = FakeRig()
    w = make(tmp_path, rig)
    w.choose("kx2")
    assert w.advance().ok
    w.update(cabling_confirmed=True)
    assert w.advance().ok and w.advance().ok
    rep = make(tmp_path, rig).report()  # reopened after a power cut
    assert rep["step"] == "cat" and rep["passed"] == ["identify", "cabling", "audio_in"] and not rep["kit_ready"]
    assert rep["profile"]["display_name"] == "Elecraft KX2"
    radios = {r["name"]: r for r in rep["radios"]}
    assert radios["kx2"]["cat_baud"] == 38400 and radios["kx2"]["min_power_w"] == 0.1
    assert radios["signalink-generic"]["rig_model"] == 0 and radios["signalink-generic"]["ptt_method"] == "vox"
    assert wizard.interview({"box_label": "signalink"})["profiles"] == ["signalink-generic"]
    assert "signalink-generic" not in wizard.interview({"box_label": "other"})["profiles"]


def test_an_unknown_rig_on_a_digirig_starts_rigctld_to_key_rts(tmp_path, monkeypatch):
    port = tmp_path / "usb-Silicon_Labs_CP2102N-if00-port0"
    port.write_text("")
    monkeypatch.setattr(wizard, "with_port", lambda p: replace(p, cat_device=p.cat_device or str(port)))
    rig, started = FakeRig(), []
    w = make(tmp_path, rig)
    w.probes.start_cat = started.append
    w.choose("digirig-generic")
    assert w.advance().ok
    w.update(cabling_confirmed=True)
    for _ in ("cabling", "audio_in", "cat", "ptt"):
        r = w.advance()
        assert r.ok, r.detail
    assert [p.name for p in started] == ["digirig-generic"]  # the CAT step started rigctld for keying
    assert w.resume_cat() is True and len(started) == 2  # and a reboot starts it again


def test_rts_keying_with_no_cat_fails_the_cat_step_when_the_line_cannot_open(tmp_path):
    rig = FakeRig()
    w = make(tmp_path, rig)

    def refuse(prof):
        raise ValueError("needs the serial port")

    w.probes.start_cat = refuse
    w.choose("digirig-generic")
    w.advance()
    w.update(cabling_confirmed=True)
    for _ in ("cabling", "audio_in"):
        assert w.advance().ok
    r = w.advance()
    assert not r.ok and "RTS keying line could not be opened" in r.detail


def test_a_civ_address_rechecks_cat_and_is_kept_in_the_kit(tmp_path):
    w = make(tmp_path, FakeRig())
    w.choose("ic-705")
    w.advance()
    w.update(cabling_confirmed=True)
    for _ in ("cabling", "audio_in", "cat", "ptt"):
        assert w.advance().ok
    w.update(civaddr="0xa4")
    assert w.profile().civaddr == "A4" and w.state.step == "cat" and "cat" not in w.state.passed
    with pytest.raises(wizard.WizardError, match="CI-V"):
        w.update(civaddr="ZZ")
    for _ in ("cat", "ptt"):
        assert w.advance().ok
    w.save_kit("705 A4")
    w.choose("kx2")
    assert w.profile().civaddr == ""
    w.apply_kit("705 A4")
    assert w.profile().civaddr == "A4"


def test_any_hamlib_radio_can_be_chosen_and_is_kept_as_a_preset(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard.hamlib, "models", lambda: [{"model": 3073, "mfg": "Icom", "name": "IC-7300", "status": "Stable"}])
    w = make(tmp_path, FakeRig())
    assert w.choose_model(3073, "digirig") == "hamlib-3073"
    prof = w.profile()
    assert (prof.display_name, prof.rig_model, prof.interface, prof.ptt_method) == ("Icom IC-7300", 3073, "digirig", "cat")
    assert prof.cat_baud == 0 and not prof.can_report_alc  # radio default baud; drive is set by the radio's meter
    assert "hamlib-3073" in [r["name"] for r in w.report()["radios"]]  # listed from now on
    assert (tmp_path / "presets" / "hamlib-3073.toml").is_file()
    for bad in (9999, "3073", True, 1):
        with pytest.raises(wizard.WizardError):
            w.choose_model(bad)


def test_the_hamlib_list_and_choose_model_over_http(tmp_path, monkeypatch):
    ic7300 = {"model": 3073, "mfg": "Icom", "name": "IC-7300", "status": "Stable"}
    monkeypatch.setattr(wizard.hamlib, "models", lambda: [ic7300])  # tuner reads the same module
    rig = FakeRig()
    w = make(tmp_path, rig)
    httpd = tuner.serve(tuner.TunerApp(rig.control, mixer_fn=lambda c: [], wiz=w), bind="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/hamlib/models") as r:
            assert json.load(r) == {"ok": True, "models": [ic7300]}
    finally:
        httpd.shutdown()
        httpd.server_close()
    code, body = wizard.handle(w, {"action": "choose_model", "rig_model": 3073})
    assert code == 200 and body["profile"]["name"] == "hamlib-3073"
    code, body = wizard.handle(w, {"action": "choose_model", "rig_model": 42})
    assert code != 200 and "no radio model 42" in body["error"]


IC705_CARD = CardInfo(2, "Codec", "USB Audio CODEC", "08bb", "2901", "1-1.2")
SPARE_CARD = CardInfo(1, "Device", "USB Audio Device", "0d8c", "0014", "1-1.3")


def _to_cabling(w, **settings):
    w.choose("ic-705")
    assert w.advance().ok
    w.update(cabling_confirmed=True, cat_device="/dev/ttyACM0", **settings)


def test_cabling_picks_the_card_on_the_radios_usb_connection(tmp_path):
    rig = FakeRig()
    rig.alsa = "alsa:hw:2 USB Audio CODEC usb=08bb:2901"
    w = make(tmp_path, rig, cards=(SPARE_CARD, IC705_CARD), ports={"/dev/ttyACM0": "1-1.2"})
    _to_cabling(w)
    r = w.advance()
    assert r.ok and "USB Audio CODEC, on the same USB connection" in r.detail
    picks = [c for c in rig.calls if c["cmd"] == "select_audio"]
    assert picks == [{"cmd": "select_audio", "vendor": "08bb", "product": "2901", "usb_path": "1-1.2", "strict": False}]
    assert w.report()["sound_cards"][1] == {"index": 2, "name": "USB Audio CODEC", "usb_id": "08bb:2901", "usb_path": "1-1.2"}


def test_cabling_leaves_an_unmatched_card_to_the_service(tmp_path):
    rig = FakeRig()
    w = make(tmp_path, rig, cards=(SPARE_CARD,), ports={"/dev/ttyACM0": "2-1"})
    _to_cabling(w)
    assert w.advance().ok
    assert [c for c in rig.calls if c["cmd"] == "select_audio"] == [
        {"cmd": "select_audio", "vendor": "", "product": "", "usb_path": ""}]


def test_a_card_chosen_by_hand_wins_and_must_be_plugged_in(tmp_path):
    rig = FakeRig()
    w = make(tmp_path, rig, cards=(SPARE_CARD, IC705_CARD), ports={"/dev/ttyACM0": "1-1.2"})
    _to_cabling(w, audio_usb_id="0d8c:0014", audio_usb_path="1-1.3")
    assert w.advance().ok
    assert [c for c in rig.calls if c["cmd"] == "select_audio"][-1]["usb_path"] == "1-1.3"
    assert w.report()["audio_card"] == {"usb_id": "0d8c:0014", "usb_path": "1-1.3"}
    gone = make(tmp_path, rig, cards=(IC705_CARD,), ports={"/dev/ttyACM0": "1-1.2"})
    gone.goto("cabling")
    r = gone.advance()
    assert not r.ok and "not plugged in" in r.detail


def test_a_bad_sound_card_setting_is_refused(tmp_path):
    w = make(tmp_path, FakeRig())
    w.choose("ic-705")
    for bad in ({"audio_usb_id": "0D8C-0014"}, {"audio_usb_path": "../1-1"}):
        with pytest.raises(wizard.WizardError):
            w.update(**bad)


def test_the_kit_remembers_the_chosen_sound_card(tmp_path):
    rig = FakeRig()
    w = make(tmp_path, rig, cards=(SPARE_CARD, IC705_CARD), ports={"/dev/ttyACM0": "1-1.2"})
    _to_cabling(w, audio_usb_id="0d8c:0014", audio_usb_path="1-1.3")
    for _ in ("cabling", "audio_in", "cat", "ptt"):
        assert w.advance().ok, w.state.step
    kit = json.loads(w.save_kit("ic705 field").read_text())
    assert kit["audio"] == {"usb_id": "0d8c:0014", "usb_path": "1-1.3", "strict": False}
    w.update(audio_usb_id="", audio_usb_path="")
    w.apply_kit("ic705 field")
    assert (w.state.audio_usb_id, w.state.audio_usb_path) == ("0d8c:0014", "1-1.3")


def test_one_of_two_identical_cards_gone_is_not_swapped_for_the_other(tmp_path):
    twin = replace(SPARE_CARD, index=3, usb_path="1-1.5")
    rig = FakeRig()
    w = make(tmp_path, rig, cards=(SPARE_CARD, twin), ports={"/dev/ttyACM0": "1-1.2"})
    _to_cabling(w, audio_usb_id="0d8c:0014", audio_usb_path="1-1.4")
    r = w.advance()
    assert not r.ok and "not plugged in" in r.detail
    moved = make(tmp_path / "moved", rig, cards=(SPARE_CARD,), ports={"/dev/ttyACM0": "1-1.2"})
    _to_cabling(moved, audio_usb_id="0d8c:0014", audio_usb_path="1-1.4")
    assert moved.advance().ok  # the only card of that id, moved to another socket, is still found


def test_a_card_chosen_beside_its_twin_is_not_swapped_once_it_is_unplugged(tmp_path):
    twin = replace(SPARE_CARD, index=3, usb_path="1-1.5")
    rig = FakeRig()
    w = make(tmp_path, rig, cards=(SPARE_CARD, twin), ports={"/dev/ttyACM0": "1-1.2"})
    _to_cabling(w, audio_usb_id="0d8c:0014", audio_usb_path="1-1.3")
    assert w.state.audio_usb_strict is True  # chosen while its twin was plugged in
    for _ in ("cabling", "audio_in", "cat", "ptt"):
        assert w.advance().ok, w.state.step
    assert [c for c in rig.calls if c["cmd"] == "select_audio"][-1]["strict"] is True
    kit = json.loads(w.save_kit("twins").read_text())
    assert kit["audio"]["strict"] is True
    w.probes.cards_fn = lambda: [twin]  # the chosen card is gone; only its twin is left
    w.goto("cabling")
    r = w.advance()
    assert not r.ok and "not plugged in" in r.detail


def test_automatic_matching_marks_a_twinned_card_as_position_only(tmp_path):
    twin = replace(IC705_CARD, index=3, usb_path="1-1.5")
    rig = FakeRig()
    rig.alsa = "alsa:hw:2 USB Audio CODEC"
    w = make(tmp_path, rig, cards=(IC705_CARD, twin), ports={"/dev/ttyACM0": "1-1.2"})
    _to_cabling(w)
    assert w.advance().ok
    assert [c for c in rig.calls if c["cmd"] == "select_audio"][-1] == {
        "cmd": "select_audio", "vendor": "08bb", "product": "2901", "usb_path": "1-1.2", "strict": True}


def test_cabling_fails_when_the_service_never_opens_the_chosen_card(tmp_path):
    rig = FakeRig()
    rig.alsa = "alsa:hw:1 USB Audio Device"  # still on the spare card
    w = make(tmp_path, rig, cards=(SPARE_CARD, IC705_CARD), ports={"/dev/ttyACM0": "1-1.2"})
    _to_cabling(w)
    r = w.advance()
    assert not r.ok and "did not open USB Audio CODEC" in r.detail


def test_rts_keying_with_no_cat_fails_when_rigctld_does_not_answer(tmp_path):
    rig = FakeRig()
    w = make(tmp_path, rig)
    w.probes.start_cat = lambda prof: None  # started, but the dummy rig could not open the line
    w.choose("digirig-generic")
    w.advance()
    w.update(cabling_confirmed=True, cat_device="/dev/ttyUSB0")
    for _ in ("cabling", "audio_in"):
        assert w.advance().ok
    rig.cat_up = False
    r = w.advance()
    assert not r.ok and "RTS keying line on /dev/ttyUSB0 could not be opened" in r.detail
    rig.cat_up = True
    assert w.advance().ok


def test_choose_model_refuses_a_bad_interface_before_saving_a_preset(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard.hamlib, "models", lambda: [{"model": 3073, "mfg": "Icom", "name": "IC-7300", "status": "Stable"}])
    w = make(tmp_path, FakeRig())
    with pytest.raises(wizard.WizardError):
        w.choose_model(3073, interface="carrier-pigeon")
    assert not (tmp_path / "presets").exists() or not list((tmp_path / "presets").iterdir())


def test_an_unrelated_apply_keeps_the_twin_flag_of_the_chosen_card(tmp_path):
    twin = replace(SPARE_CARD, index=3, usb_path="1-1.5")
    w = make(tmp_path, FakeRig(), cards=(SPARE_CARD, twin), ports={"/dev/ttyACM0": "1-1.2"})
    _to_cabling(w, audio_usb_id="0d8c:0014", audio_usb_path="1-1.3")
    assert w.state.audio_usb_strict is True
    w.probes.cards_fn = lambda: [SPARE_CARD]  # the twin is unplugged later
    w.update(cat_baud=19200, audio_usb_id="0d8c:0014", audio_usb_path="1-1.3")  # Advanced resends the same card
    assert w.state.audio_usb_strict is True
    w.update(audio_usb_path="1-1.4")  # choosing again does re-judge it
    assert w.state.audio_usb_strict is False


def test_a_cat_failure_carries_rigctlds_own_words(tmp_path):
    rig = FakeRig()
    w = make(tmp_path, rig)
    w.probes.cat_log = lambda prof: ["serial_open: Unable to open /dev/ttyACM0 - Device or resource busy"]
    w.choose("kx2")
    assert w.advance().ok
    w.update(cabling_confirmed=True, cat_device="/dev/ttyACM0")
    assert w.advance().ok and w.advance().ok and w.state.step == "cat"
    rig.cat_up = False
    r = w.advance()
    assert not r.ok and r.log == ["serial_open: Unable to open /dev/ttyACM0 - Device or resource busy"]
    rig.cat_up = True
    assert w.advance().log == []  # a pass carries no log


def test_the_report_lists_serial_ports_by_name_with_their_sound_card(tmp_path):
    port = "/dev/serial/by-id/usb-Icom_Inc._IC-705_IC-705_12011136-if00"
    ftdi = "/dev/serial/by-id/usb-FTDI_FT231X_USB_UART_D30A1B2C-if00-port0"
    w = make(tmp_path, FakeRig(), cards=(SPARE_CARD, IC705_CARD), ports={port: "1-1.2", ftdi: "1-1.4"})
    w.probes.ports_fn = lambda: [ftdi, port]
    assert w.report()["serial_ports"] == [
        {"path": ftdi, "label": "FTDI FT231X USB UART", "sound_card": ""},
        {"path": port, "label": "Icom Inc. IC-705", "sound_card": "USB Audio CODEC"}]


def test_a_chosen_second_uart_is_listed_by_its_interface(tmp_path):
    first, second = (tmp_path / "usb-Silicon_Labs_CP2105_Dual_USB_to_UART_Bridge_Controller_01A2B3C4-if00-port0",
                     tmp_path / "usb-Silicon_Labs_CP2105_Dual_USB_to_UART_Bridge_Controller_01A2B3C4-if01-port0")
    first.touch()
    second.touch()
    w = make(tmp_path, FakeRig())
    w.probes.ports_fn = lambda: [str(first)]  # one entry per device: its lowest interface
    w.choose("digirig-generic")
    w.state.cat_device = str(second)  # as saved; a real one is under /dev/serial/by-id
    assert [p["label"] for p in w.report()["serial_ports"]] == [
        "Silicon Labs CP2105 Dual USB to UART Bridge Controller",
        "Silicon Labs CP2105 Dual USB to UART Bridge Controller (if01-port0)"]


class BaudRig(FakeRig):
    """A radio that answers CAT only at one baud rate. start_cat records each rate tried."""

    def __init__(self, answers_at):
        super().__init__()
        self.answers_at, self.baud, self.tried = answers_at, None, []

    def start(self, prof):
        self.baud = prof.cat_baud
        self.tried.append(prof.cat_baud)

    def alive(self):
        return self.baud == self.answers_at


def at_cat_step(tmp_path, rig):
    w = make(tmp_path, rig)
    w.probes.start_cat = rig.start
    w.choose("kx2")  # 38400 baud
    assert w.advance().ok
    w.update(cabling_confirmed=True, cat_device="/dev/ttyUSB0")
    assert w.advance().ok and w.advance().ok and w.state.step == "cat"
    assert not w.advance().ok
    rig.tried.clear()
    return w


def test_find_baud_saves_the_rate_that_answers_and_passes_cat(tmp_path):
    rig = BaudRig(answers_at=9600)
    w = at_cat_step(tmp_path, rig)
    r = w.find_baud()
    assert r.ok and "9600" in r.detail
    assert rig.tried[:3] == [38400, 19200, 9600]  # stops at the first that answers
    assert w.state.cat_baud == 9600 and w.profile().cat_baud == 9600
    assert w.state.step == "ptt" and "cat" in w.state.passed
    assert not any(c.get("cmd") == "set_ptt" for c in rig.calls)


def test_find_baud_with_no_answer_keeps_the_kit_rate_and_restarts_it(tmp_path):
    rig = BaudRig(answers_at=None)
    w = at_cat_step(tmp_path, rig)
    w.probes.cat_log = lambda prof: ["serial_open: Unable to open /dev/ttyUSB0"]
    r = w.find_baud()
    assert not r.ok and "tried 38400, 19200, 9600, 115200, 57600, 4800" in r.detail
    assert r.log == ["serial_open: Unable to open /dev/ttyUSB0"]
    assert rig.tried[-1] == 38400  # the closing CAT check put the kit's own rate back
    assert w.state.cat_baud == 38400 and w.state.step == "cat"


def test_find_baud_needs_radio_control_and_a_known_port(tmp_path):
    rig = BaudRig(answers_at=9600)
    w = at_cat_step(tmp_path, rig)
    w.update(cat_device="")  # Automatic, with no USB serial device plugged in
    with pytest.raises(wizard.WizardError, match="port"):
        w.find_baud()
    w.update(ptt_method="vox")
    with pytest.raises(wizard.WizardError, match="no radio control"):
        w.find_baud()
    assert rig.tried == []
    status, body = wizard.handle(w, {"action": "find_baud"})
    assert status == 400 and "no radio control" in body["error"]


def kit_saved(tmp_path):
    w = make(tmp_path, FakeRig())
    walk_to_ladder(w)
    w.save_kit("Field One")
    return w


def test_a_kit_can_be_renamed_and_the_setup_in_use_follows(tmp_path):
    w = kit_saved(tmp_path)
    w.rename_kit("Field One", "Park Kit")
    assert [k["name"] for k in w.kits()] == ["Park Kit"] and w.state.kit == "Park Kit"
    assert sorted(p.name for p in (tmp_path / "kits").iterdir()) == ["park-kit.json"]
    w.rename_kit("Park Kit", "park kit")  # a new spelling of the same file
    assert [k["name"] for k in w.kits()] == ["park kit"] and w.state.kit == "park kit"
    w.save_kit("Other")
    with pytest.raises(wizard.WizardError, match="too close"):
        w.rename_kit("park kit", "OTHER")
    with pytest.raises(wizard.WizardError, match="Kit names"):
        w.rename_kit("park kit", "bad/name")
    with pytest.raises(wizard.WizardError, match="no saved kit"):
        w.rename_kit("Gone", "New")
    assert sorted(k["name"] for k in w.kits()) == ["Other", "park kit"]


def test_deleting_the_kit_in_use_keeps_the_radio_setup(tmp_path):
    w = kit_saved(tmp_path)
    w.delete_kit("Field One")
    assert w.kits() == [] and w.state.kit == "" and w.state.step == "ladder"
    status, body = wizard.handle(w, {"action": "delete_kit", "name": "Field One"})
    assert status == 400 and "no saved kit" in body["error"]


def test_reset_forgets_the_radio_setup_and_stops_rigctld(tmp_path):
    from vector_bridge import profiles
    w = kit_saved(tmp_path)
    stopped = []
    w.probes.stop_cat = lambda: stopped.append(True)
    presets = tmp_path / "presets"
    profiles.save(profiles.RadioProfile("hamlib-3073", "Icom IC-7300", rig_model=3073, builtin=False), presets)
    profiles.save(profiles.RadioProfile("club-rig", "Club Rig", rig_model=2044, builtin=False), presets)
    w.reset()
    assert stopped == [True] and w.state == wizard.State() and w.kits() == []
    assert sorted(p.name for p in presets.iterdir()) == ["club-rig.toml"]  # installed presets stay
    assert make(tmp_path, FakeRig()).state == wizard.State()  # saved: a power cycle starts over too


def test_expert_settings_and_keying_port_are_checked_and_kept_in_the_kit(tmp_path, monkeypatch):
    from vector_bridge import hamlib
    seen = []
    monkeypatch.setattr(hamlib, "conf_params", lambda model: seen.append(model) or [
        {"name": "rts_state", "desc": "", "default": "Unset", "kind": "combo", "options": ["Unset", "ON", "OFF"]}])
    w = make(tmp_path, FakeRig())
    walk_to_ladder(w)
    with pytest.raises(wizard.WizardError, match="no setting called nope"):
        w.update(set_conf="nope=1")
    w.update(set_conf="rts_state = OFF", ptt_device="/dev/ttyUSB1")
    assert seen[-1] == 2044 and w.state.step == "cat"  # the KX2's own list; radio control checks again
    assert (w.profile().set_conf, w.profile().ptt_device) == ("rts_state=OFF", "/dev/ttyUSB1")
    for _ in ("cat", "ptt"):
        assert w.advance().ok
    w.save_kit("Expert")
    kit = w.kits()[0]
    assert kit["cat"]["set_conf"] == "rts_state=OFF" and kit["ptt"]["device"] == "/dev/ttyUSB1"
    w.choose("kx2")
    assert w.profile().set_conf == "" and w.profile().ptt_device == ""
    w.apply_kit("Expert")
    assert (w.profile().set_conf, w.profile().ptt_device) == ("rts_state=OFF", "/dev/ttyUSB1")


def test_a_radio_with_expert_settings_can_switch_to_vox_and_a_refusal_changes_nothing(tmp_path, monkeypatch):
    from vector_bridge import hamlib
    monkeypatch.setattr(hamlib, "conf_params", lambda model: [] if model == 1 else [
        {"name": "rts_state", "desc": "", "default": "Unset", "kind": "combo", "options": ["Unset", "ON", "OFF"]}])
    w = make(tmp_path, FakeRig())
    walk_to_ladder(w)
    w.update(set_conf="rts_state=OFF")
    for _ in ("cat", "ptt"):
        assert w.advance().ok
    with pytest.raises(wizard.WizardError, match="must be one of"):
        w.update(cat_baud=9600, set_conf="rts_state=MAYBE")
    assert w.state.cat_baud != 9600 and w.state.step == "ladder"  # nothing half set, nothing re-verified
    w.update(ptt_method="vox", set_conf="rts_state=OFF")  # the page sends the field as it stood
    assert w.profile().rig_model == 0 and w.state.set_conf == "" and w.state.ptt_method == "vox"


def test_rts_keying_on_a_separate_port_checks_that_port(tmp_path):
    w = make(tmp_path, FakeRig())
    w.choose("kx2")
    w.advance()
    w.update(cabling_confirmed=True)
    for _ in range(3):
        assert w.advance().ok
    cat_port = tmp_path / "ttyCAT"
    cat_port.write_text("")
    w.state.cat_device, w.state.ptt_method = str(cat_port), "rts"
    w.state.ptt_device = str(tmp_path / "ttyKEY")  # not plugged in
    r = w.advance()
    assert not r.ok and "ttyKEY" in r.detail
    (tmp_path / "ttyKEY").write_text("")
    r = w.advance()
    assert r.ok and "ttyKEY" in r.detail


# -- REQ-AUDIO-009: choosing a profile applies its TX start level --

def test_choosing_a_profile_sets_its_tx_start_level(tmp_path):
    rig = FakeRig()
    w = make(tmp_path, rig)
    w.choose("ic-705")
    assert rig.gain["tx_gain"] == 70
    w.choose("kx2")
    assert rig.gain["tx_gain"] == 20
    assert rig.gain["rx_gain"] == 83  # the receive level is not the profile's business


# -- REQ-AUDIO-033: a kit stores levels only after Drive has settled them --

def test_a_kit_saved_before_drive_passed_records_no_levels_and_apply_keeps_the_profile_start(tmp_path):
    rig = FakeRig()
    gates = {"passed": False, "drive_passed": False, "bypassed": ["drive", "carrier"]}
    w = make(tmp_path, rig, ladder=lambda: {"gates": gates})
    w.choose("ic-705")
    assert w.advance().ok
    w.update(cabling_confirmed=True)
    for _ in ("cabling", "audio_in", "cat", "ptt"):
        assert w.advance().ok
    rig.gain["tx_gain"] = 20  # the stale level a fresh card had before Drive ran
    kit = json.loads(w.save_kit("Early 705").read_text())
    assert kit["levels"] == {} and kit["levels_note"] == wizard.LEVELS_NOT_SET  # the IC-705 reports ALC: a skip is not a hand-set
    assert w.report()["kits"][0]["levels_note"] == wizard.LEVELS_NOT_SET

    gates.update(passed=True, drive_passed=True, bypassed=[])
    rig.gain["tx_gain"] = 70  # Drive settled here
    kit = json.loads(w.save_kit("Early 705").read_text())
    assert kit["levels"] == {"rx_gain": 83, "tx_gain": 70} and kit["levels_note"] == ""

    # Applying a kit without levels leaves the profile's start level in place.
    (tmp_path / "kits" / "early-705.json").write_text(json.dumps({**kit, "levels": {}, "levels_note": wizard.LEVELS_NOT_SET}))
    rig.gain = {"rx_gain": 10, "tx_gain": 10}
    fresh = make(tmp_path / "other", rig, ladder=lambda: {"gates": gates})
    fresh.kits_dir = tmp_path / "kits"
    fresh.apply_kit("Early 705")
    assert rig.gain["tx_gain"] == 70 and rig.gain["rx_gain"] == 10
