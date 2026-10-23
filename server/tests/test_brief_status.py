"""Rank 2: the last valid brief, its age and whether it is overdue.

The narratives table is a cache that every recompute empties for its dates, so
a published brief is also recorded in a small content-free file beside the
store; /api/health (a small block), /api/freshness, the MCP tool's
/api/tool/freshness and /api/today read it. Complete (valid): today's brief
with last night's recovery in and at least one settled signal, from the local
model or the template. A brief published while the recovery is awaited is
recorded apart as waiting and never counts as delivered. Synthetic values only."""

from __future__ import annotations

import json
import os
import stat
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from heliosd.config import Settings
from heliosd.main import create_app
from heliosd.narrative import brief
from heliosd.signals import recompute as rc
from heliosd.store import db

DAY = date(2026, 10, 8)
DUBAI = ZoneInfo("Asia/Dubai")
TOKEN = "test-token-0123456789"
H = {"X-Helios-Token": TOKEN}
PART_KEYS = {"date", "first_published_at", "published_at", "kind", "model", "signals", "awaiting"}
ON_DAY = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)      # an instant on DAY in UTC (no policy)


@pytest.fixture
def on_day(monkeypatch):
    """generate_brief without a policy reads DAY in UTC; publish on that day."""
    monkeypatch.setattr(brief, "_utcnow", lambda: ON_DAY)


def _signals(conn, day, rows):
    with db.transaction(conn) as c:
        c.execute("DELETE FROM signals WHERE date = ?", [day])
        for metric, state, value in rows:
            c.execute("INSERT INTO signals (date, metric, state, value, unit, context_flags, why) "
                      "VALUES (?, ?, ?, ?, 'u', '[]', 'synthetic')", [day, metric, state, value])


def _owned(conn, day, policy, rows):
    """Signals from each metric's owner device under the real policy (a row
    from another device is a stand-in, never "arrived")."""
    from heliosd.signals.markers import owner_device
    with db.transaction(conn) as c:
        c.execute("DELETE FROM signals WHERE date = ?", [day])
        for metric, state, value in rows:
            c.execute("INSERT INTO signals (date, metric, state, value, unit, device_key, context_flags, why) "
                      "VALUES (?, ?, ?, ?, 'u', ?, '[]', 'synthetic')",
                      [day, metric, state, value, owner_device(policy, metric)])


def _rec(f):
    return json.loads(f.read_text(encoding="utf-8"))


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


COMPLETE = [("recovery_score", "favorable", 71.5), ("sleep_duration", "flag", 5.25)]


def test_a_published_complete_brief_is_recorded_as_the_last_valid_one(tmp_path, on_day):
    conn = db.connect_memory()
    _signals(conn, DAY, COMPLETE)
    f = brief.status_file_for(tmp_path / "helios.duckdb")
    assert f == tmp_path / "last_valid_brief.json"
    out = brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=f)
    assert out["narrative_status"] == "template"
    rec = _rec(f)
    assert set(rec) == {"complete"} and set(rec["complete"]) == PART_KEYS
    c = rec["complete"]
    assert c["date"] == "2026-10-08" and c["kind"] == "template" and c["model"] == "template"
    assert c["signals"] == 2 and c["awaiting"] and "recovery_score" not in c["awaiting"]
    assert c["first_published_at"] == c["published_at"] == "2026-10-08T06:00:00+00:00"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))


def test_the_record_carries_no_health_values(tmp_path, on_day):
    conn = db.connect_memory()
    _signals(conn, DAY, COMPLETE + [("hrv_rmssd", "neutral", 83.25)])
    f = tmp_path / "last_valid_brief.json"
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=f)
    text = f.read_text(encoding="utf-8")
    for value in ("71.5", "83.25", "5.25", "favorable", "flag", "synthetic"):
        assert value not in text, value


def test_a_running_total_alone_is_waiting_never_delivered(tmp_path, on_day):
    # Minutes after midnight: one step batch, last night's recovery not in.
    conn = db.connect_memory()
    _signals(conn, DAY, [("steps", "in_progress", 1234)])
    f = tmp_path / "last_valid_brief.json"
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=f)
    rec = _rec(f)
    assert set(rec) == {"waiting"} and rec["waiting"]["signals"] == 0
    assert "recovery_score" in rec["waiting"]["awaiting"]
    s = brief.brief_status(f, timezone.utc, ON_DAY + timedelta(hours=4))
    assert s["today"] is False and s["overdue"] is True and s["waiting_since"] == "2026-10-08T06:00:00+00:00"


