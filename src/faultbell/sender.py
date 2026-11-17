"""The background sender: a bounded queue, one daemon thread, de-duplication and rate limiting.

Nothing here runs in the caller's thread except :func:`enqueue` (a non-blocking ``put``). A full queue,
a webhook error or a timeout drops the alert and bumps a counter in :func:`stats`.
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from . import config
from .card import Event, build_card, fingerprint
from .transports import Transport, transport_named

_log = logging.getLogger("faultbell")

RETRY_AFTER_CAP_S = 5.0
SEEN_MAX = 1000

_STOP = object()


class RateLimiter:
    """At most ``limit`` calls per ``per`` seconds (sliding window); sleeps the sender thread."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.clock, self.sleep = clock, sleep
        self.sent: list[float] = []

    def wait(self, limit: int, per: float) -> None:
        now = self.clock()
        self.sent = [t for t in self.sent if now - t < per]
        if limit > 0 and len(self.sent) >= limit:
            self.sleep(max(0.0, per - (now - self.sent[0])))
            now = self.clock()
            self.sent = [t for t in self.sent if now - t < per]
        self.sent.append(now)


_queue: queue.Queue[Any] | None = None
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
_atexit_registered = False
_limiters: dict[tuple[str, str], RateLimiter] = {}  # one per channel
_seen: dict[str, list[float]] = {}  # fingerprint -> [last sent (settings clock), suppressed since]
_stats_lock = threading.Lock()
_stats = {"queued": 0, "sent": 0, "suppressed": 0, "dropped": 0, "failed": 0}


def _bump(name: str) -> None:
    with _stats_lock:
        _stats[name] += 1


def stats() -> dict[str, int]:
    """Counters since start (or the last :func:`reset`): queued, sent, suppressed, dropped, failed."""
    with _stats_lock:
        return dict(_stats)


def active_transport() -> Transport:
    chosen = config.settings().transport
    if chosen is not None:
        return chosen
    return transport_named(os.environ.get("FAULTBELL_TRANSPORT") or "discord")


def _ensure_sender() -> queue.Queue[Any]:
    global _queue, _thread, _atexit_registered
    with _thread_lock:
        if _thread is None or not _thread.is_alive():
            _queue = queue.Queue(maxsize=max(1, config.settings().queue_size))
            _thread = threading.Thread(target=_loop, args=(_queue,), name="faultbell-sender", daemon=True)
            _thread.start()
            if not _atexit_registered:
                atexit.register(flush, 2.0)
                _atexit_registered = True
        assert _queue is not None
        return _queue


def enqueue(event: Event) -> None:
    """Hand an event to the sender thread. Never blocks; drops when the queue is full."""
    q = _ensure_sender()
    try:
        q.put_nowait(event)
        _bump("queued")
    except queue.Full:
        _bump("dropped")


def flush(timeout: float = 2.0) -> bool:
    """Wait up to ``timeout`` seconds for queued alerts to be sent or dropped. True when drained."""
    try:
        q = _queue
        if q is None or _thread is None:
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        with q.all_tasks_done:
            while q.unfinished_tasks:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                q.all_tasks_done.wait(left)
        return True
    except Exception:
        _log.debug("flush failed", exc_info=True)
        return False


def _loop(q: queue.Queue[Any]) -> None:
    while True:
        item = q.get()
        try:
            if item is _STOP:
                return
            process(item)
        except Exception:  # one bad event never stops the thread
            _log.debug("alert dropped", exc_info=True)
            _bump("failed")
        finally:
            q.task_done()


def process(event: Event) -> None:
    """De-duplicate, build the card and deliver it (runs on the sender thread)."""
    fp = fingerprint(event)
    now = config.settings().clock()
    state = _seen.get(fp)
    if state and now - state[0] < config.dedupe_window():
        state[1] += 1  # counted; the next card that goes out reports it
        _bump("suppressed")
        return
    seen = 1 + int(state[1] if state else 0)
    _seen[fp] = [now, 0]
    if len(_seen) > SEEN_MAX:  # bounded memory: forget the oldest half
        for key in sorted(_seen, key=lambda k: _seen[k][0])[: SEEN_MAX // 2]:
            _seen.pop(key, None)
    deliver(build_card(event, fp, seen))


def deliver(card: Any) -> None:
    """Send one card to every channel that wants it. One channel failing never stops the others.
    The ``sent`` / ``failed`` counters count channel deliveries."""
    for transport, destination in config.channels():
        try:
            if transport.wants(card):
                _deliver_one(transport, destination, card)
        except Exception:
            _log.debug("channel %s failed", transport.name, exc_info=True)
            _bump("failed")


def _deliver_one(transport: Transport, url: str, card: Any) -> None:
    s = config.settings()
    limiter = _limiters.setdefault((type(transport).__name__, url), RateLimiter())
    for attempt in (1, 2):
        limiter.wait(*s.rate_limit)
        try:
            result = transport.send(card, url, s.timeout)
        except Exception:  # network trouble: drop it
            _log.debug("%s delivery failed", transport.name, exc_info=True)
            _bump("failed")
            return
        if result.status == 429 and attempt == 1:
            time.sleep(min(max(result.retry_after or 1.0, 0.0), RETRY_AFTER_CAP_S))
            continue
        _bump("sent" if result.ok else "failed")
        return


def stop(timeout: float = 2.0) -> None:
    """Stop the sender thread (a new one starts on the next alert)."""
    global _thread, _queue
    with _thread_lock:
        thread, q = _thread, _queue
        _thread = _queue = None
    if thread is None or q is None or not thread.is_alive():
        return
    try:
        q.put(_STOP, timeout=timeout)
    except queue.Full:
        return
    thread.join(timeout)


def reset_state() -> None:
    flush(2.0)
    stop()
    _seen.clear()
    _limiters.clear()
    with _stats_lock:
        for key in _stats:
            _stats[key] = 0


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()
