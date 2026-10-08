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
import json
from datetime import date, datetime, timedelta

from heliosd.config import load_metric_policy
from heliosd.ingest import whoop
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
    if not rows:
        return
    db.insert_batch(conn, "INSERT INTO samples (sample_id, metric, device_key, sync_path, start_ts, end_ts, value, "
                          "text_value, source_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [[r[0], r[1], r[2], r[3], _t(r[4]), _t(r[5]), r[6], r[7] if len(r) > 7 else None,
                      f"Synthetic {r[2]}"] for r in rows])


def _store(policy: MetricPolicy, rows=()):
    conn = db.connect_memory()
    policy.sync_registry(conn)
    _insert(conn, rows)
    return conn


def _sleep_rec(id_, start_z: str, end_z: str, rr=None, need=None, light=300, sws=90, rem=100,
               updated="2025-03-11T00:00:00.000Z", state="SCORED", nap=False, cycle_id=None) -> dict:
    """A Whoop API v2 sleep record (UTC instants with a Z, as the API sends them)."""
    score = {"stage_summary": {"total_in_bed_time_milli": 9 * 3600000, "total_awake_time_milli": 20 * 60000,
                               "total_light_sleep_time_milli": light * 60000,
                               "total_slow_wave_sleep_time_milli": sws * 60000,
                               "total_rem_sleep_time_milli": rem * 60000},
             "sleep_efficiency_percentage": 91.0}
    if rr is not None:
        score["respiratory_rate"] = rr
    if need is not None:
        score["sleep_needed"] = need
    return {"id": id_, "user_id": 1, "cycle_id": cycle_id, "created_at": end_z, "updated_at": updated,
            "start": start_z, "end": end_z, "timezone_offset": "+04:00", "nap": nap, "score_state": state,
            "score": score if state == "SCORED" else None}


def _apply(conn, policy: MetricPolicy, kind: str, *recs) -> set[date]:
    """Store Whoop API records the way the puller does; returns the dates it journaled."""
    dirty: set[date] = set()
    with db.transaction(conn) as c:
        for rec in recs:
            dirty |= whoop.apply_record(c, kind, rec, policy, datetime(2025, 3, 11, 9, 0), "pull-test")["dirty"]
    return dirty


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



def test_sleep_end_points_reach_sql_as_runs_of_one_wake_date(monkeypatch):
    """The builder's answers reach the aggregation as runs: consecutive points
    of a key with one wake date are one (key, first, last, wake date) range,
    so a year of readings binds a few thousand ranges instead of one
    parameter per point (over 20 s for a year of readings before). Two
    adjacent nights, a daytime reading between them, a repeated instant."""
    from heliosd.signals import baselines as bl
    from heliosd.signals import episodes
    nights = {D: ("2025-03-03 22:00", "2025-03-04 06:00"), NEXT: ("2025-03-04 22:30", "2025-03-05 06:30")}

    def wake(conn, policy, key, instants):
        return [next((d for d, (lo, hi) in nights.items() if _t(lo) <= t <= _t(hi)), None) for t in instants]
    monkeypatch.setattr(episodes, "point_wake_dates", wake)
    times = ["2025-03-03 23:00", "2025-03-04 03:00", "2025-03-04 03:00", "2025-03-04 12:00", "2025-03-04 23:00", "2025-03-05 05:00"]
    values = [14.0, 15.0, 16.0, 20.0, 13.0, 14.0]
    policy = _policy(respiratory_rate=RR)
    conn = _store(policy, [(f"hk:rr-{i}", "respiratory_rate", "apple_watch_ultra", "bridge", t, t, v)
                           for i, (t, v) in enumerate(zip(times, values))])
    assert _daily(conn, policy, "respiratory_rate", PREV, NEXT) == {
        D: (15.0, "apple_watch_ultra", 3), NEXT: (13.5, "apple_watch_ultra", 2)}
    assert bl._wake_runs(conn, policy, [("apple_watch_ultra", _t(t)) for t in times]) == [
        ("apple_watch_ultra", _t(times[0]), _t(times[2]), D), ("apple_watch_ultra", _t(times[4]), _t(times[5]), NEXT)]

