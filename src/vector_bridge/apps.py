"""The Apps card: client apps describe themselves (name, icon, launch link) for the home page.

DOCS/app-listing.md is the protocol. A description is stored per (source IP, app_id) and shown
only while the audio service reports a live stream from that IP whose `client` string is that
app_id, so a row always means "connected now". Everything is in memory: a restart forgets it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import struct
import threading
import time
import unicodedata
import zlib

MAX_BODY = 64 * 1024
FRESH_S = 180.0  # a description older than this is not shown; apps re-POST every 60 s
MAX_RECORDS = 32  # bounds memory: 32 icons of 24 KB
ICON_MAX = 24 * 1024
ICON_SIDE = (64, 256)
APP_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}")
SCHEME = re.compile(r"([a-z][a-z0-9+.-]*):", re.IGNORECASE)
REFUSED_SCHEMES = {"javascript", "data", "file", "blob", "vbscript", "about"}
SHA256 = re.compile(r"[0-9a-f]{64}")
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _plain(s, limit: int) -> str | None:
    """`s` trimmed, when it is 1..limit characters of plain text; else None."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not 0 < len(s) <= limit:
        return None
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in s):
        return None
    return s


def _launch_url(u) -> str | None:
    """The link as given, or None when its scheme or characters could run in the page."""
    if not isinstance(u, str) or not 0 < len(u) <= 512:
        return None
    if any(ord(c) <= 0x20 or ord(c) == 0x7F or unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in u):
        return None  # no whitespace or controls: they hide a scheme from a naive check
    m = SCHEME.match(u)
    if not m or m.group(1).lower() in REFUSED_SCHEMES:
        return None
    return u


def png_ok(data: bytes) -> bool:
    """A whole, square PNG of 64 to 256 pixels a side: signature, IHDR first, every CRC, IEND last."""
    if not data.startswith(PNG_SIG):
        return False
    pos, first, end = len(PNG_SIG), True, False
    while pos < len(data):
        if end or pos + 12 > len(data):
            return False
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk_end = pos + 12 + length
        if chunk_end > len(data):
            return False
        body = data[pos + 8:pos + 8 + length]
        if struct.unpack(">I", data[pos + 8 + length:chunk_end])[0] != zlib.crc32(kind + body):
            return False
        if first:
            if kind != b"IHDR" or length != 13:
                return False
            w, h = struct.unpack(">II", body[:8])
            if w != h or not ICON_SIDE[0] <= w <= ICON_SIDE[1]:
                return False
            first = False
        end = kind == b"IEND"
        pos = chunk_end
    return end


def validate(body: dict) -> tuple[dict | None, str | None]:
    """(record, None) for a good description, else (None, error code)."""
    app_id = body.get("app_id")
    if not isinstance(app_id, str) or not APP_ID.fullmatch(app_id):
        return None, "bad_app_id"
    name = _plain(body.get("name"), 32)
    if name is None:
        return None, "bad_name"
    url = body.get("launch_url")
    if url is not None:
        url = _launch_url(url)
        if url is None:
            return None, "bad_launch_url"
    note = body.get("note")
    if note is not None:
        note = _plain(note, 60)
        if note is None:
            return None, "bad_note"
    icon, icon_sha = None, None
    if body.get("icon_png") is not None:
        b64 = body["icon_png"]
        if not isinstance(b64, str) or len(b64) > (ICON_MAX * 4) // 3 + 4:
            return None, "bad_icon"
        try:
            icon = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return None, "bad_icon"
        if len(icon) > ICON_MAX or not png_ok(icon):
            return None, "bad_icon"
        icon_sha = hashlib.sha256(icon).hexdigest()
    elif body.get("icon_sha256") is not None:
        icon_sha = body["icon_sha256"]
        if not isinstance(icon_sha, str) or not SHA256.fullmatch(icon_sha := icon_sha.lower()):
            return None, "bad_icon"
    return {"app_id": app_id, "name": name, "launch_url": url, "note": note,
            "icon": icon, "icon_sha256": icon_sha}, None


def ip_of(addr: str) -> str:
    """"ip:port" from the status reply to the ip."""
    return addr.rsplit(":", 1)[0]


class AppStore:
    """Descriptions keyed by (source IP, app_id), and the icons they name."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._mu = threading.Lock()
        self._records: dict[tuple[str, str], dict] = {}  # oldest first
        self._icons: dict[str, bytes] = {}  # full sha256 hex -> png

    def put(self, ip: str, body: dict) -> tuple[int, dict]:
        """Store one POST. 200 with `icon_needed` when it names an icon this bridge does not hold."""
        rec, err = validate(body)
        if err:
            return 400, {"ok": False, "error": err}
        icon = rec.pop("icon")
        reply: dict = {"ok": True}
        with self._mu:
            if icon is not None:
                self._icons[rec["icon_sha256"]] = icon
            elif rec["icon_sha256"] is not None and rec["icon_sha256"] not in self._icons:
                rec["icon_sha256"] = None  # the generic icon until the app sends the PNG again
                reply["icon_needed"] = True
            key = (ip, rec["app_id"])
            self._records.pop(key, None)
            self._records[key] = {**rec, "at": self._clock()}
            self._prune()
        return 200, reply

    def _prune(self) -> None:
        """Drop stale or excess records and the icons nothing names. Caller holds _mu."""
        now = self._clock()
        for key in [k for k, r in self._records.items() if now - r["at"] > FRESH_S]:
            del self._records[key]
        while len(self._records) > MAX_RECORDS:
            del self._records[next(iter(self._records))]
        named = {r["icon_sha256"] for r in self._records.values()}
        for sha in [s for s in self._icons if s not in named]:
            del self._icons[sha]

    def rows(self, clients: list[dict]) -> list[dict]:
        """One row per live stream in the status reply's `clients`, described when a fresh POST matches."""
        out = []
        with self._mu:
            now = self._clock()
            for c in clients:
                addr, client = str(c.get("addr", "")), str(c.get("client", ""))
                rec = self._records.get((ip_of(addr), client))
                if rec is not None and now - rec["at"] > FRESH_S:
                    rec = None
                row = {"app_id": client or None, "name": client or "Unnamed app", "role": c.get("role"),
                       "addr": addr, "launch_url": None, "note": None, "icon_url": None}
                if rec is not None:
                    row.update(name=rec["name"], launch_url=rec["launch_url"], note=rec["note"])
                    if rec["icon_sha256"] in self._icons:
                        row["icon_url"] = f"/api/apps/icon/{rec['icon_sha256'][:16]}.png"
                out.append(row)
        return out

    def icon(self, prefix: str) -> bytes | None:
        """The PNG whose sha256 starts with these 16 hex digits."""
        if not re.fullmatch(r"[0-9a-f]{16}", prefix):
            return None
        with self._mu:
            return next((png for sha, png in self._icons.items() if sha.startswith(prefix)), None)


def listed(clients: list[dict], ip: str, app_id: str) -> bool:
    """A live stream from `ip` carries this app_id."""
    return any(ip_of(str(c.get("addr", ""))) == ip and c.get("client") == app_id for c in clients)
