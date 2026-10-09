"""Wave 2 group C (design B10, B11, B15 and the owner-scope baselines): which
device's value a day takes and who only corroborates it, the steps merge, the
owner and device baselines, the CGM series and the loader's registry check.

Synthetic rows only (invented values and dates); expected values are worked
out by hand here, never taken from the code under test. The device lineup is
the fixture overlay (tests/fixtures), which mirrors the owner's lists of
decision 4h (fix program D11) with synthetic keys."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from heliosd.config import load_yaml
from heliosd.signals import baselines as bl
from heliosd.signals.baselines import compute_daily_values
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

D0, D1, D2 = date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)
AS_OF = date(2026, 6, 20)          # every day below is history: no running total, no freshness


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _store(rows, policy: MetricPolicy | None = None):
    """An in-memory store holding `rows`: (sample_id, metric, device_key,
    start, end, value) or with a seventh element, the sync path (default
    bridge). Wall times in the reporting zone."""
    conn = db.connect_memory()
    policy = policy or MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    if rows:
        db.insert_batch(conn, "INSERT INTO samples (sample_id, metric, device_key, sync_path, start_ts, end_ts, value, "
                              "source_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [[r[0], r[1], r[2], r[6] if len(r) > 6 else "bridge", _t(r[3]), _t(r[4]), r[5], f"Synthetic {r[2]}"]
                         for r in rows])
    return conn, policy


def _daily(conn, policy, start=D0, end=D2) -> dict:
    """{(metric, date): (value, device_key, corroboration dict or None)} after a recompute."""
    compute_daily_values(conn, policy, SourceRegistry(), start, end, as_of=AS_OF)
    return {(m, d): (v, dk, json.loads(co) if co else None) for d, m, v, dk, co in db.fetchall(
        conn, "SELECT date, metric, value, device_key, corroboration FROM daily_values")}


def _point(sid, metric, device, at, value, path="bridge"):
    return (sid, metric, device, at, at, value, path)


# ---- B10: priorities and corroboration (owner decision 4h, fix program D11) ----

def test_corroboration_device_is_never_the_value():
    """SpO2 lists Whoop and the strap as corroboration only: a day with only
    Whoop has no value (old: Whoop filled it), and a day with the watch shows
    both beside the watch's value."""
    conn, policy = _store([
        _point("hk:o-w0", "spo2", "whoop", "2026-06-10 03:00", 97.0),
        _point("hk:o-z0", "spo2", "zepp_helio", "2026-06-10 03:05", 95.0),
        _point("hk:o-a1", "spo2", "apple_watch_ultra", "2026-06-11 03:00", 96.0),
        _point("hk:o-w1", "spo2", "whoop", "2026-06-11 03:00", 95.0),
        _point("hk:o-z1", "spo2", "zepp_helio", "2026-06-11 03:10", 90.0),
    ])
    got = _daily(conn, policy)
    assert ("spo2", D0) not in got
    assert got[("spo2", D1)] == (96.0, "apple_watch_ultra", {"whoop": 95.0, "zepp_helio": 90.0})


def test_arm_and_whoop_never_fill_energy_or_sdnn():
    conn, policy = _store([
        ("hk:e-w0", "active_energy", "whoop", "2026-06-10 08:00", "2026-06-10 20:00", 500.0),
        ("hk:e-z0", "active_energy", "zepp_helio", "2026-06-10 08:00", "2026-06-10 20:00", 450.0),
        ("hk:e-a1", "active_energy", "apple_watch_ultra", "2026-06-11 08:00", "2026-06-11 20:00", 600.0),
        ("hk:e-w1", "active_energy", "whoop", "2026-06-11 08:00", "2026-06-11 20:00", 300.0),
        _point("hk:v-z0", "hrv_sdnn", "zepp_helio", "2026-06-10 02:00", 40.0),
        _point("hk:v-w0", "hrv_sdnn", "whoop", "2026-06-10 02:30", 41.0),
        _point("hk:v-a1", "hrv_sdnn", "apple_watch_ultra", "2026-06-11 02:00", 50.0),
        _point("hk:v-z1", "hrv_sdnn", "zepp_helio", "2026-06-11 02:00", 20.0),
    ])
    got = _daily(conn, policy)
    assert ("active_energy", D0) not in got and ("hrv_sdnn", D0) not in got
    # the arm devices are not even shown beside the watch: neither metric lists them
    assert got[("active_energy", D1)] == (600.0, "apple_watch_ultra", None)
    assert got[("hrv_sdnn", D1)] == (50.0, "apple_watch_ultra", None)


WATCH6_METRICS = {   # metric: (row on D0, Watch 6 alone; rows on D1, Ultra and Watch 6)
    "steps": (("2026-06-10 09:00", "2026-06-10 09:10", 900.0), ("2026-06-11 09:00", "2026-06-11 09:10", 1000.0, 990.0)),
    "active_energy": (("2026-06-10 09:00", "2026-06-10 09:10", 40.0), ("2026-06-11 09:00", "2026-06-11 09:10", 50.0, 48.0)),
    "basal_energy": (("2026-06-10 09:00", "2026-06-10 09:10", 70.0), ("2026-06-11 09:00", "2026-06-11 09:10", 72.0, 71.0)),
    "hrv_sdnn": (("2026-06-10 02:00", "2026-06-10 02:00", 45.0), ("2026-06-11 02:00", "2026-06-11 02:00", 52.0, 51.0)),
    "spo2": (("2026-06-10 03:00", "2026-06-10 03:00", 97.0), ("2026-06-11 03:00", "2026-06-11 03:00", 96.0, 95.0)),
}   # respiratory rate and sleep follow the night (group A and B day rules): their Watch 6 slot is checked as a list


