"""Wave 2 integration: end-to-end checks across the four groups on the REAL
main-sleep episode builder (signals/episodes.py), never a stand-in.

Each group's test file checks its own rule, and group B's point tests had to
monkeypatch episodes.point_wake_dates because the builder did not exist yet.
These tests run the integrated path from stored rows to daily values,
baselines and signals:
(a) sleep_end points (Apple respiratory rate) file on the wake date of their
    own device's main episode (_rows_sleep_end, _wake_runs, point_wake_dates),
    and sample_context sleep_only drops a point no main episode holds;
(b) one night that exercises every part of the design's B1 rule: Whoop's API
    night is the value, Apple's whole night (the union of its stages across
    midnight, a near-duplicate row once) corroborates it, Whoop's HealthKit
    copy never does, grade A, and no schedule-shift flag;
(c) resting HR under D11: Whoop's recovery record is the value on its wake
    date; an Apple day without a Whoop record is a labelled fallback, never
    compared to the Whoop baseline.

Synthetic rows only: the dates, ids and values are invented, and every
expected value is worked out here by hand from the rows written. Wall times
are in the reporting zone (Asia/Dubai), as samples.start_ts is. The policy is
the shipped default with the test fixture overlay, whose lists are the owner's
D11 lists with synthetic device keys."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from heliosd.config import load_metric_policy
from heliosd.ingest import whoop
from heliosd.signals import baselines as bl
from heliosd.signals import context, episodes
from heliosd.signals.baselines import compute_baselines, compute_daily_values, get_baseline
from heliosd.signals.markers import compute_signals, signals_for
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

AWU, ZEPP, WHOOP, HK = "apple_watch_ultra", "zepp_helio", "whoop", "whoop:healthkit"
ING = datetime(2025, 2, 1, 9, 0)        # ingestion instant of a synthetic Bridge row
_seq = [0]


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _policy(**patch) -> MetricPolicy:
    """The shipped default merged with the fixture overlay, metrics patched."""
    cfg = load_metric_policy()
    for metric, keys in patch.items():
        cfg["metrics"][metric] = {**cfg["metrics"].get(metric, {}), **keys}
    return MetricPolicy(cfg, default_tz="Asia/Dubai")


def _env(policy: MetricPolicy | None = None):
    conn = db.connect_memory()
    policy = policy or MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    return conn, policy, SourceRegistry()


def _sid() -> str:
    _seq[0] += 1
    return f"hk:int-{_seq[0]}"


def _stages(conn, device: str, rows, ingested: datetime = ING) -> None:
    """Bridge sleep_analysis rows of one device: (stage, start, end) wall times."""
    db.insert_batch(conn, "INSERT INTO samples (sample_id, metric, value, text_value, unit, start_ts, end_ts, "
                          "source_name, device_key, sync_path, ingested_at) "
                          "VALUES (?, 'sleep_analysis', ?, ?, 'min', ?, ?, ?, ?, 'bridge', ?)",
                    [[_sid(), (_t(e) - _t(s)).total_seconds() / 60.0, stage, _t(s), _t(e), f"Synthetic {device}",
                      device, ingested] for stage, s, e in rows])


def _readings(conn, metric: str, device: str, rows) -> None:
    """Bridge quantity rows of one device: (start, end, value); a point has end = start."""
    db.insert_batch(conn, "INSERT INTO samples (sample_id, metric, value, start_ts, end_ts, source_name, device_key, "
                          "sync_path, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'bridge', ?)",
                    [[_sid(), metric, v, _t(s), _t(e), f"Synthetic {device}", device, ING] for s, e, v in rows])


def _apply(conn, policy: MetricPolicy, kind: str, *recs) -> None:
    """Whoop API records stored the way the puller stores them (whoop_live)."""
    with db.transaction(conn) as c:
        for rec in recs:
            whoop.apply_record(c, kind, rec, policy, datetime(2025, 2, 1, 9, 0), "pull-int")


def _daily(conn, metric: str) -> dict:
    """{date: (value, device_key, n_samples, corroboration dict or None, grade)} of one metric."""
    return {d: (v, dk, n, json.loads(co) if co else None, g) for d, v, dk, n, co, g in db.fetchall(
        conn, "SELECT date, value, device_key, n_samples, corroboration, grade FROM daily_values WHERE metric = ?",
        [metric])}


# ---- (a) sleep_end points follow the real builder's main episodes ----

DA = date(2024, 11, 6)          # the wake date of (a)'s night


def test_sleep_end_points_file_on_the_real_episodes_wake_date():
    """Apple respiratory rate readings under the shipped policy (day basis
    sleep_end, sample_context sleep_only) file on the wake date of the Ultra's
    own main episode (22:30 to 06:15): 23:00 on the bed date and two readings
    at the same instant 03:00 all file on the wake date, (14 + 15 + 16) / 3 =
    15.0 over 3 readings, and the bed date gets nothing. A reading inside a
    1.5 h nap (an episode, never a main one) and a 15:00 reading outside every
    episode are dropped. Each date recomputed alone gives the range's rows."""
    conn, policy, reg = _env()
    assert (policy.day_basis("respiratory_rate"), policy.sample_context("respiratory_rate")) == ("sleep_end", "sleep_only")
    _stages(conn, AWU, [("core", "2024-11-05 22:30", "2024-11-06 01:00"), ("deep", "2024-11-06 01:00", "2024-11-06 02:00"),
                        ("core", "2024-11-06 02:00", "2024-11-06 06:15"),
                        ("core", "2024-11-06 12:00", "2024-11-06 13:30")])          # the nap
    at = ["2024-11-05 23:00", "2024-11-06 03:00", "2024-11-06 03:00", "2024-11-06 13:00", "2024-11-06 15:00"]
    _readings(conn, "respiratory_rate", AWU, [(a, a, v) for a, v in zip(at, [14.0, 15.0, 16.0, 20.0, 19.0])])
    instants = [_t(a) for a in at]
    # The builder's answer per instant (sorted, one repeated), and the runs SQL joins on.
    assert episodes.point_wake_dates(conn, policy, AWU, instants) == [DA, DA, DA, None, None]
    assert bl._wake_runs(conn, policy, [(AWU, t) for t in instants]) == [(AWU, instants[0], instants[2], DA)]
    for d in (DA - timedelta(days=1), DA):
        compute_daily_values(conn, policy, reg, d, d, as_of=DA + timedelta(days=5))
    alone = _daily(conn, "respiratory_rate")
    compute_daily_values(conn, policy, reg, DA - timedelta(days=1), DA, as_of=DA + timedelta(days=5))
    assert _daily(conn, "respiratory_rate") == alone
    assert {d: v[:3] for d, v in alone.items()} == {DA: (15.0, AWU, 3)}