# ---- B6: respiratory rate, one value per night on the wake date; Whoop's HealthKit copy never blends ----

def test_the_shipped_policy_takes_whoop_respiratory_rate_from_the_api_on_the_wake_date():
    p = MetricPolicy(default_tz="Asia/Dubai")
    assert p.day_basis("respiratory_rate") == "sleep_end" and p.sample_context("respiratory_rate") == "sleep_only"
    assert p.sync_paths("respiratory_rate") == {"whoop": ["whoop_live"]}
    assert p.priority("respiratory_rate")[0] == "whoop"


def test_respiratory_rate_one_night_per_wake_date():
    """Two Whoop API nights (21:00 to 04:30, then 21:09 to 04:15) and the
    HealthKit copy of each, written at the wake. Each API night files on its
    wake date and the copies never count (old: the second night's API value
    averaged with the first night's copy on the bed date)."""
    policy = MetricPolicy(default_tz="Asia/Dubai")
    conn = _store(policy)
    _apply(conn, policy, "sleep",
           _sleep_rec("n1", "2025-03-02T17:00:00.000Z", "2025-03-03T00:30:00.000Z", rr=16.4),
           _sleep_rec("n2", "2025-03-03T17:09:00.000Z", "2025-03-04T00:15:00.000Z", rr=16.9))
    _insert(conn, [("hk:wrr-1", "respiratory_rate", "whoop", "bridge", "2025-03-03 04:35", "2025-03-03 04:35", 16.2),
                   ("hk:wrr-2", "respiratory_rate", "whoop", "bridge", "2025-03-04 04:20", "2025-03-04 04:20", 17.3)])
    assert _daily(conn, policy, "respiratory_rate", D - timedelta(days=2), D) == {
        PREV: (16.4, "whoop", 1), D: (16.9, "whoop", 1)}


def test_hk_rr_copy_never_blends_with_the_api_value():
    """A night that starts after midnight: the API value and the HealthKit copy
    share a date on every basis. The copy is stored and eligible, yet it is
    neither the value nor corroboration (old: the two averaged)."""
    policy = MetricPolicy(default_tz="Asia/Dubai")
    conn = _store(policy)
    _apply(conn, policy, "sleep", _sleep_rec("n3", "2025-03-03T20:30:00.000Z", "2025-03-04T03:00:00.000Z", rr=17.0))
    _insert(conn, [("hk:wrr-3", "respiratory_rate", "whoop", "bridge", "2025-03-04 07:05", "2025-03-04 07:05", 18.0)])
    assert _daily(conn, policy, "respiratory_rate", D, D) == {D: (17.0, "whoop", 1)}
    assert db.fetchall(conn, "SELECT corroboration FROM daily_values WHERE metric = 'respiratory_rate'") == [(None,)]
    assert db.fetchall(conn, "SELECT COUNT(*) FROM eligible_samples WHERE sample_id = 'hk:wrr-3'") == [(1,)]


def test_a_listed_healthkit_key_arbitrates_the_copy_as_its_own_key(monkeypatch):
    """Owner question Q1 is one policy line: with whoop:healthkit listed after
    whoop, a night without an API record takes Whoop's HealthKit copy under
    its own key (a labelled fallback), and a night with one keeps the API
    value under whoop. Rows of other paths never count under whoop itself."""
    from heliosd.signals import episodes
    # Every reading here sits inside a night that wakes on its own date.
    monkeypatch.setattr(episodes, "point_wake_dates", lambda conn, policy, key, instants: [t.date() for t in instants])
    policy = _policy(respiratory_rate={**RR, "priority": ["whoop", "whoop:healthkit", "apple_watch_ultra"],
                                       "sync_paths": {"whoop": ["whoop_live"]}})
    conn = _store(policy, [
        ("wh:respiratory_rate:sleep:n4", "respiratory_rate", "whoop", "whoop_live", "2025-03-03 21:09", "2025-03-04 04:15", 16.9),
        ("hk:wrr-4", "respiratory_rate", "whoop", "bridge", "2025-03-03 04:00", "2025-03-03 04:00", 16.1),
        ("hk:wrr-5", "respiratory_rate", "whoop", "bridge", "2025-03-04 04:00", "2025-03-04 04:00", 17.3),
        ("hk:arr-1", "respiratory_rate", "apple_watch_ultra", "bridge", "2025-03-03 03:00", "2025-03-03 03:00", 15.0),
    ])
    assert _daily(conn, policy, "respiratory_rate", PREV, D) == {PREV: (16.1, "whoop:healthkit", 1), D: (16.9, "whoop", 1)}