def test_watch6_fills_history_never_outranks_ultra():
    """apple_watch_6_legacy sits right after the Ultra (B10, decision 4h's
    session reading): it fills a day the Ultra has no value for and only
    corroborates a day the Ultra has. In resting HR and all-day HR it is the
    LAST fallback (owner Q4, 2026-10-09: after Whoop, the Ultra and the Helio),
    so it fills a history day no other device has."""
    rows = []
    for metric, ((s0, e0, v0), (s1, e1, ultra, watch6)) in WATCH6_METRICS.items():
        rows += [(f"hk:{metric}-6a", metric, "apple_watch_6_legacy", s0, e0, v0),
                 (f"hk:{metric}-ub", metric, "apple_watch_ultra", s1, e1, ultra),
                 (f"hk:{metric}-6b", metric, "apple_watch_6_legacy", s1, e1, watch6)]
    rows += [_point("hk:r-6", "resting_hr", "apple_watch_6_legacy", "2026-06-10 07:00", 58.0),
             _point("hk:h-6", "heart_rate", "apple_watch_6_legacy", "2026-06-10 07:00", 70.0)]
    conn, policy = _store(rows)
    got = _daily(conn, policy)
    for metric, ((_, _, v0), (_, _, ultra, watch6)) in WATCH6_METRICS.items():
        assert got[(metric, D0)][:2] == (v0, "apple_watch_6_legacy"), metric
        assert got[(metric, D1)][:2] == (ultra, "apple_watch_ultra"), metric
        assert got[(metric, D1)][2]["apple_watch_6_legacy"] == watch6, metric
    assert got[("resting_hr", D0)][:2] == (58.0, "apple_watch_6_legacy")
    assert got[("heart_rate", D0)][:2] == (70.0, "apple_watch_6_legacy")


def test_glucose_cgm_never_fills_a_glucose_day():
    """glucose is the fingerstick meter only (decision 4h): a day with only
    CGM readings has no glucose value (old: the CGM filled it)."""
    rows = [_point(f"hk:g-c{i}", "glucose", "test_cgm", f"2026-06-10 {h:02d}:00", 100.0 + i)
            for i, h in enumerate(range(0, 24, 2))]
    rows += [_point("hk:g-m1", "glucose", "test_meter", "2026-06-11 08:00", 95.0),
             _point("hk:g-c99", "glucose", "test_cgm", "2026-06-11 08:00", 140.0)]
    conn, policy = _store(rows)
    got = _daily(conn, policy)
    assert ("glucose", D0) not in got
    assert got[("glucose", D1)] == (95.0, "test_meter", None)        # the CGM is not even shown beside it


def test_others_follows_the_corroboration_rule():
    """Absent: the other priority keys present; []: none; a list: the other
    priority keys present plus the listed ones; never a key outside both, and
    never another key of the value's own device (Whoop's HealthKit copy beside
    Whoop's API value)."""
    cfg = load_yaml("metric_policy.yaml")
    present = {"whoop": (7.0, 1, None), "whoop:healthkit": (6.9, 1, None), "apple_watch_ultra": (6.5, 1, None),
               "zepp_helio": (6.0, 1, None), "iphone": (1.0, 1, None)}
    p = MetricPolicy(cfg)
    assert p.corroboration("sleep_duration") is None
    assert bl._others(p, "sleep_duration", "whoop", present) == {"apple_watch_ultra": 6.5, "zepp_helio": 6.0}
    # the HealthKit copy as the value: the API key is its own device too
    assert bl._others(p, "sleep_duration", "whoop:healthkit", present) == {"apple_watch_ultra": 6.5, "zepp_helio": 6.0}
    assert bl._others(p, "sleep_duration", "apple_watch_ultra", present) == {"whoop": 7.0, "whoop:healthkit": 6.9,
                                                                             "zepp_helio": 6.0}
    cfg["metrics"]["sleep_duration"]["corroboration"] = []
    assert bl._others(MetricPolicy(cfg), "sleep_duration", "whoop", present) == {}
    cfg["metrics"]["sleep_duration"]["corroboration"] = ["iphone"]
    q = MetricPolicy(cfg)
    assert bl._others(q, "sleep_duration", "whoop", present) == {"apple_watch_ultra": 6.5, "zepp_helio": 6.0, "iphone": 1.0}
    assert bl._row_keys(q, "sleep_duration")[-1] == "iphone"          # read, so it can be shown
    assert bl._row_keys(p, "spo2") == ["apple_watch_ultra", "apple_watch_6_legacy", "whoop", "zepp_helio"]


# The D11 table (decision 4h) with B10's Watch 6 slot, on the fixture's synthetic keys.
D11_LISTS = {
    "heart_rate": ["zepp_helio", "apple_watch_ultra", "whoop", "apple_watch_6_legacy"],
    "resting_hr": ["whoop", "apple_watch_ultra", "zepp_helio", "apple_watch_6_legacy"],
    "hrv_sdnn": ["apple_watch_ultra", "apple_watch_6_legacy"],
    "hrv_rmssd": ["whoop"],
    "respiratory_rate": ["whoop", "apple_watch_ultra", "apple_watch_6_legacy", "zepp_helio"],
    "spo2": ["apple_watch_ultra", "apple_watch_6_legacy"],
    "wrist_temp": ["apple_watch_ultra"],
    "steps": ["apple_watch_ultra", "apple_watch_6_legacy", "iphone", "zepp_helio"],
    "active_energy": ["apple_watch_ultra", "apple_watch_6_legacy"],
    "basal_energy": ["apple_watch_ultra", "apple_watch_6_legacy"],
    "sleep_analysis": ["whoop", "apple_watch_ultra", "apple_watch_6_legacy", "zepp_helio"],
    "sleep_duration": ["whoop", "whoop:healthkit", "apple_watch_ultra", "apple_watch_6_legacy", "zepp_helio"],
    "glucose": ["test_meter"],
    "recovery_score": ["whoop"], "strain": ["whoop"], "sleep_need": ["whoop"],
}
# Trust labels before Wave 2; decision 4h changes sleep_duration only.
TRUST_BEFORE = {"heart_rate": "absolute", "resting_hr": "absolute", "hrv_sdnn": "directional", "hrv_rmssd": "absolute",
                "respiratory_rate": "absolute", "spo2": "screening", "wrist_temp": "absolute", "body_temp": "directional",
                "vo2max": "trend_only", "steps": "absolute", "active_energy": "trend_only", "basal_energy": "trend_only",
                "sleep_analysis": "directional", "sleep_duration": "absolute", "glucose": "absolute",
                "body_mass": "absolute", "body_fat_pct": "trend_only", "lean_mass": "trend_only", "bmi": "absolute",
                "dietary_energy": "absolute", "recovery_score": "directional", "strain": "directional",
                "sleep_need": "directional"}
