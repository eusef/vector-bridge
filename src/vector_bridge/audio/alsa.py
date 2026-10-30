"""ALSA backend for the Pi: pyalsaaudio PCM capture/playback plus mixer gain.

Card selection is by USB identity (vendor:product from /proc/asound/cardN/usbid),
never by assuming hw:1,0. `alsaaudio` is imported lazily so the package imports on
machines without libasound (the Mac) and so tests can inject a stub module.

Error mapping (pyalsaaudio 0.11 semantics, checked against alsaaudio.c):
- read() returns (frames, bytes); frames < 0 is an overrun that ALSA already recovered
  from (counted, not fatal); frames == 0 is EAGAIN. DEAD_READS_LIMIT of either in a
  row with no audio between them is fatal: the stream will not start.
- write() returns frames written; < 0 is an underrun already recovered (counted).
- Any ALSAAudioError (ENODEV when the UGreen is unplugged, EBADFD, ...) raises
  AudioDeviceError so the supervisor closes and reopens with backoff.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import usbtopo
from ..protocol import FRAME_SAMPLES, SAMPLE_RATE
from .base import AudioBackend, AudioDeviceError

PERIOD_FRAMES = FRAME_SAMPLES  # 480 = 10 ms
PERIODS = 4  # buffer = 4 periods = 40 ms; report what held in the summary
# A healthy stream recovers from an overrun on the next read. A stream the kernel
# refuses to start (IC-705 codec: "cannot submit urb ... endpoint not enabled") fails
# every read in about 1 ms; without this bound read() spun forever at 100% CPU.
DEAD_READS_LIMIT = 50

# Mixer control names to try, in order, until one exists with the right capability.
# The UGreen names are discovered with `amixer -c N scontrols`; these are fallbacks
# for the common USB Audio Class names, not guesses about a specific product.
PLAYBACK_CONTROLS = ("PCM", "Speaker", "Headphone", "Master")
CAPTURE_CONTROLS = ("Mic", "Capture", "Line")


def _has_volume(caps, direction: str) -> bool:
    """volumecap() names a volume for this direction ('Volume'/'Joined Volume' cover both)."""
    side = "Playback" if direction == "tx" else "Capture"
    return any(side in c or c in ("Volume", "Joined Volume") for c in caps)

# Sample formats the backend can carry, as bytes per sample. hw: never converts, so a
# card that only does S24_3LE (QRP Labs QMX) comes back S24_3LE despite the S16 request;
# read as int16 that audio was scrambled and ran 1.5x fast (2026-10-02).
SAMPLE_WIDTHS = {"S16_LE": 2, "S24_3LE": 3}


@dataclass
class CardInfo:
    index: int
    id: str  # short ALSA id, e.g. "Device"
    name: str  # long name, e.g. "USB Audio Device"
    usb_vendor_id: str = ""  # hex, lowercase, no 0x
    usb_product_id: str = ""
    usb_path: str = ""  # where it sits on the USB bus, e.g. "1-1.2"; tells two identical cards apart

    @property
    def is_usb(self) -> bool:
        return bool(self.usb_vendor_id)

    @property
    def usb_id(self) -> str:
        return f"{self.usb_vendor_id}:{self.usb_product_id}" if self.is_usb else "-"


_CARD_LINE = re.compile(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s*(\S+)\s*-\s*(.*)$")


def list_cards(proc_asound: Path = Path("/proc/asound"), sys_sound: Path = usbtopo.SYS_SOUND) -> list[CardInfo]:
    """Parse /proc/asound/cards and each card's usbid file. Works without alsaaudio."""
    cards: list[CardInfo] = []
    cards_file = proc_asound / "cards"
    if not cards_file.exists():
        return cards
    for line in cards_file.read_text().splitlines():
        m = _CARD_LINE.match(line)
        if not m:
            continue
        idx, short_id, _driver, name = m.groups()
        info = CardInfo(index=int(idx), id=short_id, name=name.strip())
        usbid = proc_asound / f"card{idx}" / "usbid"
        if usbid.exists():
            vid, _, pid = usbid.read_text().strip().lower().partition(":")
            info.usb_vendor_id, info.usb_product_id = vid, pid
            info.usb_path = usbtopo.card_device(info.index, sys_sound)
        cards.append(info)
    return cards


