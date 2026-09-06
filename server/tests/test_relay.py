"""Overnight relay: the spool receiver's contract (same ack the Bridge expects,
durable before ack, refusals that keep the Bridge's outbox intact) and the
puller's replay (idempotent, deletes remote only after heliosd acks)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heliosd.config import Settings
from heliosd.main import create_app
from heliosd.store import db
from tests.synth import synth_batch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
TOKEN = "test-token-0123456789"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


receiver = _load("m1_spool_receiver")
puller = _load("m4_spool_pull")


def test_receiver_persists_then_acks_with_the_bridge_shape(tmp_path):
    spool = tmp_path / "spool"
    body = json.dumps({"batch_id": "b-1", "samples": [{"uuid": "u1"}]}).encode()
    status, reply = receiver.accept_batch(body, spool, 1 << 20, 1 << 30, 0.0,
                                          now=datetime(2026, 9, 6, 1, 2, 3, tzinfo=timezone.utc))
    assert status == 200 and reply["ack"] is True and reply["batch_id"] == "b-1"
    files = list((spool / "inbox").glob("*.json"))
    assert len(files) == 1 and files[0].read_bytes() == body
    assert files[0].name.startswith("20260906T010203") and files[0].name.endswith("_b-1.json")
    assert not list((spool / "inbox").glob("*.part"))


def test_receiver_refusals_never_ack(tmp_path):
    spool = tmp_path / "spool"
    ok = json.dumps({"batch_id": "x", "samples": []}).encode()
    assert receiver.accept_batch(b"x" * 100, spool, 50, 1 << 30, 0.0)[0] == 413
    assert receiver.accept_batch(b"not json", spool, 1 << 20, 1 << 30, 0.0)[0] == 400
    assert receiver.accept_batch(b"[1,2]", spool, 1 << 20, 1 << 30, 0.0)[0] == 400
    receiver.accept_batch(ok, spool, 1 << 20, 1 << 30, 0.0)
    status, reply = receiver.accept_batch(ok, spool, 1 << 20, 1, 0.0)
    assert status == 507 and reply["ack"] is False and "spool" in reply["error"]
    status, reply = receiver.accept_batch(ok, spool, 1 << 20, 1 << 30, 10 ** 6)
    assert status == 507 and "disk" in reply["error"]
    assert len(list((spool / "inbox").glob("*.json"))) == 1


def test_batch_id_is_sanitised_for_the_filename(tmp_path):
    spool = tmp_path / "spool"
    body = json.dumps({"batch_id": "../../etc/passwd; rm -rf", "samples": []}).encode()
    status, reply = receiver.accept_batch(body, spool, 1 << 20, 1 << 30, 0.0)
    assert status == 200
    name = list((spool / "inbox").glob("*.json"))[0].name
    assert "/" not in name and ";" not in name and " " not in name


def test_replay_acks_delete_and_keep_failures(tmp_path):
    staging, delivered = tmp_path / "staging", tmp_path / "delivered"
    staging.mkdir()
    (staging / "20260906T010000_ok.json").write_bytes(b'{"batch_id": "ok"}')
    (staging / "20260906T010001_bad.json").write_bytes(b'{"batch_id": "bad"}')
    (staging / "20260906T010002_noack.json").write_bytes(b'{"batch_id": "noack"}')
    deleted = []

    def post(body: bytes):
        bid = json.loads(body)["batch_id"]
        if bid == "ok":
            return 200, {"ack": True}
        if bid == "noack":
            return 200, {"ack": False}
        raise ConnectionError("daemon down")

    res = puller.replay(post, lambda n: deleted.append(n) or True, staging, delivered)
    assert res == {"replayed": 3, "acked": 1, "failed": 2, "remote_deleted": 1}
    assert deleted == ["20260906T010000_ok.json"]
    assert sorted(f.name for f in staging.glob("*.json")) == ["20260906T010001_bad.json", "20260906T010002_noack.json"]
    assert [f.name for f in delivered.glob("*.json")] == ["20260906T010000_ok.json"]


def test_end_to_end_spool_then_replay_into_heliosd_is_idempotent(tmp_path, monkeypatch):
    """Bridge-shaped batch goes through the receiver's accept path, then the
    puller replays it into a real heliosd twice; the second pass adds no rows."""
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    raw = {"server": {"ingest_token": TOKEN}, "storage": {"db_path": str(tmp_path / "helios.duckdb")},
           "notifications": {"macos_alerts": False}}
    spool = tmp_path / "spool"
    batch = synth_batch(days=2, end_day=datetime(2026, 7, 15).date())
    status, _ = receiver.accept_batch(json.dumps(batch).encode(), spool, 64 << 20, 1 << 30, 0.0)
    assert status == 200
    with TestClient(create_app(Settings(raw=raw))) as c:
        def post(body: bytes):
            r = c.post("/ingest", content=body, headers={"X-Helios-Token": TOKEN, "Content-Type": "application/json"})
            return r.status_code, r.json()

        staging = spool / "inbox"
        delivered = tmp_path / "delivered"
        res = puller.replay(post, lambda n: True, staging, delivered)
        assert res["acked"] == 1 and res["failed"] == 0
        n1 = c.post("/api/tool/sql", json={"query": "SELECT COUNT(*) AS n FROM samples"},
                    headers={"X-Helios-Token": TOKEN}).json()[0]["n"]
        assert n1 > 50
        # Replay the delivered copy again (crash-between-ack-and-delete case).
        for f in delivered.glob("*.json"):
            f.rename(staging / f.name)
        res2 = puller.replay(post, lambda n: True, staging, delivered)
        assert res2["acked"] == 1
        n2 = c.post("/api/tool/sql", json={"query": "SELECT COUNT(*) AS n FROM samples"},
                    headers={"X-Helios-Token": TOKEN}).json()[0]["n"]
        assert n2 == n1
        batches = c.post("/api/tool/sql", json={"query": "SELECT COUNT(*) AS n FROM sync_log"},
                         headers={"X-Helios-Token": TOKEN}).json()[0]["n"]
        assert batches == 1  # INSERT OR REPLACE on batch_id


@pytest.mark.skipif(sys.version_info < (3, 9), reason="receiver targets 3.9+")
def test_receiver_source_stays_stdlib_only():
    src = (TOOLS / "m1_spool_receiver.py").read_text(encoding="utf-8")
    for banned in ("import httpx", "import duckdb", "import fastapi", "from heliosd", "import yaml", "tomllib"):
        assert banned not in src, banned
