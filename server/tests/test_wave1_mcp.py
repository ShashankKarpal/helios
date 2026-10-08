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


# ---------------------------------------------------------------- A8 (P4, P5)

def _daily(conn, metric, first: date, values, device="apple_watch_ultra", unit="count"):
    for i, v in enumerate(values):
        db.execute(conn, "INSERT INTO daily_values (date, metric, value, unit, device_key, n_samples, confidence, grade) "
                         "VALUES (?, ?, ?, ?, ?, 1, 0.9, 'A')", [first + timedelta(days=i), metric, v, unit, device])


def test_query_metric_returns_exactly_n_complete_days_ending_yesterday_in_the_reporting_zone():
    from heliosd.narrative.chat import _tool_query_metric
    conn, policy = _store()
    # 22:30 UTC on Oct 7 is 02:30 on Oct 8 in Dubai: the reporting today is Oct 8 whatever the Mac says.
    now = datetime(2026, 10, 7, 22, 30, tzinfo=timezone.utc)
    _daily(conn, "steps", date(2026, 9, 29), [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 50])  # ends Oct 8 (partial 50)
    out = _tool_query_metric(conn, "steps", days=7, stat="series", zone=policy.zone, now=now)
    assert out["reporting_date"] == "2026-10-08"
    assert out["window"] == {"start": "2026-10-01", "end": "2026-10-07", "days": 7}
    assert [r["date"] for r in out["series"]] == [str(date(2026, 10, 1) + timedelta(days=i)) for i in range(7)]
    assert "partial_today" not in out
    with_today = _tool_query_metric(conn, "steps", days=7, stat="series", zone=policy.zone, now=now, include_today=True)
    assert with_today["partial_today"]["date"] == "2026-10-08" and with_today["partial_today"]["partial"] is True
    assert with_today["partial_today"]["row"]["value"] == 50
    assert len(with_today["series"]) == 7


def test_query_metric_summary_counts_complete_days_reports_devices_and_a_true_median():
    from heliosd.narrative.chat import _tool_query_metric
    conn, policy = _store()
    now = datetime(2026, 10, 8, 7, 0, tzinfo=DUBAI)
    _daily(conn, "resting_hr", date(2026, 10, 1), [60, 62, 64, 66], device="apple_watch_ultra", unit="count/min")
    _daily(conn, "resting_hr", date(2026, 10, 5), [70, 70, 70], device="apple_watch_ultra", unit="count/min")
    _daily(conn, "resting_hr", date(2026, 10, 8), [99], device="whoop", unit="count/min")   # today, partial, another device
    out = _tool_query_metric(conn, "resting_hr", days=30, stat="summary", zone=policy.zone, now=now)
    s = out["summary"]
    assert s["n"] == 7 and s["min"] == 60 and s["max"] == 70
    assert s["median"] == 66          # sorted 60 62 64 66 70 70 70, odd n
    assert s["devices"] == {"apple_watch_ultra": 7}
    assert s["latest"]["date"] == "2026-10-07"
    assert "series" not in out
    even = _tool_query_metric(conn, "resting_hr", days=6, stat="summary", zone=policy.zone, now=now)["summary"]
    assert even["n"] == 6 and even["median"] == 68   # 62 64 66 70 70 70 -> mean of the two middle values, not the upper 70


def test_compare_periods_uses_two_equal_windows_that_end_on_the_last_complete_day():
    from heliosd.narrative.chat import _tool_compare
    conn, policy = _store()
    now = datetime(2026, 10, 8, 7, 0, tzinfo=DUBAI)
    _daily(conn, "steps", date(2026, 9, 24), [1000] * 7 + [2000] * 7 + [50])   # Sep 24..30, Oct 1..7, Oct 8 partial
    out = _tool_compare(conn, "steps", 7, 7, zone=policy.zone, now=now)
    assert out["recent"] == {"start": "2026-10-01", "end": "2026-10-07", "median": 2000, "n": 7}
    assert out["previous"] == {"start": "2026-09-24", "end": "2026-09-30", "median": 1000, "n": 7}
    assert out["recent_days"] == 7 and out["previous_days"] == 7 and out["change_pct"] == 100.0
    assert out["reporting_date"] == "2026-10-08"


# ---------------------------------------------------------------- A9 (P8), server half

def test_signals_default_day_and_events_window_follow_the_reporting_zone_not_the_mac_clock():
    from heliosd.narrative.chat import _tool_signals
    conn, policy = _store()
    now = datetime(2026, 10, 7, 22, 30, tzinfo=timezone.utc)     # 02:30 Oct 8 in Dubai
    assert _tool_signals(conn, None, zone=policy.zone, now=now)["date"] == "2026-10-08"
    assert _tool_signals(conn, None, zone=timezone.utc, now=now)["date"] == "2026-10-07"
    out = _tool_signals(conn, "2026-10-08", zone=policy.zone, now=now)
    assert out["reporting_date"] == "2026-10-08" and out["partial_day"] is True
    assert _tool_signals(conn, "2026-10-07", zone=policy.zone, now=now)["partial_day"] is False


class _StubLM:
    def __init__(self):
        self.seen = []

    def available(self):
        return True

    def chat(self, messages, temperature=0.0, tools=None):
        self.seen.append(messages)
        return {"content": "No data was needed."}

    def structured(self, messages, schema, temperature=0.0):
        return {"answer": "No data was needed.", "citations": [], "caveats": []}


def test_chat_system_prompt_dates_today_in_the_reporting_zone():
    from heliosd.ingest.normalize import reporting_today
    from heliosd.narrative.chat import run_chat
    conn, policy = _store()
    lm = _StubLM()
    run_chat(conn, lm, "hello", policy=policy)
    system = lm.seen[0][0]["content"]
    assert f"Today is {reporting_today(policy.zone)}." in system


@pytest.fixture()
def dubai_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from heliosd.config import Settings
    from heliosd.main import create_app
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><html><head></head><body></body></html>", encoding="utf-8")
    monkeypatch.setenv("HELIOS_WEB_DIST", str(dist))
    raw = {"server": {"ingest_token": "test-token-0123456789"},
           "storage": {"db_path": str(tmp_path / "helios.duckdb")},
           "owner": {"timezone": "Asia/Dubai"},
           "notifications": {"macos_alerts": False}}
    with TestClient(create_app(Settings(raw=raw))) as c:
        yield c


H = {"X-Helios-Token": "test-token-0123456789"}


def test_metric_activity_and_actions_routes_carry_the_reporting_date(dubai_client):
    from heliosd.ingest.normalize import reporting_today
    today = str(reporting_today(DUBAI))
    assert dubai_client.get("/api/metrics/steps?days=7", headers=H).json()["reporting_date"] == today
    assert dubai_client.get("/api/activity?days=7", headers=H).json()["reporting_date"] == today
    assert dubai_client.get("/api/actions?days=7", headers=H).json()["reporting_date"] == today


def test_labs_confirm_defaults_the_panel_date_to_the_reporting_day(dubai_client):
    from heliosd.ingest.normalize import reporting_today
    r = dubai_client.post("/api/labs/confirm", json={"rows": [{"biomarker": "ferritin", "value": 80, "unit": "ng/mL"}]},
                          headers=H)
    assert r.status_code == 200, r.text
    labs = dubai_client.get("/api/labs", headers=H).json()["labs"]
    assert labs and labs[0]["panel_date"] == str(reporting_today(DUBAI))
