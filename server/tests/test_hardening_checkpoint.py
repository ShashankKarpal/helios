"""Hardening round 2026-10-28, H5: the shutdown budget (owner yes to a and b).

On 2026-10-28 at 09:39 a logout stopped heliosd and its shutdown checkpoint
overran the budget launchd allows, so the close was skipped and the WAL
replayed at the next start. (a) After each loop recompute (the journal drain,
the hourly tick, the recompute after a Whoop pull) the store is checkpointed in
the same worker queue, so the WAL a shutdown must flush stays small. The
checkpoint never waits for the store lock: busy means skipped, the next one
catches up. (b) Each such checkpoint logs the WAL size before and after and
its time (a WARNING past CHECKPOINT_SLOW_S), and the shutdown line carries the
WAL size and the time of each step. (c) launchd ExitTimeOut is left alone."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from heliosd import main
from heliosd.store import db
from tests.test_wave1_r2 import _OneTickAsyncio


def _app(path):
    return SimpleNamespace(state=SimpleNamespace(conn=db.connect(path), workers=set(), stopping=False,
                                                 db_path=path))


def _fill(app, n=200):
    for i in range(n):
        db.execute(app.state.conn, "INSERT INTO events (event_id, kind, ts, payload) "
                                   "VALUES (?, 'note', now()::TIMESTAMP, '{}')", [f"e{i}"])


def test_wal_bytes_reads_the_file_beside_the_store(tmp_path):
    path = tmp_path / "helios.duckdb"
    app = _app(path)
    _fill(app)
    assert db.wal_bytes(path) > 0
    assert db.wal_bytes(tmp_path / "missing.duckdb") == 0


def test_a_checkpoint_after_a_recompute_empties_the_wal_and_says_so(tmp_path, caplog):
    path = tmp_path / "helios.duckdb"
    app = _app(path)
    _fill(app)
    before = db.wal_bytes(path)
    assert before > 0
    with caplog.at_level(logging.INFO, logger="heliosd"):
        out = asyncio.run(main.checkpoint_after(app, "drain"))
    assert out["checkpointed"] is True and out["wal_before"] == before and out["wal_after"] == 0
    line = [r for r in caplog.records if "checkpoint after drain" in r.getMessage()]
    assert len(line) == 1 and line[0].levelno == logging.INFO
    assert f"wal {before} to 0 bytes" in line[0].getMessage()


def test_a_slow_checkpoint_is_a_warning(tmp_path, caplog, monkeypatch):
    path = tmp_path / "helios.duckdb"
    app = _app(path)
    _fill(app, 5)
    monkeypatch.setattr(main, "CHECKPOINT_SLOW_S", -1.0)
    with caplog.at_level(logging.INFO, logger="heliosd"):
        asyncio.run(main.checkpoint_after(app, "hourly recompute"))
    line = [r for r in caplog.records if "checkpoint after hourly recompute" in r.getMessage()]
    assert line and line[0].levelno == logging.WARNING


def test_a_busy_store_lock_skips_at_once(tmp_path, caplog):
    path = tmp_path / "helios.duckdb"
    app = _app(path)
    _fill(app, 5)
    held, release = threading.Event(), threading.Event()

    def hold():
        with db._lock:
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        t0 = time.monotonic()
        with caplog.at_level(logging.INFO, logger="heliosd"):
            out = asyncio.run(main.checkpoint_after(app, "drain"))
        assert time.monotonic() - t0 < 0.5
    finally:
        release.set()
        t.join()
    assert out["checkpointed"] is False and out["skipped"] == "lock busy"
    assert any("checkpoint after drain skipped" in r.getMessage() for r in caplog.records)
    assert db.wal_bytes(path) > 0


def test_nothing_runs_once_the_shutdown_began(tmp_path):
    app = _app(tmp_path / "helios.duckdb")
    app.state.stopping = True
    assert asyncio.run(main.checkpoint_after(app, "drain")) == {"checkpointed": False, "skipped": "stopping"}


def test_a_recompute_after_a_pull_checkpoints_and_an_unchanged_pull_does_not(tmp_path, monkeypatch):
    calls = []

    async def record(app, what):
        calls.append(what)
    monkeypatch.setattr(main, "checkpoint_after", record)
    monkeypatch.setattr(main, "recompute", lambda *a, **k: {"ok": 1})
    app = _app(tmp_path / "helios.duckdb")
    app.state.policy = app.state.registry = None
    asyncio.run(main._recompute_after_pull(app, {"dates": ["2026-10-28"]}, 2))
    asyncio.run(main._recompute_after_pull(app, {"dates": []}, 2))
    assert calls == ["whoop pull"]


def test_the_journal_drain_checkpoints_only_when_it_did_work(tmp_path, monkeypatch):
    from heliosd.signals import recompute as rc
    calls, outs = [], [{"dates": 1}, None]

    async def record(app, what):
        calls.append(what)
    monkeypatch.setattr(main, "checkpoint_after", record)
    monkeypatch.setattr(rc, "drain_journal", lambda *a, **k: outs.pop(0))
    monkeypatch.setattr(main, "asyncio", _OneTickAsyncio(ticks=2))
    app = _app(tmp_path / "helios.duckdb")
    app.state.policy = SimpleNamespace(zone=ZoneInfo("Asia/Dubai"))
    app.state.registry = None
    app.state.ingest_clock = {"first": 0.0, "last": 0.0}
    app.state.reporting_day = rc.reporting_today(app.state.policy.zone)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main._recompute_loop(app))
    assert calls == ["drain"]


def test_the_shutdown_line_carries_the_wal_size_and_the_step_times(tmp_path, caplog):
    path = tmp_path / "helios.duckdb"
    app = _app(path)
    _fill(app)
    before = db.wal_bytes(path)
    with caplog.at_level(logging.INFO, logger="heliosd"):
        out = asyncio.run(main.shutdown_store(app, tasks=[], grace=0.5))
    assert out["wal_bytes"] == before and out["checkpointed"] is True and out["closed"] is True
    assert set(out["steps_s"]) == {"drain", "checkpoint", "close"}
    assert all(isinstance(v, float) and v >= 0 for v in out["steps_s"].values())
    assert any("shutdown:" in r.getMessage() and "wal_bytes" in r.getMessage() for r in caplog.records)
