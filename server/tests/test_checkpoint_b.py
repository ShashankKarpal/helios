"""Checkpoint B (Codex diff review of the complete Phase 1a diff, 2026-10-05)
plus the dry run's own finding: every accepted point has a test that fails on
the old behaviour and passes on the fix. Oracles are inputs written here,
never read back from the code under test."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import duckdb
import pytest
from fastapi import HTTPException

from heliosd import main
from heliosd.config import load_metric_policy
from heliosd.ingest import whoop
from heliosd.ingest.bridge import ingest_batch
from heliosd.ingest.normalize import normalize_sample
from heliosd.narrative import brief as brief_mod
from heliosd.signals import baselines as bl, recompute as rc
from heliosd.signals.markers import compute_signals, signals_for
from heliosd.signals.sleep_stages import nightly_stages
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy, PolicyError
from tests.test_single_source import _env, _steps, AWU
from tests.test_whoop_records import FakeClient, NOW, recovery_rec, sleep_rec

D0 = date(2026, 6, 1)
STEPS = "HKQuantityTypeIdentifierStepCount"
RHR = "HKQuantityTypeIdentifierRestingHeartRate"
SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"


def _one(conn, sql, params=None):
    return db.fetchall(conn, sql, params)[0][0]


def _q(hk, uuid, day, hour, value, source=AWU, unit="count", **extra):
    t = f"{day}T{hour:02d}:00:00Z"
    return {"hk_type": hk, "value": value, "unit": unit, "start": t, "end": t, "source_name": source, "uuid": uuid, **extra}


# ---- 1. the journal row an ingest replaced during the pass survives the drain ----

def test_journal_row_replaced_during_the_pass_survives_the_drain(monkeypatch):
    conn, policy, reg = _env()
    ingest_batch(conn, {"batch_id": "b1", "samples": [_q(STEPS, "s1", D0, 6, 100)]}, policy, reg)
    assert _one(conn, "SELECT COUNT(*) FROM dirty_dates") == 1
    real = rc.recompute_dates

    def with_late_ingest(c, p, r, dates, today=None, now=None):
        out = real(c, p, r, dates, today=today, now=now)
        # a batch for the SAME date lands while the pass is still running
        ingest_batch(c, {"batch_id": "b2", "samples": [_q(STEPS, "s2", D0, 8, 200)]}, p, r)
        return out
    monkeypatch.setattr(rc, "recompute_dates", with_late_ingest)
    out = rc.drain_journal(conn, policy, reg, today=D0 + timedelta(days=1))
    assert out["journal_rows"] == 1
    # the replaced row is still there and the next drain picks the new sample up
    assert _one(conn, "SELECT COUNT(*) FROM dirty_dates WHERE date = ?", [D0]) == 1
    monkeypatch.setattr(rc, "recompute_dates", real)
    assert rc.drain_journal(conn, policy, reg, today=D0 + timedelta(days=1))["journal_rows"] == 1
    assert _one(conn, "SELECT value FROM daily_values WHERE metric = 'steps' AND date = ?", [D0]) == 300
    assert _one(conn, "SELECT COUNT(*) FROM dirty_dates") == 0


# ---- 2. recompute passes never overlap ----

def test_recompute_passes_are_serialized(monkeypatch):
    conn, policy, reg = _env()
    ingest_batch(conn, {"batch_id": "b1", "samples": [_q(STEPS, "s1", D0, 6, 100)]}, policy, reg)
    inside, overlaps = [0], [0]
    real = bl.compute_daily_values

    def slow(*a, **k):
        inside[0] += 1
        if inside[0] > 1:
            overlaps[0] += 1
        time.sleep(0.2)
        try:
            return real(*a, **k)
        finally:
            inside[0] -= 1
    monkeypatch.setattr(rc, "compute_daily_values", slow)
    ts = [threading.Thread(target=rc.recompute_window, args=(conn, policy, reg, 1, 3, datetime(2026, 6, 2, 8, tzinfo=timezone.utc))) for _ in range(3)]
    ts.append(threading.Thread(target=rc.drain_journal, args=(conn, policy, reg, D0 + timedelta(days=1))))
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert overlaps[0] == 0


# ---- 3. shutdown is bounded even when a worker holds the store lock ----

def test_shutdown_is_bounded_when_a_worker_holds_the_lock(tmp_path):
    path = tmp_path / "helios.duckdb"

    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(conn=db.connect(path), workers=set(), stopping=False))

        def hog():
            with db._lock:                       # Python code holding the lock, not a DuckDB statement
                time.sleep(1.5)

        worker = asyncio.create_task(main.run_worker(app, hog))
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        out = await main.shutdown_store(app, tasks=[], grace=0.2, close_budget=0.3)
        elapsed = time.monotonic() - t0
        with contextlib.suppress(Exception):
            await worker
        return out, elapsed

    out, elapsed = asyncio.run(scenario())
    assert out["drained"] is False and out["interrupted"] is True
    assert out["checkpointed"] is False and out["closed"] is False     # skipped, not hung
    assert elapsed < 1.0                                              # grace 0.2 + budget 0.3 + slack


def test_shutdown_interrupts_a_long_statement_and_still_closes(tmp_path):
    path = tmp_path / "helios.duckdb"

    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(conn=db.connect(path), workers=set(), stopping=False))
        errors = []

        def long_query():
            try:
                db.fetchall(app.state.conn, "SELECT COUNT(*) FROM range(3000000000) r1, range(3) r2")
            except Exception as e:  # noqa: BLE001
                errors.append(type(e).__name__)

        worker = asyncio.create_task(main.run_worker(app, long_query))
        await asyncio.sleep(0.3)
        t0 = time.monotonic()
        out = await main.shutdown_store(app, tasks=[], grace=0.2, close_budget=1.5)
        elapsed = time.monotonic() - t0
        with contextlib.suppress(Exception):
            await worker
        return out, elapsed, errors

    out, elapsed, errors = asyncio.run(scenario())
    assert out["interrupted"] is True and out["closed"] is True and elapsed < 2.5
    assert errors and errors[0] != "TimeoutError"                      # the statement was interrupted


# ---- 4. no new store work once the shutdown has begun ----

def test_run_worker_refuses_after_stopping():
    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(workers=set(), stopping=True))
        with pytest.raises(HTTPException) as e:
            await main.run_worker(app, lambda: 1)
        return e.value.status_code
    assert asyncio.run(scenario()) == 503


# ---- 5. Whoop revisions: older payloads lose, identical payloads are no-ops ----

def test_older_whoop_revision_is_ignored_and_identical_replay_is_a_noop(tmp_path):
    conn, policy, reg = _env()
    s, e = "2026-07-09T19:30:00.000Z", "2026-07-10T02:40:00.000Z"
    scored = sleep_rec("s1", s, e, updated="2026-07-10T04:00:00.000Z")
    unscorable = sleep_rec("s1", s, e, state="UNSCORABLE", updated="2026-07-10T04:30:00.000Z")
    whoop.pull(conn, FakeClient(tmp_path, sleep=[scored]), policy, now=NOW)
    out = whoop.pull(conn, FakeClient(tmp_path, sleep=[unscorable]), policy, now=NOW)
    assert out["retracted"] == 3 and _one(conn, "SELECT COUNT(*) FROM samples") == 0
    rc.drain_journal(conn, policy, reg, today=date(2026, 7, 10))
    # the OLDER scored payload arrives again (a stale page): nothing comes back
    out = whoop.pull(conn, FakeClient(tmp_path, sleep=[scored]), policy, now=NOW)
    assert out["older"] == 1 and out["sleep"] == 0 and out["dates"] == []
    assert _one(conn, "SELECT COUNT(*) FROM samples") == 0 and _one(conn, "SELECT COUNT(*) FROM tombstones") == 3
    assert _one(conn, "SELECT score_state FROM whoop_records") == "UNSCORABLE"
    # an identical replay of the current revision rewrites nothing and dirties nothing
    before = db.fetchall(conn, "SELECT * FROM whoop_records")
    out = whoop.pull(conn, FakeClient(tmp_path, sleep=[unscorable]), policy, now=NOW)
    assert out["unchanged"] == 1 and out["dates"] == []
    assert db.fetchall(conn, "SELECT * FROM whoop_records") == before
    assert _one(conn, "SELECT COUNT(*) FROM dirty_dates") == 0


# ---- 6. the cache follows a record that moves; cached stages respect eligibility ----

def test_cache_drops_the_old_slot_when_a_sleep_becomes_a_nap_or_moves(tmp_path):
    conn, policy, reg = _env()
    s, e = "2026-07-09T19:30:00.000Z", "2026-07-10T02:40:00.000Z"
    whoop.pull(conn, FakeClient(tmp_path, sleep=[sleep_rec("s1", s, e)]), policy, now=NOW)
    assert db.fetchall(conn, "SELECT date, kind FROM whoop_cache") == [(date(2026, 7, 10), "sleep")]
    # revised: the record is now a nap
    out = whoop.pull(conn, FakeClient(tmp_path, sleep=[sleep_rec("s1", s, e, nap=True, updated="2026-07-10T05:00:00.000Z")]), policy, now=NOW)
    # the record's own transaction already moved its slot (checkpoint C, 19); the window sweep finds nothing left
    assert out["cache_removed"] == 0
    assert db.fetchall(conn, "SELECT date, kind FROM whoop_cache") == [(date(2026, 7, 10), "sleep_nap")]
    assert nightly_stages(conn, policy, date(2026, 7, 10), date(2026, 7, 10)) == {}      # a nap gives no night
    # revised again: a real sleep whose end moved to the next reporting day
    whoop.pull(conn, FakeClient(tmp_path, sleep=[sleep_rec("s1", "2026-07-10T19:30:00.000Z", "2026-07-11T02:40:00.000Z", updated="2026-07-11T05:00:00.000Z")]),
               policy, now=datetime(2026, 7, 11, 10, 0, tzinfo=NOW.tzinfo))
    assert db.fetchall(conn, "SELECT date, kind FROM whoop_cache ORDER BY 1") == [(date(2026, 7, 11), "sleep")]
    # a cache row whose record is unknown (pre-1a pull) is left alone
    db.execute(conn, "INSERT INTO whoop_cache (date, kind, payload) VALUES ('2026-07-08', 'sleep', '{\"id\": \"legacy\", \"score\": {\"stage_summary\": {\"total_rem_sleep_time_milli\": 3600000}}}')")
    whoop.pull(conn, FakeClient(tmp_path, sleep=[]), policy, now=datetime(2026, 7, 11, 10, 0, tzinfo=NOW.tzinfo))
    assert _one(conn, "SELECT COUNT(*) FROM whoop_cache WHERE date = DATE '2026-07-08'") == 1


def test_cached_stages_ignore_unscored_payloads(tmp_path):
    conn, policy, reg = _env()
    s, e = "2026-07-09T19:30:00.000Z", "2026-07-10T02:40:00.000Z"
    whoop.pull(conn, FakeClient(tmp_path, sleep=[sleep_rec("s1", s, e, state="PENDING_SCORE")]), policy, now=NOW)
    assert db.fetchall(conn, "SELECT kind FROM whoop_cache") == [("sleep",)]
    assert nightly_stages(conn, policy, date(2026, 7, 10), date(2026, 7, 10)) == {}


# ---- 7. replacing a legacy Whoop day row journals the row's own dates ----

def test_legacy_row_replacement_dirties_the_removed_rows_own_dates(tmp_path):
    conn, policy, reg = _env()
    # legacy respiratory_rate row filed under 07-10 by id, but it lies on 07-09 (21:00 to 23:50 Dubai), so its
    # value files on 07-09 on the start-date basis and on the wake-date basis alike (Wave 2 B6: sleep_end)
    db.execute(conn, "INSERT INTO samples (sample_id, metric, value, unit, start_ts, end_ts, source_name, device_key, sync_path) "
                     "VALUES ('wh:respiratory_rate:2026-07-10', 'respiratory_rate', 14.0, 'count/min', '2026-07-09 21:00', '2026-07-09 23:50', 'WHOOP', 'whoop', 'whoop_live')")
    rc.recompute_dates(conn, policy, reg, {date(2026, 7, 9)}, today=date(2026, 7, 10))
    assert _one(conn, "SELECT value FROM daily_values WHERE metric = 'respiratory_rate' AND date = DATE '2026-07-09'") == 14.0
    # the record starts after midnight: the new row files under 07-10
    out = whoop.pull(conn, FakeClient(tmp_path, sleep=[sleep_rec("s1", "2026-07-09T20:30:00.000Z", "2026-07-10T02:40:00.000Z", rr=15.1)]), policy, now=NOW)
    assert out["replaced_legacy"] == 1 and "2026-07-09" in out["dates"]
    rc.drain_journal(conn, policy, reg, today=date(2026, 7, 10))
    assert db.fetchall(conn, "SELECT date, value FROM daily_values WHERE metric = 'respiratory_rate' ORDER BY 1") == [(date(2026, 7, 10), 15.1)]


# ---- 8. narrative generations: stale cache never served, publish refused when inputs moved ----

def _brief_env():
    conn, policy, reg = _env()
    samples = [_q(RHR, f"r-{i}", D0 + timedelta(days=i), 3, 55, unit="count/min") for i in range(8)]
    ingest_batch(conn, {"batch_id": "b", "samples": samples}, policy, reg)
    # Whoop's recovery for each night, so the brief may ask the model at all:
    # while recovery is awaited the narrative is the deterministic template
    # (fix program A5).
    from heliosd.ingest.whoop import store_direct_sample
    with db.transaction(conn) as c:
        for i in range(8):
            at = datetime.combine(D0 + timedelta(days=i), datetime.min.time()) + timedelta(hours=2)
            store_direct_sample(c, "recovery_score", f"recovery:{i}", 60, "%", at, at, policy.zone)
    rc.drain_journal(conn, policy, reg, today=D0 + timedelta(days=7))
    return conn, policy, reg, D0 + timedelta(days=7)


def test_stale_cached_narrative_is_not_served():
    conn, policy, reg, day = _brief_env()
    gen = rc.generation_of(conn, day)
    db.execute(conn, "INSERT INTO narratives (date, narrative, model, validated, generation) VALUES (?, 'OLD TEXT', 'm', TRUE, ?)", [day, gen])
    assert brief_mod.generate_brief(conn, None, day, "Owner", allow_llm=False)["narrative"] == "OLD TEXT"   # current: served
    with db.transaction(conn) as c:
        rc.invalidate_derived(c, {day})          # generation moves; the delete happened...
    db.execute(conn, "INSERT INTO narratives (date, narrative, model, validated, generation) VALUES (?, 'OLD TEXT', 'm', TRUE, ?)", [day, gen])  # ...but a racing writer re-cached the old generation
    out = brief_mod.generate_brief(conn, None, day, "Owner", allow_llm=False)
    assert out["narrative"] != "OLD TEXT" and out["narrative_status"] == "template"
    assert _one(conn, "SELECT generation FROM narratives WHERE date = ?", [day]) == gen + 1 == rc.generation_of(conn, day)


def test_publish_is_refused_when_the_generation_moves_during_generation():
    conn, policy, reg, day = _brief_env()

    class LM:
        primary = fallback = "m"

        def available(self):
            return True

        def structured(self, *a, **k):
            with db.transaction(conn) as c:
                rc.invalidate_derived(c, {day})   # a recompute lands while the model is thinking
            return {"narrative": "fresh words without numbers", "actions": []}

    gen = rc.generation_of(conn, day)
    out = brief_mod.generate_brief(conn, LM(), day, "Owner", force=True, allow_llm=True)
    assert out["narrative_status"] == "generating"
    assert _one(conn, "SELECT COUNT(*) FROM narratives WHERE date = ?", [day]) == 0
    assert rc.generation_of(conn, day) == gen + 1
    # the next read publishes against the new generation
    out = brief_mod.generate_brief(conn, None, day, "Owner", allow_llm=False)
    assert out["narrative_status"] == "template" and _one(conn, "SELECT generation FROM narratives WHERE date = ?", [day]) == gen + 1


# ---- 9. identity is derived on the server ----

def test_supplied_sample_id_is_ignored_and_bridge_rows_need_a_uuid():
    conn, policy, reg = _env()
    res = ingest_batch(conn, {"batch_id": "b", "samples": [
        {**_q(STEPS, "u1", D0, 6, 100), "sample_id": "hk:spoofed"},
        {**_q(STEPS, "u1", D0, 7, 200), "sample_id": "hk:other"},         # same uuid, another supplied id
        {k: v for k, v in _q(STEPS, "x", D0, 8, 300).items() if k != "uuid"}]}, policy, reg)
    assert res["accepted"] == 1 and res["skipped_types"] == {"no_uuid": 1}
    assert db.fetchall(conn, "SELECT sample_id, value FROM samples") == [("hk:u1", 100.0)]
    # a tombstoned uuid cannot be bypassed through a supplied id
    ingest_batch(conn, {"batch_id": "del", "samples": [], "deleted": ["dead"]}, policy, reg)
    res = ingest_batch(conn, {"batch_id": "b2", "samples": [{**_q(STEPS, "dead", D0, 9, 1), "sample_id": "hk:dead"}]}, policy, reg)
    assert res["accepted"] == 0 and res["guarded"] == 1


# ---- 10 and 11. units and malformed values never become data ----

def test_unit_mismatch_is_quarantined_not_relabelled_and_aliases_pass():
    conn, policy, reg = _env()
    res = ingest_batch(conn, {"batch_id": "b", "samples": [
        _q("HKQuantityTypeIdentifierBodyMass", "m1", D0, 6, 180, source="Zepp Life", unit="lb"),
        _q("HKQuantityTypeIdentifierVO2Max", "v1", D0, 6, 41.5, unit="mL/kg*min"),
        _q(STEPS, "s1", D0, 6, 100, unit="count")]}, policy, reg)
    assert res["accepted"] == 3
    rows = {r["hk_uuid"]: r for r in db.fetchdicts(conn, "SELECT * FROM samples")}
    assert rows["m1"]["quality"] == "unit_mismatch" and rows["m1"]["unit"] == "lb"
    assert rows["v1"]["quality"] is None and rows["v1"]["unit"] == "mL/min/kg"
    assert set(db.fetchall(conn, "SELECT hk_uuid FROM eligible_samples")) == {("v1",), ("s1",)}
    assert db.fetchall(conn, "SELECT unit FROM eligible_samples WHERE hk_uuid = 'v1'") == [("mL/min/kg",)]


@pytest.mark.parametrize("value", [True, "NaN", "inf", "oops", None])
def test_malformed_quantities_are_bad_value(value):
    conn, policy, reg = _env()
    row = normalize_sample(_q(STEPS, "u", D0, 6, value), policy, reg, "bridge", "b")
    assert row["quality"] == "bad_value" and row["value"] is None
    ingest_batch(conn, {"batch_id": "b", "samples": [_q(STEPS, "u", D0, 6, value)]}, policy, reg)
    assert _one(conn, "SELECT COUNT(*) FROM eligible_samples") == 0


def test_unknown_category_is_not_rescued_by_text_value():
    conn, policy, reg = _env()
    row = normalize_sample({"hk_type": SLEEP, "value": "HKCategoryValueSleepAnalysisBrandNew", "text_value": "deep", "unit": "min",
                            "start": "2026-07-01T20:00:00Z", "end": "2026-07-01T21:00:00Z", "source_name": AWU, "uuid": "n1"},
                           policy, reg, "bridge", "b")
    assert row["quality"] == "unknown_category" and row["text_value"] == "HKCategoryValueSleepAnalysisBrandNew"
    row = normalize_sample({"hk_type": SLEEP, "text_value": "deep", "unit": "min",      # no category at all: text is the category
                            "start": "2026-07-01T20:00:00Z", "end": "2026-07-01T21:00:00Z", "source_name": AWU, "uuid": "n2"},
                           policy, reg, "bridge", "b")
    assert row["quality"] is None and row["text_value"] == "deep"


# ---- 12. provenance of naive inputs ----

def test_time_source_tells_an_assumed_wall_time_from_a_real_instant():
    conn, policy, reg = _env()
    aware = normalize_sample(_q(STEPS, "a", D0, 6, 1), policy, reg, "bridge", "b")
    naive = normalize_sample({**_q(STEPS, "n", D0, 6, 1), "start": "2026-06-01T10:00:00", "end": "2026-06-01T10:00:00"}, policy, reg, "bridge", "b")
    assert aware["time_source"] == "bridge_utc" and naive["time_source"] == "assumed_reporting_wall"
    assert aware["start_utc"] == naive["start_utc"] == datetime(2026, 6, 1, 6, 0)   # 10:00 Dubai is 06:00Z either way


# ---- 13. the view judges time validity on the UTC instants ----

def test_dst_fall_back_interval_stays_eligible():
    conn, policy, reg = _env(tz="America/New_York")
    ingest_batch(conn, {"batch_id": "b", "samples": [
        {"hk_type": STEPS, "value": 10, "unit": "count", "start": "2025-11-02T05:50:00Z", "end": "2025-11-02T06:10:00Z",
         "source_name": AWU, "uuid": "d1"}]}, policy, reg)
    row = db.fetchdicts(conn, "SELECT start_ts, end_ts FROM samples")[0]
    assert row["start_ts"] == datetime(2025, 11, 2, 1, 50) and row["end_ts"] == datetime(2025, 11, 2, 1, 10)   # wall runs backwards
    assert _one(conn, "SELECT COUNT(*) FROM eligible_samples") == 1                                           # the instants do not


# ---- 16. derived rows of metrics the policy no longer treats as daily are removed ----

def test_disabling_a_metric_removes_its_derived_rows_on_the_next_recompute():
    conn, policy, reg = _env()
    ingest_batch(conn, {"batch_id": "b", "samples": [_q(STEPS, f"s{i}", D0 + timedelta(days=i), 6, 100 + i) for i in range(10)]}, policy, reg)
    rc.drain_journal(conn, policy, reg, today=D0 + timedelta(days=9))
    assert _one(conn, "SELECT COUNT(*) FROM daily_values WHERE metric = 'steps'") == 10
    assert _one(conn, "SELECT COUNT(*) FROM signals WHERE metric = 'steps'") == 10
    assert _one(conn, "SELECT COUNT(*) FROM baselines WHERE metric = 'steps'") > 0
    cfg = load_metric_policy()
    cfg["metrics"]["steps"]["daily"] = False
    off = MetricPolicy(cfg, default_tz="Asia/Dubai")
    off.sync_registry(conn)
    rc.recompute_window(conn, off, reg, days=9, value_window=9, now=datetime(2026, 6, 10, 8, tzinfo=timezone.utc))
    assert _one(conn, "SELECT COUNT(*) FROM daily_values WHERE metric = 'steps'") == 0
    assert _one(conn, "SELECT COUNT(*) FROM signals WHERE metric = 'steps'") == 0
    assert _one(conn, "SELECT COUNT(*) FROM baselines WHERE metric = 'steps'") == 0


# ---- 17. validation gaps ----

def test_validator_rejects_bad_numbers_partial_zones_impossible_dates_and_an_uncomputed_default_window():
    cfg = load_metric_policy()

    def bad(metric, **keys):
        c = json.loads(json.dumps(cfg))
        c["metrics"][metric] = {**c["metrics"][metric], **keys}
        return c
    with pytest.raises(PolicyError, match="flag_rule"):
        MetricPolicy(bad("sleep_duration", flag_rule="below_hours 1..5"))
    with pytest.raises(PolicyError, match="zones"):
        MetricPolicy(bad("recovery_score", zones={"green": [67, 100]}))
    with pytest.raises(PolicyError, match="calendar date"):
        MetricPolicy(bad("body_mass", snooze_until="2026-99-99"))
    with pytest.raises(PolicyError, match="default_window"):
        MetricPolicy({**cfg, "baseline": {"windows_days": [7], "min_days": 3, "mad_flag_multiplier": 1.5}})
    MetricPolicy({**cfg, "baseline": {"windows_days": [7, 30], "min_days": 3, "mad_flag_multiplier": 1.5}})   # 30 is the effective default


# ---- 18. a wide value window journals the dependents it moved ----

def test_wide_value_window_journals_dependents_outside_the_derived_window():
    conn, policy, reg = _env()
    ingest_batch(conn, {"batch_id": "b", "samples": [_q(STEPS, f"s{i}", D0 + timedelta(days=i), 6, 100) for i in range(4)]}, policy, reg)
    today = D0 + timedelta(days=3)
    rc.drain_journal(conn, policy, reg, today=today)
    assert _one(conn, "SELECT COUNT(*) FROM signals WHERE date = ?", [D0]) == 1
    ingest_batch(conn, {"batch_id": "d", "samples": [], "deleted": ["s0"]}, policy, reg)
    db.execute(conn, "DELETE FROM dirty_dates")                               # pretend the journal was lost
    now = datetime(2026, 6, 4, 8, tzinfo=timezone.utc)
    out = rc.recompute_window(conn, policy, reg, days=0, value_window=3, now=now)
    assert out["journaled"] == 1 and _one(conn, "SELECT COUNT(*) FROM daily_values WHERE date = ?", [D0]) == 0
    assert sorted((str(d), r) for d, r in db.fetchall(conn, "SELECT date, reason FROM dirty_dates")) == [(str(D0), "recompute")]
    rc.drain_journal(conn, policy, reg, today=today)
    assert _one(conn, "SELECT COUNT(*) FROM signals WHERE date = ?", [D0]) == 0           # the stale signal is gone
    # an unchanged wide pass journals nothing
    assert rc.recompute_window(conn, policy, reg, days=0, value_window=3, now=now)["journaled"] == 0


# ---- 20. signal states against an independent oracle; baseline window boundaries ----

def test_resting_hr_flag_rule_fires_with_the_computed_delta():
    conn, policy, reg = _env()
    # The owner's resting HR is Whoop's cloud value (owner decision 4h, fix
    # program D11), stored the way the puller stores a recovery's value.
    with db.transaction(conn) as c:
        for i in range(9):
            at = datetime.combine(D0 + timedelta(days=i), datetime.min.time()).replace(hour=3)   # naive UTC
            whoop.store_direct_sample(c, "resting_hr", f"recovery:r{i}", 70 if i == 8 else 55, "count/min", at, at,
                                      policy.zone)
    rc.enqueue(conn, {D0 + timedelta(days=i) for i in range(9)}, "test")
    # Evaluated the day after D0+8 closed: the reporting today's resting HR is
    # provisional and shown "so far", never flagged (fix program A4, Codex A point 4).
    rc.drain_journal(conn, policy, reg, today=D0 + timedelta(days=9))
    sig = {s["metric"]: s for s in signals_for(conn, D0 + timedelta(days=8))}["resting_hr"]
    assert sig["state"] == "flag" and sig["baseline_median"] == 55.0 and sig["delta_pct"] == round(15 / 55 * 100, 1)
    assert "above your 30-day baseline" in sig["why"]
    calm = {s["metric"]: s for s in signals_for(conn, D0 + timedelta(days=7))}["resting_hr"]
    assert calm["state"] == "favorable" and calm["delta_pct"] == 0.0


def test_baseline_windows_use_exactly_the_days_before_as_of():
    conn, policy, reg = _env()
    for i in range(35):
        db.execute(conn, "INSERT INTO daily_values (date, metric, value, unit, device_key) VALUES (?, 'steps', ?, 'count', 'apple_watch_ultra')",
                   [D0 + timedelta(days=i), float(i)])
    as_of = D0 + timedelta(days=35)
    bl.compute_baselines(conn, policy, as_of)
    got = {w: (med, n) for w, med, n in db.fetchall(conn, "SELECT window_days, median, n_days FROM baselines WHERE date = ? AND metric = 'steps'", [as_of])}
    assert got[30] == (19.5, 30)          # days 5..34
    assert got[60] == (17.0, 35) and got[90] == (17.0, 35)
    bl.compute_baselines(conn, policy, D0 + timedelta(days=6))
    assert db.fetchall(conn, "SELECT COUNT(*) FROM baselines WHERE date = ?", [D0 + timedelta(days=6)]) == [(0,)]   # 6 < min_days


# ---- D1. deterministic outputs ----

def test_corroboration_keys_are_sorted_and_sums_are_exact():
    conn, policy, reg = _env()
    ingest_batch(conn, {"batch_id": "b", "samples": [
        _q("HKQuantityTypeIdentifierHeartRate", "z", D0, 6, 60, source="Zepp", unit="count/min"),
        _q("HKQuantityTypeIdentifierHeartRate", "a", D0, 6, 62, unit="count/min"),
        _q("HKQuantityTypeIdentifierHeartRate", "w", D0, 6, 61, source="WHOOP", unit="count/min"),
        _q(STEPS, "s1", D0, 6, 1.0), _q(STEPS, "s2", D0, 7, 0.0005)]}, policy, reg)
    rc.drain_journal(conn, policy, reg, today=D0 + timedelta(days=1))
    corr = _one(conn, "SELECT corroboration FROM daily_values WHERE metric = 'heart_rate'")
    assert corr == json.dumps(json.loads(corr), sort_keys=True) and list(json.loads(corr)) == sorted(json.loads(corr))
    assert _one(conn, "SELECT value FROM daily_values WHERE metric = 'steps'") == 1.001          # exact decimal sum, rounded once
    assert "DECIMAL" in bl._AGG_SQL["sum"] and "DECIMAL" in bl._AGG_SQL["avg"]


# =====================================================================================
# Checkpoint C (Codex review of the deploy dry run, 2026-10-05): the code points.
# =====================================================================================

def test_daily_values_and_their_journal_rows_commit_together(monkeypatch):
    """C10: a failure in the journal step rolls the metric's daily values back with it."""
    conn, policy, reg = _env()
    ingest_batch(conn, {"batch_id": "b", "samples": [_q(STEPS, f"s{i}", D0 + timedelta(days=i), 6, 100) for i in range(3)]}, policy, reg)
    db.execute(conn, "DELETE FROM dirty_dates")
    now = datetime(2026, 6, 20, 8, tzinfo=timezone.utc)

    def boom(*a, **k):
        raise RuntimeError("journal write failed")
    monkeypatch.setattr(bl, "journal_dates", boom)
    with pytest.raises(RuntimeError):
        bl.compute_daily_values(conn, policy, reg, D0, D0 + timedelta(days=2), now=now, as_of=now.date(), journal="recompute")
    assert _one(conn, "SELECT COUNT(*) FROM daily_values WHERE metric = 'steps'") == 0      # rolled back with the journal
    monkeypatch.undo()
    bl.compute_daily_values(conn, policy, reg, D0, D0 + timedelta(days=2), now=now, as_of=now.date(), journal="recompute")
    assert _one(conn, "SELECT COUNT(*) FROM daily_values WHERE metric = 'steps'") == 3
    assert sorted(str(d) for d, in db.fetchall(conn, "SELECT date FROM dirty_dates WHERE reason = 'recompute'")) == [str(D0 + timedelta(days=i)) for i in range(3)]