def select_card(cards: list[CardInfo], vendor_id: str = "", product_id: str = "",
                usb_path: str = "", strict: bool = False) -> CardInfo | None:
    """Pick the configured USB card, or the first USB audio card when config is empty.

    A configured USB position wins when a card of that id is still there; a card moved to
    another socket is then found by its id alone, unless ``strict`` (the card had an
    identical twin when it was chosen, so a card of that id elsewhere may be the twin).
    """
    vendor_id, product_id = vendor_id.strip().lower(), product_id.strip().lower()
    if usb_path:
        at = next((c for c in cards if c.is_usb and c.usb_path == usb_path
                   and (not vendor_id or c.usb_vendor_id == vendor_id)
                   and (not product_id or c.usb_product_id == product_id)), None)
        if at is not None or strict:
            return at
    for c in cards:
        if not c.is_usb:
            continue
        if vendor_id and c.usb_vendor_id != vendor_id:
            continue
        if product_id and c.usb_product_id != product_id:
            continue
        return c
    return None


def _alsa():
    mod = sys.modules.get("alsaaudio")
    if mod is None:
        try:
            import alsaaudio as mod  # type: ignore
        except ImportError as e:  # pragma: no cover - Mac
            raise AudioDeviceError(f"pyalsaaudio not installed: {e}") from e
    return mod


def _to_int16(data: bytes, fmt: str) -> np.ndarray:
    """Interleaved samples as int16. S24_3LE keeps its top 16 bits (bytes 1-2 of each 3)."""
    if fmt == "S16_LE":
        return np.frombuffer(data, dtype="<i2")
    raw = np.frombuffer(data, dtype=np.uint8)
    raw = raw[: raw.size - raw.size % 3].reshape(-1, 3)
    return np.ascontiguousarray(raw[:, 1:]).view("<i2").reshape(-1)


def _from_int16(pcm: np.ndarray, fmt: str) -> bytes:
    """int16 samples as the card's bytes. S24_3LE gets a zero low byte."""
    pcm = np.ascontiguousarray(pcm, dtype="<i2")
    if fmt == "S16_LE":
        return pcm.tobytes()
    out = np.zeros((pcm.size, 3), dtype=np.uint8)
    out[:, 1:] = pcm.view(np.uint8).reshape(-1, 2)
    return out.tobytes()


