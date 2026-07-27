"""One-shot Apple Health export importer + ch2 hash migration.

Usage (heliosd MUST be stopped; the script takes the exclusive DuckDB lock):

    cd ~/Projects/helios/server
    ./.venv/bin/python tools/import_health_export.py \
        --xml ~/Downloads/helios-import/apple_health_export/export.xml \
        [--limit 300000]

What it does, in order:
1. MIGRATE: rewrites every existing 'ch:' sample to the ch2 canonical id
   (whole-second timestamps, canon_value, text_value in the hash) and
   truncates stored start/end to whole seconds. 'wh:' (whoop_live) and any
   other id schemes pass through untouched: their writers use OR REPLACE
   upserts keyed on their own ids. Keep-first on collisions (by ingested_at),
   so re-delivered sub-second twins merge into one row.
2. IMPORT: streams the export.xml Records through the SAME normalize_sample
   the server uses (policy mapping, source registry, sleep stages, SpO2
   scaling), inserting with sync_path='health_export'. INSERT OR IGNORE on
   the ch2 id makes this idempotent and collision-safe against everything
   the Bridge has sent or will ever send.
3. REPORT: prints per-metric inserts, per-source resolution (including
   sources that fell back to 'other' and ignored ones), and writes JSON to
   /tmp/helios_import_report.json.

Personal data never leaves the machine; this only moves bytes from the
export file into the local DuckDB.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from heliosd.ingest.normalize import normalize_sample, sample_id_for  # noqa: E402
from heliosd.trust.policy import MetricPolicy  # noqa: E402
from heliosd.trust.registry import SourceRegistry  # noqa: E402

COLS = ["sample_id", "hk_uuid", "metric", "hk_type", "value", "text_value", "unit",
        "start_ts", "end_ts", "source_name", "device_key", "sync_path"]

SAMPLES_DDL = """
CREATE TABLE samples_new (
    sample_id     VARCHAR PRIMARY KEY,
    hk_uuid       VARCHAR,
    metric        VARCHAR NOT NULL,
    hk_type       VARCHAR,
    value         DOUBLE,
    text_value    VARCHAR,
    unit          VARCHAR,
    start_ts      TIMESTAMP NOT NULL,
    end_ts        TIMESTAMP,
    source_name   VARCHAR NOT NULL,
    device_key    VARCHAR NOT NULL,
    sync_path     VARCHAR NOT NULL,
    ingested_at   TIMESTAMP DEFAULT current_timestamp
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_samples_metric_ts ON samples (metric, start_ts)",
    "CREATE INDEX IF NOT EXISTS idx_samples_hk_uuid ON samples (hk_uuid)",
    "CREATE INDEX IF NOT EXISTS idx_samples_device ON samples (device_key, metric, start_ts)",
]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- migration
#
# Row-wise executemany measured ~1k rows/s on this table (an hour for 3.9M
# rows), so both phases stage rows through a CSV instead: python computes the
# canonical fields and hashes (the same code path future ingests use), and
# DuckDB bulk-loads the file in one vectorized INSERT. Same correctness,
# roughly 20x faster.

NULLSTR = "\\N"

MIG_COLS_SQL = ("{'sample_id':'VARCHAR','hk_uuid':'VARCHAR','metric':'VARCHAR',"
                "'hk_type':'VARCHAR','value':'DOUBLE','text_value':'VARCHAR',"
                "'unit':'VARCHAR','start_ts':'TIMESTAMP','end_ts':'TIMESTAMP',"
                "'source_name':'VARCHAR','device_key':'VARCHAR','sync_path':'VARCHAR',"
                "'ingested_at':'TIMESTAMP'}")


def _c(v):
    return NULLSTR if v is None else v


