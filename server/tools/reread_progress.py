#!/usr/bin/env python
"""Phase 1b step 2 (the owner gate): progress of the HealthKit re-read, read
through the daemon's read-only SQL tool (never the store file; the daemon
holds the lock). Per HealthKit type: legacy uuids the store holds, uuids the
re-read has landed (hk_reread), coverage, variants, new rows inserted since
the prep deploy, plus batches per hour and minutes since the last batch.

    server/.venv/bin/python server/tools/reread_progress.py [--since ISO] [--threshold 0.95]
        [--quiet-minutes 15] [--out FILE.json] [--json]

Exit 0 when every type is at or above the threshold and no batch has arrived
for --quiet-minutes (the re-read is complete), 2 while it is in progress, 1 on
an error. Counts only; no sample values; the token stays in the process.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SINCE_FILE = Path("/tmp/helios-build/phase-1b/deploy-since.txt")
# The store's own stamp of the prep deploy (schema v3 was created at the first
# start of the 1b code): the default --since when the /tmp file is gone (a reboot
# wipes /tmp; it did on 2026-10-06 11:24, and the tool then counted every
# bridge_utc row ever inserted as "new"). Version 3 exactly: a later version's
# row carries the stamp of the start that upgraded to it (schema v4, Wave 2),
# which is not the prep deploy.
SINCE_SQL = "SELECT CAST(MAX(applied_at) AS VARCHAR) AS since FROM schema_version WHERE version = 3"
# "landed": only landings whose uuid is STILL a legacy row count toward
# coverage: a uuid landed and then deleted on the phone is not evidence for the
# remaining rows (checkpoint B point 21). Typed by the sample's own hk_type.
# Keep SQL comments OUT of these strings: the CLI collapses the statement to one
# line for the daemon's SQL tool, and a "--" comment then swallows the rest of
# the query (DuckDB "syntax error at end of input", 2026-10-06). cli_sql()
# strips comment lines anyway, and the test runs the statements through it.
COVERAGE_SQL = """
WITH legacy AS (
    SELECT hk_type, COUNT(DISTINCT hk_uuid) AS legacy_uuids
    FROM samples WHERE sync_path = 'bridge' AND hk_uuid IS NOT NULL AND (time_source IS NULL OR time_source = 'era_rebase_v1')
    GROUP BY 1),
landed AS (
    SELECT s.hk_type, COUNT(DISTINCT h.hk_uuid) AS landed, SUM(h.n_seen) AS observations, MAX(h.last_seen) AS last_landed,
           COUNT(DISTINCT h.hk_uuid) FILTER (WHERE h.time_source = 'bridge_utc') AS confirmed
    FROM hk_reread h JOIN samples s ON s.hk_uuid = h.hk_uuid
    WHERE s.sync_path = 'bridge' AND (s.time_source IS NULL OR s.time_source = 'era_rebase_v1') GROUP BY 1),
orphans AS (SELECT COUNT(*) AS n FROM hk_reread h WHERE NOT EXISTS (SELECT 1 FROM samples s WHERE s.hk_uuid = h.hk_uuid)),
variants AS (SELECT hk_type, COUNT(*) AS variants, COUNT(*) FILTER (WHERE existing_time_source = 'legacy') AS variants_legacy FROM hk_reread_variants GROUP BY 1),
unconfirmed AS (SELECT hk_type, COUNT(*) AS unconfirmed FROM hk_reread WHERE time_source IS DISTINCT FROM 'bridge_utc' GROUP BY 1),
fresh AS (SELECT hk_type, COUNT(*) AS new_rows FROM samples WHERE sync_path = 'bridge' AND time_source = 'bridge_utc' {since_clause} GROUP BY 1)
SELECT l.hk_type, l.legacy_uuids, COALESCE(d.landed, 0) AS landed, COALESCE(d.observations, 0) AS observations,
       COALESCE(d.confirmed, 0) AS confirmed, COALESCE(v.variants, 0) AS variants, COALESCE(v.variants_legacy, 0) AS variants_legacy,
       COALESCE(u.unconfirmed, 0) AS unconfirmed, COALESCE(f.new_rows, 0) AS new_rows,
       CAST(d.last_landed AS VARCHAR) AS last_landed, (SELECT n FROM orphans) AS landed_then_deleted_total
FROM legacy l LEFT JOIN landed d ON d.hk_type = l.hk_type LEFT JOIN variants v ON v.hk_type = l.hk_type
     LEFT JOIN unconfirmed u ON u.hk_type = l.hk_type LEFT JOIN fresh f ON f.hk_type = l.hk_type
ORDER BY l.legacy_uuids
"""
BATCH_SQL = """
SELECT COUNT(*) FILTER (WHERE received_at >= now()::TIMESTAMP - INTERVAL 1 HOUR) AS batches_last_hour,
       COALESCE(SUM(n_landed) FILTER (WHERE received_at >= now()::TIMESTAMP - INTERVAL 1 HOUR), 0) AS landed_last_hour,
       COALESCE(SUM(n_samples) FILTER (WHERE received_at >= now()::TIMESTAMP - INTERVAL 1 HOUR), 0) AS inserted_last_hour,
       CAST(MAX(received_at) AS VARCHAR) AS last_batch,
       CAST(now()::TIMESTAMP AS VARCHAR) AS store_now,
       (SELECT COUNT(*) FROM hk_reread) AS hk_reread_rows,
       (SELECT COUNT(*) FROM hk_reread_variants) AS variant_rows,
       (SELECT COUNT(*) FROM hk_reread_variants WHERE existing_time_source <> 'legacy') AS native_variant_rows,
       COUNT(*) AS bridge_receipts_total
