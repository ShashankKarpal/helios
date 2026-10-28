"""Hardening round 2026-10-28, H4: the Whoop wake polling must cover the brief
window. The morning step publishes only once Whoop's night has settled, which
needs a pull at most FRESH_PULL_MINUTES (30) old, and it rebuilds the brief
when Whoop revises the night until morning_window ends (12:00). So [whoop]
wake_window must start by [brief] not_before and end no earlier than
morning_window, and wake_poll_minutes must be at most 30. A gap is a warning
only (owner decision 2026-10-28): the step stays on, because publish_by still
publishes whatever is there. The warning is logged at startup and shown in
/api/health and in the freshness tool (/api/tool/freshness)."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from heliosd.config import Settings
from heliosd.main import create_app, whoop_window_warnings

TOKEN = "test-token-0123456789"


def _st(whoop=None, brief=None, tmp_path=None):
    raw = {"server": {"ingest_token": TOKEN}, "notifications": {"macos_alerts": False},
           "whoop": {"enabled": True, **(whoop or {})}, "brief": brief or {}}
    if tmp_path is not None:
        raw["storage"] = {"db_path": str(tmp_path / "h.duckdb")}
    return Settings(raw=raw)


def test_the_defaults_cover_the_brief_window():
    assert whoop_window_warnings(_st()) == []


def test_whoop_off_has_nothing_to_cover():
    assert whoop_window_warnings(Settings(raw={"whoop": {"enabled": False, "wake_window": "09:00-10:00"}})) == []


@pytest.mark.parametrize("whoop,brief,needle", [
    ({"wake_window": "08:00-12:00"}, {}, "starts at 08:00, after not_before 07:30"),
    ({"wake_window": "05:00-10:00"}, {}, "ends at 10:00, before morning_window ends at 12:00"),
    ({"wake_poll_minutes": 45}, {}, "wake_poll_minutes 45 is over 30"),
    ({"wake_poll_minutes": 0}, {}, "wake polling is off"),
    ({"wake_window": "nonsense"}, {}, "wake polling is off"),
    ({}, {"not_before": "04:30", "morning_window": "04:00-12:00"}, "after not_before 04:30"),
    ({}, {"morning_window": "05:00-13:00"}, "before morning_window ends at 13:00"),
])
def test_a_gap_is_named(whoop, brief, needle):
    warnings = whoop_window_warnings(_st(whoop, brief))
    assert len(warnings) == 1 and needle in warnings[0], warnings


def test_unusable_brief_settings_are_left_to_the_morning_step():
    assert whoop_window_warnings(_st(brief={"publish_by": "garbage"})) == []


def test_the_warning_is_logged_and_shown_in_health_and_freshness(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    with caplog.at_level(logging.WARNING, logger="heliosd"):
        app = create_app(_st({"wake_window": "05:00-10:00"}, tmp_path=tmp_path))
    assert any("whoop wake window" in r.getMessage() for r in caplog.records)
    with TestClient(app) as c:
        health = c.get("/api/health").json()
        assert len(health["warnings"]) == 1 and "ends at 10:00" in health["warnings"][0]
        fresh = c.get("/api/tool/freshness", headers={"X-Helios-Token": TOKEN}).json()
        assert fresh["warnings"] == health["warnings"]
        assert c.get("/api/brief-check").status_code == 200


def test_no_gap_shows_an_empty_list(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    with TestClient(create_app(_st(tmp_path=tmp_path))) as c:
        assert c.get("/api/health").json()["warnings"] == []
        assert c.get("/api/tool/freshness", headers={"X-Helios-Token": TOKEN}).json()["warnings"] == []
