"""Bridge batch ingestion with an ack protocol: the Bridge advances its
HealthKit anchors only after the Mac confirms the batch is durably stored.

Phase 1a rules (single-source plan v2, items 2 and 3):
- Identity: a Bridge sample is `hk:<uuid>`. A uuid already present in the store
  under any id scheme (including the legacy ch2 twins) is never inserted again,
  so no new duplicate can form. History itself is repaired in Phase 1b.
- Deletions leave a tombstone for EVERY deleted uuid, whether or not a row
  exists yet (a deletion can be delivered before its insert), and the insert
  guard refuses tombstoned uuids. Replaying an old batch after a deletion can
  therefore never resurrect the sample.
- One lock, one transaction: tombstones, the physical delete, the guard, the
  inserts, the dirty-date journal and the batch receipt commit together. The
  ack is sent only after the commit, so a failure anywhere leaves nothing
  behind and the Bridge retries the whole batch.
- The dirty-date journal (dirty_dates) is the durable hand-off to recompute:
  every reporting date touched by an insert or a delete is recorded here, in
  the same transaction, and removed only after a successful recompute pass.

Phase 1b (step 2, the re-read landing): the guard outcomes are disjoint and
counted (new, landed, native, tombstoned, deleted in batch). A guarded row
whose uuid the store holds and that is neither tombstoned nor deleted in the
batch is handed to heliosd.ingest.landing inside the same transaction, which
records the re-delivered observation beside the legacy row instead of
dropping it (hk_reread, hk_reread_variants). The ack and sync_log carry the
counts; no samples row changes because of a landing.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime

from heliosd.ingest import landing
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


def _in_chunks(conn, sql: str, values: list[str]) -> set:
    found: set = set()
    for i in range(0, len(values), 1000):
        found.update(r[0] for r in conn.execute(sql, [values[i:i + 1000]]).fetchall())
    return found


def _existing_uuids(conn, uuids: list[str]) -> set[str]:
    """uuids already present in samples, any id scheme (hk_uuid index)."""
    return _in_chunks(conn, "SELECT DISTINCT hk_uuid FROM samples WHERE hk_uuid IN (SELECT unnest(?))", uuids) if uuids else set()


def _tombstoned_uuids(conn, uuids: list[str]) -> set[str]:
    return _in_chunks(conn, "SELECT DISTINCT hk_uuid FROM tombstones WHERE hk_uuid IN (SELECT unnest(?))", uuids) if uuids else set()


def _journal(conn, dates: set[date], reason: str, batch_id: str) -> None:
    """Record touched reporting dates in the ingest transaction. Separate
    function so a test can inject a failure here and prove nothing else of
    the batch survived."""
    if dates:
        # enqueued_at is stamped here: DuckDB 1.5.4 keeps the old DEFAULT value
        # on INSERT OR REPLACE, and the drain relies on a replaced row being newer.
        now = datetime.now()
        conn.executemany("INSERT OR REPLACE INTO dirty_dates (date, reason, batch_id, enqueued_at) VALUES (?, ?, ?, ?)",
                         [[d, reason, batch_id, now] for d in sorted(dates)])


def ingest_batch(conn, payload: dict, policy: MetricPolicy, registry: SourceRegistry,
                 sync_path: str = "bridge") -> dict:
    batch_id = payload.get("batch_id") or "no-id"
    rows, skipped_types = [], Counter()
    seen: dict[str, list[dict]] = {}
    dup_rows: list[dict] = []
    for raw in payload.get("samples", []):
        # Identity on the Bridge path is the HealthKit uuid and nothing else
        # (checkpoint B, point 9): a row without one is counted, not stored.
        if sync_path == "bridge" and raw.get("uuid") in (None, ""):
            skipped_types["no_uuid"] += 1
            continue
        row = normalize_sample(raw, policy, registry, sync_path, batch_id)
        if row is None:
            skipped_types[str(raw.get("hk_type") or raw.get("type") or "?")] += 1
            continue
        # The delivered strings, verbatim: evidence for the landing (never a key).
        row["start_raw"] = landing.raw_string(raw.get("start") or raw.get("start_ts"))
        row["end_raw"] = landing.raw_string(raw.get("end") or raw.get("end_ts"))
        key = row["hk_uuid"] or row["sample_id"]
        if key in seen:
            # The same uuid twice in one page: the first is the row the insert
            # path sees; a second content is kept for the landing (a variant),
            # an identical repeat is counted and dropped (checkpoint B, point 9).
            if row["hk_uuid"] and not any(landing.same_content(row, r) for r in seen[key]):
                seen[key].append(row)
                dup_rows.append(row)
            else:
                skipped_types["dup_in_batch_identical"] += 1
            continue
        seen[key] = [row]
        rows.append(row)
    deleted_ids = sorted({u for u in payload.get("deleted", []) if u})

    with db.transaction(conn) as c:
        # 1. Deletions: tombstone every uuid (row or not), remember the dates
        #    of the rows about to vanish, then delete them.
        deleted_dates: set[date] = set()
        if deleted_ids:
            victims = c.execute("SELECT hk_uuid, metric, start_utc, start_ts, end_ts FROM samples "
                                "WHERE hk_uuid IN (SELECT unnest(?))", [deleted_ids]).fetchall()
            meta = {}
            for uuid, metric, start_utc, start_ts, end_ts in victims:
                meta.setdefault(uuid, (metric, start_utc))
                deleted_dates.add(start_ts.date())
                if end_ts is not None:
                    deleted_dates.add(end_ts.date())
            c.executemany("INSERT OR IGNORE INTO tombstones (tomb_id, hk_uuid, metric, start_utc, reason, batch_id, deleted_at) "
                          "VALUES (?, ?, ?, ?, 'bridge_deleted', ?, ?)",
                          [[f"hk:{u}", u, meta.get(u, (None, None))[0], meta.get(u, (None, None))[1], batch_id, datetime.now()]
                           for u in deleted_ids])
            c.execute("DELETE FROM samples WHERE hk_uuid IN (SELECT unnest(?))", [deleted_ids])
        # 2. Guard: never a second row for a uuid, never a tombstoned uuid.
        #    Outcomes are disjoint, in this precedence: deleted in this batch,
        #    tombstoned, existing (landed, Phase 1b), new.
        uuids = [r["hk_uuid"] for r in rows if r["hk_uuid"]]
        existing = _existing_uuids(c, uuids)
        tombstoned = _tombstoned_uuids(c, uuids)
        deleted = set(deleted_ids)
        to_insert, to_land = [], []
        outcomes = Counter()
        for r in rows:
            u = r["hk_uuid"]
            if u and u in deleted:
                outcomes["deleted_in_batch"] += 1
            elif u and u in tombstoned:
                outcomes["tombstoned"] += 1
            elif u and u in existing:
                to_land.append(r)
            else:
                to_insert.append(r)
                outcomes["new"] += 1
        guarded = len(rows) - len(to_insert)
        if to_insert:
            c.executemany(INSERT_SQL, [[r[col] for col in COLS] for r in to_insert])
        # Second contents of a uuid inside the page land after the insert, so
        # the landing sees the uuid as existing (new or legacy) and keeps them.
        for r in dup_rows:
            if r["hk_uuid"] not in deleted and r["hk_uuid"] not in tombstoned:
                to_land.append(r)
        landed = landing.land(c, to_land, batch_id, datetime.now())
        outcomes.update({k: v for k, v in landed["outcomes"].items() if v})     # only outcomes that happened
        writes = landed["writes"]
        # The batch's landed count, not the attempt's: a retried batch (an
        # outbox retry after a lost ack) replaces its own receipt, and the rows
        # it recorded the first time still count as landed by this batch
        # (classification counts, never write counts; checkpoint B point 10).
        n_landed = sum(v for k, v in landed["outcomes"].items() if k != "native_identical")
        # 3. Journal the touched reporting dates (inserts and deletes).
        inserted_dates = {r["start_ts"].date() for r in to_insert} | {r["end_ts"].date() for r in to_insert if r["end_ts"]}
        _journal(c, inserted_dates, "ingest", batch_id)
        _journal(c, deleted_dates, "delete", batch_id)
        # 4. Receipt. received_at is written explicitly in the store's own
        #    clock (local naive) rather than DuckDB's session zone (audit B4).
        c.execute("INSERT OR REPLACE INTO sync_log (batch_id, received_at, sender, n_samples, n_deleted, sync_path, "
                  "n_skipped, n_guarded, n_landed, guard_outcomes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  [batch_id, datetime.now(), payload.get("device", "unknown"), len(to_insert), len(deleted_ids),
                   sync_path, sum(skipped_types.values()), guarded, n_landed, json.dumps(dict(outcomes), sort_keys=True)])

    dates = sorted(inserted_dates | deleted_dates)
    ins = sorted(inserted_dates)
    return {"ack": True, "batch_id": batch_id, "accepted": len(to_insert),
            "deleted": len(deleted_ids), "skipped": sum(skipped_types.values()),
            "skipped_types": dict(skipped_types), "guarded": guarded,
            "landed": n_landed, "guard_outcomes": dict(outcomes), "writes": writes,
            "affected_dates": [str(d) for d in dates],
            "date_min": str(ins[0]) if ins else None,
            "date_max": str(ins[-1]) if ins else None}
