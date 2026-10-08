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


# ------------------------------------------------------- A19 (S7, S9, S10) --

def _whoop_sleep_payload(start: datetime, end: datetime, efficiency: float, rid: str) -> str:
    import json
    return json.dumps({
        "id": rid, "nap": False, "score_state": "SCORED",
        "start": start.isoformat() + "Z", "end": end.isoformat() + "Z",
        "score": {"sleep_efficiency_percentage": efficiency,
                  "stage_summary": {"total_slow_wave_sleep_time_milli": 3_600_000,
                                    "total_rem_sleep_time_milli": 5_400_000,
                                    "total_light_sleep_time_milli": 12_600_000,
                                    "total_awake_time_milli": 1_200_000,
                                    "total_in_bed_time_milli": 22_800_000}}})


def test_efficiency_average_reports_the_real_night_count():
    conn, policy, _ = _env()
    today = date(2026, 7, 20)
    effs = [90.0, 92.0, 88.0, 94.0, 86.0]
    for i in range(7):
        d = today - timedelta(days=6 - i)
        _daily(conn, d, "sleep_duration", 6.5)
    for i, eff in enumerate(effs):
        d = today - timedelta(days=4 - i)
        # Whoop's record is in UTC; 20:30 UTC is 00:30 Dubai on d
        s = datetime.combine(d - timedelta(days=1), datetime.min.time()) + timedelta(hours=20, minutes=30)
        db.execute(conn, "INSERT INTO whoop_cache (date, kind, payload) VALUES (?, 'sleep', ?)",
                   [d, _whoop_sleep_payload(s, s + timedelta(hours=6, minutes=20), eff, f"r{i}")])
    rep = build_sleep_report(conn, days=31, policy=policy, today=today)
    sm = rep["summary"]
    assert sm["efficiency_avg_7d"] == 90.0
    assert sm["efficiency_n_7d"] == 5          # five of the seven nights carry an efficiency
    assert sm["n_7d"] == 7 and sm["window_nights"] == 7
    assert sm["median_n"] == 7


def test_same_day_last_week_is_keyed_on_the_last_night_not_on_today():
    conn, policy, _ = _env()
    today = date(2026, 7, 20)
    # Nights up to yesterday only (today's night is not in yet). The last night
    # is July 19, so "same day last week" is July 12, not July 13 (today - 7).
    for i in range(1, 10):
        d = today - timedelta(days=i)
        _daily(conn, d, "sleep_duration", {7: 7.7, 8: 6.2}.get(i, 7.0))
    sm = build_sleep_report(conn, days=31, policy=policy, today=today)["summary"]
    assert sm["last_night"]["date"] == "2026-07-19"
    assert sm["same_weekday_last_week"] == 6.2
    assert sm["same_weekday_last_week_date"] == "2026-07-12"


def test_sleep_window_is_labelled_in_bed_for_whoop_and_asleep_for_stage_rows():
    from heliosd.signals.sleep_stages import nightly_stages
    conn, policy, _ = _env()
    n1, n2 = date(2026, 7, 18), date(2026, 7, 19)
    _daily(conn, n1, "sleep_duration", 6.0, device="whoop")
    _daily(conn, n2, "sleep_duration", 6.0, device="apple_watch_ultra")
    s = datetime(2026, 7, 17, 20, 30)   # 00:30 Dubai on n1
    db.execute(conn, "INSERT INTO whoop_cache (date, kind, payload) VALUES (?, 'sleep', ?)",
               [n1, _whoop_sleep_payload(s, s + timedelta(hours=6, minutes=20), 91.0, "w1")])
    # Apple on n2: in bed 23:50 to 06:40, first asleep stage 00:10, last ends 06:30 (Dubai wall).
    def row(sid, stage, start, end):
        minutes = (end - start).total_seconds() / 60
        db.execute(conn, """INSERT INTO samples (sample_id, metric, value, text_value, unit, start_ts, end_ts,
                            source_name, device_key, sync_path) VALUES (?, 'sleep_analysis', ?, ?, 'min', ?, ?,
                            'Watch', 'apple_watch_ultra', 'bridge')""", [sid, minutes, stage, start, end])
    row("ib", "in_bed", datetime(2026, 7, 18, 23, 50), datetime(2026, 7, 19, 6, 40))
    row("c1", "core", datetime(2026, 7, 19, 0, 10), datetime(2026, 7, 19, 3, 0))
    row("d1", "deep", datetime(2026, 7, 19, 3, 0), datetime(2026, 7, 19, 4, 0))
    row("r1", "rem", datetime(2026, 7, 19, 4, 0), datetime(2026, 7, 19, 6, 30))
    st = nightly_stages(conn, policy, n1, n2)
    assert st[n1]["window"] == "in_bed"
    assert st[n1]["in_bed_start"] == datetime(2026, 7, 18, 0, 30) and st[n1]["in_bed_end"] == datetime(2026, 7, 18, 6, 50)
    assert st[n1]["fell_asleep"] == st[n1]["in_bed_start"]       # Whoop's record gives no stage times
    assert st[n2]["window"] == "asleep"
    assert st[n2]["fell_asleep"] == datetime(2026, 7, 19, 0, 10) and st[n2]["woke"] == datetime(2026, 7, 19, 6, 30)
    assert st[n2]["in_bed_start"] == datetime(2026, 7, 18, 23, 50) and st[n2]["in_bed_end"] == datetime(2026, 7, 19, 6, 40)
    rep = build_sleep_report(conn, days=31, policy=policy, today=n2 + timedelta(days=1))
    by = {n["date"]: n for n in rep["nights"]}
    assert by["2026-07-18"]["window"] == "in_bed" and by["2026-07-18"]["in_bed_start"] == "00:30"
    assert by["2026-07-19"]["window"] == "asleep" and by["2026-07-19"]["in_bed_end"] == "06:40"