def test_a_qualified_key_points_follow_their_own_devices_episode():
    """The day basis hands the builder the arbitration key, and a qualified
    key reads its own device's stage rows. With whoop:healthkit listed for
    respiratory rate (owner question Q1's alternative, one policy line),
    Whoop's HealthKit reading at 03:00 files on the wake date of Whoop's own
    episode (23:30 to 05:00) under whoop:healthkit, and its 23:00 reading,
    outside that episode though inside Apple's (22:30 to 06:15), is dropped;
    Apple's 23:00 and 03:00 readings follow Apple's episode and corroborate
    (14.5)."""
    conn, policy, reg = _env(_policy(respiratory_rate={"priority": [WHOOP, HK, AWU]}))
    assert policy.sync_paths("respiratory_rate") == {WHOOP: ["whoop_live"]}
    _stages(conn, AWU, [("core", "2024-11-05 22:30", "2024-11-06 06:15")])
    _stages(conn, WHOOP, [("asleep", "2024-11-05 23:30", "2024-11-06 05:00")])
    _readings(conn, "respiratory_rate", WHOOP, [("2024-11-05 23:00", "2024-11-05 23:00", 16.5),
                                                ("2024-11-06 03:00", "2024-11-06 03:00", 16.0)])
    _readings(conn, "respiratory_rate", AWU, [("2024-11-05 23:00", "2024-11-05 23:00", 14.0),
                                              ("2024-11-06 03:00", "2024-11-06 03:00", 15.0)])
    two = [_t("2024-11-05 23:00"), _t("2024-11-06 03:00")]
    assert episodes.point_wake_dates(conn, policy, HK, two) == [None, DA]
    assert episodes.point_wake_dates(conn, policy, AWU, two) == [DA, DA]
    compute_daily_values(conn, policy, reg, DA - timedelta(days=1), DA, as_of=DA + timedelta(days=5))
    assert {d: v[:4] for d, v in _daily(conn, "respiratory_rate").items()} == {DA: (16.0, HK, 1, {AWU: 14.5})}


