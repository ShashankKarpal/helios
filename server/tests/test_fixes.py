"""Regression tests for the audit fixes: action dedupe, narrative reuse,
backfill-scale recompute, watchdog noise filters, Whoop v2."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from heliosd.ingest.bridge import ingest_batch
from heliosd.ingest import whoop
from heliosd.narrative.brief import generate_brief
from heliosd.signals import watchdog
from heliosd.signals.baselines import compute_baselines, compute_daily_values
from heliosd.signals.markers import compute_signals
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
    ingest_batch(conn, synth_batch(days=45, end_day=END), policy, registry)
    compute_daily_values(conn, policy, registry, END - timedelta(days=44), END)
    for back in range(0, 3):
        compute_baselines(conn, policy, END - timedelta(days=back))
        compute_signals(conn, policy, END - timedelta(days=back))
    return conn, policy, registry


def test_brief_actions_never_duplicate(env):
    conn, _, _ = env
    for _ in range(4):  # simulate opening the app four times
        generate_brief(conn, None, END, "Owner")
    n = db.fetchall(conn,
        "SELECT COUNT(*) FROM actions WHERE date = ? AND status = 'suggested'", [END])[0][0]
    briefs = generate_brief(conn, None, END, "Owner")
    assert n == len(briefs["actions"]) and n <= 3


def test_brief_reuses_stored_narrative(env):
    conn, _, _ = env
    first = generate_brief(conn, None, END, "Owner")
    n_narr = db.fetchall(conn, "SELECT COUNT(*) FROM narratives WHERE date = ?", [END])[0][0]
    second = generate_brief(conn, None, END, "Owner")
    assert n_narr == 1 and second["narrative"] == first["narrative"]
    # force regenerates
    third = generate_brief(conn, None, END, "Owner", force=True)
    assert isinstance(third["narrative"], str) and third["narrative"]


def test_adopted_actions_survive_regeneration(env):
    conn, _, _ = env
    generate_brief(conn, None, END, "Owner")
    row = db.fetchall(conn,
        "SELECT action_id FROM actions WHERE date = ? LIMIT 1", [END])[0][0]
    db.execute(conn, "UPDATE actions SET status = 'adopted' WHERE action_id = ?", [row])
    generate_brief(conn, None, END, "Owner", force=True)
    status = db.fetchall(conn,
        "SELECT status FROM actions WHERE action_id = ?", [row])[0][0]
    assert status == "adopted"


def test_wide_window_recompute_is_setbased_and_correct(env):
    conn, policy, registry = env
    # Rebuild the full window in one pass; must complete quickly and match spot values.
    n = compute_daily_values(conn, policy, registry, END - timedelta(days=44), END)
    assert n > 200  # many metric-days in one call
    row = db.fetchdicts(conn,
        "SELECT device_key FROM daily_values WHERE metric='steps' AND date=?", [END])
    # Watch is the steps primary per the wearable audit.
    assert row and row[0]["device_key"] == "apple_watch_ultra"


def test_watchdog_ignores_inactive_and_nonpriority(env):
    conn, policy, registry = env
    later = datetime.combine(END + timedelta(days=9), datetime.min.time())
    report = watchdog.check(conn, policy, now=later, registry=registry)
    keys = {(r["metric"], r["device_key"]) for r in report}
    # Retired devices never alarm.
    assert all(dk not in ("redacted_cgm", "apple_watch_6_legacy") for _, dk in keys)
    # Priority streams that really went quiet still do.
    assert any(dk == "zepp_helio" for _, dk in keys)


def test_whoop_targets_v2():
    assert "/developer/v2" in whoop.API
    assert "v1" not in whoop.API


def test_ingest_reports_date_span(env):
    conn, policy, registry = env
    res = ingest_batch(conn, synth_batch(days=5, seed=99, end_day=END), policy, registry)
    assert res["date_min"] is not None and res["date_max"] is not None
    span = (date.fromisoformat(res["date_max"]) - date.fromisoformat(res["date_min"])).days
    assert 3 <= span <= 6
