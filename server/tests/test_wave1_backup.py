"""Wave 1 (fix program 2026-10-08) item A26, audit P18: the nightly export carries
every table a restore needs, the off-Mac copy never mirrors a local deletion,
the remote copy is pruned by age on its own, and a restore drill runs weekly
with one log line. Every test here failed on the code before A26."""
from __future__ import annotations

import gzip
import importlib.util
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from heliosd import backup as bk
from heliosd.store import db

TOOL = Path(__file__).resolve().parents[1] / "tools" / "helios_backup.py"


@pytest.fixture
def tool(tmp_path, monkeypatch):
    """The CLI module, with every path it writes pointed into tmp_path."""
    spec = importlib.util.spec_from_file_location("helios_backup_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "BACKUP_DIR", tmp_path / "backup")
    monkeypatch.setattr(mod, "LOG", tmp_path / "logs" / "backup.log")
    monkeypatch.setattr(mod, "LAST_OK", tmp_path / "backup" / "LAST_OK")
    (tmp_path / "backup").mkdir()
    return mod


def _seed_all(conn):
    db.execute(conn, "INSERT INTO events (event_id, kind, ts, payload, source) VALUES ('e1', 'note', ?, '{}', 'shortcut')",
               [datetime(2026, 10, 7, 9)])
    db.execute(conn, "INSERT INTO hk_reread (hk_uuid, hk_type, metric, value, unit, start_utc, end_utc) "
                     "VALUES ('u-1', 'HKQuantityTypeIdentifierStepCount', 'steps', 12, 'count', ?, ?)",
               [datetime(2026, 10, 1, 5), datetime(2026, 10, 1, 5, 5)])
    db.execute(conn, "INSERT INTO hk_reread_variants (hk_uuid, seq, hk_type, metric, value, unit) "
                     "VALUES ('u-1', 1, 'HKQuantityTypeIdentifierStepCount', 'steps', 13, 'count')")
    db.execute(conn, "INSERT INTO metric_registry (metric, hk, unit, agg, daily, day_basis, direction, trust) "
                     "VALUES ('steps', 'HKQuantityTypeIdentifierStepCount', 'count', 'sum', true, 'calendar', 'up', 'validated')")
    db.execute(conn, "INSERT INTO migrations (name, applied_at, code_commit, input_fingerprint, summary) "
                     "VALUES ('phase1b_history_rebase_v1', ?, 'abc1234', 'fp', '{\"archive_manifest_sha256\": \"d1\"}')",
               [datetime(2026, 10, 7, 16)])


def _export(tmp_path, name="2026-10-08"):
    conn = db.connect_memory()
    _seed_all(conn)
    dest = tmp_path / "backup" / name
    m = bk.export_tables(conn, dest)
    conn.close()
    return dest, m


# ---------------------------------------------------------------- A26a: the export set

def test_export_set_carries_reread_registry_and_migrations(tmp_path):
    for t in ("hk_reread", "hk_reread_variants", "metric_registry", "migrations"):
        assert t in bk.IRREPLACEABLE_TABLES, t
    dest, m = _export(tmp_path)
    assert m["tables"]["hk_reread"]["rows"] == 1
    assert m["tables"]["hk_reread_variants"]["rows"] == 1
    assert m["tables"]["metric_registry"]["rows"] == 1
    assert m["tables"]["migrations"]["rows"] == 1
    res = bk.restore_test(dest)
    assert res["ok"], res["problems"]
    assert res["tables"]["metric_registry"] == {"expected": 1, "loaded": 1, "restored": 1}
    conn2 = db.connect_memory()
    bk.load_tables(conn2, dest)
    assert db.fetchall(conn2, "SELECT agg, day_basis FROM metric_registry WHERE metric = 'steps'") == [("sum", "calendar")]
    assert db.fetchall(conn2, "SELECT seq, value FROM hk_reread_variants") == [(1, 13.0)]
    assert json.loads(db.fetchall(conn2, "SELECT summary FROM migrations")[0][0])["archive_manifest_sha256"] == "d1"


def test_archive_dir_from_manifest_picks_the_first_place_that_exists(tmp_path):
    here = tmp_path / "ssd-archive"
    here.mkdir()
    m = {"migrations": {"phase1b_history_rebase_v1": {
        "archive_manifest_sha256": "d1",
        "archive_places": [str(tmp_path / "missing-local"), str(here)]}}}
    assert bk.archive_dir_from_manifest(m) == here
    assert bk.archive_dir_from_manifest({"migrations": {}}) is None
    assert bk.archive_dir_from_manifest({}) is None


# ---------------------------------------------------------------- A26b: the off-Mac copy

def test_rsync_of_the_exports_never_carries_delete(tool):
    cmd = tool.rsync_exports_cmd(Path("/x/backup"), "user@host", "Backups/helios")
    assert "--delete" not in cmd and not any(a.startswith("--delete") for a in cmd)
    assert cmd[0] == "/usr/bin/rsync" and cmd[1] == "-a"
    assert cmd[-2:] == ["/x/backup/", "user@host:Backups/helios/exports/"]
    for p in tool.NEVER_SHIP:
        assert f"--exclude={p}" in cmd


def test_remote_prune_plan_is_by_age_and_only_dated_export_names(tool):
    listing = ["2026-06-01", "2026-07-09", "2026-07-10", "2026-10-08", "LAST_OK", "overlays", "..",
               "2026-13-40", "x; rm -rf ~", "2026-07-01.bak"]
    gone = tool.remote_prune_plan(listing, keep_days=90, today=date(2026, 10, 8))
    assert gone == ["2026-06-01", "2026-07-09"]          # cutoff 2026-07-10 is kept
    cmd = tool.remote_prune_cmd("Backups/helios", gone)
    assert cmd == "cd ~/Backups/helios/exports && rm -rf -- 2026-06-01 2026-07-09"
    assert tool.remote_prune_plan(listing, keep_days=3650, today=date(2026, 10, 8)) == []
    assert tool.DEFAULTS["remote_keep_days"] == 90 and tool.DEFAULTS["keep_days"] == 30


def test_sync_syncs_without_delete_then_prunes_the_remote_by_age(tool, tmp_path):
    dest, m = _export(tmp_path)
    calls: list[list[str]] = []
    want = [spec["sha256"] for spec in m["tables"].values()]

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        tail = cmd[-1]
        if cmd[0] == "ssh" and tail.startswith("ls -1 "):
            return R(0, "2026-01-01\n2026-07-09\n2026-10-08\nLAST_OK\n")
        if cmd[0] == "ssh" and "shasum" in tail:
            return R(0, "\n".join(want) + "\n")
        return R(0)

    cfg = {"remote": "user@host", "remote_dir": "Backups/helios", "remote_keep_days": 90, "include_overlays": False}
    soft = tool._sync(cfg, dest, run=fake_run, today=date(2026, 10, 8))
    assert soft == []
    rsyncs = [c for c in calls if c[0] == "/usr/bin/rsync"]
    assert len(rsyncs) == 1 and "--delete" not in rsyncs[0]
    prunes = [c for c in calls if c[0] == "ssh" and "rm -rf --" in c[-1]]
    assert len(prunes) == 1
    assert prunes[0][-1] == "cd ~/Backups/helios/exports && rm -rf -- 2026-01-01 2026-07-09"
    # Order: mkdir, rsync, ls, prune, shasum.
    kinds = ["mkdir" if c[0] == "ssh" and "mkdir" in c[-1] else "rsync" if c[0] == "/usr/bin/rsync"
             else "ls" if c[-1].startswith("ls -1 ") else "prune" if "rm -rf --" in c[-1] else "shasum" for c in calls]
    assert kinds == ["mkdir", "rsync", "ls", "prune", "shasum"]
    log = (tmp_path / "logs" / "backup.log").read_text()
    assert "remote pruned 2 export dirs older than 90 days" in log and "remote ok" in log


def test_sync_with_nothing_old_on_the_remote_sends_no_prune(tool, tmp_path):
    dest, m = _export(tmp_path)
    want = [spec["sha256"] for spec in m["tables"].values()]
    calls = []

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[-1].startswith("ls -1 "):
            return R(0, "2026-10-08\n")
        if "shasum" in cmd[-1]:
            return R(0, "\n".join(want) + "\n")
        return R(0)

    tool._sync({"remote": "u@h", "remote_dir": "Backups/helios", "include_overlays": False}, dest, run=fake_run,
               today=date(2026, 10, 8))
    assert not any("rm -rf --" in c[-1] for c in calls)


def test_remote_prune_failure_is_logged_and_soft_not_an_abort(tool, tmp_path):
    dest, m = _export(tmp_path)
    want = [spec["sha256"] for spec in m["tables"].values()]

    class R:
        def __init__(self, rc=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def fake_run(cmd, **kw):
        if cmd[-1].startswith("ls -1 "):
            return R(0, "2020-01-01\n")
        if "rm -rf --" in cmd[-1]:
            return R(1, "", "permission denied")
        if "shasum" in cmd[-1]:
            return R(0, "\n".join(want) + "\n")
        return R(0)

    soft = tool._sync({"remote": "u@h", "remote_dir": "Backups/helios", "include_overlays": False}, dest, run=fake_run,
                      today=date(2026, 10, 8))
    assert soft and "remote prune" in soft[0]
    log = (tmp_path / "logs" / "backup.log").read_text()
    assert "remote prune FAILED" in log and "remote ok" in log


# ---------------------------------------------------------------- A26c: the weekly restore drill

def test_restore_drill_day_is_the_configured_weekday(tool):
    assert tool.DEFAULTS["restore_test_weekday"] == 0                       # Monday
    assert tool.restore_drill_due(date(2026, 10, 12), 0)                    # a Monday
    assert not tool.restore_drill_due(date(2026, 10, 13), 0)                # Tuesday
    assert tool.restore_drill_due(date(2026, 10, 13), 1)


def _run_with(tool, tmp_path, monkeypatch, now, remote=""):
    dest, _ = _export(tmp_path)
    monkeypatch.setattr(tool, "_export_via_daemon", lambda: dest)
    monkeypatch.setattr(tool, "_cfg", lambda: dict(tool.DEFAULTS, remote=remote))
    return dest, tool.cmd_run(sync=True, now=now)


def test_run_logs_restore_test_ok_on_the_drill_day_and_nothing_on_other_days(tool, tmp_path, monkeypatch):
    dest, rc = _run_with(tool, tmp_path, monkeypatch, datetime(2026, 10, 12, 2, 30))   # Monday
    log = (tmp_path / "logs" / "backup.log").read_text()
    assert rc == 0
    assert "restore_test ok 2026-10-08" in log
    assert (tmp_path / "backup" / "LAST_OK").is_file()
    (tmp_path / "logs" / "backup.log").unlink()
    dest, rc = _run_with(tool, tmp_path, monkeypatch, datetime(2026, 10, 13, 2, 30))   # Tuesday
    log = (tmp_path / "logs" / "backup.log").read_text()
    assert rc == 0 and "restore_test" not in log and "backup ok" in log


def test_run_logs_restore_test_fail_keeps_last_ok_and_exits_nonzero(tool, tmp_path, monkeypatch):
    conn = db.connect_memory()
    _seed_all(conn)
    dest = tmp_path / "backup" / "2026-10-08"
    bk.export_tables(conn, dest)
    conn.close()
    # Damage one row without touching the checksum: the file verifies, the restore does not reproduce the count.
    p = dest / "events.jsonl.gz"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write("")
    m = json.loads((dest / "manifest.json").read_text())
    m["tables"]["events"]["sha256"] = bk._sha256(p)
    (dest / "manifest.json").write_text(json.dumps(m))
    monkeypatch.setattr(tool, "_export_via_daemon", lambda: dest)
    monkeypatch.setattr(tool, "_cfg", lambda: dict(tool.DEFAULTS, remote=""))
    rc = tool.cmd_run(sync=True, now=datetime(2026, 10, 12, 2, 30))
    log = (tmp_path / "logs" / "backup.log").read_text()
    assert "restore_test FAIL 2026-10-08" in log and "events: restored 0 of 1" in log
    assert (tmp_path / "backup" / "LAST_OK").is_file()        # LAST_OK still certifies export + checksums + copy
    assert rc == 7                                            # the drill result is in the exit code and the log


# ---------------------------------------------------------------- the drill and the lineage archive

def _fake_archive(tmp_path):
    """An archive directory whose parquet is junk: a drill that only checksums
    passes, a drill that loads it cannot."""
    a = tmp_path / "archive"
    a.mkdir()
    (a / bk.ARCHIVE_ALIASES).write_bytes(b"not parquet at all")
    (a / "other.parquet").write_bytes(b"x")
    lines = [f"{bk._sha256(a / n)}  {n}" for n in (bk.ARCHIVE_ALIASES, "other.parquet")]
    (a / bk.ARCHIVE_MANIFEST).write_text("\n".join(lines) + "\n")
    return a


def _export_with_filtered_aliases(tmp_path, archive):
    conn = db.connect_memory()
    _seed_all(conn)
    db.execute(conn, "INSERT INTO sample_aliases (old_id, new_id, reason, created_at) VALUES "
                     "('o1', 'n1', 'history_rebase_v1', ?), ('o2', 'n2', NULL, ?)",
               [datetime(2026, 10, 7), datetime(2026, 10, 7)])
    db.execute(conn, "UPDATE migrations SET summary = ?",
               [json.dumps({"archive_manifest_sha256": bk.archive_manifest_digest(archive), "archive_places": [str(archive)]})])
    dest = tmp_path / "backup" / "2026-10-08"
    m = bk.export_tables(conn, dest)
    conn.close()
    assert m["tables"]["sample_aliases"] ["rows"] == 1 and m["tables"]["sample_aliases"]["rows_excluded"] == 1
    return dest


def test_unattended_drill_verifies_the_archive_by_checksum_and_does_not_load_it(tmp_path):
    archive = _fake_archive(tmp_path)
    dest = _export_with_filtered_aliases(tmp_path, archive)
    res = bk.restore_test(dest, archive, load_archive=False)
    assert res["ok"], res["problems"]
    assert res["tables"]["sample_aliases"]["archive_verified"] is True
    assert res["tables"]["sample_aliases"]["archive_rows_not_loaded"] == 1
    # The full drill would have to read the junk parquet and must say so.
    full = bk.restore_test(dest, archive, load_archive=True)
    assert not full["ok"] and any("sample_aliases" in p for p in full["problems"])
    # A tampered archive fails the checksum drill.
    (archive / "other.parquet").write_bytes(b"changed")
    bad = bk.restore_test(dest, archive, load_archive=False)
    assert not bad["ok"] and any("checksum mismatch" in p for p in bad["problems"])
    # No reachable archive is reported, not hidden.
    none = bk.restore_test(dest, None, load_archive=False)
    assert not none["ok"] and any("archive" in p and "required" in p for p in none["problems"])


def test_run_drill_line_names_the_archive_rows_held_there(tool, tmp_path, monkeypatch):
    archive = _fake_archive(tmp_path)
    dest = _export_with_filtered_aliases(tmp_path, archive)
    monkeypatch.setattr(tool, "_export_via_daemon", lambda: dest)
    monkeypatch.setattr(tool, "_cfg", lambda: dict(tool.DEFAULTS, remote=""))
    rc = tool.cmd_run(sync=True, now=datetime(2026, 10, 12, 2, 30))
    log = (tmp_path / "logs" / "backup.log").read_text()
    assert rc == 0
    assert "restore_test ok 2026-10-08: 16 tables restored in" in log
    assert "archive verified by checksum, 1 alias rows held there" in log


def test_drill_runs_after_the_off_mac_copy(tool, tmp_path, monkeypatch):
    dest, m = _export(tmp_path)
    want = [spec["sha256"] for spec in m["tables"].values()]

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(cmd, **kw):
        if cmd[-1].startswith("ls -1 "):
            return R(0, "2026-10-08\n")
        if "shasum" in cmd[-1]:
            return R(0, "\n".join(want) + "\n")
        return R(0)

    monkeypatch.setattr(tool, "_export_via_daemon", lambda: dest)
    monkeypatch.setattr(tool, "_cfg", lambda: dict(tool.DEFAULTS, remote="u@h", include_overlays=False))
    rc = tool.cmd_run(sync=True, now=datetime(2026, 10, 12, 2, 30), run=fake_run)
    lines = (tmp_path / "logs" / "backup.log").read_text().splitlines()
    order = [i for i, l in enumerate(lines) if " remote ok " in l] + [i for i, l in enumerate(lines) if "restore_test ok" in l]
    assert rc == 0 and len(order) == 2 and order[0] < order[1]
    assert lines[-1].endswith("backup ok 2026-10-08")


def test_a_crashing_drill_is_a_fail_line_and_a_soft_failure_not_a_lost_night(tool, tmp_path, monkeypatch):
    dest, _ = _export(tmp_path)
    monkeypatch.setattr(tool, "_export_via_daemon", lambda: dest)
    monkeypatch.setattr(tool, "_cfg", lambda: dict(tool.DEFAULTS, remote=""))

    def boom(*a, **k):
        raise MemoryError("simulated")

    monkeypatch.setattr(tool.bk, "restore_test", boom)
    rc = tool.cmd_run(sync=True, now=datetime(2026, 10, 12, 2, 30))
    log = (tmp_path / "logs" / "backup.log").read_text()
    assert rc == tool.SOFT_FAIL_EXIT == 7
    assert "restore_test FAIL 2026-10-08: drill raised MemoryError: simulated" in log
    assert (tmp_path / "backup" / "LAST_OK").is_file() and "backup ok 2026-10-08 (with 1 soft failure(s)" in log


def test_manual_drill_checksums_the_archive_by_default_and_loads_it_only_with_full(tool, tmp_path):
    archive = _fake_archive(tmp_path)
    dest = _export_with_filtered_aliases(tmp_path, archive)
    assert tool.main(["helios_backup.py", "restore-test", str(dest)]) == 0
    assert tool.main(["helios_backup.py", "restore-test", str(dest), "--full"]) == 1   # the junk parquet cannot load
    log = (tmp_path / "logs" / "backup.log").read_text()
    assert "restore_test ok 2026-10-08 (manual drill, checksum)" in log
    assert "restore_test FAIL 2026-10-08 (manual drill, full)" in log
