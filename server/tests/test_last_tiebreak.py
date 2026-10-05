"""Phase 1b item (e): `last` is the latest instant of the day, a same-instant
tie goes to sample_id (owner decision 2026-10-05, 4c.3; adjudication-A point
28). The order is the reporting-zone wall, which equals the instant order for
every row that has an instant (no daylight saving in the zone; the migration
asserts the rendering); checkpoint B point 22 rejected a comparator that put
instant-less legacy rows first. The expected winners are written into the
fixture."""

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


def test_before_the_migration_the_order_is_the_wall_order_for_every_row():
    """A native row the re-read inserts into history must not outrank a legacy
    row of that day by construction (checkpoint B point 22): both order by
    their reporting-zone wall, as Phase 1a did."""
    conn, policy, reg = _env()
    db.execute(conn, "INSERT INTO samples (sample_id, hk_uuid, metric, hk_type, value, unit, start_ts, end_ts, source_name, device_key, sync_path) "
                     "VALUES ('ch2:old', 'old', 'body_mass', ?, 81.0, 'kg', '2026-06-10 20:00', '2026-06-10 20:00', ?, 'zepp_life_scale', 'bridge')", [MASS, SCALE])
    ingest_batch(conn, {"batch_id": "b", "samples": [_mass("new", "2026-06-10T03:00:00Z", 80.2)]}, policy, reg)   # 07:00 Dubai wall
    assert _daily(conn, policy, reg) == (81.0, 2)
    ingest_batch(conn, {"batch_id": "b2", "samples": [_mass("newer", "2026-06-10T17:30:00Z", 80.4)]}, policy, reg)  # 21:30 Dubai wall
    assert _daily(conn, policy, reg) == (80.4, 3)
