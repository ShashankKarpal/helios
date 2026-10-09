"""Wave 3 (design note docs/briefs/build-2026-10-09/wave3/design.md, section 5):
C4, the full rebuild leaves no journal behind for the dates it covered, and
C5, the diff tool (server/tools/wave3_diff.py) for the Whoop back-pull, on
tiny synthetic stores. Every expected value is worked out by hand.
Synthetic data only."""

from __future__ import annotations

from datetime import datetime, timedelta

from heliosd.signals.rebuild import rebuild_all
from heliosd.store import db
from heliosd.trust.registry import SourceRegistry
from tests.test_wave2_export import _policy
from tests.test_wave2_tools import FIRST, TODAY, _tiny_store


# ---------------------------------------------------------------- C4: the rebuild clears the journal it covers

def test_rebuild_all_clears_the_journal_it_covers(tmp_path):
    path = tmp_path / "s.duckdb"
    _tiny_store(path)
    conn = db.connect(path)
    policy = _policy()
    policy.sync_registry(conn)
    at = datetime(2026, 5, 1, 9, 0)
    inside = [(FIRST, "whoop"), (FIRST + timedelta(days=4), "whoop"), (FIRST + timedelta(days=4), "ingest"), (TODAY, "startup")]
    outside = [(FIRST - timedelta(days=3), "ingest"), (TODAY + timedelta(days=2), "manual")]
    db.insert_batch(conn, "INSERT INTO dirty_dates (date, reason, batch_id, enqueued_at) VALUES (?, ?, ?, ?)",
                    [[d, r, "b-1", at] for d, r in inside + outside])
    db.execute(conn, "INSERT INTO dirty_dates (date, reason, batch_id, enqueued_at) VALUES (?, 'delete', 'b-2', NULL)",
               [FIRST + timedelta(days=6)])                                # an unstamped row inside the range goes too
    out = rebuild_all(conn, policy, TODAY, registry=SourceRegistry())
    assert out["range"] == [FIRST, TODAY]
    left = sorted((d, r) for d, r in db.fetchall(conn, "SELECT date, reason FROM dirty_dates"))
    assert left == sorted(outside)                                        # only the dates the rebuild did not cover
    assert out["journal_cleared"] == len(inside) + 1
    conn.close()


def test_rebuild_all_keeps_a_journal_row_written_while_it_ran(tmp_path, monkeypatch):
    """A row replaced during the rebuild (a newer enqueued_at) stays, as in the drain."""
    from heliosd.signals import rebuild as rb
    path = tmp_path / "s.duckdb"
    _tiny_store(path)
    conn = db.connect(path)
    policy = _policy()
    policy.sync_registry(conn)
    day = FIRST + timedelta(days=2)
    db.execute(conn, "INSERT INTO dirty_dates (date, reason, batch_id, enqueued_at) VALUES (?, 'ingest', 'b-1', ?)",
               [day, datetime(2026, 5, 1, 9, 0)])
    real = rb.bl.compute_daily_values

    def ingest_meanwhile(*a, **k):                                         # an ingest journals the day again mid-rebuild
        db.execute(conn, "INSERT OR REPLACE INTO dirty_dates (date, reason, batch_id, enqueued_at) VALUES (?, 'ingest', 'b-2', ?)",
                   [day, datetime(2026, 5, 1, 9, 5)])
        return real(*a, **k)
    monkeypatch.setattr(rb.bl, "compute_daily_values", ingest_meanwhile)
    out = rebuild_all(conn, policy, TODAY, registry=SourceRegistry())
    assert db.fetchall(conn, "SELECT date, reason, batch_id FROM dirty_dates") == [(day, "ingest", "b-2")]
    assert out["journal_cleared"] == 0
    conn.close()


# ---------------------------------------------------------------- C5: the Wave 3 diff tool

import json  # noqa: E402
import shutil  # noqa: E402
from datetime import date  # noqa: E402

from tests.test_wave2_tools import DIFF_POLICY, _dv, _sha, _stage, _tool  # noqa: E402