# ------------------------------------------------------------- A15 (T10) --

def _plant_pair(conn, a, b, days, end, device="whoop", seed=3):
    import random
    rng = random.Random(seed)
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        latent = rng.gauss(0, 1)
        _daily(conn, d, a, round(50 + 10 * latent + rng.gauss(0, 0.5), 2), device=device, unit="x")
        _daily(conn, d, b, round(60 + 8 * latent + rng.gauss(0, 0.5), 2), device=device, unit="y")


def test_insights_window_ends_on_the_last_complete_day_and_holds_exactly_n_dates():
    import pytest
    pytest.importorskip("scipy")
    from heliosd.insights import correlations
    conn, policy, _ = _env()
    today = date(2026, 7, 20)
    # 120 complete days plus a partial today with an absurd pair of values.
    _plant_pair(conn, "steps", "heart_rate", 120, today - timedelta(days=1))
    _daily(conn, today, "steps", 1.0, unit="x")
    _daily(conn, today, "heart_rate", 999.0, unit="y")
    rep = correlations.insights_report(conn, days=90, policy=policy, today=today)
    assert rep["error"] is None
    assert rep["window"] == {"start": "2026-04-21", "end": "2026-07-19", "days": 90}
    card = next(i for i in rep["insights"] if set(i["metrics"]) == {"steps", "heart_rate"})
    assert card["n"] == 90                      # was 91 (today-90 .. today) before


def test_definitional_pairs_never_surface_as_insights():
    import pytest
    pytest.importorskip("scipy")
    from heliosd.insights import correlations
    conn, policy, _ = _env()
    today = date(2026, 7, 20)
    _plant_pair(conn, "hrv_rmssd", "recovery_score", 60, today - timedelta(days=1), seed=1)
    _plant_pair(conn, "active_energy", "basal_energy", 60, today - timedelta(days=1), seed=2)
    _plant_pair(conn, "sleep_need", "sleep_duration", 60, today - timedelta(days=1), seed=4)
    out = correlations.top_insights(conn, days=90, policy=policy, today=today)
    pairs = [frozenset(i["metrics"]) for i in out if i["method"].startswith("Spearman, ")]
    for pair in (("hrv_rmssd", "recovery_score"), ("active_energy", "basal_energy"),
                 ("sleep_need", "sleep_duration")):
        assert frozenset(pair) not in pairs, pair
    assert frozenset(("hrv_rmssd", "recovery_score")) in correlations.DERIVED_PAIRS


def test_insights_report_carries_the_error_instead_of_an_empty_list():
    from heliosd.insights import correlations
    conn, policy, _ = _env()
    db.execute(conn, "DROP VIEW IF EXISTS eligible_samples")
    db.execute(conn, "DROP TABLE daily_values")
    rep = correlations.insights_report(conn, days=90, policy=policy, today=date(2026, 7, 20))
    assert rep["insights"] == [] and rep["error"] and "daily_values" in rep["error"]
    # the list form stays quiet for callers that only want cards
    assert correlations.top_insights(conn, days=90, policy=policy, today=date(2026, 7, 20)) == []


