from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from faultbell import watch as watch_mod
from faultbell.watch import (
    Heartbeat,
    MemoryHeartbeatStore,
    ServerWatchAlert,
    SQLHeartbeatStore,
    Watch,
    WatchMiddleware,
    container_memory_limit_mb,
    process_memory_mb,
)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(**kw):
    alerts: list[str] = []
    clock = Clock()
    w = Watch(alert=alerts.append, clock=clock, memory_limit_mb=512, **kw)
    return w, alerts, clock


def test_5xx_burst_alerts_once_per_quiet_period():
    w, alerts, clock = make()
    assert not w.record("get", "/orders/{id}", 500)
    clock.t += 10
    assert not w.record("GET", "/orders/{id}", 502)
    clock.t += 10
    assert w.record("GET", "/orders/{id}", 503)
    assert alerts == ["3 5xx responses in 5 minutes on GET /orders/{id}"]
    clock.t += 10
    assert not w.record("GET", "/orders/{id}", 500)  # still quiet
    clock.t += 700
    for _ in range(3):
        w.record("GET", "/orders/{id}", 500)
    assert len(alerts) == 2


def test_window_drops_old_hits_and_ignores_success_and_health():
    w, alerts, clock = make()
    w.record("GET", "/a", 500)
    clock.t += 400
    w.record("GET", "/a", 500)
    w.record("GET", "/a", 500)
    assert alerts == []
    for _ in range(5):
        w.record("GET", "/health", 500)
        w.record("GET", "/a", 200)
    assert alerts == []


def test_4xx_burst_has_a_higher_bar():
    w, alerts, _ = make(burst_4xx=5)
    for _ in range(4):
        w.record("POST", "/login", 401)
    assert alerts == []
    w.record("POST", "/login", 429)
    assert alerts == ["5 4xx responses in 5 minutes on POST /login"]


def test_memory_alert_threshold_and_peak():
    w, alerts, _ = make()
    assert not w.check_memory(300)
    assert w.check_memory(420)
    assert alerts == ["Memory at 420 MB of 512 MB (82%)"]
    assert not w.check_memory(500)  # quiet period
    assert w.peak_mb == 500


def test_no_limit_means_no_memory_alert(monkeypatch):
    monkeypatch.delenv("FAULTBELL_MEMORY_LIMIT_MB", raising=False)
    monkeypatch.setattr(watch_mod, "container_memory_limit_mb", lambda *a, **k: None)
    w = Watch(alert=lambda m: None)
    assert w.memory_limit_mb is None and not w.check_memory(10_000)


def test_memory_limit_from_env(monkeypatch):
    monkeypatch.setenv("FAULTBELL_MEMORY_LIMIT_MB", "256")
    assert Watch().memory_limit_mb == 256


def test_container_limit_reading(tmp_path):
    v2 = tmp_path / "memory.max"
    v2.write_text("536870912\n")
    assert container_memory_limit_mb([str(v2)]) == 512
    v2.write_text("max\n")
    unlimited = tmp_path / "limit_in_bytes"
    unlimited.write_text(str(1 << 62))
    assert container_memory_limit_mb([str(v2), str(unlimited), str(tmp_path / "missing")]) is None


def test_process_memory_is_positive():
    assert process_memory_mb() > 0


def _crash_flow(store):
    alerts: list[str] = []
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    old = Watch(store=store, alert=alerts.append)
    old.start_heartbeat(now - timedelta(minutes=20))
    old.peak_mb = 497
    old.beat(now - timedelta(minutes=10))  # then it was killed: no stop()
    clean = Watch(store=store, alert=alerts.append)
    clean.start_heartbeat(now - timedelta(minutes=9))
    clean.stop()
    new = Watch(store=store, alert=alerts.append)
    assert new.start_heartbeat(now) == 1
    assert len(alerts) == 1 and "last heartbeat 600s ago" in alerts[0] and "497 MB" in alerts[0]
    again = Watch(store=store, alert=alerts.append)
    assert again.start_heartbeat(now + timedelta(seconds=30)) == 0  # the dead one was closed
    return store


