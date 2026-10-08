#!/usr/bin/env python
"""Nightly durability run for Helios (see heliosd/backup.py for the shape).

    server/.venv/bin/python server/tools/helios_backup.py run
        export via the daemon, verify, prune, weekly restore drill, sync off
        the Mac, prune the remote copy by age, touch LAST_OK
    ... helios_backup.py export            export only (no sync)
    ... helios_backup.py restore-test [DIR] [ARCHIVE_DIR]
        restore drill on the newest (or given) export directory, with the
        lineage archive the export's manifest names (or the one given)
    ... helios_backup.py status            last OK, last log lines

Configuration, all in ~/Helios/helios.toml under [backup] (every key optional):

    [backup]
    remote = "user@host"                 # rsync/ssh target; empty = local only
    remote_dir = "Backups/helios"        # relative to the remote home
    keep_days = 30                       # local retention (export dirs by name)
    remote_keep_days = 90                # remote retention, independent of local (A26)
    restore_test_weekday = 0             # 0 = Monday: the night the restore drill runs (A26)
    include_overlays = true              # ship ~/Helios/*.yaml alongside

Never shipped: helios.toml (secrets), whoop_tokens.json, the DuckDB itself.
Failure contract: every abort exits non-zero and writes a dated line to
~/Helios/logs/backup.log; LAST_OK (in ~/Helios/backup) is touched ONLY after
export, checksum verification, and, when a remote is configured, a verified
remote copy. The sync watchdog watches LAST_OK's age, so a failing night shows
up in `freshness` instead of in nobody's inbox.

Since Wave 1 A26 (audit 2026-10-08, P18): the exports are copied WITHOUT
rsync --delete, so a local prune or a damaged local backup directory can
never erase the remote copy; the remote keeps its own, longer retention,
pruned by directory name age with a command that can only name dated export
directories under the remote exports directory. Once a week the run also
restores the fresh export into memory (with the lineage archive the manifest
binds) and writes one "restore_test ok|FAIL ..." line. A failed drill or a
failed remote prune does not change what LAST_OK certifies; it is in the log
line and in the run's exit code (7, after LAST_OK), which launchd records.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from heliosd import backup as bk  # noqa: E402
from heliosd.config import helios_home, load_settings  # noqa: E402

HOME = helios_home()
BACKUP_DIR = HOME / "backup"
LOG = HOME / "logs" / "backup.log"
LAST_OK = BACKUP_DIR / "LAST_OK"
NEVER_SHIP = ("helios.toml", "whoop_tokens.json", "*.duckdb", "*.duckdb.wal")
DEFAULTS = {"remote": "", "remote_dir": "Backups/helios", "keep_days": 30, "remote_keep_days": 90,
            "restore_test_weekday": 0, "include_overlays": True}
EXPORT_NAME = re.compile(r"^20\d\d-\d\d-\d\d$")
SOFT_FAIL_EXIT = 7


def log(line: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{stamp} {line}\n")
    print(line)


def abort(code: int, line: str) -> None:
    log(f"aborted: {line}")
    sys.exit(code)


def _cfg() -> dict:
    st = load_settings()
    c = dict(DEFAULTS)
    c.update(st.raw.get("backup", {}))
    return c


def _export_via_daemon() -> Path:
    st = load_settings()
    if not st.ingest_token:
        abort(5, "no ingest_token in config; cannot call the daemon")
    base = os.environ.get("HELIOS_API", f"https://127.0.0.1:{st.port}")
    try:
        r = httpx.post(f"{base}/api/admin/export", headers={"X-Helios-Token": st.ingest_token},
                       verify=False, timeout=120)
        r.raise_for_status()
    except httpx.HTTPError as e:
        abort(1, f"daemon export failed: {e}")
    body = r.json()
    dest = Path(body["path"])
    counts = ", ".join(f"{t}={v['rows']}" for t, v in body["manifest"]["tables"].items())
    log(f"export ok {dest.name} ({counts})")
    return dest


def _prune(keep_days: int) -> list[str]:
    cutoff = date.today() - timedelta(days=keep_days)
    gone = []
    for d in sorted(BACKUP_DIR.glob("20??-??-??")):
        try:
            if date.fromisoformat(d.name) < cutoff and d.is_dir():
                shutil.rmtree(d)
                gone.append(d.name)
        except ValueError:
            continue
    return gone


def _overlays() -> list[Path]:
    return [p for p in HOME.glob("*.yaml") if p.is_file()]


def rsync_exports_cmd(backup_dir: Path, remote: str, rdir: str) -> list[str]:
    """The exports copy. Never --delete (A26): the remote copy outlives any
    local prune or damage; remote retention is remote_prune_plan's job."""
    excludes = [f"--exclude={p}" for p in NEVER_SHIP]
    return ["/usr/bin/rsync", "-a", *excludes, f"{backup_dir}/", f"{remote}:{rdir}/exports/"]


