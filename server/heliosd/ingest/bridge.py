"""Bridge batch ingestion with an ack protocol: the Bridge advances its
HealthKit anchors only after the Mac confirms the batch is durably stored.

Phase 1a identity rule: a Bridge sample is `hk:<uuid>`. A uuid that already
exists in the store (under any id scheme, including the legacy ch2 twins) is
never inserted again, so no new duplicate can ever be created. Legacy history
is repaired in Phase 1b, not here.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from heliosd.ingest.normalize import normalize_sample
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

COLS = ["sample_id", "hk_uuid", "metric", "hk_type", "value", "text_value", "unit",
        "start_ts", "end_ts", "source_name", "device_key", "sync_path",
        "start_utc", "end_utc", "src_offset_min", "time_source", "content_hash", "unit_rule",
        "score_state", "quality", "batch_id", "sync_identifier", "sync_version", "writer_id"]
INSERT_SQL = (
    f"INSERT OR IGNORE INTO samples ({', '.join(COLS)}) "
    f"VALUES ({', '.join(['?'] * len(COLS))})"
)


def _existing_uuids(conn, uuids: list[str]) -> set[str]:
    """uuids already present in samples, any id scheme. Chunked so the IN list
    stays small; the hk_uuid index serves the lookup."""
    found: set[str] = set()
    for i in range(0, len(uuids), 1000):
        chunk = uuids[i:i + 1000]
        rows = conn.execute("SELECT DISTINCT hk_uuid FROM samples WHERE hk_uuid IN (SELECT unnest(?))",
                            [chunk]).fetchall()
        found.update(r[0] for r in rows)
    return found


def ingest_batch(conn, payload: dict, policy: MetricPolicy, registry: SourceRegistry,
                 sync_path: str = "bridge") -> dict:
    batch_id = payload.get("batch_id") or "no-id"
    rows, skipped_types = [], Counter()
    seen: set[str] = set()
    for raw in payload.get("samples", []):
        row = normalize_sample(raw, policy, registry, sync_path, batch_id)
        if row is None:
            skipped_types[str(raw.get("hk_type") or raw.get("type") or "?")] += 1
            continue
        if row["sample_id"] in seen:  # the same uuid twice in one batch
            continue
        seen.add(row["sample_id"])
        rows.append(row)
    deleted_ids = [u for u in payload.get("deleted", []) if u]

    with db.transaction(conn) as c:
        uuids = [r["hk_uuid"] for r in rows if r["hk_uuid"]]
        existing = _existing_uuids(c, uuids) if uuids else set()
        to_insert = [r for r in rows if not (r["hk_uuid"] and r["hk_uuid"] in existing)]
        guarded = len(rows) - len(to_insert)
        if to_insert:
            c.executemany(INSERT_SQL, [[r[col] for col in COLS] for r in to_insert])
        n_deleted = 0
        if deleted_ids:
            c.execute("DELETE FROM samples WHERE hk_uuid IN (SELECT unnest(?))", [deleted_ids])
            n_deleted = len(deleted_ids)
        # received_at is written explicitly in the same clock every other
        # timestamp in the store uses (local naive, datetime.now()). Relying on
        # the column's DEFAULT current_timestamp made the bridge age depend on
        # DuckDB's session TimeZone, which under launchd is not necessarily the
        # Mac's (audit B4).
        c.execute("INSERT OR REPLACE INTO sync_log (batch_id, received_at, sender, n_samples, n_deleted, sync_path, n_skipped, n_guarded) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  [batch_id, datetime.now(), payload.get("device", "unknown"), len(to_insert), n_deleted,
                   sync_path, sum(skipped_types.values()), guarded])

    dates = sorted({r["start_ts"].date() for r in to_insert} | {r["end_ts"].date() for r in to_insert if r["end_ts"]})
    return {"ack": True, "batch_id": batch_id, "accepted": len(to_insert),
            "deleted": n_deleted, "skipped": sum(skipped_types.values()),
            "skipped_types": dict(skipped_types), "guarded": guarded,
            "affected_dates": [str(d) for d in dates],
            "date_min": str(dates[0]) if dates else None,
            "date_max": str(dates[-1]) if dates else None}
