"""Durability: export of the irreplaceable tables, checksum verification, and
the restore drill, plus the daemon endpoint that the nightly CLI calls."""

from __future__ import annotations

import json
from datetime import date, datetime

from fastapi.testclient import TestClient

from heliosd import backup as bk
from heliosd.config import Settings
from heliosd.main import create_app
from heliosd.store import db

TOKEN = "test-token-0123456789"
H = {"X-Helios-Token": TOKEN}


def _seed(conn):
    db.execute(conn, "INSERT INTO events (event_id, kind, ts, payload, source) VALUES "
                     "('e1', 'caffeine', ?, '{\"item\": \"espresso\"}', 'shortcut')", [datetime(2026, 8, 17, 16)])
    db.execute(conn, "INSERT INTO labs (lab_id, panel_date, biomarker, value, unit) VALUES "
                     "('l1', ?, 'Fasting glucose', 92, 'mg/dL')", [date(2026, 3, 1)])
    db.execute(conn, "INSERT INTO narratives (date, narrative, model, validated) VALUES (?, 'Steady.', 'm', true)",
               [date(2026, 9, 5)])
    db.execute(conn, "INSERT INTO whoop_cache (date, kind, payload) VALUES (?, 'recovery', '{\"score\": 71}')",
               [date(2026, 9, 5)])
    db.execute(conn, "INSERT INTO actions (action_id, date, text, status) VALUES ('a1', ?, 'Walk', 'adopted')",
               [date(2026, 9, 5)])
    db.execute(conn, "INSERT INTO profile_facts (key, value) VALUES ('tz', 'Asia/Dubai')")


def test_export_and_restore_round_trip(tmp_path):
    conn = db.connect_memory()
    _seed(conn)
    m = bk.export_tables(conn, tmp_path / "2026-09-05")
    assert set(m["tables"]) == set(bk.IRREPLACEABLE_TABLES)
    assert m["tables"]["events"]["rows"] == 1 and m["tables"]["chat_messages"]["rows"] == 0
    assert bk.verify_files(tmp_path / "2026-09-05") == []
    res = bk.restore_test(tmp_path / "2026-09-05")
    assert res["ok"], res
    assert res["tables"]["whoop_cache"] == {"expected": 1, "loaded": 1, "restored": 1}
    # Restored values survive the JSON round trip with their types.
    conn2 = db.connect_memory()
    bk.load_tables(conn2, tmp_path / "2026-09-05")
    row = db.fetchdicts(conn2, "SELECT ts, payload FROM events")[0]
    assert row["ts"] == datetime(2026, 8, 17, 16) and json.loads(row["payload"])["item"] == "espresso"
    assert db.fetchall(conn2, "SELECT status FROM actions")[0][0] == "adopted"


def test_restore_test_catches_tampering(tmp_path):
    conn = db.connect_memory()
    _seed(conn)
    d = tmp_path / "x"
    bk.export_tables(conn, d)
    (d / "labs.jsonl.gz").write_bytes(b"not gzip")
    res = bk.restore_test(d)
    assert not res["ok"] and any("labs" in p and "checksum" in p for p in res["problems"])
    (d / "events.jsonl.gz").unlink()
    assert any("missing" in p for p in bk.verify_files(d))


def test_export_endpoint_writes_under_helios_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "data").mkdir(parents=True)
    # Keep the fixture overlays in play by copying them into the temp home.
    import os
    import shutil
    fixtures = os.environ["HELIOS_HOME"]
    for n in ("metric_policy.yaml", "source_registry.yaml"):
        shutil.copy(os.path.join(fixtures, n), home / n)
    monkeypatch.setenv("HELIOS_HOME", str(home))
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    raw = {"server": {"ingest_token": TOKEN}, "storage": {"db_path": str(home / "data" / "helios.duckdb")},
           "notifications": {"macos_alerts": False}}
    with TestClient(create_app(Settings(raw=raw))) as c:
        assert c.post("/api/admin/export").status_code == 401
        r = c.post("/api/admin/export", headers=H)
        assert r.status_code == 200
        body = r.json()
        dest = home / "backup" / date.today().isoformat()
        assert body["path"] == str(dest) and (dest / "manifest.json").is_file()
        assert bk.restore_test(dest)["ok"]


def test_reconcile_tombstones_deletes_resurrected_rows_and_journals_their_dates():
    """Checkpoint C point 27: a restored capture replayed with later tombstones
    must not serve the deleted sample again."""
    conn = db.connect_memory()
    for sid, u, d in (("hk:u1", "u1", "2026-06-01"), ("hk:u2", "u2", "2026-06-02")):
        db.execute(conn, "INSERT INTO samples (sample_id, hk_uuid, metric, hk_type, value, unit, start_ts, end_ts, source_name, device_key, sync_path) "
                         "VALUES (?, ?, 'steps', 'HKQuantityTypeIdentifierStepCount', 5, 'count', ?, ?, 'w', 'apple_watch_ultra', 'bridge')",
                   [sid, u, f"{d} 10:00:00", f"{d} 10:05:00"])
    db.execute(conn, "INSERT INTO tombstones (tomb_id, hk_uuid, reason, batch_id, deleted_at) VALUES ('hk:u1', 'u1', 'bridge_delete', 'b9', now()::TIMESTAMP)")
    assert db.fetchall(conn, "SELECT COUNT(*) FROM samples WHERE hk_uuid = 'u1'")[0][0] == 1   # the deleted sample is back: the gap
    out = bk.reconcile_tombstones(conn)
    assert out == {"deleted": 1, "dates_journaled": 1, "live_tombstoned_left": 0, "promoted": 0}
    assert db.fetchall(conn, "SELECT sample_id FROM samples ORDER BY 1") == [("hk:u2",)]
    assert db.fetchall(conn, "SELECT CAST(date AS VARCHAR), reason FROM dirty_dates") == [("2026-06-01", "restore_reconcile")]
    assert bk.reconcile_tombstones(conn) == {"deleted": 0, "dates_journaled": 0, "live_tombstoned_left": 0, "promoted": 0}
