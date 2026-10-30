"""Setup wizard: a resumable state machine that ends in a named kit.

Steps run in a fixed order: identify the rig, cabling, audio in, CAT, PTT, then the
transmit ladder. Each step verifies before the wizard advances, and the position is
saved after every change, so closing the browser or losing power reopens on the step
the operator left. Nothing here keys the radio: keying is only ever done by the
transmit ladder, which asks first.

The result is a kit: radio, interface, cabling, levels, CAT and PTT settings under one
name. Picking a saved kit re-runs every verification and lands on the first step that
does not pass, so the same hardware is one tap next time.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from . import hamlib, persist, profiles, readiness, usbtopo
from .audio.alsa import list_cards, select_card
from .cat import CatError, port_label, serial_ports, with_port
from .profiles import RadioProfile

STEPS = ("identify", "cabling", "audio_in", "cat", "ptt", "ladder")
LEVELS_NOT_SET = "levels not yet set"  # a kit saved before the Drive check settled the TX level (REQ-AUDIO-033)
TITLES = {"identify": "Identify the rig", "cabling": "Cabling", "audio_in": "Audio in",
          "cat": "Radio control (CAT)", "ptt": "Transmit keying (PTT)", "ladder": "Transmit checks"}
KIT_STEP = "ptt"  # once this step has passed, every setting a kit holds is known
KIT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,39}")
# "Find It for Me" on a CAT failure tries the kit's own rate, then these, most common first.
# A wrong rate costs about 3 s (rigctld's 2 s settle plus one read), so a search that finds nothing takes ~30 s (27 s on the bench).
FIND_BAUDS = (38400, 19200, 9600, 115200, 57600, 4800)

INTERFACES = {
    "internal-usb": "The radio's own USB socket (one cable carries audio and control)",
    "digirig": "Digirig Mobile (small box, one USB cable to the Pi, two leads to the radio)",
    "other": "Another USB sound card or interface box",
}

# ---- "I'm not sure": a short interview instead of a list ---------------------------

INTERVIEW = (
    {"key": "usb_count", "question": "How many USB connectors are in play between the radio and the Pi?",
     "options": {"one": "One cable straight from the radio to the Pi",
                 "box": "One cable from the Pi to a small box, and the box has leads to the radio",
                 "two": "Two USB cables (one for sound, one for control)",
                 "none": "None yet"}},
    {"key": "data_jack", "question": "Does the radio have a separate data or accessory jack, apart from the microphone jack?",
     "options": {"yes": "Yes, a round DATA or ACC socket", "no": "No, only microphone and headphone jacks",
                 "unsure": "I can't tell"}},
    {"key": "box_label", "question": "What is printed on the interface box, if there is one?",
     "options": {"digirig": "Digirig", "signalink": "SignaLink", "other": "Something else",
                 "none": "There is no box"}},
)


def interview(answers: dict) -> dict:
    """Turn interview answers into a suggested interface and the profiles worth trying."""
    usb, jack, label = answers.get("usb_count"), answers.get("data_jack"), answers.get("box_label")
    if label == "digirig":
        iface, why = "digirig", "The box is a Digirig."
    elif usb == "one" and label in (None, "none"):
        iface, why = "internal-usb", "One cable straight from the radio means the radio has its own USB sound and control."
    elif usb in ("box", "two") or label in ("signalink", "other"):
        iface, why = "other", "A separate box or sound card sits between the radio and the Pi."
    else:
        iface, why = "other", "Not enough to go on, so start with the generic setup."
    advice = []
    if iface != "internal-usb":
        advice.append("Use the radio's DATA or ACC jack for audio." if jack == "yes" else
                      "With no data jack, audio goes through the headphone and microphone jacks."
                      if jack == "no" else
                      "Look on the back of the radio for a round DATA or ACC socket; use it if it is there.")
    if label == "signalink":
        advice.append("A SignaLink keys the radio from the audio itself, so PTT is set to VOX.")
    # A VOX-only profile (the SignaLink) is suggested for a SignaLink box and for nothing else.
    names = [p.name for p in profiles.BUILTINS
             if p.interface == iface and (p.ptt_method == "vox") == (label == "signalink")]
    if not names:  # no built-in is wired this way: offer every radio, the interface is overridden
        names = [p.name for p in profiles.BUILTINS if p.rig_model]
    return {"interface": iface, "why": why, "advice": advice, "profiles": names,
            "ptt_method": "vox" if label == "signalink" else None}


# ---- "this isn't working": the two or three likeliest causes per step --------------

WRONG_JACK = ("The cable is in the wrong jack. Audio and control belong in the radio's DATA, ACC or USB "
              "socket, not the microphone jack. Check each plug against the radio's labels.")

HELP: dict[str, tuple[str, ...]] = {
    "identify": (
        "The radio is not in the list. Pick \"I'm not sure\" and answer the three questions instead.",
        "The radio is in the list but wired through a different interface box. Pick the radio, then change the interface.",
    ),
    "cabling": (
        WRONG_JACK,
        "The USB cable to the Pi is charge-only or loose. Swap it for a known data cable and reseat both ends.",
        "The interface box has no power light. Try another USB port on the Pi.",
    ),
    "audio_in": (
        WRONG_JACK,
        "The radio's volume or data output level is at zero. Turn it up until the level bar moves.",
        "The radio is squelched or muted. Open the squelch and tune to a busy frequency.",
    ),
    "cat": (
        WRONG_JACK,
        "The baud rate on the radio does not match the kit. Set the radio's CAT rate to the number shown here.",
        "The radio is off, or its USB or CAT port is set to another function in the menu.",
    ),
    "ptt": (
        WRONG_JACK,
        "VOX or a hand microphone is keying the radio. Turn VOX off and unplug the microphone.",
        "This radio needs a different PTT method. Try RTS if CAT keying is not supported.",
    ),
    "ladder": (
        "The antenna or dummy load is not connected. Connect it before anything transmits.",
        "The radio's power is set too high for a first test. Turn it to its lowest setting.",
    ),
}


# ---- state -------------------------------------------------------------------------


@dataclass
class State:
    step: str = STEPS[0]
    profile: str = ""  # profiles name
    interface: str = ""
    ptt_method: str = ""
    cat_device: str = ""
    cat_baud: int = 0
    civaddr: str = ""  # Icom CI-V address in hex; empty = the profile's, or Hamlib's default
    set_conf: str = ""  # expert Hamlib settings (rigctld -C); empty = the profile's
    ptt_device: str = ""  # a separate RTS or DTR keying port; empty = the radio control port
    audio_usb_id: str = ""  # sound card chosen by hand, "vendor:product"; both empty = automatic
    audio_usb_path: str = ""  # and its USB position, which tells two identical cards apart
    audio_usb_strict: bool = False  # it had an identical twin when chosen: only that position will do
    cabling_confirmed: bool = False
    answers: dict = field(default_factory=dict)  # interview answers, kept so the interview resumes too
    kit: str = ""  # name of the kit this state was loaded from or saved as
    passed: list[str] = field(default_factory=list)  # steps verified, in order
    cat_proven: dict = field(default_factory=dict)  # the radio control settings that last passed the CAT step


@dataclass
class Result:
    ok: bool
    detail: str
    log: list[str] = field(default_factory=list)  # rigctld's last stderr lines, on a CAT failure


@dataclass
class Probes:
    """Everything the wizard reads from the outside world. None of it transmits."""

    control_fn: Callable[[dict], dict | None] = lambda req: None
    cat: object | None = None  # RigctldClient or a stand-in: alive(), get_ptt()
    start_cat: Callable[[RadioProfile], None] | None = None  # (re)start rigctld for this profile
    ladder: Callable[[], dict] | None = None  # area 5 hook, same shape as readiness uses
    samples: int = 5  # status samples read for the audio check
    sample_gap_s: float = 0.2
    cards_fn: Callable[[], list] = list_cards  # the sound cards the Pi sees, with their USB ids and positions
    serial_usb_fn: Callable[[str], str] = usbtopo.serial_device  # serial port -> its USB position
    ports_fn: Callable[[], list[str]] = serial_ports  # one /dev/serial/by-id path per USB serial device
    cat_log: Callable[[RadioProfile], list[str]] | None = None  # stops this profile's failed rigctld, returns its last stderr lines
    stop_cat: Callable[[], None] | None = None  # stops rigctld and forgets its profile (a reset)


class WizardError(ValueError):
    """The operator asked for something the current state does not allow."""


class Wizard:
    def __init__(self, probes: Probes | None = None, state_path: Path | None = None,
                 kits_dir: Path | None = None, presets_dir: Path | None = None) -> None:
        self.probes = probes or Probes()
        self.state_path = state_path or persist.APP_DIR / "wizard.json"
        self.kits_dir = kits_dir or persist.KITS_DIR
        self.presets_dir = presets_dir
        self._lock = threading.RLock()
        self.state = self._load_state()

    # -- persistence --

    def _load_state(self) -> State:
        try:
            data = json.loads(self.state_path.read_text())
            known = State.__dataclass_fields__
            st = State(**{k: v for k, v in data.items() if k in known})
            if st.step not in STEPS or not isinstance(st.passed, list):
                return State()
            # passed is always an unbroken run from the first step; anything else is damage
            n = 0
            while n < len(STEPS) and STEPS[n] in st.passed:
                n += 1
            st.passed = list(STEPS[:n])
            if STEPS.index(st.step) > n:
                st.step = STEPS[n]
            return st
        except (OSError, ValueError, TypeError):
            return State()  # no file, or an unreadable one: start over rather than crash

    def _save_state(self) -> None:
        persist.save(self.state_path, json.dumps(asdict(self.state), indent=1))

    # -- the effective radio profile: the chosen one with the operator's overrides --

    def profile(self) -> RadioProfile | None:
        """VOX keying means no radio control: the radio keeps its name and notes, but loses
        CAT, so setup, the transmit checks and readiness all treat it as having none."""
        st = self.state
        if not st.profile:
            return None
        try:
            base = profiles.get(st.profile, self.presets_dir)
        except KeyError:
            return None
        prof = replace(base, interface=st.interface or base.interface,
                       ptt_method=st.ptt_method or base.ptt_method,
                       cat_device=st.cat_device or base.cat_device,
                       cat_baud=st.cat_baud or base.cat_baud,
                       civaddr=st.civaddr or base.civaddr, set_conf=st.set_conf or base.set_conf,
                       ptt_device=st.ptt_device or base.ptt_device)
        if prof.ptt_method == "vox":
            prof = replace(prof, rig_model=0, can_report_swr=False, can_report_alc=False)
        return prof

    # -- operator actions --

    def choose(self, profile: str, interface: str = "", ptt_method: str = "", set_level: bool = True) -> None:
        """Pick the radio. Changing it clears everything verified for the old one, and sets the profile's
        TX start level unless the caller (apply_kit) sets levels itself once the sound device is known."""
        with self._lock:
            base = profiles.get(profile, self.presets_dir)  # KeyError for an unknown name
            if interface and interface not in INTERFACES:
                raise WizardError(f"unknown interface {interface!r}")
            if ptt_method and ptt_method not in profiles.PTT_METHODS:
                raise WizardError(f"unknown PTT method {ptt_method!r}")
            self.state = State(profile=base.name, interface=interface or base.interface,
                               ptt_method=ptt_method or base.ptt_method,
                               cat_device=base.cat_device, cat_baud=base.cat_baud, civaddr=base.civaddr,
                               set_conf=base.set_conf, ptt_device=base.ptt_device,
                               answers=self.state.answers,
                               cat_proven=self.state.cat_proven)  # names its radio: another one never matches
            self._save_state()
            if set_level:
                # The profile's TX start level, so the Drive search begins where this radio makes drive at all
                # (REQ-AUDIO-009: level 20 is silence on the IC-705). The audio service writes it to the config.
                self.probes.control_fn({"cmd": "set_gain", "tx": base.tx_start_gain})

    def choose_model(self, rig_model: int, interface: str = "", ptt_method: str = "") -> str:
        """Pick any radio Hamlib knows, by model number. Saved as a user preset, so it is listed
        with the built-ins from then on and kits can name it. Returns the preset's name."""
        if isinstance(rig_model, bool) or not isinstance(rig_model, int) or not 1 < rig_model < 100_000:
            raise WizardError("the Hamlib model must be a model number from the list")
        known = hamlib.models()
        entry = next((m for m in known if m["model"] == rig_model), None)
        if known and entry is None:
            raise WizardError(f"Hamlib on this bridge has no radio model {rig_model}")
        if interface and interface not in INTERFACES:  # checked before the preset is written
            raise WizardError(f"unknown interface {interface!r}")
        if ptt_method and ptt_method not in profiles.PTT_METHODS:
            raise WizardError(f"unknown PTT method {ptt_method!r}")
        title = f"{entry['mfg']} {entry['name']}" if entry else f"Hamlib model {rig_model}"
        prof = profiles.RadioProfile(
            f"hamlib-{rig_model}", title, rig_model=rig_model, interface=interface or "internal-usb",
            ptt_method="cat", can_report_alc=False, builtin=False,
            notes="Chosen from Hamlib's list. Radio control starts at the radio's default baud rate: "
                  "if it does not answer, set the baud rate and port under Advanced to match the radio's menu.")
        with self._lock:
            try:
                profiles.save(prof, self.presets_dir)
            except OSError as e:
                raise WizardError(f"The radio could not be saved on the card: {e}") from None
            self.choose(prof.name, interface, ptt_method)
        return prof.name

    def answer(self, key: str, value: str) -> dict:
        """Record one interview answer and return the suggestion so far."""
        with self._lock:
            q = next((q for q in INTERVIEW if q["key"] == key), None)
            if q is None or value not in q["options"]:
                raise WizardError(f"unknown interview answer {key}={value!r}")
            self.state.answers[key] = value
            self._save_state()
            return interview(self.state.answers)

    def update(self, **settings) -> None:
        """Change a setting mid-flow. The step that owns it, and all after, must verify again."""
        owner = {"interface": "cabling", "cabling_confirmed": "cabling", "audio_usb_id": "cabling",
                 "audio_usb_path": "cabling", "cat_device": "cat",
                 "cat_baud": "cat", "civaddr": "cat", "ptt_method": "cat", "set_conf": "cat",
                 "ptt_device": "cat"}  # VOX turns radio control off
        with self._lock:
            before = deepcopy(self.state)
            try:
                earliest = self._apply_settings(settings, owner)
            except WizardError:
                self.state = before  # all or nothing: a refused setting leaves none of the others half set
                raise
            if (self.state.audio_usb_id, self.state.audio_usb_path) != (before.audio_usb_id, before.audio_usb_path):
                st = self.state  # a card chosen beside its identical twin is known by position alone
                st.audio_usb_strict = bool(st.audio_usb_path) and self._twins(st.audio_usb_id)
            self._invalidate_from(earliest)
            self._save_state()

    def _apply_settings(self, settings: dict, owner: dict) -> int:
        """Check and set each setting; returns the index of the earliest step that must verify again.
        Expert settings go last, checked against the radio as the other settings leave it."""
        earliest = len(STEPS)
        for k, v in sorted(settings.items(), key=lambda kv: kv[0] == "set_conf"):
            if k not in owner:
                raise WizardError(f"unknown setting {k!r}")
            if k == "interface" and v not in INTERFACES:
                raise WizardError(f"unknown interface {v!r}")
            if k == "ptt_method" and v not in profiles.PTT_METHODS:
                raise WizardError(f"unknown PTT method {v!r}")
            if k == "cat_baud" and (isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 4_000_000):
                raise WizardError("baud must be a whole number, 0 for the radio's default")
            if k in ("cat_device", "ptt_device") and not (isinstance(v, str) and re.fullmatch(r"(/dev/[A-Za-z0-9._/-]{1,80})?", v)):
                raise WizardError("the serial port must be a /dev/ path, or empty for automatic")
            if k == "set_conf":
                prof = self.profile()  # no Hamlib model (VOX, or no radio control): nothing takes them
                v = self._check_conf(v) if prof is not None and prof.rig_model else ""
            if k == "civaddr":
                try:
                    v = profiles.normalize_civaddr(v)
                except ValueError as e:
                    raise WizardError(str(e)) from None
            if k == "audio_usb_id" and not (isinstance(v, str) and re.fullmatch(r"([0-9a-f]{4}:[0-9a-f]{4})?", v)):
                raise WizardError("the sound card id must be vendor:product in hex, or empty for automatic")
            if k == "audio_usb_path" and not (isinstance(v, str) and re.fullmatch(r"(\d+-\d+(\.\d+)*)?", v)):
                raise WizardError("the sound card position must look like 1-1.2, or be empty")
            if k == "cabling_confirmed" and not isinstance(v, bool):
                raise WizardError("cabling_confirmed must be true or false")
            if getattr(self.state, k) != v:
                setattr(self.state, k, v)
                earliest = min(earliest, STEPS.index(owner[k]))
        return earliest

    def _check_conf(self, text) -> str:
        """Expert settings, checked against the chosen radio's own Hamlib list (the dummy rig's when
        it keys by RTS or DTR with no radio control)."""
        prof = self.profile()
        model = prof.rig_model if prof is not None and prof.rig_model else 1
        try:
            return hamlib.check_conf(text, hamlib.conf_params(model))
        except ValueError as e:
            raise WizardError(f"Expert settings: {e}.") from None

    def _invalidate_from(self, index: int) -> None:
        st = self.state
        st.passed = [s for s in st.passed if STEPS.index(s) < index]
        if STEPS.index(st.step) > index:
            st.step = STEPS[index]

    def goto(self, step: str) -> None:
        """Go back to an earlier step, or forward to one already reached. Never skips verification."""
        with self._lock:
            if step not in STEPS:
                raise WizardError(f"unknown step {step!r}")
            reach = len(self.state.passed)  # index of the first unverified step
            if STEPS.index(step) > reach:
                raise WizardError(f"{TITLES[STEPS[reach]]} has not passed yet")
            self.state.step = step
            self._save_state()

    def advance(self) -> Result:
        """Verify the current step; move on only if it passes."""
        with self._lock:
            st = self.state
            res = self.verify(st.step)
            i = STEPS.index(st.step)
            if not res.ok:  # a step that fails now un-proves itself and everything after it
                self._invalidate_from(i)
                self._save_state()
                return res
            self._mark_passed(st.step)
            if i + 1 < len(STEPS):
                st.step = STEPS[i + 1]
            self._save_state()
            return res

    def _mark_passed(self, step: str) -> None:
        if step == "cat":
            self.state.cat_proven = _cat_settings(self.profile())
        if step not in self.state.passed:
            self.state.passed.append(step)
            self.state.passed.sort(key=STEPS.index)

    # -- verification: one function per step, none of them transmit --

    def verify(self, step: str) -> Result:
        return getattr(self, f"_verify_{step}")()

    def _verify_identify(self) -> Result:
        prof = self.profile()
        if prof is None:
            return Result(False, "Pick your radio from the list, or choose \"I'm not sure\".")
        try:
            prof.validate()
        except ValueError:
            return Result(False, f"{prof.display_name} has no radio control, so PTT cannot be CAT. Pick RTS, DTR or VOX.")
        return Result(True, f"{prof.display_name} via {INTERFACES.get(prof.interface, prof.interface)}.")

    def _verify_cabling(self) -> Result:
        if not self.state.cabling_confirmed:
            return Result(False, "Plug the cables in as shown, then confirm.")
        picked = self._pick_sound_card()
        if picked is not None and not picked.ok:
            return picked
        status = self.probes.control_fn({"cmd": "status"})
        if status is None or not status.get("alsa_device"):
            return Result(False, "The Pi cannot see a USB sound device yet. Check the USB cable at both ends.")
        why = f" {picked.detail}" if picked is not None and picked.detail else ""
        return Result(True, f"The Pi sees the sound device ({status.get('alsa_device', 'unknown')}).{why}")

    def _pick_sound_card(self) -> Result | None:
        """Point the audio service at the radio's sound card. None when the Pi lists no USB card.

        A card chosen by hand wins. Otherwise the card on the same USB device as the radio's
        control port, or alone behind the same hub (a Digirig), is the radio's. With no such
        card the service takes the first USB card, as it always has.
        """
        try:
            cards = [c for c in self.probes.cards_fn() if c.is_usb]
        except OSError:
            cards = []
        if not cards:
            return None
        st, prof = self.state, self.profile()
        why = ""
        if st.audio_usb_id or st.audio_usb_path:
            vid, _, pid = st.audio_usb_id.partition(":")
            # A twin now, or when it was chosen, means a card of that id elsewhere may be the other one.
            strict = bool(st.audio_usb_path) and (st.audio_usb_strict or self._twins(st.audio_usb_id, cards))
            want = select_card(cards, vid, pid, st.audio_usb_path, strict)
            if want is None:
                return Result(False, "The sound card chosen for this radio is not plugged in. Plug it in, "
                                     "or set the sound card back to Automatic under Advanced.")
        else:
            port = with_port(prof).cat_device if prof is not None and prof.needs_rigctld else ""
            want = usbtopo.best_match(self.probes.serial_usb_fn(port), cards) if port else None
            if want is not None and len(cards) > 1:
                why = f"Using {want.name}, on the same USB connection as the radio's control cable."
            strict = want is not None and bool(want.usb_path) and self._twins(want.usb_id, cards)
        sel = {"vendor": want.usb_vendor_id, "product": want.usb_product_id, "usb_path": want.usb_path,
               "strict": strict} if want is not None else {"vendor": "", "product": "", "usb_path": ""}
        r = self.probes.control_fn({"cmd": "select_audio", **sel})
        if not r or not r.get("ok"):
            return Result(False, "The audio service did not take the sound card. Wait a few seconds and try again.")
        if r.get("changed") and want is not None:
            for _ in range(100):  # about a second; up to 10 s when recent reopens have climbed the backoff
                s = self.probes.control_fn({"cmd": "status"}) or {}
                if (s.get("audio_device") or {}).get("ok") and f"hw:{want.index} " in f"{s.get('alsa_device', '')} ":
                    break
                threading.Event().wait(self.probes.sample_gap_s / 2)
            else:
                return Result(False, f"The audio service did not open {want.name}. Check its USB cable, then try again.")
        return Result(True, why)

    @staticmethod
    def _answers(cat) -> bool:
        """rigctld is up and reads the keying line (read-only: get_ptt never keys)."""
        try:
            if not cat.alive():
                return False
            cat.get_ptt()
        except (CatError, KeyError, ValueError, OSError):
            return False
        return True

    def _twins(self, usb_id: str, cards: list | None = None) -> bool:
        """True when more than one USB sound card has this vendor:product id."""
        if cards is None:
            try:
                cards = [c for c in self.probes.cards_fn() if c.is_usb]
            except OSError:
                return False
        return bool(usb_id) and sum(c.usb_id == usb_id for c in cards) > 1

    def _verify_audio_in(self) -> Result:
        stop = threading.Event()
        statuses = []
        for n in range(self.probes.samples):
            s = self.probes.control_fn({"cmd": "status"})
            if s is not None:
                statuses.append(s)
            if n + 1 < self.probes.samples:
                stop.wait(self.probes.sample_gap_s)
        chk = readiness.check_rx_audio(readiness.Inputs(statuses=statuses, service_up=bool(statuses)))
        return Result(chk.state == readiness.OK, chk.detail if chk.state == readiness.OK else f"{chk.detail} {chk.fix}")

    def resume_cat(self) -> bool:
        """At boot: restart radio control for a rig whose CAT step already passed.

        rigctld is otherwise only started by the CAT step, so a power-cycled kit had no CAT.
        A step before CAT that fails (a kit loaded while the audio service restarts) un-passes CAT
        too, but the radio control settings that passed it still stand, so those resume as well.
        """
        prof = self.profile()
        if prof is None or not prof.needs_rigctld or self.probes.start_cat is None:
            return False
        if "cat" not in self.state.passed and self.state.cat_proven != _cat_settings(prof):
            return False
        try:
            self.probes.start_cat(prof)
        except (CatError, OSError, ValueError):
            return False  # radio not plugged in yet: the supervisor's keep-alive retries this profile
        return True

    def find_baud(self) -> Result:
        """At the CAT step: try each common baud rate until the radio answers, then save that rate
        and check CAT again. Read-only: each try only reads the frequency. With no answer the kit's
        own rate is kept, and the CAT check that ends the search puts its rigctld back."""
        with self._lock:
            prof = self.profile()
            if self.state.step != "cat":
                raise WizardError("The baud rate is searched for at the radio control step.")
            if prof is None or prof.rig_model == 0:
                raise WizardError("This setup has no radio control, so there is no baud rate to find.")
            if self.probes.start_cat is None or self.probes.cat is None:
                raise WizardError("Radio control is not available on this bridge.")
            port = with_port(prof).cat_device
            if not port:
                raise WizardError("Choose the radio's port under Advanced first, then search for the baud rate.")
            tried, found = list(dict.fromkeys((prof.cat_baud, *FIND_BAUDS))), None
            for baud in tried:
                try:
                    self.probes.start_cat(replace(prof, cat_baud=baud))
                    if self.probes.cat.alive():
                        found = baud
                        break
                except (CatError, OSError, ValueError):
                    continue
            if found is not None:
                self.update(cat_baud=found)
            res = self.advance()
            if found is not None:
                lead = f"Found it: the radio answers at {found or 'its default'} baud."
                return Result(res.ok, lead if res.ok else f"{lead} {res.detail}", res.log)
            if res.ok:
                return res
            rates = ", ".join(str(b) if b else "its default" for b in tried)
            return Result(False, f"No baud rate got an answer on {port} (tried {rates}). {res.detail}", res.log)

    def _verify_cat(self) -> Result:
        res = self._check_cat()
        prof = self.profile()
        if not res.ok and prof is not None and prof.needs_rigctld and self.probes.cat_log is not None:
            res.log = self.probes.cat_log(prof)  # Hamlib's own words: wrong port, busy port, no reply
        return res

    def _check_cat(self) -> Result:
        prof = self.profile()
        if prof is None:
            return Result(False, "Pick a radio first.")
        if prof.rig_model == 0:
            if prof.ptt_method == "vox":
                return Result(True, "Keying by VOX, so radio control is not used. Nothing to check; SWR and ALC cannot be read.")
            # Keying by RTS or DTR still goes through rigctld (Hamlib's dummy rig), so start it here.
            line = prof.ptt_method.upper()
            if self.probes.start_cat is not None:
                try:
                    self.probes.start_cat(prof)
                except (CatError, OSError, ValueError) as e:
                    return Result(False, f"The {line} keying line could not be opened: {e}")
            if self.probes.cat is not None and not self._answers(self.probes.cat):
                port = with_port(prof).cat_device or "the serial port"
                return Result(False, f"The {line} keying line on {port} could not be opened. Check the interface's "
                                     "USB cable, and that no other program is using the port.")
            return Result(True, f"This setup has no radio control, so there is nothing to check; SWR and ALC "
                                f"cannot be read. Keying uses the {line} line.")
        if self.probes.start_cat is not None:
            try:
                self.probes.start_cat(prof)
            except (CatError, OSError, ValueError) as e:
                return Result(False, f"Radio control could not start: {e}")
        if self.probes.cat is not None and self.probes.cat.alive():
            return Result(True, f"The radio answers on CAT at {prof.cat_baud or 'its default'} baud.")
        if not prof.cat_device:
            ports = self.probes.ports_fn()
            if not ports:
                return Result(False, "The radio is not answering on CAT. No USB serial port was found: "
                                     "check the radio's USB cable, or type the port under Advanced.")
            if len(ports) > 1:
                names = ", ".join(Path(p).name for p in ports)
                return Result(False, f"The radio is not answering on CAT. {len(ports)} USB serial devices "
                                     f"are plugged in ({names}): choose the radio's port under Advanced.")
            return Result(False, f"The radio is not answering on CAT on {ports[0]}.")
        return Result(False, "The radio is not answering on CAT.")

    def _verify_ptt(self) -> Result:
        """Checks the keying path is set up and the radio is in receive. It does not key."""
        prof = self.profile()
        if prof is None:
            return Result(False, "Pick a radio first.")
        if prof.ptt_method == "vox":
            return Result(True, "PTT is VOX: the radio keys itself from transmit audio. Turn VOX on at the radio.")
        if prof.ptt_method in ("rts", "dtr"):
            prof = with_port(prof)  # "automatic" is the one USB serial device, as rigctld gets it
            port = prof.ptt_device or prof.cat_device  # a separate keying port, as rigctld's -p gets it
            if not port:
                return Result(False, f"PTT on {prof.ptt_method.upper()} needs the interface's serial port. Choose it above.")
            if not Path(port).exists():
                return Result(False, f"The serial port {port} is not there. Check the interface's USB cable.")
            return Result(True, f"PTT is the {prof.ptt_method.upper()} line on {port}. Keying is proven in the transmit checks.")
        cat = self.probes.cat
        try:
            keyed = cat.get_ptt() if cat is not None and cat.alive() else None
        except (CatError, KeyError, ValueError):
            keyed = None
        if keyed is None:
            return Result(False, "The radio's transmit state cannot be read over CAT.")
        if keyed:
            return Result(False, "The radio is transmitting and the bridge did not ask it to. Turn VOX off and unplug the microphone.")
        return Result(True, "The radio reports receive and accepts CAT keying. Keying is proven in the transmit checks.")

    def _verify_ladder(self) -> Result:
        g = (self.probes.ladder() if self.probes.ladder else {}).get("gates") or {}
        if g.get("passed"):
            return Result(True, "All transmit checks passed.")
        if g.get("bypassed"):
            return Result(True, "Rig unverified: bypassed "
                          + ", ".join("ham band check" if x == "band" else x for x in g["bypassed"]) + ".")
        return Result(False, "Transmit is locked until the transmit checks have run.")

    # -- kits --

    def kit_ready(self) -> bool:
        return all(s in self.state.passed for s in STEPS[:STEPS.index(KIT_STEP) + 1])

    def save_kit(self, name: str) -> Path:
        with self._lock:
            name = name.strip()
            if not KIT_NAME_RE.fullmatch(name):
                raise WizardError("Kit names are 1 to 40 letters, digits, spaces, dashes or underscores.")
            if not self.kit_ready():
                raise WizardError("Finish the steps up to PTT before saving a kit.")
            prof, st = self.profile(), self.state
            for i, step in enumerate(STEPS[:STEPS.index(KIT_STEP) + 1]):  # a kit records what works now, not last week
                res = self.verify(step)
                if not res.ok:
                    self._invalidate_from(i)
                    self._save_state()
                    raise WizardError(f"{TITLES[step]} no longer passes: {res.detail}")
            target = self.kits_dir / f"{_slug(name)}.json"
            if target.is_file():
                try:
                    other = json.loads(target.read_text()).get("name")
                except (OSError, ValueError):
                    other = None
                if other and other != name:
                    raise WizardError(f"That name is too close to the saved kit {other!r}. Pick another.")
            status = self.probes.control_fn({"cmd": "status"}) or {}
            # Levels are worth keeping only once Drive has settled the TX level, or the operator has set it by
            # hand on a radio with no ALC readout (its Drive check is skipped). Earlier, the kit would store the
            # profile's start level and restore it over a later Drive pass (REQ-AUDIO-033).
            levels = ({k: status[k] for k in ("rx_gain", "tx_gain") if k in status}
                      if self._levels_settled(prof) else {})
            kit = {"name": name, "radio": st.profile, "radio_name": prof.display_name,
                   "interface": prof.interface, "cabling": {"confirmed": True, "answers": st.answers},
                   "levels": levels, "levels_note": "" if levels else LEVELS_NOT_SET,
                   "cat": {"rig_model": prof.rig_model, "device": prof.cat_device, "baud": prof.cat_baud,
                           "civaddr": prof.civaddr, "set_conf": prof.set_conf},
                   "audio": {"usb_id": st.audio_usb_id, "usb_path": st.audio_usb_path, "strict": st.audio_usb_strict},
                   "ptt": {"method": prof.ptt_method, "device": prof.ptt_device}}
            path = persist.save(target, json.dumps(kit, indent=1))
            st.kit = name
            self._save_state()
            return path

    def _levels_settled(self, prof: profiles.RadioProfile) -> bool:
        g = (self.probes.ladder() if self.probes.ladder else {}).get("gates") or {}
        if g.get("drive_passed"):
            return True
        # No ALC over CAT: the Unlock sheet tells the operator to set the level by the radio's meter and
        # skip the check, so a skipped Drive on such a radio is the hand-set case.
        return not prof.can_report_alc and "drive" in (g.get("bypassed") or [])

    def kits(self) -> list[dict]:
        out = []
        if self.kits_dir.is_dir():
            for path in sorted(self.kits_dir.glob("*.json")):
                try:
                    out.append(json.loads(path.read_text()))
                except (OSError, ValueError):
                    continue  # one damaged kit must not hide the others
        return out

    def _kit_file(self, name: str) -> tuple[Path, dict]:
        """The file and contents of the kit called `name`, found by its name as apply_kit does."""
        for path in sorted(self.kits_dir.glob("*.json")) if self.kits_dir.is_dir() else []:
            try:
                kit = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(kit, dict) and kit.get("name") == name:
                return path, kit
        raise WizardError(f"There is no saved kit called {name!r}.")

    def delete_kit(self, name: str) -> None:
        """The radio setup stays as it is; it is just no longer saved under that name."""
        with self._lock:
            path, _ = self._kit_file(name)
            try:
                with persist.writable(path):
                    path.unlink(missing_ok=True)
            except OSError as e:
                raise WizardError(f"The kit could not be deleted from the card: {e}") from None
            if self.state.kit == name:
                self.state.kit = ""
                self._save_state()

    def rename_kit(self, name: str, new_name: str) -> None:
        with self._lock:
            new_name = new_name.strip()
            if not KIT_NAME_RE.fullmatch(new_name):
                raise WizardError("Kit names are 1 to 40 letters, digits, spaces, dashes or underscores.")
            path, kit = self._kit_file(name)
            if new_name == name:
                return
            target = self.kits_dir / f"{_slug(new_name)}.json"
            if target != path and target.exists():
                raise WizardError("That name is too close to another saved kit. Pick another.")
            try:
                with persist.writable(target):  # one remount: writable() does not nest
                    persist.atomic_write(target, json.dumps(kit | {"name": new_name}, indent=1))
                    if target != path:
                        path.unlink(missing_ok=True)
            except OSError as e:
                raise WizardError(f"The kit could not be renamed on the card: {e}") from None
            if self.state.kit == name:
                self.state.kit = new_name
                self._save_state()

    def reset(self) -> None:
        """Forget the radio setup: the wizard's place, every kit and every radio chosen from
        Hamlib's list. rigctld stops, so no app on 4532 keeps controlling the old radio."""
        with self._lock:
            if self.probes.stop_cat is not None:
                self.probes.stop_cat()
            presets = self.presets_dir or profiles.DEFAULT_PRESETS_DIR
            kits = sorted(self.kits_dir.glob("*.json")) if self.kits_dir.is_dir() else []
            # Only the radios picked from Hamlib's list: presets installed by vb-maintenance stay.
            files = kits + (sorted(presets.glob("hamlib-*.toml")) if presets.is_dir() else [])
            self.state = State()
            try:
                with persist.writable(self.state_path):  # one remount: writable() does not nest
                    for path in files:
                        path.unlink(missing_ok=True)
                    persist.atomic_write(self.state_path, json.dumps(asdict(self.state), indent=1))
            except OSError as e:
                raise WizardError(f"The card refused part of the reset: {e}") from None

    def apply_kit(self, name: str) -> Result:
        """One tap: load the kit's settings, restore its levels, then re-verify every step
        in order and stop on the first that does not pass."""
        with self._lock:
            kit = next((k for k in self.kits() if k.get("name") == name), None)
            if kit is None:
                raise WizardError(f"no kit named {name!r}")
            self.choose(kit["radio"], kit.get("interface", ""), kit.get("ptt", {}).get("method", ""), set_level=False)
            st = self.state
            st.cat_device = kit.get("cat", {}).get("device", "") or st.cat_device
            st.cat_baud = int(kit.get("cat", {}).get("baud", 0) or st.cat_baud)
            try:
                st.civaddr = profiles.normalize_civaddr(kit.get("cat", {}).get("civaddr", "")) or st.civaddr
            except ValueError:
                pass  # a hand-edited kit: keep the radio's default and let the CAT step say so
            try:
                st.set_conf = self._check_conf(kit.get("cat", {}).get("set_conf", "")) or st.set_conf
            except WizardError:
                pass  # a setting this Hamlib no longer takes: leave it out and let the CAT step say so
            ptt_dev = kit.get("ptt", {}).get("device", "")
            if isinstance(ptt_dev, str) and re.fullmatch(r"/dev/[A-Za-z0-9._/-]{1,80}", ptt_dev):
                st.ptt_device = ptt_dev
            audio = kit.get("audio", {})
            if isinstance(audio, dict) and isinstance(audio.get("usb_id", ""), str) and isinstance(audio.get("usb_path", ""), str):
                st.audio_usb_id, st.audio_usb_path = audio.get("usb_id", ""), audio.get("usb_path", "")
                st.audio_usb_strict = audio.get("strict") is True
            st.cabling_confirmed = True  # same hardware; the sound-device probe still has to pass
            st.answers = kit.get("cabling", {}).get("answers", {})
            st.kit = name
            levels = {k.removesuffix("_gain"): v for k, v in kit.get("levels", {}).items()}
            if "tx" not in levels:  # a kit saved before Drive settled the level: the profile's start applies (REQ-AUDIO-033)
                levels["tx"] = self.profile().tx_start_gain
            res = Result(True, "")
            for step in STEPS:
                st.step = step
                res = self.verify(step)
                if not res.ok:
                    break
                self._mark_passed(step)
                if step == "cabling" and levels:  # only once the sound device is known to be there
                    self.probes.control_fn({"cmd": "set_gain", **levels})
            self._save_state()
            return res

    # -- what the page shows --

    def _usb_cards(self) -> list:
        try:
            return [c for c in self.probes.cards_fn() if c.is_usb]
        except OSError:
            return []

    def _serial_ports(self, cards: list, current: str) -> list[dict]:
        """The USB serial ports to pick from, each with the sound card that shares its USB device or hub.

        One per device (its lowest interface), plus the chosen port when it is another interface
        that is plugged in, such as the second UART of a CP2105 or FT2232.
        """
        listed = self.probes.ports_fn()
        extra = [current] if current and current not in listed and Path(current).exists() else []
        out = []
        for path in listed + extra:
            card = usbtopo.best_match(self.probes.serial_usb_fn(path), cards)
            label = port_label(path)
            iface = re.search(r"-(if\d+(-port\d+)?)$", path) if path in extra else None
            out.append({"path": path, "label": f"{label} ({iface.group(1)})" if iface else label,
                        "sound_card": card.name if card is not None else ""})
        return out

    def report(self) -> dict:
        with self._lock:
            st, prof = self.state, self.profile()
            cards = self._usb_cards()
            return {"ok": True, "step": st.step, "passed": list(st.passed), "kit": st.kit,
                    "kit_ready": self.kit_ready(),
                    "steps": [{"key": s, "title": TITLES[s], "passed": s in st.passed, "help": list(HELP[s])}
                              for s in STEPS],
                    "profile": prof.as_dict() if prof else None,
                    "cabling_confirmed": st.cabling_confirmed,
                    "audio_card": {"usb_id": st.audio_usb_id, "usb_path": st.audio_usb_path},
                    "sound_cards": [{"index": c.index, "name": c.name, "usb_id": c.usb_id, "usb_path": c.usb_path}
                                    for c in cards],
                    "serial_ports": self._serial_ports(cards, prof.cat_device if prof else ""),
                    "radios": [{"name": p.name, "display_name": p.display_name, "interface": p.interface, "notes": p.notes,
                                "rig_model": p.rig_model, "cat_baud": p.cat_baud, "ptt_method": p.ptt_method,
                                "can_report_swr": p.can_report_swr, "can_report_alc": p.can_report_alc,
                                "min_power_w": p.min_power_w}
                               for p in profiles.load_all(self.presets_dir).values()],
                    "interfaces": INTERFACES, "ptt_methods": list(profiles.PTT_METHODS),
                    "interview": {"questions": list(INTERVIEW), "answers": st.answers,
                                  "suggestion": interview(st.answers) if st.answers else None},
                    "kits": [{"name": k.get("name"), "radio_name": k.get("radio_name"), "interface": k.get("interface"),
                              "ptt_method": k.get("ptt", {}).get("method"), "cat_baud": k.get("cat", {}).get("baud"),
                              "levels": k.get("levels", {}), "levels_note": k.get("levels_note", "")} for k in self.kits()]}


