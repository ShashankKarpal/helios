"""Phase 1b item (a): schema v3 is additive (rebase_era, the re-read landing
tables, sync_log landing counts, the migrations table), the old composite
indexes are no longer created but never dropped by the DDL, every version row
is recorded, and a partial upgrade fails loudly at startup."""

from __future__ import annotations

import duckdb
import pytest

from heliosd.store import db

V2_TABLE = """CREATE TABLE samples (
    sample_id VARCHAR PRIMARY KEY, hk_uuid VARCHAR, metric VARCHAR NOT NULL, hk_type VARCHAR,
    value DOUBLE, text_value VARCHAR, unit VARCHAR, start_ts TIMESTAMP NOT NULL, end_ts TIMESTAMP,
    source_name VARCHAR NOT NULL, device_key VARCHAR NOT NULL, sync_path VARCHAR NOT NULL,
    ingested_at TIMESTAMP DEFAULT current_timestamp)"""


def _indexes(conn, table="samples"):
    return sorted(r[0] for r in conn.execute(
        "SELECT index_name FROM duckdb_indexes() WHERE table_name = ?", [table]).fetchall())


def _cols(conn, table):
    return [r[0] for r in conn.execute(f"DESCRIBE {table}").fetchall()]


def test_fresh_store_has_the_phase_1b_shape():
    conn = db.connect_memory()
    assert "rebase_era" in _cols(conn, "samples")
    assert {"n_landed", "guard_outcomes", "n_guarded"} <= set(_cols(conn, "sync_log"))
    assert _cols(conn, "hk_reread")[0] == "hk_uuid" and "start_raw" in _cols(conn, "hk_reread")
    assert _cols(conn, "hk_reread_variants")[:2] == ["hk_uuid", "seq"]
    assert set(_cols(conn, "migrations")) == {"name", "applied_at", "code_commit", "input_fingerprint", "summary"}
    assert [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY 1").fetchall()] == [2, 3]
    # Only the uuid index: the two composite indexes are not created any more.
    assert _indexes(conn) == ["idx_samples_hk_uuid"]


def test_old_store_keeps_its_composite_indexes_and_its_rows(tmp_path):
    """A store shaped like the live one before the prep deploy (v2 columns and
    the three indexes): the upgrade adds columns and tables, drops nothing and
    changes no row; a second open is a no-op."""
    path = tmp_path / "old.duckdb"
    raw = duckdb.connect(str(path))
    raw.execute(V2_TABLE)
    raw.execute("CREATE INDEX idx_samples_metric_ts ON samples (metric, start_ts)")
    raw.execute("CREATE INDEX idx_samples_hk_uuid ON samples (hk_uuid)")
    raw.execute("CREATE INDEX idx_samples_device ON samples (device_key, metric, start_ts)")
    raw.execute("INSERT INTO samples (sample_id, hk_uuid, metric, value, start_ts, end_ts, source_name, device_key, sync_path) "
                "VALUES ('ch2:a', 'u1', 'steps', 10, '2026-06-01 10:00', '2026-06-01 10:05', 'Watch', 'apple_watch_ultra', 'bridge')")
    raw.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT current_timestamp, note VARCHAR)")
    raw.execute("INSERT INTO schema_version (version, note) VALUES (2, 'phase 1a')")
    raw.close()
    for _ in range(2):
        conn = db.connect(path)
        assert _indexes(conn) == ["idx_samples_device", "idx_samples_hk_uuid", "idx_samples_metric_ts"]
        row = conn.execute("SELECT sample_id, value, rebase_era, time_source FROM samples").fetchall()
        assert row == [("ch2:a", 10.0, None, None)]
        assert [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY 1").fetchall()] == [2, 3]
        assert conn.execute("SELECT COUNT(*) FROM hk_reread").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 0
        conn.close()


def test_partial_upgrade_fails_loudly():
    conn = db.connect_memory()
    conn.execute("DROP TABLE hk_reread_variants")
    with pytest.raises(RuntimeError, match="hk_reread_variants"):
        db.assert_schema(conn)
    conn = db.connect_memory()
    conn.execute("ALTER TABLE sync_log DROP COLUMN n_landed")
    with pytest.raises(RuntimeError, match="n_landed"):
        db.assert_schema(conn)


def test_migration_flag_reads_the_migrations_table():
    conn = db.connect_memory()
    assert db.migration_applied(conn, "phase1b_history_rebase_v1") is False
    db.execute(conn, "INSERT INTO migrations (name, applied_at, code_commit, input_fingerprint, summary) "
                     "VALUES ('phase1b_history_rebase_v1', now()::TIMESTAMP, 'abc', 'fp', '{}')")
    assert db.migration_applied(conn, "phase1b_history_rebase_v1") is True