# -------------------------------------------------------------- A14 (T9) --

def _apple_night(conn, night: date, prefix: str):
    """Core, deep and REM rows for one Apple Watch night ending at 06:30 on `night`."""
    def row(sid, stage, start, end):
        minutes = (end - start).total_seconds() / 60
        db.execute(conn, """INSERT INTO samples (sample_id, metric, value, text_value, unit, start_ts, end_ts,
                            source_name, device_key, sync_path) VALUES (?, 'sleep_analysis', ?, ?, 'min', ?, ?,
                            'Watch', 'apple_watch_ultra', 'bridge')""", [sid, minutes, stage, start, end])
    m = datetime.combine(night, datetime.min.time())
    row(f"{prefix}-c", "core", m + timedelta(hours=0, minutes=10), m + timedelta(hours=3))
    row(f"{prefix}-d", "deep", m + timedelta(hours=3), m + timedelta(hours=4))
    row(f"{prefix}-r", "rem", m + timedelta(hours=4), m + timedelta(hours=6, minutes=30))


def test_weekly_review_covers_seven_complete_days_and_states_the_value_count():
    from heliosd.insights.weekly_review import build_weekly_review
    conn, policy, _ = _env()
    today = date(2026, 7, 20)
    for i in range(0, 11):                      # recovery for ten past days and the partial today
        d = today - timedelta(days=i)
        if i != 3:                              # one missing day inside the week
            _daily(conn, d, "recovery_score", 60.0 + i, unit="%")
        _daily(conn, d, "strain", 5.0 + (i % 3), unit="")
    rev = build_weekly_review(conn, policy, today=today)
    data, md = rev["data"], rev["markdown"]
    assert (data["start"], data["end"], data["anchor"]) == ("2026-07-13", "2026-07-19", "2026-07-19")
    assert data["window_days"] == 7 and data["recovery"]["n"] == 6 and data["strain_n"] == 7
    assert "7-day average recovery score (6 days with data)" in md
    assert "Seven day average" not in md
    assert "2026-07-20" not in md               # the partial today is nowhere in the review


def test_weekly_review_labels_light_sleep_by_device_instead_of_one_core_row():
    from heliosd.insights.weekly_review import build_weekly_review
    conn, policy, _ = _env()
    today = date(2026, 7, 20)
    for i in (1, 2):                            # two Whoop nights
        d = today - timedelta(days=i)
        _daily(conn, d, "sleep_duration", 6.5, device="whoop")
        s = datetime.combine(d - timedelta(days=1), datetime.min.time()) + timedelta(hours=20, minutes=30)
        db.execute(conn, "INSERT INTO whoop_cache (date, kind, payload) VALUES (?, 'sleep', ?)",
                   [d, _whoop_sleep_payload(s, s + timedelta(hours=6, minutes=20), 90.0, f"w{i}")])
    for i in (3, 4):                            # two Apple Watch nights
        d = today - timedelta(days=i)
        _daily(conn, d, "sleep_duration", 6.3, device="apple_watch_ultra")
        _apple_night(conn, d, f"a{i}")
    rev = build_weekly_review(conn, policy, today=today)
    sleep, md = rev["data"]["sleep"], rev["markdown"]
    by = {b["device"]: b for b in sleep["by_device"]}
    assert set(by) == {"whoop", "apple_watch_ultra"}
    assert by["whoop"]["nights"] == 2 and by["whoop"]["light_label"] == "Light"
    assert by["apple_watch_ultra"]["nights"] == 2 and by["apple_watch_ultra"]["light_label"] == "Core"
    assert "Light (Whoop)" in md and "Core (Apple Watch Ultra)" in md
    assert "| Core |" not in md and "| Light |" not in md
    assert sleep["nights"] == 4


def test_weekly_review_states_whoop_bands_instead_of_the_strain_recovery_ratio():
    from heliosd.insights.weekly_review import build_weekly_review
    conn, policy, _ = _env()
    today = date(2026, 7, 20)
    for i in range(1, 8):
        d = today - timedelta(days=i)
        _daily(conn, d, "recovery_score", 64.0, unit="%")
        _daily(conn, d, "strain", 5.2, unit="")
    rev = build_weekly_review(conn, policy, today=today)
    data, md = rev["data"], rev["markdown"]
    assert data["strain_band"] == "light" and data["recovery_band"] == "yellow"
    assert "out of step" not in md and "look matched" not in md
    assert "Whoop's published bands" in md


