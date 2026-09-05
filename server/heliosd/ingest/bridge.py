"""Bridge batch ingestion with an ack protocol: the Bridge advances its
HealthKit anchors only after the Mac confirms the batch is durably stored."""

from __future__ import annotations

from datetime import datetime

from heliosd.ingest.normalize import normalize_sample
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

COLS = ["sample_id", "hk_uuid", "metric", "hk_type", "value", "text_value", "unit",
        "start_ts", "end_ts", "source_name", "device_key", "sync_path"]
INSERT_SQL = (
    f"INSERT OR IGNORE INTO samples ({', '.join(COLS)}) "
    f"VALUES ({', '.join(['?'] * len(COLS))})"
)


def ingest_batch(conn, payload: dict, policy: MetricPolicy, registry: SourceRegistry,
                 sync_path: str = "bridge") -> dict:
    batch_id = payload.get("batch_id") or "no-id"
    rows, skipped = [], 0
    for raw in payload.get("samples", []):
        row = normalize_sample(raw, policy, registry, sync_path)
        if row is None:
            skipped += 1
            continue
        rows.append([row[c] for c in COLS])
    if rows:
        db.insert_batch(conn, INSERT_SQL, rows)
    deleted_ids = [u for u in payload.get("deleted", []) if u]
    n_deleted = 0
    if deleted_ids:
        placeholders = ", ".join(["?"] * len(deleted_ids))
        db.execute(conn, f"DELETE FROM samples WHERE hk_uuid IN ({placeholders})", deleted_ids)
        n_deleted = len(deleted_ids)
    # received_at is written explicitly in the same clock every other timestamp
    # in the store uses (local naive, datetime.now()). Relying on the column's
    # DEFAULT current_timestamp made the bridge age depend on DuckDB's session
    # TimeZone, which under launchd is not necessarily the Mac's (audit B4).
    db.execute(conn,
               "INSERT OR REPLACE INTO sync_log (batch_id, received_at, sender, n_samples, n_deleted, sync_path) "
               "VALUES (?, ?, ?, ?, ?, ?)",
               [batch_id, datetime.now(), payload.get("device", "unknown"), len(rows), n_deleted, sync_path])
    ts_idx = COLS.index("start_ts")
    dates = [r[ts_idx].date() for r in rows if r[ts_idx] is not None]
    return {"ack": True, "batch_id": batch_id, "accepted": len(rows),
            "deleted": n_deleted, "skipped": skipped,
            "date_min": str(min(dates)) if dates else None,
            "date_max": str(max(dates)) if dates else None}
