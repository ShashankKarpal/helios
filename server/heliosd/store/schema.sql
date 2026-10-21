-- Helios store. All health data at rest lives here, on local disk.
--
-- Single-source Phase 1a (2026-10-04): identity, time and deletion columns are
-- ADDITIVE and nullable so history is untouched until Phase 1b rebases it.
-- Phase 1b (2026-10-05): rebase_era, the re-read landing tables, sync_log
-- landing counts and the migrations table, additive as well.
-- Wave 2 (2026-10-08, schema v4): daily_values.detail and the derived table
-- device_baselines, additive as well. The Wave 1 code never names either, so
-- it still runs on a v4 store (no code here refuses a newer schema version).
-- Phase 2 (2026-10-20, schema v5): the history import tables, new tables only.
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
-- One secondary index: the uuid lookup the ingest guard, the deletion path and
-- the re-read landing need. The two composite indexes the old schema created
-- (metric, start_ts) and (device_key, metric, start_ts) are not created any
-- more: measured unused by the production daily-value query (no index scan,
-- equal timings with and without; Phase 1b step-1 rehearsal) and the recorded
-- composite ART delete fault (OPS record 2026-09-18) sits on exactly those.
-- Owner decision 2026-10-05 (decisions file 4c.2): dropped at the Phase 1b
-- swap, when the samples table is rebuilt without them; until then a store
-- that already has them keeps them (nothing here drops an index).
CREATE INDEX IF NOT EXISTS idx_samples_hk_uuid ON samples (hk_uuid);

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

-- Phase 1b additions (2026-10-05): the history rebase and the HealthKit
-- re-read landing. Additive and nullable: the Phase 1b code runs unchanged on
-- an un-migrated store (the prep deploy), and the migration tool
-- (server/tools/rebase_history.py, daemon stopped) is the only writer of the
-- rebased values. time_source gains era_rebase_v1 (instants from the frozen
-- era rule, unconfirmed), bridge_reread_v1 (instants confirmed or taken from
-- the re-read) and export_linked_v1 (an export row linked one-to-one to its
-- Bridge row). quality gains export_duplicate (the linked export row, out of
-- daily values because the Bridge row is the sample), export_ambiguous (more
-- than one candidate either way, listed), legacy_whoop_unresolved (a
-- day-keyed Whoop row no native record accounts for) and hk_content_twin
-- (2026-10-07, owner decision 4d: a Bridge row whose content another row of
-- the same source and device already carries under another uuid; kept as
-- lineage, one row per content group stays eligible; the transitions are in
-- content_twins; heliosd/ingest/twins.py).
ALTER TABLE samples ADD COLUMN IF NOT EXISTS rebase_era INTEGER;         -- 1, 2 or 4 on a rebased legacy row; NULL on every row the 1a code wrote

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

-- Content twins (owner decision 4d, 2026-10-07; heliosd/ingest/twins.py): the
-- migration's demotions (one eligible row per content group, the rest quality
-- hk_content_twin) and the later promotions (a demoted row made eligible again
-- when its survivor was deleted). The row's quality is the materialized state;
-- this table is the durable, idempotent record of every transition and the only
-- lookup the deletion path needs (survivor_id is indexed). Never deleted from.
CREATE TABLE IF NOT EXISTS content_twins (
    sample_id     VARCHAR NOT NULL,      -- the row whose eligibility changed
    survivor_id   VARCHAR,               -- the group's eligible row at the time (demoted: the survivor; promoted: the deleted survivor)
    event         VARCHAR NOT NULL,      -- demoted_v1 | promoted_v1
    source        VARCHAR NOT NULL,      -- migration:<name> | batch:<batch id> | restore
    created_at    TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (sample_id, event, source)
);
CREATE INDEX IF NOT EXISTS idx_content_twins_survivor ON content_twins (survivor_id);
CREATE INDEX IF NOT EXISTS idx_content_twins_sample ON content_twins (sample_id);

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
-- Schema v4 (Wave 2): what the winning row says about itself, as JSON, or NULL.
-- Written by compute_daily_values from the fifth element of a day row: the
-- night's window for sleep (B1), the devices that fed a merged day (B11), an
-- open Whoop cycle (B7). NULL on every row the Wave 1 code wrote.
ALTER TABLE daily_values ADD COLUMN IF NOT EXISTS detail VARCHAR;