T5 = date(2026, 5, 10)                     # --today: the synthetic nights are May 3, 4 and 8
MS = 3.6e6


def _rec(c, kind, nid, start, end, payload, sleep_id=None, cycle_id=None, created=None, updated=None):
    created = created or start
    db.execute(c, "INSERT INTO whoop_records (record_key, kind, native_id, sleep_id, cycle_id, start_utc, end_utc, score_state, nap, "
                  "created_at, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, 'SCORED', ?, ?, ?, ?)",
               [f"{kind}:{nid}", kind, nid, sleep_id, cycle_id, start if kind != "recovery" else created, end,
                False if kind == "sleep" else None, created, updated or created, json.dumps(payload)])


def _sleep(c, nid, start, end, light_h, sws_h, rem_h):
    _rec(c, "sleep", nid, start, end, {"id": nid, "score": {"stage_summary": {
        "total_light_sleep_time_milli": light_h * MS, "total_slow_wave_sleep_time_milli": sws_h * MS,
        "total_rem_sleep_time_milli": rem_h * MS}}}, sleep_id=nid)


def _night(c, n, sleep_start, sleep_end, stages, recovery, strain, created, strain_updated=None):
    """One Whoop night: the cycle c-n, its recovery and its sleep s-n (UTC instants)."""
    _rec(c, "cycle", f"c-{n}", sleep_start - timedelta(hours=4), sleep_end + timedelta(hours=12), {"id": f"c-{n}", "score": {"strain": strain}},
         cycle_id=f"c-{n}", updated=strain_updated)
    _rec(c, "recovery", f"c-{n}", None, None, {"cycle_id": f"c-{n}", "sleep_id": f"s-{n}", "score": recovery},
         sleep_id=f"s-{n}", cycle_id=f"c-{n}", created=created)
    _sleep(c, f"s-{n}", sleep_start, sleep_end, *stages)


def _backpull_copies(tmp_path, revised=False):
    """old: the record of the night ending May 8 only; new: the back-pull adds the
    nights ending May 3 and 4 and a lone March cycle. Whoop's Apple Health copy of
    all three nights is in both (7.033 h, 8.0 h and 7.833 h asleep)."""
    policy = _policy(**DIFF_POLICY)
    old, new = tmp_path / "old.duckdb", tmp_path / "new.duckdb"
    for path in (old, new):
        c = db.connect(path)
        policy.sync_registry(c)
        is_new = path == new
        _stage(c, "hk:w3", "whoop", (2, "22:00"), (3, "05:02"), "core")
        _stage(c, "hk:w4", "whoop", (3, "22:00"), (4, "06:00"), "core")
        _stage(c, "hk:w8", "whoop", (7, "22:00"), (8, "05:50"), "core")
        _night(c, 8, datetime(2026, 5, 7, 18), datetime(2026, 5, 8, 1, 50), (4, 2, 1 + 2 / 3),
               {"recovery_score": 70, "resting_heart_rate": 55, "hrv_rmssd_milli": 50.0}, 8.4 if is_new and revised else 8.0,
               datetime(2026, 5, 8, 2), strain_updated=datetime(2026, 5, 9, 3) if is_new and revised else None)
        _dv(c, 8, "sleep_duration", 7.67, "whoop", "A")
        _dv(c, 8, "resting_hr", 55.0, "whoop", "A")
        _dv(c, 8, "recovery_score", 70.0, "whoop", "A")
        _dv(c, 8, "strain", 8.4 if is_new and revised else 8.0, "whoop", "A")
        db.execute(c, "INSERT INTO baselines VALUES (?, 'strain', 30, ?, 1.0, 20), (?, 'strain', 60, 7.5, 1.0, 40)",
                   [_d5(9), 7.2 if is_new and revised else 7.0, _d5(9)])
        if is_new:
            _night(c, 3, datetime(2026, 5, 2, 18), datetime(2026, 5, 3, 1), (4, 1.5, 1.5),
                   {"recovery_score": 64, "resting_heart_rate": 58, "hrv_rmssd_milli": 41.0}, 6.5, datetime(2026, 5, 3, 2))
            _night(c, 4, datetime(2026, 5, 3, 18), datetime(2026, 5, 4, 2), (4, 2, 4 / 3),
                   {"recovery_score": 80, "resting_heart_rate": 61, "hrv_rmssd_milli": 45.5}, 9.0, datetime(2026, 5, 4, 3))
            _rec(c, "cycle", "c-1", datetime(2026, 3, 10, 14), datetime(2026, 3, 11, 14), {"id": "c-1", "score": {"strain": 3.0}},
                 cycle_id="c-1")
            _dv(c, 3, "sleep_duration", 7.0, "whoop", "A")
            _dv(c, 4, "sleep_duration", 7.33, "whoop", "A")
            _dv(c, 3, "resting_hr", 58.0, "whoop", "A")
            _dv(c, 4, "resting_hr", 61.0, "whoop", "A")
            _dv(c, 3, "respiratory_rate", 15.2, "whoop", "A")
            _dv(c, 3, "recovery_score", 64.0, "whoop", "A")
            _dv(c, 4, "recovery_score", 80.0, "whoop", "A")
            _dv(c, 4, "hrv_rmssd", 45.5, "whoop", "A")
            _dv(c, 3, "strain", 6.5, "whoop", "A")
            _dv(c, 3, "sleep_need", 7.8, "whoop", "A")
            if revised:
                _dv(c, 9, "hrv_rmssd", 47.0, "whoop", "A")
        else:
            _dv(c, 3, "sleep_duration", 7.03, "whoop:healthkit", "B")
            _dv(c, 4, "sleep_duration", 8.0, "whoop:healthkit", "B")
            _dv(c, 3, "resting_hr", 60.0, "apple_watch_ultra", "B")
            _dv(c, 3, "respiratory_rate", 15.0, "apple_watch_ultra", "B")
        c.close()
    return old, new, policy


