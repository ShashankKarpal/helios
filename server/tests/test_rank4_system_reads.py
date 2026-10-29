"""Rank 4 (2026-10-29): the web app's System screen reads /api/health and
/api/tool/freshness, nothing else. This pins the contract it relies on: the
health block is open and carries the fields the screen shows; the freshness
tool needs a signed-in browser (cookie plus X-Helios-Web) and answers with the
full brief block, the watchdog rows and the Whoop window warnings. The server
did not change for rank 4; a change here would break the screen silently."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from heliosd import main
from heliosd.config import Settings
from heliosd.main import create_app
from tests.synth import synth_batch

TOKEN = "test-token-0123456789"
BASE = "https://testserver"
WEB = {"X-Helios-Web": "1"}
# Every field the System screen reads from a watchdog row (web/src/types.ts
# WatchdogRow) and from the brief block (BriefStatus plus check, morning_step).
ROW_KEYS = {"metric", "device_key", "status", "last_seen", "age_hours", "fix"}
BRIEF_KEYS = {"date", "first_published_at", "published_at", "age_hours", "kind", "waiting_since",
              "awaiting", "today", "due", "overdue", "check", "morning_step"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><div id=root></div>\n", encoding="utf-8")
    monkeypatch.setenv("HELIOS_WEB_DIST", str(dist))
    monkeypatch.setattr(main, "SESSION_FAIL_DELAY_S", 0.0)
    raw = {"server": {"ingest_token": TOKEN}, "storage": {"db_path": str(tmp_path / "h.duckdb")},
           "notifications": {"macos_alerts": False}}
    with TestClient(create_app(Settings(raw=raw)), base_url=BASE) as c:
        yield c


def test_health_is_open_and_has_what_system_shows(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    for key in ("ok", "samples", "llm", "whoop", "auth", "started_at", "warnings"):
        assert key in body, key
    assert body["auth"] is True and isinstance(body["samples"], int)


def test_the_freshness_tool_needs_a_signed_in_browser(client):
    assert client.get("/api/tool/freshness", headers=WEB).status_code == 401
    r = client.post("/api/session", json={"token": TOKEN}, headers={**WEB, "Origin": BASE})
    assert r.status_code == 200
    assert client.get("/api/tool/freshness").status_code == 401  # the cookie alone does not count


def test_the_freshness_tool_answers_with_the_brief_rows_and_warnings(client):
    assert client.post("/api/session", json={"token": TOKEN}, headers={**WEB, "Origin": BASE}).status_code == 200
    r = client.get("/api/tool/freshness", headers=WEB)
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"brief", "watchdog", "warnings"}
    assert isinstance(body["watchdog"], list) and isinstance(body["warnings"], list)
    assert BRIEF_KEYS <= set(body["brief"]), BRIEF_KEYS - set(body["brief"])


def test_a_late_source_comes_back_as_a_row_with_every_field_system_reads(client):
    """Review finding 2: an empty store returns no rows, so the row shape was
    never pinned. Three days of synthetic data that ended ten days ago make
    every stream late; each row must carry what WatchRow renders, and tier is
    absent or one of the two values the screen splits on."""
    old = synth_batch(days=3, seed=11, end_day=date.today() - timedelta(days=10))
    assert client.post("/ingest", json=old, headers={"X-Helios-Token": TOKEN}).status_code == 200
    assert client.post("/api/session", json={"token": TOKEN}, headers={**WEB, "Origin": BASE}).status_code == 200
    rows = client.get("/api/tool/freshness", headers=WEB).json()["watchdog"]
    late = [r for r in rows if r["metric"] != "*"]
    assert late, "ten-day-old data must leave at least one late stream"
    for r in late:
        assert ROW_KEYS <= set(r), (r["metric"], ROW_KEYS - set(r))
        assert isinstance(r["age_hours"], (int, float)) and r["age_hours"] > 24 * 9
        assert r["status"] in {"stale", "silent", "corroboration_decayed"}
    for r in rows:
        assert r.get("tier", "primary") in {"primary", "informational"}
        assert isinstance(r.get("message", ""), str) and isinstance(r.get("fix", ""), str)
