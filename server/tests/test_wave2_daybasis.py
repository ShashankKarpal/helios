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


# ---- B3: interval metrics on the Dubai day that holds most of them; night metrics on the wake date ----

RR = {"hk": "HKQuantityTypeIdentifierRespiratoryRate", "unit": "count/min", "agg": "avg", "direction": "band",
      "day_basis": "sleep_end", "sample_context": "sleep_only", "priority": ["apple_watch_ultra"]}
TEMP = {"hk": "HKQuantityTypeIdentifierAppleSleepingWristTemperature", "unit": "degC", "direction": "band",
        "day_basis": "sleep_end", "sample_context": "sleep_only", "priority": ["apple_watch_ultra"]}
STEPS = {"hk": "HKQuantityTypeIdentifierStepCount", "unit": "count", "agg": "sum", "direction": "higher",
         "priority": ["apple_watch_ultra", "iphone"]}
PREV = D - timedelta(days=1)


def test_the_shipped_policy_sets_the_day_bases():
    p = MetricPolicy(default_tz="Asia/Dubai")
    assert p.day_basis("resting_hr") == "interval_midpoint"
    assert p.day_basis("wrist_temp") == "sleep_end"
    assert p.day_basis("steps") == "calendar" and p.day_basis("heart_rate") == "calendar"


def test_resting_hr_interval_files_on_majority_day():
    """Apple's resting HR summary runs 22:30 to 22:29: almost all of it is the
    second day, so it files there (old: the start date, a day early)."""
    policy = _policy(resting_hr={**RHR, "day_basis": "interval_midpoint"})
    conn = _store(policy, [("hk:rhr-1", "resting_hr", "apple_watch_ultra", "bridge", "2025-03-03 22:30", "2025-03-04 22:29", 59)])
    assert _daily(conn, policy, "resting_hr", PREV, D) == {D: (59.0, "apple_watch_ultra", 1)}


def test_an_interim_version_never_lands_on_the_previous_day():
    """Two versions of one summary share a start: an early interim one (ending
    before midnight, so its own midpoint is on the first day) and the final
    one. Only the latest-ending member of a same-start group counts, so the
    interim value never files a day early and the versions are one sample."""
    policy = _policy(resting_hr={**RHR, "day_basis": "interval_midpoint"})
    conn = _store(policy, [
        ("hk:rhr-z", "resting_hr", "apple_watch_ultra", "bridge", "2025-03-03 22:30", "2025-03-03 23:40", 64),
        ("hk:rhr-a", "resting_hr", "apple_watch_ultra", "bridge", "2025-03-03 22:30", "2025-03-04 22:29", 59),
        ("hk:rhr-b", "resting_hr", "apple_watch_ultra", "bridge", "2025-03-04 22:31", "2025-03-05 22:28", 60),
    ])
    assert _daily(conn, policy, "resting_hr", PREV, D + timedelta(days=1)) == {
        D: (59.0, "apple_watch_ultra", 1), D + timedelta(days=1): (60.0, "apple_watch_ultra", 1)}


def test_wrist_temp_files_on_wake_date():
    """Wrist temperature is measured over the night 21:40 to 05:50: it belongs
    to the night's wake date (old: the bed date)."""
    policy = _policy(wrist_temp=TEMP)
    conn = _store(policy, [("hk:t-1", "wrist_temp", "apple_watch_ultra", "bridge", "2025-03-03 21:40", "2025-03-04 05:50", 34.12)])
    assert _daily(conn, policy, "wrist_temp", PREV, D) == {D: (34.12, "apple_watch_ultra", 1)}


def test_step_sums_keep_start_date():
    """Guard: steps stay on the calendar basis. A 23:50 to 00:10 interval counts
    on its start date, as Apple Health counts it."""
    policy = _policy(steps=STEPS)
    conn = _store(policy, [
        ("hk:s-1", "steps", "apple_watch_ultra", "bridge", "2025-03-03 18:00", "2025-03-03 18:20", 400),
        ("hk:s-2", "steps", "apple_watch_ultra", "bridge", "2025-03-03 23:50", "2025-03-04 00:10", 120),
        ("hk:s-3", "steps", "apple_watch_ultra", "bridge", "2025-03-04 09:00", "2025-03-04 09:30", 900),
    ])
    assert _daily(conn, policy, "steps", PREV, D) == {PREV: (520.0, "apple_watch_ultra", 2), D: (900.0, "apple_watch_ultra", 1)}


