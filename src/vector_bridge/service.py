"""The bridge service: capture, playback, UDP and supervisor threads (proposal section 18).

    capture   : backend.read() -> levels -> RX packet -> peer and monitors
    playback  : jitter.pop() (or the test tone while it plays) -> soft gain (cards with no playback volume) -> levels -> TX audio cap -> backend.write()
    udp       : TX packets -> jitter; control datagrams -> ControlHandler
    supervisor: reopen the audio device with backoff when either audio thread reports failure
"""

from __future__ import annotations

import collections
import select
import socket
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from . import config as configmod
from . import softgain
from .audio import AudioDeviceError, open_backend
from .control import ControlHandler
from .jitter import JitterBuffer
from .log import error, log, warn
from .protocol import DIR_RX, DIR_TX, FRAME_MS, AudioPacket, ProtocolError, decode
from .telemetry import SendMeter, Telemetry, measure
from .tone import ToneSource
from .txcap import DEFAULT_CAP_S, MAX_CAP_S, TxAudioCap

PEER_TIMEOUT_S = 5.0
MAX_CLIENT_NAMES = 64  # client strings remembered by sending address (DOCS/app-listing.md)
MAX_MONITORS = 2  # listen-only RX destinations beside the peer, e.g. JS8Call on a Mac for decode checks
JITTER_WARN_MS = 20.0  # a TX packet this far off its 10 ms slot is "excessive jitter"
REOPEN_BACKOFF = (0.5, 1, 2, 4, 8, 10)
HEALTHY_S = 10.0  # a reopened device that fails sooner than this keeps its backoff step
STALL_WARN_MS = 40.0  # one capture step this long outlasts the ALSA capture buffer (4 x 10 ms periods)
FRAME_S = FRAME_MS / 1000


def _note_stall(step: str, since: float, **fields) -> None:
    """Warn when one capture-loop step took longer than the capture buffer holds, naming the step."""
    ms = (time.monotonic() - since) * 1000
    if ms > STALL_WARN_MS:
        warn("capture_stall", step=step, ms=round(ms, 1), every=1.0, **fields)