# ---- B8: sleep need = baseline + debt + recent strain + recent nap, on the wake date ----

H = 3600000      # one hour in milliseconds


def _need(baseline=7.5, debt=0.0, strain=0.0, nap=0.0, drop=()) -> dict:
    parts = {"baseline_milli": baseline * H, "need_from_sleep_debt_milli": debt * H,
             "need_from_recent_strain_milli": strain * H, "need_from_recent_nap_milli": nap * H}
    return {k: v for k, v in parts.items() if k not in drop}


def _need_samples(rec) -> list[float]:
    return [s["value"] for s in whoop.derive_samples("sleep", rec) if s["metric"] == "sleep_need"]


NIGHT = ("2025-03-03T17:30:00.000Z", "2025-03-04T01:40:00.000Z")      # 21:30 to 05:40 Dubai


def test_sleep_need_sums_the_four_parts():
    """7.5 h baseline + 0.6 h of debt + 0.15 h for recent strain + no nap
    (old: the baseline alone, 7.5 on every night)."""
    assert _need_samples(_sleep_rec("q1", *NIGHT, need=_need(debt=0.6, strain=0.15))) == [8.25]


def test_negative_nap_part_lowers_need():
    """A recent nap lowers the need (Whoop stores that part as a negative
    number); a missing part counts 0; no baseline, no sample."""
    assert _need_samples(_sleep_rec("q2", *NIGHT, need=_need(debt=0.3, nap=-0.4))) == [7.4]
    assert _need_samples(_sleep_rec("q3", *NIGHT, need=_need(drop=("need_from_sleep_debt_milli",
                                                                   "need_from_recent_nap_milli")))) == [7.5]
    assert _need_samples(_sleep_rec("q4", *NIGHT, need=_need(debt=1.0, drop=("baseline_milli",)))) == []


def test_sleep_need_files_on_wake_date():
    """The need of the night 21:30 to 05:40 sits on the wake date, beside the
    night's sleep (old: on the bed date)."""
    policy = MetricPolicy(default_tz="Asia/Dubai")
    assert policy.day_basis("sleep_need") == "sleep_end"
    conn = _store(policy)
    _apply(conn, policy, "sleep", _sleep_rec("q5", *NIGHT, need=_need(debt=0.6, strain=0.15)))
    assert _daily(conn, policy, "sleep_need", PREV, D) == {D: (8.25, "whoop", 1)}


