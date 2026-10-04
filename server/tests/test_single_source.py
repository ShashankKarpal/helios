"""Single-source Phase 1a: UTC instants, native identity, closed bypass.
Oracles are independent expected values (absolute UTC instants, reporting
dates), never the code under test (adjudication-A points 30 to 34)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from heliosd.config import load_metric_policy
from heliosd.ingest.bridge import ingest_batch
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

AWU = "Owner’s Ultra 1"


def _env(tz="Asia/Dubai"):
    conn = db.connect_memory()
    policy = MetricPolicy(default_tz=tz)
    policy.sync_registry(conn)
    return conn, policy, SourceRegistry()


def _steps(uuid, start, end, value=100, source=AWU):
    return {"hk_type": "HKQuantityTypeIdentifierStepCount", "value": value, "unit": "count",
            "start": start, "end": end, "source_name": source, "uuid": uuid}


def test_bridge_row_stores_utc_instant_and_reporting_wall():
    conn, policy, reg = _env("Asia/Dubai")
    res = ingest_batch(conn, {"batch_id": "b1", "samples": [
        _steps("u1", "2026-10-03T20:30:00.123Z", "2026-10-03T20:45:00.900Z")]}, policy, reg)
    assert res["accepted"] == 1 and res["affected_dates"] == ["2026-10-04"]
    row = db.fetchdicts(conn, "SELECT * FROM samples WHERE hk_uuid = 'u1'")[0]
    assert row["sample_id"] == "hk:u1"                      # native identity
    assert row["start_utc"] == datetime(2026, 10, 3, 20, 30)  # the instant, whole seconds
    assert row["end_utc"] == datetime(2026, 10, 3, 20, 45)
    assert row["start_ts"] == datetime(2026, 10, 4, 0, 30)    # Dubai wall: next calendar day
    assert row["time_source"] == "bridge_utc" and row["src_offset_min"] is None
    assert row["content_hash"].startswith("ch3:") and row["batch_id"] == "b1"


def test_session_timezone_never_shifts_the_stored_instant():
    conn, policy, reg = _env("Asia/Dubai")
    conn.execute("SET TimeZone = 'Asia/Dubai'")
    ingest_batch(conn, {"batch_id": "b", "samples": [_steps("u2", "2026-10-03T20:30:00Z", "2026-10-03T20:30:00Z")]},
                 policy, reg)
    assert db.fetchall(conn, "SELECT start_utc FROM samples WHERE hk_uuid = 'u2'")[0][0] == datetime(2026, 10, 3, 20, 30)
    conn.execute("SET TimeZone = 'UTC'")
    assert db.fetchall(conn, "SELECT start_utc FROM samples WHERE hk_uuid = 'u2'")[0][0] == datetime(2026, 10, 3, 20, 30)


def test_same_uuid_under_two_reporting_zones_is_one_row_with_one_instant():
    conn, policy, reg = _env("Asia/Dubai")
    s = _steps("u3", "2026-10-03T20:30:00Z", "2026-10-03T20:30:00Z")
    ingest_batch(conn, {"batch_id": "zoneA", "samples": [s]}, policy, reg)
    other = MetricPolicy(default_tz="America/Los_Angeles")
    r2 = ingest_batch(conn, {"batch_id": "zoneB", "samples": [s]}, other, reg)
    assert r2["accepted"] == 0 and r2["guarded"] == 1
    rows = db.fetchall(conn, "SELECT start_utc, start_ts FROM samples WHERE hk_uuid = 'u3'")
    assert rows == [(datetime(2026, 10, 3, 20, 30), datetime(2026, 10, 4, 0, 30))]


def test_uuid_guard_adds_nothing_beside_legacy_twins_and_writes_no_alias():
    conn, policy, reg = _env()
    # Two legacy ch2 twins of one uuid (the 330-minute pair), as the live store has.
    for sid, st in (("ch2:a", "2026-06-01 10:00"), ("ch2:b", "2026-06-01 15:30")):
        db.execute(conn, "INSERT INTO samples (sample_id, hk_uuid, metric, value, start_ts, end_ts, source_name, device_key, sync_path) "
                         "VALUES (?, 'legacy-1', 'steps', 100, ?, ?, ?, 'apple_watch_ultra', 'bridge')", [sid, st, st, AWU])
    res = ingest_batch(conn, {"batch_id": "replay", "samples": [_steps("legacy-1", "2026-06-01T06:00:00Z", "2026-06-01T06:00:00Z")]},
                       policy, reg)
    assert res["accepted"] == 0 and res["guarded"] == 1 and res["affected_dates"] == []
    assert db.fetchall(conn, "SELECT COUNT(*) FROM samples WHERE hk_uuid = 'legacy-1'")[0][0] == 2  # history untouched until 1b
    assert db.fetchall(conn, "SELECT COUNT(*) FROM sample_aliases")[0][0] == 0


def test_payload_cannot_name_its_own_metric_and_unknown_types_are_counted():
    conn, policy, reg = _env()
    res = ingest_batch(conn, {"batch_id": "x", "samples": [
        {"metric": "sleep_duration", "value": 7.5, "unit": "h", "start": "2026-07-01T20:00:00Z",
         "end": "2026-07-02T04:00:00Z", "source_name": "WHOOP", "uuid": "w1"},
        {"hk_type": "HKWorkoutTypeIdentifier", "value": 30, "unit": "min", "start": "2026-07-01T20:00:00Z",
         "end": "2026-07-01T20:30:00Z", "source_name": AWU, "uuid": "wk1"}]}, policy, reg)
    assert res["accepted"] == 0 and res["skipped"] == 2
    assert res["skipped_types"] == {"?": 1, "HKWorkoutTypeIdentifier": 1}
    assert db.fetchall(conn, "SELECT COUNT(*) FROM samples")[0][0] == 0


def test_unknown_sleep_category_keeps_raw_string_and_is_not_eligible():
    conn, policy, reg = _env()
    ingest_batch(conn, {"batch_id": "s", "samples": [
        {"hk_type": "HKCategoryTypeIdentifierSleepAnalysis", "value": "HKCategoryValueSleepAnalysisBrandNew",
         "unit": "min", "start": "2026-07-01T20:00:00Z", "end": "2026-07-01T21:00:00Z", "source_name": AWU, "uuid": "n1"},
        {"hk_type": "HKCategoryTypeIdentifierSleepAnalysis", "value": "HKCategoryValueSleepAnalysisAsleepDeep",
         "unit": "min", "start": "2026-07-01T21:00:00Z", "end": "2026-07-01T22:00:00Z", "source_name": AWU, "uuid": "n2"}]},
        policy, reg)
    rows = {r["hk_uuid"]: r for r in db.fetchdicts(conn, "SELECT hk_uuid, text_value, quality, value FROM samples")}
    assert rows["n1"]["text_value"] == "HKCategoryValueSleepAnalysisBrandNew" and rows["n1"]["quality"] == "unknown_category"
    assert rows["n2"]["text_value"] == "deep" and rows["n2"]["quality"] is None and rows["n2"]["value"] == 60.0
    eligible = {r[0] for r in db.fetchall(conn, "SELECT hk_uuid FROM eligible_samples")}
    assert eligible == {"n2"}


def test_ignored_mode_store_lands_rows_as_excluded_and_view_filters_them():
    cfg = {"devices": [{"key": "apple_watch_ultra", "patterns": ["*Ultra 1*"]}],
           "ignored": ["Athlytic*"], "fallback_key": "other", "ignored_mode": "store"}
    reg = SourceRegistry(cfg)
    assert reg.resolve("Athlytic") == "excluded"
    conn, policy, _ = _env()
    ingest_batch(conn, {"batch_id": "e", "samples": [_steps("a1", "2026-07-01T06:00:00Z", "2026-07-01T06:00:00Z", source="Athlytic")]},
                 policy, reg)
    assert db.fetchall(conn, "SELECT device_key FROM samples WHERE hk_uuid = 'a1'")[0][0] == "excluded"
    assert db.fetchall(conn, "SELECT COUNT(*) FROM eligible_samples")[0][0] == 0
    with pytest.raises(ValueError):
        SourceRegistry({**cfg, "ignored_mode": "maybe"})


def test_eligibility_view_is_null_safe_for_legacy_rows():
    conn, policy, reg = _env()
    db.execute(conn, "INSERT INTO samples (sample_id, hk_uuid, metric, value, start_ts, end_ts, source_name, device_key, sync_path) "
                     "VALUES ('ch2:legacy', 'l1', 'steps', 5, '2026-06-01 10:00', '2026-06-01 10:05', ?, 'apple_watch_ultra', 'bridge')", [AWU])
    db.execute(conn, "INSERT INTO tombstones (tomb_id, hk_uuid, reason) VALUES ('hk:other', 'other', 'bridge_deleted')")
    assert db.fetchall(conn, "SELECT COUNT(*) FROM eligible_samples WHERE hk_uuid = 'l1'")[0][0] == 1


def test_reporting_zone_resolution_order():
    cfg = load_metric_policy()
    assert MetricPolicy(cfg, default_tz="Asia/Dubai").reporting_timezone == "Asia/Dubai"
    assert MetricPolicy({**cfg, "reporting_timezone": "Europe/London"}, default_tz="Asia/Dubai").reporting_timezone == "Europe/London"
    assert MetricPolicy(cfg).reporting_timezone == "UTC"
    assert MetricPolicy(cfg).agg("steps") == "sum" and MetricPolicy(cfg).agg("heart_rate") == "avg"
    assert MetricPolicy({**cfg, "metrics": {**cfg["metrics"], "steps": {**cfg["metrics"]["steps"], "agg": "max"}}}).agg("steps") == "max"
    assert MetricPolicy(cfg).day_basis("sleep_duration") == "sleep_end" and MetricPolicy(cfg).daily("sleep_analysis") is False


# ---- item 3: tombstones, replay guard, atomic batch, dirty-date journal ----

def _one(conn, sql, params=None):
    return db.fetchall(conn, sql, params)[0][0]


def test_deletion_before_insert_wins_and_is_journaled():
    conn, policy, reg = _env("Asia/Dubai")
    r1 = ingest_batch(conn, {"batch_id": "del-first", "samples": [], "deleted": ["late-1"]}, policy, reg)
    assert r1["deleted"] == 1 and _one(conn, "SELECT COUNT(*) FROM tombstones WHERE hk_uuid = 'late-1'") == 1
    r2 = ingest_batch(conn, {"batch_id": "insert-later", "samples": [
        _steps("late-1", "2026-10-03T20:30:00Z", "2026-10-03T20:30:00Z")]}, policy, reg)
    assert r2["accepted"] == 0 and r2["guarded"] == 1
    assert _one(conn, "SELECT COUNT(*) FROM samples WHERE hk_uuid = 'late-1'") == 0


def test_delete_then_replay_old_batch_resurrects_nothing():
    conn, policy, reg = _env("Asia/Dubai")
    old = {"batch_id": "old", "samples": [_steps("gone-1", "2026-10-03T20:30:00Z", "2026-10-03T20:40:00Z", value=100),
                                           _steps("stays-1", "2026-10-03T21:30:00Z", "2026-10-03T21:40:00Z", value=40)]}
    ingest_batch(conn, old, policy, reg)
    assert _one(conn, "SELECT SUM(value) FROM samples WHERE metric = 'steps'") == 140
    rd = ingest_batch(conn, {"batch_id": "d", "samples": [], "deleted": ["gone-1"]}, policy, reg)
    assert rd["affected_dates"] == ["2026-10-04"]           # the Dubai date of the removed row
    assert _one(conn, "SELECT COUNT(*) FROM dirty_dates WHERE date = DATE '2026-10-04' AND reason = 'delete'") == 1
    rr = ingest_batch(conn, old, policy, reg)                # outbox replay of the old batch
    assert rr["accepted"] == 0 and rr["guarded"] == 2
    assert _one(conn, "SELECT COUNT(*) FROM samples WHERE hk_uuid = 'gone-1'") == 0
    assert _one(conn, "SELECT SUM(value) FROM samples WHERE metric = 'steps'") == 40
    assert _one(conn, "SELECT reason FROM tombstones WHERE hk_uuid = 'gone-1'") == "bridge_deleted"


def test_same_batch_twice_changes_nothing_but_the_receipt():
    conn, policy, reg = _env()
    b = {"batch_id": "twice", "device": "iphone", "samples": [_steps(f"t-{i}", f"2026-07-01T0{i}:00:00Z", f"2026-07-01T0{i}:05:00Z") for i in range(5)]}
    a = ingest_batch(conn, b, policy, reg)
    dump1 = db.fetchall(conn, "SELECT sample_id, value, start_utc FROM samples ORDER BY 1")
    a2 = ingest_batch(conn, b, policy, reg)
    assert a["accepted"] == 5 and a2["accepted"] == 0 and a2["guarded"] == 5
    assert db.fetchall(conn, "SELECT sample_id, value, start_utc FROM samples ORDER BY 1") == dump1
    assert _one(conn, "SELECT COUNT(*) FROM sync_log WHERE batch_id = 'twice'") == 1


def test_failure_inside_the_batch_leaves_nothing_behind(monkeypatch):
    import heliosd.ingest.bridge as bridge_mod
    conn, policy, reg = _env()
    ingest_batch(conn, {"batch_id": "seed", "samples": [_steps("keep-1", "2026-07-01T06:00:00Z", "2026-07-01T06:05:00Z")]}, policy, reg)
    before = db.fetchall(conn, "SELECT sample_id FROM samples ORDER BY 1")

    def boom(*a, **k):
        raise RuntimeError("injected after inserts and deletes")
    monkeypatch.setattr(bridge_mod, "_journal", boom)
    with pytest.raises(RuntimeError):
        ingest_batch(conn, {"batch_id": "fails", "deleted": ["keep-1"],
                            "samples": [_steps("new-1", "2026-07-02T06:00:00Z", "2026-07-02T06:05:00Z")]}, policy, reg)
    assert db.fetchall(conn, "SELECT sample_id FROM samples ORDER BY 1") == before   # delete rolled back
    assert _one(conn, "SELECT COUNT(*) FROM tombstones") == 0                        # tombstone rolled back
    assert _one(conn, "SELECT COUNT(*) FROM sync_log WHERE batch_id = 'fails'") == 0  # no receipt, no ack
    assert _one(conn, "SELECT COUNT(*) FROM dirty_dates") == 1                       # only the seed batch's date


def test_journal_records_both_start_and_end_dates_of_new_rows():
    conn, policy, reg = _env("Asia/Dubai")
    ingest_batch(conn, {"batch_id": "span", "samples": [
        {"hk_type": "HKCategoryTypeIdentifierSleepAnalysis", "value": "HKCategoryValueSleepAnalysisAsleepCore", "unit": "min",
         "start": "2026-10-03T19:00:00Z", "end": "2026-10-04T02:00:00Z", "source_name": "WHOOP", "uuid": "sl-1"}]}, policy, reg)
    assert sorted(str(r[0]) for r in db.fetchall(conn, "SELECT date FROM dirty_dates")) == ["2026-10-03", "2026-10-04"]


# ---- item 4: recompute by explicit dates ----

from datetime import timedelta  # noqa: E402

from heliosd.signals import recompute as rc  # noqa: E402
from heliosd.signals.baselines import compute_daily_values  # noqa: E402
from heliosd.narrative.brief import generate_brief  # noqa: E402

D0 = date(2026, 6, 1)


def _daily_steps(conn, policy, reg, n_days, start=D0, per_day=1):
    """One Apple Watch steps sample per day at 06:00Z (Dubai 10:00), uuid st-<i>-<k>."""
    samples = []
    for i in range(n_days):
        d = start + timedelta(days=i)
        for k in range(per_day):
            uid = f"st-{i}-{k}" if start == D0 else f"st-{start}-{i}-{k}"
            samples.append(_steps(uid, f"{d}T0{6 + k}:00:00Z", f"{d}T0{6 + k}:05:00Z", value=1000 + 100 * i))
    return ingest_batch(conn, {"batch_id": f"steps-{start}", "samples": samples}, policy, reg)


def test_deletion_cascades_to_daily_value_signal_baseline_and_narrative():
    conn, policy, reg = _env("Asia/Dubai")
    today = D0 + timedelta(days=7)                       # eight days D0..D0+7
    _daily_steps(conn, policy, reg, 8)
    out = rc.drain_journal(conn, policy, reg, today=today)
    assert out["journal_rows"] == 8 and _one(conn, "SELECT COUNT(*) FROM dirty_dates") == 0
    # baseline for today uses the 7 days before it: exactly min_days
    b = db.fetchdicts(conn, "SELECT n_days FROM baselines WHERE metric = 'steps' AND date = ? AND window_days = 30", [today])
    assert b and b[0]["n_days"] == 7
    assert _one(conn, "SELECT value FROM daily_values WHERE metric = 'steps' AND date = ?", [D0]) == 1000
    assert _one(conn, "SELECT COUNT(*) FROM signals WHERE metric = 'steps' AND date = ?", [D0]) == 1
    # a cached narrative and actions for today; one adopted action must survive
    generate_brief(conn, None, today, "Owner", allow_llm=False)
    db.execute(conn, "INSERT OR REPLACE INTO actions (action_id, date, text, category, status) VALUES ('keep', ?, 'walk', 'move', 'adopted')", [today])
    assert _one(conn, "SELECT COUNT(*) FROM narratives WHERE date = ?", [today]) == 1
    gen_before = rc.generation_of(conn, today)
    # delete the ONLY input of D0 and drain
    ingest_batch(conn, {"batch_id": "del", "samples": [], "deleted": ["st-0-0"]}, policy, reg)
    out = rc.drain_journal(conn, policy, reg, today=today)
    assert out["dates"] == 1 and out["derived_dates"] == 8
    assert _one(conn, "SELECT COUNT(*) FROM daily_values WHERE metric = 'steps' AND date = ?", [D0]) == 0   # no input, no value
    assert _one(conn, "SELECT COUNT(*) FROM signals WHERE metric = 'steps' AND date = ?", [D0]) == 0
    assert _one(conn, "SELECT COUNT(*) FROM baselines WHERE metric = 'steps' AND date = ? AND window_days = 30", [today]) == 0  # 6 < min_days
    assert _one(conn, "SELECT COUNT(*) FROM narratives WHERE date = ?", [today]) == 0
    assert rc.generation_of(conn, today) == gen_before + 1
    assert _one(conn, "SELECT COUNT(*) FROM actions WHERE date = ? AND status = 'suggested'", [today]) == 0
    assert _one(conn, "SELECT COUNT(*) FROM actions WHERE action_id = 'keep'") == 1


def test_dependency_expansion_boundaries():
    daily, derived = rc.expand({D0}, max_window=90, today=D0 + timedelta(days=200))
    assert daily == {D0} and D0 + timedelta(days=90) in derived and D0 + timedelta(days=91) not in derived
    assert len(derived) == 91
    daily, derived = rc.expand({D0}, max_window=90, today=D0 + timedelta(days=5))
    assert derived == {D0 + timedelta(days=i) for i in range(6)}
    daily, derived = rc.expand({D0 + timedelta(days=400)}, max_window=90, today=D0)   # future date: nothing
    assert daily == set() and derived == set()
    assert rc.intervals({D0, D0 + timedelta(days=1), D0 + timedelta(days=5)}) == [(D0, D0 + timedelta(days=1)), (D0 + timedelta(days=5), D0 + timedelta(days=5))]


def test_freshness_is_judged_against_the_reporting_today_not_the_range_end():
    conn, policy, reg = _env("Asia/Dubai")
    _daily_steps(conn, policy, reg, 3)
    today = date(2026, 10, 4)
    compute_daily_values(conn, policy, reg, D0, D0 + timedelta(days=2), as_of=today)
    wide = db.fetchall(conn, "SELECT confidence, grade FROM daily_values WHERE metric = 'steps' AND date = ?", [D0])
    compute_daily_values(conn, policy, reg, D0, D0, as_of=today)
    alone = db.fetchall(conn, "SELECT confidence, grade FROM daily_values WHERE metric = 'steps' AND date = ?", [D0])
    assert wide == alone
    # the reporting today itself does carry the freshness term
    compute_daily_values(conn, policy, reg, D0, D0, as_of=D0, now=datetime(2026, 6, 3, 9))
    stale = db.fetchall(conn, "SELECT confidence FROM daily_values WHERE metric = 'steps' AND date = ?", [D0])
    assert stale[0][0] < alone[0][0]


def test_drain_removes_only_the_rows_it_processed(monkeypatch):
    conn, policy, reg = _env()
    rc.enqueue(conn, {D0}, "ingest", "b1")
    real = rc.recompute_dates

    def during(conn_, *a, **k):
        rc.enqueue(conn_, {D0 + timedelta(days=30)}, "ingest", "b2")  # arrives mid-pass
        return real(conn_, *a, **k)
    monkeypatch.setattr(rc, "recompute_dates", during)
    out = rc.drain_journal(conn, policy, reg, today=D0 + timedelta(days=60))
    assert out["journal_rows"] == 1
    left = db.fetchall(conn, "SELECT date, batch_id FROM dirty_dates")
    assert left == [(D0 + timedelta(days=30), "b2")]
    assert rc.drain_journal(conn, policy, reg, today=D0 + timedelta(days=60))["journal_rows"] == 1
    assert rc.drain_journal(conn, policy, reg) is None


def test_sparse_dates_beyond_the_span_bound_collapse_to_one_wide_pass():
    conn, policy, reg = _env("Asia/Dubai")
    # 200 days apart, both in the past (the view excludes future-dated rows).
    far = D0 - timedelta(days=200)
    _daily_steps(conn, policy, reg, 1, start=D0)
    _daily_steps(conn, policy, reg, 1, start=far)
    out = rc.recompute_dates(conn, policy, reg, {D0, far}, today=D0 + timedelta(days=10))
    assert out["wide"] is True and out["dates"] == 2
    assert _one(conn, "SELECT COUNT(*) FROM daily_values WHERE metric = 'steps'") == 2


def test_narrative_is_not_published_when_the_generation_moved(monkeypatch):
    import heliosd.narrative.brief as brief_mod
    conn, policy, reg = _env()
    today = D0 + timedelta(days=7)
    _daily_steps(conn, policy, reg, 8)
    rc.drain_journal(conn, policy, reg, today=today)
    gens = iter([3, 4])  # read 3 at start, see 4 at publish time
    monkeypatch.setattr(brief_mod, "generation_of", lambda conn_, day: next(gens))
    out = generate_brief(conn, None, today, "Owner", force=True, allow_llm=True)
    assert out["narrative_status"] == "generating"
    assert _one(conn, "SELECT COUNT(*) FROM narratives WHERE date = ?", [today]) == 0
