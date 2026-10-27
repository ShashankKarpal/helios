"""Rank 3 fix (2026-10-27): the brief waits for a settled night.

On 2026-10-27 Whoop scored a provisional night at 04:11 while the owner was
still asleep; the morning step published it at 05:00, Whoop updated the night
at 06:21, and the phone kept the 05:00 numbers. Now nothing is recorded (the
status file, the glance) before [brief] not_before (07:30 in the reporting
zone); from 07:30 the brief is published once the night has settled
(whoop.night_stable: landed, the newest Whoop pull at most 30 minutes old and
not the pull that changed the night, at least settle_minutes after Whoop's last
change of the night, and the recompute after it done); from publish_by (09:30,
was 08:30) whatever is there. /api/today passes through the same gate. The
brief is due at 10:00 (was 09:00). The Whoop records go through the real import
(apply_records) and the real recompute (drain_journal). Synthetic values only."""

from __future__ import annotations

import asyncio
import json
import math
import time as clock
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from heliosd import main
from heliosd.config import Settings
from heliosd.ingest import whoop
from heliosd.narrative import brief
from heliosd.signals import recompute as rc
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

DUBAI = ZoneInfo("Asia/Dubai")
DAY = date(2026, 10, 24)
TOKEN = "test-token-0123456789"
H = {"X-Helios-Token": TOKEN}
START = datetime(2026, 10, 23, 23, 40, tzinfo=DUBAI)


def _z(h, m, s=0):
    return datetime(2026, 10, 24, h, m, s, tzinfo=DUBAI)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# The night as Whoop first scored it (the owner still asleep) and as Whoop
# revised it later. Synthetic numbers, never the owner's.
PROVISIONAL = {"end": _z(4, 11), "created": _z(4, 11), "updated": _z(4, 11), "stages": (220, 60, 60),
               "recovery": 61, "hrv": 41.2, "rhr": 57}
UPDATED = {"end": _z(6, 9), "created": _z(4, 11), "updated": _z(6, 21), "stages": (300, 70, 70),
           "recovery": 66, "hrv": 44.0, "rhr": 55}
PROVISIONAL_TEXTS = {"recovery_score": "61%", "sleep_duration": "5h 40m", "hrv_rmssd": "41.2 ms", "resting_hr": "57 bpm"}
UPDATED_TEXTS = {"recovery_score": "66%", "sleep_duration": "7h 20m", "hrv_rmssd": "44 ms", "resting_hr": "55 bpm"}


def _sleep(night, id_="s-night", nap=False, start=START):
    light, sws, rem = night["stages"]
    return {"id": id_, "user_id": 1, "cycle_id": 900, "created_at": _iso(night["created"]),
            "updated_at": _iso(night["updated"]), "start": _iso(start), "end": _iso(night["end"]),
            "timezone_offset": "+04:00", "nap": nap, "score_state": "SCORED",
            "score": {"stage_summary": {"total_in_bed_time_milli": 9 * 3.6e6, "total_awake_time_milli": 20 * 60000,
                                        "total_light_sleep_time_milli": light * 60000,
                                        "total_slow_wave_sleep_time_milli": sws * 60000,
                                        "total_rem_sleep_time_milli": rem * 60000},
                      "sleep_needed": {"baseline_milli": 7.9 * 3.6e6}, "sleep_efficiency_percentage": 90.0,
                      "respiratory_rate": 15.1}}


def _recovery(night, sleep_id="s-night"):
    return {"cycle_id": 900, "sleep_id": sleep_id, "user_id": 1, "created_at": _iso(night["created"] + timedelta(minutes=1)),
            "updated_at": _iso(night["updated"] + timedelta(minutes=1)), "score_state": "SCORED",
            "timezone_offset": "+04:00",
            "score": {"recovery_score": night["recovery"], "hrv_rmssd_milli": night["hrv"],
                      "resting_heart_rate": night["rhr"]}}


def _store():
    conn = db.connect_memory()
    policy = MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    return conn, policy, SourceRegistry()


def _apply(conn, policy, at, sleeps=(), recoveries=()):
    """The records a pull starting at `at` fetched, stored by the real import
    (each record keeps the time of the pull that last changed it)."""
    return whoop.apply_records(conn, policy, {"recovery": list(recoveries), "sleep": list(sleeps), "cycle": []},
                               at + timedelta(seconds=5), f"whoop:{at:%H%M%S}",
                               cache_sweep=(DAY - timedelta(days=1), DAY))