def test_rederive_all_rewrites_stored_records_once_and_records_a_verified_migration():
    """The rebuild re-derives every stored record from its payload: a sample
    written by the old rule is corrected, a sample the payload no longer
    yields is retracted, and a second run writes nothing at all."""
    policy = MetricPolicy(default_tz="Asia/Dubai")
    conn = _store(policy)
    _apply(conn, policy, "sleep", _sleep_rec("q6", *NIGHT, rr=15.5, need=_need(debt=0.6, strain=0.15)),
           _sleep_rec("q7", "2025-03-04T17:20:00.000Z", "2025-03-05T01:10:00.000Z", need=_need(nap=-0.5)))
    # The store as the old rule left it: the baseline alone, and a sample the payload does not yield.
    conn.execute("UPDATE samples SET value = 7.5 WHERE sample_id = 'wh:sleep_need:sleep:q6'")
    conn.execute("INSERT INTO samples (sample_id, metric, value, unit, start_ts, end_ts, source_name, device_key, sync_path) "
                 "VALUES ('wh:strain:sleep:q7', 'strain', 3.0, 'score', '2025-03-04 21:20', '2025-03-05 05:10', 'WHOOP', 'whoop', 'whoop_live')")
    out = whoop.rederive_all(conn, policy, code_commit="abc123")
    assert (out["records"], out["unchanged"], out["rewritten"], out["samples_written"], out["samples_retracted"]) == (2, 0, 2, 1, 1)
    assert out["samples_written_by_metric"] == {"sleep_need": 1}
    vals = dict(db.fetchall(conn, "SELECT sample_id, value FROM samples WHERE metric = 'sleep_need'"))
    assert vals == {"wh:sleep_need:sleep:q6": 8.25, "wh:sleep_need:sleep:q7": 7.0}
    assert db.fetchall(conn, "SELECT reason FROM tombstones WHERE tomb_id = 'wh:strain:sleep:q7'") == [("whoop_retracted",)]
    name, applied, commit, summary = db.fetchall(conn, "SELECT name, applied_at, code_commit, summary FROM migrations")[0]
    assert (name, commit, json.loads(summary)["phase"]) == ("wave2_whoop_rederive_v1", "abc123", "verified")
    assert db.unverified_migrations(conn) == []
    before = db.fetchall(conn, "SELECT * FROM samples ORDER BY sample_id")
    again = whoop.rederive_all(conn, policy, code_commit="def456")
    assert (again["unchanged"], again["rewritten"], again["samples_written"], again["samples_retracted"]) == (2, 0, 0, 0)
    assert db.fetchall(conn, "SELECT * FROM samples ORDER BY sample_id") == before            # not even ingested_at moved
    assert db.fetchall(conn, "SELECT applied_at, code_commit FROM migrations") == [(applied, "abc123")]


# ---- B7: strain on the recovery's day; the open cycle is in progress ----

NEXT = D + timedelta(days=1)


def _cycle_rec(id_, start_z: str, end_z: str | None, strain=9.4, updated="2025-03-11T00:00:00.000Z") -> dict:
    """A Whoop API v2 cycle; end None is the open cycle."""
    return {"id": id_, "user_id": 1, "created_at": start_z, "updated_at": updated, "start": start_z, "end": end_z,
            "timezone_offset": "+04:00", "score_state": "SCORED",
            "score": {"strain": strain, "kilojoule": 8000.0, "average_heart_rate": 70, "max_heart_rate": 150}}


def _recovery_rec(cycle_id, created_z: str, score=55, hrv=48.0, rhr=None, sleep_id="s-1", updated=None) -> dict:
    """A Whoop API v2 recovery (identity: its cycle_id)."""
    sc = {"user_calibrating": False, "recovery_score": score, "hrv_rmssd_milli": hrv}
    if rhr is not None:
        sc["resting_heart_rate"] = rhr
    return {"cycle_id": cycle_id, "sleep_id": sleep_id, "user_id": 1, "created_at": created_z,
            "updated_at": updated or created_z, "score_state": "SCORED", "timezone_offset": "+04:00", "score": sc}


def test_strain_files_on_its_recovery_day():
    """The cycle from 22:10 to 22:40 the next evening is the day its recovery
    opens (06:05): its strain files there (old: on the start date, a day early)."""
    policy = MetricPolicy(default_tz="Asia/Dubai")
    assert policy.day_basis("strain") == "whoop_cycle"
    conn = _store(policy)
    _apply(conn, policy, "recovery", _recovery_rec(701, "2025-03-04T02:05:00.000Z"))
    _apply(conn, policy, "cycle", _cycle_rec(701, "2025-03-03T18:10:00.000Z", "2025-03-04T18:40:00.000Z", strain=9.4))
    assert _daily(conn, policy, "strain", PREV, NEXT) == {D: (9.4, "whoop", 1)}