def migrate(conn) -> dict:
    n_ch = conn.execute("SELECT COUNT(*) FROM samples WHERE sample_id LIKE 'ch:%'").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    if n_ch == 0:
        log("migration: no 'ch:' rows found, already migrated; skipping")
        return {"migrated": 0, "passed_through": total, "merged_duplicates": 0}

    log(f"migration: rehashing {n_ch:,} of {total:,} rows to ch2 (keep-first by ingested_at)")
    conn.execute("DROP TABLE IF EXISTS samples_new")
    conn.execute(SAMPLES_DDL)

    cur = conn.cursor()
    cur.execute("""
        SELECT sample_id, hk_uuid, metric, hk_type, value, text_value, unit,
               start_ts, end_ts, source_name, device_key, sync_path, ingested_at
        FROM samples""")
    staging = "/tmp/helios_migrate_staging.csv"
    seen = 0
    t0 = time.time()
    with open(staging, "w", newline="") as f:
        w = csv.writer(f)
        while True:
            rows = cur.fetchmany(200_000)
            if not rows:
                break
            for (sid, hk_uuid, metric, hk_type, value, text_value, unit,
                 start_ts, end_ts, source_name, device_key, sync_path, ingested_at) in rows:
                start = start_ts.replace(microsecond=0)
                end = end_ts.replace(microsecond=0) if end_ts is not None else None
                # Sleep durations were derived from sub-second spans; recompute
                # from the truncated span so the stored value (and its hash) is
                # identical to what the XML export path computes for the night.
                if metric == "sleep_analysis" and end is not None:
                    value = (end - start).total_seconds() / 60.0
                if sid.startswith("ch:"):
                    sid = sample_id_for(hk_type, str(start), str(end if end is not None else start),
                                        source_name, value, text_value)
                w.writerow([sid, _c(hk_uuid), metric, _c(hk_type), _c(value),
                            _c(text_value), _c(unit), start, _c(end), source_name,
                            device_key, sync_path, ingested_at])
            seen += len(rows)
            log(f"migration: {seen:,}/{total:,} rows staged "
                f"({seen / max(time.time() - t0, 1):,.0f}/s)")

    log("migration: bulk-loading staged rows (keep-first by ingested_at)")
    conn.execute(
        f"INSERT OR IGNORE INTO samples_new "
        f"SELECT * FROM read_csv('{staging}', header=false, nullstr='{NULLSTR}', "
        f"columns={MIG_COLS_SQL}) ORDER BY ingested_at")

    new_total = conn.execute("SELECT COUNT(*) FROM samples_new").fetchone()[0]
    merged = total - new_total
    log(f"migration: {new_total:,} rows kept, {merged:,} sub-second/duplicate twins merged")
    if new_total < total * 0.98:
        raise RuntimeError(
            f"migration sanity check failed: would drop {merged:,} rows (>2%). Aborting; "
            "samples table left untouched.")

    conn.execute("DROP TABLE samples")
    conn.execute("ALTER TABLE samples_new RENAME TO samples")
    for ddl in INDEXES:
        conn.execute(ddl)
    os.remove(staging)
    log("migration: table swapped, indexes rebuilt")
    return {"migrated": n_ch, "passed_through": total - n_ch, "merged_duplicates": merged}


# ------------------------------------------------------------------- import

def parse_export_ts(s: str, tz_cache: dict) -> datetime:
    """'2026-07-24 20:20:35 +0530' -> aware datetime (fast path, cached tzinfo)."""
    off = s[20:]
    tz = tz_cache.get(off)
    if tz is None:
        sign = -1 if off[0] == "-" else 1
        tz = timezone(sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:5])))
        tz_cache[off] = tz
    return datetime.fromisoformat(s[:19]).replace(tzinfo=tz)


