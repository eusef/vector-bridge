"""The owner's own access point name and passphrase.

A card burned from the release image comes up with an OPEN access point and a marker file.
While the marker exists the web UI shows only the Wi-Fi page and the transmitter cannot be
keyed. The owner picks a name (always prefixed VectorBridge-) and a passphrase; the same page
changes them later.

The web process has no root (its only sudo is the /persist remount), so it only writes a pending
request to the persist partition. vb-ap-apply.path sees the file and runs `apply` as root:

  root is the overlay (field mode)  -> write the NetworkManager profile (on /persist, remounted
                                       rw for those seconds) and the text file on the boot
                                       partition, clear the request and the marker. No reboot:
                                       the access point restarts under its new name.
  root is writable (first boot)     -> the same, then switch read-only boot and the overlay on
                                       and reboot into field mode.
                                       If NetworkManager refuses the profile the previous one
                                       is put back and the request is dropped.

NetworkManager keeps its keyfiles under /persist/vector-bridge/networks (`[keyfile] path`, set by
install.sh), so a change made under the overlay survives the next boot and the root is never
made writable for it. Power can go at any moment.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import manage, persist, qr, wificountry, wifiradio

PREFIX = "VectorBridge-"
SSID_MAX = 32                       # bytes, 802.11
PENDING = persist.APP_DIR / "ap-pending.json"
UNSECURED = persist.APP_DIR / "ap-unsecured"
WIFI_TXT = Path("/boot/firmware/vector-bridge-wifi.txt")
FIRSTBOOT_FLAG = Path("/etc/vector-bridge-firstboot")
PATH_UNIT = "vb-ap-apply.path"
_SUFFIX = re.compile(r"[A-Za-z0-9_-]+")
_PASS = re.compile(r"[\x20-\x7e]{8,63}")     # WPA2: 8 to 63 printable ASCII


def validate(suffix, passphrase) -> tuple[str, str, list[str]]:
    """(ssid, passphrase, errors). Root runs this again on the pending file: the file is
    written by an unprivileged process, so nothing in it is trusted."""
    errors = []
    suffix = suffix.strip() if isinstance(suffix, str) else ""
    room = SSID_MAX - len(PREFIX)
    if not _SUFFIX.fullmatch(suffix) or len(suffix) > room:
        errors.append(f"Name: 1 to {room} letters, digits, hyphen or underscore.")
    if not isinstance(passphrase, str) or not _PASS.fullmatch(passphrase):
        errors.append("Passphrase: 8 to 63 characters, plain letters, digits and symbols.")
    return PREFIX + suffix, passphrase if isinstance(passphrase, str) else "", errors


_open_cache: list = [0.0, None]     # [when asked, answer]; None = never answered


def ap_profile_is_open() -> bool:
    """NetworkManager's own answer. key-mgmt is not a secret, so the web process can read it.

    This sits on the PTT keying path, so it must not fork nmcli there in normal use. A profile
    only ever becomes open in vb-firstboot, which runs before this process starts, and only
    becomes secured on the way to a reboot. So "secured" is remembered for the life of the
    process; "open" is asked again every few seconds; no answer counts as not open, is not
    remembered, and is asked again."""
    when, answer = _open_cache
    now = time.monotonic()
    if answer is False or (answer is True and now - when < 5):
        return answer
    try:
        r = subprocess.run(["nmcli", "-g", "802-11-wireless.mode,802-11-wireless-security.key-mgmt",
                            "connection", "show", manage.AP_CON], capture_output=True, text=True, check=False, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:           # a missing profile too: apply() deletes it for a moment
        return False
    mode, _, key_mgmt = r.stdout.strip().partition("\n")
    _open_cache[:] = [now, mode == "ap" and not key_mgmt.strip()]
    return _open_cache[1]


def unsecured() -> bool:
    """The marker alone would fail open: it is on /persist, which can fail to mount, and its
    directory is writable by the web user. So an open profile in NetworkManager counts too."""
    return UNSECURED.exists() or ap_profile_is_open()


def current_ssid() -> str:
    r = manage._nmcli("-g", "802-11-wireless.ssid", "connection", "show", manage.AP_CON, sudo=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def report() -> dict:
    return {"ok": True, "unsecured": unsecured(), "pending": PENDING.exists(), "prefix": PREFIX,
            "ssid": current_ssid(), "suffix_max": SSID_MAX - len(PREFIX), "country": wificountry.current()}


def handle(body: dict, pending: Path | None = None, country_path: Path | None = None) -> tuple[int, dict]:
    """POST /api/wifi from the web UI. The first-boot sheet also sends the Wi-Fi country (R-D7),
    optional: while the access point is open no other POST answers."""
    ssid, passphrase, errors = validate(body.get("suffix"), body.get("passphrase"))
    country = wificountry.check(body.get("country"))
    if body.get("country") not in (None, "") and not country:
        errors.append("Country: choose one from the list.")
    # Securing the open network under its own name strands every phone that joined it: they keep the open
    # profile and fail to join (iPhone, iPad and Mac on the 2.0 card, 2026-10-15). A new name joins first try.
    if not errors and unsecured() and ssid == current_ssid():
        errors.append("Name: choose a new name. Phones that joined the open Wi-Fi remember it and will not join it with a passphrase.")
    if errors:
        return 400, {"ok": False, "errors": errors}
    try:
        if country:     # first: the apply below may reboot a brand-new card
            wificountry.save(country, country_path)
        persist.save(pending or PENDING, json.dumps({"ssid": ssid, "passphrase": passphrase}), mode=0o600)
    except OSError as e:
        return 500, {"ok": False, "errors": [f"Could not write to the card: {e}"]}
    return 200, {"ok": True, "ssid": ssid}


# ---- root side ---------------------------------------------------------------------


def _run(*cmd: str) -> None:
    subprocess.run(cmd, check=True)


def overlay_on() -> bool:
    r = subprocess.run(["findmnt", "-no", "FSTYPE", "/"], capture_output=True, text=True, check=False)
    return "overlay" in r.stdout


def write_wifi_txt(ssid: str, passphrase: str | None, path: Path | None = None) -> None:
    """The copy the owner can read by putting the card in any computer. The boot partition is
    remounted rw for the write and put back the way it was found."""
    path = path or WIFI_TXT
    if passphrase:
        text = f"VectorBridge Wi-Fi\n\nNetwork:    {ssid}\nPassphrase: {passphrase}\n\nChange them at http://vectorbridge/wifi\n"
    else:
        text = (f"VectorBridge Wi-Fi\n\nNetwork:    {ssid}\nPassphrase: none yet, the network is open\n\n"
                "Join it from a phone. The page that opens asks you to name the network and set a passphrase.\n")
    boot = str(path.parent)
    opts = subprocess.run(["findmnt", "-no", "OPTIONS", boot], capture_output=True, text=True, check=False).stdout
    was_ro = "ro" in opts.strip().split(",")
    if was_ro:
        subprocess.run(["mount", "-o", "remount,rw", boot], check=True)
    try:
        persist.atomic_write(path, text)
    finally:
        if was_ro:
            subprocess.run(["mount", "-o", "remount,ro", boot], check=False)


def read_wifi_txt(path: Path | None = None) -> tuple[str, str | None]:
    """(ssid, passphrase or None) from the owner's copy on the boot partition, which is the
    one place the web process (no root) can read the passphrase. ("", None) if unreadable."""
    try:
        text = (path or WIFI_TXT).read_text()
    except OSError:
        return "", None
    fields = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
    ssid = fields.get("Network", "").strip()
    psk = fields.get("Passphrase", "").strip()
    open_net = psk.startswith("none yet")       # the wording write_wifi_txt uses for an open network
    return ssid, (psk if psk and not open_net and _PASS.fullmatch(psk) else None)


def qr_svg() -> str | None:
    """The WIFI: code phones scan to join this bridge; None while the network is open."""
    ssid, psk = read_wifi_txt()
    if not ssid or not psk or unsecured():
        return None
    return qr.svg(qr.encode(qr.wifi_string(ssid, psk)))


def lock_down() -> None:
    """Same steps as `vb-maintenance exit`. Only changes what the NEXT boot does."""
    r = subprocess.run(["raspi-config", "nonint", "get_bootro_conf"], capture_output=True, text=True, check=True)
    if r.stdout.strip() != "0":   # enable_bootro appends ",ro" every time it runs
        _run("raspi-config", "nonint", "enable_bootro")
    _run("raspi-config", "nonint", "enable_overlayfs")


def _clear(*paths: Path) -> None:
    # writable() decides by resolving its argument; a hostile symlink must not talk it out of the remount.
    try:
        with persist.writable(persist.APP_DIR / ".apsetup"):
            for path in paths:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"vb-ap-apply: could not remove {[p.name for p in paths]}: {e}", file=sys.stderr)
    if PENDING in paths and os.path.lexists(PENDING):
        # The path unit would start this service again for ever. No more changes until a reboot.
        print(f"vb-ap-apply: request cannot be removed, stopping {PATH_UNIT}", file=sys.stderr)
        subprocess.run(["systemctl", "stop", PATH_UNIT], check=False)


def _read_request() -> tuple[str, str, list[str]]:
    """Nothing in the file is trusted: the web user wrote it, and owns the directory it is in."""
    try:
        fd = os.open(PENDING, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)   # a FIFO must not hang root
        with os.fdopen(fd) as f:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return "", "", ["not a regular file"]
            req = json.loads(f.read(4096))
        raw = req["ssid"]
        if not isinstance(raw, str) or not raw.startswith(PREFIX):
            return "", "", ["bad prefix"]
        return validate(raw[len(PREFIX):], req["passphrase"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        return "", "", [str(e)]


def _current() -> tuple[str, str]:
    r = manage._nmcli("-s", "-g", "802-11-wireless.ssid,802-11-wireless-security.psk", "connection", "show", manage.AP_CON)
    ssid, _, psk = r.stdout.strip().partition("\n")
    return (ssid, psk.strip()) if r.returncode == 0 else ("", "")


def first_boot_open() -> int:
    """Called by vb-firstboot: open AP on this Pi's serial, marker, text file."""
    ssid = manage.default_ap_ssid()
    if not manage._ap_create(ssid, None, open_ap=True, announce=False):
        return 1
    persist.save(UNSECURED, "", mode=0o644)
    write_wifi_txt(ssid, None)
    manage._nmcli("connection", "up", manage.AP_CON)
    return 0