def _d5(day: int) -> date:
    return date(2026, 5, day)


def test_wave3_diff_counts_switches_and_additions(tmp_path):
    tool = _tool("wave3_diff")
    old, new, policy = _backpull_copies(tmp_path)
    shas = (_sha(old), _sha(new))
    R = tool.run(old, new, T5, tmp_path / "diff", policy=policy, reference=tool.parse_dates("2026-05-03:2026-05-04,2026-05-08"))
    assert (_sha(old), _sha(new)) == shas                                 # both attached read only
    rec = R["records"]
    assert rec["kinds"] == {
        "cycle": {"old": 1, "new": 4, "old_first": "2026-05-07", "old_last": "2026-05-07", "new_first": "2026-03-10", "new_last": "2026-05-07"},
        "recovery": {"old": 1, "new": 3, "old_first": "2026-05-08", "old_last": "2026-05-08", "new_first": "2026-05-03", "new_last": "2026-05-08"},
        "sleep": {"old": 1, "new": 3, "old_first": "2026-05-07", "old_last": "2026-05-07", "new_first": "2026-05-02", "new_last": "2026-05-07"}}
    assert rec["by_month"] == [{"month": "2026-03", "kind": "cycle", "old": 0, "new": 1},
                               {"month": "2026-05", "kind": "cycle", "old": 1, "new": 3},
                               {"month": "2026-05", "kind": "recovery", "old": 1, "new": 3},
                               {"month": "2026-05", "kind": "sleep", "old": 1, "new": 3}]
    assert rec["months_changed"] == 4 and rec["revised_by_whoop"] == 0
    assert rec["new_months_under_25_cycles"] == [{"month": "2026-03", "cycles": 1}, {"month": "2026-04", "cycles": 0},
                                                 {"month": "2026-05", "cycles": 3}]
    assert R["copy_vs_api"] == {"api_nights": 3, "copy_nights": 3, "nights_with_both": 3, "within_3_min": 1, "within_3_min_pct": 33.3,
                                "median_abs_diff_min": 10.0, "over_30_min": 1,
                                "over_30_min_nights": [{"wake_date": "2026-05-04", "copy_h": 8.0, "api_h": 7.333, "diff_min": 40.0}]}
    wm = {r["metric"]: r for r in R["whoop_only_metrics"]}
    assert wm["recovery_score"] == {"metric": "recovery_score", "days_added": 2, "first_added": "2026-05-03", "last_added": "2026-05-04",
                                    "typical_added": 72.0, "lowest_added": 64.0, "largest_added": 80.0, "days_changed": 0,
                                    "days_switched": 0, "typical_change": None, "largest_change": None, "days_removed": 0,
                                    "days_old": 1, "days_new": 3}
    assert (wm["hrv_rmssd"]["days_added"], wm["hrv_rmssd"]["typical_added"], wm["hrv_rmssd"]["days_old"]) == (1, 45.5, 0)
    assert (wm["strain"]["days_added"], wm["strain"]["largest_added"], wm["strain"]["days_changed"]) == (1, 6.5, 0)
    assert (wm["sleep_need"]["days_added"], wm["sleep_need"]["first_added"]) == (1, "2026-05-03")
    assert R["owner_switches"] == [
        {"metric": "respiratory_rate", "old_device": "apple_watch_ultra", "new_device": "whoop", "days": 1, "first": "2026-05-03",
         "last": "2026-05-03", "median_signed_change": 0.2, "typical_change": 0.2, "largest_change": 0.2},
        {"metric": "resting_hr", "old_device": "apple_watch_ultra", "new_device": "whoop", "days": 1, "first": "2026-05-03",
         "last": "2026-05-03", "median_signed_change": -2.0, "typical_change": 2.0, "largest_change": 2.0},
        {"metric": "resting_hr", "old_device": None, "new_device": "whoop", "days": 1, "first": "2026-05-04", "last": "2026-05-04",
         "median_signed_change": None, "typical_change": None, "largest_change": None},
        {"metric": "sleep_duration", "old_device": "whoop:healthkit", "new_device": "whoop", "days": 2, "first": "2026-05-03",
         "last": "2026-05-04", "median_signed_change": -0.35, "typical_change": 0.35, "largest_change": 0.67}]
    ref = {n["date"]: n for n in R["reference_nights"]}
    assert list(ref) == ["2026-05-03", "2026-05-04", "2026-05-08"]
    assert ref["2026-05-03"]["metrics"]["sleep_duration"] == {"old": {"value": 7.03, "device": "whoop:healthkit", "grade": "B"},
                                                              "new": {"value": 7.0, "device": "whoop", "grade": "A"}}
    assert ref["2026-05-03"]["metrics"]["recovery_score"] == {"old": None, "new": {"value": 64.0, "device": "whoop", "grade": "A"}}
    w3 = ref["2026-05-03"]["whoop"]
    assert (w3["asleep_h"], w3["recovery_score"], w3["resting_hr"], w3["rmssd_ms"], w3["strain"]) == (7.0, 64, 58, 41.0, 6.5)
    rw = R["recent_window"]
    assert rw["daily_values"] == {"from": "2026-05-07", "moved": 0, "rows": [], "by_metric": {}}   # the oldest old record: May 7
    assert rw["latest_baselines_moved"] == {"baselines": [], "device_baselines": []}
    assert R["whole_tables"]["daily_values"] == {"rows_differing": 10, "only_old": 0, "only_new": 6, "first": "2026-05-03",
                                                 "last": "2026-05-04"}
    assert R["whole_tables"]["baselines"]["rows_differing"] == 0
    assert R["whole_tables"]["whoop_records"] == {"rows_differing": 7, "only_old": 0, "only_new": 7, "first": None, "last": None}
    assert R["empty"] is False
    assert json.loads((tmp_path / "diff" / "diff.json").read_text())["copy_vs_api"] == R["copy_vs_api"]
    md = (tmp_path / "diff" / "summary.md").read_text()
    assert "## Whoop's Apple Health copy against the API" in md and "| 2026-03 | 1 / 1 | 0 / 0 | 0 / 0 |" not in md
    assert "| 2026-03 | 0 / 1 | 0 / 0 | 0 / 0 |" in md and "| 2026-05-04 | 8 | 7.333 | 40 |" in md