def _pull(app, at, night):
    """One successful Whoop pull that started at `at`, then the recompute
    loop's drain of the journal it wrote."""
    _apply(app.state.conn, app.state.policy, at, [_sleep(night)], [_recovery(night)])
    rc.drain_journal(app.state.conn, app.state.policy, app.state.registry, today=DAY, now=at)
    app.state.whoop_last_ok_at = at.astimezone(timezone.utc).isoformat(timespec="seconds")


def _settings(tmp_path, brief_cfg=None):
    raw = {"server": {"ingest_token": TOKEN}, "owner": {"timezone": "Asia/Dubai"},
           "storage": {"db_path": str(tmp_path / "helios.duckdb")}, "notifications": {"macos_alerts": False}}
    raw["brief"] = {"first_check_seconds": 0, **(brief_cfg or {})}
    return Settings(raw=raw)


class _NoModel:
    primary = fallback = "none"

    def available(self):
        return False


def _app(tmp_path, brief_cfg=None):
    """App state set by hand (no lifespan) over an in-memory store with the
    real policy, for the morning loop run one tick at a time (_tick)."""
    app = main.create_app(_settings(tmp_path, brief_cfg))
    app.state.stopping, app.state.workers = False, set()
    app.state.conn, app.state.policy, app.state.registry = _store()
    app.state.lm, app.state.narrative_inflight = _NoModel(), set()
    app.state.brief_status_file = tmp_path / "last_valid_brief.json"
    app.state.whoop_last_ok_at = None
    return app


def _pin(monkeypatch, at):
    monkeypatch.setattr(main, "_now_local", lambda zone: at.astimezone(zone))
    monkeypatch.setattr(brief, "_utcnow", lambda: at.astimezone(timezone.utc))


def _tick(app, monkeypatch, at):
    """One tick of the real morning step at `at`; returns the delays it asked for."""
    _pin(monkeypatch, at)
    delays = []

    async def fake_sleep(s):
        delays.append(s)
        if len(delays) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(main, "SLEEP", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main._brief_morning_loop(app))
    return delays


def _status(app):
    f = app.state.brief_status_file
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def _glance(app):
    f = app.state.brief_status_file.with_name("glance.json")
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def _texts(g):
    return {r["metric"]: r["text"] for r in g["rows"]}


# ---- T1: the morning of 2026-10-27, replayed

def test_the_provisional_night_is_never_published_at_0500_and_0730_publishes_the_updated_one(tmp_path, monkeypatch):
    app = _app(tmp_path)
    _pull(app, _z(4, 57, 40), PROVISIONAL)            # the hourly pull that brought the provisional night
    _tick(app, monkeypatch, _z(5, 0))
    assert _status(app) is None and _glance(app) is None
    for t in (_z(5, 15), _z(5, 30), _z(5, 45), _z(6, 0), _z(6, 15)):
        _pull(app, t, PROVISIONAL)                    # unchanged
    # The window that went wrong: the provisional night unchanged, fresh and
    # settled; only the 07:30 floor holds it back.
    assert whoop.night_stable(app.state.conn, DAY, DUBAI, app.state.whoop_last_ok_at, _z(6, 15, 30))[0] is True
    _tick(app, monkeypatch, _z(6, 15, 30))
    assert _status(app) is None and _glance(app) is None
    _pull(app, _z(6, 30), UPDATED)                    # Whoop updated the night at 06:21
    _tick(app, monkeypatch, _z(6, 35))
    assert _status(app) is None and _glance(app) is None
    for t in (_z(6, 45), _z(7, 0), _z(7, 15), _z(7, 30)):
        _pull(app, t, UPDATED)                        # unchanged
    _tick(app, monkeypatch, _z(7, 30, 30))
    rec = _status(app)
    assert set(rec) == {"complete"} and rec["complete"]["first_published_at"] == "2026-10-24T07:30:30+04:00"
    g = _glance(app)
    assert g["complete"] is True and _texts(g) == UPDATED_TEXTS


def test_before_0730_nothing_is_published_with_the_night_in_and_the_next_check_is_0730(tmp_path, monkeypatch):
    app = _app(tmp_path)
    _pull(app, _z(6, 0), PROVISIONAL)
    _pull(app, _z(6, 45), PROVISIONAL)                # settled from here: unchanged, long after Whoop's last change
    _pull(app, _z(7, 15), PROVISIONAL)                # and still fresh at 07:27
    assert whoop.night_stable(app.state.conn, DAY, DUBAI, app.state.whoop_last_ok_at, _z(7, 27))[0] is True
    assert _tick(app, monkeypatch, _z(7, 27)) == [0.0, 180]
    assert _status(app) is None and _glance(app) is None