def test_metadata_only_change_is_journaled_too():
    """C11: a daily value whose grade or corroboration differs (same value) still journals its date."""
    conn, policy, reg = _env()
    ingest_batch(conn, {"batch_id": "b", "samples": [_q(STEPS, f"s{i}", D0 + timedelta(days=i), 6, 100) for i in range(4)]}, policy, reg)
    today = D0 + timedelta(days=3)
    now = datetime(2026, 6, 4, 8, tzinfo=timezone.utc)
    rc.drain_journal(conn, policy, reg, today=today, now=now)
    db.execute(conn, "UPDATE daily_values SET grade = 'D' WHERE metric = 'steps' AND date = ?", [D0])   # a stale copy of the row
    out = rc.recompute_window(conn, policy, reg, days=0, value_window=3, now=now)
    assert out["journaled"] == 1
    assert [str(d) for d, in db.fetchall(conn, "SELECT date FROM dirty_dates WHERE reason = 'recompute'")] == [str(D0)]


def test_a_pass_invalidates_its_derived_dates_at_the_start_and_the_end():
    """C12: text published while a pass runs is written against the start generation and dies with the end bump."""
    conn, policy, reg = _env()
    ingest_batch(conn, {"batch_id": "b", "samples": [_q(STEPS, "s0", D0, 6, 100)]}, policy, reg)
    before = rc.generation_of(conn, D0)
    rc.recompute_dates(conn, policy, reg, {D0}, today=D0)
    assert rc.generation_of(conn, D0) == before + 2
    before = rc.generation_of(conn, D0)
    rc.recompute_window(conn, policy, reg, days=0, value_window=0, now=datetime(2026, 6, 1, 8, tzinfo=timezone.utc))
    assert rc.generation_of(conn, D0) == before + 2


