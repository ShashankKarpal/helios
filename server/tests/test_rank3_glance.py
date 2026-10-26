"""Rank 3: the glance, a small summary of today's brief for the phone's widget
and morning notification. Written beside the status file when today's brief
is published (0600, atomic, only when its content changed), served by
GET /api/glance with the brief state read at that moment; the route needs the
token and never reads the store. Synthetic values only."""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from heliosd.config import Settings
from heliosd.main import create_app
from heliosd.narrative import brief, glance
from heliosd.signals import recompute as rc
from heliosd.store import db

DAY = date(2026, 10, 8)
TOKEN = "test-token-0123456789"
H = {"X-Helios-Token": TOKEN}
ON_DAY = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
COMPLETE = [("recovery_score", "favorable", 71.5), ("sleep_duration", "flag", 5.25),
            ("hrv_rmssd", "neutral", 83.25)]


@pytest.fixture
def clock(monkeypatch):
    """The brief's clock, movable: generate_brief without a policy reads DAY in UTC."""
    now = {"t": ON_DAY}
    monkeypatch.setattr(brief, "_utcnow", lambda: now["t"])
    return now


def _signals(conn, day, rows):
    with db.transaction(conn) as c:
        c.execute("DELETE FROM signals WHERE date = ?", [day])
        for metric, state, value in rows:
            c.execute("INSERT INTO signals (date, metric, state, value, unit, context_flags, why) "
                      "VALUES (?, ?, ?, ?, 'u', '[]', 'synthetic')", [day, metric, state, value])


def _owned(conn, day, policy, rows):
    from heliosd.signals.markers import owner_device
    with db.transaction(conn) as c:
        c.execute("DELETE FROM signals WHERE date = ?", [day])
        for metric, state, value in rows:
            c.execute("INSERT INTO signals (date, metric, state, value, unit, device_key, context_flags, why) "
                      "VALUES (?, ?, ?, ?, 'u', ?, '[]', 'synthetic')",
                      [day, metric, state, value, owner_device(policy, metric)])


def _rec(f):
    return json.loads(f.read_text(encoding="utf-8"))


def _texts(rec):
    return {r["metric"]: (r["text"], r["state"]) for r in rec["rows"]}


# ---- display text and rows

@pytest.mark.parametrize("metric, value, text", [
    ("recovery_score", 71.5, "72%"),
    ("recovery_score", 70.5, "71%"),            # half up, as the web app rounds
    ("sleep_duration", 7.2, "7h 12m"),
    ("sleep_duration", 0.75, "45m"),
    ("sleep_duration", 7.999, "8h 0m"),
    ("hrv_rmssd", 47.5, "48 ms"),
    ("resting_hr", 57.4, "57 bpm"),
    ("resting_hr", None, None),
    ("hrv_rmssd", float("nan"), None),
    ("hrv_rmssd", "not a number", None),
])
def test_display_text_follows_the_web_app(metric, value, text):
    assert glance.display_text(metric, value) == text


def test_rows_come_in_the_phone_order_with_waiting_stand_in_and_judged_states():
    signals = [
        {"metric": "steps", "state": "in_progress", "value": 4321},
        {"metric": "resting_hr", "state": "neutral", "value": 58.0, "fallback": True},
        {"metric": "hrv_rmssd", "state": "flag", "value": 30.4},
        {"metric": "recovery_score", "state": "insufficient", "value": 64.0},
    ]
    rec = glance.build_glance(DAY, signals, "Mostly steady.", ["sleep_duration"], False, ON_DAY)
    assert [(r["metric"], r["label"]) for r in rec["rows"]] == [
        ("recovery_score", "Recovery"), ("sleep_duration", "Sleep"), ("hrv_rmssd", "HRV"),
        ("resting_hr", "Resting HR")]
    assert _texts(rec) == {"recovery_score": ("64%", "insufficient"), "sleep_duration": ("waiting", "waiting"),
                           "hrv_rmssd": ("30 ms", "flag"), "resting_hr": ("58 bpm", "stand-in")}
    assert rec["flags"] == 1 and rec["awaiting"] == ["sleep_duration"] and rec["complete"] is False
    assert rec["date"] == "2026-10-08" and rec["made_at"] == "2026-10-08T06:00:00+00:00"
    assert rec["verdict"] == "Mostly steady."
    assert "steps" not in json.dumps(rec)            # four morning numbers only


