"""Wave 1 fork R2 (fix program 2026-10-08): the partial day (A4, owner decision
D7), the "waiting for Whoop" state (A5), fallback labels (A6), integer medians
(A21, T14), the /api/today route additions and the Codex checkpoint A fixes
(adjudication-A, points 2, 4, 9, 11, 12, 13, 14, 15, 16, 19, 20). Every test
here failed on the commit before its fix (the failing line is named in each
commit message), and the first assertion of each behavioural test reads stored
rows, so it fails on the old judgement, not on a missing name.

Synthetic store only: an Apple Watch steps series, Apple stage rows for one
night, Apple resting HR, and Whoop API-shaped recovery and sleep rows written
the way the puller writes them. The reporting today is D; the clock is 06:40
Dubai; every call that needs "today" is given it explicitly."""

from __future__ import annotations

import json
import os
import re
import time
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
STEPS = "HKQuantityTypeIdentifierStepCount"
SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
STAGE_HK = {"deep": "HKCategoryValueSleepAnalysisAsleepDeep", "rem": "HKCategoryValueSleepAnalysisAsleepREM",
            "core": "HKCategoryValueSleepAnalysisAsleepCore", "awake": "HKCategoryValueSleepAnalysisAwake"}
JUDGED = ("favorable", "neutral", "flag")
GRADES = ("A", "B", "C", "D")
WAITING = "Waiting for Whoop's recovery for last night."
TOKEN = "test-token-0123456789"
SHELL = "<!doctype html>\n<html><head><title>Helios</title></head><body><div id=root></div></body></html>\n"


def _env():
    conn = db.connect_memory()
    policy = MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    return conn, policy, SourceRegistry()


def _q(hk, value, unit, start, end, uuid, source=AWU):
    return {"hk_type": hk, "value": value, "unit": unit, "start": start, "end": end,
            "source_name": source, "uuid": uuid}


def _seed_history(conn, policy, reg, days=30, end=D - timedelta(days=1), base=4000, step=100):
    """One Apple steps sample (base + step * i) and one Apple resting HR sample
    per day for `days` complete days ending `end`."""
    samples = []
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        samples.append(_q(STEPS, base + step * i, "count", f"{d}T06:00:00Z", f"{d}T06:05:00Z", f"st-{d}"))
        samples.append(_q("HKQuantityTypeIdentifierRestingHeartRate", 56 + (i % 5), "count/min",
                          f"{d}T03:00:00Z", f"{d}T03:00:00Z", f"rhr-{d}"))
    ingest_batch(conn, {"batch_id": f"hist-{end}", "samples": samples}, policy, reg)


def _seed_today(conn, policy, reg, day=D, steps=True, rhr=True, apple_night=True):
    """The reporting today at 06:40: six small step samples between 05:30 and
    06:00 Dubai (114 steps so far), an interim Apple resting HR, and an Apple
    night ending 06:20."""
    samples = []
    if steps:
        for k in range(6):
            t = datetime(day.year, day.month, day.day, 1, 30) + timedelta(minutes=5 * k)   # 05:30 Dubai = 01:30Z
            samples.append(_q(STEPS, 19, "count", t.isoformat() + "Z", (t + timedelta(minutes=3)).isoformat() + "Z",
                              f"today-st-{day}-{k}"))
    if rhr:
        samples.append(_q("HKQuantityTypeIdentifierRestingHeartRate", 58, "count/min",
                          f"{day}T02:30:00Z", f"{day}T02:30:00Z", f"today-rhr-{day}"))
    if apple_night:
        t = datetime(day.year, day.month, day.day, 0, 30) - timedelta(hours=4)   # 00:30 Dubai as UTC
        for i, (stage, mins) in enumerate((("deep", 45), ("rem", 70), ("core", 220), ("awake", 15))):
            e = t + timedelta(minutes=mins)
            samples.append(_q(SLEEP, STAGE_HK[stage], "min", t.isoformat() + "Z", e.isoformat() + "Z", f"night-{day}-{i}"))
            t = e
    ingest_batch(conn, {"batch_id": f"today-{day}", "samples": samples}, policy, reg)


def _seed_whoop_recovery(conn, policy, days=31, end=D):
    """Whoop API-shaped recovery_score and hrv_rmssd rows, one per day ending
    `end`, stored the way the puller stores them (record-keyed, SCORED)."""
    with db.transaction(conn) as c:
        for i in range(days):
            d = end - timedelta(days=days - 1 - i)
            at = datetime(d.year, d.month, d.day, 2, 0)        # 06:00 Dubai, naive UTC
            store_direct_sample(c, "recovery_score", f"recovery:{d}", 60 + (i % 7), "%", at, at, policy.zone)
            store_direct_sample(c, "hrv_rmssd", f"recovery:{d}", 48 + (i % 4), "ms", at, at, policy.zone)