CAT_SETTINGS = ("name", "rig_model", "cat_device", "cat_baud", "civaddr", "set_conf", "ptt_method", "ptt_device")


def _cat_settings(prof: RadioProfile | None) -> dict:
    """What rigctld is started with: the radio and its control and keying settings."""
    return {} if prof is None else {k: getattr(prof, k) for k in CAT_SETTINGS}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "kit"


def handle(wiz: Wizard, body: dict) -> tuple[int, dict]:
    """One POST endpoint for the page: {"action": ..., ...}. Replies with the fresh report."""
    action = body.get("action")
    result: Result | None = None
    extra: dict = {}
    try:
        if action == "choose":
            wiz.choose(str(body.get("profile", "")), str(body.get("interface", "")), str(body.get("ptt_method", "")))
        elif action == "choose_model":
            wiz.choose_model(body.get("rig_model"), str(body.get("interface", "")), str(body.get("ptt_method", "")))
        elif action == "answer":
            extra["suggestion"] = wiz.answer(str(body.get("key", "")), str(body.get("value", "")))
        elif action == "update":
            settings = body.get("settings")
            if not isinstance(settings, dict):
                raise WizardError("settings must be an object")
            wiz.update(**settings)
        elif action == "goto":
            wiz.goto(str(body.get("step", "")))
        elif action == "advance":
            result = wiz.advance()
        elif action == "find_baud":
            result = wiz.find_baud()
        elif action == "save_kit":
            wiz.save_kit(str(body.get("name", "")))
        elif action == "apply_kit":
            result = wiz.apply_kit(str(body.get("name", "")))
        elif action == "delete_kit":
            wiz.delete_kit(str(body.get("name", "")))
        elif action == "rename_kit":
            wiz.rename_kit(str(body.get("name", "")), str(body.get("new_name", "")))
        else:
            return 400, {"ok": False, "error": "unknown_action"}
    except KeyError:
        return 400, {"ok": False, "error": "unknown_radio"}
    except (WizardError, TypeError, ValueError) as e:
        return 400, {"ok": False, "error": str(e)}
    report = wiz.report() | extra
    if result is not None:
        report["result"] = asdict(result)
    return 200, report