def test_shutdown_bounds_a_checkpoint_that_overruns(tmp_path, monkeypatch):
    """C14: the checkpoint's own execution is bounded, not only the wait for the lock."""
    path = tmp_path / "helios.duckdb"

    def slow_checkpoint(conn, timeout=None):
        # Stands in for a long checkpoint. A COUNT over a cross join of ranges
        # is answered from statistics by DuckDB 1.5.4 in under a second, inside
        # the budget below, so the statement must do real work per row (about
        # 3.5 s uninterrupted on the M4; the interrupt ends it at the budget).
        db.fetchall(conn, "SELECT SUM(hash(i) % 7) FROM range(600000000) t(i)")
    monkeypatch.setattr(db, "checkpoint", slow_checkpoint)

    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(conn=db.connect(path), workers=set(), stopping=False))
        t0 = time.monotonic()
        out = await main.shutdown_store(app, tasks=[], grace=0.1, close_budget=0.8)
        return out, time.monotonic() - t0

    out, elapsed = asyncio.run(scenario())
    assert out["checkpointed"] is False and out["closed"] is True and elapsed < 2.5


def test_definitive_whoop_record_supersedes_legacy_rows_and_pending_keeps_them(tmp_path):
    """C18: UNSCORABLE or SCORED-without-field supersedes the day's legacy rows; PENDING keeps them."""
    conn, policy, reg = _env()
    for sid, metric, v, u in [("wh:sleep_duration:2026-07-10", "sleep_duration", 5.0, "h"),
                              ("wh:respiratory_rate:2026-07-10", "respiratory_rate", 14.0, "count/min"),
                              ("wh:sleep_need:2026-07-10", "sleep_need", 8.0, "h")]:
        db.execute(conn, "INSERT INTO samples (sample_id, metric, value, unit, start_ts, end_ts, source_name, device_key, sync_path) "
                         "VALUES (?, ?, ?, ?, '2026-07-09 23:30', '2026-07-10 06:40', 'WHOOP', 'whoop', 'whoop_live')", [sid, metric, v, u])
    s, e = "2026-07-09T19:30:00.000Z", "2026-07-10T02:40:00.000Z"
    out = whoop.pull(conn, FakeClient(tmp_path, sleep=[sleep_rec("s1", s, e, state="PENDING_SCORE")]), policy, now=NOW)
    assert out["legacy_kept_pending"] == 3 and out["superseded_legacy"] == 0
    assert _one(conn, "SELECT COUNT(*) FROM samples WHERE sample_id LIKE 'wh:%:2026-07-10'") == 3
    out = whoop.pull(conn, FakeClient(tmp_path, sleep=[sleep_rec("s1", s, e, state="UNSCORABLE", updated="2026-07-10T04:00:00.000Z")]), policy, now=NOW)
    assert out["superseded_legacy"] == 3 and _one(conn, "SELECT COUNT(*) FROM samples WHERE sample_id LIKE 'wh:%:2026-07-10'") == 0
    assert db.fetchall(conn, "SELECT COUNT(*) FROM tombstones WHERE reason = 'legacy_superseded'") == [(3,)]
    assert sorted(str(d) for d, in db.fetchall(conn, "SELECT date FROM dirty_dates")) == ["2026-07-09", "2026-07-10"]
    assert _one(conn, "SELECT COUNT(*) FROM eligible_samples WHERE device_key = 'whoop'") == 0
    # a nap on the same day never touches the day's main-sleep rows
    db.execute(conn, "INSERT INTO samples (sample_id, metric, value, unit, start_ts, end_ts, source_name, device_key, sync_path) "
                     "VALUES ('wh:sleep_duration:2026-07-11', 'sleep_duration', 6.0, 'h', '2026-07-10 23:30', '2026-07-11 06:40', 'WHOOP', 'whoop', 'whoop_live')")
    nap = sleep_rec("n1", "2026-07-11T10:00:00.000Z", "2026-07-11T10:40:00.000Z", nap=True, updated="2026-07-11T11:00:00.000Z")
    out = whoop.pull(conn, FakeClient(tmp_path, sleep=[nap]), policy, now=datetime(2026, 7, 11, 16, 0, tzinfo=NOW.tzinfo))
    assert out["superseded_legacy"] == 0 and _one(conn, "SELECT COUNT(*) FROM samples WHERE sample_id = 'wh:sleep_duration:2026-07-11'") == 1


