"""The Wi-Fi country (R-D7). It decides which 5 GHz channels a USB adapter may use for the
bridge's access point. A fresh image has none, so the bridge stays on 2.4 GHz until the owner
picks one: on the first-boot sheet (sent with POST /api/wifi) or later on the Wi-Fi page
(POST /api/wifi_country, behind the admin PIN).

The web process writes the choice to /persist (SAVED); vb-wifi-radio, already root, reads it,
runs `iw reg set` and moves the access point to 5 GHz when it can (vector_bridge.wifiradio).
The overlay forgets nothing here: the kernel's domain is set again at every boot.

A card with no saved choice keeps a country given on the kernel command line
(`cfg80211.ieee80211_regdom=`), which Raspberry Pi Imager's OS settings write; the bench card has
one. prepare-golden.sh removes both, so an image assumes no country.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from . import persist

SAVED = persist.APP_DIR / "wifi-country.json"
CMDLINE = Path("/proc/cmdline")

# ISO 3166-1 alpha-2. A code wireless-regdb does not know leaves the kernel on its world domain:
# 2.4 GHz only, which is safe.
_ISO_3166 = """
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS
BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE
EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM
HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC
LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA
NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW
SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO
TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
"""
CODES = frozenset(_ISO_3166.split())
_REGDOM = re.compile(r"(?:^|\s)cfg80211\.ieee80211_regdom=(\S+)")


def check(code) -> str:
    """The code, upper case, or "" when it is not a country."""
    code = code.strip().upper() if isinstance(code, str) else ""
    return code if code in CODES else ""


def saved(path: Path | None = None) -> str:
    """The owner's choice, or "". Root reads this too, and the web user owns the directory, so a
    symlink or a FIFO in its place counts as nothing."""
    try:
        fd = os.open(path or SAVED, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as f:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return ""
            body = json.loads(f.read(256))
    except (OSError, ValueError):
        return ""
    return check(body.get("country")) if isinstance(body, dict) else ""


def cmdline(path: Path | None = None) -> str:
    try:
        m = _REGDOM.search((path or CMDLINE).read_text())
    except OSError:
        return ""
    return check(m.group(1)) if m else ""


def current() -> str:
    """The country in force: the saved choice, else the kernel command line's, else ""."""
    return saved() or cmdline()


def save(code: str, path: Path | None = None) -> None:
    persist.save(path or SAVED, json.dumps({"country": code}), mode=0o644)


def handle(body: dict, path: Path | None = None) -> tuple[int, dict]:
    """POST /api/wifi_country from the Wi-Fi page: {"country": "US"}."""
    code = check(body.get("country")) if isinstance(body, dict) else ""
    if not code:
        return 400, {"ok": False, "errors": ["Choose a country from the list."]}
    try:
        save(code, path)
    except OSError as e:
        return 500, {"ok": False, "errors": [f"Could not write to the card: {e}"]}
    return 200, {"ok": True, "country": code}
