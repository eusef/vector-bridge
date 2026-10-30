"""Safety proxy for desktop apps on the rigctl port (HANDOFF M4, D6).

WSJT-X and JS8Call talk Hamlib NET rigctl to port 4532. rigctld itself listens on a
private loopback port, and this proxy sits in front of it: every command passes
through byte for byte except PTT, which goes through the Ladder, the same door as
``POST /api/ladder``. So a desktop key gets the transmit checks, the one TX lock and
the watchdog, plus two rules of its own:

- a client that disconnects while keyed is unkeyed at once;
- a key lasts at most ``max_key_s``, the operator's Transmit Time Limit (the same setting
  as the TX audio cap, 0 = off, changed live by the tuner). The apps hold ``T 1`` for a whole 13 to 15 s slot
  with no heartbeat, so the proxy heartbeats for a connected client and enforces
  the cap instead.

After the bridge drops a key (cap, watchdog, re-lock), that client's next ``T 1`` is
refused until it sends ``T 0``, so a stuck app cannot re-key straight away.

rigctld reads a token stream, so ``f T 1`` on one line would key. A line that carries
a PTT or other transmit command anywhere but its start is refused. That keeps
well-behaved apps inside the rules; it is not a security boundary against someone
who holds the Wi-Fi passphrase. The apps are request and response, one command per
line, which is what the ordering of replies here relies on.

Who can connect is the ``rigctl_access`` setting (config.RIGCTL_ACCESS), switched live
by ``rebind``: loopback only, loopback plus the access point's address, loopback plus
whichever Wi-Fi the Pi is on (R-D9), or every interface. The AP address exists only while
the AP is up, so on Linux it is bound with IP_FREEBIND and starts answering whenever the AP
comes up. The Wi-Fi setting listens on every interface and admits a client only when the
address it came in on is loopback or a Wi-Fi interface's, read at each connection, so it
follows a switch between the AP and a joined network and never answers on the tailnet.

Every client that is not loopback, whatever the setting (the maintainer ruled 2026-10-28:
Wi-Fi peers too, so the admin PIN guards CAT and PTT the way it guards the web pages), must
first send ``\\vb_auth <app token>`` (roadmap 1.10 built it for the tailnet; the token is a
1.8 app token). The reply is ``RPRT 0``; a wrong or missing token, any other first line, or no line
within AUTH_TIMEOUT_S gets ``RPRT -9`` and the connection closed, so nothing reaches
rigctld. The token is checked again every AUTH_RECHECK_S, so a revoke or a PIN change
closes the client (and unkeys it). WSJT-X and JS8Call cannot send it: VectorCable relays them.
"""

from __future__ import annotations

import fcntl
import ipaddress
import logging
import re
import socket
import struct
import sys
import threading
import time
from pathlib import Path

from .cat import DEFAULT_RIGCTLD_PORT
from .portal import AP_ADDRESS
from .txcap import DEFAULT_CAP_S

log = logging.getLogger(__name__)

PROXY_PORT = 4532  # what WSJT-X and JS8Call expect for Hamlib NET rigctl
MAX_KEY_S = float(DEFAULT_CAP_S)  # the Transmit Time Limit (config tx_audio_cap_s) shared with the TX audio cap; 0 = no cap
TICK_S = 0.5  # heartbeat for keyed clients, well inside the 3 s watchdog
MAX_CLIENTS = 8
MAX_PENDING = 2  # clients still to log in; they hold no rigctld connection and no MAX_CLIENTS slot
MAX_LINE = 4096
UPSTREAM_TIMEOUT_S = 2.0
EVERY_INTERFACE = "0.0.0.0"
AUTH_COMMAND = "\\vb_auth"
AUTH_TIMEOUT_S = 5.0  # a client that must log in and has not
AUTH_RECHECK_S = 5.0  # a revoked token closes its client within this
TAILNET = ipaddress.ip_network("100.64.0.0/10")  # Tailscale's addresses
WIFI = "wifi"  # in binds: listen on every interface, admit loopback and Wi-Fi addresses only
SYS_NET = Path("/sys/class/net")
SIOCGIFADDR = 0x8915  # Linux: an interface's IPv4 address
# Not in every Python's socket module; 15 is Linux's value. Elsewhere (a Mac bench) the AP address cannot be bound.
IP_FREEBIND = getattr(socket, "IP_FREEBIND", 15 if sys.platform.startswith("linux") else None)