def test_a_stand_in_device_never_makes_a_brief_complete(tmp_path, on_day):
    conn = db.connect_memory()
    with db.transaction(conn) as c:
        c.execute("INSERT INTO signals (date, metric, state, value, unit, context_flags, why) "
                  "VALUES (?, 'recovery_score', 'fallback', 60, 'u', '[]', 'synthetic')", [DAY])
    f = tmp_path / "last_valid_brief.json"
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=f)
    assert set(_rec(f)) == {"waiting"}


def test_a_brief_with_no_signal_at_all_is_waiting(tmp_path, on_day):
    conn = db.connect_memory()
    f = tmp_path / "last_valid_brief.json"
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=f)
    assert set(_rec(f)) == {"waiting"}


def test_nothing_is_recorded_when_the_publish_is_refused(tmp_path, on_day, monkeypatch):
    # A recompute moved the inputs between the read and the publish: nothing is cached.
    conn = db.connect_memory()
    _signals(conn, DAY, COMPLETE)
    monkeypatch.setattr(brief, "_publish", lambda *a, **k: False)
    f = tmp_path / "last_valid_brief.json"
    out = brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=f)
    assert out["narrative_status"] == "generating" and not f.exists()


def test_only_the_reporting_today_s_brief_is_recorded(tmp_path, on_day):
    conn = db.connect_memory()
    _signals(conn, DAY, COMPLETE)
    f = tmp_path / "last_valid_brief.json"
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, today=DAY + timedelta(days=1), status_file=f)
    assert not f.exists()


def test_a_rewrite_that_finishes_after_midnight_is_not_recorded(tmp_path):
    f = tmp_path / "last_valid_brief.json"
    after_midnight = datetime(2026, 10, 8, 20, 5, tzinfo=timezone.utc)     # 00:05 on Oct 9 in Dubai
    assert not brief.record_brief(f, DAY, "model-x", True, 3, [], DUBAI, after_midnight)
    assert not f.exists()


class _ModelLM:
    primary = fallback = "model-x"

    def available(self):
        return True

    def structured(self, messages, schema, temperature=0.2, model=None):
        return {"narrative": "A steady day with nothing that needs attention.", "actions": [], "flags": []}


def test_the_background_rewrite_is_recorded_as_a_model_brief(tmp_path, on_day):
    conn = db.connect_memory()
    _signals(conn, DAY, COMPLETE)
    f = tmp_path / "last_valid_brief.json"
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=f)
    first = _rec(f)["complete"]
    assert first["kind"] == "template"
    out = brief.generate_brief(conn, _ModelLM(), DAY, "Owner", force=True, allow_llm=True, status_file=f)
    assert out["validated"] is True and out["narrative_status"] == "ready"
    c = _rec(f)["complete"]
    assert c["kind"] == "model" and c["model"] == "model-x"
    assert c["first_published_at"] == first["published_at"]


def test_a_final_template_while_waiting_for_whoop_is_waiting_not_delivered(tmp_path, on_day):
    conn = db.connect_memory()
    _signals(conn, DAY, [("sleep_duration", "flag", 5.25)])           # last night's recovery is not in
    f = tmp_path / "last_valid_brief.json"
    out = brief.generate_brief(conn, _ModelLM(), DAY, "Owner", force=True, allow_llm=True, status_file=f)
    assert out["model"] == brief.FINAL_TEMPLATE
    rec = _rec(f)
    assert set(rec) == {"waiting"} and rec["waiting"]["model"] == brief.FINAL_TEMPLATE
    assert "recovery_score" in rec["waiting"]["awaiting"]


def test_a_cached_brief_served_again_is_recorded_once_when_missing(tmp_path, on_day, monkeypatch):
    conn = db.connect_memory()
    _signals(conn, DAY, COMPLETE)
    f = tmp_path / "last_valid_brief.json"
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=f)
    f.unlink()                                # a restart, or a write that failed
    later = ON_DAY + timedelta(hours=1)
    monkeypatch.setattr(brief, "_utcnow", lambda: later)
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=f)   # the cached brief
    c = _rec(f)["complete"]
    assert c["date"] == "2026-10-08" and c["published_at"] == "2026-10-08T07:00:00+00:00"
    # Already recorded today: serving it again rewrites nothing.
    monkeypatch.setattr(brief, "_utcnow", lambda: later + timedelta(hours=1))
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=f)
    assert _rec(f)["complete"]["published_at"] == "2026-10-08T07:00:00+00:00"


