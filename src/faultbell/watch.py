"""Server watch: alerts for problems an exception hook never sees.

    from faultbell.watch import Watch, WatchMiddleware

    watch = Watch()                                  # memory limit read from the container if not given
    app.add_middleware(WatchMiddleware, watch=watch)
    watch.start()                                    # memory checks + heartbeat on a daemon thread

What it reports, each as a normal Faultbell card (scrubbed, de-duplicated, sent off the request path):

* **Error bursts**: several 5xx responses (default 3) or many 4xx responses (default 30) on one route
  within a window (default 5 minutes). Single 4xx responses never alert; a burst usually means a broken
  client or someone probing.
* **Memory**: the process using more than a share (default 80%) of its memory limit.
* **Crashes**: a previous process that stopped sending heartbeats without a clean shutdown, which on small
  hosts almost always means it was killed for using too much memory. Needs a shared heartbeat store
  (``SQLHeartbeatStore`` for any SQLAlchemy database); the default in-memory store only covers one process.

Routes are counted by their template (``/orders/{id}``), so one broken endpoint is one alert, not one per id.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from ._capture import capture

__all__ = [
    "Heartbeat",
    "HeartbeatStore",
    "MemoryHeartbeatStore",
    "SQLHeartbeatStore",
    "ServerWatchAlert",
    "Watch",
    "WatchMiddleware",
    "container_memory_limit_mb",
    "process_memory_mb",
]


class ServerWatchAlert(Exception):
    """The exception type on watch cards. It is never raised; Faultbell reports it like any other error."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- memory


def process_memory_mb() -> int:
    """Resident memory of this process in MB. Linux reads ``/proc``; elsewhere the peak is used."""
    try:
        with open("/proc/self/statm") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") // (1024 * 1024)
    except (OSError, ValueError, IndexError):
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes, Linux kilobytes.
        return int(peak / (1024 * 1024)) if peak > 50_000_000 else int(peak / 1024)


def container_memory_limit_mb(
    paths: Iterable[str] = ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
) -> int | None:
    """The container's memory limit in MB (cgroup v2 or v1), or None when there is none."""
    for path in paths:
        try:
            with open(path) as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 1 << 60:  # v1 reports a huge number for "no limit"
            return int(raw) // (1024 * 1024)
    return None


# ---------------------------------------------------------------- heartbeat stores


@dataclass
class Heartbeat:
    boot_id: str
    started_at: datetime
    beat_at: datetime
    ended_at: datetime | None = None
    peak_mb: int = 0


class HeartbeatStore(Protocol):
    def begin(self, boot_id: str, now: datetime) -> None: ...
    def beat(self, boot_id: str, now: datetime, peak_mb: int) -> None: ...
    def end(self, boot_id: str, now: datetime, peak_mb: int) -> None: ...
    def open_rows(self) -> list[Heartbeat]: ...
    def close(self, boot_id: str, at: datetime) -> None: ...
    def prune(self, before: datetime) -> None: ...


class MemoryHeartbeatStore:
    """Keeps heartbeats in this process. Fine for tests; use a shared store to detect real crashes."""

    def __init__(self) -> None:
        self.rows: dict[str, Heartbeat] = {}

    def begin(self, boot_id: str, now: datetime) -> None:
        self.rows[boot_id] = Heartbeat(boot_id, now, now)

    def beat(self, boot_id: str, now: datetime, peak_mb: int) -> None:
        row = self.rows.get(boot_id)
        if row:
            row.beat_at, row.peak_mb = now, peak_mb

    def end(self, boot_id: str, now: datetime, peak_mb: int) -> None:
        row = self.rows.get(boot_id)
        if row:
            row.ended_at, row.peak_mb = now, peak_mb

    def open_rows(self) -> list[Heartbeat]:
        return [r for r in self.rows.values() if r.ended_at is None]

    def close(self, boot_id: str, at: datetime) -> None:
        if boot_id in self.rows:
            self.rows[boot_id].ended_at = at

    def prune(self, before: datetime) -> None:
        self.rows = {k: v for k, v in self.rows.items() if v.started_at >= before}


