"""Rank 2: the last valid brief, its age and whether it is overdue.

The narratives table is a cache that every recompute empties for its dates, so
a published brief is also recorded in a small content-free file beside the
store; /api/health, /api/freshness, the MCP tool's /api/tool/freshness and
/api/today read it. Valid: today's brief with at least one computed signal,
from the local model or the template. Synthetic values only."""

from __future__ import annotations

import json
import os
import stat
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
KEYS = {"date", "first_published_at", "published_at", "kind", "model", "signals", "awaiting"}


def _signals(conn, day, rows):
    with db.transaction(conn) as c:
        c.execute("DELETE FROM signals WHERE date = ?", [day])
        for metric, state, value in rows:
            c.execute("INSERT INTO signals (date, metric, state, value, unit, context_flags, why) "
                      "VALUES (?, ?, ?, ?, 'u', '[]', 'synthetic')", [day, metric, state, value])


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def test_a_published_brief_is_recorded_as_the_last_valid_one(tmp_path):
    conn = db.connect_memory()
    _signals(conn, DAY, [("recovery_score", "favorable", 71.5), ("sleep_duration", "flag", 5.25)])
    f = brief.status_file_for(tmp_path / "helios.duckdb")
    assert f == tmp_path / "last_valid_brief.json"
    out = brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=f)
    assert out["narrative_status"] == "template"
    rec = json.loads(f.read_text(encoding="utf-8"))
    assert set(rec) == KEYS
    assert rec["date"] == "2026-10-08" and rec["kind"] == "template" and rec["model"] == "template"
    assert rec["signals"] == 2 and rec["first_published_at"] == rec["published_at"]
    assert _at(rec["published_at"]).tzinfo is not None
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))


def test_the_record_carries_no_health_values(tmp_path):
    conn = db.connect_memory()
    _signals(conn, DAY, [("recovery_score", "favorable", 71.5), ("hrv_rmssd", "neutral", 83.25),
                         ("sleep_duration", "flag", 5.25)])
    f = tmp_path / "last_valid_brief.json"
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=f)
    text = f.read_text(encoding="utf-8")
    for value in ("71.5", "83.25", "5.25", "favorable", "flag", "synthetic"):
        assert value not in text, value


def test_a_brief_without_a_computed_signal_is_not_valid(tmp_path):
    conn = db.connect_memory()
    f = tmp_path / "last_valid_brief.json"
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=f)
    assert not f.exists()


def test_nothing_is_recorded_when_the_publish_is_refused(tmp_path, monkeypatch):
    # A recompute moved the inputs between the read and the publish: nothing is cached.
    conn = db.connect_memory()
    _signals(conn, DAY, [("recovery_score", "favorable", 71.5)])
    monkeypatch.setattr(brief, "_publish", lambda *a, **k: False)
    f = tmp_path / "last_valid_brief.json"
    out = brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=f)
    assert out["narrative_status"] == "generating" and not f.exists()


def test_only_the_reporting_today_s_brief_is_recorded(tmp_path):
    conn = db.connect_memory()
    _signals(conn, DAY, [("recovery_score", "favorable", 71.5)])
    f = tmp_path / "last_valid_brief.json"
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, today=DAY + timedelta(days=1), status_file=f)
    assert not f.exists()


class _ModelLM:
    primary = fallback = "model-x"

    def available(self):
        return True

    def structured(self, messages, schema, temperature=0.2, model=None):
        return {"narrative": "A steady day with nothing that needs attention.", "actions": [], "flags": []}


def test_the_background_rewrite_is_recorded_as_a_model_brief(tmp_path):
    conn = db.connect_memory()
    _signals(conn, DAY, [("recovery_score", "favorable", 71.5)])
    f = tmp_path / "last_valid_brief.json"
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=f)
    first = json.loads(f.read_text(encoding="utf-8"))
    assert first["kind"] == "template"
    out = brief.generate_brief(conn, _ModelLM(), DAY, "Owner", force=True, allow_llm=True, status_file=f)
    assert out["validated"] is True and out["narrative_status"] == "ready"
    rec = json.loads(f.read_text(encoding="utf-8"))
    assert rec["kind"] == "model" and rec["model"] == "model-x"
    assert rec["first_published_at"] == first["published_at"]


