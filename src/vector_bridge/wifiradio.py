"""Which Wi-Fi radio carries the bridge's network, and moving it when a USB adapter comes or goes.

The Pi's built-in radio is 2.4 GHz only and drops audio packets under load (bench runs on
2026-10-06: 0.25 to 1.56 % of sends failed; through a USB 5 GHz adapter, 0 to 0.03 %). So a USB
Wi-Fi adapter that can run an access point, plugged into any port, takes over whatever the bridge
is doing: its own access point, on a free 5 GHz channel when the adapter and the country allow
one (else 2.4 GHz), or the network it joined. The built-in radio sits idle meanwhile. Unplug the
adapter and everything moves back to the built-in radio.

Nothing is written to the card for a move: the saved profile is re-pointed in NetworkManager's
memory only (`connection modify --temporary`). At boot the access point starts on the built-in
radio as before, and the watcher moves it within seconds.

`python -m vector_bridge.wifiradio watch` runs as root under vb-wifi-radio.service and looks
every POLL_S. wifijoin, apsetup and the `vector-bridge` Wi-Fi commands hold LOCK while they change
networks, so nobody moves a radio at the same time. An adapter that cannot bring the access point
up is listed in SKIP until it is unplugged, and pick() passes it over too.

5 GHz needs a Wi-Fi country (R-D7, vector_bridge.wificountry). With none, every radio counts as
2.4 GHz only, even when a driver or the world domain would allow some 5 GHz channel. The watcher
also sets the kernel's domain to the owner's saved choice, at start and whenever it changes.
"""
from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import manage, wificountry

SYS_NET = Path("/sys/class/net")
LOCK = Path("/run/vb-wifi-radio.lock")
SKIP = Path("/run/vb-wifi-radio.skip")
POLL_S = 2.0
# Nothing on the air this long, with no request running: put the access point up.
IDLE_S = 20.0
# Channels without radar detection, upper band first: it allows more power in the US, and 165
# lost nothing in the 2026-10-07 test. iw lists only what the country allows, with flags.
CHANNELS_5 = (149, 153, 157, 161, 165, 36, 40, 44, 48)
_UNUSABLE = ("disabled", "no ir", "radar", "passive scan", "no ibss")
_FREQ = re.compile(r"^\*\s*(\d+)(?:\.\d+)?\s*MHz\s*\[(\d+)\](.*)$")
_NOT_READY = ("unmanaged", "unavailable")
WIFI = "802-11-wireless"
# A login shell's PATH leaves out /usr/sbin, where iw lives.
IW = shutil.which("iw") or "/usr/sbin/iw"


@dataclass(frozen=True)
class Radio:
    name: str
    usb: bool
    ap: bool = True
    channels: tuple[int, ...] = ()   # 5 GHz access point channels it may use, best first; none: 2.4 GHz

    @property
    def channel5(self) -> int:
        return self.channels[0] if self.channels else 0

    @property
    def band(self) -> str:
        return "a" if self.channel5 else "bg"

    def describe(self) -> str:
        return f"{self.name} ({'USB adapter' if self.usb else 'built-in'}, {'5' if self.channel5 else '2.4'} GHz)"


@dataclass(frozen=True)
class Active:
    uuid: str
    device: str
    state: str
    name: str


def parse_phy(text: str) -> tuple[bool, tuple[int, ...]]:
    """(can run an access point, usable 5 GHz channels in CHANNELS_5 order) from `iw phy <phy> info`."""
    ap, in_modes, usable = False, False, set()
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Supported interface modes:"):
            in_modes = True
            continue
        if in_modes:
            if s.startswith("* "):
                ap |= s[2:].strip() == "AP"
                continue
            in_modes = False
        m = _FREQ.match(s)
        if m and 5150 <= int(m.group(1)) <= 5895 and not any(f in m.group(3).lower() for f in _UNUSABLE):
            usable.add(int(m.group(2)))
    return ap, tuple(c for c in CHANNELS_5 if c in usable)


_phy_cache: dict[str, tuple[bool, tuple[int, ...]]] = {}


