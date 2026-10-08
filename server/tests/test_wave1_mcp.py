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


# ---------------------------------------------------------------- A10 (P9)

def test_query_metric_and_compare_reject_an_unknown_metric_in_every_shape():
    from heliosd.narrative.chat import _tool_compare, _tool_query_metric
    conn, policy = _store()
    out = _tool_query_metric(conn, "resting_hr_sleep", days=14, stat="summary", zone=policy.zone)
    assert "unknown metric" in out["error"] and "series" not in out and "summary" not in out
    assert "unknown metric" in _tool_query_metric(conn, "nope", zone=policy.zone)["error"]
    assert "unknown metric" in _tool_compare(conn, "nope", zone=policy.zone)["error"]
    assert "stat" in _tool_query_metric(conn, "steps", stat="average", zone=policy.zone)["error"]


def test_signals_tool_reports_a_bad_date_instead_of_a_500(dubai_client):
    r = dubai_client.get("/api/tool/signals?day=2026-13-01", headers=H)
    assert r.status_code == 200, r.text
    assert "bad date" in r.json()["error"]


def test_mcp_get_passes_the_daemons_reason_through_instead_of_cannot_reach(monkeypatch):
    import httpx
    from heliosd.mcp_server import server as mcp_server

    class R:
        def __init__(self, code, body):
            self.status_code, self._body = code, body
            self.text = json.dumps(body) if isinstance(body, dict) else body

        def json(self):
            if isinstance(self._body, dict):
                return self._body
            raise ValueError("not json")

    class C:
        def __init__(self, resp):
            self.resp = resp

        def get(self, path, params=None):
            if isinstance(self.resp, Exception):
                raise self.resp
            return self.resp

    monkeypatch.setattr(mcp_server, "_client", C(R(500, {"detail": "ParserException: syntax error at or near \"days\""})))
    out = json.loads(mcp_server._get("/api/tool/sql"))
    assert "cannot reach" not in out["error"] and "500" in out["error"] and "syntax error" in out["error"]
    monkeypatch.setattr(mcp_server, "_client", C(R(400, {"detail": "bad date '2026-13-01'"})))
    assert "bad date" in json.loads(mcp_server._get("/api/tool/signals"))["error"]
    monkeypatch.setattr(mcp_server, "_client", C(R(503, "Service Unavailable")))
    assert "503" in json.loads(mcp_server._get("/api/tool/signals"))["error"]
    monkeypatch.setattr(mcp_server, "_client", C(httpx.ConnectError("connection refused")))
    assert "cannot reach" in json.loads(mcp_server._get("/api/tool/signals"))["error"]
    monkeypatch.setattr(mcp_server, "_client", C(R(401, {"detail": "bad token"})))
    assert "ingest_token" in json.loads(mcp_server._get("/api/tool/signals"))["error"]