def _seed_whoop_sleep(conn, policy, day=D):
    """Whoop's API sleep record for the night ending `day` (00:20 to 06:30 Dubai)."""
    with db.transaction(conn) as c:
        store_direct_sample(c, "sleep_duration", f"sleep:{day}", 6.5, "h",
                            datetime(day.year, day.month, day.day, 0, 20) - timedelta(hours=4),
                            datetime(day.year, day.month, day.day, 6, 30) - timedelta(hours=4), policy.zone)


def _recompute(conn, policy, reg, today=D, now=NOW, days=32):
    dates = {today - timedelta(days=i) for i in range(days)}
    return rc.recompute_dates(conn, policy, reg, dates, today=today, now=now)


def _signal(conn, day, metric, policy=None, today=None):
    kw = {}
    if policy is not None:
        kw["policy"] = policy
    if today is not None:
        kw["today"] = today
    rows = [s for s in signals_for(conn, day, **kw) if s["metric"] == metric]
    return rows[0] if rows else None


def _stored(conn, day, metric):
    r = db.fetchdicts(conn, "SELECT state, delta_pct, grade, confidence, why FROM signals WHERE metric = ? AND date = ?",
                      [metric, day])
    return r[0] if r else None


def _grade(conn, day, metric):
    r = db.fetchall(conn, "SELECT confidence, grade FROM daily_values WHERE metric = ? AND date = ?", [metric, day])
    return r[0] if r else None


class StubLM:
    """A local model stand-in: always available, returns a fixed narrative and
    records every prompt it was sent."""
    primary, fallback = "stub-primary", "stub-fallback"

    def __init__(self, narrative="A calm summary.", actions=None):
        self.narrative = narrative
        self.actions = actions if actions is not None else [{"text": "Keep the day easy.", "category": "general"}]
        self.calls, self.prompts = 0, []

    def available(self):
        return True

    def structured(self, messages, schema, temperature=0.0, model=None):
        self.calls += 1
        self.prompts.append(messages[-1]["content"])
        return {"narrative": self.narrative, "actions": self.actions, "flags": []}


def _payload_of(prompt: str) -> dict:
    return json.loads(prompt.split("DATA:\n", 1)[1].split("\n\nVALIDATION", 1)[0])


@pytest.fixture()
def client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(SHELL, encoding="utf-8")
    monkeypatch.setenv("HELIOS_WEB_DIST", str(dist))
    raw = {"server": {"ingest_token": TOKEN}, "owner": {"timezone": "Asia/Dubai"},
           "storage": {"db_path": str(tmp_path / "helios.duckdb")},
           "notifications": {"macos_alerts": False}}
    with TestClient(create_app(Settings(raw=raw))) as c:
        yield c


H = {"X-Helios-Token": TOKEN}


# ---------- A21 (T14): integer medians ----------

def test_why_prints_an_integer_median_with_a_thousands_separator_for_counts():
    conn, policy, reg = _env()
    _seed_history(conn, policy, reg)
    _recompute(conn, policy, reg)
    s = _signal(conn, D - timedelta(days=1), "steps")           # a closed day, judged
    assert s and s["state"] in JUDGED
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
    rhr = _signal(conn, D - timedelta(days=1), "resting_hr", policy)  # the owner device on a closed day
    assert rhr and rhr["fallback"] is False and rhr["state"] in JUDGED
    text = templates.fallback_narrative(D, "v", signals_for(conn, D, policy))
    assert "standing in for Whoop" in text and "against a median" not in text


# ---------- /api/today route additions ----------

def test_today_route_reports_zone_and_an_offset_aware_as_of(client):
    assert client.post("/ingest", json={"samples": [], "sync_path": "bridge", "batch_id": "b1"}, headers=H).status_code == 200
    body = client.get("/api/today", headers=H).json()
    assert body["zone"] == "Asia/Dubai"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+04:00", body["as_of"]), body["as_of"]


# ---------- Codex A point 16: provenance independent of the state ----------

def test_fallback_is_unknown_without_a_policy_and_a_stale_judged_stand_in_is_presented_as_fallback():
    conn, policy, reg = _env()
    _seed_history(conn, policy, reg)
    _seed_today(conn, policy, reg)
    _recompute(conn, policy, reg)
    raw = _signal(conn, D, "sleep_duration")           # no policy: provenance unknown, never guessed from the state
    assert raw["fallback"] is None and raw["owner_device"] is None
    # A judged row from a non-owner device (written before a policy change) is presented as a fallback.
    db.execute(conn, "UPDATE signals SET state = 'flag', delta_pct = -20.0, why = 'under 7h' "
                     "WHERE date = ? AND metric = 'sleep_duration'", [D])
    s = _signal(conn, D, "sleep_duration", policy)
    assert s["state"] == "fallback" and s["delta_pct"] is None and s["fallback"] is True