def test_from_0730_a_night_that_is_not_settled_waits_and_is_published_once_it_is(tmp_path, monkeypatch, caplog):
    app = _app(tmp_path)
    _pull(app, _z(7, 20), PROVISIONAL)                # the first pull to see the night: it changed it
    with caplog.at_level("INFO", logger="heliosd"):
        assert _tick(app, monkeypatch, _z(7, 30)) == [0.0, 300]
    assert _status(app) is None
    assert any("waiting for the night to settle: the last Whoop pull changed the night" in r.getMessage()
               for r in caplog.records)
    _pull(app, _z(7, 35), PROVISIONAL)                # unchanged, more than 30 minutes after 04:12
    _tick(app, monkeypatch, _z(7, 35, 20))
    assert _status(app)["complete"]["first_published_at"] == "2026-10-24T07:35:20+04:00"
    assert _texts(_glance(app)) == PROVISIONAL_TEXTS


# ---- T5: the settled rule

def _night_in(conn, policy, at, night=PROVISIONAL, **sleep_kw):
    _apply(conn, policy, at, [_sleep(night, **sleep_kw)], [_recovery(night, sleep_kw.get("id_", "s-night"))])


def _stable(conn, last_pull, now, settle=30):
    return whoop.night_stable(conn, DAY, DUBAI, last_pull.astimezone(timezone.utc).isoformat(), now, settle)


def test_the_night_is_settled_only_when_every_part_of_the_rule_holds():
    conn, policy, reg = _store()
    assert whoop.night_stable(conn, DAY, DUBAI, None, _z(7, 30)) == (False, "the night has not landed")
    _night_in(conn, policy, _z(6, 0))
    rc.drain_journal(conn, policy, reg, today=DAY)
    # (b) the newest pull is the one that wrote the night
    assert _stable(conn, _z(6, 0), _z(6, 10)) == (False, "the last Whoop pull changed the night")
    # all of it: unchanged since, 2 h 33 min after Whoop's last change (04:12), fresh, recomputed
    assert _stable(conn, _z(6, 45), _z(6, 50)) == (True, "settled")
    # (a) the newest pull is more than 30 minutes old
    assert _stable(conn, _z(6, 45), _z(7, 16)) == (False, "no Whoop pull in the last 30 minutes")
    assert whoop.night_stable(conn, DAY, DUBAI, None, _z(6, 50))[0] is False
    # (c) settle_minutes counts from Whoop's last change, not from the pull that brought it
    assert _stable(conn, _z(6, 45), _z(6, 50), settle=160) == (False, "changed less than 160 minutes before the last pull")
    assert _stable(conn, _z(6, 45), _z(6, 50), settle=150) == (True, "settled")
    early, early_policy, early_reg = _store()
    _night_in(early, early_policy, _z(4, 20))
    rc.drain_journal(early, early_policy, early_reg, today=DAY)
    assert _stable(early, _z(4, 35), _z(4, 36)) == (False, "changed less than 30 minutes before the last pull")
    # (d) a Whoop entry for today still in the journal: its recompute has not run
    rc.enqueue(conn, {DAY}, "whoop")
    assert _stable(conn, _z(6, 45), _z(6, 50)) == (False, "the recompute after the last pull is pending")
    rc.drain_journal(conn, policy, reg, today=DAY)
    rc.enqueue(conn, {DAY}, "bridge")                 # the phone's batches do not hold the brief back
    assert _stable(conn, _z(6, 45), _z(6, 50)) == (True, "settled")


def test_a_second_sleep_ending_today_counts_and_a_nap_does_not():
    conn, policy, reg = _store()
    _night_in(conn, policy, _z(5, 0))
    back_to_sleep = {"end": _z(7, 10), "created": _z(7, 10), "updated": _z(7, 12), "stages": (40, 0, 10),
                     "recovery": 61, "hrv": 41.2, "rhr": 57}
    _apply(conn, policy, _z(7, 15), [_sleep(back_to_sleep, id_="s-again", start=_z(6, 20))])
    rc.drain_journal(conn, policy, reg, today=DAY)
    assert _stable(conn, _z(7, 30), _z(7, 31)) == (False, "changed less than 30 minutes before the last pull")
    assert _stable(conn, _z(7, 45), _z(7, 46)) == (True, "settled")
    conn, policy, reg = _store()
    _night_in(conn, policy, _z(5, 0))
    nap = {"end": _z(7, 10), "created": _z(7, 10), "updated": _z(7, 12), "stages": (40, 0, 10),
           "recovery": 61, "hrv": 41.2, "rhr": 57}
    _apply(conn, policy, _z(7, 15), [_sleep(nap, id_="s-nap", nap=True, start=_z(6, 20))])
    rc.drain_journal(conn, policy, reg, today=DAY)
    assert _stable(conn, _z(7, 30), _z(7, 31)) == (True, "settled")