class SQLHeartbeatStore:
    """Heartbeats in any SQLAlchemy database (needs the ``sqlalchemy`` extra).

    Creates its own small table (default ``faultbell_heartbeats``) on first use.
    """

    def __init__(self, engine: Any, table_name: str = "faultbell_heartbeats") -> None:
        import sqlalchemy as sa

        self._sa = sa
        self.engine = engine
        meta = sa.MetaData()
        self.table = sa.Table(
            table_name,
            meta,
            sa.Column("boot_id", sa.String(32), primary_key=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("peak_mb", sa.Integer, nullable=False, default=0),
        )
        meta.create_all(engine, checkfirst=True)

    def _run(self, stmt: Any) -> Any:
        with self.engine.begin() as conn:
            return conn.execute(stmt)

    def begin(self, boot_id: str, now: datetime) -> None:
        self._run(self.table.insert().values(boot_id=boot_id, started_at=now, beat_at=now, peak_mb=0))

    def beat(self, boot_id: str, now: datetime, peak_mb: int) -> None:
        t = self.table
        self._run(t.update().where(t.c.boot_id == boot_id).values(beat_at=now, peak_mb=peak_mb))

    def end(self, boot_id: str, now: datetime, peak_mb: int) -> None:
        t = self.table
        self._run(t.update().where(t.c.boot_id == boot_id).values(ended_at=now, peak_mb=peak_mb))

    def open_rows(self) -> list[Heartbeat]:
        t = self.table
        rows = self._run(self._sa.select(t).where(t.c.ended_at.is_(None))).all()
        return [Heartbeat(r.boot_id, _aware(r.started_at), _aware(r.beat_at), None, r.peak_mb) for r in rows]

    def close(self, boot_id: str, at: datetime) -> None:
        t = self.table
        self._run(t.update().where(t.c.boot_id == boot_id).values(ended_at=at))

    def prune(self, before: datetime) -> None:
        t = self.table
        self._run(t.delete().where(t.c.started_at < before))


# ---------------------------------------------------------------- the watcher


HEALTH_PATHS = ("/health", "/healthz", "/ready", "/readyz", "/api/health", "/api/ready")


class Watch:
    """Counts error bursts, checks memory and keeps a heartbeat. All thresholds are arguments."""

    def __init__(
        self,
        *,
        memory_limit_mb: int | None = None,
        memory_alert_percent: int = 80,
        burst_5xx: int = 3,
        burst_4xx: int = 30,
        window: float = 300.0,
        quiet: float = 600.0,
        ignore_paths: Iterable[str] = HEALTH_PATHS,
        store: HeartbeatStore | None = None,
        beat_every: float = 60.0,
        dead_after: float = 150.0,
        keep_days: int = 30,
        alert: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        env_limit = os.environ.get("FAULTBELL_MEMORY_LIMIT_MB", "")
        self.memory_limit_mb = (
            memory_limit_mb
            or (int(env_limit) if env_limit.isdigit() else None)
            or container_memory_limit_mb()
        )
        self.memory_alert_percent = memory_alert_percent
        self.burst_5xx, self.burst_4xx = burst_5xx, burst_4xx
        self.window, self.quiet = window, quiet
        self.ignore_paths = tuple(ignore_paths)
        self.store: HeartbeatStore = store or MemoryHeartbeatStore()
        self.beat_every, self.dead_after, self.keep_days = beat_every, dead_after, keep_days
        self.boot_id = secrets.token_hex(8)
        self.peak_mb = 0
        self._alert_fn = alert
        self._clock = clock
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- alerts
    def _alert(self, key: str, message: str) -> bool:
        now = self._clock()
        with self._lock:
            if now - self._last.get(key, float("-inf")) < self.quiet:
                return False
            self._last[key] = now
        if self._alert_fn is not None:
            self._alert_fn(message)
        else:
            capture(ServerWatchAlert(message))
        return True

    # -- error bursts
    def record(self, method: str, route: str, status: int) -> bool:
        """Count one response. Returns True when it triggered an alert."""
        if status < 400 or route in self.ignore_paths:
            return False
        kind, limit = ("5xx", self.burst_5xx) if status >= 500 else ("4xx", self.burst_4xx)
        where = f"{method.upper()} {route}"
        now = self._clock()
        with self._lock:
            hits = self._hits[(kind, where)]
            hits.append(now)
            while hits and now - hits[0] > self.window:
                hits.popleft()
            count = len(hits)
        if count < limit:
            return False
        minutes = max(1, round(self.window / 60))
        return self._alert(f"{kind}:{where}", f"{count} {kind} responses in {minutes} minutes on {where}")

    # -- memory
    def check_memory(self, mb: int | None = None) -> bool:
        mb = process_memory_mb() if mb is None else mb
        self.peak_mb = max(self.peak_mb, mb)
        limit = self.memory_limit_mb
        if not limit or mb * 100 < limit * self.memory_alert_percent:
            return False
        return self._alert("memory", f"Memory at {mb} MB of {limit} MB ({mb * 100 // limit}%)")

    # -- heartbeat
    def start_heartbeat(self, now: datetime | None = None) -> int:
        """Record this process and report earlier ones that died. Returns how many were found dead."""
        now = now or _now()
        dead = 0
        for row in self.store.open_rows():
            beat_at = _aware(row.beat_at)
            silent = (now - beat_at).total_seconds()
            if silent > self.dead_after:
                dead += 1
                self.store.close(row.boot_id, beat_at)
                self._alert(
                    f"crash:{row.boot_id}",
                    f"A server process stopped without a clean shutdown: last heartbeat {int(silent)}s ago, "
                    f"peak memory {row.peak_mb} MB. Usually it was killed for using too much memory.",
                )
        self.store.begin(self.boot_id, now)
        self.store.prune(now - timedelta(days=self.keep_days))
        return dead

    def beat(self, now: datetime | None = None) -> None:
        self.store.beat(self.boot_id, now or _now(), self.peak_mb)

    def stop(self) -> None:
        """Mark this process as cleanly stopped and end the background thread."""
        self._stop.set()
        with contextlib.suppress(Exception):  # shutting down; never raise
            self.store.end(self.boot_id, _now(), self.peak_mb)

    def tick(self) -> None:
        """One round of the background loop: memory check, and a heartbeat when one is due."""
        self.check_memory()
        if self._clock() - getattr(self, "_last_beat", float("-inf")) >= self.beat_every:
            self.beat()
            self._last_beat = self._clock()

    def start(self, interval: float = 5.0) -> None:
        """Start the heartbeat and the background loop once. Never raises."""
        if self._thread is not None:
            return
        try:
            self.start_heartbeat()
        except Exception as exc:  # noqa: BLE001 - e.g. the database is not ready yet
            capture(exc)
        atexit.register(self.stop)

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - the watcher must never die
                    capture(exc)

        self._thread = threading.Thread(target=loop, daemon=True, name="faultbell-watch")
        self._thread.start()


class WatchMiddleware:
    """Pure ASGI middleware: counts response status per route template. An exception counts as a 500."""

    def __init__(self, app: Any, watch: Watch) -> None:
        self.app = app
        self.watch = watch

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        status = {"code": 500}

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = getattr(scope.get("route"), "path", None) or scope.get("path", "")
            self.watch.record(scope.get("method", "GET"), route, status["code"])
