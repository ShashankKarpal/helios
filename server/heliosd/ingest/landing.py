"""Phase 1b step 2: the HealthKit re-read landing (design.md section 5,
adjudication-A points 17, 18, 19, 21; checkpoint B points 9, 10, 11).

The 1a insert guard refuses any uuid the store already holds, so during the
owner's "Reset & re-pull" every re-delivered sample would vanish on arrival and
the migration would have nothing to compare the era rule against. Instead a
guarded row is classified into one of these disjoint outcomes:

- deleted_in_batch: the same batch deletes the uuid (precedence over all);
- tombstoned: a tombstone exists for the uuid; never landed, only counted;
- legacy: the existing row(s) carry time_source NULL or era_rebase_v1. The
  observation is recorded in hk_reread with the NORMALIZED fields a fresh
  insert would get (UTC instants, unit rule, quality, device key), its own
  time_source (bridge_utc, or assumed_reporting_wall for an offset-free input,
  which the migration never takes as confirmation) and the delivered start
  and end strings verbatim. One row per uuid holds the FIRST observation; an
  identical later observation from a batch not yet in `batches` appends that
  batch (n_seen is the number of distinct batches); the same batch again, an
  outbox retry, adjacent or not, changes nothing; differing content goes to
  hk_reread_variants with the next seq, once per distinct content;
- native: the existing row is already an instant row (bridge_utc, or
  bridge_reread_v1 after the migration). Nothing to learn from an identical
  re-delivery (the 48-hour foreground sweeps re-deliver recent rows all the
  time); a differing one is kept as a variant for a later reconciliation;
- new_same_batch: the existing row is native AND carries this very batch id: a
  retry (lost ack) re-delivering rows the batch itself inserted the first time.
  Counted as the batch's inserts again so the receipt and the ack stay a ledger
  of what the batch committed, never of the attempt (checkpoint C point 6).

A page that carries one uuid twice with DIFFERENT content keeps both: the
first is the row the insert path sees, the second is landed like any other
re-delivery (a variant), so no conflicting observation is discarded
(checkpoint B point 9).

Set-based per batch: the rows go into a temp table, one query classifies them
against samples and hk_reread, and three statements write. Runs inside the
batch transaction on the raw connection (the caller holds the store lock), so
the landing commits or rolls back with the batch receipt. The landing never
changes a samples row: the migration tool reads hk_reread offline, the re-read
wins there, and the rows it consumed move to the lineage archive.
"""

from __future__ import annotations

from datetime import datetime

LEGACY_TIME_SOURCE = "era_rebase_v1"      # besides NULL: a rebased row the re-read has not confirmed yet
LANDED = ["hk_uuid", "hk_type", "metric", "value", "text_value", "unit", "start_utc", "end_utc",
          "start_raw", "end_raw", "source_name", "device_key", "quality", "unit_rule", "time_source"]
# What makes two observations of one uuid "the same": every normalized field.
# The raw strings and the provenance label are evidence, not identity.
CONTENT = ["hk_type", "metric", "value", "text_value", "unit", "start_utc", "end_utc",
           "source_name", "device_key", "quality", "unit_rule"]
OUTCOMES = ("new", "new_same_batch", "landed_first", "landed_repeat", "landed_same_batch", "landed_variant",
            "native_identical", "native_variant", "tombstoned", "deleted_in_batch")
_BATCH_DDL = ("CREATE OR REPLACE TEMP TABLE landing_batch (pos INTEGER, hk_uuid VARCHAR, hk_type VARCHAR, metric VARCHAR, "
              "value DOUBLE, text_value VARCHAR, unit VARCHAR, start_utc TIMESTAMP, end_utc TIMESTAMP, "
              "start_raw VARCHAR, end_raw VARCHAR, source_name VARCHAR, device_key VARCHAR, quality VARCHAR, "
              "unit_rule VARCHAR, time_source VARCHAR)")


def _same(a: str, b: str) -> str:
    """NULL-safe equality of every content column between two row aliases."""
    return " AND ".join(f"{a}.{c} IS NOT DISTINCT FROM {b}.{c}" for c in CONTENT)


