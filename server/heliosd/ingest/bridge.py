"""Bridge batch ingestion with an ack protocol: the Bridge advances its
HealthKit anchors only after the Mac confirms the batch is durably stored."""

from __future__ import annotations

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
        db.execute(conn, "BEGIN")
        try:
            for r in rows:
                db.execute(conn, INSERT_SQL, r)
            db.execute(conn, "COMMIT")
        except Exception:
            db.execute(conn, "ROLLBACK")
            raise
    deleted_ids = [u for u in payload.get("deleted", []) if u]
    n_deleted = 0
    if deleted_ids:
        placeholders = ", ".join(["?"] * len(deleted_ids))
        db.execute(conn, f"DELETE FROM samples WHERE hk_uuid IN ({placeholders})", deleted_ids)
        n_deleted = len(deleted_ids)
    db.execute(conn,
               "INSERT OR REPLACE INTO sync_log (batch_id, sender, n_samples, n_deleted, sync_path) "
               "VALUES (?, ?, ?, ?, ?)",
               [batch_id, payload.get("device", "unknown"), len(rows), n_deleted, sync_path])
    return {"ack": True, "batch_id": batch_id, "accepted": len(rows),
            "deleted": n_deleted, "skipped": skipped}