def test_wave3_diff_lists_a_moved_recent_day(tmp_path):
    tool = _tool("wave3_diff")
    old, new, policy = _backpull_copies(tmp_path, revised=True)          # Whoop revised the May 8 strain after the old pull
    R = tool.run(old, new, T5, tmp_path / "diff", policy=policy)
    dv = R["recent_window"]["daily_values"]
    assert dv["from"] == "2026-05-07" and dv["moved"] == 2 and dv["by_metric"] == {"hrv_rmssd": 1, "strain": 1}
    assert dv["rows"] == [
        {"date": "2026-05-08", "metric": "strain", "fields": ["value"], "old": {"value": 8.0, "device": "whoop", "grade": "A"},
         "new": {"value": 8.4, "device": "whoop", "grade": "A"}},
        {"date": "2026-05-09", "metric": "hrv_rmssd", "fields": ["added"], "old": None, "new": {"value": 47.0, "device": "whoop", "grade": "A"}}]
    assert R["recent_window"]["latest_baselines_moved"]["baselines"] == [
        {"metric": "strain", "window_days": 30, "old_date": "2026-05-09", "old_median": 7.0, "old_mad": 1.0, "old_n_days": 20,
         "new_date": "2026-05-09", "new_median": 7.2, "new_mad": 1.0, "new_n_days": 20}]          # the 60-day one did not move
    assert R["records"]["revised_by_whoop"] == 1 and R["records"]["revised"][0]["record_key"] == "cycle:c-8"
    later = tool.run(old, new, T5, tmp_path / "diff2", policy=policy, recent_from=_d5(9))
    assert [(r["date"], r["metric"]) for r in later["recent_window"]["daily_values"]["rows"]] == [("2026-05-09", "hrv_rmssd")]
    md = (tmp_path / "diff" / "summary.md").read_text()
    assert "| 2026-05-08 | strain | value | 8.0 whoop A | 8.4 whoop A |" in md


