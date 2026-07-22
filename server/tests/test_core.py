"""Core test suite: registry, ingest, arbitration, baselines, signals,
watchdog, validator. Runs entirely on synthetic fixtures."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from heliosd.ingest.bridge import ingest_batch
from heliosd.narrative.templates import rule_based_actions
from heliosd.narrative.validator import validate_text
from heliosd.signals import watchdog
from heliosd.signals.baselines import compute_baselines, compute_daily_values, get_baseline
from heliosd.signals.markers import compute_signals, signals_for, verdict
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry
from tests.synth import synth_batch

END = date(2026, 7, 15)


@pytest.fixture(scope="module")
def env():
    conn = db.connect_memory()
    policy = MetricPolicy()
    registry = SourceRegistry()
    result = ingest_batch(conn, synth_batch(days=45, end_day=END), policy, registry)
    assert result["ack"] and result["accepted"] > 1000
    compute_daily_values(conn, policy, registry, END - timedelta(days=44), END,
                         now=datetime.combine(END, datetime.min.time()) + timedelta(hours=9))
    for back in range(0, 8):
        compute_baselines(conn, policy, END - timedelta(days=back))
        compute_signals(conn, policy, END - timedelta(days=back))
    return conn, policy, registry


def test_registry_curly_apostrophe():
    r = SourceRegistry()
    assert r.resolve("Shashank’s Ultra 1") == "apple_watch_ultra"
    assert r.resolve("Shashank's 16 Pro Max") == "iphone"
    assert r.resolve("WHOOP") == "whoop"
    assert r.resolve("Zepp Life") == "zepp_life_scale"
    assert r.resolve("Zepp") == "zepp_helio"
    assert r.resolve("Athlytic") is None            # ignored
    assert r.resolve("Random App") == "other"


def test_ingest_dedupe_and_ignore(env):
    conn, policy, registry = env
    # Re-ingesting the identical batch must add zero new rows (content-hash dedupe).
    before = db.fetchall(conn, "SELECT COUNT(*) FROM samples")[0][0]
    ingest_batch(conn, synth_batch(days=45, end_day=END), policy, registry)
    after = db.fetchall(conn, "SELECT COUNT(*) FROM samples")[0][0]
    assert after == before
    # Ignored sources never land.
    n = db.fetchall(conn, "SELECT COUNT(*) FROM samples WHERE source_name = 'Athlytic'")[0][0]
    assert n == 0


def test_trust_priority_no_blending(env):
    conn, policy, registry = env
    rows = db.fetchdicts(conn,
        "SELECT device_key, corroboration FROM daily_values WHERE metric='steps' AND date=?", [END])
    assert rows and rows[0]["device_key"] == "iphone"      # iPhone outranks the Watch
    assert rows[0]["corroboration"] is not None            # Watch kept as corroboration, not averaged
    rhr = db.fetchdicts(conn,
        "SELECT device_key FROM daily_values WHERE metric='resting_hr' AND date=?", [END])
    assert rhr and rhr[0]["device_key"] == "apple_watch_ultra"


def test_sleep_duration_from_stages(env):
    conn, _, _ = env
    rows = db.fetchdicts(conn,
        "SELECT value, device_key FROM daily_values WHERE metric='sleep_duration' AND date=?", [END])
    assert rows and rows[0]["device_key"] == "whoop"       # Whoop owns sleep
    assert 4.0 < rows[0]["value"] < 10.0


def test_spo2_percent_scale(env):
    conn, _, _ = env
    v = db.fetchdicts(conn,
        "SELECT value FROM daily_values WHERE metric='spo2' AND date=?", [END])
    assert v and 90 < v[0]["value"] <= 100                 # fraction converted to percent


def test_baselines_median_mad(env):
    conn, policy, _ = env
    b = get_baseline(conn, "resting_hr", END, policy.default_window)
    assert b and 50 < b["median"] < 64 and b["mad"] >= 0 and b["n_days"] >= policy.min_days


def test_signals_states_and_verdict(env):
    conn, _, _ = env
    sig = signals_for(conn, END)
    assert sig, "signals must exist"
    states = {s["state"] for s in sig}
    assert states <= {"favorable", "neutral", "flag", "insufficient"}
    assert isinstance(verdict(sig), str) and len(verdict(sig)) > 10
    for s in sig:
        assert s["device_key"] and s["grade"] in ("A", "B", "C", "D")


def test_hrv_series_never_blended(env):
    conn, _, _ = env
    # rMSSD (whoop) and SDNN (apple) must be separate metrics end to end.
    sdnn = db.fetchdicts(conn,
        "SELECT DISTINCT device_key FROM daily_values WHERE metric='hrv_sdnn'")
    assert all(r["device_key"] != "whoop" for r in sdnn)


def test_watchdog_detects_silent_stream(env):
    conn, policy, _ = env
    later = datetime.combine(END + timedelta(days=9), datetime.min.time())
    report = watchdog.check(conn, policy, now=later)
    assert any(r["status"] in ("stale", "silent") for r in report)
    zepp = [r for r in report if r["device_key"] == "zepp_helio"]
    assert zepp and "Zepp" in zepp[0]["fix"]


def test_validator_blocks_invented_numbers():
    payload = {"signals": [{"metric": "resting_hr", "value": 57.0}]}
    assert validate_text("Your resting HR was 57.", payload) == []
    assert validate_text("Your resting HR was 44.", payload) != []
    assert any("blocked" in e for e in
               validate_text("This diagnosis suggests 57 problems.", payload))


def test_rule_actions_bounded(env):
    conn, _, _ = env
    sig = signals_for(conn, END)
    acts = rule_based_actions(sig, ["heat"])
    assert 1 <= len(acts) <= 3 and all(a["text"] and a["category"] for a in acts)


def test_deletion_by_uuid(env):
    conn, policy, registry = env
    target = db.fetchall(conn,
        "SELECT hk_uuid FROM samples WHERE hk_uuid IS NOT NULL LIMIT 1")[0][0]
    ingest_batch(conn, {"batch_id": "del-1", "samples": [], "deleted": [target]},
                 policy, registry)
    n = db.fetchall(conn, "SELECT COUNT(*) FROM samples WHERE hk_uuid = ?", [target])[0][0]
    assert n == 0