def test_a_final_template_while_waiting_for_whoop_records_what_it_waits_for(tmp_path):
    conn = db.connect_memory()
    _signals(conn, DAY, [("sleep_duration", "flag", 5.25)])           # last night's recovery is not in
    f = tmp_path / "last_valid_brief.json"
    out = brief.generate_brief(conn, _ModelLM(), DAY, "Owner", force=True, allow_llm=True, status_file=f)
    assert out["model"] == brief.FINAL_TEMPLATE
    rec = json.loads(f.read_text(encoding="utf-8"))
    assert rec["kind"] == "template" and rec["model"] == brief.FINAL_TEMPLATE
    assert "recovery_score" in rec["awaiting"]


def test_a_later_brief_keeps_the_first_time_and_a_new_day_starts_again(tmp_path):
    f = tmp_path / "last_valid_brief.json"
    t1 = datetime(2026, 10, 8, 3, 12, tzinfo=timezone.utc)          # 07:12 Dubai
    t2 = datetime(2026, 10, 8, 6, 52, tzinfo=timezone.utc)          # 10:52 Dubai
    assert brief.record_valid(f, DAY, "template", False, 3, ["recovery_score"], DUBAI, t1)
    assert brief.record_valid(f, DAY, "model-x", True, 4, [], DUBAI, t2)
    rec = json.loads(f.read_text(encoding="utf-8"))
    assert rec["first_published_at"] == "2026-10-08T07:12:00+04:00"
    assert rec["published_at"] == "2026-10-08T10:52:00+04:00"
    assert rec["kind"] == "model" and rec["model"] == "model-x" and rec["awaiting"] == []
    # An earlier day never replaces a later record.
    assert not brief.record_valid(f, DAY - timedelta(days=1), "template", False, 3, [], DUBAI, t2)
    assert json.loads(f.read_text(encoding="utf-8"))["date"] == "2026-10-08"
    # The next day starts its own first time.
    t3 = datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc)
    assert brief.record_valid(f, DAY + timedelta(days=1), "template", False, 3, [], DUBAI, t3)
    rec = json.loads(f.read_text(encoding="utf-8"))
    assert rec["date"] == "2026-10-09" and rec["first_published_at"] == rec["published_at"] == "2026-10-09T06:00:00+04:00"


def test_a_failed_write_never_breaks_the_brief(tmp_path):
    conn = db.connect_memory()
    _signals(conn, DAY, [("recovery_score", "favorable", 71.5)])
    blocked = tmp_path / "a-file"
    blocked.write_text("x")
    out = brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False,
                               status_file=blocked / "last_valid_brief.json")
    assert out["narrative_status"] == "template" and out["narrative"]


def test_status_reads_age_today_and_overdue(tmp_path):
    f = tmp_path / "last_valid_brief.json"
    before_due = datetime(2026, 10, 8, 4, 59, tzinfo=timezone.utc)   # 08:59 Dubai
    at_due = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)        # 09:00 Dubai
    # Nothing recorded yet.
    s = brief.brief_status(f, DUBAI, before_due)
    assert s["published_at"] is None and s["age_hours"] is None and s["today"] is False
    assert s["overdue"] is False and s["due"] == "09:00"
    assert brief.brief_status(f, DUBAI, at_due)["overdue"] is True
    # Yesterday's brief: not today, overdue from 09:00.
    brief.record_valid(f, DAY - timedelta(days=1), "template", False, 3, [], DUBAI,
                       datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc))
    s = brief.brief_status(f, DUBAI, at_due)
    assert s["date"] == "2026-10-07" and s["today"] is False and s["overdue"] is True and s["age_hours"] == 26.0
    assert brief.brief_status(f, DUBAI, before_due)["overdue"] is False
    # Today's brief: on time, never overdue.
    brief.record_valid(f, DAY, "model-x", True, 4, [], DUBAI, datetime(2026, 10, 8, 3, 12, tzinfo=timezone.utc))
    s = brief.brief_status(f, DUBAI, datetime(2026, 10, 8, 8, 0, tzinfo=timezone.utc))
    assert s["today"] is True and s["overdue"] is False and s["kind"] == "model" and s["age_hours"] == 4.8
    assert s["first_published_at"] == "2026-10-08T07:12:00+04:00"