class CaptureClock:
    """Count the 10 ms frames an ALSA overrun threw away, from when reads return.

    While capture keeps up, (read return time - frames read x 10 ms) holds steady. An
    overrun discards audio but not time, so that offset jumps by what was lost. The
    reference is the earliest offset over the last WINDOW frames, which keeps the
    codec-versus-Pi clock drift of a long session out of the estimate.
    """

    WINDOW = 200  # frames, 2 s

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Forget the reference, e.g. after a device reopen leaves a gap that is no overrun."""
        self._frames = 0
        self._recent: collections.deque[float] = collections.deque(maxlen=self.WINDOW)

    def frame(self, t: float, overran: bool) -> int:
        """Note one frame read at monotonic time t. Returns the frames lost before it if an overrun preceded it."""
        offset = t - self._frames * FRAME_S
        lost = 0
        if overran and self._recent:
            lost = max(0, round((offset - min(self._recent)) / FRAME_S))
            self._frames += lost
            offset -= lost * FRAME_S
        self._frames += 1
        self._recent.append(offset)
        return lost


def _utc(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="milliseconds")


class SendBursts:
    """Each RX destination's run of failed sends, from its first failure until a send goes
    through after QUIET_S with no failure. rx_send_failed is rate-limited and its suppressed=
    count cannot say when the dropped packets fell; one rx_send_burst line per run names the
    decode slots a Wi-Fi stall cost that client. The quiet gap keeps a flapping link to one
    line instead of one per good packet."""

    QUIET_S = 1.0

    def __init__(self) -> None:
        self._open: dict[tuple, list] = {}  # dest -> [first UTC, last UTC, failed sends]

    def failed(self, dest: tuple, now: float) -> None:
        run = self._open.get(dest)
        if run is None:
            self._open[dest] = [now, now, 1]
        else:
            run[1] = now
            run[2] += 1

    def sent(self, dest: tuple) -> None:
        if self._open:
            run = self._open.get(dest)
            if run is not None and time.time() - run[1] >= self.QUIET_S:
                self._close(dest)

    def keep_only(self, dests: list[tuple]) -> None:
        """Close the runs of destinations that are gone, e.g. a monitor that timed out mid-stall."""
        for dest in [d for d in self._open if d not in dests]:
            self._close(dest)

    def _close(self, dest: tuple) -> None:
        run = self._open.pop(dest, None)
        if run is not None:
            warn("rx_send_burst", dest=f"{dest[0]}:{dest[1]}", first=_utc(run[0]), last=_utc(run[1]),
                 failed=run[2])


class BridgeService:
    def __init__(self, cfg: configmod.Config, audio: str = "alsa", bind: str = "0.0.0.0",
                 config_path: Path | None = None) -> None:
        self.cfg = cfg
        self.audio_name = audio
        self.bind = bind
        self.config_path = config_path
        tx = cfg.tx_gain
        if isinstance(tx, bool) or not isinstance(tx, int) or not 0 <= tx <= 100:
            # An out-of-range value (INT32 garbage from a switch-only mixer) must not clamp to 100,
            # which is +10 dB on a soft-gain card: fall back to the cautious default instead,
            # before the backend copies it.
            default = configmod.Config().tx_gain
            warn("bad_tx_gain", value=repr(tx), using=default)
            cfg.tx_gain = default
        self.backend = open_backend(audio, cfg)
        self.jitter = JitterBuffer(cfg.jitter_target_ms, cfg.jitter_max_ms)
        self.telemetry = Telemetry()
        self.tone = ToneSource()
        cap = cfg.tx_audio_cap_s
        if isinstance(cap, bool) or not isinstance(cap, int) or not 0 <= cap <= MAX_CAP_S:
            warn("bad_tx_audio_cap", value=repr(cap), using=DEFAULT_CAP_S)
            cfg.tx_audio_cap_s = cap = DEFAULT_CAP_S
        self.tx_cap = TxAudioCap(cap)
        self.control = ControlHandler(cfg.device_name, cfg.audio_port, self.status,
                                      self.set_gain, self._register_peer, self.play_tone,
                                      self.set_tx_audio_cap, self._register_monitor, self.select_audio,
                                      self.set_rigctl_access)
        self._peer: tuple | None = None
        self._peer_seen = 0.0
        self._monitors: dict[tuple, float] = {}  # addr -> last seen; guarded by _peer_lock
        self._client_names: dict[tuple, str] = {}  # addr -> client string of its last hello/monitor, oldest first; _peer_lock
        self._peer_lock = threading.Lock()
        self._rx_seq = 0
        self._capture_clock = CaptureClock()
        self._rx_last_stamp = 0.0  # seconds, the timestamp of the last frame captured
        self._send_bursts = SendBursts()  # capture thread only
        self._stop = threading.Event()
        self._device_ok = threading.Event()  # set while the backend is open and healthy
        self._device_failed = threading.Event()
        self._audio_choice = 0  # bumped by select_audio; an open that started before it reopens on the new card
        self._threads: list[threading.Thread] = []
        self.audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ctl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for s in (self.audio_sock, self.ctl_sock):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.audio_sock.bind((bind, cfg.audio_port))
        self.ctl_sock.bind((bind, cfg.control_port))
        self.audio_port = self.audio_sock.getsockname()[1]
        self.control_port = self.ctl_sock.getsockname()[1]
        self.control.audio_port = self.audio_port

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        try:
            self._open_device()
        except AudioDeviceError as e:
            error("device_disconnect", where="startup", reason=str(e), device=self.backend.describe())
            self.telemetry.set_device(False, str(e))
            self._device_failed.set()
        log("service_start", audio=self.audio_name, device=self.backend.describe(),
            audio_port=self.audio_port, control_port=self.control_port,
            tx_gain=self.cfg.tx_gain, rx_gain=self.cfg.rx_gain,
            jitter_target_ms=self.cfg.jitter_target_ms, tx_audio_cap_s=self.cfg.tx_audio_cap_s)
        for name, fn in (("capture", self._capture_loop), ("playback", self._playback_loop),
                         ("udp", self._udp_loop), ("supervisor", self._supervisor_loop)):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._device_failed.set()  # wake the supervisor so it can exit
        for t in self._threads:
            t.join(timeout=2)
        self._send_bursts.keep_only([])  # log a stall still open at shutdown
        try:
            self.backend.close()
        except Exception as e:  # noqa: BLE001 - the driver may raise anything; we are exiting anyway
            log("backend_close_error", reason=repr(e))
        self.audio_sock.close()
        self.ctl_sock.close()
        log("service_stop")

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                self._stop.wait(1)
        finally:
            self.stop()

    # ---- peer ------------------------------------------------------------

    def _register_peer(self, addr: tuple, client: str | None = None) -> None:
        """`client` is the hello's client string; None when TX audio registered the peer."""
        now = time.monotonic()
        with self._peer_lock:
            if client is not None:
                self._note_client(addr, client)
            changed = addr != self._peer
            self._peer = addr
            self._monitors.pop(addr, None)  # a hello from a monitor makes it the peer
            self._peer_seen = now
        if changed:
            self.telemetry.set(peer=f"{addr[0]}:{addr[1]}")
            self.telemetry.forget_tx_arrival()
            self.jitter.reset()  # the old peer's sequence would make the new one's audio "late"
            log("peer_registered", peer=f"{addr[0]}:{addr[1]}", client=client or "")

    def _touch_peer(self, addr: tuple) -> None:
        with self._peer_lock:
            if addr == self._peer:
                self._peer_seen = time.monotonic()
                return
        self._register_peer(addr)

    def _current_peer(self) -> tuple | None:
        with self._peer_lock:
            if self._peer is None:
                return None
            if time.monotonic() - self._peer_seen > PEER_TIMEOUT_S:
                dropped, self._peer = self._peer, None
                self.telemetry.set(peer=None)
                self.telemetry.forget_tx_arrival()
            else:
                return self._peer
        log("peer_dropped", peer=f"{dropped[0]}:{dropped[1]}", timeout_s=PEER_TIMEOUT_S)
        return None

    # ---- monitors --------------------------------------------------------

    def _register_monitor(self, addr: tuple, client: str = "") -> str | None:
        """Add or refresh a listen-only RX destination; return an error code or None.
        A monitor never takes the peer slot, and its TX packets are dropped (_on_audio)."""
        with self._peer_lock:
            if addr == self._peer:
                return "is_peer"
            dropped = self._expire_monitors()
            new = addr not in self._monitors
            if new and len(self._monitors) >= MAX_MONITORS:
                full = True
            else:
                full = False
                self._monitors[addr] = time.monotonic()
                self._note_client(addr, client)  # only an accepted monitor names a stream
        self._log_monitor_drops(dropped)
        if full:
            warn("monitors_full", monitor=f"{addr[0]}:{addr[1]}", client=client, max=MAX_MONITORS, every=5.0)
            return "monitors_full"
        if new:
            log("monitor_registered", monitor=f"{addr[0]}:{addr[1]}", client=client)
        return None

    def _expire_monitors(self) -> list[tuple]:
        """Drop monitors silent for PEER_TIMEOUT_S. Caller holds _peer_lock."""
        now = time.monotonic()
        dropped = [a for a, seen in self._monitors.items() if now - seen > PEER_TIMEOUT_S]
        for a in dropped:
            del self._monitors[a]
        return dropped

    def _log_monitor_drops(self, dropped: list[tuple]) -> None:
        for a in dropped:
            log("monitor_dropped", monitor=f"{a[0]}:{a[1]}", timeout_s=PEER_TIMEOUT_S)

    def _is_monitor(self, addr: tuple) -> bool:
        with self._peer_lock:
            return addr in self._monitors

    # ---- client names (DOCS/app-listing.md) ------------------------------

    def _note_client(self, addr: tuple, client: str) -> None:
        """Remember the client string `addr` sent, newest last. Caller holds _peer_lock."""
        self._client_names.pop(addr, None)
        self._client_names[addr] = client[:64]  # a name, not a payload: keeps the status reply small
        while len(self._client_names) > MAX_CLIENT_NAMES:
            del self._client_names[next(iter(self._client_names))]

    def _client_of(self, addr: tuple) -> str:
        """The stream's own client string, else the newest from any port on its IP, else "".
        The fallback labels apps that send hello from a control socket and audio from another.
        Caller holds _peer_lock."""
        if addr in self._client_names:
            return self._client_names[addr]
        same_ip = [c for a, c in self._client_names.items() if a[0] == addr[0]]
        return same_ip[-1] if same_ip else ""

    def _rx_destinations(self) -> list[tuple]:
        peer = self._current_peer()
        with self._peer_lock:
            dropped = self._expire_monitors()
            monitors = list(self._monitors)
        self._log_monitor_drops(dropped)
        return ([peer] if peer is not None else []) + monitors

    # ---- device supervision ---------------------------------------------

    def _open_device(self) -> None:
        choice = self._audio_choice
        self.backend.open()
        self.backend.set_gain(tx=self.cfg.tx_gain, rx=self.cfg.rx_gain)
        if self.backend.tx_soft:
            log("tx_soft_gain", tx_gain=self.cfg.tx_gain, gain_db=softgain.gain_db(self.cfg.tx_gain))
            if self.cfg.tx_gain == 0:
                warn("tx_soft_gain_muted", reason="tx_gain is 0 on a card with no playback volume; nothing will transmit")
        self.telemetry.set(alsa_device=self.backend.describe())
        self.telemetry.set_device(True)
        self._device_failed.clear()
        self._device_ok.set()
        if choice != self._audio_choice:  # setup chose another card while this one was opening
            self._device_ok.clear()
            self.telemetry.set_device(False, "switching sound card")
            self._device_failed.set()

    def _report_device_failure(self, where: str, exc: Exception) -> None:
        if self._device_ok.is_set():
            self._device_ok.clear()
            self.telemetry.set_device(False, str(exc))
            error("device_disconnect", where=where, reason=str(exc), device=self.backend.describe())
            self._device_failed.set()

    def _supervisor_loop(self) -> None:
        attempt = 0
        opened_at = time.monotonic()
        while not self._stop.is_set():
            self._device_failed.wait()
            if self._stop.is_set():
                return
            try:
                self.backend.close()
            except Exception as e:  # noqa: BLE001 - a failed device is being discarded; log and move on
                log("backend_close_error", reason=repr(e))
            # A device that opens but fails again at once keeps climbing the backoff
            # instead of flapping at the first step; one that ran a while starts over.
            if time.monotonic() - opened_at >= HEALTHY_S:
                attempt = 0
            while not self._stop.is_set():
                delay = REOPEN_BACKOFF[min(attempt, len(REOPEN_BACKOFF) - 1)]
                if self._stop.wait(delay):
                    return
                attempt += 1
                try:
                    self._open_device()
                except AudioDeviceError as e:
                    self.telemetry.set_device(False, str(e))
                    warn("device_reopen_failed", attempt=attempt, reason=str(e), next_s=delay)
                    continue
                opened_at = time.monotonic()
                self.telemetry.update(device_reopens=1)
                log("device_reconnect", attempt=attempt, device=self.backend.describe())
                break

    # ---- audio threads ---------------------------------------------------

    def _capture_loop(self) -> None:
        sock = self.audio_sock
        while not self._stop.is_set():
            if not self._device_ok.wait(0.2):
                self._capture_clock.reset()  # the reopen gap is not an overrun
                continue
            t0 = time.monotonic()
            try:
                pcm = self.backend.read()
            except AudioDeviceError as e:
                self._capture_clock.reset()
                self._report_device_failure("capture", e)
                continue
            t_read = time.monotonic()  # before the stall warning, whose write could skew the overrun estimate
            _note_stall("read", t0)
            t_prep = time.monotonic()
            lv = measure(pcm)
            self.telemetry.set(rx_peak=lv.peak, rx_rms=lv.rms)
            if lv.clips:
                self.telemetry.update(clip_count=lv.clips)
                warn("audio_clipping", direction="rx", clips=lv.clips, peak=lv.peak, every=1.0)
            over = self.backend.overruns
            overran = over > self.telemetry.rx_overruns
            # Skip the sequence numbers of the audio the overrun discarded, so clients see an
            # ordinary packet gap and pad it with silence instead of running early from here on.
            lost = self._capture_clock.frame(t_read, overran)
            self._rx_seq = (self._rx_seq + lost) & 0xFFFFFFFF
            # t0 can predate the stall, so a frame after a skip is stamped one gap past the last
            # frame: clients fill the gap at 10 ms a packet and it must meet this stamp.
            stamp = self._rx_last_stamp + (lost + 1) * FRAME_S if lost else t0
            self._rx_last_stamp = stamp
            if overran:
                self.telemetry.set(rx_overruns=over)
                warn("alsa_overrun", total=over, lost_packets=lost, every=1.0)
            dests = self._rx_destinations()
            self._send_bursts.keep_only(dests)
            _note_stall("prep", t_prep)  # levels, telemetry and the peer lock between read and send
            if not dests:
                continue
            data = AudioPacket(DIR_RX, self._rx_seq, int(stamp * 1e6), pcm).encode()
            self._rx_seq = (self._rx_seq + 1) & 0xFFFFFFFF
            for dest in dests:  # the same packet and sequence to each, so decodes line up
                t_send = time.monotonic()
                try:
                    # Never wait on the network: a stalled Wi-Fi link to one client filled the
                    # send queue and froze capture for everyone (349 ms, 2026-10-02). A full
                    # queue raises BlockingIOError and that client loses this one packet.
                    sock.sendto(data, socket.MSG_DONTWAIT, dest)
                    self.telemetry.update(rx_packets_sent=1)
                    self._send_bursts.sent(dest)
                except OSError as e:
                    self.telemetry.update(rx_packet_loss=1)
                    self._send_bursts.failed(dest, time.time())
                    warn("rx_send_failed", dest=f"{dest[0]}:{dest[1]}", reason=str(e), every=5.0)  # rate-limited per dest
                _note_stall("sendto", t_send, dest=f"{dest[0]}:{dest[1]}")

    def _playback_loop(self) -> None:
        j = self.jitter
        last_underruns = last_lost = last_late = 0
        send = SendMeter()
        while not self._stop.is_set():
            if not self._device_ok.wait(0.2):
                continue
            live = j.primed
            pcm = j.pop()  # drained even under the tone, so client audio does not pile up behind it
            tone = self.tone.frame()
            if tone is not None:
                pcm = tone
            soft = self.backend.tx_soft
            if soft:
                g = softgain.gain(self.cfg.tx_gain)
                # the tone already plays at 0.9 FS: never boost it, or it would saturate
                pcm = softgain.apply(pcm, min(g, 1.0) if tone is not None else g)
            st = j.stats
            if st.underruns != last_underruns:
                self.telemetry.update(tx_underruns=st.underruns - last_underruns)
                last_underruns = st.underruns
                warn("alsa_underrun", source="jitter_empty", total=st.underruns, every=1.0)
            if st.lost != last_lost:
                self.telemetry.update(tx_packet_loss=st.lost - last_lost)
                last_lost = st.lost
                warn("udp_loss", direction="tx", total=st.lost, every=1.0)
            if st.late != last_late:
                last_late = st.late
                warn("excessive_jitter", reason="packet_too_late_for_playback", total=st.late, every=1.0)
            self.telemetry.set(jitter_depth=st.depth, tx_late=st.late, tx_reordered=st.reordered)
            lv = measure(pcm)
            if live or tone is not None:
                send.add(lv)
            elif (summary := send.idle()) is not None:
                log("tx_send", **summary, soft=soft, gain_db=softgain.gain_db(self.cfg.tx_gain) if soft else None)
            self.telemetry.set(tx_peak=lv.peak, tx_rms=lv.rms)
            if lv.clips:
                self.telemetry.update(clip_count=lv.clips)
                warn("audio_clipping", direction="tx", clips=lv.clips, peak=lv.peak, every=1.0)
            pcm, tripped = self.tx_cap.apply(pcm, lv.peak)
            if tripped:
                warn("tx_audio_capped", cap_s=self.tx_cap.cap_s)
            try:
                self.backend.write(pcm)
            except AudioDeviceError as e:
                self._report_device_failure("playback", e)
                continue
            under = self.backend.underruns
            if under:
                # backend-level underruns are folded into the same counter
                delta = under - getattr(self, "_be_underruns", 0)
                if delta:
                    self._be_underruns = under
                    self.telemetry.update(tx_underruns=delta)
                    warn("alsa_underrun", source="alsa", total=under, every=1.0)

    # ---- network ---------------------------------------------------------

    def _udp_loop(self) -> None:
        socks = [self.audio_sock, self.ctl_sock]
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select(socks, [], [], 0.2)
            except (OSError, ValueError):
                return
            for s in ready:
                try:
                    data, addr = s.recvfrom(4096)
                except OSError:
                    continue
                try:
                    if s is self.ctl_sock:
                        reply = self.control.handle_bytes(data, addr)
                        s.sendto(reply, addr)
                    else:
                        self._on_audio(data, addr)
                except Exception as e:  # noqa: BLE001 - never let the udp thread die
                    error("udp_handler_error", reason=repr(e), every=1.0)

    def _on_audio(self, data: bytes, addr: tuple) -> None:
        try:
            pkt = decode(data)
        except ProtocolError as e:
            warn("bad_packet", reason=str(e), peer=f"{addr[0]}:{addr[1]}", every=5.0)
            return
        if pkt.direction != DIR_TX:
            return
        if self._is_monitor(addr):
            # listen-only: a monitor must never reach playback (a VOX rig would key on it)
            if len(pkt.pcm):
                warn("monitor_tx_dropped", monitor=f"{addr[0]}:{addr[1]}", every=5.0)
            return
        self._touch_peer(addr)
        if len(pkt.pcm) == 0:
            return  # keepalive: sample_count 0 registers/refreshes the peer, carries no audio (DOCS/protocol.md)
        self.telemetry.update(tx_packets_received=1)
        dev = self.telemetry.note_tx_arrival(time.monotonic())
        if dev > JITTER_WARN_MS:
            warn("arrival_jitter", deviation_ms=dev, every=1.0)
        self.jitter.push(pkt)

    # ---- control callbacks ----------------------------------------------

    def status(self) -> dict:
        """Counters plus the levels currently applied, so a client can show them without guessing."""
        now = time.monotonic()
        with self._peer_lock:
            monitors = [f"{a[0]}:{a[1]}" for a in self._monitors]
            # Live streams only: the expiry the send path applies lazily, read here without dropping anything.
            live = ([(self._peer, "peer")] if self._peer is not None and now - self._peer_seen <= PEER_TIMEOUT_S else []) + \
                [(a, "monitor") for a, seen in self._monitors.items() if now - seen <= PEER_TIMEOUT_S]
            clients = [{"addr": f"{a[0]}:{a[1]}", "role": role, "client": self._client_of(a)} for a, role in live]
        return {**self.telemetry.as_dict(), "monitors": monitors, "clients": clients,
                "tx_gain": self.cfg.tx_gain, "rx_gain": self.cfg.rx_gain, "tx_soft_gain": self.backend.tx_soft,
                "rx_gain_fixed": self.backend.rx_fixed,
                "tx_audio_cap_s": self.tx_cap.cap_s, "tx_audio_muted": self.tx_cap.muted,
                "tx_audio_cap_trips": self.tx_cap.trips,
                # TX audio not yet out of the sound card: a deliberate unkey holds PTT this long (REQ-AUDIO-022)
                "tx_queued_ms": self.jitter.stats.depth * FRAME_MS + self.backend.playback_buffer_ms}

    def set_gain(self, tx: int | None, rx: int | None) -> dict:
        applied = self.backend.set_gain(tx=tx, rx=rx)
        self.cfg.tx_gain, self.cfg.rx_gain = applied["tx"], applied["rx"]
        log("gain_set", tx=applied["tx"], rx=applied["rx"])
        try:
            configmod.save(self.cfg, self.config_path)
        except OSError as e:
            warn("config_save_failed", path=str(self.config_path or configmod.DEFAULT_PATH), reason=str(e))
        return applied

    def select_audio(self, vendor: str, product: str, usb_path: str, strict: bool = False) -> dict:
        """Setup chose the sound card: keep it in config and reopen on it (about half a second)."""
        cfg = self.cfg
        if (cfg.usb_vendor_id, cfg.usb_product_id, cfg.audio_usb_path, cfg.audio_usb_strict) \
                == (vendor, product, usb_path, strict) and self._device_ok.is_set():
            return {"changed": False}
        cfg.usb_vendor_id, cfg.usb_product_id, cfg.audio_usb_path = vendor, product, usb_path
        cfg.audio_usb_strict = strict
        self._audio_choice += 1
        log("audio_select", usb=f"{vendor or '*'}:{product or '*'}", usb_path=usb_path or "-", strict=strict)
        try:
            configmod.save(cfg, self.config_path)
        except OSError as e:
            warn("config_save_failed", path=str(self.config_path or configmod.DEFAULT_PATH), reason=str(e))
        if self._device_ok.is_set():  # the supervisor closes the old card and opens the chosen one
            self._device_ok.clear()
            self.telemetry.set_device(False, "switching sound card")
            self._stop.wait(0.1)  # let a read or write already in flight finish (a period is 10 ms)
            self._device_failed.set()
        return {"changed": True}

    def set_tx_audio_cap(self, seconds: int) -> dict:
        self.tx_cap.cap_s = self.cfg.tx_audio_cap_s = seconds
        log("tx_audio_cap_set", seconds=seconds)
        try:
            configmod.save(self.cfg, self.config_path)
        except OSError as e:
            warn("config_save_failed", path=str(self.config_path or configmod.DEFAULT_PATH), reason=str(e))
        return {"tx_audio_cap_s": seconds}

    def set_rigctl_access(self, access: str) -> dict:
        """Kept for the tuner, which listens on 4532: it reads the setting at its start.
        Unlike a gain, it changes who can reach the radio, so an unsaved change is refused, not kept."""
        cfg = replace(self.cfg, rigctl_access=access)
        try:
            configmod.save(cfg, self.config_path)
        except OSError as e:
            warn("config_save_failed", path=str(self.config_path or configmod.DEFAULT_PATH), reason=str(e))
            return {"ok": False, "error": "config_save_failed"}
        self.cfg.rigctl_access = access
        log("rigctl_access_set", access=access)
        return {"rigctl_access": access}

    def play_tone(self, hz: float, seconds: float) -> dict:
        """Play the test tone in place of client audio for `seconds`; 0 stops it."""
        if seconds > 0:
            self.tone.start(hz, seconds)
        else:
            self.tone.stop()
        log("tone", hz=hz, seconds=seconds)
        return {"tone_hz": hz, "tone_s": seconds}
