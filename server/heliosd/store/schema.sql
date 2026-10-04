-- Helios store. All health data at rest lives here, on local disk.
--
-- Single-source Phase 1a (2026-10-04): identity, time and deletion columns are
-- ADDITIVE and nullable so history is untouched until Phase 1b rebases it.
-- Every statement here is idempotent; the daemon runs this file on every start.
-- Tables come before the view that reads them. No foreign keys anywhere
-- (DuckDB 1.5 rejects delete-then-delete across a foreign key in one
-- transaction; see docs/briefs/build-2026-10-04/phase-1a/adjudication-A.md).

CREATE TABLE IF NOT EXISTS samples (
    sample_id     VARCHAR PRIMARY KEY,   -- identity. Prefix is the scheme: hk:<HealthKit uuid> (Bridge),
                                         -- wh:<metric>:<kind>:<record id> (Whoop API), xp:<export sha256[:16]>:<row>
                                         -- (export, Phase 2), ch2:<content hash> (legacy rows until Phase 1b)
    hk_uuid       VARCHAR,               -- HealthKit sample UUID (deletion handling)
    metric        VARCHAR NOT NULL,      -- canonical metric id
    hk_type       VARCHAR,               -- original HealthKit identifier (null for whoop-native)
    value         DOUBLE,
    text_value    VARCHAR,               -- category values, e.g. sleep stage names
    unit          VARCHAR,
    start_ts      TIMESTAMP NOT NULL,    -- wall time in the REPORTING zone (new rows); Mac-local wall (legacy rows).
    end_ts        TIMESTAMP,             -- Display and day bucketing only. Never part of a key.
    source_name   VARCHAR NOT NULL,      -- raw Apple Health source string
    device_key    VARCHAR NOT NULL,      -- resolved via source_registry ('excluded' when ignored_mode is store)
    sync_path     VARCHAR NOT NULL,      -- bridge | whoop_live | backfill | legacy_import | manual | health_export
    ingested_at   TIMESTAMP DEFAULT current_timestamp
);
CREATE INDEX IF NOT EXISTS idx_samples_metric_ts ON samples (metric, start_ts);
CREATE INDEX IF NOT EXISTS idx_samples_hk_uuid ON samples (hk_uuid);
CREATE INDEX IF NOT EXISTS idx_samples_device ON samples (device_key, metric, start_ts);

-- Phase 1a additions (all nullable; NULL marks a legacy row until Phase 1b).
ALTER TABLE samples ADD COLUMN IF NOT EXISTS start_utc TIMESTAMP;        -- the instant, UTC wall value, tz stripped at bind time
ALTER TABLE samples ADD COLUMN IF NOT EXISTS end_utc TIMESTAMP;
ALTER TABLE samples ADD COLUMN IF NOT EXISTS src_offset_min INTEGER;     -- the source's own UTC offset when known (export, Whoop)
ALTER TABLE samples ADD COLUMN IF NOT EXISTS time_source VARCHAR;        -- bridge_utc | export_offset | whoop_api | NULL (legacy)
ALTER TABLE samples ADD COLUMN IF NOT EXISTS content_hash VARCHAR;       -- ch3 lineage hash over UTC fields; never an identity
ALTER TABLE samples ADD COLUMN IF NOT EXISTS unit_rule VARCHAR;          -- e.g. frac_to_pct_v1 once applied; never applied twice
ALTER TABLE samples ADD COLUMN IF NOT EXISTS score_state VARCHAR;        -- Whoop SCORED | PENDING_SCORE | UNSCORABLE
ALTER TABLE samples ADD COLUMN IF NOT EXISTS quality VARCHAR;            -- NULL usable | unknown_category | unit_mismatch | bad_time
ALTER TABLE samples ADD COLUMN IF NOT EXISTS batch_id VARCHAR;           -- delivering batch (provenance)
ALTER TABLE samples ADD COLUMN IF NOT EXISTS sync_identifier VARCHAR;    -- HealthKit writer sync fields, stored when sent (Phase 3 acts on them)
ALTER TABLE samples ADD COLUMN IF NOT EXISTS sync_version INTEGER;
ALTER TABLE samples ADD COLUMN IF NOT EXISTS writer_id VARCHAR;

