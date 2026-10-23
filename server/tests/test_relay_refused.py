"""Relay: a batch heliosd refuses with HTTP 422 is set aside. It is kept on
disk for review, never deleted before heliosd acks it, never holds up the
other batches or LAST_OK, and the watchdog lists it until it is acked.
Synthetic batches only."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from heliosd.config import Settings
from heliosd.main import create_app
from heliosd.signals import watchdog
from tests.synth import synth_batch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
TOKEN = "test-token-0123456789"
H = {"X-Helios-Token": TOKEN}
REFUSAL = {"ack": False, "error": "batch schema 3 is not known to this heliosd (it reads 1 and 2)"}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


receiver = _load("m1_spool_receiver")
puller = _load("m4_spool_pull")


def _dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    staging = tmp_path / "staging"
    staging.mkdir()
    return staging, tmp_path / "delivered", tmp_path / "refused"


def _stage(staging: Path, bid: str, extra: dict | None = None) -> tuple[str, bytes]:
    name = f"20261023T0100000000{len(list(staging.iterdir())):02d}Z_{bid}.json"
    body = json.dumps({"batch_id": bid, **(extra or {})}).encode()
    (staging / name).write_bytes(body)
    return name, body


def _trail(refused: Path) -> list[dict]:
    return [json.loads(line) for line in (refused / "refusals.jsonl").read_text(encoding="utf-8").splitlines()]


def _batches(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.glob("*.json")) if folder.is_dir() else []


def test_a_refused_batch_is_set_aside_and_the_others_still_flow(tmp_path):
    staging, delivered, refused = _dirs(tmp_path)
    a, _ = _stage(staging, "a")
    b, body_b = _stage(staging, "b", {"schema": 3})
    c, _ = _stage(staging, "c")
    deleted, moved = [], []

    def post(body: bytes):
        return (422, REFUSAL) if json.loads(body).get("schema") == 3 else (200, {"ack": True})

    res = puller.replay(post, lambda n: deleted.append(n) or True, staging, delivered, refused,
                        set_aside_remote=lambda n: moved.append(n) or True)
    assert res == {"replayed": 3, "acked": 2, "refused": 1, "failed": 0, "remote_deleted": 2}
    assert deleted == [a, c] and moved == [b]
    assert _batches(staging) == [] and _batches(delivered) == [a, c]
    assert _batches(refused) == [b] and (refused / b).read_bytes() == body_b
    [ev] = _trail(refused)
    assert ev["event"] == "refused" and ev["name"] == b and ev["status"] == 422
    assert ev["reason"] == REFUSAL["error"] and ev["sha256"] == hashlib.sha256(body_b).hexdigest()
    assert ev["bytes"] == len(body_b) and ev["at"]


def test_the_kept_reason_never_carries_record_contents(tmp_path):
    # FastAPI's own 422 (a body that is not a JSON object) echoes the input.
    staging, delivered, refused = _dirs(tmp_path)
    name = "20261023T010000000001Z_x.json"
    (staging / name).write_bytes(b'[{"value": 73.25}]')
    detail = {"detail": [{"type": "dict_type", "loc": ["body"], "msg": "Input should be a valid dictionary",
                          "input": [{"value": 73.25}]}]}
    res = puller.replay(lambda body: (422, detail), lambda n: True, staging, delivered, refused)
    assert res["refused"] == 1 and _batches(refused) == [name]
    trail = (refused / "refusals.jsonl").read_text(encoding="utf-8")
    assert "dict_type: Input should be a valid dictionary" in trail
    assert "73.25" not in trail


def test_a_batch_refused_again_is_kept_once_and_nothing_is_overwritten(tmp_path):
    staging, delivered, refused = _dirs(tmp_path)
    name, body = _stage(staging, "z", {"schema": 3})

    def refuse(_body):
        return 422, REFUSAL

    puller.replay(refuse, lambda n: True, staging, delivered, refused)
    # The spool host kept its copy (a failed move): the next cycle stages the same bytes again.
    (staging / name).write_bytes(body)
    puller.replay(refuse, lambda n: True, staging, delivered, refused)
    assert _batches(refused) == [name] and (refused / name).read_bytes() == body
    # Different bytes under the same name go beside the kept copy, never over it.
    other = json.dumps({"batch_id": "z", "schema": 4}).encode()
    (staging / name).write_bytes(other)
    puller.replay(refuse, lambda n: True, staging, delivered, refused)
    assert sorted((refused / n).read_bytes() for n in _batches(refused)) == sorted([body, other])
    assert [ev["event"] for ev in _trail(refused)] == ["refused", "refused", "refused"]


def test_other_failures_still_wait_in_staging(tmp_path):
    staging, delivered, refused = _dirs(tmp_path)
    answers = {"a": (401, {"detail": "bad or missing X-Helios-Token"}), "b": (500, {}),
               "c": (503, {"detail": "shutting down"}), "d": (200, {"ack": False}), "e": None}
    names = [_stage(staging, bid)[0] for bid in answers]

    def post(body: bytes):
        answer = answers[json.loads(body)["batch_id"]]
        if answer is None:
            raise ConnectionError("daemon down")
        return answer

    res = puller.replay(post, lambda n: True, staging, delivered, refused, set_aside_remote=lambda n: True)
    assert res == {"replayed": 5, "acked": 0, "refused": 0, "failed": 5, "remote_deleted": 0}
    assert _batches(staging) == sorted(names) and _batches(refused) == []


def test_a_host_copy_that_could_not_move_keeps_the_cycle_unclean(tmp_path):
    staging, delivered, refused = _dirs(tmp_path)
    name, body = _stage(staging, "z", {"schema": 3})
    res = puller.replay(lambda b: (422, REFUSAL), lambda n: True, staging, delivered, refused,
                        set_aside_remote=lambda n: False)
    # The local copy is kept either way; the host still holds the batch in its inbox,
    # so the cycle is not clean (LAST_OK waits) and the next cycle retries the move.
    assert res["refused"] == 1 and res["failed"] == 1
    assert _batches(refused) == [name] and (refused / name).read_bytes() == body


def test_requeue_then_an_ack_clears_it_and_a_refusal_keeps_it(tmp_path):
    staging, delivered, refused = _dirs(tmp_path)
    n1, body1 = _stage(staging, "one", {"schema": 3})
    n2, body2 = _stage(staging, "two", {"schema": 3})
    puller.replay(lambda b: (422, REFUSAL), lambda n: True, staging, delivered, refused)
    assert _batches(refused) == [n1, n2]

    done, missing = puller.requeue(["--all"], refused, staging)
    assert done == [n1, n2] and missing == []
    # Copies go back to staging; the refused copies stay until heliosd acks them.
    assert _batches(staging) == [n1, n2] and _batches(refused) == [n1, n2]
    assert (staging / n1).read_bytes() == body1 and not list(staging.glob("*.part"))

    deleted = []

    def post(body: bytes):     # a fixed heliosd takes "one", still refuses "two"
        return (200, {"ack": True}) if json.loads(body)["batch_id"] == "one" else (422, REFUSAL)

    res = puller.replay(post, lambda n: deleted.append(n) or True, staging, delivered, refused,
                        set_aside_remote=lambda n: True)
    assert res == {"replayed": 2, "acked": 1, "refused": 1, "failed": 0, "remote_deleted": 1}
    assert deleted == [n1]
    assert _batches(refused) == [n2] and (refused / n2).read_bytes() == body2
    assert _batches(delivered) == [n1] and (delivered / n1).read_bytes() == body1
    events = [(ev["event"], ev["name"]) for ev in _trail(refused)]
    assert events == [("refused", n1), ("refused", n2), ("requeued", n1), ("requeued", n2),
                      ("acked", n1), ("refused", n2)]


def test_requeue_names_one_batch_and_reports_unknown_names(tmp_path):
    staging, delivered, refused = _dirs(tmp_path)
    n1, _ = _stage(staging, "one", {"schema": 3})
    n2, _ = _stage(staging, "two", {"schema": 3})
    puller.replay(lambda b: (422, REFUSAL), lambda n: True, staging, delivered, refused)
    done, missing = puller.requeue([n2, "nope.json"], refused, staging)
    assert done == [n2] and missing == ["nope.json"]
    assert _batches(staging) == [n2]


def test_an_acked_batch_with_other_bytes_never_removes_a_refused_copy(tmp_path):
    staging, delivered, refused = _dirs(tmp_path)
    name, body = _stage(staging, "z", {"schema": 3})
    puller.replay(lambda b: (422, REFUSAL), lambda n: True, staging, delivered, refused)
    (staging / name).write_bytes(b'{"batch_id": "z"}')          # same name, other bytes, acked
    puller.replay(lambda b: (200, {"ack": True}), lambda n: True, staging, delivered, refused)
    assert _batches(refused) == [name] and (refused / name).read_bytes() == body


def test_the_commands_list_and_requeue_refused_batches(tmp_path):
    """`refused`, `requeue` and `status` as the owner runs them, against a
    temporary Helios home (never ~/Helios)."""
    import os
    import subprocess
    home = tmp_path / "home"
    staging, refused = home / "relay" / "staging", home / "relay" / "refused"
    staging.mkdir(parents=True)
    name, body = _stage(staging, "z", {"schema": 3})
    puller.replay(lambda b: (422, REFUSAL), lambda n: True, staging, home / "relay" / "delivered", refused)
    env = {**os.environ, "HELIOS_HOME": str(home)}

    def run(*args):
        return subprocess.run([sys.executable, str(TOOLS / "m4_spool_pull.py"), *args],
                              capture_output=True, text=True, env=env, timeout=60)

    listed = run("refused")
    assert listed.returncode == 0 and name in listed.stdout and "HTTP 422" in listed.stdout
    assert "schema 3" in listed.stdout
    assert "refused: 1" in run("status").stdout
    missing = run("requeue", "nope.json")
    assert missing.returncode == 1 and "not in refused/: nope.json" in missing.stdout
    assert run("requeue").returncode == 2
    again = run("requeue", "--all")
    assert again.returncode == 0 and (staging / name).read_bytes() == body
    assert _batches(refused) == [name]
    assert f"requeued {name}" in (home / "logs" / "relay.log").read_text(encoding="utf-8")


def test_the_watchdog_lists_refused_batches(tmp_path):
    staging, delivered, refused = _dirs(tmp_path)
    assert watchdog.check_relay_refused(refused) == []
    _stage(staging, "one", {"schema": 3})
    _stage(staging, "two", {"schema": 3})
    puller.replay(lambda b: (422, REFUSAL), lambda n: True, staging, delivered, refused)
    [row] = watchdog.check_relay_refused(refused)
    assert row["device_key"] == "relay_refused" and row["status"] == "error" and row["count"] == 2
    assert row.get("notify") is not False and row["age_hours"] < 1
    assert "schema 3" in row["fix"] and "requeue --all" in row["fix"]
    # Nothing worse in the report: the hourly loop would notify this row.
    assert watchdog.notifiable([row]) == row


def test_end_to_end_a_real_422_is_set_aside_and_the_freshness_report_shows_it(tmp_path, monkeypatch):
    """A v1 batch and a batch with a schema this heliosd does not read go
    through the spool receiver, then the puller replays both into a real
    heliosd: the first is stored, the second is refused (422), set aside and
    listed by /api/freshness and the MCP tool's /api/tool/freshness."""
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    refused = tmp_path / "relay" / "refused"
    monkeypatch.setattr(watchdog, "relay_refused_dir", lambda: refused)
    raw = {"server": {"ingest_token": TOKEN}, "storage": {"db_path": str(tmp_path / "helios.duckdb")},
           "notifications": {"macos_alerts": False}}
    spool = tmp_path / "spool"
    good = synth_batch(days=2, end_day=datetime(2026, 7, 15).date())
    bad = dict(good, batch_id="unknown-schema", schema=3)
    for batch in (good, bad):
        assert receiver.accept_batch(json.dumps(batch).encode(), spool, 64 << 20, 1 << 30, 0.0)[0] == 200
    with TestClient(create_app(Settings(raw=raw))) as c:
        def post(body: bytes):
            r = c.post("/ingest", content=body, headers={**H, "Content-Type": "application/json"})
            return r.status_code, r.json()

        res = puller.replay(post, lambda n: True, spool / "inbox", tmp_path / "delivered", refused)
        assert res["acked"] == 1 and res["refused"] == 1 and res["failed"] == 0
        [name] = _batches(refused)
        assert name.endswith("_unknown-schema.json")
        assert "schema 3" in _trail(refused)[0]["reason"]
        n = c.post("/api/tool/sql", json={"query": "SELECT COUNT(*) AS n FROM samples"}, headers=H).json()[0]["n"]
        assert n > 50
        tool_rows = [r for r in c.get("/api/tool/freshness", headers=H).json() if r["device_key"] == "relay_refused"]
        assert len(tool_rows) == 1 and tool_rows[0]["count"] == 1
        report = c.get("/api/freshness", headers=H).json()["watchdog"]
        assert any(r["device_key"] == "relay_refused" for r in report)
