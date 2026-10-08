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


# ---------- Codex A point 15: the slow path and an hours row with no baseline ----------

def test_slow_path_survives_an_hours_row_with_no_baseline():
    conn, policy, reg = _env()
    _seed_whoop_sleep(conn, policy, D)                 # one night only: no sleep baseline
    _seed_whoop_recovery(conn, policy, days=1)
    _recompute(conn, policy, reg, days=2)
    s = _stored(conn, D, "sleep_duration")
    assert s and s["state"] == "insufficient"
    lm = StubLM("A short note with no numbers.")
    brief = generate_brief(conn, lm, D, "Owner", force=True, allow_llm=True)   # TypeError on the old code
    assert lm.calls >= 1 and brief["narrative"]


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


# ---------- A4: the partial day (D7) ----------

def test_running_totals_on_the_reporting_today_are_in_progress_with_no_delta_no_grade():
    conn, policy, reg = _env()
    _seed_history(conn, policy, reg)
    _seed_today(conn, policy, reg)
    _recompute(conn, policy, reg)
    st = _stored(conn, D, "steps")
    assert st["state"] == "in_progress", st                      # the old code stored "flag", -97%, grade A
    assert st["delta_pct"] is None and st["grade"] is None and st["confidence"] is None and "so far" in st["why"]
    assert _grade(conn, D, "steps") == (None, None)
    # the baseline is exactly the 30 closed days before D
    b = db.fetchdicts(conn, "SELECT median, n_days FROM baselines WHERE metric = 'steps' AND date = ? "
                            "AND window_days = 30", [D])[0]
    assert b["n_days"] == 30 and b["median"] == 4000 + 100 * 14.5
    s = _signal(conn, D, "steps", policy, D)
    assert s["value"] == 114 and s["state"] == "in_progress" and s["baseline_median"] == b["median"]
    # Apple's day resting HR is rewritten until the next morning (Codex A point 4): so far, not judged
    rhr = _stored(conn, D, "resting_hr")
    assert rhr["state"] == "in_progress" and rhr["delta_pct"] is None and rhr["grade"] is None
    y = _signal(conn, D - timedelta(days=1), "steps", policy, D)
    assert y["state"] in JUDGED and y["grade"] in GRADES and y["delta_pct"] is not None


def test_a_closed_day_is_finalized_by_the_hourly_window_after_midnight():
    conn, policy, reg = _env()
    _seed_history(conn, policy, reg)
    _seed_today(conn, policy, reg)
    _recompute(conn, policy, reg)
    assert _grade(conn, D, "steps") == (None, None)
    rc.recompute_window(conn, policy, reg, days=3, now=datetime(2026, 10, 9, 0, 40, tzinfo=DUBAI))
    conf, grade = _grade(conn, D, "steps")
    assert grade in GRADES and conf is not None
    st = _stored(conn, D, "steps")
    assert st["state"] in JUDGED and st["delta_pct"] is not None and "so far" not in st["why"]


def test_the_next_days_first_drain_finalizes_the_closed_day():
    conn, policy, reg = _env()
    _seed_history(conn, policy, reg)
    _seed_today(conn, policy, reg)
    _recompute(conn, policy, reg)
    assert _grade(conn, D, "steps") == (None, None)
    # one batch for D+1 only (00:05 Dubai), drained at 00:10: the drain also finalizes D (Codex A point 2)
    ingest_batch(conn, {"batch_id": "next", "samples": [
        _q(STEPS, 10, "count", "2026-10-08T20:05:00Z", "2026-10-08T20:06:00Z", "next-1")]}, policy, reg)
    rc.drain_journal(conn, policy, reg, today=D + timedelta(days=1), now=datetime(2026, 10, 9, 0, 10, tzinfo=DUBAI))
    assert _grade(conn, D, "steps")[1] in GRADES and _stored(conn, D, "steps")["state"] in JUDGED


def test_a_long_downtime_still_finalizes_the_last_reporting_today():
    conn, policy, reg = _env()
    _seed_history(conn, policy, reg)
    _seed_today(conn, policy, reg)
    _recompute(conn, policy, reg)
    assert _grade(conn, D, "steps") == (None, None)
    # the daemon comes back ten days later: D is outside the three-day window
    rc.recompute_window(conn, policy, reg, days=3, now=datetime(2026, 10, 18, 0, 40, tzinfo=DUBAI))
    assert _grade(conn, D, "steps")[1] in GRADES and _stored(conn, D, "steps")["state"] in JUDGED
    assert db.fetchall(conn, "SELECT COUNT(*) FROM daily_values WHERE grade IS NULL AND date < ?",
                       [D + timedelta(days=10)])[0][0] == 0


def test_a_stale_row_is_presented_against_the_reporting_today():
    conn, policy, reg = _env()
    _seed_history(conn, policy, reg)
    _seed_today(conn, policy, reg)
    _recompute(conn, policy, reg)
    # a row written by the old code on the reporting today (judged) is shown "so far" at once (Codex A point 3)
    db.execute(conn, "UPDATE signals SET state = 'flag', delta_pct = -97.4, grade = 'A', confidence = 0.92 "
                     "WHERE date = ? AND metric = 'steps'", [D])
    s = _signal(conn, D, "steps", policy, D)
    assert s["state"] == "in_progress" and s["delta_pct"] is None and s["grade"] is None
    # after midnight, a row still marked in progress is judged from its stored baseline (Codex A point 2)
    db.execute(conn, "UPDATE signals SET state = 'in_progress', delta_pct = NULL WHERE date = ? AND metric = 'steps'", [D])
    s = _signal(conn, D, "steps", policy, D + timedelta(days=1))
    assert s["state"] in JUDGED and s["delta_pct"] is not None and "so far" not in s["why"]


