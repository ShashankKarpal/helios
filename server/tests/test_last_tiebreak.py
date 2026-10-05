"""Phase 1b item (e): `last` orders by the instant (start_utc) and breaks a
same-instant tie by sample_id (owner decision 2026-10-05, 4c.3; adjudication-A
point 28). The expected winners are written into the fixture."""

from __future__ import annotations

from datetime import date, datetime

from heliosd.ingest.bridge import ingest_batch
from heliosd.signals.baselines import compute_daily_values
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

SCALE = "Zepp Life"
MASS = "HKQuantityTypeIdentifierBodyMass"
D = date(2026, 6, 10)


def _env():
    conn = db.connect_memory()
    policy = MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    return conn, policy, SourceRegistry()


def _mass(uuid, iso, value):
    return {"hk_type": MASS, "value": value, "unit": "kg", "start": iso, "end": iso, "source_name": SCALE, "uuid": uuid}


def _daily(conn, policy, reg):
    compute_daily_values(conn, policy, reg, D, D, as_of=D)
    return db.fetchall(conn, "SELECT value, n_samples FROM daily_values WHERE metric = 'body_mass' AND date = ?", [D])[0]


def test_same_instant_tie_goes_to_the_greater_sample_id_whatever_the_insert_order():
    for order in ((("hk-a", 80.5), ("hk-b", 80.1)), (("hk-b", 80.1), ("hk-a", 80.5))):
        conn, policy, reg = _env()
        ingest_batch(conn, {"batch_id": "b", "samples": [_mass(u, "2026-06-10T03:00:00Z", v) for u, v in order]}, policy, reg)
        assert _daily(conn, policy, reg) == (80.1, 2)          # hk:hk-b sorts after hk:hk-a


def test_the_later_instant_wins_over_an_earlier_one():
    conn, policy, reg = _env()
    ingest_batch(conn, {"batch_id": "b", "samples": [_mass("x", "2026-06-10T03:00:00Z", 80.5), _mass("y", "2026-06-10T05:00:00Z", 79.9)]}, policy, reg)
    assert _daily(conn, policy, reg) == (79.9, 2)


def test_before_the_migration_a_legacy_row_never_outranks_a_native_row_on_a_mixed_day():
    conn, policy, reg = _env()
    # A legacy row (NULL start_utc) with a LATER wall time than the native row's instant.
    db.execute(conn, "INSERT INTO samples (sample_id, hk_uuid, metric, hk_type, value, unit, start_ts, end_ts, source_name, device_key, sync_path) "
                     "VALUES ('ch2:old', 'old', 'body_mass', ?, 81.0, 'kg', '2026-06-10 20:00', '2026-06-10 20:00', ?, 'zepp_life_scale', 'bridge')", [MASS, SCALE])
    ingest_batch(conn, {"batch_id": "b", "samples": [_mass("new", "2026-06-10T03:00:00Z", 80.2)]}, policy, reg)
    assert _daily(conn, policy, reg) == (80.2, 2)
    # Two legacy rows order among themselves by wall time.
    db.execute(conn, "DELETE FROM samples WHERE hk_uuid = 'new'")
    db.execute(conn, "INSERT INTO samples (sample_id, hk_uuid, metric, hk_type, value, unit, start_ts, end_ts, source_name, device_key, sync_path) "
                     "VALUES ('ch2:old2', 'old2', 'body_mass', ?, 82.0, 'kg', '2026-06-10 21:00', '2026-06-10 21:00', ?, 'zepp_life_scale', 'bridge')", [MASS, SCALE])
    assert _daily(conn, policy, reg) == (82.0, 2)
