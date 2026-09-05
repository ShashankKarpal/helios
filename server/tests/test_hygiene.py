"""Watchdog and ingestion hygiene (audit 2026-09-02 B2, B3, B4, B9, H7):
explicit received_at, corroboration tier, informational file sources, Whoop
cloud status, lab upload cap and cleanup."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from heliosd.config import Settings
from heliosd.ingest.bridge import ingest_batch
from heliosd.main import LABS_MAX_BYTES, create_app, ingest_sources
from heliosd.signals import watchdog
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry
from tests.synth import synth_batch

END = date(2026, 7, 15)
TOKEN = "test-token-0123456789"
H = {"X-Helios-Token": TOKEN}


def _conn_with_data():
    conn = db.connect_memory()
    policy, registry = MetricPolicy(), SourceRegistry()
    ingest_batch(conn, synth_batch(days=20, end_day=END), policy, registry)
    return conn, policy, registry


def test_received_at_is_written_explicitly_in_local_clock():
    conn, policy, registry = _conn_with_data()
    before = datetime.now()
    ingest_batch(conn, {"batch_id": "b-explicit", "samples": []}, policy, registry)
    row = db.fetchall(conn, "SELECT received_at FROM sync_log WHERE batch_id = 'b-explicit'")[0][0]
    assert abs((row - before).total_seconds()) < 5


def test_corroboration_decay_is_informational_and_never_notified():
    conn, policy, registry = _conn_with_data()
    # Whoop's HealthKit heart-rate copy wrote for a while and stopped a week
    # before the end, while the primary strap keeps writing every hour.
    whoop_hr = []
    for i in range(19, 7, -1):
        ts = datetime.combine(END - timedelta(days=i), datetime.min.time()) + timedelta(hours=8)
        whoop_hr.append({"hk_type": "HKQuantityTypeIdentifierHeartRate", "value": 70, "unit": "count/min",
                         "start": ts.isoformat(), "end": (ts + timedelta(minutes=1)).isoformat(),
                         "source_name": "WHOOP", "uuid": f"whoop-hr-{i}"})
    ingest_batch(conn, {"batch_id": "whoop-hr", "samples": whoop_hr}, policy, registry)
    now = datetime.combine(END, datetime.min.time()) + timedelta(hours=9)
    report = watchdog.check(conn, policy, now=now, registry=registry)
    rows = [r for r in report if r["metric"] == "heart_rate" and r["device_key"] == "whoop"]
    assert rows and rows[0]["status"] == "corroboration_decayed"
    assert rows[0]["tier"] == "informational" and rows[0]["notify"] is False
    # Informational rows sort after real problems and are skipped for alerts.
    assert all(r.get("tier") == "informational" for r in report[report.index(rows[0]):])
    worst = watchdog.notifiable(report)
    assert worst is None or worst.get("notify") is not False


def test_file_source_freshness(tmp_path):
    feed = tmp_path / "events.jsonl"
    fresh = datetime.now() - timedelta(hours=1)
    feed.write_text(json.dumps({"ts": fresh.isoformat(), "event": "plugged_in"}) + "\n", encoding="utf-8")
    policy = MetricPolicy()
    policy.sources = [{"key": "zest_events", "path": str(feed), "cadence_hours": 24}]
    assert watchdog.check_sources(policy) == []
    stale = datetime.now() - timedelta(hours=60)
    feed.write_text(json.dumps({"ts": stale.isoformat(), "event": "plugged_in"}) + "\n", encoding="utf-8")
    rows = watchdog.check_sources(policy)
    assert rows[0]["device_key"] == "zest_events" and rows[0]["status"] == "stale"
    assert rows[0]["tier"] == "informational" and rows[0]["notify"] is False
    policy.sources = [{"key": "gone", "path": str(tmp_path / "missing.jsonl")}]
    assert watchdog.check_sources(policy)[0]["status"] == "silent"


def test_whoop_cloud_status_reports_behind_and_errors():
    conn = db.connect_memory()
    now = datetime(2026, 7, 15, 9)
    assert watchdog.whoop_cloud_status(conn, now, enabled=False, last_error=None) is None
    row = watchdog.whoop_cloud_status(conn, now, enabled=True, last_error=None)
    assert row and row["device_key"] == "whoop_cloud" and row["status"] == "silent"
    db.execute(conn, "INSERT INTO whoop_cache (date, kind, payload) VALUES (?, 'recovery', '{}')",
               [now.date()])
    assert watchdog.whoop_cloud_status(conn, now, enabled=True, last_error=None) is None
    row = watchdog.whoop_cloud_status(conn, now, enabled=True, last_error="token refresh failed (HTTP 401)")
    assert row and row["status"] == "error" and "401" in row["fix"] and "/whoop/login" in row["fix"]


def test_ingest_sources_is_idempotent_and_never_undoable(tmp_path):
    conn = db.connect_memory()
    feed = tmp_path / "zest-events.jsonl"
    lines = [{"ts": "2026-09-05T04:47:51Z", "source": "zest", "host": "mac", "event": "zest_start", "version": "1"},
             {"ts": "2026-09-05T05:10:00Z", "source": "zest", "host": "mac", "event": "thermal_state", "level": "fair"}]
    feed.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    policy = MetricPolicy()
    policy.sources = [{"key": "zest_events", "path": str(feed), "ingest": "events"}]
    app = SimpleNamespace(state=SimpleNamespace(policy=policy, conn=conn))
    assert ingest_sources(app) == {"feeds": 1, "inserted": 2}
    assert ingest_sources(app) == {"feeds": 1, "inserted": 0}
    rows = db.fetchdicts(conn, "SELECT kind, source, payload FROM events ORDER BY ts")
    assert [r["kind"] for r in rows] == ["system", "system"]
    assert all(r["source"] == "zest_events" for r in rows)
    payload = json.loads(rows[1]["payload"])
    assert payload["event"] == "thermal_state" and payload["level"] == "fair" and "host" not in payload
    from heliosd.narrative.quicklog import undo_last
    assert undo_last(conn)["removed"] is False


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    raw = {"server": {"ingest_token": TOKEN},
           "storage": {"db_path": str(tmp_path / "helios.duckdb")},
           "notifications": {"macos_alerts": False}}
    with TestClient(create_app(Settings(raw=raw))) as c:
        yield c, tmp_path


def test_lab_upload_cap_type_and_cleanup(client):
    c, tmp_path = client
    inbox = tmp_path / "labs_inbox"
    r = c.post("/api/labs/parse", files={"file": ("big.pdf", b"x" * (LABS_MAX_BYTES + 1), "application/pdf")}, headers=H)
    assert r.status_code == 413
    r = c.post("/api/labs/parse", files={"file": ("notes.docx", b"x", "application/octet-stream")}, headers=H)
    assert r.status_code == 415
    r = c.post("/api/labs/parse", files={"file": ("panel.pdf", b"%PDF-1.4 not really", "application/pdf")}, headers=H)
    assert r.status_code == 422
    # The scratch upload is gone whether or not the parse succeeded.
    assert inbox.exists() and list(inbox.iterdir()) == []