def test_wave3_diff_identical_stores_is_empty(tmp_path):
    tool = _tool("wave3_diff")
    _old, new, policy = _backpull_copies(tmp_path, revised=True)
    c = db.connect(new)                                                     # every derived table holds a row
    db.execute(c, "INSERT INTO signals (date, metric, state, context_flags) VALUES (?, 'strain', 'neutral', '[]')", [_d5(8)])
    db.execute(c, "INSERT INTO device_baselines VALUES (?, 'sleep_duration', 30, 'apple_watch_ultra', 7.0, 0.3, 20)", [_d5(9)])
    c.close()
    same = tmp_path / "same.duckdb"
    shutil.copyfile(new, same)
    R = tool.run(new, same, T5, tmp_path / "diff", policy=policy, reference=[_d5(3)])
    assert R["empty"] is True
    assert all(v["rows_differing"] == 0 for v in R["whole_tables"].values())
    assert R["recent_window"]["daily_values"]["moved"] == 0 and R["records"]["months_changed"] == 0
    assert R["owner_switches"] == [] and all(r["days_added"] == 0 and r["days_changed"] == 0 for r in R["whoop_only_metrics"])
    assert R["copy_vs_api"]["nights_with_both"] == 3                         # the copy check reads the new copy alone
    assert "Nothing differs anywhere (empty diff)." in (tmp_path / "diff" / "summary.md").read_text()


def test_parse_dates_takes_ranges_and_single_days():
    tool = _tool("wave3_diff")
    assert tool.parse_dates("2026-05-03:2026-05-05, 2026-05-01,2026-05-04") == [_d5(1), _d5(3), _d5(4), _d5(5)]
    assert tool.parse_dates(None) == [] and tool.parse_dates("") == []
    try:
        tool.parse_dates("2026-05-05:2026-05-03")
    except ValueError as e:
        assert "reversed" in str(e)
    else:
        raise AssertionError("a reversed range must be refused")