def _phy_info(phy: str) -> tuple[bool, tuple[int, ...]]:
    # A phy's capabilities do not change while it is plugged in; a replug gets a new phy name. A new
    # country clears the cache (Watcher.tick).
    if phy not in _phy_cache:
        try:
            r = subprocess.run([IW, "phy", phy, "info"], capture_output=True, text=True, check=False, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False, ()
        if r.returncode != 0:
            return False, ()
        _phy_cache[phy] = parse_phy(r.stdout)
    ap, channels = _phy_cache[phy]
    return (ap, channels) if wificountry.current() else (ap, ())


def radios(sys_net: Path = SYS_NET, phy_info=_phy_info) -> list[Radio]:
    """Every Wi-Fi interface, built-in (SDIO) or USB, with what it can do."""
    out = []
    try:
        devs = sorted(sys_net.iterdir())
    except OSError:
        return []
    for dev in devs:
        if not (dev / "wireless").is_dir():
            continue
        try:
            phy = (dev / "phy80211" / "name").read_text().strip()
        except OSError:
            continue
        ap, channels = phy_info(phy)
        out.append(Radio(dev.name, "/usb" in os.path.realpath(dev / "device"), ap, channels))
    return out


def best(rs: list[Radio], skip: frozenset[str] | set[str] = frozenset()) -> Radio | None:
    """A USB adapter over the built-in radio, then 5 GHz over 2.4 GHz. Only radios that can be an access point:
    the bridge falls back to its own access point at any time."""
    usable = [r for r in rs if r.ap and r.name not in skip]
    return min(usable, key=lambda r: (not r.usb, not r.channel5, r.name)) if usable else None


def ready_devices() -> set[str]:
    """Interfaces NetworkManager can use now (a just-plugged adapter is 'unavailable' for a moment)."""
    r = manage._nmcli("-t", "-f", "DEVICE,STATE", "device", sudo=False)
    if r.returncode != 0:
        return set()
    return {dev for dev, _, state in (line.partition(":") for line in r.stdout.splitlines())
            if state and not state.startswith(_NOT_READY)}


def skipped() -> set[str]:
    try:
        return set(SKIP.read_text().split())
    except OSError:
        return set()


def pick() -> Radio:
    """The radio a new connection goes on. The built-in radio's name when nothing better is known."""
    ready = ready_devices()
    return best([r for r in radios() if r.name in ready], skipped()) or Radio(manage.WLAN, usb=False)


def built_in() -> Radio:
    return next((r for r in radios() if not r.usb), Radio(manage.WLAN, usb=False))


def quietest(radio: Radio) -> int:
    """The radio's 5 GHz channel with the fewest other networks on it, by a scan while it is idle (an
    access point cannot scan). Ties, and no scan, go to the CHANNELS_5 order."""
    r = manage._nmcli("-t", "-f", "CHAN", "device", "wifi", "list", "ifname", radio.name, "--rescan", "yes", sudo=False)
    seen = [int(c) for c in r.stdout.split() if c.isdigit()] if r.returncode == 0 else []
    return min(radio.channels, key=lambda c: (seen.count(c), radio.channels.index(c)))


def _unescape(field: str) -> str:
    return field.replace("\\:", ":").replace("\\\\", "\\")


def active() -> list[Active]:
    """Wi-Fi connections NetworkManager has up or is bringing up."""
    r = manage._nmcli("-t", "-f", "UUID,TYPE,DEVICE,STATE,NAME", "connection", "show", "--active", sudo=False)
    if r.returncode != 0:
        return []
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split(":", 4)
        if len(parts) == 5 and parts[1] == WIFI:
            rows.append(Active(parts[0], parts[2], parts[3], _unescape(parts[4])))
    return rows


def active_device() -> str:
    """The radio the bridge's network is on now, else the one it would use."""
    up = [a.device for a in active() if a.state == "activated"]
    best_now = pick().name
    return best_now if best_now in up or not up else up[0]


_CHANNEL = re.compile(r"channel \d+ \((\d+)(?:\.\d+)? MHz\)")


def is_usb(dev: str, sys_net: Path = SYS_NET) -> bool:
    return bool(dev) and "/usb" in os.path.realpath(sys_net / dev / "device")


def label(dev: str, sys_net: Path = SYS_NET) -> str:
    """For the Wi-Fi page: "USB adapter, 5 GHz", or "Built-in, 2.4 GHz"; the band only while it is on the air."""
    if not dev:
        return ""
    kind = "USB adapter" if is_usb(dev, sys_net) else "Built-in"
    try:
        r = subprocess.run([IW, "dev", dev, "info"], capture_output=True, text=True, check=False, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return kind
    m = _CHANNEL.search(r.stdout)
    if not m:
        return kind
    mhz = int(m.group(1))
    return f"{kind}, {'2.4' if mhz < 3000 else '5' if mhz < 5925 else '6'} GHz"


def bring_up(con: str, radio: Radio, is_ap: bool | None = None) -> subprocess.CompletedProcess:
    """Re-point a saved profile at `radio`, in memory only, and activate it there. The access point also
    takes the radio's band: 5 GHz first, then 2.4 GHz if the adapter refuses it."""
    if is_ap is None:
        is_ap = con == manage.AP_CON
    if not is_ap:
        r = manage._nmcli("connection", "modify", "--temporary", con, "connection.interface-name", radio.name)
        return r if r.returncode != 0 else manage._nmcli("connection", "up", con)
    # WPA2 only, on every radio: NetworkManager adds WPA3 (SAE) where the radio can, the built-in radio
    # cannot, and an Apple device that saw WPA3 asks for the password on the WPA2-only fallback after an
    # unplug. PMF disabled is NetworkManager's switch for that. An open access point has no security to set.
    wpa2 = ["802-11-wireless-security.pmf", "disable"] if _ap_key_mgmt(con) == "wpa-psk" else []
    # 2.4 GHz gets a fixed channel, never NetworkManager's "auto" (manage.AP_CHANNEL_2G).
    bg = ("bg", manage.AP_CHANNEL_2G)
    bands = [("a", str(quietest(radio))), bg] if radio.channels else [bg]
    for band, channel in bands:
        r = manage._nmcli("connection", "modify", "--temporary", con, "connection.interface-name", radio.name,
                          "802-11-wireless.band", band, "802-11-wireless.channel", channel, *wpa2)
        if r.returncode == 0:
            r = manage._nmcli("connection", "up", con)
        if r.returncode == 0:
            break
    return r


@contextmanager
def locked(blocking: bool = True):
    """Held by whoever is moving the radio. Yields False when not blocking and someone else holds it.
    Root creates the file; a non-root caller (a bench `vector-bridge wifi join`) locks it read-only,
    or goes ahead without it before the watcher has made it."""
    try:
        fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    except PermissionError:
        try:
            fd = os.open(LOCK, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            yield True
            return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def _log(msg: str) -> None:
    print(f"vb-wifi-radio: {msg}", flush=True)


class Watcher:
    """One look per tick: keep the bridge's one Wi-Fi connection on the best radio there is."""

    def __init__(self, radios_fn=None, ready_fn=ready_devices, active_fn=active, up_fn=bring_up,
                 band_fn=None, clock=time.monotonic, down_fn=None, skip_fn=None, country_fn=None,
                 reg_fn=None) -> None:
        self.down_fn = down_fn or _disconnect
        self.skip_fn = skip_fn or _write_skip
        self.radios_fn = radios_fn or radios
        self.ready_fn, self.active_fn, self.up_fn, self.clock = ready_fn, active_fn, up_fn, clock
        self.band_fn = band_fn or _ap_band
        self.role: Active | None = None     # the last connection seen on the air
        self.bad: set[str] = set()          # radios that refused it, skipped until unplugged
        self.idle_since: float | None = None
        self.checked: str | None = None     # the radio whose access point band is known right
        self.rs: list[Radio] = []
        self.country_fn = country_fn or (lambda: (wificountry.saved(), wificountry.cmdline()))
        self.reg_fn = reg_fn or _set_country
        self.country: tuple[str, str] | None = None     # (saved, command line) as last seen

    def tick(self) -> None:
        country = self.country_fn()
        if country != self.country:
            started = self.country is not None
            self.country = country
            if country[0]:
                self.reg_fn(country[0])
            if started or country[0]:
                _phy_cache.clear()
                self.checked = None     # the access point's band is looked at again
                return                  # the kernel takes the new domain in the background: look next tick
        ready = self.ready_fn()
        rs = self.rs = [r for r in self.radios_fn() if r.name in ready]
        if self.bad - {r.name for r in rs}:
            self.bad &= {r.name for r in rs}
            self.skip_fn(self.bad)
        want = best(rs, self.bad)
        if want is None:
            return
        acts = self.active_fn()
        if acts:
            self.idle_since = None
            on_want = [a for a in acts if a.device == want.name]
            a = on_want[0] if on_want else acts[0]
            self.role = a
            if on_want:
                # One radio at a time: the built-in sits idle (NetworkManager autoconnects the access point
                # on it once the joined network has moved to the adapter).
                for other in acts:
                    if other.device != want.name and other.device:
                        _log(f"{other.name}: off {other.device}, {want.name} carries the network")
                        self.down_fn(other.device)
            if a.state != "activated":
                return
            if a.device != want.name:
                if a.name == manage.AP_CON:
                    # Down on the old radio first: its DHCP server holds 10.42.0.1 until then, and the new
                    # one fails "address in use", leaving phones with no address until NetworkManager retries.
                    self.down_fn(a.device)
                self._move(a.uuid, a.name, want, f"from {a.device}")
            elif a.name == manage.AP_CON and self.checked != a.device:
                # Up on the right radio but maybe not by us (NetworkManager's own autoconnect): check the band.
                self.checked = a.device
                band = self.band_fn(a.uuid)
                if band in ("a", "bg") and band != want.band:
                    self._move(a.uuid, a.name, want, "to its best band")
            return
        now = self.clock()
        self.checked = None
        gone = self.role is not None and self.role.device not in {r.name for r in rs}
        if self.idle_since is None:
            self.idle_since = now
        if gone:
            # Its radio was unplugged: bring the same connection back on the next best one.
            self._move(self.role.uuid, self.role.name, want, f"after {self.role.device} went away")
        elif now - self.idle_since >= IDLE_S:
            _log(f"nothing on the air for {IDLE_S:.0f} s")
            self._move(manage.AP_CON, manage.AP_CON, want, "")

    def _move(self, con: str, name: str, radio: Radio, why: str) -> None:
        is_ap = name == manage.AP_CON
        _log(f"{name}: to {radio.describe()} {why}".rstrip())
        self.idle_since = None
        self.checked = radio.name
        r = self.up_fn(con, radio, is_ap)
        if r.returncode == 0:
            self.role = Active(con, radio.name, "activated", name)
            return
        _log(f"{radio.name} refused {name}: {(r.stderr or r.stdout).strip()}")
        if not is_ap:
            # A network out of reach (or a changed passphrase): the bridge's own access point instead, as wifijoin does.
            self._move(manage.AP_CON, manage.AP_CON, radio, "instead")
            return
        if not radio.usb or "unknown connection" in (r.stderr or r.stdout).lower():
            return      # the last resort, and a deleted profile (`ap disable`), skip nothing: the idle timer tries again
        # An adapter that cannot run the access point is skipped until it is unplugged; try the next radio.
        self.bad.add(radio.name)
        self.skip_fn(self.bad)
        nxt = best(self.rs, self.bad)
        if nxt is not None:
            self._move(manage.AP_CON, manage.AP_CON, nxt, "instead")


def _disconnect(dev: str) -> None:
    # A device disconnect also stops NetworkManager autoconnecting on it until something asks for it.
    manage._nmcli("device", "disconnect", dev)


def _set_country(code: str) -> None:
    _log(f"Wi-Fi country {code}")
    try:
        r = subprocess.run([IW, "reg", "set", code], capture_output=True, text=True, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        _log(f"iw reg set {code} failed: {e}")
        return
    if r.returncode != 0:
        _log(f"iw reg set {code} failed: {(r.stderr or r.stdout).strip()}")


def _write_skip(names: set[str]) -> None:
    try:
        SKIP.write_text(" ".join(sorted(names)) + "\n")
    except OSError as e:
        _log(f"could not write {SKIP}: {e}")


def _ap_key_mgmt(con: str) -> str:
    return manage._nmcli("-g", "802-11-wireless-security.key-mgmt", "connection", "show", con, sudo=False).stdout.strip()


def _ap_band(con: str) -> str:
    return manage._nmcli("-g", "802-11-wireless.band", "connection", "show", con, sudo=False).stdout.strip()


def watch() -> int:
    w = Watcher()
    _write_skip(set())
    _log("watching for USB Wi-Fi adapters")
    while True:
        with locked(blocking=False) as mine:
            if mine:
                try:
                    w.tick()
                except (OSError, subprocess.SubprocessError) as e:
                    _log(f"tick failed: {e}")
        time.sleep(POLL_S)


def show() -> int:
    ready = ready_devices()
    for r in radios():
        print(f"{r.describe()}  ap={'yes' if r.ap else 'no'}  channels={','.join(map(str, r.channels)) or manage.AP_CHANNEL_2G + ' (2.4 GHz)'}  ready={'yes' if r.name in ready else 'no'}")
    print(f"best: {pick().describe()}")
    return 0


if __name__ == "__main__":
    sys.exit({"watch": watch, "show": show}[sys.argv[1] if len(sys.argv) > 1 else "show"]())
