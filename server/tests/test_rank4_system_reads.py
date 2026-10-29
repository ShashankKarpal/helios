"""Rank 4 (2026-10-29): the web app's System screen reads /api/health and
/api/tool/freshness, nothing else. This pins the contract it relies on: the
health block is open and carries the fields the screen shows; the freshness
tool needs a signed-in browser (cookie plus X-Helios-Web) and answers with the
full brief block, the watchdog rows and the Whoop window warnings. The server
did not change for rank 4; a change here would break the screen silently."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from heliosd import main
from heliosd.config import Settings
from heliosd.main import create_app

TOKEN = "test-token-0123456789"
BASE = "https://testserver"
WEB = {"X-Helios-Web": "1"}


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
    for key in ("today", "overdue", "due", "published_at", "age_hours", "check", "morning_step"):
        assert key in body["brief"], key