GENERIC = {"apple_watch_ultra": "apple_watch", "zepp_helio": "zepp_strap", "test_meter": "glucose_meter"}


def test_policy_heads_match_d11():
    p = MetricPolicy()
    for metric, prio in D11_LISTS.items():
        assert p.priority(metric) == prio, metric
    assert p.corroboration("spo2") == ["whoop", "zepp_helio"]
    for metric in ("active_energy", "basal_energy", "hrv_sdnn", "spo2", "wrist_temp", "glucose"):
        assert not {"whoop", "zepp_helio"} & set(p.priority(metric)), metric       # no arm or Whoop fallback
    assert {m: p.get(m).get("trust") for m in TRUST_BEFORE} == {**TRUST_BEFORE, "sleep_duration": "trend_only"}
    # the public default has the same structure on its generic keys (no legacy watch in the generic registry)
    g = MetricPolicy(load_yaml("metric_policy.yaml", overlay=False))
    for metric, prio in D11_LISTS.items():
        assert g.priority(metric) == [GENERIC.get(k, k) for k in prio if k != "apple_watch_6_legacy"], metric
    assert g.corroboration("spo2") == ["whoop", "zepp_strap"] and g.get("sleep_duration")["trust"] == "trend_only"
    assert g.sync_paths("sleep_duration") == p.sync_paths("sleep_duration") == {"whoop": ["whoop_live"]}


# ---- B11: steps interval merge (owner decision D4) ----

def _steps(sid, device, start, end, value):
    return (sid, "steps", device, start, end, value)


def _detail(conn, metric, d):
    rows = db.fetchall(conn, "SELECT detail FROM daily_values WHERE metric = ? AND date = ?", [metric, d])
    return json.loads(rows[0][0]) if rows and rows[0][0] else None


def test_steps_merge_counts_iphone_outside_watch_intervals():
    """The design's example: watch 10:00 to 10:10 = 50 counts in full; the
    iPhone's 10:00 to 10:30 = 300 counts for the 20 of its 30 minutes the
    watch did not cover (200); its 12:00 to 12:10 = 100 in full. 350 (old: the
    watch's 50 alone)."""
    conn, policy = _store([
        _steps("hk:s-w1", "apple_watch_ultra", "2026-06-10 10:00", "2026-06-10 10:10", 50.0),
        _steps("hk:s-p1", "iphone", "2026-06-10 10:00", "2026-06-10 10:30", 300.0),
        _steps("hk:s-p2", "iphone", "2026-06-10 12:00", "2026-06-10 12:10", 100.0),
    ])
    got = _daily(conn, policy)
    assert got[("steps", D0)] == (350.0, "apple_watch_ultra", {"iphone": 400.0})   # the iPhone's own total beside it


def test_steps_merge_records_fed_by(tmp_path, monkeypatch):
    """D4's per-day record: which devices fed the value and how much each
    added; an iPhone-only day is the iPhone's (a fallback day); the API passes
    the record on (/api/metrics and /api/activity)."""
    rows = [
        _steps("hk:s-w1", "apple_watch_ultra", "2026-06-10 10:00", "2026-06-10 10:10", 50.0),
        _steps("hk:s-p1", "iphone", "2026-06-10 10:00", "2026-06-10 10:30", 300.0),
        _steps("hk:s-p2", "iphone", "2026-06-10 12:00", "2026-06-10 12:10", 100.0),
        _steps("hk:s-w2", "apple_watch_ultra", "2026-06-11 08:00", "2026-06-11 09:00", 900.0),
        _steps("hk:s-p3", "iphone", "2026-06-11 08:10", "2026-06-11 08:40", 250.0),       # inside the watch's hour
        _steps("hk:s-p4", "iphone", "2026-06-12 18:00", "2026-06-12 18:20", 640.0),       # no watch at all
    ]
    conn, policy = _store(rows)
    got = _daily(conn, policy)
    assert _detail(conn, "steps", D0) == {"fed_by": {"apple_watch_ultra": 50, "iphone": 300}}
    assert got[("steps", D1)] == (900.0, "apple_watch_ultra", {"iphone": 250.0})
    assert _detail(conn, "steps", D1) == {"fed_by": {"apple_watch_ultra": 900}}         # the iPhone added nothing
    assert got[("steps", D2)] == (640.0, "iphone", None)
    assert _detail(conn, "steps", D2) == {"fed_by": {"iphone": 640}}
    # the API passes the record on
    from fastapi.testclient import TestClient
    from heliosd.config import Settings
    from heliosd.main import create_app
    from heliosd.signals import recompute as rc
    monkeypatch.setattr(rc, "reporting_today", lambda zone, now=None: AS_OF)
    token = "test-token-0123456789"
    raw = {"server": {"ingest_token": token}, "owner": {"timezone": "Asia/Dubai"},
           "storage": {"db_path": str(tmp_path / "helios.duckdb")}, "notifications": {"macos_alerts": False}}
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    with TestClient(create_app(Settings(raw=raw)), client=("127.0.0.1", 50000)) as c:
        app_conn = c.app.state.conn
        db.insert_batch(app_conn, "INSERT INTO samples (sample_id, metric, device_key, sync_path, start_ts, end_ts, value, "
                                  "source_name) VALUES (?, ?, ?, 'bridge', ?, ?, ?, 'Synthetic')",
                        [[r[0], r[1], r[2], _t(r[3]), _t(r[4]), r[5]] for r in rows])
        compute_daily_values(app_conn, c.app.state.policy, SourceRegistry(), D0, D2, as_of=AS_OF)
        h = {"X-Helios-Token": token}
        series = {r["date"]: r for r in c.get("/api/metrics/steps?days=30", headers=h).json()["series"]}
        assert series[str(D0)]["detail"] == {"fed_by": {"apple_watch_ultra": 50, "iphone": 300}}
        act = {r["date"]: r for r in c.get("/api/activity?days=30", headers=h).json()["steps"]}
        assert act[str(D0)]["detail"] == {"fed_by": {"apple_watch_ultra": 50, "iphone": 300}}
        assert act[str(D2)]["detail"] == {"fed_by": {"iphone": 640}}