def _fake_wake(calls, lo="2025-03-03 22:30", hi="2025-03-04 06:15", wake=D):
    """A stand-in for the episode builder: one main episode [lo, hi] waking on `wake`."""
    def point_wake_dates(conn, policy, device_key, instants):
        calls.append((device_key, list(instants)))
        return [wake if _t(lo) <= t <= _t(hi) else None for t in instants]
    return point_wake_dates


RR_POINTS = [("hk:rr-1", "respiratory_rate", "apple_watch_ultra", "bridge", "2025-03-03 23:00", "2025-03-03 23:00", 14.2),
             ("hk:rr-2", "respiratory_rate", "apple_watch_ultra", "bridge", "2025-03-04 03:00", "2025-03-04 03:00", 14.8),
             ("hk:rr-3", "respiratory_rate", "apple_watch_ultra", "bridge", "2025-03-04 15:00", "2025-03-04 15:00", 19.0)]


def test_sleep_only_points_follow_their_episode(monkeypatch):
    """Points of a sleep_end metric file on the wake date of the main episode
    that holds them: 23:00 and 03:00 both on the wake date. With sleep_only, a
    15:00 point that no main episode holds is dropped (old: 23:00 alone on the
    first day, 03:00 averaged with 15:00 on the second). The builder is asked
    once per key, with the instants in order."""
    from heliosd.signals import episodes
    calls: list = []
    monkeypatch.setattr(episodes, "point_wake_dates", _fake_wake(calls))
    policy = _policy(respiratory_rate=RR)
    conn = _store(policy, RR_POINTS)
    assert _daily(conn, policy, "respiratory_rate", PREV, D) == {D: (14.5, "apple_watch_ultra", 2)}
    assert calls == [("apple_watch_ultra", [_t("2025-03-03 23:00"), _t("2025-03-04 03:00"), _t("2025-03-04 15:00")])]


def test_a_point_outside_every_episode_keeps_its_own_date_unless_sleep_only(monkeypatch):
    from heliosd.signals import episodes
    monkeypatch.setattr(episodes, "point_wake_dates", _fake_wake([]))
    policy = _policy(respiratory_rate={**RR, "sample_context": "all_day"})
    conn = _store(policy, RR_POINTS)
    assert _daily(conn, policy, "respiratory_rate", PREV, D) == {D: (16.0, "apple_watch_ultra", 3)}


def test_a_single_date_recompute_gives_the_rows_of_a_range_recompute(monkeypatch):
    """The candidate windows of each basis: recomputing one date alone gives
    exactly the rows a recompute of the whole range gives for it, for rows
    that cross midnight on every basis."""
    from heliosd.signals import episodes
    monkeypatch.setattr(episodes, "point_wake_dates", _fake_wake([]))
    policy = _policy(resting_hr={**RHR, "day_basis": "interval_midpoint"}, respiratory_rate=RR, wrist_temp=TEMP, steps=STEPS)
    rows = [*RR_POINTS,
            ("hk:rhr-z", "resting_hr", "apple_watch_ultra", "bridge", "2025-03-03 22:30", "2025-03-03 23:40", 64),
            ("hk:rhr-a", "resting_hr", "apple_watch_ultra", "bridge", "2025-03-03 22:30", "2025-03-04 22:29", 59),
            ("hk:rhr-0", "resting_hr", "apple_watch_ultra", "bridge", "2025-03-02 22:30", "2025-03-03 22:29", 61),
            ("hk:t-1", "wrist_temp", "apple_watch_ultra", "bridge", "2025-03-03 21:40", "2025-03-04 05:50", 34.12),
            ("hk:t-0", "wrist_temp", "apple_watch_ultra", "bridge", "2025-03-02 22:10", "2025-03-03 06:20", 34.4),
            ("hk:s-2", "steps", "apple_watch_ultra", "bridge", "2025-03-03 23:50", "2025-03-04 00:10", 120)]
    days = [PREV - timedelta(days=1), PREV, D, D + timedelta(days=1)]
    whole = _store(policy, rows)
    compute_daily_values(whole, policy, SourceRegistry(), days[0], days[-1], as_of=AS_OF)
    alone = _store(policy, rows)
    for d in days:
        compute_daily_values(alone, policy, SourceRegistry(), d, d, as_of=AS_OF)
    q = "SELECT date, metric, value, device_key, n_samples, grade FROM daily_values ORDER BY 1, 2"
    assert db.fetchall(alone, q) == db.fetchall(whole, q)
    assert len(db.fetchall(whole, q)) == 6
