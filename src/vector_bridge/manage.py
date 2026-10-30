"""Management subcommands for the Pi: service, status, audio, wifi, ap.

Each returns an exit code, prints one line (or a table) and never raises to the
user. systemctl/nmcli are invoked through sudo when not already root.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import secrets
import socket
import string
import subprocess
import sys
import time
from pathlib import Path

from . import config as configmod
from . import persist
from .audio.alsa import list_cards, select_card

UNIT = "vector-bridge"
AP_CON = "vector-bridge-ap"
WLAN = "wlan0"   # the built-in radio; wifiradio moves the network to a USB adapter when one is plugged in
AP_ADDRESS = "10.42.0.1"
# The 2.4 GHz access point's channel, legal in every country. NetworkManager's "auto" put the built-in
# radio on 13 on 2026-10-23: off the air once the country was set to US, with NetworkManager still "activated".
AP_CHANNEL_2G = "6"
PORTAL_HOST = "vectorbridge"
CONTROL_TIMEOUT_S = 2.0


# ---- helpers ------------------------------------------------------------------


def _sudo(cmd: list[str]) -> list[str]:
    return cmd if os.geteuid() == 0 else ["sudo", "-n", *cmd]


def _run(cmd: list[str], sudo: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    if sudo:
        cmd = _sudo(cmd)
    return subprocess.run(cmd, text=True, capture_output=capture, check=False)


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def control_request(req: dict, host: str = "127.0.0.1", port: int | None = None,
                    timeout: float = CONTROL_TIMEOUT_S, config_path: Path | None = None) -> dict | None:
    """One JSON request to the running service's control port; None on timeout."""
    if port is None:
        port = configmod.load(config_path).control_port
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(json.dumps(req, separators=(",", ":")).encode(), (host, port))
        try:
            data, _ = s.recvfrom(65535)
        except (TimeoutError, OSError):
            return None
    try:
        return json.loads(data.decode())
    except ValueError:
        return None


def format_table(obj: dict) -> str:
    width = max(len(k) for k in obj) if obj else 0
    rows = []
    for k, v in obj.items():
        if isinstance(v, float):
            v = f"{v:.2f}"
        elif v is None:
            v = "-"
        rows.append(f"{k:<{width}}  {v}")
    return "\n".join(rows)


# ---- service ------------------------------------------------------------------


def cmd_service(args) -> int:
    action = args.action
    if action == "logs":
        cmd = ["journalctl", "-u", UNIT, "-n", str(args.lines), "--no-pager"]
        if args.follow:
            cmd.append("-f")
        return subprocess.run(cmd, check=False).returncode
    if action == "status":
        r = _run(["systemctl", "is-active", UNIT])
        state = r.stdout.strip() or r.stderr.strip()
        print(f"{UNIT}: {state}")
        return 0 if state == "active" else 1
    r = _run(["systemctl", action, UNIT], sudo=True)
    if r.returncode != 0:
        return _fail(f"systemctl {action} failed: {r.stderr.strip()}")
    state = _run(["systemctl", "is-active", UNIT]).stdout.strip()
    print(f"{UNIT}: {action} ok ({state})")
    return 0


# ---- status -------------------------------------------------------------------


def cmd_status(args) -> int:
    reply = control_request({"cmd": "status"}, host=args.host, port=args.port, config_path=args.config)
    if reply is None:
        return _fail(f"no reply from the service control port at {args.host} (is vector-bridge running?)")
    if args.json:
        print(json.dumps(reply, sort_keys=True))
    else:
        print(format_table(reply))
    return 0


# ---- audio --------------------------------------------------------------------


def cmd_audio_list(args) -> int:
    cfg = configmod.load(args.config)
    cards = list_cards()
    if not cards:
        return _fail("no ALSA cards found (is /proc/asound present?)")
    chosen = select_card(cards, cfg.usb_vendor_id, cfg.usb_product_id)
    print(f"{'':2} {'card':<4} {'id':<12} {'usb':<10} name")
    for c in cards:
        mark = "*" if chosen is not None and c.index == chosen.index else " "
        print(f"{mark:2} {c.index:<4} {c.id:<12} {c.usb_id:<10} {c.name}")
    if chosen is None:
        print("no USB audio card matches the config; run `vector-bridge audio select <card>`")
        return 1
    return 0