# Hamlib status codes, sent back as RPRT -n
EINVAL = 1
EIO = 6
ERJCTED = 9  # "command rejected": WSJT-X shows it as a rig control error

SEPARATORS = "+;|,"  # extended response prefixes
PTT_VALUES = ("0", "1", "2", "3")  # off, on, on (mic), on (data)
# Commands that make the rig transmit without PTT: CW, voice memories, raw CAT, DTMF.
# Short forms are one character; 0x94 and 0x89 are rigctld's byte codes for the last two.
TRANSMITS = {"b", "send_morse", "w", "send_cmd", "W", "send_cmd_rx", "send_raw", "send_voice_mem", "send_dtmf",
             "\x94", "\x89"}
_GLUED_PTT = re.compile(r"T\d")  # "T1": rigctld reads the command letter, then the rest as its argument


def _split(token: str) -> tuple[str, str, bool]:
    """(separator, command name, long form) for a token in command position."""
    sep = token[0] if token[:1] in tuple(SEPARATORS) else ""
    name = token[len(sep):]
    if name.startswith("\\"):
        return sep, name[1:], True
    return sep, name, False


def _hides_transmit(token: str) -> bool:
    _, name, long_form = _split(token)
    if long_form:
        return name == "set_ptt" or name in TRANSMITS
    return name == "T" or bool(_GLUED_PTT.fullmatch(name[:2])) or name in TRANSMITS


def classify(line: str) -> tuple[str, str, str]:
    """("pass" | "ptt" | "refuse" | "invalid", separator, PTT value) for one client line."""
    tokens = line.split()
    if not tokens:
        return "pass", "", ""
    sep, name, long_form = _split(tokens[0])
    if (long_form and name == "set_ptt") or (not long_form and name == "T"):
        if len(tokens) != 2 or tokens[1] not in PTT_VALUES:
            return "invalid", sep, tokens[1] if len(tokens) > 1 else ""
        return "ptt", sep, tokens[1]
    if not long_form and (name[:1] == "T" or name[:1] in TRANSMITS):
        return "refuse", sep, ""  # a short command with its argument glued on, like "T1"
    if long_form and name in TRANSMITS:
        return "refuse", sep, ""
    if any(_hides_transmit(t) for t in tokens[1:]):
        return "refuse", sep, ""
    return "pass", sep, ""


def binds_for(access: str) -> tuple[str, ...]:
    """Listen addresses for a rigctl_access value; anything unknown is loopback only."""
    if access == "all":
        return (EVERY_INTERFACE,)
    if access == "ap":
        return ("127.0.0.1", AP_ADDRESS)
    if access == "wifi":
        return (WIFI,)
    return ("127.0.0.1",)