def raw_string(v) -> str | None:
    """The delivered timestamp exactly as it arrived (string or datetime)."""
    if v is None:
        return None
    return v.isoformat() if isinstance(v, datetime) else str(v)


def same_content(a: dict, b: dict) -> bool:
    return all(a.get(c) == b.get(c) for c in CONTENT)


def land(c, rows: list[dict], batch_id: str, now: datetime) -> dict:
    """Record the guarded rows whose uuid exists in samples (neither tombstoned
    nor deleted in this batch). `rows` are normalize_sample outputs with
    start_raw and end_raw added, in page order; a uuid may appear more than
    once with different content. Returns {"outcomes": disjoint counts per
    row, "writes": statement counts}. Inside db.transaction, raw connection."""
    outcomes = {"landed_first": 0, "landed_repeat": 0, "landed_same_batch": 0, "landed_variant": 0,
                "native_identical": 0, "native_variant": 0, "new_same_batch": 0}
    writes = {"variants_written": 0}
    if not rows:
        return {"outcomes": outcomes, "writes": writes}
    c.execute(_BATCH_DDL)
    c.executemany(f"INSERT INTO landing_batch (pos, {', '.join(LANDED)}) VALUES ({', '.join(['?'] * (len(LANDED) + 1))})",
                  [[i] + [r.get(k) for k in LANDED] for i, r in enumerate(rows)])
    # One row per distinct (uuid, content), first in page order; the first
    # content of a uuid is its primary observation, later ones are variants.
    c.execute(f"""
        CREATE OR REPLACE TEMP TABLE landing_d AS
        SELECT *, row_number() OVER (PARTITION BY hk_uuid ORDER BY pos) AS rnk FROM (
            SELECT * FROM landing_batch QUALIFY row_number() OVER (PARTITION BY hk_uuid, {', '.join(CONTENT)} ORDER BY pos) = 1)""")
    # One classification per uuid: what the store holds (multiplicity, time
    # sources, content equality) and what hk_reread already recorded.
    c.execute(f"""
        CREATE OR REPLACE TEMP TABLE landing_class AS
        WITH e AS (
            SELECT b.hk_uuid,
                   COUNT(s.sample_id) AS existing_rows,
                   array_to_string(list_sort(list_distinct(list(COALESCE(s.time_source, 'legacy')))), ',') AS existing_time_source,
                   bool_and(s.time_source IS NULL OR s.time_source = '{LEGACY_TIME_SOURCE}') AS is_legacy,
                   bool_or({_same('s', 'b')}) AS same_as_stored,
                   COALESCE(bool_or(s.batch_id = ?), FALSE) AS by_this_batch   -- NULL batch_id (a legacy row): never this batch
            FROM landing_d b JOIN samples s ON s.hk_uuid = b.hk_uuid WHERE b.rnk = 1
            GROUP BY b.hk_uuid)
        SELECT b.hk_uuid, e.existing_rows, e.existing_time_source, e.is_legacy, e.same_as_stored, e.by_this_batch,
               h.hk_uuid IS NOT NULL AS seen_before,
               CASE WHEN h.hk_uuid IS NULL THEN NULL ELSE ({_same('h', 'b')}) END AS same_as_landed,
               CASE WHEN h.hk_uuid IS NULL THEN NULL ELSE list_contains(h.batches, ?) END AS batch_known
        FROM landing_d b
        JOIN e ON e.hk_uuid = b.hk_uuid
        LEFT JOIN hk_reread h ON h.hk_uuid = b.hk_uuid
        WHERE b.rnk = 1""", [batch_id, batch_id])
    cls = c.execute("""
        SELECT COUNT(*) FILTER (WHERE is_legacy AND NOT seen_before),
               COUNT(*) FILTER (WHERE is_legacy AND seen_before AND same_as_landed AND NOT batch_known),
               COUNT(*) FILTER (WHERE is_legacy AND seen_before AND same_as_landed AND batch_known),
               COUNT(*) FILTER (WHERE is_legacy AND seen_before AND NOT same_as_landed),
               COUNT(*) FILTER (WHERE NOT is_legacy AND same_as_stored AND NOT by_this_batch),
               COUNT(*) FILTER (WHERE NOT is_legacy AND NOT same_as_stored),
               COUNT(*) FILTER (WHERE NOT is_legacy AND same_as_stored AND by_this_batch)
        FROM landing_class""").fetchone()
    (outcomes["landed_first"], outcomes["landed_repeat"], outcomes["landed_same_batch"], outcomes["landed_variant"],
     outcomes["native_identical"], outcomes["native_variant"], outcomes["new_same_batch"]) = [int(x) for x in cls]
    extra = c.execute("SELECT COUNT(*) FROM landing_d WHERE rnk > 1").fetchone()[0]
    outcomes["landed_variant"] += int(extra)        # a second content of one uuid inside the page
    # 1. First observation of a legacy uuid.
    c.execute(f"""
        INSERT INTO hk_reread ({', '.join(LANDED)}, existing_rows, existing_time_source, batches,
                               first_batch, last_batch, first_seen, last_seen, n_seen)
        SELECT {', '.join('b.' + k for k in LANDED)}, k.existing_rows, k.existing_time_source, [?], ?, ?, ?, ?, 1
        FROM landing_d b JOIN landing_class k ON k.hk_uuid = b.hk_uuid
        WHERE b.rnk = 1 AND k.is_legacy AND NOT k.seen_before""", [batch_id, batch_id, batch_id, now, now])
    # 2. The same observation again from a batch not seen before: one more
    #    distinct delivery. The first observation's fields are never rewritten.
    c.execute("""
        UPDATE hk_reread SET batches = list_append(batches, ?), n_seen = len(batches) + 1, last_seen = ?, last_batch = ?
        FROM landing_class k
        WHERE hk_reread.hk_uuid = k.hk_uuid AND k.is_legacy AND k.seen_before AND k.same_as_landed AND NOT k.batch_known""",
        [batch_id, now, batch_id])
    # 3. Differing content (a legacy uuid seen before with other content, a
    #    native row re-delivered with other content, or a second content of
    #    one uuid inside the page): one variant per distinct content, so a
    #    retried batch never writes a twin variant.
    content_cols = ["hk_type", "metric", "value", "text_value", "unit", "start_utc", "end_utc",
                    "start_raw", "end_raw", "source_name", "device_key", "quality", "unit_rule", "time_source"]
    written = c.execute(f"""
        INSERT INTO hk_reread_variants (hk_uuid, seq, {', '.join(content_cols)}, existing_time_source, batch_id, seen_at)
        SELECT b.hk_uuid,
               COALESCE((SELECT MAX(v.seq) FROM hk_reread_variants v WHERE v.hk_uuid = b.hk_uuid), 0) + b.rnk,
               {', '.join('b.' + k for k in content_cols)}, k.existing_time_source, ?, ?
        FROM landing_d b JOIN landing_class k ON k.hk_uuid = b.hk_uuid
        WHERE (b.rnk > 1
               OR (k.is_legacy AND k.seen_before AND NOT k.same_as_landed)
               OR (NOT k.is_legacy AND NOT k.same_as_stored))
          AND NOT EXISTS (SELECT 1 FROM hk_reread_variants v WHERE v.hk_uuid = b.hk_uuid AND {_same('v', 'b')})
          AND NOT EXISTS (SELECT 1 FROM hk_reread h WHERE h.hk_uuid = b.hk_uuid AND b.rnk > 1 AND {_same('h', 'b')})""",
        [batch_id, now]).fetchone()
    writes["variants_written"] = int(written[0]) if written else 0
    c.execute("DROP TABLE IF EXISTS landing_class")
    c.execute("DROP TABLE IF EXISTS landing_d")
    c.execute("DROP TABLE IF EXISTS landing_batch")
    return {"outcomes": outcomes, "writes": writes}