def test_strap_fills_only_the_steps_gaps_and_whoop_never_counts():
    """D4 revised (owner, 2026-10-09): the strap's steps count only for the
    time neither the watch nor the iPhone covered, as in Apple Health's own
    source order; Whoop's never count (old: the strap never counted either,
    so the first day was 150 and the strap-only day had no value)."""
    conn, policy = _store([
        _steps("hk:s-w1", "apple_watch_ultra", "2026-06-10 10:00", "2026-06-10 10:10", 50.0),
        _steps("hk:s-z1", "zepp_helio", "2026-06-10 11:00", "2026-06-10 11:10", 500.0),     # nobody above it: in full
        _steps("hk:s-z3", "zepp_helio", "2026-06-10 10:00", "2026-06-10 10:20", 80.0),      # half under the watch: 40
        _steps("hk:s-h1", "whoop", "2026-06-10 11:30", "2026-06-10 11:40", 700.0),          # never counts
        _steps("hk:s-p1", "iphone", "2026-06-10 12:00", "2026-06-10 12:10", 100.0),
        _steps("hk:s-z4", "zepp_helio", "2026-06-10 12:00", "2026-06-10 12:10", 90.0),      # under the iPhone: 0
        _steps("hk:s-z2", "zepp_helio", "2026-06-11 11:00", "2026-06-11 11:10", 500.0),
        _steps("hk:s-h2", "whoop", "2026-06-12 09:00", "2026-06-12 21:00", 900.0),
    ])
    got = _daily(conn, policy)
    assert got[("steps", D0)] == (690.0, "apple_watch_ultra", {"iphone": 100.0, "zepp_helio": 670.0})
    assert _detail(conn, "steps", D0) == {"fed_by": {"apple_watch_ultra": 50, "iphone": 100, "zepp_helio": 540}}
    assert got[("steps", D1)] == (500.0, "zepp_helio", None)                         # a strap-only day: its fallback
    assert ("steps", D2) not in got                                                   # Whoop alone: no value


def test_watch6_is_the_watch_in_2021():
    """Before the Ultra the Watch 6 is the watch: its steps count in full and
    the iPhone's only outside them (old: the Watch 6's own total)."""
    d = date(2021, 3, 4)
    conn, policy = _store([
        _steps("hk:s-6a", "apple_watch_6_legacy", "2021-03-04 07:00", "2021-03-04 07:10", 120.0),
        _steps("hk:s-6b", "apple_watch_6_legacy", "2021-03-04 17:00", "2021-03-04 17:05", 80.0),
        _steps("hk:s-pa", "iphone", "2021-03-04 07:05", "2021-03-04 07:15", 60.0),       # half inside the watch
        _steps("hk:s-pb", "iphone", "2021-03-04 09:00", "2021-03-04 09:00", 15.0),       # a point outside it
    ])
    got = _daily(conn, policy, d, d)
    assert got[("steps", d)] == (245.0, "apple_watch_6_legacy", {"iphone": 75.0})     # 120 + 80 + 30 + 15
    assert _detail(conn, "steps", d) == {"fed_by": {"apple_watch_6_legacy": 200, "iphone": 45}}


def test_steps_merge_is_the_same_whatever_the_window():
    """A watch interval across midnight covers the iPhone's steps after
    midnight, which are filed on the next day: recomputing that day alone
    reads the interval too, so a day never changes with its window."""
    rows = [
        _steps("hk:s-w0", "apple_watch_ultra", "2026-06-10 23:55", "2026-06-11 00:05", 10.0),
        _steps("hk:s-p0", "iphone", "2026-06-11 00:02", "2026-06-11 00:02", 7.0),          # inside the watch's interval
        _steps("hk:s-w1", "apple_watch_ultra", "2026-06-11 09:00", "2026-06-11 09:10", 100.0),
        _steps("hk:s-p1", "iphone", "2026-06-11 12:00", "2026-06-11 12:10", 40.0),
    ]
    conn, policy = _store(rows)
    wide = _daily(conn, policy, D0, D2)
    assert wide[("steps", D1)][:2] == (140.0, "apple_watch_ultra")
    conn2, _ = _store(rows)
    alone = _daily(conn2, policy, D1, D1)
    assert alone[("steps", D1)] == wide[("steps", D1)] and _detail(conn2, "steps", D1) == {
        "fed_by": {"apple_watch_ultra": 100, "iphone": 40}}