def test_cycle_without_recovery_uses_start_plus_12h():
    policy = MetricPolicy(default_tz="Asia/Dubai")
    conn = _store(policy)
    _apply(conn, policy, "cycle", _cycle_rec(702, "2025-03-03T18:00:00.000Z", "2025-03-04T17:00:00.000Z", strain=7.2),
           _cycle_rec(703, "2025-03-05T05:00:00.000Z", "2025-03-05T19:00:00.000Z", strain=4.0))
    # 22:00 + 12 h is the next day; 09:00 + 12 h stays on its day.
    assert _daily(conn, policy, "strain", PREV, NEXT) == {D: (7.2, "whoop", 1), NEXT: (4.0, "whoop", 1)}


def test_two_cycles_starting_one_date_both_kept():
    """A cycle that starts after midnight (00:20) and the next one that starts
    the same evening (22:30) each file on their own recovery's day (old: both
    on the start date, where `last` dropped the first)."""
    policy = MetricPolicy(default_tz="Asia/Dubai")
    conn = _store(policy)
    _apply(conn, policy, "recovery", _recovery_rec(704, "2025-03-04T03:00:00.000Z"), _recovery_rec(705, "2025-03-05T02:45:00.000Z"))
    _apply(conn, policy, "cycle", _cycle_rec(704, "2025-03-03T20:20:00.000Z", "2025-03-04T18:30:00.000Z", strain=12.1),
           _cycle_rec(705, "2025-03-04T18:30:00.000Z", "2025-03-05T19:00:00.000Z", strain=6.3))
    assert _daily(conn, policy, "strain", PREV, NEXT) == {D: (12.1, "whoop", 1), NEXT: (6.3, "whoop", 1)}


def test_open_cycle_is_in_progress_and_ungraded():
    """The open cycle (no end yet) is strain so far: no confidence, no grade,
    state in_progress with its own reason, on its recovery's day and still
    after midnight, and not a leftover of a closed day. The pull that closes
    it journals the day, which is then graded (old: filed on the start date,
    graded and judged like a finished day)."""
    from heliosd.signals import recompute as rc
    from heliosd.signals.markers import signals_for
    policy = MetricPolicy(default_tz="Asia/Dubai")
    reg = SourceRegistry()
    conn = _store(policy)
    _apply(conn, policy, "recovery", _recovery_rec(710, "2025-03-04T02:30:00.000Z"))
    _apply(conn, policy, "cycle", _cycle_rec(710, "2025-03-03T18:40:00.000Z", None, strain=3.1))
    rc.drain_journal(conn, policy, reg, today=D)
    q = "SELECT date, value, confidence, grade, detail FROM daily_values WHERE metric = 'strain'"
    assert db.fetchall(conn, q) == [(D, 3.1, None, None, '{"cycle_id": "710", "in_progress": true}')]
    from heliosd.signals.markers import CYCLE_OPEN_WHY
    for today in (D, NEXT):                          # after midnight the cycle is still open
        s = {r["metric"]: r for r in signals_for(conn, D, policy, today)}["strain"]
        assert (s["state"], s["why"], s["grade"], s["delta_pct"]) == ("in_progress", CYCLE_OPEN_WHY, None, None)
    assert rc.leftover_dates(conn, NEXT) == set()
    dirty = _apply(conn, policy, "cycle", _cycle_rec(710, "2025-03-03T18:40:00.000Z", "2025-03-04T19:50:00.000Z",
                                                     strain=11.6, updated="2025-03-12T00:00:00.000Z"))
    assert D in dirty
    rc.drain_journal(conn, policy, reg, today=NEXT)
    (d, v, cf, g, de), = db.fetchall(conn, q)
    assert (d, v, de) == (D, 11.6, None) and cf is not None and g is not None
    assert {r["metric"]: r for r in signals_for(conn, D, policy, NEXT)}["strain"]["state"] != "in_progress"