def _tailnet(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in TAILNET
    except ValueError:
        return False


def wifi_addresses(sys_net: Path = SYS_NET) -> set[str]:
    """The IPv4 address of each Wi-Fi interface that has one: the AP's 10.42.0.1, a joined network's lease.
    Empty off Linux. The tailnet's interface is not Wi-Fi, so its address is never here."""
    out = set()
    try:
        devs = list(sys_net.iterdir())
    except OSError:
        return out
    for dev in devs:
        if not (dev / "wireless").is_dir():
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                ifreq = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", dev.name.encode()[:15]))
        except OSError:
            continue  # no IPv4 address yet
        out.add(socket.inet_ntoa(ifreq[20:24]))
    return out


def ptt_reply(sep: str, value: str, code: int) -> bytes:
    """Frame a set_ptt reply the way rigctld does (probed on Hamlib 4.6.5)."""
    status = f"RPRT {-code if code else 0}"
    if not sep:
        return f"{status}\n".encode()
    if sep == "+":
        return f"set_ptt: {value}\n{status}\n".encode()
    return f"set_ptt: {value}{sep}{status}\n".encode()


class Session:
    """One desktop client: its socket, its rigctld connection and its PTT state."""

    def __init__(self, proxy: RigctlProxy, client: socket.socket, peer: tuple, upstream: socket.socket | None,
                 must_log_in: bool = False) -> None:
        self.proxy = proxy
        self.client = client
        self.upstream = upstream
        self.owner = f"rigctl {peer[0]}:{peer[1]}"
        self.local = client.getsockname()[0]  # the address it came in on: a rebind may no longer allow it
        self.peer_ip = peer[0]
        self.token: str | None = None  # the app token it logged in with, when it had to
        self.authed = not must_log_in  # decided once, in _admit: else upstream is None until it logs in
        self.opened_at = self.checked_at = proxy.clock()
        self.keyed = False
        self.keyed_at = 0.0
        self.latched = False  # the bridge dropped our key: refuse T 1 until the client sends T 0
        self._mu = threading.Lock()  # PTT state
        self._write = threading.Lock()  # replies from both directions share the client socket
        self._closed = False

    @property
    def _ladder(self):
        return self.proxy.ladder

    # -- PTT --

    def _held(self) -> bool:
        return self._ladder.ptt.lock.owner == self.owner

    def _unkey(self, why: str) -> int:
        """Caller holds _mu. 0 when the rig is back in receive, else EIO (the watchdog retries).
        A client's own T 0 lets the queued TX audio play out first (REQ-AUDIO-022); every other
        reason (cap, disconnect) drops PTT at once."""
        self.keyed = False
        try:
            self._ladder.unkey(self.owner, drain=why == "client")
        except Exception:
            log.exception("%s: unkey (%s) failed", self.owner, why)
            return EIO
        log.info("%s: unkeyed (%s)", self.owner, why)
        return 0

    def ptt(self, on: bool) -> int:
        with self._mu:
            if self._closed:  # a pipelined T 1 behind a close (a revoked token): close() has already unkeyed
                return ERJCTED
            if not on:
                self.latched = False
                return self._unkey("client") if self.keyed else 0
            if self.keyed and self._held():
                return 0
            if self.keyed:  # dropped by the bridge since the last tick
                self.keyed, self.latched = False, True
            if self.latched:
                log.warning("%s: key refused until the client unkeys (its last key was dropped)", self.owner)
                return ERJCTED
            try:
                ok = self._ladder.key(self.owner)
            except Exception:
                log.exception("%s: key failed", self.owner)
                return EIO
            if not ok:
                log.warning("%s: key refused (TX held by %s, or transmit checks, battery, open Wi-Fi, receive audio or a frequency outside the ham bands)",
                            self.owner, self._ladder.ptt.lock.owner)
                return ERJCTED
            self.keyed, self.keyed_at = True, self.proxy.clock()
            log.info("%s: keyed", self.owner)
            return 0

    def tick(self, now: float) -> None:
        if not self._auth_still_good(now):
            self.close()
            return
        with self._mu:
            if not self.keyed:
                return
            if not self._held():
                self.keyed, self.latched = False, True
                log.warning("%s: key dropped by the bridge", self.owner)
                return
            if self.proxy.max_key_s > 0 and now - self.keyed_at >= self.proxy.max_key_s:
                self.latched = True
                log.warning("%s: key held %.0f s, over the %.0f s cap", self.owner, now - self.keyed_at,
                            self.proxy.max_key_s)
                self._unkey("cap")
                return
            try:
                self._ladder.heartbeat(self.owner)  # may unkey (re-lock, capped rig): the next tick sees it
            except Exception:
                log.exception("%s: heartbeat failed", self.owner)

    # -- app token (roadmap 1.10) --

    def _auth_still_good(self, now: float) -> bool:
        if not self.authed:
            if now - self.opened_at < AUTH_TIMEOUT_S:
                return True
            log.warning("%s: closed, no %s within %.0f s", self.owner, AUTH_COMMAND, AUTH_TIMEOUT_S)
            self._refuse_quietly()
            return False
        if self.token is None or now - self.checked_at < AUTH_RECHECK_S:
            return True
        self.checked_at = now
        if self.proxy.token_valid(self.token):
            return True
        log.warning("%s: closed, its app token was revoked or the PIN changed", self.owner)
        return False

    def _refuse_quietly(self) -> None:
        try:
            self.send(f"RPRT -{ERJCTED}\n".encode())
        except OSError:
            pass

    def _login(self, raw: bytes) -> bool:
        """The first line from a client that must log in. False: refused, close it."""
        parts = raw.decode("latin-1").split()
        if not (len(parts) == 2 and parts[0] == AUTH_COMMAND and self.proxy.token_valid(parts[1])):
            log.warning("%s: refused, it must send %s <app token> first", self.owner, AUTH_COMMAND)
            self._refuse_quietly()
            return False
        if self.proxy.open_sessions() >= MAX_CLIENTS:
            log.warning("%s: refused, %d clients already", self.owner, MAX_CLIENTS)
            self._refuse_quietly()
            return False
        if not self._open_upstream():
            return False
        self.token, self.checked_at = parts[1], self.proxy.clock()
        self.authed = True
        log.info("%s: logged in with an app token", self.owner)
        self.send(b"RPRT 0\n")
        return True

    def _open_upstream(self) -> bool:
        """After a login: the rigctld connection and its reply thread, which a client that must log in has not had."""
        try:
            up = socket.create_connection(("127.0.0.1", self.proxy.upstream_port), timeout=UPSTREAM_TIMEOUT_S)
            up.settimeout(None)
            up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as e:
            log.warning("%s: rigctld not answering on %d (%s); closing", self.owner, self.proxy.upstream_port, e)
            self.send(f"RPRT -{EIO}\n".encode())
            return False
        with self._mu:
            if self._closed:  # the tick closed it meanwhile
                up.close()
                return False
            self.upstream = up
        try:
            threading.Thread(target=self._pump_up, name=f"{self.owner} up", daemon=True).start()
        except Exception:
            log.exception("%s: no thread for rigctld's replies; closing", self.owner)
            return False
        return True

    # -- traffic --

    def send(self, data: bytes) -> None:
        with self._write:
            self.client.sendall(data)

    def handle(self, raw: bytes) -> None:
        kind, sep, value = classify(raw.decode("latin-1"))
        if kind == "pass":
            self.upstream.sendall(raw)
        elif kind == "ptt":
            self.send(ptt_reply(sep, value, self.ptt(value != "0")))
        elif kind == "invalid":
            self.send(ptt_reply(sep, value, EINVAL))
        else:
            log.warning("%s: refused %r", self.owner, raw[:80])
            self.send(f"RPRT -{ERJCTED}\n".encode())

    def serve(self) -> None:
        """Client to rigctld, line by line. Runs on the session's own thread."""
        try:
            if self.upstream is not None:
                threading.Thread(target=self._pump_up, name=f"{self.owner} up", daemon=True).start()
        except Exception:  # "can't start new thread" and the like: close, or the session holds a slot for good
            log.exception("%s: no thread for rigctld's replies; closing", self.owner)
            self.close()
            return
        buf = b""
        try:
            while True:
                data = self.client.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not self.authed:
                        if not self._login(line):
                            return  # finally closes it
                        continue
                    first = line.split()[:1]
                    if first and _split(first[0].decode("latin-1"))[1:] == ("vb_auth", True):  # +\vb_auth too: never to rigctld
                        self.send(b"RPRT 0\n")
                        continue
                    self.handle(line + b"\n")
                if len(buf) > MAX_LINE:
                    log.warning("%s: line too long, closing", self.owner)
                    if not self.authed:
                        self._refuse_quietly()
                    break
        except OSError:
            pass
        finally:
            self.close()

    def _pump_up(self) -> None:
        """rigctld to client, unchanged."""
        try:
            while True:
                data = self.upstream.recv(4096)
                if not data:
                    break
                self.send(data)
        except OSError:
            pass
        finally:
            self.close()

    def close(self) -> None:
        with self._mu:
            if self._closed:
                return
            self._closed = True
            if self.keyed:
                self._unkey("client disconnected")
            socks = [x for x in (self.client, self.upstream) if x is not None]
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
        self.proxy._forget(self)
        log.info("%s: disconnected", self.owner)


class RigctlProxy:
    def __init__(self, ladder, binds: tuple[str, ...] = ("127.0.0.1",), port: int = PROXY_PORT,
                 upstream_port: int = DEFAULT_RIGCTLD_PORT, max_key_s: float = MAX_KEY_S,
                 tick_s: float = TICK_S, clock=time.monotonic, wifi_addrs_fn=wifi_addresses,
                 token_ok=None) -> None:
        self.ladder = ladder
        self.wifi_addrs_fn = wifi_addrs_fn
        self.token_ok = token_ok  # token -> bool; None: no token is ever valid
        self.binds, self.port, self.upstream_port = tuple(binds), port, upstream_port
        self.max_key_s, self.tick_s, self.clock = max_key_s, tick_s, clock
        self._sessions: list[Session] = []
        self._mu = threading.Lock()
        self._bind_mu = threading.Lock()  # start, rebind and stop change the listeners one at a time
        self._stop = threading.Event()
        self._listeners: list[tuple[str, socket.socket, threading.Thread]] = []
        self._threads: list[threading.Thread] = []

    @property
    def sessions(self) -> list[Session]:
        with self._mu:
            return list(self._sessions)

    @property
    def listening(self) -> list[str]:
        """The addresses actually listened on; one that could not be bound is missing."""
        with self._bind_mu:
            return [addr for addr, _, _ in self._listeners]

    def start(self) -> None:
        """Raises OSError when no address could be listened on. The tick still runs, so a later rebind works."""
        with self._bind_mu:
            self._stop.clear()
            t = threading.Thread(target=self._tick_loop, name="rigctl-proxy-tick", daemon=True)
            t.start()
            self._threads.append(t)
            failed = self._listen()
            if not self._listeners:
                raise OSError(f"rigctl proxy could not listen on port {self.port}: {'; '.join(failed)}")

    def rebind(self, binds: tuple[str, ...]) -> list[str]:
        """Listen on these addresses instead, at once. A client that came in on an address no longer
        listened on is closed, and unkeyed if keyed. Returns "address: reason" for each that failed."""
        with self._bind_mu:
            self._close_listeners()
            self.binds = tuple(binds)
            failed = self._listen()
        for s in self.sessions:
            if not self._allows(s.local, s.peer_ip):
                log.info("%s: closed, %s is no longer listened on", s.owner, s.local)
                s.close()
        return failed

    def stop(self) -> None:
        """Close every client, unkeying any that is keyed. Never raises."""
        self._stop.set()
        with self._bind_mu:
            self._close_listeners()
        for t in self._threads:
            t.join(timeout=2)
        self._threads = []
        for s in self.sessions:
            try:
                s.close()
            except Exception:
                log.exception("closing %s failed", s.owner)

    def _allows(self, local: str, peer: str = "") -> bool:
        binds = self.binds
        if EVERY_INTERFACE in binds:
            return True
        if local in binds:
            return local.startswith("127.") or not _tailnet(peer)
        return WIFI in binds and self._open_address(local, peer)

    def _open_address(self, local: str, peer: str = "") -> bool:
        """Loopback, or a Wi-Fi address reached from outside the tailnet. Linux takes a packet for the
        Wi-Fi address on any interface, so a tailnet peer (the Pi as a subnet router) could reach it."""
        if local.startswith("127."):  # 127.0.1.1: the hostname
            return True
        return local in self.wifi_addrs_fn() and not _tailnet(peer)

    def needs_token(self, local: str, peer: str = "") -> bool:
        """Every client but the Pi's own (the tuner's readiness check and the CLI on loopback)."""
        return not local.startswith("127.")

    def open_sessions(self) -> int:
        """Clients past the login, or with none to do: the ones that hold a rigctld connection."""
        with self._mu:
            return sum(1 for s in self._sessions if s.authed)

    def token_valid(self, token: str) -> bool:
        if self.token_ok is None:
            return False
        try:
            return bool(self.token_ok(token))
        except Exception:
            log.exception("rigctl proxy: token check failed")
            return False

    def _listen(self) -> list[str]:
        """Caller holds _bind_mu. Every address shares one port (a test's port 0 becomes the first one's)."""
        failed = []
        for addr in self.binds:
            addr = EVERY_INTERFACE if addr == WIFI else addr  # _allows picks the Wi-Fi clients out
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if addr != EVERY_INTERFACE and IP_FREEBIND is not None:
                    s.setsockopt(socket.IPPROTO_IP, IP_FREEBIND, 1)  # the AP address is there only while the AP is up
                s.bind((addr, self.port))
                s.listen(MAX_CLIENTS)
                s.settimeout(0.5)
                t = threading.Thread(target=self._accept_loop, args=(s,), name=f"rigctl-proxy {addr}", daemon=True)
                t.start()
            except (OSError, RuntimeError) as e:  # RuntimeError: "can't start new thread"
                s.close()
                log.warning("rigctl proxy: cannot listen on %s:%d (%s)", addr, self.port, e)
                failed.append(f"{addr}: {getattr(e, 'strerror', None) or e}")
                continue
            self.port = s.getsockname()[1]
            self._listeners.append((addr, s, t))
            log.info("rigctl proxy listening on %s:%d", addr, self.port)
        return failed

    def _close_listeners(self) -> None:
        """Caller holds _bind_mu. Clients already connected stay."""
        for _, s, t in self._listeners:
            try:
                s.shutdown(socket.SHUT_RDWR)  # wakes accept() at once on Linux
            except OSError:
                pass
            s.close()
            t.join(timeout=2)
        self._listeners = []

    def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        for s in self.sessions:
            s.tick(now)

    def _forget(self, session: Session) -> None:
        with self._mu:
            if session in self._sessions:
                self._sessions.remove(session)

    def _tick_loop(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                self.tick()
            except Exception:
                log.exception("proxy tick failed")

    def _accept_loop(self, listener: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                client, peer = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return  # listener closed by stop() or rebind()
            # One connection that fails to start must not end the loop: on 2026-10-04 "can't start
            # new thread" did, and 4532 kept listening with nobody accepting until a restart.
            try:
                self._admit(client, peer)
            except Exception:
                log.exception("rigctl proxy: %s could not start; closing it", peer)
                client.close()

    def _admit(self, client: socket.socket, peer: tuple) -> None:
        client.settimeout(None)
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        local = client.getsockname()[0]
        if not self._allows(local, peer[0]):  # the Wi-Fi setting on every interface: the tailnet, say
            log.warning("rigctl proxy: %s refused, it came in on %s, not loopback or Wi-Fi", peer, local)
            client.close()
            return
        must_log_in = self.needs_token(local, peer[0])
        open_now = self.open_sessions()
        if must_log_in and len(self.sessions) - open_now >= MAX_PENDING:
            log.warning("rigctl proxy: %s refused, %d clients already waiting to log in", peer, MAX_PENDING)
            client.close()
            return
        if not must_log_in and open_now >= MAX_CLIENTS:
            log.warning("rigctl proxy: %s refused, %d clients already", peer, MAX_CLIENTS)
            client.close()
            return
        upstream = None  # one that must log in gets rigctld only after it has
        if not must_log_in:
            try:
                upstream = socket.create_connection(("127.0.0.1", self.upstream_port), timeout=UPSTREAM_TIMEOUT_S)
            except OSError as e:
                log.warning("rigctl proxy: rigctld not answering on %d (%s); closing %s", self.upstream_port, e, peer)
                client.close()
                return
        try:
            if upstream is not None:
                upstream.settimeout(None)
                upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            session = Session(self, client, peer, upstream, must_log_in)  # reads the client's address: a reset peer raises
        except Exception:
            if upstream is not None:
                upstream.close()  # the caller closes the client
            raise
        with self._mu:
            self._sessions.append(session)
        log.info("%s: connected", session.owner)
        if not self._allows(session.local, session.peer_ip):  # accepted just before a rebind that missed it in its sweep
            session.close()
            return
        try:
            threading.Thread(target=session.serve, name=session.owner, daemon=True).start()
        except Exception:
            session.close()  # frees its slot and its rigctld connection
            raise
