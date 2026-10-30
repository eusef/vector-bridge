"""Browser level tuner: live meters and mixer levels from a phone at the bench.

Runs as its own process and reaches the bridge service over the existing UDP control
channel, so nothing here touches the realtime audio path. It serves one page and proxies
`status` and `set_gain`; the mixer listing is read-only and comes from `amixer`.

The point of the page is the distinction that costs the most bench time: whether a peak
that will not move is a digital level set too high, or an analog input already clipping
the ADC. Sweep a gain and watch. A peak that does not follow is an analog problem and no
slider here will fix it.
"""

from __future__ import annotations

import json
import math
import re
import signal
import socketserver
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import admin as admin_mod
from . import (
    admin_page,
    apsetup,
    hamlib,
    manage,
    portal,
    readiness,
    rigproxy,
    sshswitch,
    support,
    txcap,
    wificountry,
    wifijoin,
    wizard,
)
from . import alerts as alerts_mod
from . import apps as apps_mod
from . import battery as battery_mod
from . import config as configmod
from . import ladder as ladder_mod
from .alerts_page import ALERTS
from .cat import CatError, RigctldClient, RigctldSupervisor
from .home_page import HOME
from .manage import control_request
from .tx import PttController

FULL_SCALE = 32767
DBFS_FLOOR = -90.0
# A healthy receive peak, in int16 counts (DOCS/kx2-settings.md "Redo RX calibration").
TARGET_PEAK_LOW = 8000
TARGET_PEAK_HIGH = 16000

_CONTROL_RE = re.compile(r"^Simple mixer control '(.+)',(\d+)")
_LEVEL_RE = re.compile(r"\[(\d+)%\](?:\s*\[(-?[\d.]+)dB\])?")
_CARD_RE = re.compile(r"alsa:hw:(\d+)")


CONTROL_TIMEOUT_S = 0.25
SAVE_TIMEOUT_S = 1.0  # these save to the card before replying: two sudo remounts, about 0.3 s on the Pi;
# kept under a third of the 3 s PTT watchdog, since the drive check sets gain while keyed
SAVING_CMDS = ("set_gain", "set_tx_audio_cap", "select_audio", "set_rigctl_access")


def control_timeout(req: dict) -> float:
    """How long to wait for the audio service's reply to one control request."""
    return SAVE_TIMEOUT_S if req.get("cmd") in SAVING_CMDS else CONTROL_TIMEOUT_S


def vox_aware_ptt(cat, is_vox):
    """The set_ptt and get_ptt the one PttController uses.

    On VOX the transmit audio keys the radio and there may be no rigctld, so a key sends
    nothing. Unkeys and reads still go to rigctld: the rig may have been keyed over CAT
    before the switch to VOX, and the watchdog must still see that. On VOX a refused
    connection is not an error, because rigctld is only stopped once nothing is keyed. It
    cannot tell a rigctld that died while keying over CAT, so that rig may stay keyed. A
    rigctld that answers badly or times out still fails, so the unkey is retried.
    """

    def no_rigctld(e: CatError) -> bool:
        return is_vox() and isinstance(e.__cause__, ConnectionRefusedError)

    def set_ptt(on: bool) -> None:
        if on and is_vox():
            return
        try:
            cat.set_ptt(on)
        except CatError as e:
            if not no_rigctld(e):
                raise

    def get_ptt() -> bool:
        try:
            return cat.get_ptt()
        except CatError as e:
            if no_rigctld(e):
                return False
            raise

    return set_ptt, get_ptt


def dbfs(peak: int) -> float:
    """An int16 peak count as dBFS, floored so silence plots instead of going infinite."""
    if peak <= 0:
        return DBFS_FLOOR
    return max(DBFS_FLOOR, round(20 * math.log10(min(peak, FULL_SCALE) / FULL_SCALE), 1))


def card_index(alsa_device: str) -> int | None:
    """Card number out of the telemetry device string, e.g. 'alsa:hw:0 KT USB Audio ...'."""
    m = _CARD_RE.search(alsa_device or "")
    return int(m.group(1)) if m else None


def parse_mixers(text: str) -> list[dict]:
    """`amixer -c N` output as a list of controls, keeping the card's own dB reading.

    The dB matters more than the percent: it is what says how much headroom a control
    actually has, and a control that tops out at 0 dB offers attenuation only.
    """
    controls: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = _CONTROL_RE.match(line)
        if m:
            cur = {"name": m.group(1), "index": int(m.group(2)),
                   "direction": None, "percent": None, "db": None}
            controls.append(cur)
            continue
        if cur is None:
            continue
        stripped = line.strip()
        if stripped.startswith("Capabilities:"):
            caps = stripped.split(":", 1)[1]
            if "cvolume" in caps:
                cur["direction"] = "capture"
            elif "pvolume" in caps:
                cur["direction"] = "playback"
        lm = _LEVEL_RE.search(line)
        if lm and cur["percent"] is None:
            cur["percent"] = int(lm.group(1))
            if lm.group(2) is not None:
                cur["db"] = float(lm.group(2))
    return controls


def device_mode() -> str | None:
    """`field` (overlay root: safe to pull power) or `maintenance` (writable root), as vb-maintenance
    decides it; None where findmnt does not exist (not a Pi), so the page shows nothing."""
    try:
        return "field" if apsetup.overlay_on() else "maintenance"
    except OSError:
        return None