# ---- owner-scope baselines, device baselines, the range form (design B5, B15) ----

def _dv(conn, d, metric, value, device, grade="A", corr=None):
    db.execute(conn, "INSERT OR REPLACE INTO daily_values (date, metric, value, unit, device_key, n_samples, confidence, "
                     "grade, corroboration) VALUES (?, ?, ?, 'u', ?, 1, 0.9, ?, ?)",
               [d, metric, value, device, grade, json.dumps(corr, sort_keys=True) if corr else None])


def _device_baselines(conn, metric, d) -> dict:
    return {(k, w): (m, mad, n) for k, w, m, mad, n in db.fetchall(
        conn, "SELECT device_key, window_days, median, mad, n_days FROM device_baselines WHERE metric = ? AND date = ?",
        [metric, d])}


B_AS_OF = date(2026, 6, 30)


def _ago(i: int) -> date:
    return B_AS_OF - timedelta(days=i)


def test_owner_baseline_excludes_fallback_days():
    """resting_hr is Whoop's (decision 4h): Apple's stand-in days never enter
    the baseline every judgement reads (old: one baseline over every device's
    days, median 80 over 22 days); they build Apple's own baseline instead."""
    conn, policy = _store([])
    for i in range(1, 11):
        _dv(conn, _ago(i), "resting_hr", 60.0, "whoop")
    for i in range(11, 23):
        _dv(conn, _ago(i), "resting_hr", 80.0, "apple_watch_ultra")
    assert bl.compute_baselines(conn, policy, B_AS_OF) == 3              # 30, 60 and 90 days, Whoop's days only
    assert bl.get_baseline(conn, "resting_hr", B_AS_OF, 30) == {"median": 60.0, "mad": 0.0, "n_days": 10}
    assert _device_baselines(conn, "resting_hr", B_AS_OF)[("apple_watch_ultra", 30)] == (80.0, 0.0, 12)


def test_apple_sleep_device_baseline_exists():
    """Apple's own sleep baseline beside Whoop's (design B15): from the nights
    it corroborated and the night it stood in; the strap (corroboration) gets
    one too; Whoop's HealthKit copy is not the owner, so its night is in no
    owner baseline (old: no device baselines at all)."""
    conn, policy = _store([])
    for i in range(1, 9):
        _dv(conn, _ago(i), "sleep_duration", 7.0 + i / 10, "whoop", corr={"apple_watch_ultra": 6.0 + i / 10, "zepp_helio": 7.5})
    _dv(conn, _ago(9), "sleep_duration", 5.5, "apple_watch_ultra")
    _dv(conn, _ago(10), "sleep_duration", 6.6, "whoop:healthkit", corr={"apple_watch_ultra": 6.2})
    bl.compute_baselines(conn, policy, B_AS_OF)
    own = bl.get_baseline(conn, "sleep_duration", B_AS_OF, 30)
    assert own["n_days"] == 8 and own["median"] == pytest.approx(7.45)    # the eight Whoop API nights
    dev = _device_baselines(conn, "sleep_duration", B_AS_OF)
    med, mad, n = dev[("apple_watch_ultra", 30)]                             # 6.1 to 6.8, 5.5 and 6.2
    assert n == 10 and med == pytest.approx(6.35) and mad == pytest.approx(0.2)
    assert dev[("zepp_helio", 30)] == (7.5, 0.0, 8)
    assert not any(k == "whoop:healthkit" for k, _ in dev)                 # one night: under min_days


def test_baselines_count_graded_days_only():
    """The reporting today's running total has no grade until the day closes
    (D7), so a day left ungraded never enters a window (old: counted)."""
    conn, policy = _store([])
    for i in range(1, 9):
        _dv(conn, _ago(i), "steps", 1000.0 * i, "apple_watch_ultra", grade=None if i == 1 else "A")
    bl.compute_baselines(conn, policy, B_AS_OF)
    assert bl.get_baseline(conn, "steps", B_AS_OF, 30) == {"median": 5000.0, "mad": 2000.0, "n_days": 7}


def _baseline_tables(conn):
    return (db.fetchall(conn, "SELECT date, metric, window_days, median, mad, n_days FROM baselines ORDER BY 1, 2, 3"),
            db.fetchall(conn, "SELECT date, metric, window_days, device_key, median, mad, n_days FROM device_baselines "
                              "ORDER BY 1, 2, 3, 4"))


def _baseline_history(conn, rng, first: date, days: int) -> None:
    """Synthetic daily values over `days` days: owners, stand-ins, corroboration,
    a qualified key, gaps and ungraded days, on several metrics."""
    for i in range(days):
        d = first + timedelta(days=i)
        if rng.random() < 0.08:
            continue                                                        # a gap
        grade = None if rng.random() < 0.05 else "A"
        if rng.random() < 0.8:
            _dv(conn, d, "steps", float(rng.randint(2000, 12000)), "apple_watch_ultra", grade,
                {"iphone": float(rng.randint(500, 9000))})
        else:
            _dv(conn, d, "steps", float(rng.randint(500, 9000)), "iphone", grade)
        owner = rng.choice(["whoop", "whoop", "whoop", "whoop:healthkit", "apple_watch_ultra"])
        corr = {"apple_watch_ultra": round(rng.uniform(5, 8), 2)} if owner != "apple_watch_ultra" else {}
        if rng.random() < 0.5:
            corr["zepp_helio"] = round(rng.uniform(5, 9), 2)
        _dv(conn, d, "sleep_duration", round(rng.uniform(5, 9), 2), owner, grade, corr or None)
        _dv(conn, d, "spo2", round(rng.uniform(94, 99), 1), "apple_watch_ultra", grade,
            {"whoop": round(rng.uniform(93, 99), 1), "zepp_helio": round(rng.uniform(92, 99), 1)})
        if rng.random() < 0.6:
            _dv(conn, d, "resting_hr", float(rng.randint(52, 66)), rng.choice(["whoop", "apple_watch_ultra"]), grade)