def test_an_open_cycle_is_not_a_leftover_of_a_closed_day():
    """leftover_dates finalizes closed days still in their in-progress state,
    except a value whose detail says in_progress (old: such a day came back on
    every pass, though recomputing it changes nothing until the cycle closes)."""
    from heliosd.signals import recompute as rc
    conn = db.connect_memory()
    conn.execute("INSERT INTO daily_values (date, metric, value, unit, device_key, n_samples, detail) VALUES "
                 "(?, 'strain', 3.1, 'score', 'whoop', 1, '{\"cycle_id\": \"710\", \"in_progress\": true}'), "
                 "(?, 'steps', 900, 'count', 'iphone', 1, NULL)", [D, PREV])
    conn.execute("INSERT INTO signals (date, metric, state, value, why) VALUES (?, 'strain', 'in_progress', 3.1, 'x'), "
                 "(?, 'steps', 'in_progress', 900, 'x')", [D, PREV])
    assert rc.leftover_dates(conn, NEXT) == {PREV}


def test_a_cycle_and_its_recovery_journal_the_cycle_day():
    """Either record can move a cycle's strain, so each journals the day it
    files on: an open cycle's own sample is a point at its start (the bed
    date), yet its strain files on the recovery's day; a recovery that lands
    after its cycle moves the strain from the 12-hour day to its own day."""
    policy = MetricPolicy(default_tz="Asia/Dubai")
    conn = _store(policy)
    assert _apply(conn, policy, "recovery", _recovery_rec(720, "2025-03-04T02:30:00.000Z")) == {D}
    assert _apply(conn, policy, "cycle", _cycle_rec(720, "2025-03-03T18:40:00.000Z", None, strain=2.0)) == {PREV, D}
    # A cycle starting at 09:00 files 12 h later on its start date until its recovery arrives the next morning.
    assert _apply(conn, policy, "cycle", _cycle_rec(721, "2025-03-03T05:00:00.000Z", "2025-03-04T17:00:00.000Z", strain=8.0)) == {PREV, D}
    assert _apply(conn, policy, "recovery", _recovery_rec(721, "2025-03-04T01:00:00.000Z")) == {D, PREV}


# ---- B5 as overridden by D11: Whoop's cloud resting HR first; Apple and Helio are labelled fallbacks ----

def _d11_policy() -> MetricPolicy:
    """The shipped policy with resting HR in the owner's D11 order. Group C
    commits that list to the policy files; patching it here keeps these tests
    independent of that commit."""
    cfg = load_metric_policy()
    cfg["metrics"]["resting_hr"] = {**cfg["metrics"]["resting_hr"], "priority": ["whoop", "apple_watch_ultra", "zepp_helio"]}
    return MetricPolicy(cfg, default_tz="Asia/Dubai")


APPLE_RHR_D = ("hk:arhr-1", "resting_hr", "apple_watch_ultra", "bridge", "2025-03-03 22:30", "2025-03-04 22:29", 61)


def test_the_shipped_policy_takes_whoop_resting_hr_from_the_api():
    p = MetricPolicy(default_tz="Asia/Dubai")
    assert p.sync_paths("resting_hr") == {"whoop": ["whoop_live"]}
    assert "resting_hr" in whoop.KIND_METRICS["recovery"]


def test_resting_hr_from_whoop_recovery_record():
    """The recovery record carries Whoop's overnight resting HR: it becomes a
    sample at the wake (created_at), and under D11 it is the day's value with
    Apple's day summary beside it (old: no sample, Apple chosen)."""
    policy = _d11_policy()
    conn = _store(policy, [APPLE_RHR_D])
    _apply(conn, policy, "recovery", _recovery_rec(730, "2025-03-04T02:30:00.000Z", rhr=54))
    assert db.fetchall(conn, "SELECT value, unit, start_ts, sync_path FROM samples WHERE sample_id = "
                             "'wh:resting_hr:recovery:730'") == [(54.0, "count/min", _t("2025-03-04 06:30"), "whoop_live")]
    assert _daily(conn, policy, "resting_hr", PREV, D) == {D: (54.0, "whoop", 1)}
    assert db.fetchall(conn, "SELECT corroboration FROM daily_values WHERE metric = 'resting_hr'") == [('{"apple_watch_ultra": 61.0}',)]