def apply() -> int:
    """Called by vb-ap-apply.service when a pending request exists.

    vb-ap-apply.path fires again for as long as the request file exists, so every way out of
    here either removes the file or reboots. Power can go at any line: the profile lands on
    /persist atomically (NetworkManager writes the keyfile then renames it), and on a first
    boot the request is removed only after the next boot is already set to be read-only, so a
    cut before that point repeats the apply and a cut after it boots into field mode."""
    with wifiradio.locked():    # the watcher must not move the access point while it is re-made
        return _apply()


def _apply() -> int:
    ssid, passphrase, errors = _read_request()
    if errors:
        print(f"vb-ap-apply: request rejected: {errors}", file=sys.stderr)
        _clear(PENDING)
        return 2
    rc = 0
    try:
        old_ssid, old_psk = _current()
        was_open = UNSECURED.exists() or (bool(old_ssid) and not old_psk)
        if manage._ap_create(ssid, passphrase, announce=False):
            now_ssid, now_psk = ssid, passphrase
        else:
            # _ap_create deletes the old profile first, so put back what was there. If the old
            # values could not be read, an open bridge reopens and a secured one gets a fresh
            # random passphrase: either way the text file below says what the radio really does.
            rc = 1
            print("vb-ap-apply: NetworkManager refused the new profile; restoring the previous one", file=sys.stderr)
            made = manage._ap_create(old_ssid or None, old_psk or None, open_ap=was_open, announce=False)
            now_ssid, now_psk = made or (old_ssid, old_psk)
        try:
            write_wifi_txt(now_ssid, now_psk or None)
        except (OSError, subprocess.CalledProcessError) as e:
            rc = rc or 3                # the radio is right; only the owner's copy on the card is stale
            print(f"vb-ap-apply: could not write {WIFI_TXT}: {e}", file=sys.stderr)
        # _ap_create deleted the profile that was on the air. For a secured one this also comes
        # BEFORE the gate lifts: until then the radio is still the open network, and a stranger
        # on it must not find the full UI.
        manage._nmcli("connection", "up", manage.AP_CON)
        if now_psk and overlay_on():    # field mode: the profile is on /persist, nothing else to do
            _clear(UNSECURED, PENDING)
        elif now_psk:                   # first boot (writable root): a secured bridge goes to field mode
            # A first boot that failed half way left its flag; run again it would reopen this bridge.
            FIRSTBOOT_FLAG.unlink(missing_ok=True)
            lock_down()
            _clear(UNSECURED, PENDING)
            _run("reboot")
        # an open bridge waits for the owner
    except (OSError, subprocess.CalledProcessError) as e:
        rc = rc or 4
        print(f"vb-ap-apply: failed: {e}", file=sys.stderr)
    finally:
        _clear(PENDING)                 # never leave the path unit something to spin on
    return rc


if __name__ == "__main__":
    sys.exit({"apply": apply, "first-boot-open": first_boot_open}[sys.argv[1]]())