-- One baseline per metric and window, the one every judgement reads. Its key
-- stays (date, metric, window_days) in v4: the Wave 1 code's INSERT OR REPLACE
-- relies on it, and CREATE TABLE IF NOT EXISTS could not change it in place.
-- Per-device baselines live in device_baselines instead.
CREATE TABLE IF NOT EXISTS baselines (
    date          DATE NOT NULL,
    metric        VARCHAR NOT NULL,
    window_days   INTEGER NOT NULL,
    median        DOUBLE,
    mad           DOUBLE,
    n_days        INTEGER,
    PRIMARY KEY (date, metric, window_days)
);

-- Schema v4 (Wave 2, derived): the same median and MAD for a device that is
-- not the owner, from that device's own values (the days it filled plus the
-- days it corroborated), so Apple sleep beside Whoop has a baseline of its own
-- (design B5, B15). Rebuilt from daily_values like baselines; shown labelled,
-- never judged against. Empty until Wave 2 group C writes it.
CREATE TABLE IF NOT EXISTS device_baselines (
    date          DATE NOT NULL,
    metric        VARCHAR NOT NULL,
    window_days   INTEGER NOT NULL,
    device_key    VARCHAR NOT NULL,      -- a registry device key or a qualified key such as whoop:healthkit
    median        DOUBLE,
    mad           DOUBLE,
    n_days        INTEGER,
    PRIMARY KEY (date, metric, window_days, device_key)
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
ALTER TABLE sync_log ADD COLUMN IF NOT EXISTS n_landed INTEGER;         -- guarded rows recorded in hk_reread or hk_reread_variants (Phase 1b)
ALTER TABLE sync_log ADD COLUMN IF NOT EXISTS guard_outcomes VARCHAR;   -- JSON: the disjoint guard outcomes of the batch (new, landed_first, ...)

-- Phase 1b landing table (step 2). The 1a insert guard drops any uuid the
-- store already holds; during the owner's HealthKit re-read every re-delivered
-- sample would vanish on arrival. Instead, a guarded row whose existing row is
-- LEGACY (time_source NULL or era_rebase_v1) is recorded here with the
-- NORMALIZED fields a fresh insert would get, plus the delivered start and end
-- strings verbatim as evidence. One row per uuid holds the FIRST observation;
-- an identical later observation bumps n_seen and last_seen (the same batch
-- twice is a no-op); a differing observation goes to hk_reread_variants. A
-- re-delivery of a uuid whose row is already native (bridge_utc) lands only as
-- a variant, and only when its content differs from the stored row. The
-- migration compares these instants with the era rule, uuid by uuid, and the
-- re-read wins; the rows it consumed go to the lineage archive and are
-- deleted at the apply. The canonical target is found by uuid at apply time,
-- never by a stored sample id.
CREATE TABLE IF NOT EXISTS hk_reread (
    hk_uuid       VARCHAR PRIMARY KEY,
    hk_type       VARCHAR,
    metric        VARCHAR,
    value         DOUBLE,
    text_value    VARCHAR,
    unit          VARCHAR,
    start_utc     TIMESTAMP,
    end_utc       TIMESTAMP,
    start_raw     VARCHAR,               -- the delivered start string, verbatim (full precision evidence)
    end_raw       VARCHAR,
    source_name   VARCHAR,
    device_key    VARCHAR,
    quality       VARCHAR,
    unit_rule     VARCHAR,
    existing_rows INTEGER,               -- rows the uuid had in samples when first landed (a legacy twin pair has 2)
    existing_time_source VARCHAR,        -- their time_source values, 'legacy' for NULL
    time_source   VARCHAR,               -- the observation's own provenance: bridge_utc, or assumed_reporting_wall for an offset-free input
    batches       VARCHAR[],             -- every distinct batch that delivered this exact observation; n_seen is its length
    first_batch   VARCHAR,
    last_batch    VARCHAR,
    first_seen    TIMESTAMP,
    last_seen     TIMESTAMP,
    n_seen        INTEGER
);
CREATE TABLE IF NOT EXISTS hk_reread_variants (
    hk_uuid       VARCHAR NOT NULL,
    seq           INTEGER NOT NULL,
    hk_type       VARCHAR,
    metric        VARCHAR,
    value         DOUBLE,
    text_value    VARCHAR,
    unit          VARCHAR,
    start_utc     TIMESTAMP,
    end_utc       TIMESTAMP,
    start_raw     VARCHAR,
    end_raw       VARCHAR,
    source_name   VARCHAR,
    device_key    VARCHAR,
    quality       VARCHAR,
    unit_rule     VARCHAR,
    existing_time_source VARCHAR,        -- what the stored row said when this variant arrived
    time_source   VARCHAR,
    batch_id      VARCHAR,
    seen_at       TIMESTAMP,
    PRIMARY KEY (hk_uuid, seq)
);

-- Data migrations, separate from schema_version (a schema row says the DDL
-- ran; a migration row says a rewrite of the data completed, with what code,
-- on which input). The migration tool refuses a second run when its row exists.
CREATE TABLE IF NOT EXISTS migrations (
    name          VARCHAR PRIMARY KEY,
    applied_at    TIMESTAMP,
    code_commit   VARCHAR,
    input_fingerprint VARCHAR,
    summary       VARCHAR                -- JSON: constants, counts, timings, flags
);

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

-- Phase 2 (2026-10-20, schema v5): the history import from the raw Apple
-- Health exports (server/tools/import_history_phase2.py; single-source plan
-- v2 sections 2.5, 4.2, 4.5 and 5). ADDITIVE ONLY: new tables, no statement
-- here alters a table that existed before, and the importer never writes
-- `samples` (registered types keep their existing path; raw_records says
-- which store row already holds each one). The daemon creates these tables
-- empty at start and reads none of them yet (Phase 4). Writers: the importer,
-- and since Phase 3 the Bridge ingest for raw_records and route_points only
-- (Bridge payload v2: file_sha 'bridge', run_id 'bridge:<batch id>',
-- heliosd/ingest/bridge_v2.py). The migrations row phase2_history_import_v1
-- says an import completed and passed its checks.
-- Lineage on every row: the export file (export_files.file_sha) and the
-- element's position in it (ordinal: the index of the top-level element in
-- export.xml; ord: the position among its parent's children of that kind).
-- Times: the instant in UTC (start_utc, ...), the printed string verbatim
-- (start_raw, ...) and the offset the export printed (print_offset_min). An
-- export prints every date in the zone of the exporting device, so that
-- offset is NOT the recording zone; a writer's own zone, when it stored one,
-- is the HKTimeZone metadata (workouts.tz_name).
CREATE TABLE IF NOT EXISTS export_files (
    export_label  VARCHAR NOT NULL,      -- the export folder's name
    rel_path      VARCHAR NOT NULL,      -- inside it: export.xml, electrocardiograms/<f>.csv, workout-routes/<f>.gpx, attachments/<f>
    file_sha      VARCHAR NOT NULL,      -- sha256 of the bytes
    kind          VARCHAR NOT NULL,      -- export_xml | ecg_csv | route_gpx | attachment
    size_bytes    BIGINT,
    content_id    VARCHAR,               -- the ecg_id or route_id the file holds (one ECG or route in two exports shares it)
    n_items       INTEGER,               -- top-level elements, voltage rows or track points read
    export_date   VARCHAR,               -- export.xml: the ExportDate it printed
    first_run     VARCHAR,
    PRIMARY KEY (export_label, rel_path)
);

CREATE TABLE IF NOT EXISTS import_runs (
    run_id        VARCHAR PRIMARY KEY,
    started_at    TIMESTAMP,
    finished_at   TIMESTAMP,
    code_commit   VARCHAR,
    inputs        VARCHAR,               -- JSON: the export folders and their file fingerprints
    counts        VARCHAR,               -- JSON: rows written and rows already present, per table
    status        VARCHAR                -- complete | stopped
);

-- The lossless raw envelope: every top-level Record of export.xml (registered
-- types too, for their metadata, device strings and printed offsets), every
-- Correlation and the other top-level health elements (VisionPrescription,
-- Audiogram, ClinicalRecord). Identity, idempotent across reruns and across
-- exports: xr:<24 hex of the sha256 of the element's own fields>:<n>, n
-- counting the earlier elements of the same file with the same fields
-- (identical records are all kept: multiplicity is data). The fields: the
-- element, type, unit, value, source name and version, the device string
-- without its memory address, the creation, start and end instants in UTC and
-- the metadata pairs; never a printed offset (it changes with the exporting
-- zone) and never the children (HRV beat times print as local clock times).
-- Bridge rows (Phase 3): record_id hk:<HealthKit uuid> for every sample,
-- workout, route and ECG; hkas:<day>:<16 hex> and hkds:<type>:<zone>:<day>:<16 hex>
-- for each distinct observation of an activity summary or of Apple's daily
-- total; the Bridge-only fields in children; never a printed offset (the
-- Bridge sends UTC with milliseconds, kept in start_utc and verbatim in start_raw).
CREATE TABLE IF NOT EXISTS raw_records (
    record_id     VARCHAR PRIMARY KEY,
    element       VARCHAR NOT NULL,      -- export: Record | Correlation | VisionPrescription | Audiogram | ClinicalRecord;
                                         -- Bridge: Record | Workout | WorkoutRoute | Electrocardiogram | ActivitySummary | DailyStatistic
    hk_type       VARCHAR,
    metric        VARCHAR,               -- the registered metric at import time; NULL: unregistered, raw only
    value_raw     VARCHAR,               -- the value attribute verbatim
    value         DOUBLE,                -- value_raw when it is a finite number
    unit          VARCHAR,
    source_name   VARCHAR,
    source_version VARCHAR,
    device        VARCHAR,               -- the device string verbatim
    device_key    VARCHAR,               -- source registry in store mode: 'excluded' for an ignored source
    creation_raw  VARCHAR,
    start_raw     VARCHAR,
    end_raw       VARCHAR,
    creation_utc  TIMESTAMP,
    start_utc     TIMESTAMP,
    end_utc       TIMESTAMP,
    print_offset_min INTEGER,
    metadata      VARCHAR,               -- JSON [[key, value], ...] in document order, repeats kept
    children      VARCHAR,               -- JSON: HRV beats [[time, bpm], ...], correlation members, other parts
    sync_identifier VARCHAR,             -- HKMetadataKeySyncIdentifier / SyncVersion when the writer set them
    sync_version  BIGINT,
    link_status   VARCHAR,               -- registered types: bridge | export | none; NULL for the rest
    linked_sample_id VARCHAR,            -- the samples row that already holds this record (export_relink's rule)
    link_reason   VARCHAR,               -- for none: why (no row at the same instants, none within tolerance, all claimed)
    file_sha      VARCHAR NOT NULL,
    ordinal       INTEGER NOT NULL,
    run_id        VARCHAR
);

-- One row per Workout element of an export: the source record of a session.
-- Identity as raw_records (xw:<24 hex>:<n>). A canonical session (workouts)
-- has one anchor (its timing owner) and the members the matcher linked to it.
CREATE TABLE IF NOT EXISTS workout_sources (
    source_id     VARCHAR PRIMARY KEY,
    workout_id    VARCHAR NOT NULL,      -- its canonical session
    role          VARCHAR NOT NULL,      -- anchor | member | copy (a same-writer copy of another source row)
    link_rule     VARCHAR NOT NULL,      -- anchor | explicit_copy | candidate_v1
    family        VARCHAR NOT NULL,      -- source family: apple | whoop | zepp | the device key
    activity_type VARCHAR,
    activity_family VARCHAR,
    duration_raw  VARCHAR,
    duration_unit VARCHAR,
    duration_s    DOUBLE,
    total_distance_raw VARCHAR,          -- the optional totalDistance and totalEnergyBurned attributes, verbatim
    total_distance_unit VARCHAR,
    total_energy_raw VARCHAR,
    total_energy_unit VARCHAR,
    source_name   VARCHAR,
    source_version VARCHAR,
    device        VARCHAR,
    device_key    VARCHAR,
    creation_raw  VARCHAR,
    start_raw     VARCHAR,
    end_raw       VARCHAR,
    creation_utc  TIMESTAMP,
    start_utc     TIMESTAMP,
    end_utc       TIMESTAMP,
    print_offset_min INTEGER,
    tz_name       VARCHAR,               -- HKTimeZone metadata
    sync_identifier VARCHAR,
    sync_version  BIGINT,
    match_detail  VARCHAR,               -- JSON: start gap, overlap over union and duration ratio against the anchor
    independence  VARCHAR NOT NULL,      -- unknown | same_writer_copy (lineage proves nothing else)
    file_sha      VARCHAR NOT NULL,
    ordinal       INTEGER NOT NULL,
    run_id        VARCHAR
);

-- The canonical session. Every value is nullable and names the source row it
-- came from (*_source, a workout_sources.source_id): plan v2 4.5 field owners
-- with a fixed fallback order. energy_kcal is the session's own active
-- energy and is never added to a daily energy total.
CREATE TABLE IF NOT EXISTS workouts (
    workout_id    VARCHAR PRIMARY KEY,   -- wk:<anchor source_id>
    status        VARCHAR NOT NULL,      -- matched | single | queued (its rows are in workout_ambiguity)
    n_sources     INTEGER NOT NULL,      -- source rows, copies included
    activity_type VARCHAR,
    activity_family VARCHAR,
    type_source   VARCHAR,
    start_utc     TIMESTAMP,
    end_utc       TIMESTAMP,
    duration_s    DOUBLE,
    timing_source VARCHAR,
    tz_name       VARCHAR,
    tz_source     VARCHAR,
    distance      DOUBLE,
    distance_unit VARCHAR,
    distance_type VARCHAR,
    distance_source VARCHAR,
    energy_kcal   DOUBLE,
    energy_unit   VARCHAR,
    energy_source VARCHAR,
    hr_avg        DOUBLE,
    hr_min        DOUBLE,
    hr_max        DOUBLE,
    hr_unit       VARCHAR,
    hr_source     VARCHAR,
    strain        DOUBLE,
    strain_source VARCHAR,
    route_id      VARCHAR,
    route_source  VARCHAR,               -- the workout_routes row whose FileReference names the file
    rule_version  VARCHAR,
    run_id        VARCHAR
);

CREATE TABLE IF NOT EXISTS workout_statistics (
    source_id     VARCHAR NOT NULL,
    ord           INTEGER NOT NULL,
    hk_type       VARCHAR,
    start_raw     VARCHAR,
    end_raw       VARCHAR,
    start_utc     TIMESTAMP,
    end_utc       TIMESTAMP,
    stat_sum      DOUBLE,
    stat_avg      DOUBLE,
    stat_min      DOUBLE,
    stat_max      DOUBLE,
    unit          VARCHAR,
    PRIMARY KEY (source_id, ord)
);

CREATE TABLE IF NOT EXISTS workout_events (
    source_id     VARCHAR NOT NULL,
    ord           INTEGER NOT NULL,
    event_type    VARCHAR,
    date_raw      VARCHAR,
    date_utc      TIMESTAMP,
    duration      DOUBLE,
    duration_unit VARCHAR,
    metadata      VARCHAR,               -- JSON pairs, as raw_records.metadata
    PRIMARY KEY (source_id, ord)
);

-- Every MetadataEntry of a Workout in document order. The export repeats a
-- key with the same value (seen 2026-10-20 on every Apple, Whoop and
-- Ultrahuman workout); both entries are kept.
CREATE TABLE IF NOT EXISTS workout_metadata (
    source_id     VARCHAR NOT NULL,
    ord           INTEGER NOT NULL,
    meta_key      VARCHAR,
    meta_value    VARCHAR,
    PRIMARY KEY (source_id, ord)
);

-- One row per WorkoutRoute element. The route is linked through the export's
-- FileReference path only, never by time. The export names route files by
-- their start minute, so two routes that start in the same minute share one
-- file (link_status shared_file): such a file belongs to a session only when
-- every workout that names it is in that session; otherwise it is queued.
CREATE TABLE IF NOT EXISTS workout_routes (
    route_ref_id  VARCHAR PRIMARY KEY,   -- <source_id>:route:<ord>
    source_id     VARCHAR NOT NULL,
    ord           INTEGER NOT NULL,
    file_ref      VARCHAR,               -- the FileReference path verbatim
    route_id      VARCHAR,               -- the GPX file's points (route_points); NULL when the file is missing
    link_status   VARCHAR NOT NULL,      -- linked | shared_file | missing_file
    source_name   VARCHAR,
    source_version VARCHAR,
    device        VARCHAR,
    creation_raw  VARCHAR,
    start_raw     VARCHAR,
    end_raw       VARCHAR,
    start_utc     TIMESTAMP,
    end_utc       TIMESTAMP,
    metadata      VARCHAR,
    run_id        VARCHAR
);

CREATE TABLE IF NOT EXISTS route_points (
    route_id      VARCHAR NOT NULL,      -- rt:<24 hex of the sha256 of the track points> (export); rtb:<route uuid> (Bridge)
    seq           INTEGER NOT NULL,
    time_raw      VARCHAR,
    time_utc      TIMESTAMP,
    lat           DOUBLE,
    lon           DOUBLE,
    ele           DOUBLE,
    speed         DOUBLE,
    course        DOUBLE,
    h_acc         DOUBLE,
    v_acc         DOUBLE,
    PRIMARY KEY (route_id, seq)
);

-- Activity rings as the export states them: native date components and goals.
CREATE TABLE IF NOT EXISTS activity_summaries (
    summary_id    VARCHAR PRIMARY KEY,   -- as:<dateComponents>
    date_components VARCHAR NOT NULL,
    day           DATE,
    active_energy DOUBLE,
    active_energy_goal DOUBLE,
    active_energy_unit VARCHAR,
    move_time     DOUBLE,
    move_time_goal DOUBLE,
    exercise_time DOUBLE,
    exercise_time_goal DOUBLE,
    stand_hours   DOUBLE,
    stand_hours_goal DOUBLE,
    partial       BOOLEAN,               -- the export's own day, still open when the export was taken
    file_sha      VARCHAR NOT NULL,
    ordinal       INTEGER NOT NULL,
    run_id        VARCHAR
);

-- ECGs from the export's CSV files. The sampling rate is per record (512 and
-- 514.039 Hz both occur). The name and date of birth lines are not stored.
CREATE TABLE IF NOT EXISTS ecgs (
    ecg_id        VARCHAR PRIMARY KEY,   -- ecg:<recorded instant in UTC, yyyymmddThhmmssZ>
    recorded_raw  VARCHAR,
    recorded_utc  TIMESTAMP,
    print_offset_min INTEGER,
    classification VARCHAR,
    symptoms      VARCHAR,
    software_version VARCHAR,
    device        VARCHAR,
    sample_rate_hz DOUBLE NOT NULL,
    sample_rate_raw VARCHAR,
    lead          VARCHAR,
    unit          VARCHAR,
    n_samples     INTEGER,
    voltage_sha   VARCHAR,               -- sha256 of the voltage lines as printed: the same ECG in two exports
    export_label  VARCHAR,
    rel_path      VARCHAR,
    file_sha      VARCHAR,
    run_id        VARCHAR
);

CREATE TABLE IF NOT EXISTS ecg_waveforms (
    ecg_id        VARCHAR NOT NULL,
    lead          VARCHAR NOT NULL,
    unit          VARCHAR,
    sample_rate_hz DOUBLE NOT NULL,
    n_samples     INTEGER,
    microvolts    DOUBLE[],
    PRIMARY KEY (ecg_id, lead)
);

-- The ambiguity queue (plan v2 4.5): what the rules do not decide is listed
-- here, never guessed. Phase 4 resolves items; until then status stays open.
CREATE TABLE IF NOT EXISTS workout_ambiguity (
    item_id       VARCHAR PRIMARY KEY,   -- amb:<kind>:<16 hex of the member ids>
    kind          VARCHAR NOT NULL,      -- multiple_candidates | near_miss | same_family_overlap | shared_route_file | missing_route_file
    source_ids    VARCHAR NOT NULL,      -- JSON list of workout_sources.source_id
    candidate_ids VARCHAR,               -- JSON list
    reason        VARCHAR,
    detail        VARCHAR,               -- JSON: the scores and the failed rule parts
    status        VARCHAR NOT NULL,      -- open | resolved
    run_id        VARCHAR
);

-- The ONE eligibility view. Every analytical consumer (daily values, sleep
-- report, context, reports) reads this; the watchdog, /api/freshness,
-- /api/health and the read-only SQL tool read `samples` raw and say so.
-- Eligibility is registration (metric_registry), usability (quality),
-- exclusion (device_key), score state, and time validity. Deletion is
-- physical and transactional (tombstones are checked at ingest), so no
-- anti-join runs here. Predicates are NULL-safe: legacy rows have NULL in
-- every Phase 1a column and must stay eligible. Time validity and the future
-- ceiling use the UTC instants when a row has them (wall times can run
-- backwards across a DST fall-back and the session zone is the Mac's, not the
-- reporting zone; checkpoint B point 13); legacy rows fall back to wall time.
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
  AND (CASE WHEN s.start_utc IS NOT NULL THEN (s.end_utc IS NULL OR s.end_utc >= s.start_utc)
            ELSE (s.end_ts IS NULL OR s.end_ts >= s.start_ts) END)
  AND COALESCE(s.start_utc, s.start_ts) <= timezone('UTC', now()) + INTERVAL 1 DAY;