def run_import(conn, xml_path: Path, limit: int | None) -> dict:
    policy = MetricPolicy()
    registry = SourceRegistry()
    wanted = set(policy.hk_to_metric.keys())

    stats = {
        "records_seen": 0, "type_matched": 0, "normalized": 0,
        "ignored_source": Counter(), "fallback_other": Counter(),
        "inserted": 0, "deduped": 0,
    }
    per_metric = defaultdict(lambda: {"rows": 0, "min": None, "max": None})
    tz_cache: dict = {}
    staging = "/tmp/helios_import_staging.csv"
    imp_cols_sql = ("{'sample_id':'VARCHAR','hk_uuid':'VARCHAR','metric':'VARCHAR',"
                    "'hk_type':'VARCHAR','value':'DOUBLE','text_value':'VARCHAR',"
                    "'unit':'VARCHAR','start_ts':'TIMESTAMP','end_ts':'TIMESTAMP',"
                    "'source_name':'VARCHAR','device_key':'VARCHAR','sync_path':'VARCHAR'}")

    t0 = time.time()
    f = open(staging, "w", newline="")
    w = csv.writer(f)
    context = ET.iterparse(str(xml_path), events=("start", "end"))
    _, root = next(context)  # grab the HealthData root so we can clear it
    for event, elem in context:
        if event != "end" or elem.tag != "Record":
            continue
        stats["records_seen"] += 1
        a = elem.attrib
        hk_type = a.get("type", "")
        if hk_type in wanted:
            stats["type_matched"] += 1
            src = a.get("sourceName", "unknown")
            try:
                raw = {
                    "hk_type": hk_type,
                    "source_name": src,
                    "unit": a.get("unit"),
                    "value": a.get("value"),
                    "start": parse_export_ts(a["startDate"], tz_cache),
                    "end": parse_export_ts(a["endDate"], tz_cache),
                }
                row = normalize_sample(raw, policy, registry, "health_export")
            except Exception:
                row = None
            if row is None:
                stats["ignored_source"][src] += 1
            else:
                stats["normalized"] += 1
                if row["device_key"] == "other":
                    stats["fallback_other"][src] += 1
                m = per_metric[row["metric"]]
                m["rows"] += 1
                s = str(row["start_ts"])
                m["min"] = s if m["min"] is None or s < m["min"] else m["min"]
                m["max"] = s if m["max"] is None or s > m["max"] else m["max"]
                w.writerow([_c(row[c]) for c in COLS])
        elem.clear()
        if stats["records_seen"] % 500_000 == 0:
            root.clear()
            rate = stats["records_seen"] / max(time.time() - t0, 1)
            log(f"import: {stats['records_seen']:,} records scanned, "
                f"{stats['normalized']:,} staged ({rate:,.0f} rec/s)")
        if limit and stats["records_seen"] >= limit:
            log(f"import: --limit {limit:,} reached, stopping scan")
            break
    f.close()

    if stats["normalized"]:
        log(f"import: bulk-loading {stats['normalized']:,} staged rows")
        before = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        conn.execute(
            f"INSERT OR IGNORE INTO samples ({', '.join(COLS)}) "
            f"SELECT * FROM read_csv('{staging}', header=false, nullstr='{NULLSTR}', "
            f"columns={imp_cols_sql})")
        after = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        stats["inserted"] = after - before
        stats["deduped"] = stats["normalized"] - stats["inserted"]
    os.remove(staging)
    stats["per_metric"] = {k: v for k, v in sorted(per_metric.items())}
    stats["ignored_source"] = dict(stats["ignored_source"])
    stats["fallback_other"] = dict(stats["fallback_other"])
    return stats


# --------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", required=True)
    ap.add_argument("--db", default=str(Path.home() / "Helios/data/helios.duckdb"))
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N XML records (smoke test); rerun without it, idempotent")
    args = ap.parse_args()

    xml_path = Path(args.xml).expanduser()
    if not xml_path.exists():
        sys.exit(f"export xml not found: {xml_path}")

    try:
        conn = duckdb.connect(args.db)
    except Exception as e:  # heliosd still holds the lock
        sys.exit(f"cannot open db exclusively (is heliosd stopped?): {e}")

    report: dict = {"started": str(datetime.now())}
    report["migration"] = migrate(conn)
    log("import: starting XML scan")
    report["import"] = run_import(conn, xml_path, args.limit)
    conn.execute("CHECKPOINT")
    conn.close()
    report["finished"] = str(datetime.now())

    Path("/tmp/helios_import_report.json").write_text(json.dumps(report, indent=2))
    log("report written to /tmp/helios_import_report.json")
    imp = report["import"]
    log(f"DONE. scanned={imp['records_seen']:,} matched={imp['type_matched']:,} "
        f"normalized={imp['normalized']:,} inserted={imp['inserted']:,} deduped={imp['deduped']:,}")
    if imp["fallback_other"]:
        log(f"sources that fell back to 'other': {imp['fallback_other']}")
    if imp["ignored_source"]:
        log(f"ignored sources: {imp['ignored_source']}")


if __name__ == "__main__":
    main()
