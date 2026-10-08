"""Wave 1 (fix program 2026-10-08), fork M: the MCP tools, the tool and metric
routes, the Whoop puller's persisted state and the freshness watchdog.
Audit items P1, P4, P5, P7, P8, P9, P10, P11, P15, M15, M18, S11. Every test
here failed on the code before its fix (the failure lines are recorded in the
session's verify.md). Synthetic numbers only."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from heliosd.store import db
from heliosd.trust.policy import MetricPolicy

DUBAI = ZoneInfo("Asia/Dubai")


def _store():
    conn = db.connect_memory()
    policy = MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    return conn, policy


def _cache(conn, rows):
    for d, kind, payload in rows:
        db.execute(conn, "INSERT INTO whoop_cache (date, kind, payload) VALUES (?, ?, ?)",
                   [d, kind, json.dumps(payload)])


# ---------------------------------------------------------------- A7 (P1, P7, S11)

def test_whoop_live_returns_the_newest_record_per_kind_with_stale_flags_and_resting_hr_sleep():
    from heliosd.narrative.chat import _tool_whoop_live
    conn, policy = _store()
    now = datetime(2026, 10, 8, 7, 10, tzinfo=DUBAI)
    _cache(conn, [
        ("2026-10-07", "recovery", {"cycle_id": 1, "score_state": "SCORED",
                                    "score": {"recovery_score": 61, "hrv_rmssd_milli": 44.0, "resting_heart_rate": 58}}),
        ("2026-10-08", "recovery", {"cycle_id": 2, "score_state": "SCORED",
                                    "score": {"recovery_score": 72, "hrv_rmssd_milli": 51.0, "resting_heart_rate": 55}}),
        ("2026-10-07", "sleep", {"id": "s1", "start": "2026-10-06T18:00:00.000Z", "end": "2026-10-07T01:00:00.000Z",
                                 "score_state": "SCORED", "score": {"sleep_efficiency_percentage": 90.0}}),
        ("2026-10-08", "sleep", {"id": "s2", "start": "2026-10-07T18:30:00.000Z", "end": "2026-10-08T01:30:00.000Z",
                                 "score_state": "SCORED", "score": {"sleep_efficiency_percentage": 93.0}}),
        # The open cycle started two evenings back: outside any today-1 window, still the live strain.
        ("2026-10-06", "cycle", {"id": 10, "start": "2026-10-06T17:00:00.000Z", "end": None,
                                 "score_state": "SCORED", "score": {"strain": 4.4}}),
        ("2026-10-08", "sleep_nap", {"id": "n1", "nap": True, "start": "2026-10-08T09:00:00.000Z",
                                     "end": "2026-10-08T09:40:00.000Z", "score_state": "SCORED", "score": {}}),
    ])
    db.execute(conn, "INSERT INTO whoop_records (record_key, kind, native_id, fetched_at, payload) "
                     "VALUES ('recovery:2', 'recovery', '2', '2026-10-08 03:02:27', '{}')")
    out = _tool_whoop_live(conn, zone=policy.zone, now=now)
    assert out["reporting_date"] == "2026-10-08"
    assert out["recovery"]["date"] == "2026-10-08" and out["recovery"]["recovery_score"] == 72
    assert out["recovery"]["stale"] is False and "live" not in out["recovery"]
    assert "resting_hr" not in out["recovery"] and out["recovery"]["resting_hr_sleep"] == 55
    assert out["recovery"]["fetched_at"] == "2026-10-08T07:02:27+04:00"
    assert out["sleep"]["date"] == "2026-10-08" and out["sleep"]["stale"] is False
    assert out["strain"]["strain"] == 4.4 and out["strain"]["in_progress"] is True and out["strain"]["stale"] is False
    assert out["nap"]["date"] == "2026-10-08"
    assert out["last_pull_at"] == "2026-10-08T07:02:27+04:00"


def test_whoop_live_marks_yesterdays_record_stale_and_says_todays_is_not_pulled():
    from heliosd.narrative.chat import _tool_whoop_live
    conn, policy = _store()
    now = datetime(2026, 10, 8, 7, 10, tzinfo=DUBAI)
    _cache(conn, [
        ("2026-10-07", "recovery", {"cycle_id": 1, "score_state": "SCORED", "score": {"recovery_score": 61}}),
        ("2026-10-07", "sleep", {"id": "s1", "start": "2026-10-06T18:00:00.000Z", "end": "2026-10-07T01:00:00.000Z",
                                 "score_state": "SCORED", "score": {}}),
        # A closed cycle whose end is yesterday evening: the open cycle has not been pulled yet.
        ("2026-10-06", "cycle", {"id": 10, "start": "2026-10-06T17:00:00.000Z", "end": "2026-10-07T18:00:00.000Z",
                                 "score_state": "SCORED", "score": {"strain": 9.1}}),
    ])
    out = _tool_whoop_live(conn, zone=policy.zone, now=now)
    assert out["recovery"]["date"] == "2026-10-07" and out["recovery"]["stale"] is True
    assert "2026-10-08" in out["recovery"]["note"] and "not pulled" in out["recovery"]["note"]
    assert out["sleep"]["stale"] is True
    assert out["strain"]["in_progress"] is False and out["strain"]["stale"] is True
    assert "nap" not in out
    assert out["last_pull_at"] is None


def test_whoop_live_with_an_empty_cache_says_so_instead_of_guessing():
    from heliosd.narrative.chat import _tool_whoop_live
    conn, policy = _store()
    out = _tool_whoop_live(conn, zone=policy.zone, now=datetime(2026, 10, 8, 7, 10, tzinfo=DUBAI))
    assert "note" in out and out["reporting_date"] == "2026-10-08"
    assert not {"recovery", "sleep", "strain"} & set(out)
