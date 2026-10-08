"""Wave 1 (fix program 2026-10-08), fork S: weekly review, insights, doctor
report, sleep report details, medians, computed_at and the body_temp label.
Every test here failed on the code before its fix (the failing line is noted
in the commit that carries it). Synthetic numbers only."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from heliosd.ingest.bridge import ingest_batch
from heliosd.signals import context, recompute as rc
from heliosd.signals.sleep_report import build_sleep_report
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

AWU = "Owner’s Ultra 1"


def _env():
    conn = db.connect_memory()
    policy = MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    return conn, policy, SourceRegistry()


def _daily(conn, d, metric, value, device="whoop", unit="h"):
    db.execute(conn, """
        INSERT OR REPLACE INTO daily_values
          (date, metric, value, unit, device_key, n_samples, confidence, grade)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", [d, metric, value, unit, device, 1, 0.9, "A"])


def _asleep_window(conn, sid, start: datetime, end: datetime, device="whoop"):
    minutes = (end - start).total_seconds() / 60
    db.execute(conn, """
        INSERT OR REPLACE INTO samples
          (sample_id, metric, value, text_value, unit, start_ts, end_ts, source_name, device_key, sync_path)
        VALUES (?, 'sleep_analysis', ?, 'asleep', 'min', ?, ?, 'WHOOP', ?, 'whoop_live')""",
        [sid, minutes, start, end, device])


# ---------------------------------------------------------------- A18 (S5) --

def test_sleep_report_median_is_the_true_median_for_an_even_night_count():
    conn, policy, _ = _env()
    today = date(2026, 7, 20)
    for i, h in enumerate([6.0, 6.5, 7.0, 8.0]):
        _daily(conn, today - timedelta(days=3 - i), "sleep_duration", h)
    rep = build_sleep_report(conn, days=31, policy=policy, today=today)
    # upper middle value was 7.0; the median of four nights is 6.75
    assert rep["summary"]["median"] == 6.75


def test_travel_flag_typical_midpoint_is_the_true_median():
    conn, policy, _ = _env()
    day = date(2026, 7, 20)
    # Six past nights with midpoints 01:00, 01:00, 01:30, 02:00, 02:30, 03:00
    # (sorted: upper middle 02:00, true median 01:45). Each night is a 6 h
    # window centred on its midpoint, ending on its own day.
    mids = [1.0, 1.0, 1.5, 2.0, 2.5, 3.0]
    for i, m in enumerate(mids, start=1):
        d = day - timedelta(days=i)
        mid = datetime.combine(d, datetime.min.time()) + timedelta(hours=m)
        _asleep_window(conn, f"past-{i}", mid - timedelta(hours=3), mid + timedelta(hours=3))
    # Last night's midpoint is 03:50: 1.83 h from 02:00 (no flag under the old
    # median) but 2.08 h from 01:45 (a shift of two hours or more flags).
    mid = datetime.combine(day, datetime.min.time()) + timedelta(hours=3, minutes=50)
    _asleep_window(conn, "last", mid - timedelta(hours=3), mid + timedelta(hours=3))
    assert "travel_or_shifted_schedule" in context.context_flags(conn, day, heat_months=[])
# ---------------------------------------------------------- A21 (T18) --

def test_daily_values_computed_at_is_refreshed_when_a_row_is_replaced():
    conn, policy, reg = _env()
    day = date(2026, 7, 10)
    t = datetime(2026, 7, 10, 9, 0)
    ingest_batch(conn, {"batch_id": "steps", "samples": [
        {"hk_type": "HKQuantityTypeIdentifierStepCount", "value": 1200, "unit": "count",
         "source_name": AWU, "start": (t - timedelta(hours=4)).isoformat() + "Z",
         "end": (t - timedelta(hours=3)).isoformat() + "Z", "uuid": "st-1"}]}, policy, reg)
    rc.recompute_dates(conn, policy, reg, {day}, today=day + timedelta(days=1))
    old = datetime(2020, 1, 1, 0, 0)
    db.execute(conn, "UPDATE daily_values SET computed_at = ? WHERE metric = 'steps' AND date = ?", [old, day])
    before = datetime.now()
    rc.recompute_dates(conn, policy, reg, {day}, today=day + timedelta(days=1))
    stamp = db.fetchall(conn, "SELECT computed_at FROM daily_values WHERE metric = 'steps' AND date = ?", [day])[0][0]
    assert stamp is not None and stamp >= before - timedelta(seconds=5), stamp