def test_sql_tool_returns_the_duckdb_message_as_400_and_timestamptz_columns_work(dubai_client):
    r = dubai_client.post("/api/tool/sql", json={"query": "SELECT 1 AS x FROM no_such_table"}, headers=H)
    assert r.status_code == 400, r.text
    assert "no_such_table" in r.json()["detail"]
    r = dubai_client.post("/api/tool/sql", json={"query": "SELECT COUNT(*) days FROM samples"}, headers=H)
    assert r.status_code == 400 and "syntax error" in r.json()["detail"]
    r = dubai_client.post("/api/tool/sql", json={"query": "SELECT TIMESTAMPTZ '2026-10-08 03:00:00+00' AS t"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()[0]["t"].startswith("2026-10-08")


def test_pytz_is_declared_and_pinned():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert re.search(r'^\s*"pytz', (root / "pyproject.toml").read_text(), re.M)
    assert re.search(r"^pytz==", (root / "constraints.txt").read_text(), re.M)


# ---------------------------------------------------------------- A11 (P10)

def _events(conn, rows):
    for i, (kind, ts, payload) in enumerate(rows):
        db.execute(conn, "INSERT INTO events (event_id, kind, ts, payload, source) VALUES (?, ?, ?, ?, ?)",
                   [f"e{i}", kind, ts, json.dumps(payload), "zest" if kind == "system" else "user"])


def test_list_events_keeps_system_events_out_of_the_owners_log_and_offers_them_separately():
    from heliosd.narrative.chat import _tool_events
    conn, policy = _store()
    now = datetime(2026, 10, 8, 7, 0, tzinfo=DUBAI)
    rows = [("system", datetime(2026, 10, 7, 9, 0) + timedelta(minutes=i), {"event": "thermal_state"}) for i in range(60)]
    rows += [("med", datetime(2026, 9, 18, 8, 0), {"item": "vitamin d"}),
             ("note", datetime(2026, 9, 18, 9, 0), {"text": "slept badly"}),
             ("caffeine", datetime(2026, 10, 7, 6, 30), {"item": "coffee"})]
    _events(conn, rows)
    out = _tool_events(conn, "all", 30, zone=policy.zone, now=now)
    kinds = [e["kind"] for e in out["events"]]
    assert "system" not in kinds and kinds == ["caffeine", "note", "med"]
    quick = _tool_events(conn, "quicklog", 30, zone=policy.zone, now=now)
    assert [e["kind"] for e in quick["events"]] == ["caffeine", "note", "med"] and "labs" not in quick
    system = _tool_events(conn, "system", 30, zone=policy.zone, now=now)
    assert len(system["events"]) == 50 and {e["kind"] for e in system["events"]} == {"system"}
    assert system["events_truncated"] is True
    assert "error" in _tool_events(conn, "everything", 30, zone=policy.zone, now=now)


def test_list_events_owner_limit_is_high_enough_for_a_month_of_logging():
    from heliosd.narrative.chat import _tool_events
    conn, policy = _store()
    now = datetime(2026, 10, 8, 7, 0, tzinfo=DUBAI)
    _events(conn, [("caffeine", datetime(2026, 9, 10, 6, 0) + timedelta(hours=3 * i), {"item": "coffee"}) for i in range(120)])
    out = _tool_events(conn, "quicklog", 30, zone=policy.zone, now=now)
    assert len(out["events"]) == 120 and out["events_truncated"] is False


# ---------------------------------------------------------------- A12 (P11, P7)

def _whoop_records(conn, rows):
    """(record_key, kind, nap, score_state, start_utc, end_utc, created_at, fetched_at); naive UTC like the store."""
    for key, kind, nap, state, s, e, c, f in rows:
        db.execute(conn, "INSERT INTO whoop_records (record_key, kind, native_id, nap, score_state, start_utc, end_utc, "
                         "created_at, fetched_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')",
                   [key, kind, key.split(":")[1], nap, state, s, e, c, f])


LAST_NIGHT = [("recovery:1", "recovery", None, "SCORED", "2026-10-06 23:01", None, "2026-10-06 23:01", "2026-10-08 02:30"),
              ("sleep:s1", "sleep", False, "SCORED", "2026-10-06 17:09", "2026-10-07 00:15", "2026-10-06 23:01", "2026-10-08 02:30")]
THIS_NIGHT = [("recovery:2", "recovery", None, "SCORED", "2026-10-08 01:39", None, "2026-10-08 01:39", "2026-10-08 06:00"),
              ("sleep:s2", "sleep", False, "SCORED", "2026-10-07 18:08", "2026-10-08 01:18", "2026-10-08 01:39", "2026-10-08 06:00")]


def test_whoop_cloud_row_waits_quietly_inside_the_morning_window_and_shows_the_last_pull():
    from heliosd.signals import watchdog
    conn, policy = _store()
    _whoop_records(conn, LAST_NIGHT)
    now = datetime(2026, 10, 8, 7, 10)            # reporting-zone wall clock, as every watchdog `now`
    row = watchdog.whoop_cloud_status(conn, now, enabled=True, last_error=None, zone=policy.zone)
    assert row["status"] == "waiting" and row["notify"] is False and row["tier"] == "informational"
    assert row["last_pull_at"] == "2026-10-08T06:30:00+04:00"
    assert sorted(row["missing_today"]) == ["recovery", "sleep"] and "polling" in row["fix"]


def test_whoop_cloud_row_alarms_after_scoring_time_when_todays_night_is_still_missing():
    from heliosd.signals import watchdog
    conn, policy = _store()
    _whoop_records(conn, LAST_NIGHT)
    db.execute(conn, "UPDATE whoop_records SET fetched_at = '2026-10-08 05:50'")   # pulled 09:50, 40 min ago
    now = datetime(2026, 10, 8, 10, 30)
    row = watchdog.whoop_cloud_status(conn, now, enabled=True, last_error=None, zone=policy.zone)
    assert row["status"] == "stale" and row.get("notify") is not False
    assert "2026-10-08" in row["fix"] and "not pulled" in row["fix"]
    # Both of tonight's records present and the pull recent: healthy, no row.
    _whoop_records(conn, THIS_NIGHT)
    db.execute(conn, "UPDATE whoop_records SET fetched_at = '2026-10-08 06:10'")   # 10:10 Dubai
    assert watchdog.whoop_cloud_status(conn, now, enabled=True, last_error=None, zone=policy.zone) is None
    # A PENDING_SCORE recovery does not count as pulled.
    db.execute(conn, "UPDATE whoop_records SET score_state = 'PENDING_SCORE' WHERE record_key = 'recovery:2'")
    row = watchdog.whoop_cloud_status(conn, now, enabled=True, last_error=None, zone=policy.zone)
    assert row and row["missing_today"] == ["recovery"]


def test_whoop_cloud_row_reports_a_stuck_puller_by_pull_age_without_paging():
    from heliosd.signals import watchdog
    conn, policy = _store()
    _whoop_records(conn, LAST_NIGHT + THIS_NIGHT)           # everything for today is in, pulled 10:00
    now = datetime(2026, 10, 8, 15, 0)                       # five hours later, no pull since
    row = watchdog.whoop_cloud_status(conn, now, enabled=True, last_error=None, zone=policy.zone)
    assert row["status"] == "stale" and row["notify"] is False and row["age_hours"] == 5.0
    assert "last pull" in row["fix"]
    assert watchdog.whoop_cloud_status(conn, datetime(2026, 10, 8, 11, 0), enabled=True, last_error=None,
                                       zone=policy.zone) is None
    # Never pulled at all: silent, and that one pages.
    empty, _ = _store()
    row = watchdog.whoop_cloud_status(empty, now, enabled=True, last_error=None, zone=policy.zone)
    assert row["status"] == "silent" and row.get("notify") is not False and row["last_pull_at"] is None


def test_whoop_client_keeps_its_last_error_across_a_restart_and_clears_it_on_success(tmp_path):
    from heliosd.ingest.whoop import WhoopClient
    cfg = {"token_path": str(tmp_path / "whoop" / "whoop_tokens.json"), "client_id": "x",
           "client_secret": "y", "redirect_uri": "http://localhost/cb"}
    first = WhoopClient(cfg)
    assert first.last_error is None
    first._fail("token refresh", RuntimeError("boom"))
    assert first.last_error == "token refresh failed: RuntimeError"
    restarted = WhoopClient(cfg)                               # a daemon restart builds a new client
    assert restarted.last_error == "token refresh failed: RuntimeError"
    assert restarted.last_error_at is not None
    state = json.loads((tmp_path / "whoop" / "whoop_pull_state.json").read_text())
    assert state["last_error"].startswith("token refresh failed") and "token" not in json.dumps(state).lower().replace("token refresh", "")
    restarted._clear_error()
    assert WhoopClient(cfg).last_error is None


def test_whoop_cloud_metrics_no_longer_claim_resting_hr():
    from heliosd.signals import watchdog
    assert "resting_hr" not in watchdog.WHOOP_CLOUD_METRICS
    assert "resting HR" not in watchdog.CLOUD_COVER_NOTE and "resting heart" not in watchdog.CLOUD_COVER_NOTE.lower()


def test_daily_whoop_metrics_go_stale_at_one_and_a_half_cadences():
    from heliosd.signals import watchdog
    from heliosd.trust.registry import SourceRegistry
    conn, policy = _store()
    now = datetime(2026, 10, 8, 12, 0)
    # hrv_rmssd (Whoop only): a value 1.7 cadences old is late under 1.5 x cadence, not under the old 2 x.
    hrv_age = timedelta(hours=1.7 * policy.cadence_hours("hrv_rmssd"))
    db.execute(conn, "INSERT INTO samples (sample_id, metric, value, unit, start_ts, end_ts, source_name, device_key, sync_path) "
                     "VALUES ('wh:hrv_rmssd:recovery:1', 'hrv_rmssd', 45.0, 'ms', ?, ?, 'WHOOP', 'whoop', 'whoop_api')",
               [now - hrv_age, now - hrv_age])
    # steps from the watch, also 1.7 cadences old: the ordinary 2 x rule still applies, no row.
    steps_age = timedelta(hours=1.7 * policy.cadence_hours("steps"))
    db.execute(conn, "INSERT INTO samples (sample_id, metric, value, unit, start_ts, end_ts, source_name, device_key, sync_path) "
                     "VALUES ('hk:1', 'steps', 100.0, 'count', ?, ?, 'Watch', 'apple_watch_ultra', 'bridge')",
               [now - steps_age - timedelta(minutes=10), now - steps_age])
    report = watchdog.check(conn, policy, now=now, registry=SourceRegistry())
    by_metric = {r["metric"]: r for r in report}
    assert by_metric["hrv_rmssd"]["status"] == "stale"
    assert "steps" not in by_metric


# ---------------------------------------------------------------- A17 server half (P15)

def test_today_focus_steps_is_null_not_zero_when_no_steps_row_exists(dubai_client):
    r = dubai_client.get("/api/today", headers=H)
    assert r.status_code == 200, r.text
    focus = r.json()["focus"][0]
    assert focus["name"] == "Step foundation" and focus["current"] is None and focus["target"] == 8000


# ---------------------------------------------------------------- A20 server half (M15, M18)

def test_metric_route_returns_the_latest_baseline_per_window_with_its_date(dubai_client):
    from heliosd.ingest.normalize import reporting_today
    conn = dubai_client.app.state.conn
    today = reporting_today(DUBAI)
    for d_off in (3, 2, 1):
        for w in (30, 60, 90):
            db.execute(conn, "INSERT INTO baselines (date, metric, window_days, median, mad, n_days) VALUES (?, 'bmi', ?, ?, 0.4, ?)",
                       [today - timedelta(days=d_off), w, 30.0 + w / 100 + d_off, w])
    db.execute(conn, "INSERT INTO baselines (date, metric, window_days, median, mad, n_days) VALUES (?, 'bmi', 30, 31.3, 0.4, 30)",
               [today])
    base = dubai_client.get("/api/metrics/bmi?days=7", headers=H).json()["baselines"]
    assert [b["window_days"] for b in base] == [30, 60, 90]
    assert base[0] == {"window_days": 30, "median": 31.3, "mad": 0.4, "n_days": 30, "date": str(today), "current": True}
    assert base[1]["date"] == str(today - timedelta(days=1)) and base[1]["current"] is False and base[1]["median"] == 31.6
    assert base[2]["date"] == str(today - timedelta(days=1)) and base[2]["median"] == 31.9


def test_activity_route_keeps_the_newest_vo2max_whatever_the_window(dubai_client):
    from heliosd.ingest.normalize import reporting_today
    conn = dubai_client.app.state.conn
    today = reporting_today(DUBAI)
    _daily(conn, "vo2max", today - timedelta(days=100), [31.5], unit="mL/min/kg")
    _daily(conn, "steps", today - timedelta(days=1), [4000])
    out = dubai_client.get("/api/activity?days=30", headers=H).json()
    assert [r["date"] for r in out["vo2max"]] == [str(today - timedelta(days=100))]
    assert out["vo2max"][0]["value"] == 31.5
    assert [r["date"] for r in out["steps"]] == [str(today - timedelta(days=1))]
    # Steps older than the window stay out: only the VO2 Max tile keeps its last reading.
    _daily(conn, "steps", today - timedelta(days=100), [999])
    out = dubai_client.get("/api/activity?days=30", headers=H).json()
    assert [r["date"] for r in out["steps"]] == [str(today - timedelta(days=1))]