def test_range_baselines_equal_the_per_date_function():
    """The rebuild's range form (one read per metric for the whole range)
    writes exactly the rows the per-date function writes date by date, for
    owner and device baselines, and replaces stale rows inside the range only
    (old: no range form)."""
    import random
    first = date(2026, 1, 1)
    start, end = date(2026, 2, 10), date(2026, 5, 15)                     # the range starts inside the history
    stale = [[date(2026, 3, 1), "retired_metric", 30, 1.0, 0.0, 9], [date(2026, 1, 20), "steps", 30, 1.0, 0.0, 9]]
    snaps, counts = [], []
    for form in ("per_date", "range"):
        conn, policy = _store([])
        _baseline_history(conn, random.Random(42), first, 130)
        db.insert_batch(conn, "INSERT INTO baselines (date, metric, window_days, median, mad, n_days) VALUES (?, ?, ?, ?, ?, ?)",
                        stale)
        if form == "per_date":
            d, n = start, 0
            while d <= end:
                n += bl.compute_baselines(conn, policy, d)
                d += timedelta(days=1)
        else:
            n = bl.compute_baselines_range(conn, policy, start, end)
        snaps.append(_baseline_tables(conn))
        counts.append(n)
    assert snaps[0] == snaps[1] and counts[0] == counts[1] == len(snaps[0][0]) - 1    # minus the stale row outside the range
    base, dev = snaps[0]
    assert (date(2026, 1, 20), "steps", 30, 1.0, 0.0, 9) in base and not any(r[1] == "retired_metric" for r in base)
    assert {r[3] for r in dev} == {"iphone", "apple_watch_ultra", "zepp_helio", "whoop", "whoop:healthkit"}
    assert {r[1] for r in base} == {"steps", "sleep_duration", "spo2", "resting_hr"} and len(base) > 600
    assert bl.compute_baselines_range(conn, policy, end, start) == 0                  # an empty range writes nothing


def test_baseline_rows_are_written_exactly():
    """The bulk writer (literal rows: DuckDB binds parameters slowly) keeps
    every value bit for bit, a quote in a key included."""
    conn, _ = _store([])
    rows = [[date(2026, 6, 1), "steps", 30, "it's_a_key", 0.1 + 0.2, 1e-17, 9],
            [date(2026, 6, 2), "steps", 60, "whoop:healthkit", 1 / 3, 123456.789, 60]]
    with db.transaction(conn) as c:
        bl._insert_rows(c, "device_baselines", "date, metric, window_days, device_key, median, mad, n_days", rows, chunk=1)
    assert db.fetchall(conn, "SELECT date, metric, window_days, device_key, median, mad, n_days FROM device_baselines "
                             "ORDER BY date") == [tuple(r) for r in rows]


def test_metrics_and_sleep_routes_return_owner_and_device_baselines(tmp_path, monkeypatch):
    """/api/metrics and /api/sleep return the owner's baseline (the one the
    judgement reads, naming its device) and every other device's own,
    labelled (old: owner rows only, unlabelled; /api/sleep none)."""
    from fastapi.testclient import TestClient
    from heliosd.config import Settings
    from heliosd.main import create_app
    from heliosd.signals import recompute as rc
    from heliosd.signals import sleep_report
    monkeypatch.setattr(rc, "reporting_today", lambda zone, now=None: B_AS_OF)
    monkeypatch.setattr(sleep_report, "reporting_today", lambda zone, now=None: B_AS_OF)
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    token = "test-token-0123456789"
    raw = {"server": {"ingest_token": token}, "owner": {"timezone": "Asia/Dubai"},
           "storage": {"db_path": str(tmp_path / "helios.duckdb")}, "notifications": {"macos_alerts": False}}
    with TestClient(create_app(Settings(raw=raw)), client=("127.0.0.1", 50000)) as c:
        conn, policy = c.app.state.conn, c.app.state.policy
        for i in range(1, 9):
            _dv(conn, _ago(i), "sleep_duration", 7.0, "whoop", corr={"apple_watch_ultra": 6.5})
        bl.compute_baselines(conn, policy, B_AS_OF)
        h = {"X-Helios-Token": token}
        m = c.get("/api/metrics/sleep_duration?days=30", headers=h).json()
        s = c.get("/api/sleep?days=30", headers=h).json()
    for body in (m, s):
        assert body["owner_device"] == "whoop"
        assert [(b["window_days"], b["median"], b["n_days"], b["device_key"], b["current"]) for b in body["baselines"]] == [
            (30, 7.0, 8, "whoop", True), (60, 7.0, 8, "whoop", True), (90, 7.0, 8, "whoop", True)]
        assert [(b["device_key"], b["label"], b["window_days"], b["median"], b["date"], b["current"])
                for b in body["device_baselines"]] == [
            ("apple_watch_ultra", "Apple Watch Ultra", w, 6.5, str(B_AS_OF), True) for w in (30, 60, 90)]
    assert [n["date"] for n in s["nights"]] == [str(_ago(i)) for i in range(8, 0, -1)]


# ---- B15: the CGM history as a series of its own (glucose_cgm) ----