# ---- written at publish time

def test_publishing_today_writes_the_glance_beside_the_status_file(tmp_path, clock):
    conn = db.connect_memory()
    _signals(conn, DAY, COMPLETE)
    status = brief.status_file_for(tmp_path / "helios.duckdb")
    out = brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=status)
    f = tmp_path / "glance.json"
    assert glance.glance_file_for(status) == f
    rec = _rec(f)
    assert rec["complete"] is True and rec["verdict"] == out["verdict"]
    assert _texts(rec) == {"recovery_score": ("72%", "favorable"), "sleep_duration": ("5h 15m", "flag"),
                           "hrv_rmssd": ("83 ms", "neutral"), "resting_hr": ("waiting", "waiting")}
    assert rec["flags"] == 1 and rec["made_at"] == "2026-10-08T06:00:00+00:00"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))


def test_a_publish_waiting_for_whoop_writes_a_waiting_glance(tmp_path, clock):
    conn = db.connect_memory()
    _signals(conn, DAY, [("steps", "in_progress", 1234)])
    status = tmp_path / "last_valid_brief.json"
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=status)
    rec = _rec(tmp_path / "glance.json")
    assert rec["complete"] is False and "recovery_score" in rec["awaiting"]
    assert all(r["state"] == "waiting" for r in rec["rows"])


@pytest.mark.parametrize("rows, complete", [(COMPLETE, True), ([("steps", "in_progress", 1234)], False),
                                            ([("recovery_score", "fallback", 60.0)], False),
                                            # a settled number but last night's recovery not in
                                            ([("sleep_duration", "neutral", 7.0)], False)])
def test_the_glance_and_the_status_file_agree_on_complete(tmp_path, clock, rows, complete):
    conn = db.connect_memory()
    _signals(conn, DAY, rows)
    status = tmp_path / "last_valid_brief.json"
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=status)
    assert _rec(tmp_path / "glance.json")["complete"] is complete
    assert ("complete" in _rec(status)) is complete


def test_another_day_s_brief_writes_no_glance(tmp_path, clock):
    conn = db.connect_memory()
    yesterday = DAY - timedelta(days=1)
    _signals(conn, yesterday, COMPLETE)
    brief.generate_brief(conn, None, yesterday, "Owner", allow_llm=False, today=DAY,
                         status_file=tmp_path / "last_valid_brief.json")
    assert not (tmp_path / "glance.json").exists()


def test_a_publish_after_midnight_in_the_zone_is_not_written(tmp_path):
    rec = glance.record_glance(tmp_path / "glance.json", DAY, [], "v", [], True,
                               zone=timezone.utc, now=ON_DAY + timedelta(days=1))
    assert rec is False and not (tmp_path / "glance.json").exists()


def test_an_unchanged_glance_is_not_rewritten_and_a_change_is(tmp_path, clock):
    conn = db.connect_memory()
    _signals(conn, DAY, COMPLETE)
    status = tmp_path / "last_valid_brief.json"
    f = tmp_path / "glance.json"
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=status)
    first = os.stat(f)
    clock["t"] = ON_DAY + timedelta(minutes=10)
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=status)
    again = os.stat(f)
    assert (again.st_ino, again.st_mtime_ns) == (first.st_ino, first.st_mtime_ns)
    assert _rec(f)["made_at"] == "2026-10-08T06:00:00+00:00"
    _signals(conn, DAY, COMPLETE[:2] + [("hrv_rmssd", "flag", 20.0)])
    brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, force=True, status_file=status)
    rec = _rec(f)
    assert _texts(rec)["hrv_rmssd"] == ("20 ms", "flag") and rec["flags"] == 2
    assert rec["made_at"] == "2026-10-08T06:10:00+00:00"


