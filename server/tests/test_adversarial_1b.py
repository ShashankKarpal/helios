"""Phase 1b item (g): the adversarial sequences of adjudication-A points 36
and 37 that the item tests do not already cover: a decoupled clock caught by
the independent anchor, a Whoop day row inside an era gap, and mutation tests
of the derived-diff classifier and of the independent oracle (point 34)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb
import pytest

import json
import pathlib

from heliosd.migrate import rebase_history as rh
from heliosd.store import db
from tests.test_rebase_history import AWU, STEPS, TODAY, _env, _legacy, build_fixture, run


def _fake_apple_health(path, rows):
    """A read-only stand-in for ~/health-data/health.duckdb: records in Dubai wall time."""
    c = duckdb.connect(str(path))
    c.execute("CREATE TABLE records (record_type VARCHAR, source_name VARCHAR, value DOUBLE, start_date TIMESTAMP, end_date TIMESTAMP)")
    c.executemany("INSERT INTO records VALUES (?, ?, ?, ?, ?)", rows)
    c.close()


def test_anchor_catches_a_decoupled_clock(tmp_path):
    """The era rule says era 1 (UTC wall) by ingest clock, but apple-health
    holds the same reading 60 minutes away: the rule is wrong for that row and
    the anchor says so. With a consistent reference the gate passes."""
    rhr = "HKQuantityTypeIdentifierRestingHeartRate"
    t = datetime(2026, 6, 10, 3, 0)                         # UTC; Dubai wall 07:00
    for name, wall, ok in (("good", t + timedelta(hours=4), True), ("bad", t + timedelta(hours=5), False)):
        path, conn, policy, reg = _env(tmp_path, f"{name}.duckdb")
        _legacy(conn, "ch2:r", "r-1", "resting_hr", rhr, t, 1, 55, "count/min", AWU, "apple_watch_ultra")
        conn.close()
        ah = tmp_path / f"ah-{name}.duckdb"
        _fake_apple_health(ah, [(rhr, AWU, 55.0, wall, wall)])
        m = rh.Migration(path, policy, reg, tmp_path / f"out-{name}", today=TODAY, label="test", cutover=True, apple_health=ah,
                         exceptions=("budget:whoop", "budget:export"))
        R = m.run()
        assert R["ok"] is ok, R["fails"]
        if not ok:
            assert "ah_anchor_nearest_match_delta_zero_for_every_anchored_row" in R["fails"]
            # Whole seconds since checkpoint C point 13 (no rounding to minutes): 60 minutes away.
            assert R["facts"]["ah_anchor_nearest_delta_by_era"] == [[1, 3600, 1]] and R["facts"]["ah_anchor_delta_unit"] == "seconds"


def test_whoop_day_row_in_an_era_gap_is_reconciled_not_fatal(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    _legacy(conn, "ch2:ok", "ok-1", "steps", STEPS, datetime(2026, 6, 1, 10, 0), 1, 5, "count", AWU, "apple_watch_ultra")
    conn.execute("INSERT INTO samples (sample_id, metric, value, unit, start_ts, end_ts, source_name, device_key, sync_path, ingested_at) VALUES "
                 "('wh:strain:2026-09-12', 'strain', 8.0, 'score', '2026-09-12 04:00', '2026-09-13 03:59', 'WHOOP', 'whoop', 'whoop_live', '2026-09-12 10:00:00')")
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True)
    assert R["ok"], R["fails"]
    assert R["facts"]["gap_rows_by_path"] == {"whoop_live": 1}
    assert R["facts"]["whoop_day_rows_by_outcome_note"] == [["strain", "quarantined", "no_record", 1]]
    assert R["facts"]["whoop_day_rows_by_era_outcome"] == [[0, "quarantined", 1]]
    c = duckdb.connect(str(path), read_only=True)
    assert c.execute("SELECT quality, time_source, rebase_era FROM samples WHERE sample_id = 'wh:strain:2026-09-12'").fetchone() == ("legacy_whoop_unresolved", None, None)
    assert c.execute("SELECT COUNT(*) FROM eligible_samples WHERE sample_id = 'wh:strain:2026-09-12'").fetchone()[0] == 0
    c.close()


def test_diff_classifier_flags_an_unexplained_daily_value_change(tmp_path):
    """Mutation test: a daily value that changed with no lineage behind it is
    reported as unexplained and fails the gate (it cannot hide in a reason)."""
    path, conn, policy, reg = _env(tmp_path)
    build_fixture(conn, policy, reg)
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True, rebuild=True)
    assert R["ok"] and R["facts"]["derived_diff_unexplained_cells"] == 0
    # Tamper the "before" snapshot: steps on 2026-10-05 (a native row, untouched by the migration).
    m = rh.Migration(path, policy, reg, tmp_path / "out", archive_dirs=[pathlib.Path(p).parent for p in R["facts"]["archive_places"]], today=TODAY, label="test")
    m.archive_paths = [pathlib.Path(p) for p in R["facts"]["archive_places"]]
    m.con = duckdb.connect(str(path))
    m.COLS = [r[0] for r in m.con.execute("DESCRIBE samples").fetchall()]
    before = tmp_path / "out" / "before_daily_values.parquet"
    m.con.execute(f"CREATE TEMP TABLE b AS SELECT * FROM read_parquet('{before}')")
    m.con.execute("UPDATE b SET value = value + 1 WHERE metric = 'steps' AND date = DATE '2026-10-05'")
    m.con.execute(f"COPY (SELECT * FROM b) TO '{before}' (FORMAT PARQUET)")
    with pytest.raises(rh.Stop, match="every_daily_value_difference_is_explained_by_lineage"):
        m.diff()
    assert m.R["facts"]["derived_diff_unexplained_cells"] == 1
    assert m.R["facts"]["derived_diff_unexplained_sample"] == [["steps", "2026-10-05", "changed"]]
    m.con.close()


def test_oracle_catches_a_tampered_daily_value(tmp_path):
    """Mutation test: the oracle recomputes from samples, so a wrong daily
    value is a mismatch even though the dispatcher produced it."""
    path, conn, policy, reg = _env(tmp_path)
    build_fixture(conn, policy, reg)
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True, rebuild=True)
    assert R["ok"] and R["facts"]["oracle"]["mismatches"] == 0
    m = rh.Migration(path, policy, reg, tmp_path / "out", today=TODAY, label="test")
    m.con = duckdb.connect(str(path))
    policy.sync_registry(m.con)
    # A wrong value on a cell that HAS a lineage reason (last_reorder), a deleted
    # cell, and a NaN: each is a mismatch, none hides behind a reason.
    m.con.execute("UPDATE daily_values SET value = value * 2 WHERE metric = 'resting_hr' AND date = DATE '2026-06-10'")
    m.con.execute("DELETE FROM daily_values WHERE metric = 'steps' AND date = DATE '2026-06-02'")
    m.con.execute("UPDATE daily_values SET value = 'NaN'::DOUBLE WHERE metric = 'body_mass' AND date = DATE '2026-06-10'")
    with pytest.raises(rh.Stop, match="independent_oracle_matches_every_rebuilt_daily_value_both_ways"):
        m.oracle()
    o = m.R["facts"]["oracle"]
    assert (o["value_mismatch"], o["missing"], o["non_finite"], o["mismatches"]) == (1, 1, 1, 3)
    assert "sample" not in o and all(not isinstance(v, float) or v == int(v) for v in o["delta_distribution"].values())
    cells = json.loads((tmp_path / "out" / "private-oracle-cells-test.json").read_text())
    assert cells["value_mismatch"] == [["2026-06-10", "resting_hr"]] and cells["missing"] == [["2026-06-02", "steps"]]
    m.con.close()


def test_a_tombstoned_uuid_landed_in_hk_reread_never_returns(tmp_path):
    """Apply-time tombstone dominance in isolation: the landing holds the
    uuid, the row is deleted afterwards, the migration ignores the landing."""
    from heliosd.ingest.bridge import ingest_batch
    from tests.test_rebase_history import _bridge_sample
    path, conn, policy, reg = _env(tmp_path)
    t = datetime(2026, 6, 1, 10, 0)
    _legacy(conn, "ch2:k", "k-1", "steps", STEPS, t, 1, 5, "count", AWU, "apple_watch_ultra")
    _legacy(conn, "ch2:k2", "k-1", "steps", STEPS, t, 2, 5, "count", AWU, "apple_watch_ultra")
    ingest_batch(conn, {"batch_id": "rr", "samples": [_bridge_sample("k-1", t, 5)]}, policy, reg)
    ingest_batch(conn, {"batch_id": "del", "samples": [], "deleted": ["k-1"]}, policy, reg)
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True)
    assert R["ok"], R["fails"]
    assert R["facts"]["reread_rows_tombstoned_ignored"] == 1 and R["facts"]["compare_uuids_in_both"] == 0
    c = duckdb.connect(str(path), read_only=True)
    assert c.execute("SELECT COUNT(*) FROM samples WHERE hk_uuid = 'k-1'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM hk_reread").fetchone()[0] == 1       # evidence kept, never applied
    c.close()