class AlsaBackend(AudioBackend):
    def __init__(self, config, proc_asound: Path = Path("/proc/asound")) -> None:
        self.cfg = config
        self.proc_asound = proc_asound
        self.card: CardInfo | None = None
        self._cap = None
        self._play = None
        self._cap_channels = 1
        self._play_channels = 1
        self._cap_format = self._play_format = "S16_LE"
        self._overruns = 0
        self._underruns = 0
        self._gain = {"tx": config.tx_gain, "rx": config.rx_gain}
        self._mixer_names = {"tx": None, "rx": None}
        self._tx_soft = False
        self._rx_fixed = False
        self._pending = np.zeros(0, dtype=np.int16)
        self._play_period = PERIOD_FRAMES  # what the card settled on; a rounded period lengthens the buffer

    # ---- lifecycle --------------------------------------------------------

    def open(self) -> None:
        cards = list_cards(self.proc_asound)
        card = select_card(cards, self.cfg.usb_vendor_id, self.cfg.usb_product_id,
                           getattr(self.cfg, "audio_usb_path", ""), getattr(self.cfg, "audio_usb_strict", False))
        if card is None:
            want = f"{self.cfg.usb_vendor_id or '*'}:{self.cfg.usb_product_id or '*'}"
            raise AudioDeviceError(f"no USB audio card matching {want} (cards: {[c.id for c in cards]})")
        self.card = card
        a = _alsa()
        try:
            self._cap, self._cap_channels, self._cap_format = self._open_pcm(a, a.PCM_CAPTURE)
            self._play, self._play_channels, self._play_format = self._open_pcm(a, a.PCM_PLAYBACK)
        except a.ALSAAudioError as e:
            self.close()
            raise AudioDeviceError(f"open hw:{card.index}: {e}") from e
        self._pending = np.zeros(0, dtype=np.int16)

    def _open_pcm(self, a, kind: int):
        """Open hw:N,0 mono; fall back to stereo when the card only does 2 channels."""
        last = None
        for channels in (1, 2):
            try:
                pcm = a.PCM(type=kind, mode=a.PCM_NORMAL, rate=SAMPLE_RATE, channels=channels,
                            format=a.PCM_FORMAT_S16_LE, periodsize=PERIOD_FRAMES, periods=PERIODS,
                            device=f"hw:{self.card.index},0")
            except a.ALSAAudioError as e:
                last = e
                continue
            # pyalsaaudio does not fail when the card cannot do what was asked; it
            # negotiates the nearest and only info() tells the truth. A C-Media card
            # asked for mono silently comes back stereo, which would play every
            # packet at double speed. Verify, and fall through to the next layout.
            try:
                got = self._negotiated(pcm, a)
            except Exception as e:  # noqa: BLE001 - info() itself failing: treat like an open failure
                pcm.close()
                last = a.ALSAAudioError(f"info() after open: {e}")
                continue
            if got[3] not in SAMPLE_WIDTHS:
                pcm.close()
                raise a.ALSAAudioError(f"card negotiated sample format {got[3]}; "
                                       f"only {', '.join(SAMPLE_WIDTHS)} are supported")
            if got[:2] == (channels, SAMPLE_RATE):
                if got[2] != PERIOD_FRAMES:
                    # read() re-blocks to 480 and write() tolerates any period, so a
                    # card that rounds the period only costs latency; record it.
                    self._period_note = f" hwperiod={got[2]}"
                if kind == a.PCM_PLAYBACK:
                    self._play_period = got[2] or PERIOD_FRAMES
                return pcm, channels, got[3]
            pcm.close()
            last = a.ALSAAudioError(f"asked {channels}ch/{SAMPLE_RATE}Hz, "
                                    f"card negotiated {got[0]}ch/{got[1]}Hz")
        raise last  # type: ignore[misc]

    @property
    def playback_buffer_ms(self) -> int:
        return PERIODS * self._play_period * 1000 // SAMPLE_RATE

    @staticmethod
    def _negotiated(pcm, a) -> tuple[int, int, int, str]:
        """(channels, rate, period_size, format name) the card really settled on."""
        info = pcm.info() if hasattr(pcm, "info") else {}
        fmt = info.get("format_name")
        if fmt is None and "format" in info:  # name missing: match the code against the module's
            fmt = next((n for n in SAMPLE_WIDTHS if getattr(a, f"PCM_FORMAT_{n}", None) == info["format"]),
                       str(info["format"]))
        return (int(info.get("channels", 0)), int(info.get("rate", 0)), int(info.get("period_size", 0)),
                fmt or "S16_LE")  # an info() without format fields is taken as the S16 that was asked

    def close(self) -> None:
        for attr in ("_cap", "_play"):
            pcm = getattr(self, attr)
            setattr(self, attr, None)
            if pcm is not None:
                try:
                    pcm.close()
                except Exception:  # noqa: BLE001, S110 - discarding the device; a failed close changes nothing
                    pass

    def _device_present(self) -> bool:
        return self.card is not None and (self.proc_asound / f"card{self.card.index}").exists()

    def _fail(self, where: str, exc: Exception) -> AudioDeviceError:
        gone = "" if self._device_present() else " (card node gone)"
        return AudioDeviceError(f"{where}: {exc}{gone}")

    # ---- PCM ----------------------------------------------------------------

    def read(self) -> np.ndarray:
        if self._cap is None:
            raise AudioDeviceError("capture not open")
        a = _alsa()
        buf = self._pending
        dead = 0
        while buf.size < FRAME_SAMPLES:
            # The supervisor closes the device from its own thread when playback fails (QMX USB
            # drop, 2026-10-03): an AttributeError here ended the capture thread for good.
            cap = self._cap
            if cap is None:
                raise AudioDeviceError("capture read: device closed")
            try:
                frames, data = cap.read()
            except a.ALSAAudioError as e:
                raise self._fail("capture read", e) from e
            if frames < 0:
                self._overruns += 1
            elif frames == 0 and not self._device_present():
                raise AudioDeviceError("capture read: card node gone")
            if frames <= 0:
                dead += 1
                if dead >= DEAD_READS_LIMIT:
                    raise AudioDeviceError(f"capture read: stream will not start "
                                           f"({dead} reads in a row with no audio)")
                continue
            dead = 0
            chunk = _to_int16(data, self._cap_format)
            if self._cap_channels == 2:
                chunk = chunk[0::2]  # left channel: TRRS mic is mono on both channels
            buf = np.concatenate((buf, chunk)) if buf.size else chunk
        self._pending = buf[FRAME_SAMPLES:]
        return buf[:FRAME_SAMPLES].copy()

    def write(self, pcm: np.ndarray) -> None:
        if self._play is None:
            raise AudioDeviceError("playback not open")
        a = _alsa()
        out = pcm.astype(np.int16, copy=False)
        if self._play_channels == 2:
            out = np.repeat(out, 2)
        try:
            n = self._play.write(_from_int16(out, self._play_format))
        except a.ALSAAudioError as e:
            raise self._fail("playback write", e) from e
        if n < 0:
            self._underruns += 1

    @property
    def tx_soft(self) -> bool:
        return self._tx_soft

    @property
    def rx_fixed(self) -> bool:
        return self._rx_fixed

    @property
    def overruns(self) -> int:
        return self._overruns

    @property
    def underruns(self) -> int:
        return self._underruns

    # ---- mixer --------------------------------------------------------------

    def _find_mixer(self, a, direction: str):
        if self.card is None:
            return None
        pcmtype = a.PCM_PLAYBACK if direction == "tx" else a.PCM_CAPTURE
        try:
            names = a.mixers(cardindex=self.card.index)
        except a.ALSAAudioError:
            return None
        prefer = PLAYBACK_CONTROLS if direction == "tx" else CAPTURE_CONTROLS
        for name in list(prefer) + [n for n in names if n not in prefer]:
            if name not in names:
                continue
            try:
                m = a.Mixer(control=name, cardindex=self.card.index)
                # A switch-only control (QMX 'PCM': mute, no volume) answers garbage from getvolume and
                # getrange on every second open, so only volumecap() decides (2026-10-03).
                if not _has_volume(m.volumecap(), direction):
                    continue
                # Raises if this control has no such volume, or answers [] (the IC-705's PCM2901 for capture).
                if not m.getvolume(pcmtype=pcmtype):
                    continue
                self._mixer_names[direction] = name
                return m
            except a.ALSAAudioError:
                continue
        return None

    def _no_volume(self, a, direction: str) -> bool:
        """True only when every control on the card is positively seen without a volume for this
        direction: playback on the QMX ('PCM' is a mute switch), capture on the IC-705 (its one
        control, 'PCM', is playback only). Any ALSA error answers False: a KX2 or IC-705 whose
        lookup failed once must play at unity, or soft gain would stack on its hardware mixer."""
        if self.card is None:
            return False
        pcmtype = a.PCM_PLAYBACK if direction == "tx" else a.PCM_CAPTURE
        seen = 0
        try:
            for name in a.mixers(cardindex=self.card.index):
                m = a.Mixer(control=name, cardindex=self.card.index)
                seen += 1
                try:
                    if _has_volume(m.volumecap(), direction):
                        lo, hi = m.getrange(pcmtype=pcmtype)
                        if hi > lo:
                            return False
                finally:
                    m.close()
        except a.ALSAAudioError:
            return False
        return seen > 0  # an empty list (mid-enumeration) proves nothing

    def set_gain(self, tx=None, rx=None) -> dict:
        a = _alsa()
        for direction, value in (("tx", tx), ("rx", rx)):
            if value is None:
                continue
            value = int(max(0, min(100, value)))
            m = self._find_mixer(a, direction)
            if direction == "tx":
                self._tx_soft = m is None and self._no_volume(a, "tx")
            else:
                self._rx_fixed = m is None and self._no_volume(a, "rx")
            if m is None:
                self._gain[direction] = value  # remembered, applied when a mixer appears
                continue
            pcmtype = a.PCM_PLAYBACK if direction == "tx" else a.PCM_CAPTURE
            try:
                m.setvolume(value, pcmtype=pcmtype)
                if direction == "rx" and hasattr(m, "setrec"):
                    try:
                        m.setrec(1)
                    except a.ALSAAudioError:
                        pass
                got = m.getvolume(pcmtype=pcmtype)[0]
                self._gain[direction] = got if 0 <= got <= 100 else value  # garbage readback: keep the request
            except a.ALSAAudioError as e:
                raise AudioDeviceError(f"mixer {self._mixer_names[direction]}: {e}") from e
            finally:
                try:
                    m.close()
                except Exception:  # noqa: BLE001, S110 - the level is already set or reported; a failed close changes nothing
                    pass
        return dict(self._gain)

    def mixer_names(self) -> dict:
        return dict(self._mixer_names)

    def describe(self) -> str:
        if self.card is None:
            return "alsa:none"
        ch = f"cap{self._cap_channels}ch/play{self._play_channels}ch"
        note = getattr(self, "_period_note", "")
        if (self._cap_format, self._play_format) != ("S16_LE", "S16_LE"):
            note += f" fmt=cap:{self._cap_format}/play:{self._play_format}"
        return f"alsa:hw:{self.card.index} {self.card.name} usb={self.card.usb_id} {ch} period={PERIOD_FRAMES}x{PERIODS}{note}"