def _cgm(day: str, slots: int, value, first_slot: int = 0, per_slot: int = 1) -> list:
    """CGM readings in `slots` consecutive quarter hours from `first_slot`
    (slot 0 is 00:00 to 00:15), `per_slot` readings in each."""
    out = []
    for s in range(first_slot, first_slot + slots):
        for k in range(per_slot):
            minute = s * 15 + k * 5
            v = value(s) if callable(value) else value
            out.append(_point(f"hk:cgm-{day}-{s}-{k}", "glucose", "test_cgm", f"{day} {minute // 60:02d}:{minute % 60:02d}", v))
    return out


def test_glucose_cgm_is_the_cgm_history_on_covered_days():
    """glucose_cgm (design B15) is the CGM's readings on the days they cover:
    at least 70 percent of the 96 quarter hours, so 68 count and 67 do not,
    and readings bunched in a few quarter hours never make a day. glucose
    stays the meter's (old: no glucose_cgm series at all)."""
    d3 = date(2026, 6, 13)
    rows = (_cgm("2026-06-10", 96, lambda s: 100.0 if s % 2 else 110.0)        # every quarter hour, average 105
            + _cgm("2026-06-11", 68, 120.0, per_slot=2)                      # 68 of 96: counts (136 readings)
            + _cgm("2026-06-12", 67, 130.0)                                  # 67 of 96: does not
            + _cgm("2026-06-13", 4, 140.0, per_slot=3))                      # 12 readings in one hour: does not
    rows += [_point("hk:m-1", "glucose", "test_meter", "2026-06-12 08:00", 95.0)]
    conn, policy = _store(rows)
    got = _daily(conn, policy, D0, d3)
    assert got[("glucose_cgm", D0)] == (105.0, "test_cgm", None)
    assert got[("glucose_cgm", D1)] == (120.0, "test_cgm", None)
    assert ("glucose_cgm", D2) not in got and ("glucose_cgm", d3) not in got
    assert got[("glucose", D2)] == (95.0, "test_meter", None)                 # the meter's day, the CGM never beside it
    assert not any(m == "glucose" for m, d in got if d != D2)                 # CGM-only days are no glucose days
    assert db.fetchall(conn, "SELECT n_samples FROM daily_values WHERE metric = 'glucose_cgm' ORDER BY date") == [(96,), (136,)]


def test_glucose_cgm_is_history_only_in_the_policy():
    p = MetricPolicy()
    assert p.priority("glucose_cgm") == ["test_cgm"] and p.derive("glucose_cgm") == {"from": "glucose", "devices": ["test_cgm"]}
    eff = p.effective("glucose_cgm")
    assert (eff["unit"], eff["trust"], eff["optional"], eff["direction"], eff["coverage"], eff["cadence_hours"]) == (
        "mg/dL", "trend_only", True, "band", {"slot_min": 15, "min_fraction": 0.7}, 2160.0)
    assert p.label("glucose_cgm") == "Glucose (CGM)" and p.daily("glucose_cgm") and p.agg("glucose_cgm") == "avg"
    assert "test_cgm" not in p.priority("glucose") and SourceRegistry().inactive >= {"test_cgm"}
    g = MetricPolicy(load_yaml("metric_policy.yaml", overlay=False))
    assert g.priority("glucose_cgm") == ["cgm"] and g.derive("glucose_cgm") == {"from": "glucose", "devices": ["cgm"]}


# ---- B15: the loader checks the policy against the registry ----

import importlib.util  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
from pathlib import Path  # noqa: E402

from heliosd.config import load_metric_policy  # noqa: E402
from heliosd.trust.policy import PolicyError  # noqa: E402
from heliosd.trust import schema  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _with_metric(**metrics) -> dict:
    cfg = load_metric_policy()
    for metric, keys in metrics.items():
        cfg["metrics"][metric] = {**cfg["metrics"].get(metric, {}), **keys}
    return cfg


def test_loader_rejects_unknown_device_key():
    """A typo in a device key used to drop that device silently (design B15;
    old: accepted). Every list is checked, with a suggestion."""
    reg = SourceRegistry()
    assert schema.validate_policy_against_registry(MetricPolicy(), reg) == []         # the fixture files agree
    assert schema.registry_problems(load_yaml("metric_policy.yaml", overlay=False),   # and the public defaults
                                    load_yaml("source_registry.yaml", overlay=False)) == []
    with pytest.raises(PolicyError) as e:
        schema.validate_policy_against_registry(MetricPolicy(_with_metric(resting_hr={"priority": ["whooop", "apple_watch_ultra"]})), reg)
    assert e.value.problems == ["metrics.resting_hr.priority: 'whooop' is not a device in the source registry; "
                                "did you mean 'whoop'?"]
    cfg = _with_metric(spo2={"corroboration": ["zepp_strap"]}, heart_rate={"exercise_priority": ["polar_h10"]},
                       steps={"sync_paths": {"iphon": ["bridge"]}},
                       glucose_cgm={"derive": {"from": "glucose", "devices": ["test_cgm", "cgm2"]}})
    got = schema.registry_problems(cfg, {"devices": reg.devices, "fallback_key": reg.fallback})       # plain dicts work too
    assert [p.split(":")[0] for p in got] == ["metrics.glucose_cgm.derive.devices", "metrics.heart_rate.exercise_priority",
                                              "metrics.spo2.corroboration", "metrics.steps.sync_paths"]


def test_loader_rejects_inactive_head():
    """A history-only device (active: false) never heads a metric that is
    not optional: a current day would wait on a device nobody wears."""
    reg = SourceRegistry()
    assert "test_cgm" in reg.inactive and MetricPolicy().priority("body_temp") == ["test_cgm"]   # optional: fine
    with pytest.raises(PolicyError) as e:
        schema.validate_policy_against_registry(MetricPolicy(_with_metric(glucose={"priority": ["test_cgm", "test_meter"]})), reg)
    assert e.value.problems == ["metrics.glucose.priority: its head 'test_cgm' is a history-only device (active: false); "
                                "a metric that is not optional needs a current device first"]