-- Deletions leave a marker so a replayed batch or export can never resurrect a
-- sample. One row per deleted HealthKit uuid, written whether or not a sample
-- row existed at the time (a deletion can be delivered before its insert).
CREATE TABLE IF NOT EXISTS tombstones (
    tomb_id       VARCHAR PRIMARY KEY,   -- hk:<uuid> | wh:<metric>:<kind>:<record id>
    hk_uuid       VARCHAR,
    metric        VARCHAR,
    start_utc     TIMESTAMP,
    reason        VARCHAR NOT NULL,      -- bridge_deleted | superseded | manual | whoop_retracted
    batch_id      VARCHAR,
    deleted_at    TIMESTAMP DEFAULT current_timestamp
);
CREATE INDEX IF NOT EXISTS idx_tombstones_uuid ON tombstones (hk_uuid);

-- Real identity transitions only (a Whoop day row replaced by its record row;
-- Phase 1b re-keying). Never written for a skipped duplicate.
CREATE TABLE IF NOT EXISTS sample_aliases (
    old_id        VARCHAR NOT NULL,
    new_id        VARCHAR NOT NULL,
    reason        VARCHAR,
    created_at    TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (old_id, new_id)
);

-- The loaded metric policy mirrored into SQL so the eligibility view can join
-- registration and units. Rewritten from the YAML at every daemon start.
CREATE TABLE IF NOT EXISTS metric_registry (
    metric        VARCHAR PRIMARY KEY,
    hk            VARCHAR,
    unit          VARCHAR,
    agg           VARCHAR,
    daily         BOOLEAN,
    day_basis     VARCHAR,
    direction     VARCHAR,
    trust         VARCHAR
);

-- Durable recompute journal: every ingest writes the reporting dates it
-- touched inside its own transaction; the recompute loop deletes rows only
-- after a successful pass, so a crash never loses work.
CREATE TABLE IF NOT EXISTS dirty_dates (
    date          DATE NOT NULL,
    reason        VARCHAR,               -- ingest | delete | whoop | startup | manual
    batch_id      VARCHAR,
    enqueued_at   TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (date, reason)
);