# ---- (b) one night through every part of the B1 rule ----

DB = date(2024, 12, 4)          # the wake date of (b)'s example night
MS_MIN = 60_000                 # one minute in milliseconds


def _whoop_night(id_: str, start_z: str, end_z: str, light_min: float, sws_min: float, rem_min: float,
                 in_bed_min: float) -> dict:
    """A scored Whoop API v2 sleep record (UTC instants, as the API sends them)."""
    asleep = light_min + sws_min + rem_min
    return {"id": id_, "user_id": 1, "cycle_id": None, "created_at": end_z, "updated_at": end_z,
            "start": start_z, "end": end_z, "timezone_offset": "+04:00", "nap": False, "score_state": "SCORED",
            "score": {"stage_summary": {"total_in_bed_time_milli": round(in_bed_min * MS_MIN),
                                        "total_awake_time_milli": round((in_bed_min - asleep) * MS_MIN),
                                        "total_light_sleep_time_milli": round(light_min * MS_MIN),
                                        "total_slow_wave_sleep_time_milli": round(sws_min * MS_MIN),
                                        "total_rem_sleep_time_milli": round(rem_min * MS_MIN)},
                      "sleep_efficiency_percentage": round(100 * asleep / in_bed_min, 1)}}


def test_example_night_whoop_value_apple_union_grade_a_no_shift_flag():
    """One synthetic night through every part of the design's B1 rule, under
    the fixture policy's D11 lists (sleep_duration: whoop, whoop:healthkit, the
    Ultra, the Watch 6, the strap; Whoop's value from whoop_live only):
    - Whoop's API night (in bed 22:40 to 06:20; light 215, SWS 95 and REM
      101.4 min = 411.4 min = 6.857 h asleep) is the value on its wake date,
      6.86 at the store's two decimals, with the record's in-bed window;
    - Apple's rows run from 22:46 on the bed date to 05:58 with a 31 min 24 s
      gap (under 60 min: one episode) and a near-duplicate of one row (edges
      45 s later, ingested a day later). Its night is the union, 149 + 251.6
      = 400.6 min = 6.677 h (6.68 stored), within a minute; the old end-date
      buckets gave 71 min on the bed date and 419.6 min on the wake date;
    - Whoop's HealthKit copy of the night (411.4 min asleep) is built as
      whoop:healthkit and never corroborates its own API night;
    - grade A: 0.35 (rank) + 0.25 (fresh) + 0.2 / 3 (one record) + 0.2 (6.68
      is 2.6 percent from 6.86, inside 12) = 0.867;
    - fourteen earlier nights (Whoop's API night 22:00 to 05:30 and its
      HealthKit stage rows across midnight): every window is the stored
      night's own, so no travel_or_shifted_schedule flag."""
    conn, policy, reg = _env()
    assert policy.priority("sleep_duration") == [WHOOP, HK, AWU, "apple_watch_6_legacy", ZEPP]
    assert policy.sync_paths("sleep_duration") == {WHOOP: ["whoop_live"]}
    for i in range(14, 0, -1):
        wake = DB - timedelta(days=i)
        bed = wake - timedelta(days=1)
        _apply(conn, policy, "sleep", _whoop_night(f"w-{i}", f"{bed}T18:00:00.000Z", f"{wake}T01:30:00.000Z",
                                                  230, 80, 80, 450))
        _stages(conn, WHOOP, [("asleep", f"{bed} 22:15", f"{wake} 01:00"), ("awake", f"{wake} 01:00", f"{wake} 01:10"),
                              ("asleep", f"{wake} 01:10", f"{wake} 05:10")])
    _apply(conn, policy, "sleep", _whoop_night("w-0", "2024-12-03T18:40:00.000Z", "2024-12-04T02:20:00.000Z",
                                              215, 95, 101.4, 460))
    _stages(conn, WHOOP, [("asleep", "2024-12-03 22:50:00", "2024-12-04 01:40:00"),
                          ("awake", "2024-12-04 01:40:00", "2024-12-04 01:55:00"),
                          ("asleep", "2024-12-04 01:55:00", "2024-12-04 05:56:24"),
                          ("in_bed", "2024-12-03 22:40:00", "2024-12-04 06:20:00")])
    _stages(conn, AWU, [("core", "2024-12-03 22:46:00", "2024-12-03 23:40:00"),
                        ("deep", "2024-12-03 23:40:00", "2024-12-03 23:57:00"),
                        ("core", "2024-12-03 23:57:00", "2024-12-04 01:15:00"),
                        ("rem", "2024-12-04 01:46:24", "2024-12-04 02:50:00"),      # after the 31 min 24 s gap
                        ("core", "2024-12-04 02:50:00", "2024-12-04 04:20:00"),
                        ("rem", "2024-12-04 04:20:00", "2024-12-04 05:58:00")])
    _stages(conn, AWU, [("core", "2024-12-04 02:50:45", "2024-12-04 04:20:45")], ingested=ING + timedelta(days=1))
    compute_daily_values(conn, policy, reg, DB - timedelta(days=14), DB, as_of=DB + timedelta(days=1))
    nights = _daily(conn, "sleep_duration")
    value, device, n, corr, grade = nights[DB]
    assert (value, device, n, corr, grade) == (6.86, WHOOP, 1, {AWU: 6.68}, "A")
    assert abs(corr[AWU] - 400.6 / 60) * 60 <= 1                                   # the union, within a minute
    assert json.loads(db.fetchall(conn, "SELECT detail FROM daily_values WHERE metric = 'sleep_duration' AND date = ?",
                                  [DB])[0][0]) == {"start": "2024-12-03T22:40:00", "end": "2024-12-04T06:20:00",
                                                   "window": "in_bed", "basis": "whoop_api"}
    # The builder's nights behind it: Apple's union across midnight with the copy counted once, and
    # Whoop's HealthKit episode, present as a key of its own yet not beside its API night.
    ep = episodes.main_sleep_episodes(conn, policy, DB, DB, devices=[AWU])[(AWU, DB)]
    assert (ep.start, ep.end, ep.n_rows) == (_t("2024-12-03 22:46"), _t("2024-12-04 05:58"), 7)
    assert ep.asleep_h == pytest.approx(400.6 / 60, abs=1e-9) and ep.overlap_removed_min == pytest.approx(90, abs=1e-9)
    assert episodes.main_sleep_episodes(conn, policy, DB, DB, devices=[HK])[(HK, DB)].asleep_h == pytest.approx(
        411.4 / 60, abs=1e-9)
    assert {k: v for _d, k, v, _n, _de in bl._rows_sleep(conn, policy, "sleep_duration", DB, DB)} == {
        WHOOP: 6.86, HK: 6.86, AWU: 6.68}
    # Nothing of the example night is split onto the bed date: that date is its own Whoop night alone.
    assert nights[DB - timedelta(days=1)][:4] == (6.5, WHOOP, 1, None)
    # The windows are the stored nights' own: no schedule shift, no late night.
    assert context._sleep_window(conn, DB) == (_t("2024-12-03 22:40"), _t("2024-12-04 06:20"))
    compute_signals(conn, policy, DB, today=DB + timedelta(days=1))
    s = {r["metric"]: r for r in signals_for(conn, DB, policy, today=DB + timedelta(days=1))}["sleep_duration"]
    assert (s["device_key"], s["grade"], s["fallback"]) == (WHOOP, "A", False)
    assert "travel_or_shifted_schedule" not in s["context_flags"] and "late_night" not in s["context_flags"]