def test_the_reporting_zone_decides_the_day_not_the_utc_date(tmp_path):
    f = tmp_path / "last_valid_brief.json"
    # 23:30 Dubai on Oct 7 is still Oct 7 there, though UTC says 19:30.
    brief.record_valid(f, DAY - timedelta(days=1), "template", False, 3, [], DUBAI,
                       datetime(2026, 10, 7, 19, 30, tzinfo=timezone.utc))
    # 00:30 Dubai on Oct 8 (20:30 UTC on Oct 7): a new reporting day, not overdue yet.
    s = brief.brief_status(f, DUBAI, datetime(2026, 10, 7, 20, 30, tzinfo=timezone.utc))
    assert s["today"] is False and s["overdue"] is False


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"date": "2026-10-08"}',
                                     '{"date": "2026-10-08", "published_at": "07:12"}',
                                     '{"date": "2026-10-08", "published_at": "2026-10-08T07:12:00"}'])
def test_an_unreadable_record_reads_as_no_brief(tmp_path, content):
    f = tmp_path / "last_valid_brief.json"
    f.write_text(content, encoding="utf-8")
    s = brief.brief_status(f, DUBAI, datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc))
    assert s["published_at"] is None and s["date"] is None and s["today"] is False and s["overdue"] is True


class _NoModel:
    primary = fallback = "none"

    def available(self):
        return False


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    raw = {"server": {"ingest_token": TOKEN}, "owner": {"timezone": "Asia/Dubai"},
           "storage": {"db_path": str(tmp_path / "helios.duckdb")},
           "notifications": {"macos_alerts": False}}
    with TestClient(create_app(Settings(raw=raw))) as c:
        c.app.state.lm = _NoModel()          # never the real local model on this Mac
        yield c


def test_the_routes_show_the_last_valid_brief(client, tmp_path):
    conn, zone = client.app.state.conn, client.app.state.policy.zone
    before = client.get("/api/health").json()["brief"]
    assert before["published_at"] is None and before["today"] is False and "overdue" in before
    tool = client.get("/api/tool/freshness", headers=H).json()
    assert set(tool) == {"brief", "watchdog"} and isinstance(tool["watchdog"], list)
    assert tool["brief"]["published_at"] is None

    _signals(conn, rc.reporting_today(zone), [("recovery_score", "favorable", 71.5)])
    today = client.get("/api/today", headers=H).json()
    s = today["brief_status"]
    assert s["today"] is True and s["overdue"] is False and s["kind"] == "template"
    assert s["date"] == rc.reporting_today(zone).isoformat() and s["age_hours"] == 0.0
    assert _at(s["published_at"]).utcoffset() == timedelta(hours=4)
    assert (tmp_path / "last_valid_brief.json").is_file()

    health = client.get("/api/health").json()["brief"]
    assert health["today"] is True and health["published_at"] == s["published_at"]
    assert client.get("/api/tool/freshness", headers=H).json()["brief"]["today"] is True
    assert client.get("/api/freshness", headers=H).json()["brief"]["today"] is True


def test_today_without_signals_publishes_no_valid_brief(client, tmp_path):
    body = client.get("/api/today", headers=H).json()
    assert body["brief_status"]["published_at"] is None
    assert not (tmp_path / "last_valid_brief.json").exists()
