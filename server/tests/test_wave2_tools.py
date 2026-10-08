"""Wave 2 group D (fix program design, sections 3 and 4): the full derived
rebuild shared with Phase 1b (signals/rebuild.py), the rebuild tool
(server/tools/rebuild_derived.py) and the diff tool (server/tools/wave2_diff.py),
on tiny synthetic stores. Every expected value is worked out by hand.
Synthetic data only."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from heliosd.signals import baselines as bl
from heliosd.signals.rebuild import rebuild_all
from heliosd.store import db
from heliosd.trust.registry import SourceRegistry
from tests.test_wave2_export import DAY, _add, _policy, _utc

TOOLS = Path(__file__).resolve().parents[1] / "tools"
TODAY = DAY + timedelta(days=1)            # 2026-05-04
FIRST = DAY - timedelta(days=9)            # 2026-04-24, the oldest synthetic row


def _tool(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tiny_store(path: Path, policy=None) -> None:
    """Ten days of watch steps (1000 + 100 * i on day i), heart rate on the
    last two days, an export twin of a meal on DAY, and a Phase 1b ambiguous
    BMI export row beside its Bridge row."""
    conn = db.connect(path)
    (policy or _policy()).sync_registry(conn)
    for i in range(10):
        d = FIRST + timedelta(days=i)
        _add(conn, f"hk:st-{i}", "steps", "apple_watch_ultra", "bridge", _utc("06:00", d), 1000.0 + 100 * i, "count",
             end=_utc("07:00", d))
    for d in (DAY - timedelta(days=1), DAY):
        for j, v in enumerate((60.0, 64.0)):
            _add(conn, f"hk:hr-{d.day}-{j}", "heart_rate", "apple_watch_ultra", "bridge", _utc(f"0{4 + j}:00", d), v, "count/min")
    _add(conn, "hk:d-1", "dietary_energy", "myfitnesspal", "bridge", _utc("08:00"), 650.255, "kcal")
    _add(conn, "xp:d-1", "dietary_energy", "myfitnesspal", "health_export", _utc("08:00"), 650.26, "kcal")
    _add(conn, "hk:b-1", "bmi", "zepp_life_scale", "bridge", _utc("03:00"), 31.2, "count")
    _add(conn, "xp:b-1", "bmi", "zepp_life_scale", "health_export", _utc("03:00"), 31.2, "count", quality="export_ambiguous")
    conn.close()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _derived(path: Path) -> dict:
    c = duckdb.connect(str(path), read_only=True)
    try:
        return {"daily_values": c.execute("SELECT date, metric, value, device_key, n_samples, confidence, grade, corroboration, "
                                          "detail FROM daily_values ORDER BY 1, 2").fetchall(),
                "baselines": c.execute("SELECT * FROM baselines ORDER BY 1, 2, 3").fetchall(),
                "signals": c.execute("SELECT * FROM signals ORDER BY 1, 2").fetchall(),
                "quality": c.execute("SELECT sample_id, quality FROM samples ORDER BY 1").fetchall(),
                "aliases": c.execute("SELECT old_id, new_id, reason FROM sample_aliases ORDER BY 1, 2").fetchall()}
    finally:
        c.close()


# ---------------------------------------------------------------- rebuild_all (extracted from Phase 1b)

def test_rebuild_all_clears_every_derived_table_and_keeps_the_owners_actions(tmp_path, monkeypatch):
    monkeypatch.delattr(bl, "compute_baselines_range", raising=False)     # the per-date path (group C adds the range form)
    path = tmp_path / "s.duckdb"
    _tiny_store(path)
    conn = db.connect(path)
    policy = _policy()
    policy.sync_registry(conn)
    # Leftovers a rebuild must not keep: a day with no input, a device baseline, a narrative, a suggestion.
    db.execute(conn, "INSERT INTO daily_values (date, metric, value, unit, device_key) VALUES (?, 'steps', 5, 'count', 'iphone')",
               [FIRST - timedelta(days=30)])
    db.execute(conn, "INSERT INTO device_baselines VALUES (?, 'steps', 30, 'iphone', 1.0, 0.0, 9)", [DAY])
    db.execute(conn, "INSERT INTO narratives (date, narrative) VALUES (?, 'synthetic')", [DAY])
    db.execute(conn, "INSERT INTO derived_generation (date, generation) VALUES (?, 3)", [DAY])
    db.execute(conn, "INSERT INTO actions (action_id, date, text, status) VALUES ('a-1', ?, 'walk', 'suggested'), "
                     "('a-2', ?, 'sleep', 'adopted')", [DAY, DAY])
    out = rebuild_all(conn, policy, TODAY, registry=SourceRegistry())
    assert out["range"] == [FIRST, TODAY] and out["baselines_path"] == "per_date"
    steps = dict(db.fetchall(conn, "SELECT date, value FROM daily_values WHERE metric = 'steps'"))
    assert steps == {FIRST + timedelta(days=i): 1000.0 + 100 * i for i in range(10)}
    for table in ("device_baselines", "narratives", "derived_generation"):
        assert db.fetchall(conn, f"SELECT COUNT(*) FROM {table}")[0][0] == 0, table
    assert db.fetchall(conn, "SELECT action_id FROM actions") == [("a-2",)]
    # A steps baseline needs 7 earlier days: the first is on day 8 (median of 1000..1600).
    assert db.fetchall(conn, "SELECT date, median, n_days FROM baselines WHERE metric = 'steps' AND window_days = 30 "
                             "ORDER BY date LIMIT 1") == [(FIRST + timedelta(days=7), 1300.0, 7)]
    assert out["derived_after"]["daily_values"] == db.fetchall(conn, "SELECT COUNT(*) FROM daily_values")[0][0]
    conn.close()


def test_rebuild_all_uses_the_range_baselines_when_they_exist(tmp_path, monkeypatch):
    path = tmp_path / "s.duckdb"
    _tiny_store(path)
    conn = db.connect(path)
    policy = _policy()
    calls = []
    monkeypatch.setattr(bl, "compute_baselines_range", lambda c, p, start, end: calls.append((start, end)) or 42,
                        raising=False)
    monkeypatch.setattr(bl, "compute_baselines", lambda *a, **k: pytest.fail("the per-date path ran"))
    out = rebuild_all(conn, policy, TODAY, registry=SourceRegistry())
    assert calls == [(FIRST, TODAY)] and out["baselines"] == 42 and out["baselines_path"] == "range"
    assert out["signals"] > 0
    conn.close()


# ---------------------------------------------------------------- the rebuild tool

def test_rebuild_tool_refuses_the_live_path(tmp_path, monkeypatch):
    tool = _tool("rebuild_derived")
    live = tmp_path / "home" / "data" / "helios.duckdb"
    live.parent.mkdir(parents=True)
    _tiny_store(live)
    monkeypatch.setattr(tool, "LIVE_STORE", live)
    before = _sha(live)
    out = tmp_path / "out"
    assert tool.main([str(live), "--out", str(out), "--today", str(TODAY)]) == 2
    link = tmp_path / "alias.duckdb"
    os.symlink(live, link)
    assert tool.main([str(link), "--out", str(out), "--today", str(TODAY)]) == 2
    monkeypatch.setattr(tool, "holders", lambda p: [4242])               # the daemon still holds it
    assert tool.main([str(live), "--out", str(out), "--today", str(TODAY), "--apply"]) == 2
    assert _sha(live) == before and not out.exists()
    monkeypatch.setattr(tool, "holders", lambda p: [])                   # stopped: --apply may run
    assert tool.main([str(live), "--out", str(out), "--today", str(TODAY), "--apply"]) == 0
    assert json.loads((out / "summary.json").read_text())["apply"] is True


def test_holders_reads_lsof(tmp_path):
    if not (shutil.which("lsof") or os.path.exists("/usr/sbin/lsof")):
        pytest.skip("no lsof on this machine")
    tool = _tool("rebuild_derived")
    path = tmp_path / "held.duckdb"
    conn = duckdb.connect(str(path))
    try:
        assert os.getpid() in tool.holders(path)
    finally:
        conn.close()
    assert tool.holders(path) == []


def test_rebuild_tool_runs_the_wave2_migrations_and_is_idempotent(tmp_path, monkeypatch):
    tool = _tool("rebuild_derived")
    monkeypatch.delattr(tool.whoop, "rederive_all", raising=False)        # its own test below (group B adds it)
    path = tmp_path / "copy.duckdb"
    _tiny_store(path)
    policy = _policy()
    S1 = tool.run(path, TODAY, tmp_path / "out1", wave2_migrations=True, policy=policy, log=lambda m: None)
    assert S1["stopped"] is None
    assert S1["steps"] == ["init_schema", "sync_registry", "registry_check", "export_relink", "d5_rows", "rebuild_all", "checkpoint"]
    assert S1["results"]["export_relink"]["counts"]["linked"] == 1
    assert [r["sample_id"] for r in S1["results"]["d5_rows"]["rows"]] == ["xp:b-1"]
    assert {m["name"]: m["phase"] for m in S1["migrations"]} == {"wave2_d5_scale_rows": "verified",
                                                                 "wave2_export_relink_v1": "verified"}
    assert S1["counts"]["daily_values"] == S1["results"]["rebuild_all"]["derived_after"]["daily_values"] > 0
    assert S1["eligible_rows_outside_the_rebuilt_range"] == 0 and S1["peak_rss_mb"] > 0
    assert S1["unresolved_exports"] == [{"metric": "bmi", "device_key": "zepp_life_scale", "export_ambiguous": 1,
                                         "export_unmatched": 0}]
    assert json.loads((tmp_path / "out1" / "summary.json").read_text())["counts"] == S1["counts"]
    first = _derived(path)
    meal = [r for r in first["daily_values"] if r[1] == "dietary_energy"]
    assert [(r[0], r[2]) for r in meal] == [(DAY, 650.255)]                # the export twin no longer counts
    S2 = tool.run(path, TODAY, tmp_path / "out2", wave2_migrations=True, policy=policy, log=lambda m: None)
    assert S2["stopped"] is None and S2["results"]["export_relink"] == {
        "migration": "wave2_export_relink_v1", "already_applied": True, "linked": 0, "would_link": 0}
    assert S2["results"]["d5_rows"]["already_applied"] is True
    assert _derived(path) == first
    c = db.connect(path)                                                  # the daemon's own startup check passes
    assert db.unverified_migrations(c) == []
    c.close()


def test_rebuild_tool_stops_on_a_registry_problem_and_rebuilds_nothing(tmp_path, monkeypatch):
    tool = _tool("rebuild_derived")
    path = tmp_path / "copy.duckdb"
    _tiny_store(path)
    monkeypatch.setattr(tool.schema, "validate_policy_against_registry",
                        lambda policy, registry: ["metrics.steps.priority: 'whooop' is not a device of the registry"],
                        raising=False)
    S = tool.run(path, TODAY, tmp_path / "out", wave2_migrations=True, policy=_policy(), log=lambda m: None)
    assert "does not match the source registry" in S["stopped"] and "whooop" in S["stopped"]
    assert "export_relink" not in S["steps"] and "rebuild_all" not in S["steps"]
    assert S["counts"]["daily_values"] == 0 and S["migrations"] == []


def test_rebuild_tool_rederives_whoop_records_when_the_function_exists(tmp_path, monkeypatch):
    tool = _tool("rebuild_derived")
    path = tmp_path / "copy.duckdb"
    _tiny_store(path)
    seen = []

    def rederive_all(conn, policy):
        seen.append(policy)
        return {"records": 0, "samples": 0}
    monkeypatch.setattr(tool.whoop, "rederive_all", rederive_all, raising=False)
    policy = _policy()
    S = tool.run(path, TODAY, tmp_path / "out", wave2_migrations=True, policy=policy, log=lambda m: None)
    assert seen == [policy] and S["steps"][3:6] == ["export_relink", "d5_rows", "whoop_rederive"]
    assert S["results"]["whoop_rederive"] == {"ran": True, "result": {"records": 0, "samples": 0}}
    assert {m["name"]: m["phase"] for m in S["migrations"]}["wave2_whoop_rederive_v1"] == "verified"


# ---------------------------------------------------------------- the diff tool

T = date(2026, 5, 10)                      # --today: the nights are May 3 to May 9
DIFF_POLICY = dict(
    sleep_analysis={"hk": "HKCategoryTypeIdentifierSleepAnalysis", "unit": "min", "priority": ["whoop", "apple_watch_ultra", "zepp_helio"]},
    sleep_duration={"hk": None, "unit": "h", "priority": ["whoop", "apple_watch_ultra", "zepp_helio"]},
    respiratory_rate={"hk": "HKQuantityTypeIdentifierRespiratoryRate", "unit": "count/min", "priority": ["whoop", "apple_watch_ultra"],
                      "sync_paths": {"whoop": ["whoop_live"]}},
    resting_hr={"hk": "HKQuantityTypeIdentifierRestingHeartRate", "unit": "count/min", "agg": "last",
                "priority": ["whoop", "apple_watch_ultra", "apple_watch_6_legacy"]})


def _d(day: int) -> date:
    return date(2026, 5, day)


def _dv(conn, day, metric, value, device, grade, corr=None):
    db.execute(conn, "INSERT INTO daily_values (date, metric, value, unit, device_key, grade, corroboration) VALUES (?, ?, ?, 'u', ?, ?, ?)",
               [_d(day), metric, value, device, grade, json.dumps(corr) if corr else None])


def _wall(day: int, hhmm: str):
    h, m = (int(p) for p in hhmm.split(":"))
    return datetime(2026, 5, day, h, m) - timedelta(hours=4)          # the UTC instant of a Dubai wall time


def _stage(conn, sid, device, start, end, text):
    s, e = _wall(*start), _wall(*end)
    _add(conn, sid, "sleep_analysis", device, "bridge", s, (e - s).total_seconds() / 60, "min", end=e, text=text,
         source=f"Synthetic {device}")


def _copies(tmp_path):
    """old (shaped like a v3 store: no detail, no device_baselines) and new, by hand."""
    policy = _policy(**DIFF_POLICY)
    old, new = tmp_path / "old.duckdb", tmp_path / "new.duckdb"
    for path in (old, new):
        c = db.connect(path)
        policy.sync_registry(c)
        is_new = path == new
        _dv(c, 3, "sleep_duration", 7.36, "whoop", "A" if is_new else "C", {"apple_watch_ultra": 7.56 if is_new else 8.36})
        _dv(c, 4, "sleep_duration", 8.6, "whoop", "A", {"apple_watch_ultra": 7.67 if is_new else 8.06})
        if is_new:
            _dv(c, 5, "sleep_duration", 6.0, "apple_watch_ultra", "B")
        _dv(c, 3, "steps", 1100.0 if is_new else 1000.0, "apple_watch_ultra", "B")
        _dv(c, 4, "steps", 2000.0, "apple_watch_ultra", "A" if is_new else "B")
        if not is_new:
            _dv(c, 5, "steps", 500.0, "iphone", "B")
        _dv(c, 3, "respiratory_rate", 17.7, "whoop", "A")
        _dv(c, 4, "respiratory_rate", 17.2, "apple_watch_ultra", "B")
        _dv(c, 8, "heart_rate", 70.0, "zepp_helio", "A")
        db.execute(c, "INSERT INTO baselines VALUES (?, 'steps', 30, ?, 50.0, ?), (?, 'steps', 60, 1.0, 0.0, 9)",
                   [_d(9), 1550.0 if is_new else 1500.0, 9 if is_new else 10, _d(9)])
        db.execute(c, "INSERT INTO signals (date, metric, state, context_flags) VALUES (?, 'steps', 'neutral', ?), (?, 'steps', ?, '[]')",
                   [_d(5), "[]" if is_new else '["heat", "travel_or_shifted_schedule"]', _d(6), "neutral" if is_new else "flag"])
        if is_new:
            db.execute(c, "INSERT INTO device_baselines VALUES (?, 'sleep_duration', 30, 'apple_watch_ultra', 7.5, 0.3, 20), "
                          "(?, 'sleep_duration', 30, 'apple_watch_ultra', 7.0, 0.3, 19)", [_d(9), _d(8)])
            _stage(c, "hk:n3", "apple_watch_ultra", (3, "03:00"), (3, "05:30"), "core")          # 2.5 h: a value only at 2 h
            _stage(c, "hk:n4", "apple_watch_ultra", (4, "01:00"), (4, "08:40"), "core")          # 7.667 h, stored 7.67
            _stage(c, "hk:n5a", "apple_watch_ultra", (4, "23:00"), (5, "02:00"), "core")
            _stage(c, "hk:n5b", "apple_watch_ultra", (5, "02:00"), (5, "03:00"), "deep")
            _stage(c, "hk:n5c", "apple_watch_ultra", (5, "03:00"), (5, "03:10"), "awake")
            _stage(c, "hk:n5d", "apple_watch_ultra", (5, "03:10"), (5, "05:25"), "rem")          # 4 h + 2.25 h, stored 6.0
            _stage(c, "hk:z6", "zepp_helio", (6, "01:00"), (6, "03:12"), "core")                 # 2.2 h, a blank day
            _stage(c, "hk:z7", "zepp_helio", (7, "00:00"), (7, "04:00"), "core")                 # 4 h, not stored
            for day, hhmm in ((3, "07:00"), (4, "07:10"), (6, "06:50")):                          # Whoop's HealthKit copy
                _add(c, f"hk:rr{day}", "respiratory_rate", "whoop", "bridge", _wall(day, hhmm), 17.5, "count/min")
            _add(c, "wh:rr3", "respiratory_rate", "whoop", "whoop_live", _wall(3, "07:00"), 17.7, "count/min")
            for day in (7, 8):
                _add(c, f"hk:l{day}", "heart_rate", "apple_watch_6_legacy", "bridge", _wall(day, "12:00"), 72.0, "count/min",
                     source="Synthetic Watch 6")
            ms = 3.6e6
            payload = {"id": "s-1", "score": {"stage_summary": {"total_light_sleep_time_milli": 4 * ms, "total_slow_wave_sleep_time_milli": 2 * ms,
                                                                "total_rem_sleep_time_milli": 2.6 * ms, "total_in_bed_time_milli": 10.03 * ms},
                                              "sleep_efficiency_percentage": 86.0, "respiratory_rate": 17.27,
                                              "sleep_needed": {"baseline_milli": 7.5 * ms, "need_from_sleep_debt_milli": 0.3 * ms,
                                                               "need_from_recent_strain_milli": 0.15 * ms, "need_from_recent_nap_milli": 0}}}
            nap = {"id": "s-2", "nap": True, "score": {"stage_summary": {"total_light_sleep_time_milli": 9 * ms}}}
            for key, kind, nid, sid, cid, s_utc, e_utc, napf, created, p in (
                    ("sleep:s-1", "sleep", "s-1", "s-1", None, datetime(2026, 5, 3, 15), datetime(2026, 5, 4, 1), False, None, payload),
                    ("sleep:s-2", "sleep", "s-2", "s-2", None, datetime(2026, 5, 4, 8), datetime(2026, 5, 4, 9), True, None, nap),
                    ("recovery:c-1", "recovery", "c-1", "s-1", "c-1", None, None, None, datetime(2026, 5, 4, 1, 30),
                     {"cycle_id": "c-1", "sleep_id": "s-1", "score": {"recovery_score": 95, "resting_heart_rate": 64, "hrv_rmssd_milli": 51.234}}),
                    ("cycle:c-1", "cycle", "c-1", None, "c-1", datetime(2026, 5, 3, 14), None, None, None, {"id": "c-1", "score": {"strain": 7.7412}})):
                db.execute(c, "INSERT INTO whoop_records (record_key, kind, native_id, sleep_id, cycle_id, start_utc, end_utc, nap, created_at, "
                              "payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [key, kind, nid, sid, cid, s_utc, e_utc, napf, created, json.dumps(p)])
        else:
            c.execute("DROP TABLE device_baselines")
            c.execute("ALTER TABLE daily_values DROP COLUMN detail")
        c.close()
    return old, new, policy


def test_diff_tool_on_two_hand_made_copies(tmp_path):
    tool = _tool("wave2_diff")
    old, new, policy = _copies(tmp_path)
    old_sha, new_sha = _sha(old), _sha(new)
    R = tool.run(old, new, T, tmp_path / "diff", policy=policy)
    assert (_sha(old), _sha(new)) == (old_sha, new_sha)                  # both attached read only
    pm = {r["metric"]: r for r in R["per_metric"]}
    assert pm["sleep_duration"] == {"metric": "sleep_duration", "days_changed": 1, "days_added": 1, "days_removed": 0,
                                    "median_change": None, "max_change": 0.0, "grades_changed": 2}
    assert pm["steps"] == {"metric": "steps", "days_changed": 2, "days_added": 0, "days_removed": 1,
                           "median_change": 100.0, "max_change": 100.0, "grades_changed": 2}
    assert pm["respiratory_rate"]["days_changed"] == 0 and pm["heart_rate"]["grades_changed"] == 0
    assert R["owner_device_changes"] == [
        {"metric": "sleep_duration", "old_device": None, "new_device": "apple_watch_ultra", "days": 1, "first": "2026-05-05", "last": "2026-05-05"},
        {"metric": "steps", "old_device": "iphone", "new_device": None, "days": 1, "first": "2026-05-05", "last": "2026-05-05"}]
    assert R["baselines"]["owner"] == [{"metric": "steps", "old_date": "2026-05-09", "old_median": 1500.0, "old_n_days": 10,
                                        "new_date": "2026-05-09", "new_median": 1550.0, "new_n_days": 9}]
    assert R["baselines"]["device_sleep_duration_apple_watch_ultra"] == [
        {"date": "2026-05-09", "window_days": 30, "median": 7.5, "mad": 0.3, "n_days": 20}]
    sig = R["signals_last_30_days"]
    assert sig["states"] == {"flag": {"old": 1, "new": 0}, "neutral": {"old": 1, "new": 2}}
    assert sig["travel_days"] == {"old": ["2026-05-05"], "new": []} and (sig["from"], sig["to"]) == ("2026-04-11", "2026-05-10")
    nights = {n["wake_date"]: n for n in R["nights"]}
    assert list(nights) == [str(_d(d)) for d in range(3, 10)]
    n4 = nights["2026-05-04"]
    assert n4["old"] == {"value": 8.6, "device": "whoop", "grade": "A"} and n4["new"] == {"value": 8.6, "device": "whoop", "grade": "A",
                                                                                          "detail": None}
    assert n4["per_device_old"] == {"apple_watch_ultra": 8.06, "whoop": 8.6} and n4["per_device_new"] == {"apple_watch_ultra": 7.67, "whoop": 8.6}
    assert n4["oracle_h"] == {"apple_watch_ultra": 7.667}
    assert n4["whoop"] == {"asleep_h": 8.6, "in_bed_h": 10.03, "efficiency_pct": 86.0, "respiratory_rate": 17.27,
                           "sleep_needed_h": {"baseline": 7.5, "need_from_sleep_debt": 0.3, "need_from_recent_strain": 0.15,
                                              "need_from_recent_nap": 0.0},
                           "recovery_score": 95, "resting_hr": 64, "rmssd_ms": 51.2, "strain": 7.74, "strain_in_progress": True}
    assert nights["2026-05-05"]["old"] is None and nights["2026-05-05"]["oracle_h"] == {"apple_watch_ultra": 6.25}
    assert nights["2026-05-03"]["oracle_h"] == {} and nights["2026-05-03"]["whoop"] is None
    assert R["episode_oracle_check"] == {"compared": 2, "within_1_min": 1, "off": 1, "not_stored": 1, "off_nights": [
        {"wake_date": "2026-05-05", "device": "apple_watch_ultra", "oracle_h": 6.25, "stored_h": 6.0}]}
    q = R["owner_questions"]
    assert q["q1_whoop_healthkit_fallback"] == {
        "respiratory_rate": {"applies": True, "days_with_a_whoop_healthkit_value": 3, "days_that_would_change": 2,
                             "now_from": {"apple_watch_ultra": 1, "blank": 1}, "first": "2026-05-04", "last": "2026-05-06"},
        "resting_hr": {"applies": False, "reason": "the policy counts every Whoop row of this metric already"}}
    assert q["q3_two_hour_minimum"]["by_device"] == {
        "apple_watch_ultra": {"nights_gaining_a_value": 1, "blank_days_filled": 0, "owner_days_changed": 0, "corroboration_only": 1,
                              "first": "2026-05-03", "last": "2026-05-03"},
        "zepp_helio": {"nights_gaining_a_value": 1, "blank_days_filled": 1, "owner_days_changed": 0, "corroboration_only": 0,
                       "first": "2026-05-06", "last": "2026-05-06"}}
    assert q["q4_watch6_last"] == {
        "resting_hr": {"applies": False, "reason": "apple_watch_6_legacy is already in the list"},
        "heart_rate": {"applies": True, "days_with_legacy_rows": 2, "blank_days_filled": 1, "corroboration_only": 1,
                       "first": "2026-05-07", "last": "2026-05-07"}}
    assert json.loads((tmp_path / "diff" / "diff.json").read_text())["per_metric"] == R["per_metric"]
    md = (tmp_path / "diff" / "diff.md").read_text()
    assert "## The last 7 nights" in md and "| 2026-05-04 | 8.6 whoop A |" in md and "Compared 2: 1 within 1 minute" in md