def read_mixers(card: int) -> list[dict]:
    """Every control on the card. Read-only: the service owns what actually gets set."""
    try:
        out = subprocess.run(["amixer", "-c", str(card)], check=False,
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_mixers(out.stdout) if out.returncode == 0 else []


class TunerApp:
    """Route handling with no sockets in it, so tests drive it directly."""

    def __init__(self, control_fn, mixer_fn=read_mixers, monitor=None, wiz=None, ladder=None, alert_config=None,
                 battery_fn=battery_mod.read, mode_fn=device_mode, proxy=None, rigctl_access="off",
                 config_problem_fn=lambda: None, support_parts=(), device_id_fn=manage.device_id) -> None:
        self.control_fn = control_fn
        self.device_id = device_id_fn()  # apps bind their login token to it (roadmap 1.8)
        self.mixer_fn = mixer_fn
        # Readiness runs in the background when started by cmd_tuner; without a running
        # monitor each request polls once, so the home screen is never empty.
        self.monitor = monitor or readiness.Monitor(readiness.make_gather(control_fn))
        self.monitor_running = monitor is not None
        self.wizard = wiz or wizard.Wizard(wizard.Probes(control_fn=control_fn))
        self.alert_config = alert_config or alerts_mod.AlertConfig()
        self.ladder = ladder  # None when there is no rig control: transmit then stays locked
        self.battery_fn = battery_fn
        self.mode_fn = mode_fn
        self.proxy = proxy  # the rigctl safety proxy on 4532; None when it is not running
        self.rigctl_access = rigctl_access
        self.config_problem_fn = config_problem_fn  # why config.toml cannot be read; None when it reads
        self.support_parts = list(support_parts)  # (title, fn) the bundle adds: rigctld output, config, journal
        self._rigctl_mu = threading.Lock()
        self.app_store = apps_mod.AppStore()  # what client apps say about themselves (DOCS/app-listing.md)

    def readiness(self) -> tuple[int, dict]:
        if not self.monitor_running:
            self.monitor.poll()
        return 200, self.monitor.report() | {"config_problem": self.config_problem_fn()}

    def support(self) -> str:
        """The support bundle: everything a bug report needs, read without transmitting."""
        return support.bundle([
            ("Uptime", support.uptime),
            ("Audio service status", lambda: self.status()[1]),
            ("Readiness", lambda: self.readiness()[1]),
            ("Transmit checks", lambda: self.ladder.report() if self.ladder else "No rig control: transmit stays locked."),
            ("Setup", self.wizard.report),
            *self.support_parts,
        ])

    def battery(self) -> tuple[int, dict]:
        return 200, battery_mod.assess(self.battery_fn())

    def mode(self) -> tuple[int, dict]:
        return 200, {"ok": True, "mode": self.mode_fn()}

    def status(self) -> tuple[int, dict]:
        t = self.control_fn({"cmd": "status"})
        if t is None:
            return 503, {"ok": False, "error": "service_unreachable", "device_id": self.device_id}
        t["device_id"] = self.device_id
        t["rx_dbfs"] = dbfs(t.get("rx_peak", 0))
        t["tx_dbfs"] = dbfs(t.get("tx_peak", 0))
        t["rx_rms_dbfs"] = dbfs(t.get("rx_rms", 0))
        t["tx_rms_dbfs"] = dbfs(t.get("tx_rms", 0))
        t["target_low_dbfs"] = dbfs(TARGET_PEAK_LOW)
        t["target_high_dbfs"] = dbfs(TARGET_PEAK_HIGH)
        return 200, t

    def _clients(self) -> list[dict] | None:
        """The audio service's live streams with their client strings; None when it does not answer."""
        t = self.control_fn({"cmd": "status"})
        return t.get("clients", []) if t else None

    def apps(self) -> tuple[int, dict]:
        clients = self._clients()
        if clients is None:
            return 503, {"ok": False, "error": "service_unreachable"}
        return 200, {"ok": True, "apps": self.app_store.rows(clients)}

    def describe_app(self, ip: str, body: dict) -> tuple[int, dict]:
        """POST /api/apps from `ip`: store it, and say whether a live stream from there carries it."""
        code, reply = self.app_store.put(ip, body)
        if code == 200:
            reply["listed"] = apps_mod.listed(self._clients() or [], ip, body["app_id"])
        return code, reply

    def set_gain(self, body: dict) -> tuple[int, dict]:
        req: dict = {"cmd": "set_gain"}
        for key in ("tx", "rx"):
            value = body.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
                return 400, {"ok": False, "error": f"bad_{key}"}
            req[key] = int(value)
        if "tx" not in req and "rx" not in req:
            return 400, {"ok": False, "error": "no_gain_given"}
        reply = self.control_fn(req)
        if reply is None:
            return 503, {"ok": False, "error": "service_unreachable"}
        return (200 if reply.get("ok") else 400), reply

    def set_tx_audio_cap(self, body: dict) -> tuple[int, dict]:
        seconds = body.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 0 <= seconds <= txcap.MAX_CAP_S:
            return 400, {"ok": False, "error": "bad_seconds"}
        reply = self.control_fn({"cmd": "set_tx_audio_cap", "seconds": seconds})
        if reply is None:
            return 503, {"ok": False, "error": "service_unreachable"}
        if reply.get("ok"):
            self._set_key_cap(seconds)
        return (200 if reply.get("ok") else 400), reply

    def _set_key_cap(self, seconds: int) -> None:
        # One Transmit Time Limit: the audio service mutes at it, and the 4532 proxy (in this process) cuts a key at it.
        if self.proxy is not None:
            self.proxy.max_key_s = float(seconds)

    def rigctl(self) -> tuple[int, dict]:
        if self.proxy is None:
            return 503, {"ok": False, "error": "no_rigctl_proxy"}
        # vb_auth: this proxy takes the "\vb_auth <app token>" login (roadmap 1.10). An older bridge passes that
        # line to rigctld, which runs the token's letters as commands, so an app logs in only when this is true.
        # login_required: every peer but loopback must log in (ROADMAP 4.12); a 1.10 bridge has vb_auth and no
        # login_required, and lets Wi-Fi clients in without one.
        return 200, {"ok": True, "access": self.rigctl_access, "port": self.proxy.port, "listening": self.proxy.listening,
                     "vb_auth": True, "login_required": True}

    def set_rigctl(self, body: dict) -> tuple[int, dict]:
        """Saved through the audio service (it owns the config file), then the 4532 listeners move at once."""
        access = body.get("access")
        if access not in configmod.RIGCTL_ACCESS:
            return 400, {"ok": False, "error": "bad_access"}
        if self.proxy is None:
            return 503, {"ok": False, "error": "no_rigctl_proxy"}
        with self._rigctl_mu:
            reply = self.control_fn({"cmd": "set_rigctl_access", "access": access})
            if reply is None:
                return 503, {"ok": False, "error": "service_unreachable"}
            if not reply.get("ok"):
                return 400, reply
            failed = self.proxy.rebind(rigproxy.binds_for(access))
            self.rigctl_access = access
        code, out = self.rigctl()
        if failed:
            out["failed"] = failed
        return code, out

    def _transmitting(self) -> bool:
        ptt = getattr(self.ladder, "ptt", None)
        return ptt is not None and (ptt.keyed or ptt.lock.owner is not None)

    def wizard_action(self, body: dict) -> tuple[int, dict]:
        """Renaming or deleting the kit in use changes the rig's identity, which re-locks transmit
        and unkeys whoever holds it: refused while the radio is transmitting."""
        if body.get("action") in ("rename_kit", "delete_kit") and body.get("name") == self.wizard.state.kit \
                and self.wizard.state.kit and self._transmitting():
            return 409, {"ok": False, "error": "The radio is transmitting with this kit. Try again once it is back in receive."}
        return wizard.handle(self.wizard, body)

    def reset(self, body: dict) -> tuple[int, dict]:
        """Factory reset. "radio": the wizard, every kit, the radios picked from Hamlib's list and the
        bypass record; rigctld stops. "all": that, plus every setting the page can change back to its
        default, the alert choices and the saved fault bundles. Wi-Fi is never touched."""
        scope = body.get("scope")
        if scope not in ("radio", "all"):
            return 400, {"ok": False, "error": "bad_scope"}
        if body.get("confirm") != "RESET":
            return 400, {"ok": False, "error": "Type RESET to confirm."}
        if self._transmitting():
            return 409, {"ok": False, "error": "The radio is transmitting. Reset once it is back in receive."}
        try:
            self.wizard.reset()  # the rig identity changes, so transmit re-locks
        except wizard.WizardError as e:
            return 500, {"ok": False, "error": str(e)}
        problems = []
        if self.ladder is not None and not self.ladder.clear_record():
            problems.append("the transmit bypass record could not be removed")
        if scope == "all":
            d = configmod.Config()  # config.toml is the audio service's: set back through it, as the page does
            for req, what in (({"cmd": "set_gain", "tx": d.tx_gain, "rx": d.rx_gain}, "the audio levels"),
                              ({"cmd": "set_tx_audio_cap", "seconds": d.tx_audio_cap_s}, "the transmit audio limit"),
                              ({"cmd": "select_audio", "vendor": "", "product": "", "usb_path": ""}, "the sound card choice")):
                r = self.control_fn(req)
                if not r or not r.get("ok"):
                    problems.append(f"{what} could not be reset")
                elif req["cmd"] == "set_tx_audio_cap":
                    self._set_key_cap(d.tx_audio_cap_s)
            if self.proxy is not None:
                code, out = self.set_rigctl({"access": d.rigctl_access})
                if code != 200:
                    problems.append("rig control for apps could not be reset")
                elif out.get("failed"):
                    problems.append(f"rig control for apps is not listening on {'; '.join(out['failed'])}")
            self.alert_config.reset()
            if self.alert_config.save_error:
                problems.append(self.alert_config.save_error)
            if not support.clear_faults():
                problems.append("the saved fault bundles could not be removed")
        return 200, {"ok": not problems, "scope": scope, "problems": problems, "wizard": self.wizard.report()}

    def mixers(self) -> tuple[int, dict]:
        t = self.control_fn({"cmd": "status"}) or {}
        idx = card_index(t.get("alsa_device", ""))
        if idx is None:
            return 200, {"ok": True, "card": None, "controls": []}
        return 200, {"ok": True, "card": idx, "controls": self.mixer_fn(idx)}


class _Handler(BaseHTTPRequestHandler):
    app: TunerApp
    admin: admin_mod.Admin
    docs_dir: Path = portal.DOCS_DIR
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args) -> None:
        """Silence per-request logging; at 10 Hz polling it would bury the journal."""

    def _send(self, code: int, payload, ctype: str = "application/json", cookie: str | None = None) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        if ctype.startswith("text/html"):
            body = admin_page.inject(body)  # every page can then ask for the PIN when a save is refused
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        # Captive portal first: probes and foreign hostnames go to the home screen.
        target = portal.redirect_for(self.path, self.headers.get("Host"))
        if target is not None:
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        elif self.path in ("/wifi", "/wifi/"):
            # The home page: first-boot state while the access point is open, else it opens the Wi-Fi sheet.
            self._send(200, HOME.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/wifi":
            self._send(200, apsetup.report())
        elif self.path == "/api/support" and not apsetup.unsecured():  # logs stay off an open access point
            body = self.app.support().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="vector-bridge-support.txt"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/join" and not apsetup.unsecured():
            self._send(200, wifijoin.report())
        elif self.path == "/api/ssh" and not apsetup.unsecured():
            self._send(200, sshswitch.report())
        elif self.path == "/wifi-qr.svg":
            svg = apsetup.qr_svg()
            if svg is None:
                self._send(404, {"ok": False, "error": "not_found"})
            else:
                self._send(200, svg.encode(), "image/svg+xml")
        elif apsetup.unsecured():
            # Open access point (first boot of a burned card): nothing but the Wi-Fi page.
            self.send_response(302)
            self.send_header("Location", "/wifi")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        elif self.path in ("/", "/index.html"):
            self._send(200, HOME.encode(), "text/html; charset=utf-8")
        elif self.path in ("/tuner", "/tuner/"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path in ("/setup", "/setup/"):
            self._redirect("/#setup")  # setup is a sheet on the home page now; old links still land there
        elif self.path == "/api/wizard":
            self._send(200, self.app.wizard.report())
        elif self.path == "/api/hamlib/models":
            self._send(200, {"ok": True, "models": hamlib.models()})  # the setup sheet's "any radio" search
        elif urlsplit(self.path).path == "/api/hamlib/conf":  # ?model=N: the expert settings that radio takes
            q = parse_qs(urlsplit(self.path).query).get("model", [""])[0]
            if not (q.isascii() and q.isdigit()) or not 0 < int(q) < 100_000:
                self._send(400, {"ok": False, "error": "bad_model"})
            else:
                self._send(200, {"ok": True, "model": int(q), "params": hamlib.conf_params(int(q)),
                                 "owned": sorted(hamlib.OWNED_CONF)})
        elif self.path in ("/transmit", "/transmit/"):
            self._redirect("/#transmit")
        elif self.path == "/api/ladder":
            lad = self.app.ladder
            self._send(*((200, lad.report()) if lad else (503, {"ok": False, "error": "no rig control"})))
        elif self.path in ("/alerts", "/alerts/"):
            self._send(200, ALERTS.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/alerts":
            self._send(200, self.app.alert_config.report())
        elif self.path == "/manifest.json":
            self._send(200, json.dumps(alerts_mod.MANIFEST).encode(), "application/manifest+json")
        elif self.path == "/icon.png":
            self._send(200, alerts_mod.icon_png(), "image/png")
        elif self.path == "/api/readiness":
            self._send(*self.app.readiness())
        elif self.path == "/api/battery":
            self._send(*self.app.battery())
        elif self.path == "/api/mode":
            self._send(*self.app.mode())
        elif self.path in ("/docs", "/docs/"):
            self._send(200, portal.render_index(self.docs_dir).encode(), "text/html; charset=utf-8")
        elif self.path.startswith("/docs/"):
            page = portal.render_doc(self.path[len("/docs/"):], self.docs_dir)
            if page is None:
                self._send(404, {"ok": False, "error": "not_found"})
            else:
                self._send(200, page.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._send(*self.app.status())
        elif self.path == "/api/apps":
            self._send(*self.app.apps())
        elif self.path.startswith("/api/apps/icon/") and self.path.endswith(".png"):
            png = self.app.app_store.icon(self.path[len("/api/apps/icon/"):-len(".png")])
            if png is None:
                self._send(404, {"ok": False, "error": "not_found"})
            else:  # named by its hash, so it never changes: cache it for good
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(png)
        elif self.path == "/api/mixers":
            self._send(*self.app.mixers())
        elif self.path == "/api/rigctl":
            self._send(*self.app.rigctl())
        elif self.path == "/api/pin":
            self._send(200, self.admin.report(self.headers))
        elif self.path == "/api/tokens":
            if self.admin.authorized(self.headers):
                self._send(200, self.admin.app_tokens())
            else:
                self._send(401, {"ok": False, "error": "pin_required", "pin_set": self.admin.pin_set()})
        else:
            self._send(404, {"ok": False, "error": "not_found"})

    def _cross_site(self) -> bool:
        """A POST not from our own pages: foreign Host (DNS rebinding) or foreign Origin (CSRF)."""
        host, origin = self.headers.get("Host"), self.headers.get("Origin")
        return (not portal.is_ours(host)
                or (origin is not None and portal.host_name(urlsplit(origin).netloc) != portal.host_name(host))
                or self.headers.get("Sec-Fetch-Site", "").strip().lower() == "cross-site")

    def do_POST(self) -> None:
        if self._cross_site():
            self.close_connection = True  # the unread body must not become the next request
            self._send(403, {"ok": False, "error": "forbidden_origin"})
            return
        if self.path not in ("/api/gain", "/api/wizard", "/api/ladder", "/api/alerts", "/api/wifi", "/api/join",
                             "/api/tx_audio_cap", "/api/rigctl", "/api/reset", "/api/apps", "/api/login", "/api/pin",
                             "/api/tokens", "/api/ssh", "/api/wifi_country"):
            self.close_connection = True  # the unread body must not become the next request
            self._send(404, {"ok": False, "error": "not_found"})
            return
        if self.path != "/api/wifi" and apsetup.unsecured():
            self.close_connection = True  # apps re-POST every 60 s on a kept-alive connection (DOCS/app-listing.md)
            self._send(403, {"ok": False, "error": "wifi_open", "fix": "Set the Wi-Fi passphrase at /wifi first."})
            return
        # Admin PIN (R-D3) on every POST. Exempt: logging in, creating the first PIN (set_pin
        # checks that itself), and the first-boot Wi-Fi form, which runs before any PIN can exist.
        authorized = self.admin.authorized(self.headers)
        if not authorized and self.path not in ("/api/login", "/api/pin") and not (
                self.path == "/api/wifi" and apsetup.unsecured()):
            self.close_connection = True  # the unread body must not become the next request
            self._send(401, {"ok": False, "error": "pin_required", "pin_set": self.admin.pin_set()})
            return
        # SSH on hands out the login account (sudo): a PIN session only, never a long-lived app token
        # (apps send theirs in clear on port 4532 and keep it in their settings).
        if self.path == "/api/ssh" and self.admin.app_token_valid(admin_mod.bearer(self.headers)):
            self.close_connection = True  # the unread body must not become the next request
            self._send(403, {"ok": False, "error": "pin_session_required"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > apps_mod.MAX_BODY:
            self.close_connection = True  # the unread body must not become the next request
            self._send(*((413, {"ok": False, "error": "too_large"}) if length > 0 else (400, {"ok": False, "error": "bad_json"})))
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            self._send(400, {"ok": False, "error": "bad_json"})
            return
        if not isinstance(body, dict):
            self._send(400, {"ok": False, "error": "bad_json"})
            return
        if self.path in ("/api/login", "/api/pin"):
            code, out = self.admin.login(body) if self.path == "/api/login" else self.admin.set_pin(body, authorized)
            if code == 200:
                out["device_id"] = self.app.device_id
            # An app token stays out of the browser's cookie jar: it never idles out.
            browser = code == 200 and "app" not in out
            self._send(code, out, cookie=admin_mod.cookie_header(out["token"]) if browser else None)
            return
        if self.path == "/api/tokens":
            self._send(*self.admin.revoke(body))
            return
        if self.path == "/api/wifi":
            self._send(*apsetup.handle(body))
            return
        if self.path == "/api/wifi_country":
            self._send(*wificountry.handle(body))
            return
        if self.path == "/api/join":
            self._send(*wifijoin.handle(body))
            return
        if self.path == "/api/ssh":
            self._send(*sshswitch.handle(body))
            return
        if self.path == "/api/wizard":
            self._send(*self.app.wizard_action(body))
            return
        if self.path == "/api/alerts":
            self.app.alert_config.update(body.get("events"))
            self._send(200, self.app.alert_config.report())
            return
        if self.path == "/api/ladder":
            lad = self.app.ladder
            self._send(*(ladder_mod.handle(lad, body) if lad else (503, {"ok": False, "error": "no rig control"})))
            return
        if self.path == "/api/tx_audio_cap":
            self._send(*self.app.set_tx_audio_cap(body))
            return
        if self.path == "/api/rigctl":
            self._send(*self.app.set_rigctl(body))
            return
        if self.path == "/api/reset":
            self._send(*self.app.reset(body))
            return
        if self.path == "/api/apps":
            self._send(*self.app.describe_app(self.client_address[0], body))
            return
        self._send(*self.app.set_gain(body))


class _TunerServer(ThreadingHTTPServer):
    """HTTPServer without the reverse-DNS lookup it does to fill in `server_name`.

    In field AP mode there is no resolver, and that lookup blocks startup for the full
    DNS timeout before the first page is ever served.
    """

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port


def serve(app: TunerApp, bind: str = "0.0.0.0", port: int = 8080,
          docs_dir: Path = portal.DOCS_DIR, admin: admin_mod.Admin | None = None) -> _TunerServer:
    handler = type("_BoundHandler", (_Handler,), {"app": app, "docs_dir": docs_dir,
                                                  "admin": admin or admin_mod.Admin()})
    return _TunerServer((bind, port), handler)


PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>VectorBridge Tuner</title>
<style>
  :root {
    --bg: #12151a; --panel: #1b2029; --line: #2b3240; --text: #e7ecf3;
    --dim: #8b97a8; --ok: #3ddc84; --warn: #ffc857; --clip: #ff4d4f; --accent: #5ac8fa;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    padding: max(12px, env(safe-area-inset-top)) 14px max(16px, env(safe-area-inset-bottom));
  }
  h1 { font-size: 17px; margin: 0 0 2px; letter-spacing: .2px; }
  .sub { color: var(--dim); font-size: 12px; margin-bottom: 14px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
          padding: 14px; margin-bottom: 12px; }
  .row { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; }
  .label { font-weight: 600; font-size: 14px; }
  .val { font-variant-numeric: tabular-nums; font-size: 22px; font-weight: 600; }
  .unit { color: var(--dim); font-size: 12px; font-weight: 400; }
  .meter { position: relative; height: 26px; margin: 10px 0 4px; background: #0d1015;
           border-radius: 6px; overflow: hidden; border: 1px solid var(--line); }
  .zone { position: absolute; top: 0; bottom: 0; background: rgba(61,220,132,.16);
          border-left: 1px solid rgba(61,220,132,.5); border-right: 1px solid rgba(61,220,132,.5); }
  .rms { position: absolute; top: 0; bottom: 0; left: 0; background: #2b3a4d; }
  .bar { position: absolute; top: 0; bottom: 0; left: 0; background: var(--accent);
         mix-blend-mode: screen; }
  .hold { position: absolute; top: 0; bottom: 0; width: 2px; background: #fff; opacity: .85; }
  .scale { display: flex; justify-content: space-between; color: var(--dim);
           font-size: 10px; font-variant-numeric: tabular-nums; }
  .bar.ok { background: var(--ok); }
  .bar.hot { background: var(--warn); }
  .bar.clip { background: var(--clip); mix-blend-mode: normal; }
  .advice { margin-top: 8px; padding: 8px 10px; border-radius: 8px; font-size: 13px;
            background: #23303f; color: var(--dim); }
  .advice.ok { color: var(--ok); }
  .advice.hot { background: rgba(255,200,87,.15); color: var(--warn); }
  .advice.clip { background: var(--clip); color: #fff; font-weight: 600; }
  input[type=range] { width: 100%; margin: 14px 0 2px; accent-color: var(--accent); height: 28px; }
  .hint { color: var(--dim); font-size: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid var(--line); }
  th { color: var(--dim); font-weight: 500; font-size: 11px; text-transform: uppercase; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .warnbox { border-color: var(--warn); }
  .warnbox .label { color: var(--warn); }
  #conn { font-size: 12px; color: var(--dim); }
  #conn.bad { color: var(--clip); }
  button { background: #23303f; color: var(--text); border: 1px solid var(--line);
           border-radius: 8px; padding: 8px 12px; font-size: 13px; }
</style>

<h1>VectorBridge Tuner</h1>
<div class="sub" id="conn">connecting… · <a href="/" style="color:var(--accent)">Home</a> · <a href="/docs/" style="color:var(--accent)">Docs</a></div>

<div class="card" id="rxcard">
  <div class="row">
    <span class="label">RX &middot; radio into Pi</span>
    <span><span class="val" id="rxdb">–</span> <span class="unit">dBFS</span></span>
  </div>
  <div class="meter">
    <div class="zone" id="rxzone"></div>
    <div class="rms" id="rxrms"></div>
    <div class="bar" id="rxbar"></div>
    <div class="hold" id="rxhold"></div>
  </div>
  <div class="scale"><span>-60</span><span>-40</span><span>-20</span><span>-12</span><span>0</span></div>
  <div class="advice" id="rxadvice">waiting for audio…</div>
  <div class="row" style="margin-top:8px">
    <span class="hint">peak <b id="rxpeak">0</b> &middot; rms <b id="rxrmsv">0</b></span>
    <span class="hint">clips <b id="rxclips">0</b></span>
  </div>
  <input type="range" min="0" max="100" id="rxgain">
  <div class="row"><span class="hint">capture level</span><span class="hint" id="rxgainv">–</span></div>
  <div class="hint" id="rxfixed" hidden>This radio's sound card has no capture level control, so this slider cannot change anything.
    Set the level on the radio: look in its menu for a USB audio, AF output or data output level. On a headphone or
    line-out cable, use the radio's volume or line-out level.</div>
</div>

<div class="card">
  <div class="row">
    <span class="label">TX &middot; Pi into radio</span>
    <span><span class="val" id="txdb">–</span> <span class="unit">dBFS</span></span>
  </div>
  <div class="meter">
    <div class="rms" id="txrms"></div>
    <div class="bar" id="txbar"></div>
    <div class="hold" id="txhold"></div>
  </div>
  <div class="scale"><span>-60</span><span>-40</span><span>-20</span><span>-12</span><span>0</span></div>
  <div class="advice" id="txadvice">no TX audio (idle)</div>
  <input type="range" min="0" max="100" id="txgain">
  <div class="row"><span class="hint">playback level</span><span class="hint" id="txgainv">–</span></div>
</div>

<div class="card" id="diag">
  <div class="row"><span class="label">Mixer controls</span><button id="refresh">refresh</button></div>
  <table id="mixers"><tbody></tbody></table>
  <div class="hint" id="mixnote" style="margin-top:8px"></div>
</div>

<div class="card">
  <div class="hint" id="device">–</div>
</div>

<script>
const FLOOR = -60;                      // meter scale bottom, dBFS
const $ = id => document.getElementById(id);
const pos = db => Math.max(0, Math.min(100, (Math.max(db, FLOOR) - FLOOR) / -FLOOR * 100));

let hold = {rx: FLOOR, tx: FLOOR}, holdAt = {rx: 0, tx: 0};
let lastClip = null, clipSince = -1e12, dragging = null, sendTimer = null;
const CLIP_RECENT_MS = 3000;      // the meter stays red this long after the last clip
const JUDGE_MS = 3000;                  // level advice looks at the loudest peak in this window
const SILENT_DB = -45;                  // below this there is effectively no audio
const recent = {rx: [], tx: []};

function windowMax(kind, peakDb) {
  const now = Date.now(), list = recent[kind];
  list.push([now, peakDb]);
  while (list.length && now - list[0][0] > JUDGE_MS) list.shift();
  return Math.max(...list.map(e => e[1]));
}

// What is wrong and which knob fixes it, for someone who has not read the docs. RX
// clipping happens at the adapter's analog input, before the capture slider, so only the
// radio's volume can fix it.
function advise(kind, clipping, peakDb, lo, hi) {
  const loud = windowMax(kind, peakDb);
  if (kind === 'rx') {
    if (clipping) return ['clip', "Too loud, the input is clipping. Turn the radio's audio output down (volume, or its USB or data output level)."];
    if (loud <= SILENT_DB) return ['', 'No audio. Check the radio is on, in receive, and its headphone or data jack is cabled to the adapter.'];
    if (loud > hi) return ['hot', "A little hot. Turn the radio's audio output down a notch."];
    if (loud < lo) return ['hot', "Too quiet. Turn the radio's audio output up."];
    return ['ok', "Level good. Leave the radio's audio output where it is."];
  }
  if (clipping) return ['clip', 'Too loud, the audio into the radio is clipping. Turn the playback level or the Mac output down.'];
  if (loud <= SILENT_DB) return ['', 'No TX audio (idle).'];
  if (loud > hi) return ['hot', 'A little hot. Lower the playback level a notch and check the radio ALC.'];
  if (loud < lo) return ['hot', 'Too quiet. Raise the playback level or the Mac output.'];
  return ['ok', 'Level good. Confirm with the radio ALC meter.'];
}

function paint(kind, peakDb, rmsDb) {
  $(kind + 'bar').style.width = pos(peakDb) + '%';
  $(kind + 'rms').style.width = pos(rmsDb) + '%';
  const now = Date.now();
  if (peakDb >= hold[kind] || now - holdAt[kind] > 1500) { hold[kind] = peakDb; holdAt[kind] = now; }
  const marker = $(kind + 'hold');
  marker.style.left = pos(hold[kind]) + '%';
  marker.style.opacity = hold[kind] <= FLOOR ? 0 : .85;   // no marker pinned at the floor
  $(kind + 'db').textContent = peakDb <= FLOOR ? '–' : peakDb.toFixed(1);
}

async function tick() {
  let t;
  try {
    const r = await fetch('/api/status', {cache: 'no-store'});
    t = await r.json();
    if (!r.ok) throw new Error(t.error || r.status);
  } catch (e) {
    $('conn').textContent = 'service unreachable — is vector-bridge running?';
    $('conn').className = 'bad';
    return;
  }
  $('conn').textContent = 'peer ' + (t.peer || 'none') + ' · up ' + t.uptime_s + 's';
  $('conn').className = '';

  paint('rx', t.rx_dbfs, t.rx_rms_dbfs);
  paint('tx', t.tx_dbfs, t.tx_rms_dbfs);
  $('rxpeak').textContent = t.rx_peak;
  $('rxrmsv').textContent = t.rx_rms;

  const lo = pos(t.target_low_dbfs), hi = pos(t.target_high_dbfs);
  $('rxzone').style.left = lo + '%';
  $('rxzone').style.width = (hi - lo) + '%';

  // Clipping is live, not latched: the meter goes red while the counter climbs and stays
  // red a moment after, then the advice falls back to whatever the level says.
  if (lastClip !== null && t.clip_count > lastClip) clipSince = Date.now();
  lastClip = t.clip_count;
  const clipping = Date.now() - clipSince < CLIP_RECENT_MS;
  $('rxclips').textContent = t.clip_count;
  for (const kind of ['rx', 'tx']) {
    const isClip = kind === 'rx' && clipping;   // clip_count is the capture side only
    const [state, text] = advise(kind, isClip, t[kind + '_dbfs'], t.target_low_dbfs, t.target_high_dbfs);
    $(kind + 'advice').textContent = text;
    $(kind + 'advice').className = 'advice ' + state;
    $(kind + 'bar').className = 'bar ' + state;
  }
  $('rxcard').className = 'card' + (clipping ? ' warnbox' : '');

  for (const kind of ['rx', 'tx']) {
    const level = t[kind + '_gain'];
    if (dragging === kind || level == null) continue;
    $(kind + 'gain').value = level;
    $(kind + 'gainv').textContent = level + '%';
  }
  const rxFixed = !!t.rx_gain_fixed;  // no capture control on the card: the radio sets the level
  $('rxgain').disabled = rxFixed;
  $('rxfixed').hidden = !rxFixed;
  if (rxFixed) $('rxgainv').textContent = 'set on the radio';
  $('device').textContent = t.alsa_device || 'no device';
}

async function sendGain(kind, value) {
  const body = {}; body[kind] = Number(value);
  try {
    const r = await fetch('/api/gain', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    const j = await r.json();
    if (j.ok) { $('rxgain').value = j.rx; $('txgain').value = j.tx; }
  } catch (e) { /* the next tick reports the real state */ }
  loadMixers();
}

function wire(kind) {
  const el = $(kind + 'gain');
  el.addEventListener('input', () => {
    dragging = kind;
    $(kind + 'gainv').textContent = el.value + '%';
    clearTimeout(sendTimer);
    sendTimer = setTimeout(() => sendGain(kind, el.value), 150);
  });
  el.addEventListener('change', () => { setTimeout(() => { dragging = null; }, 400); });
}

async function loadMixers() {
  let j;
  try { j = await (await fetch('/api/mixers', {cache: 'no-store'})).json(); } catch (e) { return; }
  const body = $('mixers').querySelector('tbody');
  body.innerHTML = '<tr><th>control</th><th>dir</th><th class="num">%</th><th class="num">dB</th></tr>';
  for (const c of j.controls) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td>' + c.name + '</td><td>' + (c.direction || '–') + '</td>' +
      '<td class="num">' + (c.percent ?? '–') + '</td>' +
      '<td class="num">' + (c.db === null ? '–' : c.db.toFixed(2)) + '</td>';
    body.appendChild(tr);
  }
  const caps = j.controls.filter(c => c.direction === 'capture' && c.db !== null);
  const attenuateOnly = caps.length && caps.every(c => c.db <= 0);
  $('mixnote').textContent = j.card === null
    ? 'No ALSA card: the service is on the fake backend, so there is nothing to tune here.'
    : attenuateOnly
      ? 'Capture tops out at 0 dB, so this card attenuates only. A peak pinned at full scale is the analog input overdriving the ADC, and no setting here will recover it.'
      : '';
  $('mixnote').style.color = attenuateOnly ? '#ffc857' : '';
}

$('refresh').addEventListener('click', loadMixers);
wire('rx'); wire('tx');
$('rxgain').addEventListener('input', () => $('rxgainv').textContent = $('rxgain').value + '%');
$('txgain').addEventListener('input', () => $('txgainv').textContent = $('txgain').value + '%');
tick(); loadMixers();
setInterval(tick, 100);
</script>
"""


def cmd_tuner(args) -> int:
    """`vector-bridge tuner`: serve the page until Ctrl-C."""
    import logging  # local: only startup problems and the shutdown path log

    def control(req):
        return control_request(req, host=args.host, port=args.port, timeout=control_timeout(req), config_path=args.config)

    # rigctld is a separate local process; the monitor only ever reads from it.
    startup_cfg = configmod.load(args.config)
    access = startup_cfg.rigctl_access
    key_cap = startup_cfg.tx_audio_cap_s  # the Transmit Time Limit, shared with the audio service's TX cap
    if isinstance(key_cap, bool) or not isinstance(key_cap, int) or not 0 <= key_cap <= txcap.MAX_CAP_S:
        key_cap = txcap.DEFAULT_CAP_S  # the service falls back the same way
    if access not in configmod.RIGCTL_ACCESS:
        logging.getLogger(__name__).warning("rigctl_access %r is not one of %s; port 4532 on loopback only",
                                            access, ", ".join(configmod.RIGCTL_ACCESS))
        access = "off"
    rig = RigctldSupervisor()  # loopback only: desktop apps come in through the proxy below
    rig.start()  # keep-alive: a rigctld that dies is relaunched for the chosen profile
    wiz = wizard.Wizard(wizard.Probes(control_fn=control, cat=RigctldClient(timeout=0.5), start_cat=rig.ensure,
                                      cat_log=rig.failure_log, stop_cat=rig.release))
    wiz.resume_cat()
    # Every keying goes through this one controller, so the lock and watchdog always apply.
    def vox() -> bool:
        return getattr(wiz.profile(), "ptt_method", "") == "vox"

    set_ptt, get_ptt = vox_aware_ptt(RigctldClient(timeout=1.0), vox)
    ptt = PttController(set_ptt, get_ptt=get_ptt)
    ptt.start()
    # On VOX rigctld goes only once nothing is keyed: a CAT key from before the switch is
    # unkeyed through it first (the ladder drops it on the change), so VOX unkeys find none.
    rig.release_when = lambda: vox() and ptt.lock.owner is None and not ptt.keyed  # the lock is taken before a key starts
    plug = battery_mod.PlugWatch()  # PiSugar 2 has no plugged-in bit: watch the voltage
    # Receive gates transmit: a client key is refused while the readiness receive check is red.
    # `monitor` is bound below, before the web server can take a key.
    lad = ladder_mod.Ladder(RigctldClient(timeout=1.0), ptt, control, wiz.profile,
                            identity_fn=lambda: (wiz.state.profile, wiz.state.interface, wiz.state.ptt_method, wiz.state.kit),
                            rx_ok_fn=lambda: monitor.state_of("rx_audio") != readiness.FAIL,
                            battery_fn=plug.report)
    wiz.probes.ladder = lad.hook
    monitor = readiness.Monitor(readiness.make_gather(control, cat=RigctldClient(timeout=0.5), ladder=lad.hook, vox_fn=vox,
                                                      no_cat_fn=lambda: getattr(wiz.profile(), "rig_model", 1) == 0))
    monitor.start()
    alert_config = alerts_mod.AlertConfig()
    alerter = alerts_mod.Alerter(monitor.report, alert_config)  # LED and beeper; no hardware configured means no-ops
    alerter.start()
    plug.start()
    # Desktop WSJT-X and JS8Call key through the Ladder like everyone else (HANDOFF M4).
    admin = admin_mod.Admin()  # the web server's PIN and app tokens; 4532 takes the app tokens under all
    proxy = rigproxy.RigctlProxy(lad, binds=rigproxy.binds_for(access), upstream_port=rig.port,
                                 max_key_s=float(key_cap), token_ok=admin.app_token_valid)
    try:
        proxy.start()
    except OSError:  # port taken: the web page still matters more than desktop CAT
        logging.getLogger(__name__).exception("rigctl proxy could not listen on port %d", proxy.port)
    app = TunerApp(control, monitor=monitor, wiz=wiz, ladder=lad, alert_config=alert_config, battery_fn=plug.report,
                   proxy=proxy, rigctl_access=access, config_problem_fn=lambda: configmod.problem(args.config),
                   support_parts=[("Hamlib (rigctld) output", lambda: "\n".join(rig.recent_log()) or "(none)"),
                                  ("Config file", lambda: (args.config or configmod.DEFAULT_PATH).read_text()),
                                  ("Bridge journal, this boot", support.journal), ("Kernel log", support.kernel)])
    # The journal is in RAM: keep a copy on the card whenever an alert fires, for after a power cycle.
    monitor.on_alert = lambda checks: support.save_fault(app.support())
    httpd = serve(app, bind=args.bind, port=args.http_port, admin=admin)
    shown = args.bind if args.bind not in ("0.0.0.0", "") else "<pi-address>"
    print(f"tuner on http://{shown}:{args.http_port}/  (Ctrl-C to stop)")
    print("Every POST needs the admin PIN (DOCS/web-api.md); reads are open to anyone who can reach this port.")
    print(f"rigctl proxy ({access}) on {', '.join(proxy.listening) or 'nothing'} port {proxy.port}: "
          "app keys pass the transmit checks and lock.")
    # systemd stops the unit with SIGTERM: take the same path as Ctrl-C so the buzzer, light and PTT are released.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Each step runs even if an earlier one raises, so rigctld is always stopped.
        # unkey before rigctld goes away; the proxy first, so no desktop client re-keys during shutdown
        for step in (httpd.server_close, proxy.stop, plug.stop, alerter.stop, ptt.stop, rig.stop):
            try:
                step()
            except Exception:
                logging.getLogger(__name__).exception("shutdown step %s failed", getattr(step, "__qualname__", step))
    return 0
