"""Lifespan shutdown (Phase 0 finding, owed in Phase 1a): the daemon drains
in-flight store workers under a deadline, checkpoints the WAL and closes the
connection in a finally block, so a launchd bootout (SIGTERM, SIGKILL 5 s
later) finds nothing left to kill and no WAL to replay."""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace

import duckdb
import pytest
from fastapi.testclient import TestClient

from heliosd import main
from heliosd.config import Settings
from heliosd.store import db

TOKEN = "test-token-0123456789"
H = {"X-Helios-Token": TOKEN}


def _settings(tmp_path):
    return Settings(raw={"server": {"ingest_token": TOKEN}, "owner": {"timezone": "Asia/Dubai"},
                         "storage": {"db_path": str(tmp_path / "helios.duckdb")},
                         "notifications": {"macos_alerts": False}})


def _reopen_count(path, table) -> int:
    c = duckdb.connect(str(path), read_only=True)
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


def test_lifespan_exit_checkpoints_and_closes_so_no_wal_remains(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    app = main.create_app(_settings(tmp_path))
    with TestClient(app) as c:
        r = c.post("/ingest", json={"batch_id": "b", "samples": [
            {"hk_type": "HKQuantityTypeIdentifierStepCount", "value": 12, "unit": "count", "start": "2026-07-01T06:00:00Z",
             "end": "2026-07-01T06:05:00Z", "source_name": "Owner’s Ultra 1", "uuid": "u1"}]}, headers=H)
        assert r.json()["accepted"] == 1
        assert app.state.stopping is False and isinstance(app.state.workers, set)
    # the lifespan has exited: loops stopped, WAL checkpointed, connection closed
    assert app.state.stopping is True
    wal = tmp_path / "helios.duckdb.wal"
    assert not wal.exists() or wal.stat().st_size == 0
    assert _reopen_count(tmp_path / "helios.duckdb", "samples") == 1
    with pytest.raises(duckdb.Error):
        app.state.conn.execute("SELECT 1")


def test_shutdown_waits_for_a_running_store_worker_then_checkpoints(tmp_path):
    path = tmp_path / "helios.duckdb"

    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(conn=db.connect(path), workers=set(), stopping=False))

        def slow_write():
            time.sleep(0.3)
            db.execute(app.state.conn, "INSERT INTO events (event_id, kind, ts, payload) VALUES ('e1', 'note', now()::TIMESTAMP, '{}')")
            return "written"

        worker = asyncio.create_task(main.run_worker(app, slow_write))
        await asyncio.sleep(0.05)
        assert len(app.state.workers) == 1
        out = await main.shutdown_store(app, tasks=[], grace=2.0)
        return out, await worker

    out, result = asyncio.run(scenario())
    assert out == {"workers_pending": 1, "drained": True, "checkpointed": True, "closed": True} and result == "written"
    assert _reopen_count(path, "events") == 1
    wal = tmp_path / "helios.duckdb.wal"
    assert not wal.exists() or wal.stat().st_size == 0


def test_shutdown_closes_anyway_when_a_worker_outlives_the_grace(tmp_path):
    path = tmp_path / "helios.duckdb"

    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(conn=db.connect(path), workers=set(), stopping=False))

        def too_slow():
            time.sleep(1.0)
            db.execute(app.state.conn, "SELECT 1")   # the connection is gone by now: fails loudly, never hangs

        worker = asyncio.create_task(main.run_worker(app, too_slow))
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        out = await main.shutdown_store(app, tasks=[], grace=0.2)
        elapsed = time.monotonic() - t0
        with contextlib.suppress(Exception):
            await worker
        return out, elapsed

    out, elapsed = asyncio.run(scenario())
    assert out["drained"] is False and out["closed"] is True and out["workers_pending"] == 1
    assert elapsed < 1.5                                   # bounded by the grace, not by the worker
    assert _reopen_count(path, "events") == 0


def test_cancelling_a_loop_task_does_not_lose_its_running_worker():
    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(workers=set(), stopping=False))
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def work():
            loop.call_soon_threadsafe(started.set)
            time.sleep(0.2)
            return 1

        task = asyncio.create_task(main.run_worker(app, work))
        await started.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        pending = {f for f in app.state.workers if not f.done()}
        assert len(pending) == 1                           # the thread is still running and still tracked
        await asyncio.wait(pending, timeout=2.0)
        return app.state.workers

    assert asyncio.run(scenario()) == set()                # discarded once the thread finished

    assert main.GRACEFUL_HTTP_S + main.SHUTDOWN_GRACE_S < 5  # launchd's SIGTERM-to-SIGKILL budget