def test_loader_checks_copies_and_derived_metrics():
    reg = SourceRegistry()
    bad = _with_metric(heart_rate={"corroboration": ["whoop:healthkit"]},                      # no sync_paths for whoop
                       glucose_cgm={"derive": {"from": "sleep_analysis", "devices": ["test_cgm"]}},
                       body_temp={"derive": {"from": "glucose", "devices": ["test_cgm"]}},      # degC from mg/dL
                       vo2max={"derive": {"from": "heart_rate", "devices": ["zepp_helio"]}, "unit": "count/min"})
    assert schema.registry_problems(MetricPolicy(bad), reg) == [
        "metrics.body_temp.derive.from: 'glucose' is in 'mg/dL', this metric in 'degC'",
        "metrics.glucose_cgm.derive.from: 'sleep_analysis' is not a daily metric of this policy",
        "metrics.heart_rate.corroboration: 'whoop:healthkit' needs sync_paths for 'whoop' (the paths that are "
        "whoop's own; its rows from other paths are the healthkit copy)",
        "metrics.vo2max.derive.devices: 'zepp_helio' is in neither priority nor corroboration, so its rows are never read"]
    ok = _with_metric(respiratory_rate={"corroboration": ["whoop:healthkit"], "sync_paths": {"whoop": ["whoop_live"]}})
    assert schema.registry_problems(MetricPolicy(ok), reg) == []
    unknown = _with_metric(sleep_duration={"priority": ["whoop", "whop:healthkit"]})
    assert schema.registry_problems(MetricPolicy(unknown), reg) == [
        "metrics.sleep_duration.priority: 'whop:healthkit' names 'whop', which is not a device in the source registry; "
        "did you mean 'whoop'?",
        "metrics.sleep_duration.priority: 'whop:healthkit' needs sync_paths for 'whop' (the paths that are whop's own; "
        "its rows from other paths are the healthkit copy)"]


def _home(tmp_path, **metrics) -> Path:
    """A HELIOS_HOME holding the fixture registry and a policy overlay."""
    import yaml
    home = tmp_path / "home"
    (home / "data").mkdir(parents=True)
    shutil.copy(FIXTURES / "source_registry.yaml", home / "source_registry.yaml")
    overlay = yaml.safe_load((FIXTURES / "metric_policy.yaml").read_text(encoding="utf-8"))
    for metric, keys in metrics.items():
        overlay["metrics"][metric] = {**overlay["metrics"].get(metric, {}), **keys}
    overlay["sources"] = [{"key": "feed", "path": "~/private/feed-path.jsonl", "cadence_hours": 24}]
    (home / "metric_policy.yaml").write_text(yaml.safe_dump(overlay), encoding="utf-8")
    return home


def test_daemon_refuses_to_start_with_the_list(tmp_path, monkeypatch):
    """The startup check runs where the policy and the registry are loaded,
    before the store is opened (old: the daemon started and the device's
    rows were never arbitrated)."""
    from fastapi.testclient import TestClient
    from heliosd.config import Settings
    from heliosd.main import create_app
    home = _home(tmp_path, resting_hr={"priority": ["whooop", "apple_watch_ultra"]})
    monkeypatch.setenv("HELIOS_HOME", str(home))
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    db_path = home / "data" / "helios.duckdb"
    raw = {"server": {"ingest_token": "test-token-0123456789"}, "storage": {"db_path": str(db_path)},
           "notifications": {"macos_alerts": False}}
    with pytest.raises(PolicyError) as e:
        with TestClient(create_app(Settings(raw=raw))):
            pass
    assert "metrics.resting_hr.priority: 'whooop' is not a device in the source registry" in str(e.value)
    assert not db_path.exists()


def _check_policy_tool():
    spec = importlib.util.spec_from_file_location("check_policy", Path(__file__).resolve().parents[1] / "tools" / "check_policy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_check_policy_tool_prints_problems_never_values(tmp_path, monkeypatch, capsys):
    tool = _check_policy_tool()
    home = _home(tmp_path, spo2={"corroboration": ["whoop", "zepp_strap"]})
    monkeypatch.setenv("HELIOS_HOME", str(FIXTURES))          # restored after the test; the tool sets --home
    assert tool.main(["--home", str(home)]) == 1
    out = capsys.readouterr().out
    assert "PROBLEM metrics.spo2.corroboration: 'zepp_strap' is not a device in the source registry" in out
    assert "1 problem(s): heliosd would refuse to start" in out and "feed-path" not in out
    assert tool.main(["--home", str(FIXTURES)]) == 0
    assert capsys.readouterr().out.rstrip().endswith("metrics and 10 devices agree")
    broken = _home(tmp_path / "b", steps={"priorty": ["x"]})                     # invalid on its own
    assert tool.main(["--home", str(broken)]) == 1
    assert "Additional properties are not allowed ('priorty' was unexpected)" in capsys.readouterr().out


@pytest.mark.skipif(not os.environ.get("HELIOS_REAL_HOME"), reason="set HELIOS_REAL_HOME to check a real policy home")
def test_owner_files_validate(monkeypatch):
    """The owner's real files (or the staged overlay) agree; the rebuild tool
    runs the same check (read only: the files are only loaded)."""
    monkeypatch.setenv("HELIOS_HOME", os.environ["HELIOS_REAL_HOME"])
    assert schema.registry_problems(MetricPolicy(), SourceRegistry()) == []
