"""Admin PIN for the web server's POST endpoints (roadmap 1.1, ruling R-D3).

Anyone who joins the bridge's Wi-Fi can reach port 80, so every POST needs a session: a browser
logs in once and holds an HttpOnly cookie; a native app logs in once and sends the token as
`Authorization: Bearer <token>` (DOCS/web-api.md). The PIN is stored only as a salted PBKDF2 hash
on /persist. Sessions live in memory: a restart, a PIN change or 12 h unused ends them.

An app that names itself at login (`"app"`, roadmap 1.8) gets a long-lived token instead, so it
never has to keep the PIN. Only the token's SHA-256 is kept, on /persist, so it survives a
restart; it ends when the PIN changes or is reset, or when the owner revokes it.

A forgotten PIN is reset at the HDMI console (scripts/console/vb-console, or
`sudo vector-bridge pin reset`), which deletes the hash. Until a new PIN is created every POST
is refused, and the first save on the web page asks for one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

from . import persist

PIN_PATH = persist.APP_DIR / "admin-pin.json"
APP_TOKENS_NAME = "app-tokens.json"  # next to the PIN file
COOKIE = "vb_admin"
ITERATIONS = 100_000  # about 0.3 s on a Pi Zero 2 W; paid once per login, not per request
IDLE_S = 12 * 3600
MAX_SESSIONS = 32
FREE_TRIES = 5  # wrong PINs before the lockout starts
LOCK_BASE_S, LOCK_MAX_S = 60, 3600
MAX_APP_TOKENS = 16
APP_NAME_MAX = 64


def valid_pin(pin) -> bool:
    return isinstance(pin, str) and 4 <= len(pin) <= 8 and pin.isascii() and pin.isdigit()


def _hash(pin: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations).hex()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _app_name(body: dict):
    """The `app` label from a login body: None when absent, False when unusable."""
    app = body.get("app")
    if app is None:
        return None
    if not (isinstance(app, str) and 0 < len(app.strip()) <= APP_NAME_MAX and app.isprintable()):
        return False
    return app.strip()


def bearer(headers) -> str | None:
    """The session token from `Authorization: Bearer` or the browser cookie, if either is there."""
    auth = headers.get("Authorization") or ""
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip() or None
    try:
        morsel = SimpleCookie(headers.get("Cookie") or "").get(COOKIE)
    except CookieError:
        return None
    return morsel.value if morsel else None


def cookie_header(token: str) -> str:
    # No Max-Age: the server ends the session (12 h unused), and a fixed browser expiry would cut a busy one short.
    return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"


class Admin:
    """The PIN hash on disk plus the live sessions. Thread safe: the server is threaded."""

    def __init__(self, path: Path | None = None, save=persist.save, clock=time.monotonic) -> None:
        self.path, self._save, self._clock = path or PIN_PATH, save, clock
        self.tokens_path = self.path.with_name(APP_TOKENS_NAME)
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[str, float]] = {}  # token -> (hash it was issued under, last use)
        self._failures = 0
        self._locked_until = 0.0

    def _stored(self) -> dict | None:
        """Read on every check, so a console reset takes effect without restarting the tuner."""
        try:
            rec = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        if not (isinstance(rec, dict) and isinstance(rec.get("hash"), str) and isinstance(rec.get("salt"), str)
                and isinstance(rec.get("iterations"), int)):
            return None
        return rec

    def pin_set(self) -> bool:
        return self._stored() is not None

    def _app_tokens(self, rec: dict | None) -> list[dict]:
        """The app tokens issued under the current PIN. Read on every check, like the PIN, so a
        revoke from another process counts at once; tokens from an older PIN are left out."""
        if rec is None:
            return []
        try:
            rows = json.loads(self.tokens_path.read_text())
        except (OSError, ValueError):
            return []
        if not isinstance(rows, list):
            return []
        keys = {"id": str, "app": str, "sha256": str, "salt": str, "issued": (int, float)}
        return [r for r in rows if isinstance(r, dict) and all(isinstance(r.get(k), t) for k, t in keys.items())
                and hmac.compare_digest(r["salt"], rec["salt"])]

    def _write_app_tokens(self, rows: list[dict]) -> None:
        self._save(self.tokens_path, json.dumps(rows), mode=0o600)

    def app_token_valid(self, token) -> bool:
        """A live 1.8 app token (not a browser session): what a rigctl client on port 4532 sends (roadmap 1.10)."""
        if not isinstance(token, str) or not token:
            return False
        rec = self._stored()
        if rec is None:
            return False
        digest = _digest(token)
        return any(hmac.compare_digest(r["sha256"], digest) for r in self._app_tokens(rec))

    def authorized(self, headers) -> bool:
        token = bearer(headers)
        if not token:
            return False
        rec = self._stored()
        if rec is None:
            return False
        now = self._clock()
        with self._lock:
            entry = self._sessions.get(token)
            if entry is None:
                digest = _digest(token)
                return any(hmac.compare_digest(r["sha256"], digest) for r in self._app_tokens(rec))
            issued_under, last = entry
            if not hmac.compare_digest(issued_under, rec["hash"]) or now - last > IDLE_S:
                del self._sessions[token]
                return False
            self._sessions[token] = (issued_under, now)
            return True

    def _new_session(self, pin_hash: str) -> str:
        token = secrets.token_hex(16)
        now = self._clock()
        with self._lock:
            self._sessions = {t: e for t, e in self._sessions.items() if now - e[1] <= IDLE_S}
            while len(self._sessions) >= MAX_SESSIONS:
                del self._sessions[min(self._sessions, key=lambda t: self._sessions[t][1])]
            self._sessions[token] = (pin_hash, now)
        return token

    def _new_app_token(self, rec: dict, app: str) -> str:
        """A token that outlives restarts. The oldest goes when there are MAX_APP_TOKENS: the file
        keeps issue order, as `issued` is wall time and the Pi's clock can step back."""
        token = secrets.token_hex(16)
        with self._lock:
            # Keep what is valid now: a PIN change during this login's slow hash wrote newer rows.
            rows = self._app_tokens(self._stored())
            rows.append({"id": secrets.token_hex(4), "app": app, "sha256": _digest(token), "salt": rec["salt"],
                         "issued": int(time.time())})
            self._write_app_tokens(rows[-MAX_APP_TOKENS:])
        return token

    def _issue(self, rec: dict, app: str | None) -> tuple[int, dict]:
        if app is None:
            return 200, {"ok": True, "token": self._new_session(rec["hash"])}
        try:
            return 200, {"ok": True, "token": self._new_app_token(rec, app), "app": app}
        except OSError as e:
            return 500, {"ok": False, "error": "save_failed", "detail": str(e)}

    def login(self, body: dict) -> tuple[int, dict]:
        """POST /api/login. Every wrong PIN after the free tries doubles the wait, up to an hour.
        With `app` the token is an app token (long-lived); without, a browser-style session."""
        rec = self._stored()
        if rec is None:
            return 409, {"ok": False, "error": "pin_unset"}
        app = _app_name(body)
        if app is False:
            return 400, {"ok": False, "error": "bad_app", "fix": f"Name the app in 1 to {APP_NAME_MAX} characters."}
        # Count the try as wrong before the slow hash and undo it on success: counted afterwards,
        # many tries sent at once would all pass the lock check before any of them was counted.
        now = self._clock()
        with self._lock:
            if now < self._locked_until:
                return 429, {"ok": False, "error": "locked", "retry_s": int(self._locked_until - now) + 1}
            self._failures += 1
            if self._failures >= FREE_TRIES:
                self._locked_until = now + min(LOCK_BASE_S * 2 ** (self._failures - FREE_TRIES), LOCK_MAX_S)
        pin = body.get("pin")
        good = valid_pin(pin) and hmac.compare_digest(
            _hash(pin, bytes.fromhex(rec["salt"]), rec["iterations"]), rec["hash"])
        if not good:
            return 401, {"ok": False, "error": "bad_pin"}
        with self._lock:
            self._failures, self._locked_until = 0, 0.0
        return self._issue(rec, app)

    def set_pin(self, body: dict, authorized: bool) -> tuple[int, dict]:
        """POST /api/pin: create the first PIN (no session needed) or change it (session needed).
        A new PIN ends every other session, apps included; the caller gets a fresh token."""
        if self.pin_set() and not authorized:
            return 401, {"ok": False, "error": "pin_required", "pin_set": True}
        pin = body.get("pin")
        if not valid_pin(pin):
            return 400, {"ok": False, "error": "bad_pin_format", "fix": "Use 4 to 8 digits."}
        app = _app_name(body)
        if app is False:
            return 400, {"ok": False, "error": "bad_app", "fix": f"Name the app in 1 to {APP_NAME_MAX} characters."}
        salt = secrets.token_bytes(16)
        rec = {"salt": salt.hex(), "iterations": ITERATIONS, "hash": _hash(pin, salt, ITERATIONS)}
        try:
            self._save(self.path, json.dumps(rec), mode=0o600)
        except OSError as e:
            return 500, {"ok": False, "error": "save_failed", "detail": str(e)}
        with self._lock:
            self._sessions.clear()
            self._failures, self._locked_until = 0, 0.0
            # The new salt already ends every app token; dropping the rows only tidies the file.
            if self.tokens_path.exists():
                try:
                    self._write_app_tokens([])
                except OSError:
                    pass
        return self._issue(rec, app)

    def app_tokens(self) -> dict:
        """GET /api/tokens (session needed): the live app tokens, never the tokens themselves."""
        return {"ok": True, "tokens": [{k: r[k] for k in ("id", "app", "issued")}
                                       for r in self._app_tokens(self._stored())]}

    def revoke(self, body: dict) -> tuple[int, dict]:
        """POST /api/tokens {"revoke": "<id>"} (session needed): end one app token."""
        rid = body.get("revoke")
        with self._lock:
            rows = self._app_tokens(self._stored())
            kept = [r for r in rows if r["id"] != rid]
            if len(kept) == len(rows):
                return 404, {"ok": False, "error": "no_such_token"}
            try:
                self._write_app_tokens(kept)
            except OSError as e:
                return 500, {"ok": False, "error": "save_failed", "detail": str(e)}
        return 200, {"ok": True, "revoked": rid}

    def report(self, headers) -> dict:
        """GET /api/pin: whether a PIN exists and whether this caller holds a session."""
        return {"ok": True, "set": self.pin_set(), "unlocked": self.authorized(headers)}


def reset(path: Path | None = None) -> bool:
    """Console recovery: delete the hash and the app tokens (they died with the hash anyway).
    True if there was a PIN."""
    path = path or PIN_PATH
    tokens = path.with_name(APP_TOKENS_NAME)
    had = path.exists()
    if not had and not tokens.exists():
        return False
    with persist.writable(path):
        path.unlink(missing_ok=True)
        tokens.unlink(missing_ok=True)
    return had