def test_a_failed_glance_write_never_breaks_the_brief_and_logs_no_value(tmp_path, clock, caplog):
    conn = db.connect_memory()
    _signals(conn, DAY, COMPLETE)
    (tmp_path / "glance.json").mkdir()            # the rename onto it fails
    status = tmp_path / "last_valid_brief.json"
    with caplog.at_level(logging.INFO, logger="heliosd"):
        out = brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False, status_file=status)
    assert out["narrative_status"] == "template" and "complete" in _rec(status)
    assert "could not write the glance file" in caplog.text
    for value in ("71.5", "72%", "83", "5h 15m"):
        assert value not in caplog.text


@pytest.mark.parametrize("content", ["", "{", "[]", '{"date": 5, "rows": []}', '{"date": "2026-10-08"}'])
def test_anything_but_a_glance_reads_as_none(tmp_path, content):
    f = tmp_path / "glance.json"
    f.write_text(content, encoding="utf-8")
    assert glance.read_glance(f) is None
    assert glance.read_glance(tmp_path / "missing.json") is None


# ---- the route

class _NoModel:
    primary = fallback = "none"

    def available(self):
        return False


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    raw = {"server": {"ingest_token": TOKEN}, "owner": {"timezone": "Asia/Dubai"},
           "storage": {"db_path": str(tmp_path / "helios.duckdb")},
           "notifications": {"macos_alerts": False}}
    c = TestClient(create_app(Settings(raw=raw)))
    c.__enter__()
    c.app.state.lm = _NoModel()
    return c


def test_the_route_needs_the_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    try:
        r = client.get("/api/glance")
        assert r.status_code == 401
        assert "rows" not in r.text
    finally:
        client.__exit__(None, None, None)


def test_the_route_before_and_after_today_s_brief(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    try:
        r = client.get("/api/glance", headers=H)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        body = r.json()
        assert set(body) == {"schema", "served_at", "zone", "due", "brief", "summary"}
        assert body["schema"] == 1 and body["zone"] == "Asia/Dubai" and body["due"] == "09:00"
        assert datetime.fromisoformat(body["served_at"]).utcoffset() == timedelta(hours=4)
        assert body["summary"] is None and body["brief"]["today"] is False and body["brief"]["ready_at"] is None
        assert body["brief"]["check"] == client.get("/api/brief-check").text

        conn, policy = client.app.state.conn, client.app.state.policy
        _owned(conn, rc.reporting_today(policy.zone), policy, COMPLETE)
        assert client.get("/api/today", headers=H).status_code == 200
        body = client.get("/api/glance", headers=H).json()
        assert body["brief"]["today"] is True and body["brief"]["check"] == "ok"
        fresh = client.get("/api/freshness", headers=H).json()["brief"]
        assert body["brief"]["ready_at"] == fresh["first_published_at"]
        s = body["summary"]
        assert s["date"] == rc.reporting_today(policy.zone).isoformat() and s["complete"] is True
        assert _texts(s)["recovery_score"] == ("72%", "favorable")
    finally:
        client.__exit__(None, None, None)


def test_the_route_never_touches_the_store(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    try:
        real = client.app.state.conn

        class _Exploding:
            def __getattr__(self, name):
                raise AssertionError(f"the glance route used the store ({name})")

        client.app.state.conn = _Exploding()
        try:
            r = client.get("/api/glance", headers=H)
        finally:
            client.app.state.conn = real
        assert r.status_code == 200 and r.json()["summary"] is None
    finally:
        client.__exit__(None, None, None)