# ---- (c) resting HR under D11: Whoop's record first, an Apple day a labelled fallback ----

DC = date(2025, 1, 15)
WHOOP_RHR = [55.0, 57.0, 54.0, 56.0, 58.0, 55.0, 53.0, 56.0, 57.0, 54.0]      # DC - 9 .. DC


def _recovery(cycle_id: int, created_z: str, rhr: float) -> dict:
    """A scored Whoop API v2 recovery (identity: its cycle_id), created at the wake."""
    return {"cycle_id": cycle_id, "sleep_id": f"s-{cycle_id}", "user_id": 1, "created_at": created_z,
            "updated_at": created_z, "score_state": "SCORED", "timezone_offset": "+04:00",
            "score": {"user_calibrating": False, "recovery_score": 60, "hrv_rmssd_milli": 50.0,
                      "resting_heart_rate": rhr}}


def _apple_rhr(day: date, value: float) -> tuple:
    """Apple's day summary of `day`: 20:00 the evening before to 19:59 (it files on `day`)."""
    return (f"{day - timedelta(days=1)} 20:00", f"{day} 19:59", value)


def test_resting_hr_whoop_record_on_its_wake_date_apple_day_a_labelled_fallback():
    """Resting HR under D11 (whoop, the Ultra, the strap; Whoop's value from
    whoop_live only):
    - ten Whoop recoveries, DC - 9 to DC: each is a sample at its wake
      (created_at; 06:30 in Dubai, one at 03:20 in Dubai, 23:20 UTC the
      evening before) and the value of its wake date under whoop; on DC
      Apple's day summary (61) is shown beside it;
    - two earlier Apple-only days (68, 70) are fallback days, so the owner
      baseline of DC + 1 is the ten Whoop days alone: median of 53, 54, 54,
      55, 55, 56, 56, 57, 57, 58 = 55.5, MAD 1.5, 10 days;
    - DC + 1 has Apple's summary (62) and Whoop's HealthKit copy (58) but no
      Whoop record: the Ultra's 62, labelled a fallback for whoop, no delta,
      though the owner baseline exists; the copy is neither the value nor
      corroboration (owner question Q1's default);
    - DC itself is judged against the nine Whoop days before it (median 56):
      54 is favorable, delta -3.6 percent."""
    conn, policy, reg = _env()
    assert policy.priority("resting_hr") == [WHOOP, AWU, ZEPP]
    assert policy.sync_paths("resting_hr") == {WHOOP: ["whoop_live"]}
    recs = []
    for i, rhr in zip(range(9, -1, -1), WHOOP_RHR):
        day = DC - timedelta(days=i)
        created = f"{day - timedelta(days=1)}T23:20:00.000Z" if i == 5 else f"{day}T02:30:00.000Z"
        recs.append(_recovery(800 + i, created, rhr))
    _apply(conn, policy, "recovery", *recs)
    _readings(conn, "resting_hr", AWU, [_apple_rhr(DC - timedelta(days=12), 68.0), _apple_rhr(DC - timedelta(days=11), 70.0),
                                        _apple_rhr(DC, 61.0), _apple_rhr(DC + timedelta(days=1), 62.0)])
    _readings(conn, "resting_hr", WHOOP, [("2025-01-16 06:35", "2025-01-16 06:35", 58.0)])          # the copy, DC + 1
    assert db.fetchall(conn, "SELECT start_ts, sync_path FROM samples WHERE sample_id = 'wh:resting_hr:recovery:805'") == [
        (_t("2025-01-10 03:20"), "whoop_live")]
    today = DC + timedelta(days=5)
    compute_daily_values(conn, policy, reg, DC - timedelta(days=12), DC + timedelta(days=1), as_of=today)
    rhr = _daily(conn, "resting_hr")
    want = {DC - timedelta(days=12): (68.0, AWU, None), DC - timedelta(days=11): (70.0, AWU, None),
            DC: (54.0, WHOOP, {AWU: 61.0}), DC + timedelta(days=1): (62.0, AWU, None)}
    want.update({DC - timedelta(days=i): (v, WHOOP, None) for i, v in zip(range(9, 0, -1), WHOOP_RHR)})
    assert {d: (v, dk, co) for d, (v, dk, _n, co, _g) in rhr.items()} == want
    for d in (DC, DC + timedelta(days=1)):
        compute_baselines(conn, policy, d)
        compute_signals(conn, policy, d, today=today)
    assert get_baseline(conn, "resting_hr", DC + timedelta(days=1), 30) == {"median": 55.5, "mad": 1.5, "n_days": 10}
    sig = {d: {r["metric"]: r for r in signals_for(conn, d, policy, today=today)}["resting_hr"]
           for d in (DC, DC + timedelta(days=1))}
    fb = sig[DC + timedelta(days=1)]
    assert (fb["value"], fb["device_key"], fb["owner_device"], fb["fallback"], fb["state"], fb["delta_pct"]) == (
        62.0, AWU, WHOOP, True, "fallback", None)
    assert fb["why"] == "from apple_watch_ultra standing in for whoop, not compared to your baseline"
    assert fb["baseline_median"] == 55.5                       # a baseline existed; the fallback is not compared to it
    own = sig[DC]
    assert (own["device_key"], own["fallback"], own["state"], own["delta_pct"], own["baseline_median"]) == (
        WHOOP, False, "favorable", -3.6, 56.0)