def cmd_audio_select(args) -> int:
    cards = list_cards()
    match = [c for c in cards if str(c.index) == args.card or c.id == args.card]
    if not match:
        return _fail(f"no card {args.card!r}; run `vector-bridge audio list`")
    card = match[0]
    if not card.is_usb:
        return _fail(f"card {card.index} ({card.name}) is not a USB device")
    cfg = configmod.load(args.config)
    cfg.usb_vendor_id, cfg.usb_product_id, cfg.audio_usb_path = card.usb_vendor_id, card.usb_product_id, card.usb_path
    cfg.audio_usb_strict = sum(c.usb_id == card.usb_id for c in cards) > 1
    try:
        path = configmod.save(cfg, args.config)
    except OSError as e:
        return _fail(f"cannot write config: {e}")
    r = _run(["systemctl", "restart", UNIT], sudo=True)
    restarted = "restarted" if r.returncode == 0 else f"restart failed: {r.stderr.strip()}"
    print(f"selected card {card.index} {card.name} usb={card.usb_id} -> {path}; service {restarted}")
    return 0 if r.returncode == 0 else 1


def cmd_audio_gain(args) -> int:
    if args.tx is None and args.rx is None:
        return _fail("give --tx N and/or --rx N (0-100)")
    for v in (args.tx, args.rx):
        if v is not None and not 0 <= v <= 100:
            return _fail("gain must be 0-100")
    req = {"cmd": "set_gain"}
    if args.tx is not None:
        req["tx"] = args.tx
    if args.rx is not None:
        req["rx"] = args.rx
    reply = control_request(req, host=args.host, port=args.port, config_path=args.config)
    if reply is not None and reply.get("ok"):
        print(f"gain applied tx={reply.get('tx')} rx={reply.get('rx')} (persisted by the service)")
        return 0
    # Service not running: persist to config so the next start uses it.
    cfg = configmod.load(args.config)
    if args.tx is not None:
        cfg.tx_gain = args.tx
    if args.rx is not None:
        cfg.rx_gain = args.rx
    try:
        path = configmod.save(cfg, args.config)
    except OSError as e:
        return _fail(f"service not reachable and cannot write config: {e}")
    print(f"service not reachable; saved tx={cfg.tx_gain} rx={cfg.rx_gain} to {path} for next start")
    return 0


def _bar(value: int, width: int = 20, full: int = 32767) -> str:
    n = min(width, round(width * value / full))
    return "#" * n + "." * (width - n)


def cmd_audio_meter(args) -> int:
    """Live RX/TX peak, RMS, clip readout every 250 ms until Ctrl-C (proposal section 15)."""
    print("VectorBridge meter (Ctrl-C to stop). Peak/RMS in int16 counts; clip = cumulative.")
    last_clip = None
    try:
        while True:
            t = control_request({"cmd": "status"}, host=args.host, port=args.port,
                                timeout=0.2, config_path=args.config)
            if t is None:
                line = "no reply from service"
            else:
                clip = t.get("clip_count", 0)
                flag = " CLIP" if last_clip is not None and clip > last_clip else ""
                last_clip = clip
                line = (f"RX peak {t.get('rx_peak', 0):>5} rms {t.get('rx_rms', 0):>5} [{_bar(t.get('rx_peak', 0))}]  "
                        f"TX peak {t.get('tx_peak', 0):>5} rms {t.get('tx_rms', 0):>5} [{_bar(t.get('tx_peak', 0))}]  "
                        f"clip {clip} peer {t.get('peer') or '-'}{flag}")
            sys.stdout.write("\r" + line.ljust(120))
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
    return 0


def cmd_tuner(args) -> int:
    """Imported lazily: tuner imports control_request from here."""
    from .tuner import cmd_tuner as run

    return run(args)