# ---- T6: the fallback at 09:30 and the 10:00 due time

def test_at_0930_a_night_that_never_settled_is_published_complete(tmp_path, monkeypatch):
    app = _app(tmp_path)
    _pull(app, _z(9, 20), UPDATED)                    # the newest pull changed the night: not settled
    _tick(app, monkeypatch, _z(9, 25))
    assert _status(app) is None
    _tick(app, monkeypatch, _z(9, 30))
    assert _status(app)["complete"]["first_published_at"] == "2026-10-24T09:30:00+04:00"
    assert _texts(_glance(app)) == UPDATED_TEXTS


def test_at_0930_without_the_night_a_waiting_brief_is_recorded_and_it_is_late_only_from_1000(tmp_path, monkeypatch):
    app = _app(tmp_path)
    assert _tick(app, monkeypatch, _z(9, 27)) == [0.0, 180]          # the next check is 09:30
    _tick(app, monkeypatch, _z(9, 30))
    rec = _status(app)
    assert set(rec) == {"waiting"} and "recovery_score" in rec["waiting"]["awaiting"]
    f = app.state.brief_status_file
    early = brief.brief_status(f, DUBAI, _z(9, 59, 59), full=False)
    assert early["overdue"] is False and early["due"] == "10:00"
    assert early["check"] == "early: no complete brief yet; due 10:00 in the reporting zone"
    late = brief.brief_status(f, DUBAI, _z(10, 0), full=False)
    assert late["overdue"] is True and late["check"] == "late: no complete brief for today"


