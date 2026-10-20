"""Schema v5 (single-source Phase 2): the history import tables are additive.

A v4 store (the live shape before Phase 2) opened by this code gains the v5
tables empty, keeps every row of every table it had, and a second start
changes nothing. The v5 block of schema.sql only creates tables. Synthetic
rows only."""

from __future__ import annotations

import re

import duckdb
import pytest

from heliosd.store import db

V5_PKS = {"export_files": ["export_label", "rel_path"], "import_runs": ["run_id"], "raw_records": ["record_id"],
          "workout_sources": ["source_id"], "workouts": ["workout_id"], "workout_statistics": ["source_id", "ord"],
          "workout_events": ["source_id", "ord"], "workout_metadata": ["source_id", "ord"],
          "workout_routes": ["route_ref_id"], "route_points": ["route_id", "seq"], "activity_summaries": ["summary_id"],
          "ecgs": ["ecg_id"], "ecg_waveforms": ["ecg_id", "lead"], "workout_ambiguity": ["item_id"]}


def _tables(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' "
                                       "AND table_type = 'BASE TABLE' ORDER BY 1").fetchall()]


def _pk(conn, table) -> list[str] | None:
    rows = conn.execute("SELECT constraint_column_names FROM duckdb_constraints() WHERE table_name = ? "
                        "AND constraint_type = 'PRIMARY KEY'", [table]).fetchall()
    return list(rows[0][0]) if rows else None


def _snapshot(conn) -> dict[str, list[str]]:
    out = {}
    for t in _tables(conn):
        if t == "schema_version":
            continue
        out[t] = sorted(repr(r) for r in conn.execute(f'SELECT * FROM "{t}"').fetchall())
    return out


def test_the_phase2_tables_are_the_v5_tables():
    assert tuple(V5_PKS) == db.PHASE2_TABLES
    conn = db.connect_memory()
    for t, pk in V5_PKS.items():
        assert _pk(conn, t) == pk, t
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
    assert [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY 1").fetchall()] == [2, 3, 4, 5]
    assert db.SCHEMA_VERSION == 5
    conn.close()


def test_the_v5_block_only_creates_tables():
    """Additive only: between the Phase 2 header and the eligibility view there
    is nothing but CREATE TABLE IF NOT EXISTS for the v5 tables (no ALTER, no
    DROP, no index, no statement on an older table)."""
    sql = db.SCHEMA_PATH.read_text(encoding="utf-8")
    start = sql.index("-- Phase 2 (2026-10-20, schema v5): the history import from the raw Apple")
    end = sql.index("-- The ONE eligibility view.")
    block = "\n".join(re.sub(r"--.*", "", line) for line in sql[start:end].splitlines())
    statements = [s.strip() for s in block.split(";") if s.strip()]
    created = []
    for s in statements:
        m = re.match(r"CREATE TABLE IF NOT EXISTS (\w+) \(", s)
        assert m, s[:80]
        created.append(m.group(1))
    assert tuple(created) == db.PHASE2_TABLES
    assert not re.search(r"\b(ALTER|DROP|INDEX|UPDATE|DELETE|INSERT)\b", block)


def _v4_store(path) -> None:
    """A store in the v4 shape with rows in its tables: this code's store with
    the v5 tables and the version-5 row taken away."""
    conn = db.connect(path)
    conn.execute("INSERT INTO samples (sample_id, hk_uuid, metric, value, unit, start_ts, end_ts, start_utc, end_utc, "
                 "source_name, device_key, sync_path, time_source) VALUES ('hk:u-1', 'u-1', 'steps', 120, 'count', "
                 "'2026-05-01 10:00', '2026-05-01 10:05', '2026-05-01 06:00', '2026-05-01 06:05', 'Synthetic Ultra 1', "
                 "'apple_watch_ultra', 'bridge', 'bridge_utc')")
    conn.execute("INSERT INTO daily_values (date, metric, value, unit, device_key, n_samples) "
                 "VALUES ('2026-05-01', 'steps', 120, 'count', 'apple_watch_ultra', 1)")
    conn.execute("INSERT INTO migrations (name, applied_at, summary) VALUES ('synthetic_v1', '2026-05-02 00:00', "
                 "'{\"phase\": \"verified\"}')")
    for t in db.PHASE2_TABLES:
        conn.execute(f"DROP TABLE {t}")
    conn.execute("DELETE FROM schema_version WHERE version = 5")
    conn.close()


def test_a_v4_store_upgrades_in_place_and_keeps_every_row(tmp_path):
    path = tmp_path / "v4.duckdb"
    _v4_store(path)
    raw = duckdb.connect(str(path))
    assert not set(db.PHASE2_TABLES) & set(_tables(raw))
    before = _snapshot(raw)
    assert len(before["samples"]) == 1 and len(before["daily_values"]) == 1 and len(before["migrations"]) == 1
    raw.close()
    stamp = None
    for _ in range(2):                       # the upgrade, then a start that changes nothing
        conn = db.connect(path)
        after = _snapshot(conn)
        for t in db.PHASE2_TABLES:
            assert after.pop(t) == [], t
        assert after == before
        v = conn.execute("SELECT version, applied_at FROM schema_version WHERE version = 5").fetchall()
        assert len(v) == 1 and stamp in (None, v[0][1])
        stamp = v[0][1]
        conn.close()


def test_a_partial_v5_upgrade_fails_loudly():
    conn = db.connect_memory()
    conn.execute("DROP TABLE workout_ambiguity")
    with pytest.raises(RuntimeError, match="workout_ambiguity"):
        db.assert_schema(conn)
    conn.close()
    conn = db.connect_memory()
    conn.execute("ALTER TABLE raw_records DROP COLUMN link_status")
    with pytest.raises(RuntimeError, match="raw_records"):
        db.assert_schema(conn)
    conn.close()