def test_weekly_experiment_skips_derived_pairs_and_trends():
    from heliosd.insights import weekly_review
    derived = {"title": "HRV (rMSSD) and recovery score move together", "method": "Spearman, BH-FDR",
               "metrics": ["hrv_rmssd", "recovery_score"]}
    trend = {"title": "Resting heart rate has been drifting up for four weeks",
             "method": "Spearman vs time + Theil-Sen, BH-FDR", "metrics": ["resting_hr"]}
    real = {"title": "Steps and sleep duration move together", "method": "Spearman, BH-FDR",
            "metrics": ["sleep_duration", "steps"]}
    recovery, sleep = {"trend": "steady"}, {"deep_min": 80.0}
    assert "Steps and sleep duration" in weekly_review._experiment(recovery, sleep, [derived, trend, real])
    assert "Test the pattern" not in weekly_review._experiment(recovery, sleep, [derived, trend])


# ------------------------------------------------------------- A16 (T11) --

def test_doctor_report_ends_on_the_last_complete_day_and_dates_every_latest_value():
    from heliosd.insights.doctor_report import build_doctor_report_html
    conn, policy, _ = _env()
    today = date(2026, 7, 20)
    for i in range(1, 41):
        _daily(conn, today - timedelta(days=i), "steps", 4000 + i, device="apple_watch_ultra", unit="count")
    _daily(conn, today, "steps", 114, device="apple_watch_ultra", unit="count")      # the partial today
    html = build_doctor_report_html(conn, "Alex Example", policy, today=today)
    assert "2026-06-20 to 2026-07-19" in html and "30 day window" in html
    assert "2026-07-20" not in html and ">114" not in html                            # today is nowhere
    assert "<td class='num'>4,001</td><td class='dev'>2026-07-19</td>" in html        # integer count, dated
    assert "<td class='num'>30</td>" in html                                           # n behind the median
    assert "Average daily steps (Apple Watch Ultra, 30 complete days): 4,016" in html


def test_doctor_report_rows_never_mix_devices():
    from heliosd.insights.doctor_report import build_doctor_report_html
    conn, policy, _ = _env()
    today = date(2026, 7, 20)
    for i in range(2, 30):                                                             # 28 Apple days
        _daily(conn, today - timedelta(days=i), "resting_hr", 60.0, device="apple_watch_ultra", unit="bpm")
    _daily(conn, today - timedelta(days=1), "resting_hr", 99.0, device="whoop", unit="bpm")   # a fallback day
    for i in range(1, 30):
        dev = "iphone" if i in (3, 9) else "apple_watch_ultra"
        _daily(conn, today - timedelta(days=i), "steps", 10000 if dev == "iphone" else 4000, device=dev, unit="count")
    html = build_doctor_report_html(conn, "Alex Example", policy, today=today)
    assert "99" not in html                                                            # the Whoop day is not the latest
    assert "<td>Resting heart rate</td><td class='num'>60.0</td><td class='dev'>2026-07-18</td>" in html
    assert "<td class='num'>28</td><td>bpm</td><td class='dev'>Apple Watch Ultra</td>" in html
    assert "Average daily steps (Apple Watch Ultra, 27 complete days): 4,000" in html


# ------------------------------------------------------------- A20 (M17) --

def test_body_temp_is_labelled_ring_skin_temperature_everywhere_the_server_labels_metrics():
    import pytest
    from heliosd.insights.correlations import _label
    from heliosd.trust.policy import PolicyError
    from heliosd.trust.schema import validate_policy
    policy = MetricPolicy(default_tz="Asia/Dubai")
    assert policy.label("body_temp") == "Skin temperature (ring)"
    assert policy.effective("body_temp")["label"] == "Skin temperature (ring)"
    assert policy.label("resting_hr") == "resting hr"           # no label set: the key, readable
    assert _label("body_temp") == "skin temperature (ring)"
    assert "body temp" not in _label("body_temp")
    validate_policy({"metrics": {"spo2": {"label": "Blood oxygen"}}}, strict=False)
    with pytest.raises(PolicyError):
        validate_policy({"metrics": {"spo2": {"label": 7}}}, strict=False)
