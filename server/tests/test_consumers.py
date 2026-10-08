"""Phase 1a item 7: every analytical consumer reads the eligibility view and
one arbitrated device per night (eligibility is not arbitration, checkpoint A
point 28). Oracles are the minutes written for one device, never sums."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from heliosd.ingest.bridge import ingest_batch
from heliosd.insights.doctor_report import build_doctor_report_html
from heliosd.insights.weekly_review import build_weekly_review
from heliosd.signals import context, recompute as rc
from heliosd.signals.sleep_report import build_sleep_report
from heliosd.signals.sleep_stages import nightly_stages
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry
from tests.synth import store_whoop_direct

AWU = "Owner’s Ultra 1"
N = date(2026, 7, 10)            # the night (reporting date of the wake)
SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
STAGE_HK = {"deep": "HKCategoryValueSleepAnalysisAsleepDeep", "rem": "HKCategoryValueSleepAnalysisAsleepREM",
            "core": "HKCategoryValueSleepAnalysisAsleepCore", "awake": "HKCategoryValueSleepAnalysisAwake"}


def _env():
    conn = db.connect_memory()
    policy = MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    return conn, policy, SourceRegistry()


def _stage_rows(source: str, minutes: dict[str, int], prefix: str, first: datetime = datetime(2026, 7, 9, 20, 30)) -> list[dict]:
    """Consecutive segments starting at `first` (UTC; 00:30 Dubai on N), in the
    order deep, rem, core, awake, each `minutes[stage]` long."""
    rows, t = [], first
    for i, stage in enumerate(("deep", "rem", "core", "awake")):
        if stage not in minutes:
            continue
        e = t + timedelta(minutes=minutes[stage])
        rows.append({"hk_type": SLEEP, "value": STAGE_HK[stage], "unit": "min", "source_name": source,
                     "start": t.isoformat() + "Z", "end": e.isoformat() + "Z", "uuid": f"{prefix}-{i}"})
        t = e
    return rows


WHOOP_MIN = {"deep": 60, "rem": 90, "core": 200, "awake": 20}
APPLE_MIN = {"deep": 45, "rem": 70, "core": 220, "awake": 15}


def _seed(conn, policy, reg, whoop_rows=True, apple_rows=True, owner_direct=True):
    samples = []
    if whoop_rows:
        samples += _stage_rows("WHOOP", WHOOP_MIN, "wh")
    if apple_rows:
        samples += _stage_rows(AWU, APPLE_MIN, "aw")
    ingest_batch(conn, {"batch_id": "stages", "samples": samples}, policy, reg)
    if owner_direct:
        store_whoop_direct(conn, [{"kind": "sleep", "id": "s1", "nap": False, "score_state": "SCORED",
                                   "start": datetime(2026, 7, 10, 0, 30), "end": datetime(2026, 7, 10, 6, 40),
                                   "asleep_hours": 5.83}], policy.zone)
    rc.recompute_dates(conn, policy, reg, {N - timedelta(days=1), N}, today=N + timedelta(days=1))


def test_stage_readers_pick_the_owning_device_and_never_sum_across_devices():
    conn, policy, reg = _env()
    _seed(conn, policy, reg)
    assert db.fetchall(conn, "SELECT device_key FROM daily_values WHERE metric = 'sleep_duration' AND date = ?", [N]) == [("whoop",)]
    st = nightly_stages(conn, policy, N - timedelta(days=7), N)
    # Wave 2 (B2): stages from Whoop's HealthKit copy carry the copy's own key.
    assert set(st) == {N} and st[N]["device"] == "whoop:healthkit" and st[N]["source"] == "healthkit"
    assert (st[N]["deep_min"], st[N]["rem_min"], st[N]["light_min"], st[N]["awake_min"]) == (60, 90, 200, 20)
    assert st[N]["fell_asleep"] == datetime(2026, 7, 10, 0, 30) and st[N]["woke"] == datetime(2026, 7, 10, 6, 20)
    # weekly review: averages over ONE device, not 60 + 45
    review = build_weekly_review(conn, policy)
    sleep = review["data"]["sleep"]
    assert {k: sleep[k] for k in ("nights", "deep_min", "rem_min", "core_min")} == \
        {"nights": 1, "deep_min": 60.0, "rem_min": 90.0, "core_min": 200.0}
    # Wave 1 (A14): the same minutes once more per device, labelled with the device's own stage name
    assert sleep["by_device"] == [{"device": "whoop:healthkit", "device_name": "Whoop (Apple Health copy)", "nights": 1,
                                   "deep_min": 60.0, "rem_min": 90.0, "light_min": 200.0, "light_label": "Core"}]
    # doctor report: same numbers
    html = build_doctor_report_html(conn, "Owner", policy)
    assert "deep 60 min" in html and "REM 90 min" in html and "core 200 min" in html
    # sleep report: stage source is the owner, clock times in the reporting zone
    rep = build_sleep_report(conn, days=31, policy=policy, today=N + timedelta(days=1))
    night = rep["nights"][-1]
    assert night["device"] == "whoop" and night["stage_source"] == "whoop:healthkit"
    assert night["stages"] == {"deep_min": 60, "rem_min": 90, "light_min": 200, "awake_min": 20}
    assert night["fell_asleep"] == "00:30" and night["woke"] == "06:20"


def test_owner_without_stage_rows_falls_back_to_the_priority_list():
    conn, policy, reg = _env()
    _seed(conn, policy, reg, whoop_rows=False)       # Whoop owns the night (direct record) but wrote no stages
    st = nightly_stages(conn, policy, N, N)
    assert st[N]["device"] == "apple_watch_ultra" and st[N]["deep_min"] == 45
    rep = build_sleep_report(conn, days=31, policy=policy, today=N + timedelta(days=1))
    assert rep["nights"][-1]["device"] == "whoop" and rep["nights"][-1]["stage_source"] == "apple_watch_ultra"


def test_whoop_api_record_beats_its_healthkit_copy_for_the_whoop_owner():
    conn, policy, reg = _env()
    _seed(conn, policy, reg)
    payload = {"start": "2026-07-09T20:35:00.000Z", "end": "2026-07-10T02:40:00.000Z",
               "score": {"sleep_efficiency_percentage": 91.5, "stage_summary": {
                   "total_in_bed_time_milli": 6.1 * 3.6e6, "total_awake_time_milli": 18 * 60000,
                   "total_light_sleep_time_milli": 205 * 60000, "total_slow_wave_sleep_time_milli": 55 * 60000,
                   "total_rem_sleep_time_milli": 88 * 60000}}}
    db.execute(conn, "INSERT INTO whoop_cache (date, kind, payload) VALUES (?, 'sleep', ?)", [N, json.dumps(payload)])
    st = nightly_stages(conn, policy, N, N)[N]
    assert st["source"] == "whoop_api" and (st["deep_min"], st["rem_min"], st["light_min"], st["awake_min"]) == (55, 88, 205, 18)
    assert st["efficiency_pct"] == 91.5 and st["in_bed_h"] == 6.1
    assert st["fell_asleep"] == datetime(2026, 7, 10, 0, 35)          # Dubai wall, not the Mac clock
    rep = build_sleep_report(conn, days=31, policy=policy, today=N + timedelta(days=1))
    assert rep["nights"][-1]["fell_asleep"] == "00:35" and rep["nights"][-1]["efficiency_pct"] == 91.5
    assert rep["summary"]["efficiency_avg_7d"] == 91.5                 # N is inside the trailing week of N + 1
    far = build_sleep_report(conn, days=31, policy=policy, today=N + timedelta(days=8))
    assert far["summary"]["efficiency_avg_7d"] is None and far["summary"]["avg_prev_7d"] == 5.83


def test_ineligible_rows_never_reach_a_consumer():
    conn, policy, reg = _env()
    _seed(conn, policy, reg)
    before = nightly_stages(conn, policy, N, N)[N]
    window_before = context._sleep_window(conn, N)
    # excluded device, unknown category, bad time: huge minutes that must not count
    db.execute(conn, "INSERT INTO samples (sample_id, metric, value, text_value, unit, start_ts, end_ts, source_name, device_key, sync_path) "
                     "VALUES ('x1', 'sleep_analysis', 500, 'deep', 'min', '2026-07-09 10:00', '2026-07-10 06:00', 'Athlytic', 'excluded', 'bridge')")
    db.execute(conn, "INSERT INTO samples (sample_id, metric, value, text_value, unit, start_ts, end_ts, source_name, device_key, sync_path, quality) "
                     "VALUES ('x2', 'sleep_analysis', 500, 'HKCategoryValueSleepAnalysisBrandNew', 'min', '2026-07-10 01:00', '2026-07-10 06:00', 'WHOOP', 'whoop', 'bridge', 'unknown_category')")
    db.execute(conn, "INSERT INTO samples (sample_id, metric, value, text_value, unit, start_ts, end_ts, source_name, device_key, sync_path, quality) "
                     "VALUES ('x3', 'sleep_analysis', 500, 'deep', 'min', '2026-07-10 05:00', '2026-07-10 04:00', 'WHOOP', 'whoop', 'bridge', 'bad_time')")
    assert nightly_stages(conn, policy, N, N)[N] == before
    # Wave 2 (B1): the window is the stored night's own window (here the Whoop
    # API record's in-bed edges), no longer every device's rows ending on N.
    assert context._sleep_window(conn, N) == window_before == (datetime(2026, 7, 10, 0, 30), datetime(2026, 7, 10, 6, 40))
    assert build_weekly_review(conn, policy)["data"]["sleep"]["deep_min"] == 60.0
