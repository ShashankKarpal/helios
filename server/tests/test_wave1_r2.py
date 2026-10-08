"""Wave 1 fork R2 (fix program 2026-10-08): the partial day (A4, owner decision
D7), the "waiting for Whoop" state (A5), fallback labels (A6), integer medians
(A21, T14) and the /api/today route additions. Every test here failed on the
branch point 440f625 (the failing line is named in each commit message).

Synthetic store only: an Apple Watch steps series, Apple stage rows for one
night, Apple resting HR, and Whoop API-shaped recovery rows written the way the
puller writes them. The reporting today is D; the clock is 06:40 Dubai."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from heliosd.config import Settings
from heliosd.ingest.bridge import ingest_batch
from heliosd.ingest.whoop import store_direct_sample
from heliosd.main import create_app
from heliosd.narrative import templates
from heliosd.narrative.brief import generate_brief
from heliosd.narrative.validator import validate_text
from heliosd.signals import recompute as rc
from heliosd.signals.markers import signals_for, verdict
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

AWU = "Owner’s Ultra 1"
D = date(2026, 10, 8)                      # the reporting today
DUBAI = ZoneInfo("Asia/Dubai")
NOW = datetime(2026, 10, 8, 6, 40, tzinfo=DUBAI)
SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
STAGE_HK = {"deep": "HKCategoryValueSleepAnalysisAsleepDeep", "rem": "HKCategoryValueSleepAnalysisAsleepREM",
            "core": "HKCategoryValueSleepAnalysisAsleepCore", "awake": "HKCategoryValueSleepAnalysisAwake"}


def _env():
    conn = db.connect_memory()
    policy = MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    return conn, policy, SourceRegistry()


def _q(hk, value, unit, start, end, uuid, source=AWU):
    return {"hk_type": hk, "value": value, "unit": unit, "start": start, "end": end,
            "source_name": source, "uuid": uuid}


def _seed_history(conn, policy, reg, days=30, end=D - timedelta(days=1)):
    """One Apple steps sample (4000 + 100 i) and one Apple resting HR sample per
    day for `days` complete days ending yesterday."""
    samples = []
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        samples.append(_q("HKQuantityTypeIdentifierStepCount", 4000 + 100 * i, "count",
                          f"{d}T06:00:00Z", f"{d}T06:05:00Z", f"st-{d}"))
        samples.append(_q("HKQuantityTypeIdentifierRestingHeartRate", 56 + (i % 5), "count/min",
                          f"{d}T03:00:00Z", f"{d}T03:00:00Z", f"rhr-{d}"))
    ingest_batch(conn, {"batch_id": "hist", "samples": samples}, policy, reg)


def _seed_today(conn, policy, reg, day=D, steps=True, rhr=True, apple_night=True):
    """The reporting today at 06:40: six small step samples before 06:00 Dubai
    (114 steps so far), an Apple resting HR, and an Apple night ending 06:20."""
    samples = []
    if steps:
        for k in range(6):
            t = datetime(day.year, day.month, day.day, 1, 30, tzinfo=DUBAI) + timedelta(minutes=5 * k)  # 05:30 Dubai is 01:30Z
            samples.append(_q("HKQuantityTypeIdentifierStepCount", 19, "count",
                              t.strftime("%Y-%m-%dT%H:%M:%SZ"), (t + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                              f"today-st-{k}"))
    if rhr:
        samples.append(_q("HKQuantityTypeIdentifierRestingHeartRate", 58, "count/min",
                          f"{day}T02:30:00Z", f"{day}T02:30:00Z", f"today-rhr"))
    if apple_night:
        t = datetime(day.year, day.month, day.day, 0, 30) - timedelta(hours=4)   # 00:30 Dubai as UTC wall
        for i, (stage, mins) in enumerate((("deep", 45), ("rem", 70), ("core", 220), ("awake", 15))):
            e = t + timedelta(minutes=mins)
            samples.append(_q(SLEEP, STAGE_HK[stage], "min", t.isoformat() + "Z", e.isoformat() + "Z", f"night-{i}"))
            t = e
    ingest_batch(conn, {"batch_id": f"today-{day}", "samples": samples}, policy, reg)


def _seed_whoop_recovery(conn, policy, days=31, end=D):
    """Whoop API-shaped recovery_score and hrv_rmssd rows, one per day, the way
    the puller stores them (record-keyed, SCORED), ending on `end`."""
    with db.transaction(conn) as c:
        for i in range(days):
            d = end - timedelta(days=days - 1 - i)
            at = datetime(d.year, d.month, d.day, 2, 0)        # 06:00 Dubai, naive UTC
            store_direct_sample(c, "recovery_score", f"recovery:{d}", 60 + (i % 7), "%", at, at, policy.zone)
            store_direct_sample(c, "hrv_rmssd", f"recovery:{d}", 48 + (i % 4), "ms", at, at, policy.zone)


def _recompute(conn, policy, reg, today=D, now=NOW, days=32):
    dates = {today - timedelta(days=i) for i in range(days)}
    return rc.recompute_dates(conn, policy, reg, dates, today=today, now=now)


def _signal(conn, day, metric, policy=None):
    rows = signals_for(conn, day, policy) if policy is not None else signals_for(conn, day)
    rows = [s for s in rows if s["metric"] == metric]
    return rows[0] if rows else None


def _grade(conn, day, metric):
    r = db.fetchall(conn, "SELECT confidence, grade FROM daily_values WHERE metric = ? AND date = ?", [metric, day])
    return r[0] if r else None


# ---------- A21 (T14): integer medians ----------

def test_why_prints_an_integer_median_with_a_thousands_separator_for_counts():
    conn, policy, reg = _env()
    _seed_history(conn, policy, reg)
    _recompute(conn, policy, reg)
    s = _signal(conn, D - timedelta(days=1), "steps")           # a closed day, judged
    assert s and s["state"] in ("favorable", "neutral", "flag")
    assert re.search(r"\(\d{1,3}(,\d{3})+\)$", s["why"]), s["why"]   # e.g. "(5,350)", never "5350.0"
    rhr = _signal(conn, D - timedelta(days=1), "resting_hr")
    assert rhr and re.search(r"\(\d+\)$", rhr["why"]), rhr["why"]
    text = templates.fallback_narrative(D - timedelta(days=1), "x", signals_for(conn, D - timedelta(days=1)))
    assert re.search(r"median \d{1,3}(,\d{3})+\.", text), text


# ---------- A6: fallback labels ----------

def test_a_non_owner_device_value_is_labelled_fallback_with_no_delta_and_no_flag():
    conn, policy, reg = _env()
    _seed_history(conn, policy, reg)
    _seed_today(conn, policy, reg)
    _recompute(conn, policy, reg)
    s = _signal(conn, D, "sleep_duration", policy)                   # Apple stages, no Whoop record
    assert s and s["device_key"] == "apple_watch_ultra"
    assert s["state"] == "fallback" and s["fallback"] is True and s["delta_pct"] is None
    assert s["owner_device"] == "whoop" and "standing in" in s["why"]
    rhr = _signal(conn, D, "resting_hr", policy)                     # the owner device: not a fallback
    assert rhr and rhr["fallback"] is False and rhr["state"] in ("favorable", "neutral", "flag")
    # Without a policy the boolean comes from the stored state.
    assert _signal(conn, D, "sleep_duration")["fallback"] is True
    text = templates.fallback_narrative(D, "v", signals_for(conn, D, policy))
    assert "standing in for Whoop" in text and "against a median" not in text


