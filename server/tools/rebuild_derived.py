#!/usr/bin/env python
"""Wave 2 rebuild tool (fix program design, section 3): run the Wave 2 data
migrations and the full derived rebuild on a COPY of the store, and write
summary.json with the timings, the row count of every table, the migrations
and the peak memory. Counts only: no value, no secret is printed or written.

    cd server && HELIOS_HOME=<stage folder> <venv>/bin/python tools/rebuild_derived.py <store.duckdb> \
        --today YYYY-MM-DD [--wave2-migrations] --out DIR [--apply]

Steps: refuse the live store (below); db.init_schema (schema v4; a store with
a committed but unverified migration is refused); policy.sync_registry; the
registry check validate_policy_against_registry when trust/schema.py has it
(any problem stops the run); with --wave2-migrations the export relink (B9),
the D5 row (B13) and whoop.rederive_all when ingest/whoop.py has it, each
leaving a verified migrations row; then signals/rebuild.py rebuild_all and a
CHECKPOINT. The policy and the source registry come from HELIOS_HOME (the
staged overlay), so the run uses the same loader as the daemon.

The live store (~/Helios/data/helios.duckdb, by path or by inode) is refused
unless --apply is given AND no process holds the file (lsof). Exit codes:
0 done, 1 stopped by a check or a failed step, 2 refused.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import resource
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import heliosd  # noqa: E402
from heliosd.config import load_settings  # noqa: E402
from heliosd.ingest import whoop  # noqa: E402
from heliosd.ingest.normalize import reporting_today  # noqa: E402
from heliosd.migrate import export_relink as xr  # noqa: E402
from heliosd.migrate.rebase_history import _code_commit  # noqa: E402
from heliosd.signals.rebuild import rebuild_all  # noqa: E402
from heliosd.store import db  # noqa: E402
from heliosd.trust import schema  # noqa: E402
from heliosd.trust.policy import MetricPolicy  # noqa: E402
from heliosd.trust.registry import SourceRegistry  # noqa: E402

LIVE_STORE = Path("~/Helios/data/helios.duckdb").expanduser()
REDERIVE_MIGRATION = "wave2_whoop_rederive_v1"     # written here only when rederive_all writes no row of its own


class Refused(Exception):
    """The store must not be opened by this run."""


class Stop(Exception):
    """A check failed: the run stops, summary.json says why."""


def is_live(path: Path) -> bool:
    """The live store, by resolved path or by inode (a hard link or a symlink
    counts; an APFS clone is a file of its own and does not)."""
    if path.resolve() == LIVE_STORE.resolve():
        return True
    try:
        return path.exists() and LIVE_STORE.exists() and os.path.samefile(path, LIVE_STORE)
    except OSError:
        return True                                        # cannot tell: treat it as live


def holders(path: Path) -> list[int]:
    """Process ids that hold the store file or its WAL open (lsof -t). lsof
    exits 1 with no output when no process does; anything else it cannot
    answer raises, so the caller refuses."""
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    files = [str(p) for p in (path, path.with_name(path.name + ".wal")) if p.exists()]
    r = subprocess.run([lsof, "-t", *files], capture_output=True, text=True, timeout=60)
    pids = sorted({int(x) for x in r.stdout.split() if x.strip().isdigit()})
    if r.returncode not in (0, 1) or (r.returncode == 1 and not pids and r.stderr.strip()):
        raise Refused(f"lsof could not say whether {path} is held open (exit {r.returncode}): {r.stderr.strip()[:200]}")
    return pids


def check_store(store: Path, apply: bool) -> None:
    if is_live(store):
        if not apply:
            raise Refused(f"refusing the live store {store}: rebuild a copy, or pass --apply with heliosd stopped")
        pids = holders(store)
        if pids:
            raise Refused(f"refusing the live store {store}: process(es) {pids} hold it open; stop heliosd first")
    if not store.is_file():
        raise Refused(f"no store file at {store}")


def _call(fn, conn, **available):
    """fn(conn, ...) with the keyword arguments its signature names."""
    params = inspect.signature(fn).parameters
    return fn(conn, **{k: v for k, v in available.items() if k in params})


def _rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1)


def _registry_check(policy, registry) -> list[str]:
    fn = getattr(schema, "validate_policy_against_registry", None)
    if fn is None:
        return []
    try:
        problems = fn(policy, registry)
    except schema.PolicyError as e:
        return list(e.problems)
    return list(problems or [])


def _migrations(conn) -> list[dict]:
    out = []
    for name, applied_at, summary in db.fetchall(conn, "SELECT name, applied_at, summary FROM migrations ORDER BY name"):
        try:
            s = json.loads(summary or "{}")
        except ValueError:
            s = {}
        counts = s.get("counts") or {k: v for k, v in s.items() if isinstance(v, int) and not isinstance(v, bool)}
        out.append({"name": name, "applied_at": str(applied_at), "phase": s.get("phase"), "counts": counts})
    return out


def run(store, today: date, out, wave2_migrations: bool = False, apply: bool = False,
        policy: MetricPolicy | None = None, registry: SourceRegistry | None = None, log=print) -> dict:
    """The whole run; raises Refused before anything is opened. Returns the
    summary (also written to <out>/summary.json)."""
    store = Path(store).expanduser()
    check_store(store, apply)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    S: dict = {"tool": "rebuild_derived", "store": str(store), "today": str(today), "wave2_migrations": wave2_migrations,
               "apply": apply, "heliosd_file": heliosd.__file__, "code_commit": _code_commit(Path(heliosd.__file__).parent),
               "started": datetime.now().isoformat(timespec="seconds"), "steps": [], "seconds": {}, "results": {},
               "stopped": None}
    t_all = time.time()
    conn = None
    log(f"heliosd from {heliosd.__file__}; store {store}; today {today}")

    def step(name: str, fn):
        t0 = time.time()
        log(f"{time.strftime('%H:%M:%S')} {name} ...")
        result = fn()
        S["steps"].append(name)
        S["seconds"][name] = round(time.time() - t0, 2)
        log(f"{time.strftime('%H:%M:%S')} {name} done in {S['seconds'][name]} s")
        return result
    try:
        conn = step("init_schema", lambda: db.connect(store))
        policy = policy or MetricPolicy(default_tz=load_settings().timezone)
        registry = registry or SourceRegistry()
        step("sync_registry", lambda: policy.sync_registry(conn))
        problems = step("registry_check", lambda: _registry_check(policy, registry))
        S["results"]["registry_check"] = {"ran": hasattr(schema, "validate_policy_against_registry"), "problems": problems}
        if problems:
            raise Stop(f"the policy does not match the source registry: {problems[:20]}")
        if wave2_migrations:
            S["results"]["export_relink"] = step("export_relink", lambda: xr.relink(conn))
            S["results"]["d5_rows"] = step("d5_rows", lambda: xr.record_d5_rows(conn))
            fn = getattr(whoop, "rederive_all", None)
            if fn is None:
                S["results"]["whoop_rederive"] = {"ran": False, "reason": "ingest/whoop.py has no rederive_all"}
            else:
                res = step("whoop_rederive", lambda: _call(fn, conn, policy=policy, registry=registry, today=today))
                S["results"]["whoop_rederive"] = {"ran": True, "result": res}
                own = db.fetchall(conn, "SELECT COUNT(*) FROM migrations WHERE name LIKE 'wave2_whoop%'")[0][0]
                if not own:
                    db.execute(conn, "INSERT INTO migrations (name, applied_at, code_commit, input_fingerprint, summary) "
                                     "VALUES (?, ?, ?, NULL, ?)",
                               [REDERIVE_MIGRATION, datetime.now().replace(microsecond=0), S["code_commit"],
                                json.dumps({"phase": "verified", "migration": REDERIVE_MIGRATION, "design": "Wave 2 B5 to B8",
                                            "verified_by": "rebuild_derived: rederive_all returned without an error",
                                            "result": res}, default=str)])
        rb = step("rebuild_all", lambda: rebuild_all(conn, policy, today, registry=registry))
        S["results"]["rebuild_all"] = {**rb, "range": [str(d) for d in rb["range"]]}
        step("checkpoint", lambda: db.checkpoint(conn))
        lo, hi = rb["range"]
        S["eligible_rows_outside_the_rebuilt_range"] = db.fetchall(
            conn, "SELECT COUNT(*) FROM eligible_samples WHERE CAST(start_ts AS DATE) NOT BETWEEN ? AND ?", [lo, hi])[0][0]
        S["unresolved_exports"] = xr.unresolved_exports(conn)
    except Stop as e:
        S["stopped"] = str(e)
        log(f"STOP: {e}")
    except Exception as e:                                # noqa: BLE001 - recorded, then re-raised for the log
        S["stopped"] = f"error: {type(e).__name__}: {str(e)[:300]}"
        log(f"ERROR: {S['stopped']}")
        raise
    finally:
        if conn is not None:
            S["counts"] = {t: db.fetchall(conn, f'SELECT COUNT(*) FROM "{t}"')[0][0] for (t,) in db.fetchall(
                conn, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' "
                      "AND table_type = 'BASE TABLE' ORDER BY 1")}
            S["migrations"] = _migrations(conn)
            conn.close()
        S["seconds"]["total"] = round(time.time() - t_all, 2)
        S["peak_rss_mb"] = _rss_mb()
        S["finished"] = datetime.now().isoformat(timespec="seconds")
        (out / "summary.json").write_text(json.dumps(S, indent=2, default=str), encoding="utf-8")
    return S


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Wave 2: the data migrations and the full derived rebuild, on a copy")
    ap.add_argument("store", help="the store file: a copy (the live file only with --apply and heliosd stopped)")
    ap.add_argument("--today", default=None, help="the reporting today, YYYY-MM-DD (default: today in the policy zone)")
    ap.add_argument("--wave2-migrations", action="store_true", help="the export relink, the D5 row and the Whoop re-derive first")
    ap.add_argument("--out", required=True, help="where summary.json goes")
    ap.add_argument("--apply", action="store_true", help="allow the live store, only while no process holds it")
    args = ap.parse_args(argv)
    policy = MetricPolicy(default_tz=load_settings().timezone)
    today = date.fromisoformat(args.today) if args.today else reporting_today(policy.zone)
    try:
        S = run(args.store, today, args.out, wave2_migrations=args.wave2_migrations, apply=args.apply, policy=policy)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(json.dumps({k: S.get(k) for k in ("stopped", "seconds", "peak_rss_mb", "counts")}, default=str))
    return 1 if S["stopped"] else 0


if __name__ == "__main__":
    sys.exit(main())