FROM sync_log WHERE sync_path = 'bridge'
"""


def cli_sql(sql: str) -> str:
    """The statement as the CLI sends it: comment lines dropped, whitespace
    collapsed to one line (the daemon's SQL tool takes one statement)."""
    kept = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    return " ".join(" ".join(kept).split())


def default_since(q) -> str | None:
    """--since when neither the flag nor the /tmp file gives one: the store's
    schema v3 stamp (None when the store predates Phase 1b)."""
    rows = q(SINCE_SQL)
    return rows[0].get("since") if rows else None


def progress(q, since: str | None = None, threshold: float = 0.95, quiet_minutes: int = 15) -> dict:
    """q(sql) -> list of dict rows. Pure: the same function serves the CLI
    (through the daemon) and the tests (through a connection)."""
    since_clause = f"AND ingested_at >= TIMESTAMP '{since}'" if since else ""
    types = q(COVERAGE_SQL.format(since_clause=since_clause))
    for t in types:
        t["coverage"] = round(t["landed"] / t["legacy_uuids"], 4) if t["legacy_uuids"] else None
        t["done"] = bool(t["coverage"] is not None and t["coverage"] >= threshold)
    b = (q(BATCH_SQL) or [{}])[0]
    minutes = None
    if b.get("last_batch") and b.get("store_now"):
        last = dt.datetime.fromisoformat(b["last_batch"])
        now = dt.datetime.fromisoformat(b["store_now"])
        minutes = round((now - last).total_seconds() / 60.0, 1)
    all_done = bool(types) and all(t["done"] for t in types)
    quiet = minutes is not None and minutes >= quiet_minutes
    # Quiet means inactivity, not completion: quiet below the threshold is a
    # stall (the phone locked, the link down), quiet above it is complete.
    complete = all_done and quiet
    stalled = quiet and not all_done
    return {"taken_at": dt.datetime.now().isoformat(timespec="seconds"), "since": since, "threshold": threshold,
            "quiet_minutes": quiet_minutes, "types": types, "batches": b, "minutes_since_last_batch": minutes,
            "types_done": sum(1 for t in types if t["done"]), "types_total": len(types),
            "all_types_done": all_done, "quiet": quiet, "stalled": stalled, "complete": complete,
            "landed_then_deleted_total": types[0]["landed_then_deleted_total"] if types else 0}


def render(p: dict) -> str:
    since_from = f", {p['since_from']}" if p.get("since_from") else ""
    lines = [f"re-read progress at {p['taken_at']} (since {p['since'] or 'the beginning'}{since_from}; threshold {p['threshold']:.0%}, quiet {p['quiet_minutes']} min)",
             f"{'type':46} {'legacy':>9} {'landed':>9} {'cover':>7} {'conf':>9} {'obs':>9} {'var':>5} {'varL':>5} {'unconf':>6} {'new':>7}  last landed"]
    for t in p["types"]:
        cov = f"{t['coverage']:.1%}" if t["coverage"] is not None else "n/a"
        lines.append(f"{t['hk_type']:46} {t['legacy_uuids']:>9} {t['landed']:>9} {cov:>7} {t.get('confirmed', 0):>9} {t['observations']:>9} {t['variants']:>5} {t.get('variants_legacy', 0):>5} {t['unconfirmed']:>6} {t['new_rows']:>7}  {t['last_landed'] or '-'}")
    b = p["batches"]
    lines.append(f"batches last hour {b.get('batches_last_hour')}, landed {b.get('landed_last_hour')}, inserted {b.get('inserted_last_hour')}; "
                 f"last batch {b.get('last_batch') or '-'} ({p['minutes_since_last_batch']} min ago)")
    lines.append(f"growth: hk_reread rows {b.get('hk_reread_rows')}, variant rows {b.get('variant_rows')} (native {b.get('native_variant_rows')}), "
                 f"bridge receipts {b.get('bridge_receipts_total')}")
    lines.append(f"types done {p['types_done']} of {p['types_total']}; quiet: {p['quiet']}; stalled: {p['stalled']}; complete: {p['complete']}; "
                 f"landings whose uuid is gone: {p['landed_then_deleted_total']}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="ISO timestamp (store clock) of the prep deploy; default from " + str(SINCE_FILE))
    ap.add_argument("--threshold", type=float, default=0.95)
    ap.add_argument("--quiet-minutes", type=int, default=15)
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    since = args.since or (SINCE_FILE.read_text().strip() if SINCE_FILE.exists() else None)
    since_from = "--since" if args.since else ("deploy-since.txt" if since else "store schema v3 stamp")
    import httpx  # noqa: E402
    from heliosd.config import load_settings  # noqa: E402
    st = load_settings()
    client = httpx.Client(base_url=f"https://127.0.0.1:{st.port}", verify=False, timeout=600,
                          headers={"X-Helios-Token": st.ingest_token})

    def q(sql: str) -> list[dict]:
        r = client.post("/api/tool/sql", json={"query": cli_sql(sql)})
        r.raise_for_status()
        return r.json()
    try:
        if since is None:
            since = default_since(q)
        p = progress(q, since, args.threshold, args.quiet_minutes)
        p["since_from"] = since_from
    except Exception as e:  # noqa: BLE001
        print(f"error: {type(e).__name__}: {str(e)[:200]}")
        sys.exit(1)
    if args.out:
        Path(args.out).write_text(json.dumps(p, indent=2))
    print(json.dumps(p, indent=2) if args.json else render(p))
    sys.exit(0 if p["complete"] else 2)


if __name__ == "__main__":
    main()
