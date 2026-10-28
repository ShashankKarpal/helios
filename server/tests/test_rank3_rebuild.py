"""Rank 3 fix (2026-10-27), item 2: when Whoop changes last night's sleep or
recovery after the morning brief was published, heliosd rebuilds the brief and
glance.json itself until noon, and the phone sees when (revised_at).

From the first complete publish until [brief] morning_window ends (12:00), the
morning step's tick (every 5 minutes, no new job) compares glance.json with the
glance today's data gives now, and rebuilds the brief (the template) when they
differ, once the recompute after the Whoop pull has run. After 12:00 the step
leaves it; opening Today rewrites it, as before. glance.json carries
revised_at: when a complete summary of the same day was rewritten with other
content. Whoop records go through the real import and recompute. Synthetic
values only."""

from __future__ import annotations

import json
from datetime import timedelta, timezone

from heliosd import main
from heliosd.narrative import brief, glance
from heliosd.signals import recompute as rc
from tests.test_rank3_settle import (DAY, PROVISIONAL, PROVISIONAL_TEXTS, UPDATED, UPDATED_TEXTS, _apply, _app,
                                     _client, _glance, _pin, _pull, _recovery, _sleep, _status, _texts, _tick,
                                     _today, _z)

ON_DAY = _z(6, 0).astimezone(timezone.utc)


def _published_at_0730(tmp_path, monkeypatch):
    """The provisional night published at 07:30:30: the owner was still asleep
    and Whoop had not changed the night since 04:11."""
    app = _app(tmp_path)
    for t in (_z(4, 57, 40), _z(6, 0), _z(6, 45), _z(7, 15), _z(7, 30)):
        _pull(app, t, PROVISIONAL)
    _tick(app, monkeypatch, _z(7, 30, 30))
    assert _status(app)["complete"]["first_published_at"] == "2026-10-24T07:30:30+04:00"
    g = _glance(app)
    assert _texts(g) == PROVISIONAL_TEXTS and g["revised_at"] is None
    return app


def _later(minutes_after_0600):
    return dict(UPDATED, updated=_z(6, 0) + timedelta(minutes=minutes_after_0600))


# ---- T2: a provisional night updated after the publish

def test_a_night_whoop_updates_after_the_publish_is_rebuilt_at_the_next_tick(tmp_path, monkeypatch, caplog):
    app = _published_at_0730(tmp_path, monkeypatch)
    _pull(app, _z(7, 45), _later(100))                # Whoop updated the night at 07:40
    with caplog.at_level("INFO", logger="heliosd"):
        _tick(app, monkeypatch, _z(7, 50))
    g = _glance(app)
    assert _texts(g) == UPDATED_TEXTS and g["complete"] is True
    assert g["revised_at"] == "2026-10-24T07:50:00+04:00" and g["made_at"] == "2026-10-24T07:50:00+04:00"
    rec = _status(app)["complete"]
    assert rec["first_published_at"] == "2026-10-24T07:30:30+04:00" and rec["published_at"] == "2026-10-24T07:50:00+04:00"
    assert any("rebuilt: today's data changed after the first publish" in r.getMessage() for r in caplog.records)
    # Nothing changed since: the next tick rewrites nothing.
    before = (tmp_path / "glance.json").stat().st_mtime_ns
    _tick(app, monkeypatch, _z(7, 55))
    assert (tmp_path / "glance.json").stat().st_mtime_ns == before
    assert _glance(app)["revised_at"] == "2026-10-24T07:50:00+04:00"


def test_the_rebuild_waits_for_the_recompute_after_the_pull(tmp_path, monkeypatch):
    app = _published_at_0730(tmp_path, monkeypatch)
    night = _later(100)
    # The pull and the recompute right after it (the daemon's _recompute_after_pull);
    # the journal entry stays until the recompute loop drains it.
    _apply(app.state.conn, app.state.policy, _z(7, 45), [_sleep(night)], [_recovery(night)])
    rc.recompute_window(app.state.conn, app.state.policy, app.state.registry, 2, now=_z(7, 45, 10))
    app.state.whoop_last_ok_at = _z(7, 45).astimezone(timezone.utc).isoformat()
    _tick(app, monkeypatch, _z(7, 50))
    assert _texts(_glance(app)) == PROVISIONAL_TEXTS
    rc.drain_journal(app.state.conn, app.state.policy, app.state.registry, today=DAY)
    _tick(app, monkeypatch, _z(7, 55))
    assert _texts(_glance(app)) == UPDATED_TEXTS and _glance(app)["revised_at"] == "2026-10-24T07:55:00+04:00"


def test_a_change_that_moves_no_shown_number_rebuilds_nothing(tmp_path, monkeypatch):
    app = _published_at_0730(tmp_path, monkeypatch)
    same_numbers = dict(PROVISIONAL, updated=_z(7, 40))   # Whoop touched the record, nothing shown moved
    _pull(app, _z(7, 45), same_numbers)
    before = (tmp_path / "glance.json").stat().st_mtime_ns
    _tick(app, monkeypatch, _z(7, 50))
    assert (tmp_path / "glance.json").stat().st_mtime_ns == before and _glance(app)["revised_at"] is None