def test_a_later_brief_keeps_the_first_time_and_a_new_day_starts_again(tmp_path):
    f = tmp_path / "last_valid_brief.json"
    t1 = datetime(2026, 10, 8, 3, 12, tzinfo=timezone.utc)          # 07:12 Dubai
    t2 = datetime(2026, 10, 8, 6, 52, tzinfo=timezone.utc)          # 10:52 Dubai
    assert brief.record_brief(f, DAY, "template", False, 3, [], DUBAI, t1)
    assert brief.record_brief(f, DAY, "model-x", True, 4, [], DUBAI, t2)
    c = _rec(f)["complete"]
    assert c["first_published_at"] == "2026-10-08T07:12:00+04:00"
    assert c["published_at"] == "2026-10-08T10:52:00+04:00"
    assert c["kind"] == "model" and c["model"] == "model-x"
    t3 = datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc)
    assert brief.record_brief(f, DAY + timedelta(days=1), "template", False, 3, [], DUBAI, t3)
    c = _rec(f)["complete"]
    assert c["date"] == "2026-10-09" and c["first_published_at"] == c["published_at"] == "2026-10-09T06:00:00+04:00"


def test_a_waiting_morning_turns_complete_and_the_waiting_part_stays_apart(tmp_path):
    f = tmp_path / "last_valid_brief.json"
    yesterday = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)
    assert brief.record_brief(f, DAY - timedelta(days=1), "template", False, 3, [], DUBAI, yesterday)
    w1 = datetime(2026, 10, 8, 2, 10, tzinfo=timezone.utc)          # 06:10 Dubai, Whoop not scored yet
    w2 = datetime(2026, 10, 8, 4, 30, tzinfo=timezone.utc)
    assert brief.record_brief(f, DAY, "template:final", False, 1, ["hrv_rmssd", "recovery_score"], DUBAI, w1)
    assert brief.record_brief(f, DAY, "template:final", False, 1, ["hrv_rmssd", "recovery_score"], DUBAI, w2)
    s = brief.brief_status(f, DUBAI, datetime(2026, 10, 8, 5, 30, tzinfo=timezone.utc))      # 09:30
    assert s["today"] is False and s["overdue"] is True
    assert s["waiting_since"] == "2026-10-08T06:10:00+04:00" and s["awaiting"] == ["hrv_rmssd", "recovery_score"]
    assert s["date"] == "2026-10-07" and s["age_hours"] == 26.5
    c1 = datetime(2026, 10, 8, 5, 40, tzinfo=timezone.utc)           # 09:40, the night is in
    assert brief.record_brief(f, DAY, "model-x", True, 4, [], DUBAI, c1)
    s = brief.brief_status(f, DUBAI, datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc))
    assert s["today"] is True and s["overdue"] is False and s["waiting_since"] is None and s["awaiting"] == []
    assert s["first_published_at"] == "2026-10-08T09:40:00+04:00"


def test_a_failed_write_never_breaks_the_brief(tmp_path, on_day):
    conn = db.connect_memory()
    _signals(conn, DAY, COMPLETE)
    blocked = tmp_path / "a-file"
    blocked.write_text("x")
    out = brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False,
                               status_file=blocked / "last_valid_brief.json")
    assert out["narrative_status"] == "template" and out["narrative"]


def test_status_reads_age_today_and_overdue(tmp_path):
    f = tmp_path / "last_valid_brief.json"
    before_due = datetime(2026, 10, 8, 4, 59, tzinfo=timezone.utc)   # 08:59 Dubai
    at_due = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)        # 09:00 Dubai
    s = brief.brief_status(f, DUBAI, before_due)
    assert s["published_at"] is None and s["age_hours"] is None and s["today"] is False
    assert s["overdue"] is False and s["due"] == "09:00" and s["waiting_since"] is None
    assert brief.brief_status(f, DUBAI, at_due)["overdue"] is True
    brief.record_brief(f, DAY - timedelta(days=1), "template", False, 3, [], DUBAI,
                       datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc))
    s = brief.brief_status(f, DUBAI, at_due)
    assert s["date"] == "2026-10-07" and s["today"] is False and s["overdue"] is True and s["age_hours"] == 26.0
    assert brief.brief_status(f, DUBAI, before_due)["overdue"] is False
    brief.record_brief(f, DAY, "model-x", True, 4, [], DUBAI, datetime(2026, 10, 8, 3, 12, tzinfo=timezone.utc))
    s = brief.brief_status(f, DUBAI, datetime(2026, 10, 8, 8, 0, tzinfo=timezone.utc))
    assert s["today"] is True and s["overdue"] is False and s["kind"] == "model" and s["age_hours"] == 4.8
    small = brief.brief_status(f, DUBAI, datetime(2026, 10, 8, 8, 0, tzinfo=timezone.utc), full=False)
    assert small == {"today": True, "overdue": False, "age_hours": 4.8, "kind": "model", "due": "09:00"}


