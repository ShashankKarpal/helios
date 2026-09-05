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
