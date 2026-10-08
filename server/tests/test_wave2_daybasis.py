"""Wave 2 group B (design B3 to B8 and the B15 absolute threshold): which
reporting day a sample files under, which of a day's rows `last` takes, which
of a device's sync paths count for a metric, and what the Whoop records yield.

The policies here are self-contained (`_policy`), not the shipped files the
other Wave 2 groups edit, except where a test checks a shipped policy line.
Expected values are worked out by hand from the synthetic rows, never taken
from the code under test. Synthetic data only: the values, times and ids are
made up for the rule under test."""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta

from heliosd.signals.baselines import compute_daily_values
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

D = date(2025, 3, 4)
AS_OF = date(2025, 3, 12)          # every test day is history unless a test says otherwise

CONFIDENCE = {"weights": {"source_rank": 0.35, "freshness": 0.25, "coverage": 0.2, "agreement": 0.2},
              "grades": {"A": 0.85, "B": 0.7, "C": 0.5, "D": 0.0}, "agreement_tolerance_pct": 12}
RHR = {"hk": "HKQuantityTypeIdentifierRestingHeartRate", "unit": "count/min", "agg": "last",
       "direction": "lower", "priority": ["apple_watch_ultra", "zepp_helio"]}


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _policy(**metrics) -> MetricPolicy:
    return MetricPolicy({"metrics": copy.deepcopy(metrics), "confidence": copy.deepcopy(CONFIDENCE)},
                        default_tz="Asia/Dubai")


def _insert(conn, rows) -> None:
    """rows: (sample_id, metric, device_key, sync_path, start, end, value[, text_value]); reporting-zone walls."""
    db.insert_batch(conn, "INSERT INTO samples (sample_id, metric, device_key, sync_path, start_ts, end_ts, value, "
                          "text_value, source_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [[r[0], r[1], r[2], r[3], _t(r[4]), _t(r[5]), r[6], r[7] if len(r) > 7 else None,
                      f"Synthetic {r[2]}"] for r in rows])


def _store(policy: MetricPolicy, rows=()):
    conn = db.connect_memory()
    policy.sync_registry(conn)
    _insert(conn, rows)
    return conn


def _daily(conn, policy: MetricPolicy, metric: str, start: date, end: date, as_of: date = AS_OF) -> dict:
    """{date: (value, device_key, n_samples)} of one metric after a recompute of [start, end]."""
    compute_daily_values(conn, policy, SourceRegistry(), start, end, as_of=as_of)
    return {d: (v, dk, n) for d, v, dk, n in db.fetchall(
        conn, "SELECT date, value, device_key, n_samples FROM daily_values WHERE metric = ? ORDER BY 1", [metric])}


# ---- B4: `last` ties go to the latest start, then the latest END, then sample_id ----

def test_last_tie_prefers_latest_end():
    """Three versions of one day summary share a start (the source rewrote it
    through the day); the version that ends last is the final value. The ids
    are chosen so the old order (start, then sample_id) picks the midday
    version (63) instead of the final one (58)."""
    policy = _policy(resting_hr={**RHR, "day_basis": "calendar"})
    conn = _store(policy, [
        ("hk:rhr-a", "resting_hr", "apple_watch_ultra", "bridge", "2025-03-04 00:15", "2025-03-04 07:10", 61),
        ("hk:rhr-c", "resting_hr", "apple_watch_ultra", "bridge", "2025-03-04 00:15", "2025-03-04 12:40", 63),
        ("hk:rhr-b", "resting_hr", "apple_watch_ultra", "bridge", "2025-03-04 00:15", "2025-03-04 23:05", 58),
    ])
    assert _daily(conn, policy, "resting_hr", D, D) == {D: (58.0, "apple_watch_ultra", 3)}


def test_last_still_takes_the_latest_start_and_a_same_instant_tie_goes_to_the_greater_id():
    """Guard: the end rung only orders rows that share a start. A later start
    wins over a longer earlier row, and two rows with the same start and end
    fall to the greater sample_id (the 4c.3 rule for content twins)."""
    policy = _policy(resting_hr={**RHR, "day_basis": "calendar"})
    conn = _store(policy, [
        ("hk:rhr-long", "resting_hr", "apple_watch_ultra", "bridge", "2025-03-04 00:15", "2025-03-04 23:30", 60),
        ("hk:rhr-late", "resting_hr", "apple_watch_ultra", "bridge", "2025-03-04 06:00", "2025-03-04 06:05", 57),
        ("hk:twin-1", "resting_hr", "zepp_helio", "bridge", "2025-03-04 07:00", "2025-03-04 07:00", 55),
        ("hk:twin-2", "resting_hr", "zepp_helio", "bridge", "2025-03-04 07:00", "2025-03-04 07:00", 56),
    ])
    assert _daily(conn, policy, "resting_hr", D, D) == {D: (57.0, "apple_watch_ultra", 2)}
    assert db.fetchall(conn, "SELECT corroboration FROM daily_values") == [('{"zepp_helio": 56.0}',)]

