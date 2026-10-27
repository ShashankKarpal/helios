"""Rank 2, the morning step (owner yes 2026-10-23): heliosd publishes the
morning brief itself (the deterministic template, never the local model), so a
complete brief exists before anyone opens the app; and /api/brief-check, the
one line the freshness tool and /api/health carry (the 09:05 iPhone Shortcut
that read it is retired, 2026-10-27). Since the rank 3 fix (2026-10-27) it
publishes never before 07:30, from 07:30 once last night's recovery is in and
the night has settled, and from 09:30 at the latest (test_rank3_settle.py has
the settled night). The loop runs with the real planner and a pinned clock.
Synthetic values only."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from heliosd import main
from heliosd.config import Settings
from heliosd.narrative import brief
from heliosd.signals import recompute as rc
from heliosd.signals.markers import owner_device
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy

DUBAI = ZoneInfo("Asia/Dubai")
TOKEN = "test-token-0123456789"
WINDOW, FLOOR, BY, POLL = (300, 720), 450, 570, 300     # 05:00-12:00, 07:30, 09:30, 5 minutes
COMPLETE = [("recovery_score", "favorable", 71.5), ("sleep_duration", "flag", 5.25)]


def _at(h, m, s=0):
    return datetime(2026, 10, 24, h, m, s, tzinfo=DUBAI)


@pytest.mark.parametrize("now,complete,recovery_in,settled,expected", [
    (_at(4, 0), False, False, False, ("wait", POLL)),          # before the window: the next check is capped
    (_at(4, 59, 30), False, True, True, ("wait", 30)),         # the window opens in 30 s
    (_at(6, 10), False, False, False, ("wait", POLL)),         # the night is not in, 07:30 is far
    (_at(6, 10), False, True, True, ("wait", POLL)),           # the night is in and settled: not before 07:30
    (_at(7, 27), False, True, True, ("wait", 180)),            # check again at 07:30
    (_at(7, 30), False, True, True, ("publish", POLL)),        # 07:30, in and settled: publish now
    (_at(7, 30), False, True, False, ("wait", POLL)),          # in but still changing: wait
    (_at(7, 30), False, False, True, ("wait", POLL)),          # the recompute has not brought it in yet
    (_at(9, 27), False, False, False, ("wait", 180)),          # check again at 09:30
    (_at(9, 30), False, False, False, ("publish", POLL)),      # 09:30: publish what is there
    (_at(11, 59), False, False, False, ("publish", POLL)),     # still waiting for Whoop: keep trying
    (_at(12, 0), False, True, True, ("wait", POLL)),           # the window has closed
    (_at(7, 40), True, True, True, ("wait", POLL)),            # today's complete brief is in
])
def test_morning_plan(now, complete, recovery_in, settled, expected):
    assert brief.morning_plan(now, WINDOW, FLOOR, BY, POLL, complete, recovery_in, settled) == expected


def test_parse_hhmm():
    assert brief.parse_hhmm("08:30") == 510 and brief.parse_hhmm("0:05") == 5
    assert brief.parse_hhmm(time(8, 30)) == 510                    # a TOML local time
    for bad in ("24:00", "8:60", "half eight", None):
        with pytest.raises(ValueError):
            brief.parse_hhmm(bad)


def _settings(tmp_path, brief_cfg=None, **server):
    raw = {"server": {"ingest_token": TOKEN, **server}, "owner": {"timezone": "Asia/Dubai"},
           "storage": {"db_path": str(tmp_path / "helios.duckdb")}, "notifications": {"macos_alerts": False}}
    if brief_cfg is not None:
        raw["brief"] = brief_cfg
    return Settings(raw=raw)


def test_morning_settings_defaults_wait_for_the_startup_recompute(tmp_path):
    assert main.morning_settings(_settings(tmp_path)) == ((300, 720), 450, 570, 300, 150.0, 30.0)
    assert main.morning_settings(_settings(tmp_path, first_tick_seconds=40))[4] == 70.0
    assert main.morning_settings(_settings(tmp_path, {"publish_by": time(8, 0)}))[2] == 480


def _owned(conn, policy, day, rows):
    with db.transaction(conn) as c:
        c.execute("DELETE FROM signals WHERE date = ?", [day])
        for metric, state, value in rows:
            c.execute("INSERT INTO signals (date, metric, state, value, unit, device_key, context_flags, why) "
                      "VALUES (?, ?, ?, ?, 'u', ?, '[]', 'synthetic')",
                      [day, metric, state, value, owner_device(policy, metric)])


def test_morning_state_reads_the_file_first_and_signals_only_while_needed(tmp_path, monkeypatch):
    conn, policy = db.connect_memory(), MetricPolicy(default_tz="Asia/Dubai")
    f = tmp_path / "last_valid_brief.json"
    now = _at(7, 0)
    day = now.date()
    assert brief.morning_state(conn, policy, f, now) == (False, False)
    _owned(conn, policy, day, [("steps", "in_progress", 1234)])
    assert brief.morning_state(conn, policy, f, now) == (False, False)
    _owned(conn, policy, day, COMPLETE)
    assert brief.morning_state(conn, policy, f, now) == (False, True)
    brief.record_brief(f, day, "template", False, 2, [], policy.zone, now)

    def no_store(*a, **k):
        raise AssertionError("the store was read although today's brief is complete")
    monkeypatch.setattr(brief, "signals_for", no_store)
    assert brief.morning_state(conn, policy, f, now) == (True, True)


class _NoModel:
    primary = fallback = "none"

    def available(self):
        raise AssertionError("the morning step asked LM Studio")


def _app(tmp_path, monkeypatch, local, brief_cfg=None, sleeps_allowed=1):
    """App state set by hand (no lifespan), the loop run directly with the
    real planner and the clock pinned at `local`: the first `sleeps_allowed`
    sleeps return, the next one stops the loop."""
    delays = []

    async def fake_sleep(s):
        delays.append(s)
        if len(delays) > sleeps_allowed:
            raise asyncio.CancelledError

    monkeypatch.setattr(main, "SLEEP", fake_sleep)
    monkeypatch.setattr(main, "_now_local", lambda zone: local.astimezone(zone))
    monkeypatch.setattr(brief, "_utcnow", lambda: local.astimezone(timezone.utc))
    app = main.create_app(_settings(tmp_path, brief_cfg if brief_cfg is not None else {"first_check_seconds": 0}))
    app.state.stopping, app.state.workers = False, set()
    app.state.conn, app.state.policy = db.connect_memory(), MetricPolicy(default_tz="Asia/Dubai")
    app.state.lm, app.state.narrative_inflight = _NoModel(), set()
    app.state.brief_status_file = tmp_path / "last_valid_brief.json"
    return app, delays


def _run(app):
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main._brief_morning_loop(app))


def _rec(app):
    return json.loads(app.state.brief_status_file.read_text(encoding="utf-8"))


def _settled(monkeypatch):
    """The night counts as settled (whoop.night_stable is tested in test_rank3_settle.py)."""
    monkeypatch.setattr(main, "night_stable", lambda *a, **k: (True, "settled"))


def test_at_0610_with_the_night_in_nothing_is_published_before_0730(tmp_path, monkeypatch):
    app, delays = _app(tmp_path, monkeypatch, _at(6, 10))
    _settled(monkeypatch)
    _owned(app.state.conn, app.state.policy, _at(6, 10).date(), COMPLETE)
    _run(app)
    assert not app.state.brief_status_file.exists() and delays == [0.0, POLL]


def test_at_0740_with_the_night_in_and_settled_the_template_is_published_and_complete(tmp_path, monkeypatch, caplog):
    app, delays = _app(tmp_path, monkeypatch, _at(7, 40), sleeps_allowed=2)
    _settled(monkeypatch)
    _owned(app.state.conn, app.state.policy, _at(7, 40).date(), COMPLETE)
    with caplog.at_level(logging.INFO, logger="heliosd"):
        _run(app)
    rec = _rec(app)
    assert set(rec) == {"complete"} and rec["complete"]["kind"] == "template"
    assert rec["complete"]["date"] == "2026-10-24" and rec["complete"]["first_published_at"] == "2026-10-24T07:40:00+04:00"
    assert delays == [0.0, POLL, POLL] and app.state.morning_step == "on"
    notes = [r.getMessage() for r in caplog.records if "brief morning step" in r.getMessage()]
    assert notes == ["brief morning step: 2026-10-24: complete brief recorded"]


def test_at_0610_without_the_night_nothing_is_published(tmp_path, monkeypatch):
    app, delays = _app(tmp_path, monkeypatch, _at(6, 10))
    _owned(app.state.conn, app.state.policy, _at(6, 10).date(), [("steps", "in_progress", 1234)])
    _run(app)
    assert not app.state.brief_status_file.exists() and delays == [0.0, POLL]


def test_at_0927_without_the_night_the_next_check_is_0930(tmp_path, monkeypatch):
    app, delays = _app(tmp_path, monkeypatch, _at(9, 27))
    _run(app)
    assert not app.state.brief_status_file.exists() and delays == [0.0, 180]


def test_from_0930_the_waiting_brief_is_published_and_logged_once(tmp_path, monkeypatch, caplog):
    app, delays = _app(tmp_path, monkeypatch, _at(9, 30), sleeps_allowed=3)
    _owned(app.state.conn, app.state.policy, _at(9, 30).date(), [("steps", "in_progress", 1234)])
    with caplog.at_level(logging.INFO, logger="heliosd"):
        _run(app)
    rec = _rec(app)
    assert set(rec) == {"waiting"} and "recovery_score" in rec["waiting"]["awaiting"]
    notes = [r.getMessage() for r in caplog.records if "brief morning step" in r.getMessage()]
    assert notes == ["brief morning step: 2026-10-24: published while waiting for last night's Whoop data"]
    assert delays == [0.0, POLL, POLL, POLL]


def test_a_refused_publish_is_retried_after_a_minute(tmp_path, monkeypatch, caplog):
    app, delays = _app(tmp_path, monkeypatch, _at(7, 40))
    _settled(monkeypatch)
    _owned(app.state.conn, app.state.policy, _at(7, 40).date(), COMPLETE)
    monkeypatch.setattr(brief, "_publish", lambda *a, **k: False)        # a recompute moved the inputs
    with caplog.at_level(logging.INFO, logger="heliosd"):
        _run(app)
    assert not app.state.brief_status_file.exists() and delays == [0.0, main.MORNING_RETRY_S]
    assert any("not recorded" in r.getMessage() and "retry in 60 s" in r.getMessage() for r in caplog.records)


def test_a_cached_brief_from_before_a_restart_is_recorded(tmp_path, monkeypatch):
    app, delays = _app(tmp_path, monkeypatch, _at(9, 30))
    day = _at(9, 30).date()
    _owned(app.state.conn, app.state.policy, day, COMPLETE)
    brief.generate_brief(app.state.conn, None, day, "Owner", allow_llm=False, policy=app.state.policy, today=day)
    _run(app)                                         # the brief was cached, the status file never written
    assert _rec(app)["complete"]["date"] == "2026-10-24"


def test_the_loop_ends_when_the_daemon_stops(tmp_path, monkeypatch):
    app, delays = _app(tmp_path, monkeypatch, _at(6, 10))

    async def stopping_sleep(s):
        delays.append(s)
        app.state.stopping = True

    monkeypatch.setattr(main, "SLEEP", stopping_sleep)
    asyncio.run(main._brief_morning_loop(app))          # returns, no exception
    assert delays == [0.0] and not app.state.brief_status_file.exists()


def test_a_failed_tick_is_logged_and_retried_at_the_next_poll(tmp_path, monkeypatch, caplog):
    app, delays = _app(tmp_path, monkeypatch, _at(6, 10))

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic store error")

    monkeypatch.setattr(main, "morning_state", broken)
    with caplog.at_level(logging.WARNING, logger="heliosd"):
        _run(app)
    assert delays == [0.0, POLL]
    assert any("brief morning step failed (RuntimeError)" in r.getMessage() for r in caplog.records)
    assert not any("synthetic store error" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("cfg", [{"publish_by": "13:00"}, {"morning_window": "12:00-05:00"},
                                 {"morning_window": "05:00-08:45", "publish_by": "08:30"},
                                 {"publish_by": "late"}, {"poll_minutes": "often"}, {"poll_minutes": 0},
                                 {"poll_minutes": "inf"}, {"first_check_seconds": "inf"},
                                 {"first_check_seconds": -1}])
def test_bad_settings_turn_the_step_off_and_say_so(tmp_path, monkeypatch, caplog, cfg):
    app, delays = _app(tmp_path, monkeypatch, _at(6, 10), brief_cfg=cfg)
    with caplog.at_level(logging.WARNING, logger="heliosd"):
        asyncio.run(main._brief_morning_loop(app))           # returns at once
    assert delays == [] and app.state.morning_step.startswith("off: ")
    assert any("brief morning step off" in r.getMessage() for r in caplog.records)


def test_the_defaults_wait_until_after_the_startup_recompute(tmp_path, monkeypatch):
    app, delays = _app(tmp_path, monkeypatch, _at(6, 10), brief_cfg={}, sleeps_allowed=0)
    _run(app)
    assert delays == [150.0]


def test_the_check_line(tmp_path):
    assert brief.brief_check(True, False, 0.2) == "ok"
    assert brief.brief_check(False, False, None) == "early: no complete brief yet; due 10:00 in the reporting zone"
    assert brief.brief_check(False, True, None) == "late: no complete brief for today"
    assert brief.brief_check(False, True, 26.4) == "late: no complete brief for today; the last complete one is 26 h old"
    f = tmp_path / "last_valid_brief.json"
    at_1005 = datetime(2026, 10, 24, 6, 5, tzinfo=timezone.utc)           # 10:05 Dubai
    assert brief.brief_status(f, DUBAI, at_1005, full=False)["check"] == "late: no complete brief for today"
    # 09:05 Dubai (the old 09:05 check, or later on a phone east of Dubai): not due yet, and never "ok" without a brief.
    assert brief.brief_status(f, DUBAI, at_1005 - timedelta(hours=1))["check"].startswith("early: ")
    brief.record_brief(f, (at_1005 - timedelta(days=1)).astimezone(DUBAI).date(), "template", False, 3, [], DUBAI,
                       at_1005 - timedelta(hours=26))
    assert brief.brief_status(f, DUBAI, at_1005)["check"] == "late: no complete brief for today; the last complete one is 26 h old"
    brief.record_brief(f, at_1005.astimezone(DUBAI).date(), "template", False, 4, [], DUBAI, at_1005 - timedelta(minutes=50))
    assert brief.brief_status(f, DUBAI, at_1005, full=False)["check"] == "ok"


def test_the_brief_check_route_is_open_plain_text_and_never_reads_the_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    with TestClient(main.create_app(_settings(tmp_path))) as c:
        r = c.get("/api/brief-check")                              # no token
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
        assert r.text.startswith(("early: ", "late: "))
        zone = c.app.state.policy.zone
        brief.record_brief(c.app.state.brief_status_file, rc.reporting_today(zone), "template", False, 3, [], zone)
        assert c.get("/api/brief-check").text == "ok"

        def broken(*a, **k):
            raise RuntimeError("store down")
        monkeypatch.setattr(main.db, "fetchall", broken)
        monkeypatch.setattr(main.db, "fetchdicts", broken)
        assert c.get("/api/brief-check").text == "ok"              # the store plays no part
        with pytest.raises(RuntimeError):                         # while the health route needs it
            c.get("/api/health")
        assert c.get("/api/brief-check", headers={"X-Helios-Token": "wrong"}).text == "ok"


def test_the_daemon_starts_the_morning_step_and_freshness_says_so(tmp_path, monkeypatch):
    delays = []

    async def fake_sleep(s):
        delays.append(s)
        raise asyncio.CancelledError

    monkeypatch.setattr(main, "SLEEP", fake_sleep)
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    app = main.create_app(_settings(tmp_path, {"first_check_seconds": 7}))

    async def scenario():
        async with app.router.lifespan_context(app):
            for _ in range(100):
                if 7.0 in delays:
                    break
                await asyncio.sleep(0.02)
            assert app.state.morning_step == "on"
    asyncio.run(scenario())
    assert 7.0 in delays
    (tmp_path / "off").mkdir()
    with TestClient(main.create_app(_settings(tmp_path / "off", {"poll_minutes": 0}))) as c:
        step = c.get("/api/freshness", headers={"X-Helios-Token": TOKEN}).json()["brief"]["morning_step"]
        assert step.startswith("off: poll_minutes")
