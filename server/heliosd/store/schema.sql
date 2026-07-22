-- Helios store. All health data at rest lives here, on local disk.

CREATE TABLE IF NOT EXISTS samples (
    sample_id     VARCHAR PRIMARY KEY,   -- content hash: type|start|end|source|value
    hk_uuid       VARCHAR,               -- HealthKit sample UUID (deletion handling)
    metric        VARCHAR NOT NULL,      -- canonical metric id
    hk_type       VARCHAR,               -- original HealthKit identifier (null for whoop-native)
    value         DOUBLE,
    text_value    VARCHAR,               -- category values, e.g. sleep stage names
    unit          VARCHAR,
    start_ts      TIMESTAMP NOT NULL,
    end_ts        TIMESTAMP,
    source_name   VARCHAR NOT NULL,      -- raw Apple Health source string
    device_key    VARCHAR NOT NULL,      -- resolved via source_registry
    sync_path     VARCHAR NOT NULL,      -- bridge | whoop_live | backfill | legacy_import | manual
    ingested_at   TIMESTAMP DEFAULT current_timestamp
);
CREATE INDEX IF NOT EXISTS idx_samples_metric_ts ON samples (metric, start_ts);
CREATE INDEX IF NOT EXISTS idx_samples_hk_uuid ON samples (hk_uuid);
CREATE INDEX IF NOT EXISTS idx_samples_device ON samples (device_key, metric, start_ts);

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

CREATE TABLE IF NOT EXISTS whoop_cache (
    date          DATE NOT NULL,
    kind          VARCHAR NOT NULL,      -- recovery | sleep | cycle
    payload       VARCHAR,               -- JSON as returned (local only)
    fetched_at    TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (date, kind)
);