# ---- T3: after noon

def test_after_noon_the_step_rebuilds_nothing_and_opening_today_does(tmp_path, monkeypatch):
    app = _published_at_0730(tmp_path, monkeypatch)
    _pull(app, _z(12, 10), _later(365))               # Whoop updated the night at 12:05
    _tick(app, monkeypatch, _z(12, 15))
    assert _texts(_glance(app)) == PROVISIONAL_TEXTS
    # Opening Today: the route's gate and its fast path, at 12:20.
    _pin(monkeypatch, _z(12, 20))
    assert main.brief_gate(app, DAY, _z(12, 20)) is True
    brief.generate_brief(app.state.conn, None, DAY, "Owner", 0.2, False, False, app.state.policy, DAY,
                         app.state.brief_status_file, True)
    g = _glance(app)
    assert _texts(g) == UPDATED_TEXTS and g["revised_at"] == "2026-10-24T12:20:00+04:00"


def test_opening_today_rebuilds_through_the_route_with_revised_at(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    try:
        app = c.app
        for t in (_z(6, 0), _z(6, 45), _z(7, 25)):
            _pull(app, t, PROVISIONAL)
        _today(c, monkeypatch, _z(7, 30))
        assert _glance(app)["revised_at"] is None
        _pull(app, _z(13, 0), _later(415))
        _today(c, monkeypatch, _z(13, 5))
        assert _texts(_glance(app)) == UPDATED_TEXTS and _glance(app)["revised_at"] == "2026-10-24T13:05:00+04:00"
        body = c.get("/api/glance", headers={"X-Helios-Token": "test-token-0123456789"}).json()
        assert body["summary"]["revised_at"] == "2026-10-24T13:05:00+04:00" and body["revise_until"] == "12:00"
    finally:
        c.__exit__(None, None, None)


# ---- revised_at itself

def _rows(value):
    return [{"metric": "hrv_rmssd", "state": "neutral", "value": value}]


def test_revised_at_marks_only_a_change_of_a_complete_summary_of_the_same_day(tmp_path):
    f = tmp_path / "glance.json"
    rec = lambda: json.loads(f.read_text(encoding="utf-8"))          # noqa: E731
    assert glance.record_glance(f, DAY, _rows(50.0), "v", ["recovery_score"], False, now=ON_DAY, generation=1)
    assert rec()["revised_at"] is None                                # waiting
    t1 = ON_DAY + timedelta(minutes=30)
    assert glance.record_glance(f, DAY, _rows(50.0), "v", [], True, now=t1, generation=2)
    assert rec()["revised_at"] is None                                # the first complete one is no revision
    t2 = ON_DAY + timedelta(minutes=60)
    assert glance.record_glance(f, DAY, _rows(50.0), "v", [], True, now=t2, generation=3)
    assert rec()["revised_at"] is None                                # the same content
    t3 = ON_DAY + timedelta(minutes=90)
    assert glance.record_glance(f, DAY, _rows(44.0), "v", [], True, now=t3, generation=4)
    assert rec()["revised_at"] == t3.isoformat(timespec="seconds")
    t4 = ON_DAY + timedelta(minutes=120)
    assert glance.record_glance(f, DAY, _rows(44.0), "v", [], True, now=t4, generation=5)
    assert rec()["revised_at"] == t3.isoformat(timespec="seconds")   # kept while nothing changes
    assert glance.record_glance(f, DAY, _rows(44.0), "another verdict", [], True, now=t4, generation=6)
    assert rec()["verdict"] == "another verdict"                      # rewritten, but
    assert rec()["revised_at"] == t3.isoformat(timespec="seconds")   # the verdict alone is no revision (H3)
    nxt = ON_DAY + timedelta(days=1)
    assert glance.record_glance(f, DAY + timedelta(days=1), _rows(44.0), "v", [], True, now=nxt, generation=1)
    assert rec()["revised_at"] is None                                # a new day starts afresh


def test_complete_then_waiting_then_complete_again_moves_revised_at_each_time(tmp_path):
    """Verification N1: Whoop takes the recovery back (waiting), then scores it
    again. Each step after the first complete summary is a new revision, so the
    phone, which withdrew the notification at the first one, posts the
    restored numbers at the second."""
    f = tmp_path / "glance.json"
    rec = lambda: json.loads(f.read_text(encoding="utf-8"))          # noqa: E731
    assert glance.record_glance(f, DAY, _rows(50.0), "v", [], True, now=ON_DAY, generation=1)
    assert rec()["revised_at"] is None
    taken_back = ON_DAY + timedelta(minutes=50)
    assert glance.record_glance(f, DAY, _rows(50.0), "v", ["recovery_score"], False, now=taken_back, generation=2)
    assert rec()["revised_at"] == taken_back.isoformat(timespec="seconds")
    scored_again = ON_DAY + timedelta(minutes=100)
    assert glance.record_glance(f, DAY, _rows(52.0), "v", [], True, now=scored_again, generation=3)
    assert rec()["revised_at"] == scored_again.isoformat(timespec="seconds")