def test_the_reporting_zone_decides_the_day_not_the_utc_date(tmp_path):
    f = tmp_path / "last_valid_brief.json"
    brief.record_brief(f, DAY - timedelta(days=1), "template", False, 3, [], DUBAI,
                       datetime(2026, 10, 7, 19, 30, tzinfo=timezone.utc))      # 23:30 Oct 7 in Dubai
    s = brief.brief_status(f, DUBAI, datetime(2026, 10, 7, 20, 30, tzinfo=timezone.utc))   # 00:30 Oct 8
    assert s["today"] is False and s["overdue"] is False


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"complete": 1}',
                                     '{"complete": {"date": "2026-10-08"}}',
                                     '{"complete": {"date": "2026-10-08", "published_at": "07:12"}}',
                                     '{"complete": {"date": "2026-10-08", "published_at": "2026-10-08T07:12:00"}}',
                                     '{"date": "2026-10-08", "published_at": "2026-10-08T07:12:00+04:00"}'])
def test_an_unreadable_record_reads_as_no_brief(tmp_path, content):
    f = tmp_path / "last_valid_brief.json"
    f.write_text(content, encoding="utf-8")
    s = brief.brief_status(f, DUBAI, datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc))
    assert s["published_at"] is None and s["date"] is None and s["today"] is False and s["overdue"] is True


class _NoModel:
    primary = fallback = "none"

    def available(self):
        return False


def _client(tmp_path, monkeypatch, lm):
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    raw = {"server": {"ingest_token": TOKEN}, "owner": {"timezone": "Asia/Dubai"},
           "storage": {"db_path": str(tmp_path / "helios.duckdb")},
           "notifications": {"macos_alerts": False}}
    c = TestClient(create_app(Settings(raw=raw)))
    c.__enter__()
    c.app.state.lm = lm                   # never the real local model on this Mac
    return c


def test_the_routes_show_the_last_valid_brief(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, _NoModel())
    try:
        conn, zone = client.app.state.conn, client.app.state.policy.zone
        before = client.get("/api/health").json()["brief"]
        assert set(before) == {"today", "overdue", "age_hours", "kind", "due"}
        assert before["today"] is False and before["age_hours"] is None
        tool = client.get("/api/tool/freshness", headers=H).json()
        assert set(tool) == {"brief", "watchdog"} and isinstance(tool["watchdog"], list)
        assert tool["brief"]["published_at"] is None

        _owned(conn, rc.reporting_today(zone), client.app.state.policy, COMPLETE)
        today = client.get("/api/today", headers=H).json()
        s = today["brief_status"]
        assert s["today"] is True and s["overdue"] is False and s["kind"] == "template"
        assert s["date"] == rc.reporting_today(zone).isoformat() and s["age_hours"] == 0.0
        assert _at(s["published_at"]).utcoffset() == timedelta(hours=4)
        assert (tmp_path / "last_valid_brief.json").is_file()

        health = client.get("/api/health").json()["brief"]
        assert health["today"] is True and "published_at" not in health and "awaiting" not in health
        assert client.get("/api/tool/freshness", headers=H).json()["brief"]["today"] is True
        assert client.get("/api/freshness", headers=H).json()["brief"]["today"] is True
    finally:
        client.__exit__(None, None, None)


def test_today_with_only_a_running_total_shows_waiting(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, _NoModel())
    try:
        _owned(client.app.state.conn, rc.reporting_today(client.app.state.policy.zone), client.app.state.policy,
               [("steps", "in_progress", 1234)])
        s = client.get("/api/today", headers=H).json()["brief_status"]
        assert s["today"] is False and s["waiting_since"] and "recovery_score" in s["awaiting"]
        assert client.get("/api/health").json()["brief"]["today"] is False
    finally:
        client.__exit__(None, None, None)


def test_the_route_s_background_rewrite_is_recorded_with_the_real_policy(tmp_path, monkeypatch):
    """The production path: /api/today publishes the template, then the
    background task (policy given, today from the policy's clock) writes the
    model's brief, and that is what the status file says."""
    client = _client(tmp_path, monkeypatch, _ModelLM())
    try:
        _owned(client.app.state.conn, rc.reporting_today(client.app.state.policy.zone), client.app.state.policy, COMPLETE)
        assert client.get("/api/today", headers=H).json()["narrative_status"] == "generating"
        f = tmp_path / "last_valid_brief.json"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (not f.exists() or _rec(f).get("complete", {}).get("kind") != "model"):
            time.sleep(0.05)
        c = _rec(f)["complete"]
        assert c["kind"] == "model" and c["model"] == "model-x"
        assert client.get("/api/today", headers=H).json()["brief_status"]["kind"] == "model"
    finally:
        client.__exit__(None, None, None)