def test_cache_follows_a_record_inside_its_own_transaction_and_scored_beats_unscored(tmp_path):
    """C19: no window sweep needed for consistency; a SCORED record owns the night over an unscored longer one."""
    conn, policy, reg = _env()
    s, e = "2026-07-09T19:30:00.000Z", "2026-07-10T02:40:00.000Z"
    fetched_at = datetime(2026, 7, 10, 6, 0, tzinfo=timezone.utc)
    with db.transaction(conn) as c:
        whoop.apply_record(c, "sleep", sleep_rec("s1", s, e), policy, fetched_at, "t1")
    assert db.fetchall(conn, "SELECT date, kind FROM whoop_cache") == [(date(2026, 7, 10), "sleep")]
    with db.transaction(conn) as c:   # the record moves a day later: old slot gone, new slot present, no sweep ran
        whoop.apply_record(c, "sleep", sleep_rec("s1", "2026-07-10T19:30:00.000Z", "2026-07-11T02:40:00.000Z", updated="2026-07-11T04:00:00.000Z"), policy, fetched_at, "t2")
    assert db.fetchall(conn, "SELECT date, kind FROM whoop_cache ORDER BY 1") == [(date(2026, 7, 11), "sleep")]
    # an UNSCORABLE record with more asleep time in its stale payload does not take the night from a SCORED one
    big = sleep_rec("u1", "2026-07-10T18:00:00.000Z", "2026-07-11T03:00:00.000Z", light=400, sws=100, rem=100, updated="2026-07-11T05:00:00.000Z")
    big["score_state"] = "UNSCORABLE"
    whoop.pull(conn, FakeClient(tmp_path, sleep=[big]), policy, now=datetime(2026, 7, 11, 10, 0, tzinfo=NOW.tzinfo))
    assert json.loads(_one(conn, "SELECT payload FROM whoop_cache WHERE date = DATE '2026-07-11' AND kind = 'sleep'"))["id"] == "s1"
