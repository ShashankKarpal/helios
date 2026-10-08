"""Wave 1 integration follow-ups (fix program 2026-10-08), found while merging the
worker branches: the action status route on an unknown id (Codex A point 9 on the
A2 design) and the lab upload warning that named the owner's file (now visible
because A13 turned logging on)."""
import logging

from fastapi.testclient import TestClient

from heliosd import main
from heliosd.config import Settings
from heliosd.store import db

TOKEN = "test-token-0123456789"
H = {"X-Helios-Token": TOKEN}


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    s = Settings(raw={"server": {"ingest_token": TOKEN},
                      "owner": {"timezone": "Asia/Dubai"},
                      "storage": {"db_path": str(tmp_path / "helios.duckdb")},
                      "notifications": {"macos_alerts": False}})
    return TestClient(main.create_app(s), client=("127.0.0.1", 50000))


def test_action_status_on_an_unknown_id_is_404_and_a_known_one_is_updated(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        conn = c.app.state.conn
        r = c.post("/api/actions/2026-10-08:sleep:no_such_rule/adopted", headers=H)
        assert r.status_code == 404
        db.execute(conn, "INSERT INTO actions (action_id, date, text, category) VALUES (?, ?, ?, ?)",
                   ["2026-10-08:sleep:sleep_short", "2026-10-08", "Synthetic sleep action", "sleep"])
        r = c.post("/api/actions/2026-10-08:sleep:sleep_short/dismissed", headers=H)
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert db.fetchall(conn, "SELECT status FROM actions") == [("dismissed",)]
        assert c.post("/api/actions/2026-10-08:sleep:sleep_short/bogus", headers=H).status_code == 400


def test_lab_upload_warning_logs_the_suffix_never_the_file_name(tmp_path, monkeypatch, caplog):
    with _client(tmp_path, monkeypatch) as c:
        caplog.set_level(logging.WARNING, logger="heliosd")
        r = c.post("/api/labs/parse", headers=H,
                   files={"file": ("Owner_Name_Bloodwork_2026.pdf", b"%PDF-1.4 not really a pdf", "application/pdf")})
        assert r.status_code == 422
        text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "could not be parsed" in text and ".pdf" in text
        assert "Owner_Name" not in text and "Bloodwork" not in text
