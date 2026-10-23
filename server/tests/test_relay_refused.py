"""Relay: a batch heliosd refuses with HTTP 422 is set aside. It is kept on
disk for review, never deleted before heliosd acks it, never holds up the
other batches or LAST_OK, and the watchdog lists it until it is acked.
Synthetic batches only."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
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
                        set_aside_remote=lambda n, d: moved.append(n) or True)
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

    res = puller.replay(post, lambda n: True, staging, delivered, refused, set_aside_remote=lambda n, d: True)
    assert res == {"replayed": 5, "acked": 0, "refused": 0, "failed": 5, "remote_deleted": 0}
    assert _batches(staging) == sorted(names) and _batches(refused) == []


def test_a_host_copy_that_could_not_move_keeps_the_cycle_unclean(tmp_path):
    staging, delivered, refused = _dirs(tmp_path)
    name, body = _stage(staging, "z", {"schema": 3})
    res = puller.replay(lambda b: (422, REFUSAL), lambda n: True, staging, delivered, refused,
                        set_aside_remote=lambda n, d: False)
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
                        set_aside_remote=lambda n, d: True)
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
        tool_rows = [r for r in c.get("/api/tool/freshness", headers=H).json()["watchdog"]
                     if r["device_key"] == "relay_refused"]
        assert len(tool_rows) == 1 and tool_rows[0]["count"] == 1
        report = c.get("/api/freshness", headers=H).json()["watchdog"]
        assert any(r["device_key"] == "relay_refused" for r in report)


# ---------- review fixes (2026-10-23): rulings 2, 3, 4, 7, 9, 11, 12, 14 ----------

def test_an_error_on_one_batch_never_holds_the_others(tmp_path, monkeypatch):
    staging, delivered, refused = _dirs(tmp_path)
    a, _ = _stage(staging, "a", {"schema": 3})
    b, _ = _stage(staging, "b")
    real = puller.set_aside

    def broken(f, *args, **kwargs):
        if f.name == a:
            raise PermissionError("disk says no")
        return real(f, *args, **kwargs)

    monkeypatch.setattr(puller, "set_aside", broken)
    errors = []
    res = puller.replay(lambda body: (422, REFUSAL) if json.loads(body).get("schema") else (200, {"ack": True}),
                        lambda n: True, staging, delivered, refused, errors=errors)
    assert res["acked"] == 1 and res["failed"] == 1 and _batches(delivered) == [b]
    assert _batches(staging) == [a] and errors == [f"{a}: PermissionError"]


def test_a_remote_step_that_hangs_reads_as_failed(monkeypatch):
    def hang(*args, **kwargs):
        raise puller.subprocess.TimeoutExpired(cmd="ssh", timeout=60)
    monkeypatch.setattr(puller.subprocess, "run", hang)
    assert puller.run_remote("user@host", "true") is None


def test_unsafe_names_never_reach_heliosd_or_a_remote_shell(tmp_path):
    staging, delivered, refused = _dirs(tmp_path)
    (staging / "a b;rm -rf ~.json").write_bytes(b'{"batch_id": "x"}')
    calls, errors = [], []
    res = puller.replay(lambda body: calls.append("post") or (422, REFUSAL), lambda n: calls.append("rm") or True,
                        staging, delivered, refused, set_aside_remote=lambda n, d: calls.append("mv") or True,
                        errors=errors)
    assert res["failed"] == 1 and calls == [] and _batches(refused) == []
    assert errors == ["unsafe name skipped: abrm-rf.json"]


def _zsh(home: Path, command: str) -> str:
    import os
    import subprocess
    r = subprocess.run(["/bin/zsh", "-f", "-c", command], capture_output=True, text=True,
                       env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.split()[-1]


@pytest.mark.parametrize("spool", ["HeliosSpool", "Helios Spool/sub"])
def test_the_real_remote_commands_never_overwrite_or_delete_a_different_batch(tmp_path, spool):
    """The exact command strings the puller sends, run by zsh in a temporary
    home standing in for the spool host."""
    import os
    import stat
    import subprocess
    home = tmp_path / "m1"
    inbox, held = home / spool / "inbox", home / spool / "refused"
    inbox.mkdir(parents=True)
    (home / "Helios").mkdir()
    name = "20261023T010000123456Z_b-1.json"
    first, other = b'{"batch_id": "b-1", "schema": 3}', b'{"batch_id": "b-1", "schema": 4}'
    d1, d2 = hashlib.sha256(first).hexdigest(), hashlib.sha256(other).hexdigest()

    (inbox / name).write_bytes(first)
    assert _zsh(home, puller.remote_set_aside_cmd(spool, name, d1)) == "MOVED"
    assert (held / name).read_bytes() == first and not (inbox / name).exists()
    assert stat.S_IMODE(os.stat(held).st_mode) == 0o700
    assert _zsh(home, puller.remote_set_aside_cmd(spool, name, d1)) == "ABSENT"
    (inbox / name).write_bytes(first)                     # staged again after a failed move
    assert _zsh(home, puller.remote_set_aside_cmd(spool, name, d1)) == "SAME"
    assert not (inbox / name).exists() and (held / name).read_bytes() == first
    (inbox / name).write_bytes(other)                     # other bytes, same name: kept beside it
    assert _zsh(home, puller.remote_set_aside_cmd(spool, name, d2)) == "KEPT"
    alt = puller._alt_name(name, d2)
    assert (held / name).read_bytes() == first and (held / alt).read_bytes() == other

    # A release removes a refused copy only when its bytes are the acked ones.
    assert _zsh(home, puller.remote_release_cmd(spool, name, d2)) == "KEPT"
    assert (held / name).exists()
    assert _zsh(home, puller.remote_release_cmd(spool, name, d1)) == "REMOVED"
    assert not (held / name).exists() and (held / alt).exists()
    assert _zsh(home, puller.remote_release_cmd(spool, name, d1)) == "ABSENT"

    # The inbox delete touches the inbox copy only.
    (inbox / name).write_bytes(first)
    assert _zsh(home, puller.remote_delete_cmd(spool, name)) == "DELETED"
    assert not (inbox / name).exists() and (held / alt).exists()
    # Both names taken by other bytes: nothing moves, nothing is printed (a failure).
    third = b'{"batch_id": "b-1", "schema": 5}'
    (held / alt).write_bytes(b'{"something": "else"}')
    (inbox / name).write_bytes(other)
    (held / name).write_bytes(third)
    r = subprocess.run(["/bin/zsh", "-f", "-c", puller.remote_set_aside_cmd(spool, name, d2)], capture_output=True,
                       text=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}, timeout=30)
    assert r.returncode != 0 and r.stdout.split() == []
    assert (inbox / name).read_bytes() == other and (held / alt).read_bytes() == b'{"something": "else"}'
    (held / alt).write_bytes(other)                       # the same bytes under the hash name: SAME
    assert _zsh(home, puller.remote_set_aside_cmd(spool, name, d2)) == "SAME" and not (inbox / name).exists()
    # A copy that cannot be hashed is never reported KEPT or REMOVED.
    (held / name).chmod(0)
    try:
        r = subprocess.run(["/bin/zsh", "-f", "-c", puller.remote_release_cmd(spool, name, d1)], capture_output=True,
                           text=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}, timeout=30)
        assert r.returncode != 0 and r.stdout.split() == []
    finally:
        (held / name).chmod(0o600)
    # A spool name with a space never reaches a neighbouring folder.
    assert stat.S_IMODE(os.stat(home / "Helios").st_mode) != 0o700 or spool == "HeliosSpool"
    assert sorted(p.name for p in home.iterdir()) == sorted({"Helios", spool.split("/")[0]})
    assert (inbox / name).exists() is False


def test_a_refused_batch_is_retried_once_after_a_heliosd_restart(tmp_path):
    staging, delivered, refused = _dirs(tmp_path)
    name, body = _stage(staging, "z", {"schema": 3})
    puller.replay(lambda b: (422, REFUSAL), lambda n: True, staging, delivered, refused, daemon_started_at="S1")
    assert puller.retry_after_restart("S1", refused, staging) == []
    assert puller.retry_after_restart(None, refused, staging) == []
    assert puller.retry_after_restart("S2", refused, staging) == [name]
    assert (staging / name).read_bytes() == body and _batches(refused) == [name]
    # Still staged (heliosd was down for the post): not copied twice.
    assert puller.retry_after_restart("S2", refused, staging) == []
    # Refused again by the new process: no more retries until the next restart.
    puller.replay(lambda b: (422, REFUSAL), lambda n: True, staging, delivered, refused, daemon_started_at="S2")
    assert puller.retry_after_restart("S2", refused, staging) == []
    assert puller.retry_after_restart("S3", refused, staging) == [name]
    # A fixed heliosd takes it: it leaves refused/, the host copy is released with its hash.
    released = []
    res = puller.replay(lambda b: (200, {"ack": True}), lambda n: True, staging, delivered, refused,
                        release_remote=lambda n, d: released.append((n, d)) or True)
    assert res["acked"] == 1 and _batches(refused) == [] and _batches(delivered) == [name]
    assert released == [(name, hashlib.sha256(body).hexdigest())]
    whys = [ev.get("why") for ev in _trail(refused) if ev["event"] == "requeued"]
    assert whys == ["heliosd restarted", "heliosd restarted"]


def test_an_acked_set_aside_batch_whose_host_copy_stays_is_finished_next_cycle(tmp_path):
    staging, delivered, refused = _dirs(tmp_path)
    name, body = _stage(staging, "z", {"schema": 3})
    puller.replay(lambda b: (422, REFUSAL), lambda n: True, staging, delivered, refused)
    puller.requeue(["--all"], refused, staging)
    res = puller.replay(lambda b: (200, {"ack": True}), lambda n: True, staging, delivered, refused,
                        release_remote=lambda n, d: False)
    # In the store, but the host's refused copy is still there: nothing moves on this side.
    assert res["acked"] == 1 and res["failed"] == 1
    assert _batches(refused) == [name] and _batches(staging) == [name] and _batches(delivered) == []
    res = puller.replay(lambda b: (200, {"ack": True}), lambda n: True, staging, delivered, refused,
                        release_remote=lambda n, d: True)
    assert res == {"replayed": 1, "acked": 1, "refused": 0, "failed": 0, "remote_deleted": 1}
    assert _batches(refused) == [] and _batches(staging) == [] and _batches(delivered) == [name]


def test_requeue_takes_paths_and_all_anywhere(tmp_path):
    staging, delivered, refused = _dirs(tmp_path)
    n1, _ = _stage(staging, "one", {"schema": 3})
    n2, _ = _stage(staging, "two", {"schema": 3})
    puller.replay(lambda b: (422, REFUSAL), lambda n: True, staging, delivered, refused)
    assert puller.requeue([str(refused / n2)], refused, staging) == ([n2], [])
    assert puller.requeue(["x.json", "--all"], refused, staging) == ([n1, n2], [])


def test_the_trail_line_is_written_before_the_move(tmp_path, monkeypatch):
    staging, delivered, refused = _dirs(tmp_path)
    name, _ = _stage(staging, "z", {"schema": 3})

    def no_move(*a, **k):
        raise OSError("crash between the trail and the move")
    monkeypatch.setattr(puller.os, "replace", no_move)
    res = puller.replay(lambda b: (422, REFUSAL), lambda n: True, staging, delivered, refused)
    assert res["failed"] == 1 and _batches(staging) == [name] and _batches(refused) == []
    assert [ev["event"] for ev in _trail(refused)] == ["refused"]
    # No file, so the watchdog says nothing is waiting.
    assert watchdog.check_relay_refused(refused) == []


def test_the_watchdog_reason_belongs_to_a_batch_still_held(tmp_path):
    staging, delivered, refused = _dirs(tmp_path)
    a, _ = _stage(staging, "a", {"schema": 3})
    puller.replay(lambda b: (422, {"ack": False, "error": "reason a"}), lambda n: True, staging, delivered, refused)
    b, _ = _stage(staging, "b", {"schema": 3})
    puller.replay(lambda b: (422, {"ack": False, "error": "reason b"}), lambda n: True, staging, delivered, refused)
    puller.requeue([b], refused, staging)
    puller.replay(lambda b: (200, {"ack": True}), lambda n: True, staging, delivered, refused)
    [row] = watchdog.check_relay_refused(refused)
    assert row["count"] == 1 and "reason a" in row["fix"] and "reason b" not in row["fix"]
    assert row["message"] == "1 relay batch refused by heliosd, kept for review"
    later = datetime.now(timezone.utc) + timedelta(hours=5)
    assert watchdog.check_relay_refused(refused, now=later)[0]["age_hours"] >= 4.9


def test_a_whoop_error_row_never_masks_the_relay_row():
    whoop = {"metric": "*", "device_key": "whoop_cloud", "status": "error", "fix": "x"}
    relay = {"metric": "*", "device_key": "relay_refused", "status": "error", "count": 2,
             "message": "2 relay batches refused by heliosd, kept for review", "fix": "y"}
    info = {"metric": "*", "device_key": "zest_events", "status": "stale", "notify": False, "fix": "z"}
    wk, rk = ("whoop_cloud", "*", "error"), ("relay_refused", "*", "error")
    now = datetime(2026, 10, 23, 9, 0)
    report = [whoop, info, relay]
    row, keys, n = watchdog.pick_notification(report, {}, now)
    assert row is whoop and keys == [wk, rk] and n == 2
    assert watchdog.notification_text(row, n) == "whoop_cloud * is error, and 1 more"
    # Both cooldowns started together: nothing again within six hours.
    assert watchdog.pick_notification(report, {wk: now, rk: now}, now + timedelta(hours=5)) is None
    assert watchdog.pick_notification(report, {wk: now, rk: now}, now + timedelta(hours=6))[0] is whoop
    # Refused batches that appear while the Whoop row is in its cooldown post at the next tick.
    row, keys, n = watchdog.pick_notification(report, {wk: now}, now + timedelta(hours=1))
    assert row is relay and keys == [rk] and n == 1
    assert watchdog.notification_text(row, n) == "2 relay batches refused by heliosd, kept for review"


def test_lasting_problems_post_at_most_once_per_cooldown():
    rows = [{"metric": f"m{i}", "device_key": "d", "status": "stale", "fix": ""} for i in range(7)]
    last, posts, t0 = {}, [], datetime(2026, 10, 23, 0, 0)
    for h in range(12):
        pick = watchdog.pick_notification(rows, last, t0 + timedelta(hours=h))
        if pick:
            posts.append((h, pick[2]))
            for k in pick[1]:
                last[k] = t0 + timedelta(hours=h)
    assert posts == [(0, 7), (6, 7)]


def test_the_watchdog_check_passes_its_own_clock_to_the_relay_row(tmp_path, monkeypatch):
    import os
    from heliosd.store import db as store_db
    from heliosd.trust.policy import MetricPolicy
    refused = tmp_path / "refused"
    refused.mkdir()
    held = refused / "20261023T010000000000Z_z.json"
    held.write_bytes(b"{}")
    t = datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc).timestamp()
    os.utime(held, (t, t))
    monkeypatch.setattr(watchdog, "relay_refused_dir", lambda: refused)
    policy = MetricPolicy(default_tz="Asia/Dubai")
    wall = datetime(2026, 10, 23, 12, 0)                      # the reporting zone's wall clock, naive
    expected = round((wall.replace(tzinfo=policy.zone) - datetime.fromtimestamp(t, timezone.utc)).total_seconds() / 3600, 1)
    rows = [r for r in watchdog.check(store_db.connect_memory(), policy, now=wall) if r["device_key"] == "relay_refused"]
    assert len(rows) == 1 and rows[0]["age_hours"] == expected


def test_health_reports_when_heliosd_started(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    raw = {"server": {"ingest_token": TOKEN}, "storage": {"db_path": str(tmp_path / "helios.duckdb")},
           "notifications": {"macos_alerts": False}}
    with TestClient(create_app(Settings(raw=raw))) as c:
        started = c.get("/api/health").json()["started_at"]
    assert datetime.fromisoformat(started).tzinfo is not None


def test_requeue_skips_a_file_it_cannot_read_and_says_so(tmp_path):
    staging, delivered, refused = _dirs(tmp_path)
    n1, _ = _stage(staging, "one", {"schema": 3})
    n2, _ = _stage(staging, "two", {"schema": 3})
    puller.replay(lambda b: (422, REFUSAL), lambda n: True, staging, delivered, refused)
    (refused / n1).chmod(0)
    try:
        errors = []
        done, missing = puller.requeue(["--all"], refused, staging, errors=errors)
        assert done == [n2] and missing == [] and errors == [f"{n1}: PermissionError"]
    finally:
        (refused / n1).chmod(0o600)


# ---------- the whole `run` command, faked at its edges (round 2, new 8) ----------

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class _FakeHTTP:
    """Stands in for httpx.Client: /api/health gives started_at, /ingest the
    answer listed for the batch id."""

    def __init__(self, answers, started="S1"):
        self.answers, self.started, self.posted = answers, started, []

    def __call__(self, *args, **kwargs):
        return self

    def get(self, path):
        return _Resp(200, {"ok": True, "started_at": self.started})

    def post(self, path, content=None):
        bid = json.loads(content)["batch_id"]
        self.posted.append(bid)
        return _Resp(*self.answers[bid])


def _run_env(tmp_path, monkeypatch, answers, remote_spool="HeliosSpool", started="S1"):
    import httpx
    home = tmp_path / "home"
    home.mkdir()
    (home / "helios.toml").write_text(
        f'[server]\ningest_token = "{TOKEN}"\n\n[relay]\nremote = "user@spool-host"\nremote_spool = "{remote_spool}"\n',
        encoding="utf-8")
    monkeypatch.setenv("HELIOS_HOME", str(home))
    spec = importlib.util.spec_from_file_location("m4_spool_pull_run", TOOLS / "m4_spool_pull.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    http = _FakeHTTP(answers, started)
    monkeypatch.setattr(httpx, "Client", http)
    remote_calls = []

    def fake_remote(remote, command):
        remote_calls.append(command)
        for word in ("DELETED", "MOVED", "REMOVED"):
            if f"echo {word}" in command:
                return word
        return None

    monkeypatch.setattr(mod, "run_remote", fake_remote)
    return mod, http, remote_calls


def _stage_into(mod, bid, extra=None, at=0):
    mod.STAGING.mkdir(parents=True, exist_ok=True)
    name = f"20261023T0200000000{at:02d}Z_{bid}.json"
    (mod.STAGING / name).write_bytes(json.dumps({"batch_id": bid, **(extra or {})}).encode())
    return name


def test_run_sets_aside_a_refusal_keeps_last_ok_and_retries_after_a_restart(tmp_path, monkeypatch):
    mod, http, remote_calls = _run_env(tmp_path, monkeypatch,
                                       {"a": (200, {"ack": True}), "b": (422, REFUSAL), "r": (200, {"ack": True})})
    # A batch set aside by an earlier heliosd process (started S0).
    old = _stage_into(mod, "r", {"schema": 3}, at=0)
    mod.replay(lambda b: (422, REFUSAL), lambda n: True, mod.STAGING, mod.DELIVERED, mod.REFUSED, daemon_started_at="S0")
    staged = []

    def fake_fetch(remote, spool):
        staged.append(_stage_into(mod, "a", at=1))
        staged.append(_stage_into(mod, "b", {"schema": 3}, at=2))
        return 2

    monkeypatch.setattr(mod, "fetch", fake_fetch)
    assert mod.cmd_run() == 0
    assert http.posted == ["r", "a", "b"]
    assert mod.LAST_OK.is_file()
    assert _batches(mod.REFUSED) == [staged[1]] and sorted(_batches(mod.DELIVERED)) == sorted([old, staged[0]])
    log = mod.LOG.read_text(encoding="utf-8")
    assert f"requeued {old}: heliosd restarted since it was refused" in log
    assert f"refused {staged[1]}: heliosd answered 422" in log and "spool host copy: MOVED" in log
    assert "refused=1 failed=0" in log
    assert any("echo REMOVED" in c for c in remote_calls)        # the old one's host copy released


def test_run_after_a_failed_fetch_replays_what_is_staged_without_remote_steps(tmp_path, monkeypatch):
    mod, http, remote_calls = _run_env(tmp_path, monkeypatch, {"c": (200, {"ack": True})})
    name = _stage_into(mod, "c")

    def broken_fetch(remote, spool):
        raise RuntimeError("rsync rc=255: unreachable")

    monkeypatch.setattr(mod, "fetch", broken_fetch)
    assert mod.cmd_run() == 1
    assert http.posted == ["c"] and _batches(mod.DELIVERED) == [name]
    assert remote_calls == [] and not mod.LAST_OK.exists()
    assert "remote steps skipped" in mod.LOG.read_text(encoding="utf-8")


def test_run_carries_on_when_the_restart_retry_fails(tmp_path, monkeypatch):
    mod, http, remote_calls = _run_env(tmp_path, monkeypatch, {"a": (200, {"ack": True})})

    def broken_retry(*args, **kwargs):
        raise PermissionError("refused/ unreadable")

    monkeypatch.setattr(mod, "retry_after_restart", broken_retry)
    monkeypatch.setattr(mod, "fetch", lambda remote, spool: [_stage_into(mod, "a")] and 1)
    assert mod.cmd_run() == 3
    assert http.posted == ["a"] and not mod.LAST_OK.exists()
    assert "retry error: retry after restart: PermissionError" in mod.LOG.read_text(encoding="utf-8")


def test_run_refuses_an_unsafe_remote_spool(tmp_path, monkeypatch):
    mod, http, remote_calls = _run_env(tmp_path, monkeypatch, {}, remote_spool="../elsewhere")
    monkeypatch.setattr(mod, "fetch", lambda remote, spool: pytest.fail("fetched with a bad spool path"))
    assert mod.cmd_run() == 2 and http.posted == [] and remote_calls == []


def test_run_stops_while_another_run_holds_the_lock(tmp_path, monkeypatch):
    import fcntl
    mod, http, remote_calls = _run_env(tmp_path, monkeypatch, {})
    mod.RELAY.mkdir(parents=True, exist_ok=True)
    with open(mod.LOCK, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(mod, "fetch", lambda remote, spool: pytest.fail("fetched while locked"))
        assert mod.cmd_run() == 0
    assert "another run is in progress" in mod.LOG.read_text(encoding="utf-8")