def test_health_and_the_check_route_say_late_only_from_1000(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    with TestClient(main.create_app(_settings(tmp_path, {"first_check_seconds": 3600}))) as c:
        monkeypatch.setattr(brief, "_utcnow", lambda: _z(9, 59).astimezone(timezone.utc))
        assert c.get("/api/health").json()["brief"]["overdue"] is False
        assert c.get("/api/brief-check").text.startswith("early: ")
        monkeypatch.setattr(brief, "_utcnow", lambda: _z(10, 0).astimezone(timezone.utc))
        assert c.get("/api/health").json()["brief"]["overdue"] is True
        assert c.get("/api/brief-check").text.startswith("late: ")
        assert c.get("/api/freshness", headers=H).json()["brief"]["due"] == "10:00"


# ---- T4: /api/today passes through the same gate

def _client(tmp_path, monkeypatch, brief_cfg=None):
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    monkeypatch.setattr(rc, "reporting_today", lambda zone, now=None: DAY)
    c = TestClient(main.create_app(_settings(tmp_path, {"first_check_seconds": 3600, **(brief_cfg or {})})))
    c.__enter__()
    c.app.state.lm = _NoModel()
    return c


def _today(c, monkeypatch, at):
    _pin(monkeypatch, at)
    r = c.get("/api/today", headers=H)
    assert r.status_code == 200
    return r.json()


def test_today_records_nothing_before_0730_then_once_the_night_has_settled(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    try:
        app = c.app
        _pull(app, _z(6, 0), PROVISIONAL)
        _pull(app, _z(6, 45), PROVISIONAL)
        body = _today(c, monkeypatch, _z(7, 0))
        assert body["signals"] and body["brief_status"]["today"] is False        # Today still shows the brief
        assert _status(app) is None and _glance(app) is None
        _today(c, monkeypatch, _z(7, 29, 59))
        assert _status(app) is None and _glance(app) is None
        _pull(app, _z(7, 25), PROVISIONAL)
        body = _today(c, monkeypatch, _z(7, 30))
        assert body["brief_status"]["today"] is True
        assert _status(app)["complete"]["first_published_at"] == "2026-10-24T07:30:00+04:00"
        assert _texts(_glance(app)) == PROVISIONAL_TEXTS
        # Once today's complete brief is recorded, Today records again whatever the night does.
        _pull(app, _z(7, 40), UPDATED)                # changed: not settled now
        _today(c, monkeypatch, _z(7, 45))
        assert _status(app)["complete"]["published_at"] == "2026-10-24T07:45:00+04:00"
        assert _texts(_glance(app)) == UPDATED_TEXTS
    finally:
        c.__exit__(None, None, None)


def test_today_from_0930_records_whatever_is_there(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    try:
        _today(c, monkeypatch, _z(9, 29))
        assert _status(c.app) is None
        _today(c, monkeypatch, _z(9, 30))
        assert set(_status(c.app)) == {"waiting"}
    finally:
        c.__exit__(None, None, None)


def test_with_unusable_brief_settings_today_keeps_the_default_floor(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, {"not_before": "late"})
    try:
        _pull(c.app, _z(5, 0), PROVISIONAL)
        _pull(c.app, _z(6, 0), PROVISIONAL)
        _today(c, monkeypatch, _z(6, 10))
        assert _status(c.app) is None
        assert c.app.state.morning_step.startswith("off: ")
    finally:
        c.__exit__(None, None, None)


# ---- T7: the wake loop pulls until noon

def test_the_wake_plan_pulls_inside_the_window_whether_or_not_the_night_has_landed():
    window, poll = whoop.parse_wake_window("05:00-12:00"), 15 * 60
    assert whoop.wake_plan(_z(6, 0), window, poll) == ("pull", poll)
    assert whoop.wake_plan(_z(11, 59), window, poll) == ("pull", poll)
    assert whoop.wake_plan(_z(12, 0), window, poll) == ("wait", poll)
    assert whoop.wake_plan(_z(4, 50), window, poll) == ("wait", 600)
    assert whoop.wake_plan(_z(6, 0), window, 0) == ("wait", poll)
    assert Settings().whoop["wake_window"] == "05:00-12:00"


def test_the_wake_loop_keeps_pulling_after_the_night_has_landed(tmp_path, monkeypatch):
    calls, delays = [], []
    app = _app(tmp_path)
    _pull(app, _z(5, 0), PROVISIONAL)                 # landed

    def fake_pull(conn, client, policy, days=8, now=None):
        calls.append(days)
        return {"recovery": 0, "sleep": 0, "cycle": 0, "samples": 0, "dates": []}

    async def fake_sleep(s):
        delays.append(s)
        raise asyncio.CancelledError

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _z(8, 0).astimezone(tz) if tz else _z(8, 0)

    monkeypatch.setattr(main, "whoop_pull", fake_pull)
    monkeypatch.setattr(main, "SLEEP", fake_sleep)
    monkeypatch.setattr(main, "datetime", _Clock)
    app.state.settings.raw["whoop"] = {"enabled": True, "client_id": "x", "wake_poll_minutes": 15}
    app.state.whoop = object()
    app.state.whoop_pull_last_at, app.state.whoop_last_pull = None, None

    async def scenario():
        app.state.whoop_pull_lock = asyncio.Lock()
        with pytest.raises(asyncio.CancelledError):
            await main._whoop_wake_loop(app)
    asyncio.run(scenario())
    assert calls == [3] and delays == [15 * 60]


# ---- T8: the settings

def test_the_morning_settings_defaults():
    ms = main.morning_settings(Settings(raw={}))
    assert ms == ((300, 720), 450, 570, 300, 150.0, 30.0)
    assert (ms.not_before, ms.publish_by, ms.settle_minutes) == (450, 570, 30.0)
    assert main.morning_settings(Settings(raw={"brief": {"not_before": time(7, 0), "publish_by": "09:00",
                                                         "settle_minutes": 0}}))[1:3] == (420, 540)


@pytest.mark.parametrize("cfg, words", [
    ({"not_before": "04:30"}, "not_before must fall inside morning_window"),
    ({"not_before": "12:00"}, "not_before must fall inside morning_window"),
    ({"not_before": "08:00", "publish_by": "07:45"}, "publish_by must not come before not_before"),
    ({"publish_by": "10:00"}, "publish_by must come before the 10:00 due time"),
    ({"morning_window": "05:00-10:00"}, "morning_window must end after 10:00"),
    ({"settle_minutes": "half an hour"}, "settle_minutes"),
    ({"settle_minutes": -1}, "settle_minutes"),
    ({"settle_minutes": 181}, "settle_minutes"),
    ({"settle_minutes": math.inf}, "settle_minutes"),
    ({"not_before": "soon"}, "expected HH:MM"),
])
def test_bad_brief_settings_are_refused_with_the_reason(cfg, words):
    with pytest.raises(ValueError) as e:
        main.morning_settings(Settings(raw={"brief": cfg}))
    assert words in str(e.value)


# ---- T9: the phone learns the times

def test_the_glance_route_carries_the_due_time_and_the_floor(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    try:
        body = c.get("/api/glance", headers=H).json()
        assert body["due"] == "10:00" and body["not_before"] == "07:30"
    finally:
        c.__exit__(None, None, None)
    (tmp_path / "eight").mkdir()
    c = _client(tmp_path / "eight", monkeypatch, {"not_before": "08:00"})
    try:
        assert c.get("/api/glance", headers=H).json()["not_before"] == "08:00"
    finally:
        c.__exit__(None, None, None)


# ---- review fixes (2026-10-27)

class _ModelLM:
    """An available local model whose narrative passes the validator."""
    primary = fallback = "model-x"

    def available(self):
        return True

    def structured(self, messages, schema, temperature=0.2, model=None):
        return {"narrative": "A steady day with nothing that needs attention.", "actions": [], "flags": []}


def test_today_s_background_model_rewrite_records_nothing_before_0730(tmp_path, monkeypatch):
    """Today opened at 07:00 with the local model up: the fast path records
    nothing, and neither does the background rewrite that finishes later (it
    carries the gate's answer from the request). The real reporting day, the
    gate's clock pinned at 07:00 on it."""
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    monkeypatch.setattr(main, "_now_local",
                        lambda zone: datetime.now(zone).replace(hour=7, minute=0, second=0, microsecond=0))
    c = TestClient(main.create_app(_settings(tmp_path, {"first_check_seconds": 3600})))
    c.__enter__()
    try:
        from heliosd.signals.markers import owner_device
        c.app.state.lm = _ModelLM()
        conn, policy = c.app.state.conn, c.app.state.policy
        day = rc.reporting_today(policy.zone)
        with db.transaction(conn) as tx:
            for metric, state, value in (("recovery_score", "favorable", 71.5), ("sleep_duration", "flag", 5.25)):
                tx.execute("INSERT INTO signals (date, metric, state, value, unit, device_key, context_flags, why) "
                           "VALUES (?, ?, ?, ?, 'u', ?, '[]', 'synthetic')",
                           [day, metric, state, value, owner_device(policy, metric)])
        assert c.get("/api/today", headers=H).json()["narrative_status"] == "generating"
        deadline, status = clock.monotonic() + 10, None
        while clock.monotonic() < deadline:
            status = c.get("/api/today", headers=H).json()["narrative_status"]
            if status == "ready":
                break
            clock.sleep(0.05)
        assert status == "ready"                      # the model's narrative is in the cache
        assert _status(c.app) is None and _glance(c.app) is None
    finally:
        c.__exit__(None, None, None)


def test_a_first_tick_above_the_check_limit_never_breaks_today_or_the_glance(tmp_path, monkeypatch):
    """first_tick_seconds 3590 makes the default first check 3620 s, which the
    morning step refuses (it says so); the gate falls back to plain defaults,
    so /api/today and /api/glance still answer, with the 07:30 floor."""
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    monkeypatch.setattr(rc, "reporting_today", lambda zone, now=None: DAY)
    raw = {"server": {"ingest_token": TOKEN, "first_tick_seconds": 3590}, "owner": {"timezone": "Asia/Dubai"},
           "storage": {"db_path": str(tmp_path / "helios.duckdb")}, "notifications": {"macos_alerts": False}}
    c = TestClient(main.create_app(Settings(raw=raw)))
    c.__enter__()
    try:
        c.app.state.lm = _NoModel()
        _pull(c.app, _z(5, 0), PROVISIONAL)
        _pull(c.app, _z(6, 0), PROVISIONAL)
        g = c.get("/api/glance", headers=H)
        assert g.status_code == 200 and g.json()["not_before"] == "07:30"
        _today(c, monkeypatch, _z(6, 10))
        assert _status(c.app) is None
        assert c.app.state.morning_step.startswith("off: first_check_seconds")
    finally:
        c.__exit__(None, None, None)


def test_a_window_to_midnight_sends_a_time_the_phone_can_read(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, {"morning_window": "05:00-24:00"})
    try:
        assert c.get("/api/glance", headers=H).json()["revise_until"] == "23:59"
    finally:
        c.__exit__(None, None, None)