def remote_prune_plan(listing: list[str], keep_days: int, today: date) -> list[str]:
    """Which remote export directories are older than keep_days, by NAME (the
    same rule as the local prune). Only names shaped like an export date count;
    anything else in the listing (LAST_OK, overlays, a stray file, a name with
    shell characters) is never touched."""
    cutoff = today - timedelta(days=keep_days)
    gone = []
    for name in listing:
        name = name.strip()
        if not EXPORT_NAME.match(name):
            continue
        try:
            d = date.fromisoformat(name)
        except ValueError:
            continue
        if d < cutoff:
            gone.append(name)
    return sorted(gone)


def remote_prune_cmd(rdir: str, names: list[str]) -> str:
    """One bounded shell line: enter the exports directory (nothing runs if
    that fails) and remove only the validated, relative, dated names."""
    assert all(EXPORT_NAME.match(n) for n in names), names
    return f"cd ~/{rdir}/exports && rm -rf -- " + " ".join(names)


def restore_drill_due(today: date, weekday: int) -> bool:
    return today.weekday() == int(weekday)


def _sync(cfg: dict, export_dir: Path, run=subprocess.run, today: date | None = None) -> list[str]:
    """Copy the exports off the Mac, prune the remote copy by age, verify the
    fresh export's checksums on the remote. Returns the soft failures (the
    remote prune is best effort and never blocks a verified copy)."""
    remote, rdir = cfg["remote"], cfg["remote_dir"].rstrip("/")
    today = today or date.today()
    soft: list[str] = []
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", remote]
    if run(ssh + [f"mkdir -p ~/{rdir}/overlays ~/{rdir}/exports"], capture_output=True, timeout=60).returncode != 0:
        abort(2, "remote unreachable")
    rc = run(rsync_exports_cmd(BACKUP_DIR, remote, rdir), capture_output=True, text=True, timeout=600)
    if rc.returncode != 0:
        abort(3, f"rsync exports rc={rc.returncode}: {rc.stderr.strip()[:200]}")
    if cfg.get("include_overlays", True) and _overlays():
        excludes = [f"--exclude={p}" for p in NEVER_SHIP]
        rc = run(["/usr/bin/rsync", "-a", *excludes, *[str(p) for p in _overlays()],
                  f"{remote}:{rdir}/overlays/"], capture_output=True, text=True, timeout=120)
        if rc.returncode != 0:
            abort(3, f"rsync overlays rc={rc.returncode}: {rc.stderr.strip()[:200]}")
    # Remote retention, independent of the local one (A26): by name age, bounded
    # to dated directories under the exports directory.
    keep = int(cfg.get("remote_keep_days", DEFAULTS["remote_keep_days"]))
    listing = run(ssh + [f"ls -1 ~/{rdir}/exports"], capture_output=True, text=True, timeout=60)
    if listing.returncode != 0:
        msg = f"remote prune FAILED: listing rc={listing.returncode}: {listing.stderr.strip()[:200]}"
        log(msg)
        soft.append(msg)
    else:
        gone = remote_prune_plan(listing.stdout.splitlines(), keep, today)
        if gone:
            pr = run(ssh + [remote_prune_cmd(rdir, gone)], capture_output=True, text=True, timeout=300)
            if pr.returncode != 0:
                msg = f"remote prune FAILED rc={pr.returncode}: {pr.stderr.strip()[:200]}"
                log(msg)
                soft.append(msg)
            else:
                log(f"remote pruned {len(gone)} export dirs older than {keep} days")
    # Verify the remote copy byte for byte against the manifest checksums.
    m = bk.read_manifest(export_dir)
    cmd = " && ".join(
        f"shasum -a 256 ~/{rdir}/exports/{export_dir.name}/{spec['file']} | cut -c1-64"
        for spec in m["tables"].values())
    out = run(ssh + [cmd], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        abort(4, f"remote checksum command failed: {out.stderr.strip()[:200]}")
    got = out.stdout.split()
    want = [spec["sha256"] for spec in m["tables"].values()]
    if got != want:
        abort(4, f"remote checksum mismatch ({sum(g != w for g, w in zip(got, want))} files)")
    log(f"remote ok {remote}:{rdir}/exports/{export_dir.name} ({len(want)} files verified)")
    return soft


def _restore_drill(export_dir: Path) -> bool:
    """The weekly drill (A26): restore the fresh export into memory with the
    lineage archive its manifest binds, one log line either way."""
    m = bk.read_manifest(export_dir)
    archive = bk.archive_dir_from_manifest(m)
    res = bk.restore_test(export_dir, archive)
    aliases = res["tables"].get("sample_aliases", {}).get("from_archive")
    if res["ok"]:
        extra = f", {aliases} alias rows from the archive" if aliases is not None else ""
        log(f"restore_test ok {export_dir.name} ({len(res['tables'])} tables{extra})")
        return True
    log(f"restore_test FAIL {export_dir.name}: " + "; ".join(res["problems"])[:400])
    return False


def cmd_run(sync: bool = True, now: datetime | None = None, run=subprocess.run) -> int:
    cfg = _cfg()
    now = now or datetime.now()
    soft: list[str] = []
    export_dir = _export_via_daemon()
    problems = bk.verify_files(export_dir)
    if problems:
        abort(6, "local checksum problems: " + "; ".join(problems))
    gone = _prune(int(cfg["keep_days"]))
    if gone:
        log(f"pruned {len(gone)} export dirs older than {cfg['keep_days']} days")
    if restore_drill_due(now.date(), cfg.get("restore_test_weekday", DEFAULTS["restore_test_weekday"])):
        if not _restore_drill(export_dir):
            soft.append("restore drill failed")
    if sync and cfg["remote"]:
        soft.extend(_sync(cfg, export_dir, run=run, today=now.date()))
    elif sync:
        log("no [backup] remote configured; local export only")
    LAST_OK.parent.mkdir(parents=True, exist_ok=True)
    LAST_OK.write_text(datetime.now().isoformat() + "\n", encoding="utf-8")
    log(f"backup ok {export_dir.name}" + (f" (with {len(soft)} soft failure(s), see above)" if soft else ""))
    return SOFT_FAIL_EXIT if soft else 0


def cmd_restore_test(arg: str | None, archive_arg: str | None = None) -> int:
    if arg:
        src = Path(arg).expanduser()
    else:
        dirs = sorted(d for d in BACKUP_DIR.glob("20??-??-??") if d.is_dir())
        if not dirs:
            print("no export directories found")
            return 1
        src = dirs[-1]
    archive = Path(archive_arg).expanduser() if archive_arg else bk.archive_dir_from_manifest(bk.read_manifest(src))
    res = bk.restore_test(src, archive)
    for t, v in res["tables"].items():
        print(f"{t:18s} expected {v['expected']:7d} restored {v['restored']:7d}"
              + (f" (+{v['from_archive']} from the archive)" if "from_archive" in v else ""))
    for p in res["problems"]:
        print("PROBLEM:", p)
    verdict = "RESTORE DRILL OK" if res["ok"] else "RESTORE DRILL FAILED"
    print(f"{verdict} {src}" + (f" (archive {archive})" if archive else " (no archive)"))
    log(f"restore_test {'ok' if res['ok'] else 'FAIL'} {src.name} (manual drill)")
    return 0 if res["ok"] else 1


def cmd_status() -> int:
    if LAST_OK.is_file():
        stamp = LAST_OK.read_text().strip()
        age_h = (datetime.now() - datetime.fromisoformat(stamp)).total_seconds() / 3600
        print(f"last ok: {stamp} ({age_h:.1f} h ago)")
    else:
        print("last ok: never")
    if LOG.is_file():
        print("".join(LOG.read_text(encoding="utf-8").splitlines(True)[-5:]), end="")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "run":
        return cmd_run(sync=True)
    if cmd == "export":
        return cmd_run(sync=False)
    if cmd == "restore-test":
        return cmd_restore_test(argv[2] if len(argv) > 2 else None, argv[3] if len(argv) > 3 else None)
    if cmd == "status":
        return cmd_status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