# ---- wifi ---------------------------------------------------------------------


def _nmcli(*argv: str, sudo: bool = True) -> subprocess.CompletedProcess:
    return _run(["nmcli", *argv], sudo=sudo)


def cmd_wifi_join(args) -> int:
    password = getpass.getpass(f"Wi-Fi password for {args.ssid!r}: ")
    if not password:
        return _fail("empty password")
    # --ask makes nmcli read the secret from stdin, so it never appears in ps or sudo logs.
    from . import wifiradio
    with wifiradio.locked():
        return _wifi_join(args.ssid, password, wifiradio.pick().name)   # a USB adapter when one is plugged in


def _wifi_join(ssid: str, password: str, radio: str) -> int:
    with persist.writable(persist.NETWORKS_DIR):    # the profile is written to /persist
        r = subprocess.run(_sudo(["nmcli", "--ask", "device", "wifi", "connect", ssid, "ifname", radio]),
                           input=password + "\n", text=True, capture_output=True, check=False)
        if r.returncode != 0:
            return _fail(f"nmcli: {(r.stderr or r.stdout).strip()}")
        # Bench use only: a known network must never win over the AP at boot, so the
        # saved profile does not autoconnect. `wifi join` again to use it next time.
        # The profile is usually named after the SSID, but not always; ask which one is active.
        active = _nmcli("-g", "GENERAL.CONNECTION", "device", "show", radio, sudo=False).stdout.strip() or ssid
        m = _nmcli("connection", "modify", active, "connection.autoconnect", "no")
    if m.returncode != 0:
        print(f"warning: could not turn off autoconnect for {active!r}: {(m.stderr or m.stdout).strip()}", file=sys.stderr)
    print(f"joined {ssid} (bench only; the AP comes back at reboot): {wifi_summary()}")
    return 0


def wifi_summary() -> str:
    from . import wifiradio
    dev = wifiradio.active_device()
    r = _nmcli("-t", "-f", "GENERAL.CONNECTION,GENERAL.STATE,IP4.ADDRESS", "device", "show", dev, sudo=False)
    if r.returncode != 0:
        return f"nmcli unavailable: {(r.stderr or r.stdout).strip()}"
    fields = dict(line.split(":", 1) for line in r.stdout.splitlines() if ":" in line)
    con = fields.get("GENERAL.CONNECTION", "") or "-"
    state = fields.get("GENERAL.STATE", "") or "-"
    ip = fields.get("IP4.ADDRESS[1]", "") or "-"
    return f"connection={con} state={state} ip={ip} radio={dev} host={socket.gethostname()}"


def cmd_wifi_status(args) -> int:
    print(wifi_summary())
    return 0


# ---- ap -----------------------------------------------------------------------


SERIAL_FILE = Path("/sys/firmware/devicetree/base/serial-number")


def _hardware_hex(serial_file: Path, wlan: str) -> str:
    """The Pi's serial as hex digits, falling back to the wlan0 MAC; "" when neither reads."""
    for path in (serial_file, Path(f"/sys/class/net/{wlan}/address")):
        try:
            raw = path.read_bytes().rstrip(b"\x00").decode(errors="ignore")
        except OSError:
            continue
        digits = re.sub(r"[^0-9a-fA-F]", "", raw)
        if digits:
            return digits
    return ""


def device_serial_suffix(serial_file: Path = SERIAL_FILE, wlan: str = WLAN) -> str:
    """Last four hex digits of the Pi's serial, falling back to the wlan0 MAC.

    Both are burned in, so the SSID is the same after every reboot and a phone that
    saved it reconnects on its own; two bridges at one site still differ."""
    return _hardware_hex(serial_file, wlan)[-4:].upper() or "0000"


def device_id(serial_file: Path = SERIAL_FILE, wlan: str = WLAN) -> str | None:
    """A stable ID apps bind their login token to (roadmap 1.8): unlike the SSID it cannot be
    renamed. A hash of the serial, so the whole serial is not handed to anyone on the Wi-Fi.
    None when neither the serial nor the MAC reads (off a Pi)."""
    digits = _hardware_hex(serial_file, wlan).lower()
    return hashlib.sha256(f"vector-bridge:{digits}".encode()).hexdigest()[:16] if digits else None


