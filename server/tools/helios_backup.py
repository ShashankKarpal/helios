#!/usr/bin/env python
"""Nightly durability run for Helios (see heliosd/backup.py for the shape).

    server/.venv/bin/python server/tools/helios_backup.py run
        export via the daemon, verify, prune, sync off the Mac, touch LAST_OK
    ... helios_backup.py export            export only (no sync)
    ... helios_backup.py restore-test [DIR]
        restore drill on the newest (or given) export directory
    ... helios_backup.py status            last OK, last log lines

Configuration, all in ~/Helios/helios.toml under [backup] (every key optional):

    [backup]
    remote = "user@host"                 # rsync/ssh target; empty = local only
    remote_dir = "Backups/helios"        # relative to the remote home
    keep_days = 30                       # local and remote retention
    include_overlays = true              # ship ~/Helios/*.yaml alongside

Never shipped: helios.toml (secrets), whoop_tokens.json, the DuckDB itself.
Failure contract: every abort exits non-zero and writes a dated line to
~/Helios/logs/backup.log; LAST_OK (in ~/Helios/backup) is touched ONLY after
export, checksum verification, and, when a remote is configured, a verified
remote copy. The sync watchdog watches LAST_OK's age, so a failing night shows
up in `freshness` instead of in nobody's inbox.
"""

from __future__ import annotations

import os
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
    c = {"remote": "", "remote_dir": "Backups/helios", "keep_days": 30, "include_overlays": True}
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


def _sync(cfg: dict, export_dir: Path) -> None:
    remote, rdir = cfg["remote"], cfg["remote_dir"].rstrip("/")
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", remote]
    if subprocess.run(ssh + [f"mkdir -p ~/{rdir}/overlays"], capture_output=True, timeout=60).returncode != 0:
        abort(2, "remote unreachable")
    excludes = [f"--exclude={p}" for p in NEVER_SHIP]
    rc = subprocess.run(["/usr/bin/rsync", "-a", "--delete", *excludes, f"{BACKUP_DIR}/",
                         f"{remote}:{rdir}/exports/"], capture_output=True, text=True, timeout=600)
    if rc.returncode != 0:
        abort(3, f"rsync exports rc={rc.returncode}: {rc.stderr.strip()[:200]}")
    if cfg.get("include_overlays", True) and _overlays():
        rc = subprocess.run(["/usr/bin/rsync", "-a", *excludes, *[str(p) for p in _overlays()],
                             f"{remote}:{rdir}/overlays/"], capture_output=True, text=True, timeout=120)
        if rc.returncode != 0:
            abort(3, f"rsync overlays rc={rc.returncode}: {rc.stderr.strip()[:200]}")
    # Verify the remote copy byte for byte against the manifest checksums.
    m = bk.read_manifest(export_dir)
    cmd = " && ".join(
        f"shasum -a 256 ~/{rdir}/exports/{export_dir.name}/{spec['file']} | cut -c1-64"
        for spec in m["tables"].values())
    out = subprocess.run(ssh + [cmd], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        abort(4, f"remote checksum command failed: {out.stderr.strip()[:200]}")
    got = out.stdout.split()
    want = [spec["sha256"] for spec in m["tables"].values()]
    if got != want:
        abort(4, f"remote checksum mismatch ({sum(g != w for g, w in zip(got, want))} files)")
    log(f"remote ok {remote}:{rdir}/exports/{export_dir.name} ({len(want)} files verified)")


def cmd_run(sync: bool = True) -> int:
    cfg = _cfg()
    export_dir = _export_via_daemon()
    problems = bk.verify_files(export_dir)
    if problems:
        abort(6, "local checksum problems: " + "; ".join(problems))
    gone = _prune(int(cfg["keep_days"]))
    if gone:
        log(f"pruned {len(gone)} export dirs older than {cfg['keep_days']} days")
    if sync and cfg["remote"]:
        _sync(cfg, export_dir)
    elif sync:
        log("no [backup] remote configured; local export only")
    LAST_OK.parent.mkdir(parents=True, exist_ok=True)
    LAST_OK.write_text(datetime.now().isoformat() + "\n", encoding="utf-8")
    log(f"backup ok {export_dir.name}")
    return 0


def cmd_restore_test(arg: str | None) -> int:
    if arg:
        src = Path(arg).expanduser()
    else:
        dirs = sorted(d for d in BACKUP_DIR.glob("20??-??-??") if d.is_dir())
        if not dirs:
            print("no export directories found")
            return 1
        src = dirs[-1]
    res = bk.restore_test(src)
    for t, v in res["tables"].items():
        print(f"{t:14s} expected {v['expected']:6d} restored {v['restored']:6d}")
    for p in res["problems"]:
        print("PROBLEM:", p)
    verdict = "RESTORE DRILL OK" if res["ok"] else "RESTORE DRILL FAILED"
    print(f"{verdict} {src}")
    log(f"restore drill {'ok' if res['ok'] else 'FAILED'} {src.name}")
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
        return cmd_restore_test(argv[2] if len(argv) > 2 else None)
    if cmd == "status":
        return cmd_status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
