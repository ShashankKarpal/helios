#!/usr/bin/env python
"""Single-source Phase 2 history import: the raw Apple Health export folders
into the schema v5 tables of a store (a copy first; the live store only after
the owner's yes to the diff table, with --apply and heliosd stopped).

    cd server && HELIOS_HOME=<overlay dir> <venv>/bin/python tools/import_history_phase2.py \
        <store.duckdb> --export <folder> [--export <folder> ...] --out <dir> [--apply]

Give every export folder in one run (export.xml with its electrocardiograms/,
workout-routes/ and attachments/, or a folder of side files only): workouts
are matched across all of them at once. The folders are only read; a folder
whose sha256 manifest does not verify stops the run before any write.
HELIOS_HOME supplies the owner's policy and source registry overlays (the
daemon's own); ignored sources are stored as device_key 'excluded'.

What it writes (heliosd/history/importer.py has the contract): raw_records,
the workout tables, route_points, activity_summaries, ecgs, ecg_waveforms,
export_files, import_runs and the migrations row phase2_history_import_v1, in
one transaction with the checks; never the samples table or anything derived
from it. A second run of the same folders writes nothing. Bulk rows pass
through a private staging folder beside the store (mode 0700), removed at the
end.

<out>/reconciliation.json holds counts, identifiers and dates only (no
measured value); <out>/run.log the progress lines.

Exit codes: 0 imported and verified, 1 stopped by a check (nothing kept),
2 refused (the live store without --apply or while held open, a bad input,
or the R1 guard: a run that would add workout records to a store that already
holds an import, refused until stored sessions can be rebuilt; nothing kept).
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

# The server and tools folders on the import path, once: a test that loads this module again and again must
# not grow sys.path (every import that misses, such as DuckDB's probe for pandas on each bound parameter, walks
# the whole path; 28 extra entries made the landing budget test 1.3 s slower, 2026-10-20).
for _p in (Path(__file__).resolve().parents[1], Path(__file__).resolve().parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import rebuild_derived as rd  # noqa: E402  (check_store, Refused: one definition of "live")
from heliosd.config import load_source_registry  # noqa: E402
from heliosd.history import importer  # noqa: E402
from heliosd.store import db  # noqa: E402
from heliosd.trust.policy import MetricPolicy  # noqa: E402
from heliosd.trust.registry import SourceRegistry  # noqa: E402


def store_mode_registry() -> SourceRegistry:
    """The owner's registry with ignored sources kept as 'excluded' (plan v2 4.2), whatever its own mode."""
    return SourceRegistry(dict(load_source_registry(), ignored_mode="store"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 2: import raw Apple Health exports into the v5 tables")
    ap.add_argument("store", help="the store file: a copy (the live file only with --apply and heliosd stopped)")
    ap.add_argument("--export", action="append", required=True, help="an export folder (repeat for several)")
    ap.add_argument("--out", required=True, help="where reconciliation.json and run.log go")
    ap.add_argument("--apply", action="store_true", help="allow the live store (heliosd must be stopped)")
    args = ap.parse_args(argv)
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    logf = open(out / "run.log", "a", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    store = Path(args.store).expanduser()
    try:
        rd.check_store(store, args.apply)
        folders = [Path(f).expanduser() for f in args.export]
        for f in folders:
            if not f.is_dir():
                raise rd.Refused(f"no export folder at {f}")
    except rd.Refused as e:
        log(f"REFUSED: {e}")
        return 2
    run_id = "p2-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    stage = store.parent / f".{run_id}-stage"
    log(f"run {run_id}: store {store.name}, exports {[f.name for f in folders]}")
    policy = MetricPolicy()
    registry = store_mode_registry()
    t0 = time.time()
    conn = duckdb.connect(str(store))
    try:
        db.init_schema(conn)
        commit = importer.code_commit_of(Path(__file__).parent)
        try:
            rep = importer.import_exports(conn, folders, policy, registry, stage, run_id, code_commit=commit, log=log)
            rc = 0
        except importer.Refused as e:
            rep = getattr(e, "report", {"stopped": str(e)})
            log(f"REFUSED: {e}")
            rc = 2
        except importer.Stop as e:
            rep = getattr(e, "report", {"stopped": str(e)})
            log(f"STOPPED: {e}")
            rc = 1
        except importer.ex.UnknownShape as e:
            rep = {"stopped": f"UnknownShape: {e}"}
            log(f"STOPPED: the export holds a shape this importer does not know: {e}")
            rc = 1
        conn.execute("CHECKPOINT")
    finally:
        conn.close()
    rep["total_seconds"] = round(time.time() - t0, 1)
    (out / "reconciliation.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    if rc == 0:
        failed = [k for k, v in rep.get("checks", {}).items() if not v.get("ok")]
        log(f"DONE in {rep['total_seconds']} s: {len(rep.get('checks', {}))} checks, {len(failed)} failed; "
            f"migrations row {rep.get('migration_row')}")
        # reopen through the daemon's own path: it must accept the store
        c2 = db.connect(store)
        c2.close()
        log("reopened through db.connect: ok")
    logf.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