def default_ap_ssid(wlan: str = WLAN) -> str:
    return f"VectorBridge-{device_serial_suffix(wlan=wlan)}"


def generate_passphrase(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def ap_connection_argv(ssid: str, passphrase: str | None) -> list[str]:
    """nmcli arguments for the field access point. Shared mode gives DHCP and DNS
    (dnsmasq) on a pinned address; autoconnect at the highest priority makes the AP the
    boot default rather than a fallback after a failed scan for known networks.
    No passphrase means an open network: only the first boot of a burned card (apsetup).
    Saved for the built-in radio at 2.4 GHz; wifiradio moves it to a USB adapter (5 GHz when it can) in memory only."""
    # PMF disabled keeps it WPA2 only (no WPA3 transition mode): the built-in radio cannot do WPA3, see wifiradio.bring_up.
    security = ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", passphrase, "wifi-sec.pmf", "disable"] if passphrase else []
    return ["connection", "add", "type", "wifi", "ifname", WLAN, "con-name", AP_CON, "ssid", ssid,
            "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg", "802-11-wireless.channel", AP_CHANNEL_2G,
            "ipv4.method", "shared", "ipv4.addresses", f"{AP_ADDRESS}/24", *security,
            "connection.autoconnect", "yes", "connection.autoconnect-priority", "100",
            "connection.autoconnect-retries", "0"]  # 0 = retry forever; never give up on the AP


def ap_exists() -> bool:
    r = _nmcli("-t", "-f", "NAME", "connection", "show", sudo=False)
    return r.returncode == 0 and AP_CON in r.stdout.splitlines()


def _ap_create(ssid: str | None, passphrase: str | None, open_ap: bool = False,
               announce: bool = True) -> tuple[str, str | None] | None:
    ssid = ssid or default_ap_ssid()
    passphrase = None if open_ap else passphrase or generate_passphrase()
    with persist.writable(persist.NETWORKS_DIR):    # keyfiles live on /persist (install.sh)
        _nmcli("connection", "delete", AP_CON)  # idempotent: replace any previous hotspot
        r = _nmcli(*ap_connection_argv(ssid, passphrase))
    if r.returncode != 0:
        _fail(f"nmcli add: {(r.stderr or r.stdout).strip()}")
        return None
    if not announce:  # apsetup runs under systemd: the owner's passphrase stays out of the journal
        return ssid, passphrase
    print(f"AP configured ssid={ssid} passphrase={passphrase} (stored only in NetworkManager; `sudo vector-bridge ap show` prints it again). "
          f"Pi address on the AP is {AP_ADDRESS}, home page http://{PORTAL_HOST}/ ; "
          f"run `vector-bridge ap disable` to return to infrastructure Wi-Fi.", flush=True)
    return ssid, passphrase


def cmd_ap_ensure(args) -> int:
    """Installer entry point: make the AP the boot default without activating it now, so
    an SSH session over bench Wi-Fi survives. Idempotent: an existing AP keeps its passphrase."""
    if ap_exists() and not args.reset:
        print(f"AP already configured ({AP_CON}); passphrase unchanged. Use --reset to replace it.")
        return 0
    return 0 if _ap_create(args.ssid, args.passphrase) else 1


def cmd_pin_status(args) -> int:
    from . import admin
    if admin.PIN_PATH.exists() and not os.access(admin.PIN_PATH, os.R_OK):
        return _fail(f"cannot read {admin.PIN_PATH} (run with sudo)")
    print("admin PIN: set" if admin.Admin().pin_set() else "admin PIN: not set (the next save on the web page asks for one)")
    return 0


def cmd_pin_reset(args) -> int:
    """Console recovery for a forgotten PIN (roadmap 1.1). Running apps are logged out with it."""
    from . import admin
    try:
        had = admin.reset()
    except OSError as e:
        return _fail(f"could not remove {admin.PIN_PATH}: {e} (run with sudo)")
    print("admin PIN reset: the next save on the web page asks for a new one" if had else "no admin PIN was set")
    return 0


def cmd_ssh_status(args) -> int:
    from . import sshswitch
    print("SSH: on" if sshswitch.is_on() else "SSH: off")
    return 0


def cmd_ssh_on(args) -> int:
    """Console switch (R-D4): a new login password, then SSH on, kept across reboots."""
    from . import sshswitch
    if os.geteuid() != 0:
        return _fail("run with sudo")
    password = getpass.getpass(f"New password for {sshswitch.login_user()}: ")
    errors = sshswitch.check_password(password)
    if errors:
        return _fail(" ".join(errors))
    if getpass.getpass("Type it again: ") != password:
        return _fail("the passwords do not match; nothing changed")
    try:
        h = sshswitch.hash_password(password)
    except OSError as e:
        return _fail(str(e))
    ok, text = sshswitch.turn({"action": "on", "hash": h})
    print(text)
    return 0 if ok else 1


def cmd_ssh_off(args) -> int:
    from . import sshswitch
    if os.geteuid() != 0:
        return _fail("run with sudo")
    ok, text = sshswitch.turn({"action": "off"})
    print(text)
    return 0 if ok else 1


def cmd_ap_show(args) -> int:
    """SSID and passphrase straight from NetworkManager, so there is no second copy to go stale."""
    r = _nmcli("-s", "-g", "802-11-wireless.ssid,802-11-wireless-security.psk", "connection", "show", AP_CON)
    if r.returncode != 0:
        return _fail(f"no access point configured: {(r.stderr or r.stdout).strip()}")
    ssid, _, psk = r.stdout.strip().partition("\n")
    print(f"ssid={ssid} passphrase={psk} address={AP_ADDRESS} home=http://{PORTAL_HOST}/")
    return 0


def cmd_ap_enable(args) -> int:
    """Create the hotspot fully configured, print the passphrase, and activate it last:
    activation drops infrastructure Wi-Fi (and an SSH session on it), so nothing
    important may run after `connection up`."""
    if not _ap_create(args.ssid, None):
        return 1
    r = _nmcli("connection", "up", AP_CON)
    if r.returncode != 0:
        return _fail(f"nmcli up: {(r.stderr or r.stdout).strip()} (hotspot is configured and will start on reboot)")
    return 0


def cmd_ap_disable(args) -> int:
    """Delete first: nmcli deactivates and removes in one call, and the infrastructure
    connection autoconnects on its own. The SSH session over the AP drops here."""
    from . import wifiradio
    with wifiradio.locked():
        with persist.writable(persist.NETWORKS_DIR):
            r = _nmcli("connection", "delete", AP_CON)
        if r.returncode != 0 and "unknown connection" not in (r.stderr + r.stdout).lower():
            return _fail(f"nmcli delete: {(r.stderr or r.stdout).strip()}")
        _nmcli("device", "connect", wifiradio.pick().name)
    print(f"AP disabled; {wifi_summary()}")
    return 0


# ---- argparse wiring ------------------------------------------------------------


def add_subcommands(sub) -> None:
    def ctl(p):
        p.add_argument("--host", default="127.0.0.1", help="service host (default local; another host refuses settings changes)")
        p.add_argument("--port", type=int, default=None, help="control port (default from config)")

    service = sub.add_parser("service", help="start|stop|restart|status|logs the systemd unit")
    service.add_argument("action", choices=["start", "stop", "restart", "status", "logs"])
    service.add_argument("-n", "--lines", type=int, default=50, help="log lines (logs)")
    service.add_argument("-f", "--follow", action="store_true", help="follow the log (logs)")
    service.set_defaults(func=cmd_service)

    status = sub.add_parser("status", help="telemetry from the running service, as a table")
    ctl(status)
    status.add_argument("--json", action="store_true", help="print raw JSON instead of a table")
    status.set_defaults(func=cmd_status)

    audio = sub.add_parser("audio", help="ALSA card selection, gain and live meter")
    asub = audio.add_subparsers(dest="audio_cmd", required=True)
    asub.add_parser("list", help="show ALSA cards with USB ids; * marks the selected one").set_defaults(func=cmd_audio_list)
    sel = asub.add_parser("select", help="select a card by index or id, persist its USB id, restart")
    sel.add_argument("card")
    sel.set_defaults(func=cmd_audio_select)
    gain = asub.add_parser("gain", help="set and persist mixer levels")
    ctl(gain)
    gain.add_argument("--tx", type=int, default=None, help="playback level to the KX2 mic, 0-100")
    gain.add_argument("--rx", type=int, default=None, help="capture level from the KX2 phones, 0-100")
    gain.set_defaults(func=cmd_audio_gain)
    meter = asub.add_parser("meter", help="live RX/TX peak, RMS, clip readout until Ctrl-C")
    ctl(meter)
    meter.add_argument("--interval", type=float, default=0.25, help="refresh seconds (default 0.25)")
    meter.set_defaults(func=cmd_audio_meter)

    tuner = sub.add_parser("tuner", help="browser level tuner: live meters and gain from a phone")
    ctl(tuner)
    tuner.add_argument("--bind", default="0.0.0.0", help="address to serve on (default all interfaces)")
    tuner.add_argument("--http-port", type=int, default=8080, help="web port (default 8080)")
    tuner.set_defaults(func=cmd_tuner)

    wifi = sub.add_parser("wifi", help="infrastructure Wi-Fi through nmcli")
    wsub = wifi.add_subparsers(dest="wifi_cmd", required=True)
    join = wsub.add_parser("join", help="join an SSID (password prompted, never on the command line)")
    join.add_argument("ssid")
    join.set_defaults(func=cmd_wifi_join)
    wsub.add_parser("status", help="active connection and IP").set_defaults(func=cmd_wifi_status)

    ap = sub.add_parser("ap", help="field-mode access point, on a USB Wi-Fi adapter when one is plugged in (drops your Wi-Fi SSH session)")
    apsub = ap.add_subparsers(dest="ap_cmd", required=True)
    en = apsub.add_parser("enable", help="create the hotspot and print its passphrase once")
    en.add_argument("--ssid", default=None, help="SSID (default VectorBridge-<last 4 hex of the Pi serial>)")
    en.set_defaults(func=cmd_ap_enable)
    ens = apsub.add_parser("ensure", help="make the AP the boot default without activating it (installer)")
    ens.add_argument("--ssid", default=None)
    ens.add_argument("--passphrase", default=None, help="fixed passphrase (default: generated and printed once)")
    ens.add_argument("--reset", action="store_true", help="replace an existing AP profile and passphrase")
    ens.set_defaults(func=cmd_ap_ensure)
    apsub.add_parser("show", help="print the AP name and passphrase (needs sudo)").set_defaults(func=cmd_ap_show)
    apsub.add_parser("disable", help="remove the hotspot and return to infrastructure Wi-Fi").set_defaults(func=cmd_ap_disable)

    pin = sub.add_parser("pin", help="the web pages' admin PIN (DOCS/web-api.md)")
    pinsub = pin.add_subparsers(dest="pin_cmd", required=True)
    pinsub.add_parser("status", help="say whether an admin PIN is set").set_defaults(func=cmd_pin_status)
    pinsub.add_parser("reset", help="forget the admin PIN; the next save on the web page asks for a new one (needs sudo)"
                      ).set_defaults(func=cmd_pin_reset)

    ssh = sub.add_parser("ssh", help="SSH login, off on a fresh image (R-D4); kept across reboots")
    sshsub = ssh.add_subparsers(dest="ssh_cmd", required=True)
    sshsub.add_parser("status", help="say whether SSH is on").set_defaults(func=cmd_ssh_status)
    sshsub.add_parser("on", help="set a new login password and turn SSH on (needs sudo)").set_defaults(func=cmd_ssh_on)
    sshsub.add_parser("off", help="turn SSH off; the password stays (needs sudo)").set_defaults(func=cmd_ssh_off)