-- Generation per reporting date, bumped by every recompute that touches it.
-- The narrative slow path publishes only if the generation it read is current.
CREATE TABLE IF NOT EXISTS derived_generation (
    date          DATE PRIMARY KEY,
    generation    INTEGER NOT NULL DEFAULT 0,
    updated_at    TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS schema_version (
    version       INTEGER PRIMARY KEY,
    applied_at    TIMESTAMP DEFAULT current_timestamp,
    note          VARCHAR
);

-- Canonical value per metric per day after trust arbitration. Never cross-device averaged.
CREATE TABLE IF NOT EXISTS daily_values (
    date          DATE NOT NULL,
    metric        VARCHAR NOT NULL,
    value         DOUBLE,
    unit          VARCHAR,
    device_key    VARCHAR NOT NULL,
    n_samples     INTEGER,
    confidence    DOUBLE,
    grade         VARCHAR,
    corroboration VARCHAR,               -- JSON: other devices' values, never blended
    computed_at   TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (date, metric)
);

CREATE TABLE IF NOT EXISTS baselines (
    date          DATE NOT NULL,
    metric        VARCHAR NOT NULL,
    window_days   INTEGER NOT NULL,
    median        DOUBLE,
    mad           DOUBLE,
    n_days        INTEGER,
    PRIMARY KEY (date, metric, window_days)
);

CREATE TABLE IF NOT EXISTS signals (
    date          DATE NOT NULL,
    metric        VARCHAR NOT NULL,
    state         VARCHAR NOT NULL,      -- favorable | neutral | flag | insufficient
    value         DOUBLE,
    unit          VARCHAR,
    baseline_median DOUBLE,
    baseline_mad  DOUBLE,
    delta_pct     DOUBLE,
    device_key    VARCHAR,
    confidence    DOUBLE,
    grade         VARCHAR,
    context_flags VARCHAR,               -- JSON list: travel, heat, late_night
    why           VARCHAR,               -- plain-language reason, deterministic
    PRIMARY KEY (date, metric)
);

CREATE TABLE IF NOT EXISTS events (
    event_id      VARCHAR PRIMARY KEY,
    kind          VARCHAR NOT NULL,      -- quicklog | med | caffeine | alcohol | symptom | note
    ts            TIMESTAMP NOT NULL,
    payload       VARCHAR,               -- JSON
    source        VARCHAR DEFAULT 'user',
    created_at    TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS labs (
    lab_id        VARCHAR PRIMARY KEY,
    panel_date    DATE NOT NULL,
    biomarker     VARCHAR NOT NULL,
    value         DOUBLE,
    unit          VARCHAR,
    ref_low       DOUBLE,
    ref_high      DOUBLE,
    panel_source  VARCHAR,               -- lab name / file
    imported_at   TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS actions (
    action_id     VARCHAR PRIMARY KEY,
    date          DATE NOT NULL,
    text          VARCHAR NOT NULL,
    category      VARCHAR,
    status        VARCHAR DEFAULT 'suggested',  -- suggested | adopted | dismissed | done
    created_by    VARCHAR DEFAULT 'engine',     -- engine | llm | user
    created_at    TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS narratives (
    date          DATE PRIMARY KEY,
    narrative     VARCHAR,
    model         VARCHAR,
    validated     BOOLEAN,
    created_at    TIMESTAMP DEFAULT current_timestamp
);
ALTER TABLE narratives ADD COLUMN IF NOT EXISTS generation INTEGER;      -- derived_generation it was written against

CREATE TABLE IF NOT EXISTS chat_messages (
    msg_id        VARCHAR PRIMARY KEY,
    session_id    VARCHAR NOT NULL,
    role          VARCHAR NOT NULL,
    content       VARCHAR,
    citations     VARCHAR,               -- JSON
    created_at    TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS profile_facts (
    key           VARCHAR PRIMARY KEY,
    value         VARCHAR,
    updated_at    TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS sync_log (
    batch_id      VARCHAR PRIMARY KEY,
    received_at   TIMESTAMP DEFAULT current_timestamp,
    sender        VARCHAR,
    n_samples     INTEGER,
    n_deleted     INTEGER,
    sync_path     VARCHAR
);
ALTER TABLE sync_log ADD COLUMN IF NOT EXISTS n_skipped INTEGER;
ALTER TABLE sync_log ADD COLUMN IF NOT EXISTS n_guarded INTEGER;        -- rows skipped because the uuid or tombstone already existed

-- Dated projection read by the Today screen, the sleep report and the chat
-- tools. Since Phase 1a it is rewritten per pull from whoop_records.
CREATE TABLE IF NOT EXISTS whoop_cache (
    date          DATE NOT NULL,
    kind          VARCHAR NOT NULL,      -- recovery | sleep | cycle
    payload       VARCHAR,               -- JSON as returned (local only)
    fetched_at    TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (date, kind)
);

-- Native Whoop records: one row per (kind, record id), revisions by updated_at.
CREATE TABLE IF NOT EXISTS whoop_records (
    record_key    VARCHAR PRIMARY KEY,   -- <kind>:<native id>; recovery uses its cycle_id
    kind          VARCHAR NOT NULL,      -- recovery | sleep | cycle
    native_id     VARCHAR NOT NULL,
    sleep_id      VARCHAR,
    cycle_id      VARCHAR,
    start_utc     TIMESTAMP,
    end_utc       TIMESTAMP,             -- NULL for the open cycle
    src_offset_min INTEGER,
    score_state   VARCHAR,
    nap           BOOLEAN,
    created_at    TIMESTAMP,
    updated_at    TIMESTAMP,
    payload       VARCHAR,
    fetched_at    TIMESTAMP DEFAULT current_timestamp
);

-- The ONE eligibility view. Every analytical consumer (daily values, sleep
-- report, context, reports) reads this; the watchdog, /api/freshness,
-- /api/health and the read-only SQL tool read `samples` raw and say so.
-- Eligibility is registration (metric_registry), usability (quality),
-- exclusion (device_key), score state, and time validity. Deletion is
-- physical and transactional (tombstones are checked at ingest), so no
-- anti-join runs here. Predicates are NULL-safe: legacy rows have NULL in
-- every Phase 1a column and must stay eligible.
CREATE OR REPLACE VIEW eligible_samples AS
SELECT s.sample_id, s.hk_uuid, s.metric, s.hk_type,
       CASE WHEN s.metric = 'body_fat_pct' AND s.unit_rule IS NULL AND s.value IS NOT NULL AND s.value <= 1.5
            THEN s.value * 100.0 ELSE s.value END AS value,
       s.text_value,
       COALESCE(m.unit, s.unit) AS unit,
       s.start_ts, s.end_ts, s.start_utc, s.end_utc, s.src_offset_min, s.time_source,
       s.source_name, s.device_key, s.sync_path, s.ingested_at, s.unit_rule, s.score_state
FROM samples s
JOIN metric_registry m ON m.metric = s.metric
WHERE s.quality IS NULL
  AND s.device_key <> 'excluded'
  AND (s.score_state IS NULL OR s.score_state = 'SCORED')
  AND s.start_ts IS NOT NULL
  AND (s.end_ts IS NULL OR s.end_ts >= s.start_ts)
  AND s.start_ts <= now()::TIMESTAMP + INTERVAL 1 DAY;
