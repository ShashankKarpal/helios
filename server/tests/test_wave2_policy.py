"""Wave 2 group C (design B10, B11, B15 and the owner-scope baselines): which
device's value a day takes and who only corroborates it, the steps merge, the
owner and device baselines, the CGM series and the loader's registry check.

Synthetic rows only (invented values and dates); expected values are worked
out by hand here, never taken from the code under test. The device lineup is
the fixture overlay (tests/fixtures), which mirrors the owner's lists of
decision 4h (fix program D11) with synthetic keys."""

from __future__ import annotations

import json
from datetime import date, datetime

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
    corroborates a day the Ultra has. It is not in resting HR or all-day HR
    (Q4 default), so it never fills those."""
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
    assert ("resting_hr", D0) not in got and ("heart_rate", D0) not in got


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
    "heart_rate": ["zepp_helio", "apple_watch_ultra", "whoop"],
    "resting_hr": ["whoop", "apple_watch_ultra", "zepp_helio"],
    "hrv_sdnn": ["apple_watch_ultra", "apple_watch_6_legacy"],
    "hrv_rmssd": ["whoop"],
    "respiratory_rate": ["whoop", "apple_watch_ultra", "apple_watch_6_legacy", "zepp_helio"],
    "spo2": ["apple_watch_ultra", "apple_watch_6_legacy"],
    "wrist_temp": ["apple_watch_ultra"],
    "steps": ["apple_watch_ultra", "apple_watch_6_legacy", "iphone"],
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