def test_whoop_hk_rhr_copy_is_not_the_whoop_value():
    """Whoop's HealthKit copy (70) and its cloud value (66) on one day give 66:
    the copy is stored and never arbitrated (old: the copy was the whoop
    value, and the cloud value had no sample)."""
    policy = _d11_policy()
    conn = _store(policy, [("hk:wrhr-1", "resting_hr", "whoop", "bridge", "2025-03-04 06:35", "2025-03-04 06:35", 70)])
    _apply(conn, policy, "recovery", _recovery_rec(731, "2025-03-04T02:30:00.000Z", rhr=66))
    assert _daily(conn, policy, "resting_hr", D, D) == {D: (66.0, "whoop", 1)}


def test_apple_rhr_day_is_labelled_fallback_without_delta():
    """A day with Apple's summary and Whoop's HealthKit copy but no cloud
    record: Apple stands in for Whoop, labelled a fallback with no delta
    against the Whoop baseline (old: the copy counted as Whoop's value and
    was judged against that baseline)."""
    from heliosd.signals.markers import compute_signals, signals_for
    policy = _d11_policy()
    conn = _store(policy, [APPLE_RHR_D, ("hk:wrhr-2", "resting_hr", "whoop", "bridge", "2025-03-04 06:35", "2025-03-04 06:35", 58)])
    conn.execute("INSERT INTO baselines (date, metric, window_days, median, mad, n_days) VALUES (?, 'resting_hr', 30, 55.0, 2.0, 20)", [D])
    _daily(conn, policy, "resting_hr", D, D)
    compute_signals(conn, policy, D, today=AS_OF)
    s = {r["metric"]: r for r in signals_for(conn, D, policy, AS_OF)}["resting_hr"]
    assert (s["value"], s["device_key"], s["state"], s["delta_pct"], s["fallback"]) == (61.0, "apple_watch_ultra", "fallback", None, True)


def test_whoop_rhr_today_is_final_apple_rhr_today_is_so_far():
    """On the reporting today Whoop's cloud resting HR is the night's final
    value: graded, and judged once a baseline exists. Apple's day summary is
    still being rewritten: "so far", no grade (old: every resting HR of the
    reporting today was so far, whatever its device)."""
    from heliosd.signals.markers import IN_PROGRESS_WHY, compute_signals, signals_for
    policy = _d11_policy()
    conn = _store(policy, [("hk:arhr-2", "resting_hr", "apple_watch_ultra", "bridge", "2025-03-04 22:30", "2025-03-05 22:29", 60)])
    _apply(conn, policy, "recovery", _recovery_rec(732, "2025-03-04T02:30:00.000Z", rhr=53))
    for day in (D, NEXT):                                                  # each day while it is the reporting today
        compute_daily_values(conn, policy, SourceRegistry(), day, day, as_of=day)
        compute_signals(conn, policy, day, today=day)
    rows = {d: (dk, g) for d, dk, g in db.fetchall(conn, "SELECT date, device_key, grade FROM daily_values WHERE metric = 'resting_hr'")}
    assert rows[D][0] == "whoop" and rows[D][1] is not None
    assert rows[NEXT] == ("apple_watch_ultra", None)
    s_d = {r["metric"]: r for r in signals_for(conn, D, policy, D)}["resting_hr"]
    s_n = {r["metric"]: r for r in signals_for(conn, NEXT, policy, NEXT)}["resting_hr"]
    assert s_d["state"] != "in_progress" and s_d["grade"] is not None
    assert (s_n["state"], s_n["why"], s_n["grade"]) == ("in_progress", IN_PROGRESS_WHY, None)
    assert policy.running_total("resting_hr") is True                    # the metric's answer (the web's list)
    assert policy.running_total("resting_hr", "whoop") is False
    assert policy.running_total("resting_hr", "apple_watch_ultra") is True
    assert policy.running_total("resting_hr", "whoop:healthkit") is True