def test_crash_detection_memory_store():
    store = _crash_flow(MemoryHeartbeatStore())
    store.prune(datetime(2026, 1, 1, 11, 55, tzinfo=timezone.utc))
    assert all(r.started_at >= datetime(2026, 1, 1, 11, 55, tzinfo=timezone.utc) for r in store.rows.values())


def test_crash_detection_sql_store(tmp_path):
    sa = pytest.importorskip("sqlalchemy")
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'hb.db'}")
    store = _crash_flow(SQLHeartbeatStore(engine))
    assert all(isinstance(r, Heartbeat) for r in store.open_rows())
    SQLHeartbeatStore(engine)  # second instance reuses the table


def test_default_alert_goes_through_faultbell(monkeypatch):
    seen: list[BaseException] = []
    monkeypatch.setattr(watch_mod, "capture", lambda exc, **kw: seen.append(exc))
    w = Watch(memory_limit_mb=100)
    w.check_memory(90)
    assert isinstance(seen[0], ServerWatchAlert) and "90 MB of 100 MB" in str(seen[0])


def test_start_runs_loop_and_stop_ends_cleanly(monkeypatch):
    monkeypatch.setattr(watch_mod.atexit, "register", lambda fn: None)
    store = MemoryHeartbeatStore()
    w = Watch(store=store, memory_limit_mb=1_000_000, beat_every=0, alert=lambda m: None)
    w.start(interval=0.01)
    w.start(interval=0.01)  # second call is a no-op
    deadline = time.time() + 2
    while store.rows[w.boot_id].beat_at == store.rows[w.boot_id].started_at and time.time() < deadline:
        time.sleep(0.02)
    assert store.rows[w.boot_id].beat_at > store.rows[w.boot_id].started_at
    w.stop()
    assert store.rows[w.boot_id].ended_at is not None
    assert w._thread is not None
    w._thread.join(1)
    assert not w._thread.is_alive()


def test_start_survives_a_broken_store(monkeypatch):
    seen: list[BaseException] = []
    monkeypatch.setattr(watch_mod, "capture", lambda exc, **kw: seen.append(exc))
    monkeypatch.setattr(watch_mod.atexit, "register", lambda fn: None)

    class Broken(MemoryHeartbeatStore):
        def open_rows(self):
            raise RuntimeError("database not ready")

        def end(self, *a):
            raise RuntimeError("gone")

    w = Watch(store=Broken(), memory_limit_mb=1_000_000)
    w.tick = lambda: (_ for _ in ()).throw(ValueError("tick failed"))  # type: ignore[method-assign]
    w.start(interval=0.01)
    time.sleep(0.1)
    w.stop()
    assert any("database not ready" in str(e) for e in seen) and any("tick failed" in str(e) for e in seen)


def test_middleware_counts_by_route_template():
    pytest.importorskip("fastapi")
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    alerts: list[str] = []
    w = Watch(alert=alerts.append, memory_limit_mb=512)
    app = FastAPI()
    app.add_middleware(WatchMiddleware, watch=w)

    @app.get("/orders/{order_id}")
    def order(order_id: int) -> dict:
        if order_id > 100:
            raise HTTPException(503)
        return {"id": order_id}

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    client = TestClient(app, raise_server_exceptions=False)
    for i in (101, 102, 103):
        assert client.get(f"/orders/{i}").status_code == 503
    assert client.get("/orders/1").status_code == 200
    assert alerts == ["3 5xx responses in 5 minutes on GET /orders/{order_id}"]
    for _ in range(3):
        client.get("/boom")
    assert alerts[-1] == "3 5xx responses in 5 minutes on GET /boom"


@pytest.mark.anyio
async def test_middleware_passes_through_non_http():
    calls: list[str] = []

    async def app(scope, receive, send):
        calls.append(scope["type"])

    mw = WatchMiddleware(app, Watch(alert=lambda m: None, memory_limit_mb=1))
    await mw({"type": "lifespan"}, None, None)
    assert calls == ["lifespan"]
