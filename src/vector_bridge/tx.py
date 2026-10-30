"""Transmit arbitration: one keyer at a time, and never keyed without a live client.

These protect the radio from the software, so nothing here has an override. The
exclusive lock stops two clients keying at once. The hang-PTT watchdog unkeys when
heartbeats stop, which is what a Wi-Fi drop mid-transmission looks like from the Pi.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

DEFAULT_WATCHDOG_S = 3.0
SUSPECT_S = 30.0  # after a CAT failure while keying, keep reading the rig's real PTT this long
STOP_TRIES = 5  # stop() tries the unkey this many times
STOP_RETRY_S = 0.2  # apart by this long
RELEASE_GRACE_S = 2.0  # after an unkey the rig's PTT read-back can still say transmit (rigctld lags ~0.8 s)


class TxLock:
    def __init__(self) -> None:
        self._owner: str | None = None
        self._mu = threading.Lock()

    @property
    def owner(self) -> str | None:
        return self._owner

    def acquire(self, owner: str) -> bool:
        with self._mu:
            if self._owner in (None, owner):
                self._owner = owner
                return True
            return False

    def release(self, owner: str) -> bool:
        with self._mu:
            if self._owner != owner:
                return False
            self._owner = None
            return True


class PttController:
    """Keys the rig for exactly one owner and drops PTT when that owner goes quiet."""

    def __init__(
        self,
        set_ptt: Callable[[bool], None],
        lock: TxLock | None = None,
        watchdog_s: float = DEFAULT_WATCHDOG_S,
        clock: Callable[[], float] = time.monotonic,
        on_hang: Callable[[str], None] | None = None,
        get_ptt: Callable[[], bool] | None = None,
    ) -> None:
        self._set_ptt = set_ptt
        self._get_ptt = get_ptt
        # Open from the start: after a crash while keyed, the restarted service unkeys the rig.
        self._suspect_until = clock() + SUSPECT_S
        self.lock = lock or TxLock()
        self.watchdog_s = watchdog_s
        self._clock = clock
        self._on_hang = on_hang
        self._last_beat = 0.0
        self._released_at = float("-inf")
        self._keyed = False
        self._mu = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.hang_count = 0
        self.stop_tries = STOP_TRIES
        self.stop_retry_s = STOP_RETRY_S

    @property
    def keyed(self) -> bool:
        return self._keyed

    @property
    def watchdog_armed(self) -> bool:
        """Always true while keyed. There is deliberately no way to turn this off."""
        return self._keyed

    def key(self, owner: str) -> bool:
        with self._mu:
            if not self.lock.acquire(owner):
                return False
            self._last_beat = self._clock()
            if not self._keyed:
                try:
                    self._set_ptt(True)
                except Exception:
                    # A timed-out key can still reach the rig late, after the unkey below.
                    self._suspect_until = self._clock() + SUSPECT_S
                    # The rig may have keyed before the error, and the watchdog skips an
                    # unkeyed controller, so unkey and free the lock here or nobody will.
                    try:
                        self._set_ptt(False)
                    except Exception:
                        # Cannot prove RX: stay "keyed" with the lock held so the
                        # watchdog keeps retrying the unkey.
                        log.exception("could not unkey after a failed key")
                        self._keyed = True
                        raise
                    self.lock.release(owner)
                    raise
                self._keyed = True
            return True

    def just_released(self) -> bool:
        """True for RELEASE_GRACE_S after this controller unkeyed the rig."""
        return self._clock() - self._released_at < RELEASE_GRACE_S

    def heartbeat(self, owner: str) -> None:
        with self._mu:
            if self.lock.owner == owner:
                self._last_beat = self._clock()

    def unkey(self, owner: str) -> bool:
        with self._mu:
            if self.lock.owner != owner:
                return False
            self._drop()
            return True

    def check(self, now: float | None = None) -> bool:
        """Unkey if the owner has been silent longer than the watchdog. True if it fired."""
        now = self._clock() if now is None else now
        with self._mu:
            if not self._keyed:
                self._unkey_a_stray_key(now)
                return False
            if now - self._last_beat < self.watchdog_s:
                return False
            owner = self.lock.owner or ""
            self._drop()
            self.hang_count += 1
        if self._on_hang:
            self._on_hang(owner)
        return True

    def _unkey_a_stray_key(self, now: float) -> None:
        """After a CAT failure the rig can be keyed while this controller says receive.

        Only inside the suspect window, so an operator's hand microphone is left alone.
        """
        if self._get_ptt is None or now >= self._suspect_until:
            return
        if self._get_ptt():
            log.error("rig is keyed but nobody holds PTT: unkeying")
            self._set_ptt(False)

    def _drop(self) -> None:
        # Unkey first, then release the lock, so a crash between the two leaves the
        # radio in RX with the lock still held rather than the other way round.
        if self._keyed:
            try:
                self._set_ptt(False)
            except Exception:
                # Every failed unkey, from a client, the watchdog or stop(), opens the window again.
                self._suspect_until = self._clock() + SUSPECT_S
                raise
            self._keyed = False
            self._released_at = self._clock()
        if self.lock.owner is not None:
            self.lock.release(self.lock.owner)

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ptt-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the watchdog and unkey, retrying. Never raises, so shutdown carries on.

        If every try fails the rig may still be keyed; that is logged, and the next
        start's open suspect window unkeys it.
        """
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        for attempt in range(1, self.stop_tries + 1):
            try:
                with self._mu:
                    self._drop()
                return
            except Exception:
                log.exception("unkey at stop failed (try %d of %d)", attempt, self.stop_tries)
            if attempt < self.stop_tries:
                time.sleep(self.stop_retry_s)
        log.critical("RIG MAY STILL BE KEYED: could not unkey after %d tries", self.stop_tries)

    def _loop(self) -> None:
        while not self._stop.wait(self.watchdog_s / 4):
            try:
                self.check()
            except Exception:
                # Still keyed if the unkey failed, so the next pass tries again.
                log.exception("ptt watchdog check failed")