def test_running_totals_never_drive_the_steps_action_or_a_judgement_in_the_template():
    conn, policy, reg = _env()
    _seed_history(conn, policy, reg)
    _seed_today(conn, policy, reg)
    _seed_whoop_recovery(conn, policy)
    _seed_whoop_sleep(conn, policy, D)
    _recompute(conn, policy, reg)
    assert _stored(conn, D, "steps")["state"] == "in_progress"
    sig = signals_for(conn, D, policy, D)
    acts = templates.rule_based_actions(sig, [])
    assert not any("Steps are behind" in a["text"] for a in acts), acts
    text = templates.fallback_narrative(D, verdict(sig), sig)
    assert "Steps so far today: 114 on Apple Watch Ultra." in text, text
    assert "resting heart rate so far today is 58 bpm" in text, text
    brief = generate_brief(conn, None, D, "Owner", allow_llm=False, policy=policy, today=D)
    assert not any("Steps are behind" in a["text"] for a in brief["actions"])


def test_the_llm_never_sees_a_running_total_and_cannot_mention_one():
    conn, policy, reg = _env()
    _seed_history(conn, policy, reg)
    _seed_today(conn, policy, reg)
    _seed_whoop_recovery(conn, policy)
    _seed_whoop_sleep(conn, policy, D)
    _recompute(conn, policy, reg)
    assert _stored(conn, D, "steps")["state"] == "in_progress"
    lm = StubLM("Your steps are low today.")                       # a judgement of a running total
    brief = generate_brief(conn, lm, D, "Owner", force=True, allow_llm=True, policy=policy, today=D)
    payload = _payload_of(lm.prompts[0])
    assert "steps" not in {r["metric"] for r in payload["signals"]}
    assert {"steps", "resting_hr"} <= set(payload["not_for_narrative"])
    assert not {"steps", "resting_hr"} & {r["metric"] for r in payload["signals"]}
    assert brief["validated"] is False and "steps are low" not in brief["narrative"].lower()


def test_validator_rejects_any_mention_of_a_metric_left_out_of_the_narrative():
    payload = {"date": str(D), "verdict": "x", "awaiting": [], "context_flags": [], "rule_actions": [],
               "not_for_narrative": ["steps", "heart_rate", "active_energy"],
               "signals": [{"metric": "resting_hr", "value": 58}, {"metric": "hrv_rmssd", "value": 49.92}]}
    assert validate_text("Your steps are below your median.", payload)
    assert validate_text("Steps are disappointing.", payload)                 # no judgement word needed
    assert any("steps" in e for e in validate_text("Steps total 58. That is a poor result.", payload))
    assert validate_text("Average heart rate was high.", payload)
    assert validate_text("You burned few calories.", payload)
    # legitimate sentences (Codex A point 13)
    assert validate_text("Resting heart rate is 58 bpm on Apple Watch Ultra.", payload) == []
    assert validate_text("Heart rate variability is 49.92 ms on Whoop.", payload) == []
    assert validate_text("Take an easy walk after lunch.", payload) == []
    # chat passes a list of tool outputs: the new checks do not apply (Codex A point 17)
    assert validate_text("Your steps are below your median.", [{"tool": "query_metric"}]) == []
    # a closed day is narrated freely
    assert validate_text("Your steps are below your median.", dict(payload, not_for_narrative=[])) == []


def test_today_route_lists_the_running_metrics(client):
    body = client.get("/api/today", headers=H).json()
    assert {"steps", "active_energy", "heart_rate", "resting_hr"} <= set(body["running_metrics"])
    assert not {"sleep_duration", "recovery_score", "hrv_rmssd"} & set(body["running_metrics"])


def test_running_total_classification_comes_from_the_policy():
    policy = MetricPolicy(default_tz="Asia/Dubai")           # fixture overlay over the public default
    running = {m for m in policy.metrics if policy.running_total(m)}
    assert running == {"steps", "active_energy", "basal_energy", "dietary_energy", "heart_rate",
                       "hrv_sdnn", "spo2", "glucose", "body_temp", "resting_hr"}
    for m in ("sleep_duration", "recovery_score", "strain", "sleep_need", "respiratory_rate",
              "hrv_rmssd", "wrist_temp", "body_mass", "vo2max", "bmi", "sleep_analysis"):
        assert not policy.running_total(m), m


def test_the_default_action_never_claims_steady_without_judged_evidence():
    assert templates.rule_based_actions([], []) == [
        {"text": "Nothing to act on yet. Check back once last night's data is in.", "category": "general"}]
    so_far = [{"metric": "steps", "state": "in_progress", "value": 114, "device_key": "apple_watch_ultra"}]
    assert "steady" not in templates.rule_based_actions(so_far, [])[0]["text"]