def test_rederive_all_writes_resting_hr_for_stored_recoveries():
    """A store whose recoveries were derived before B5 holds no resting HR
    sample; the rebuild's rederive writes it from the stored payload."""
    policy = _d11_policy()
    conn = _store(policy)
    _apply(conn, policy, "recovery", _recovery_rec(733, "2025-03-04T02:30:00.000Z", rhr=57))
    conn.execute("DELETE FROM samples WHERE sample_id = 'wh:resting_hr:recovery:733'")      # as the old derive left it
    out = whoop.rederive_all(conn, policy)
    assert (out["rewritten"], out["samples_written_by_metric"]) == (1, {"resting_hr": 1})
    assert db.fetchall(conn, "SELECT value FROM samples WHERE sample_id = 'wh:resting_hr:recovery:733'") == [(57.0,)]


# ---- B15: the absolute sleep threshold needs no baseline and annotates a fallback night ----

def _sleep_signal(conn, policy: MetricPolicy, value: float, device: str, base=None, day: date = D) -> dict:
    """The presented sleep_duration signal of `day` for a stored night value (and a 30-day baseline)."""
    from heliosd.signals.markers import compute_signals, signals_for
    conn.execute("INSERT OR REPLACE INTO daily_values (date, metric, value, unit, device_key, n_samples, confidence, grade) "
                 "VALUES (?, 'sleep_duration', ?, 'h', ?, 1, 0.9, 'A')", [day, value, device])
    if base:
        conn.execute("INSERT OR REPLACE INTO baselines (date, metric, window_days, median, mad, n_days) "
                     "VALUES (?, 'sleep_duration', 30, ?, ?, 20)", [day, *base])
    compute_signals(conn, policy, day, today=AS_OF)
    return {r["metric"]: r for r in signals_for(conn, day, policy, AS_OF)}["sleep_duration"]


def test_below_hours_flags_without_baseline():
    """An owner night under the absolute threshold (7 h) is a flag before any
    baseline exists, with no delta (old: insufficient, never flagged)."""
    policy = MetricPolicy(default_tz="Asia/Dubai")
    assert policy.get("sleep_duration")["flag_rule"] == "below_hours 7"
    conn = _store(policy)
    s = _sleep_signal(conn, policy, 6.2, "whoop")
    assert (s["state"], s["why"], s["delta_pct"]) == ("flag", "under 7h", None)
    # Guards (pass on the old code too): with a baseline the flag carries its
    # delta; at or over the threshold without a baseline it is insufficient.
    s = _sleep_signal(conn, policy, 6.5, "whoop", base=(7.2, 0.3), day=NEXT)
    assert (s["state"], s["why"], s["delta_pct"]) == ("flag", "under 7h", -9.7)
    assert _sleep_signal(conn, policy, 7.4, "whoop", day=NEXT + timedelta(days=1))["state"] == "insufficient"


def test_below_hours_annotates_a_fallback_night():
    """A stand-in device's night under 7 h stays a fallback, never judged
    against the owner's baseline, and its why says it is under 7 h (old: the
    fallback said nothing about the threshold)."""
    policy = MetricPolicy(default_tz="Asia/Dubai")
    conn = _store(policy)
    s = _sleep_signal(conn, policy, 5.9, "apple_watch_ultra", base=(7.2, 0.3))
    assert (s["state"], s["delta_pct"], s["fallback"]) == ("fallback", None, True)
    assert s["why"] == "from apple_watch_ultra standing in for whoop, not compared to your baseline; under 7h"
    assert _sleep_signal(conn, policy, 7.5, "apple_watch_ultra", day=NEXT)["why"] == (
        "from apple_watch_ultra standing in for whoop, not compared to your baseline")
