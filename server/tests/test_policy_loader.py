"""Phase 1a item 8: the schema-validated policy loader. The overlay is a
patch (nothing required), the merged policy is strict (unit per metric, no
unknown keys, no duplicate hk, full HealthKit identifiers, closed enums), the
effective defaults of plan v2 4.3 are exposed, and the aggregation dispatcher
covers sum, avg, last, min, max with ties by wall time then sample id."""

from __future__ import annotations

import copy
from datetime import date, datetime

import pytest

from heliosd import config
from heliosd.config import deep_merge, load_metric_policy, load_yaml
from heliosd.ingest.bridge import ingest_batch
from heliosd.signals.baselines import compute_daily_values
from heliosd.signals import watchdog
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy, PolicyError
from heliosd.trust.registry import SourceRegistry
from heliosd.trust.schema import METRIC_KEYS, validate_policy, validate_registry

AWU = "Owner’s Ultra 1"


def _with(cfg: dict, metric: str, **keys) -> dict:
    out = copy.deepcopy(cfg)
    out["metrics"][metric] = {**out["metrics"][metric], **keys}
    return out


def _problems(exc: pytest.ExceptionInfo) -> str:
    return "\n".join(exc.value.problems)


# ---- the files as shipped ----

def test_tracked_default_and_fixture_merge_both_validate_strictly():
    validate_policy(load_yaml("metric_policy.yaml", overlay=False), strict=True)
    validate_registry(load_yaml("source_registry.yaml", overlay=False))
    p = MetricPolicy()
    assert all(p.unit(m) for m in p.metrics)
    assert set(p.blocks) <= {"reporting_timezone", "unknown_types", "workouts", "activity_rings", "ecg", "labs"}


def test_owner_shaped_overlay_is_a_valid_patch_and_merges_into_a_valid_policy():
    """Priority lists only, snoozes as YAML dates, a sources list with label,
    path, cadence_hours, ingest, notify and fix: the shape of a real overlay."""
    overlay = {"metrics": {"heart_rate": {"priority": ["strap", "watch", "band"]},
                           "body_mass": {"snooze_until": date(2026, 12, 31)},
                           "bmi": {"snooze_until": datetime(2026, 11, 30, 8, 0)}},
               "sources": [{"key": "power_log", "label": "Power events", "path": "~/log/power.jsonl",
                            "cadence_hours": 24, "ingest": "events"},
                           {"key": "backup_marker", "label": "Nightly backup", "path": "~/backup/LAST_OK",
                            "cadence_hours": 26, "notify": True, "fix": "run the backup CLI"}]}
    patch = validate_policy(overlay, strict=False)                 # nothing required in a patch
    assert patch["metrics"]["body_mass"]["snooze_until"] == "2026-12-31"
    assert patch["metrics"]["bmi"]["snooze_until"] == "2026-11-30"
    merged = MetricPolicy(deep_merge(load_yaml("metric_policy.yaml", overlay=False), patch))
    assert merged.priority("heart_rate") == ["strap", "watch", "band"]
    assert merged.unit("heart_rate") == "count/min" and merged.cadence_hours("heart_rate") == 12   # untouched keys keep defaults
    assert len(merged.sources) == 2
    # the normalised snooze is understood by the watchdog
    assert watchdog._snoozed(merged.get("body_mass"), datetime(2026, 12, 1)) is True
    assert watchdog._snoozed(merged.get("body_mass"), datetime(2027, 1, 1)) is False


def test_patch_still_rejects_unknown_keys_and_bad_shapes():
    with pytest.raises(PolicyError) as e:
        validate_policy({"metrics": {"heart_rate": {"priorty": ["a"]}}}, strict=False)
    assert "metrics.heart_rate" in _problems(e) and "priorty" in _problems(e)
    with pytest.raises(PolicyError) as e:
        validate_policy({"metrics": {"spo2": {"direction": "sideways", "agg": "median", "hk": "OxygenSaturation"}}}, strict=False)
    msg = _problems(e)
    assert "direction" in msg and "agg" in msg and "hk" in msg     # short HealthKit identifier refused
    with pytest.raises(PolicyError):
        validate_policy({"baseline": {"windows": [30]}}, strict=False)       # unknown top-level sub-key
    with pytest.raises(PolicyError):
        validate_policy({"thresholds": {}}, strict=False)                    # unknown top-level block
    # a patch may add a metric without a unit; the merge is where the unit is required
    validate_policy({"metrics": {"naps": {"priority": ["whoop"]}}}, strict=False)
    with pytest.raises(PolicyError) as e:
        validate_policy({"metrics": {"naps": {"priority": ["whoop"]}}}, strict=True)
    assert "unit" in _problems(e)


def test_strict_rejects_duplicate_hk_missing_unit_bad_derive_and_bad_zone():
    cfg = load_metric_policy()
    with pytest.raises(PolicyError) as e:
        MetricPolicy(_with(cfg, "hrv_sdnn", hk="HKQuantityTypeIdentifierHeartRate"))
    assert "mapped by more than one metric" in _problems(e) and "heart_rate" in _problems(e)
    bad = copy.deepcopy(cfg)
    del bad["metrics"]["steps"]["unit"]
    with pytest.raises(PolicyError) as e:
        MetricPolicy(bad)
    assert "metrics.steps" in _problems(e) and "unit" in _problems(e)
    with pytest.raises(PolicyError) as e:
        MetricPolicy(_with(cfg, "glucose", derive={"from": "glucose_points", "devices": ["cgm"]}))
    assert "derive.from" in _problems(e)
    with pytest.raises(PolicyError) as e:
        MetricPolicy({**cfg, "reporting_timezone": "Mars/Olympus"})
    assert "IANA" in _problems(e)
    with pytest.raises(PolicyError):
        MetricPolicy({**cfg, "baseline": {**cfg["baseline"], "default_window": 45}})
    # every problem is reported at once, with its path
    worst = _with(_with(cfg, "steps", agg="median"), "spo2", trust="gospel")
    with pytest.raises(PolicyError) as e:
        MetricPolicy(worst)
    assert len(e.value.problems) == 2 and all(p.startswith("metrics.") for p in e.value.problems)


def test_every_documented_key_is_accepted_and_nothing_else():
    cfg = load_metric_policy()
    full = _with(cfg, "heart_rate", corroboration=["whoop"], exercise_priority=["zepp_helio", "whoop", "apple_watch_ultra"],
                 sample_context="all_day", episode_group="main_sleep", day_basis="calendar",
                 daily=True, agg="avg", baseline_scope="source", discrepancy={"abs": 5, "pct": 10},
                 coverage={"slot_min": 15, "min_fraction": 0.7}, derive={"from": "heart_rate", "devices": ["whoop"]},
                 live_overlay="whoop", never_blend=True, note="x", label="Heart rate", optional=False, flag_rule="none",
                 zones={"green": [67, 100], "yellow": [34, 66], "red": [0, 33]}, snooze_until="2026-12-31",
                 sync_paths={"whoop": ["whoop_live"]}, merge="interval")                      # Wave 2 keys (schema v4 scaffold)
    p = MetricPolicy(full)
    assert set(full["metrics"]["heart_rate"]) == METRIC_KEYS
    eff = p.effective("heart_rate")
    assert eff["corroboration"] == ["whoop"] and eff["coverage"] == {"slot_min": 15, "min_fraction": 0.7}
    with pytest.raises(PolicyError):
        MetricPolicy(_with(cfg, "heart_rate", discrepancy={"abs": 5, "unexpected": 1}))
    with pytest.raises(PolicyError):
        MetricPolicy(_with(cfg, "heart_rate", coverage={"slot_min": 15}))              # min_fraction required
    with pytest.raises(PolicyError):
        MetricPolicy(_with(cfg, "heart_rate", flag_rule="whenever >= 5"))
    for blk in ("unknown_types", "workouts", "activity_rings", "ecg", "labs"):
        assert blk in MetricPolicy({**cfg, blk: {"note": "kept"}}).blocks            # top-level blocks kept


def test_effective_defaults_absent_explicit_and_overlay_merged():
    cfg = load_metric_policy()
    p = MetricPolicy(cfg)
    # absent: plan v2 4.3 defaults
    assert p.effective("sleep_duration")["day_basis"] == "sleep_end" and p.effective("steps")["day_basis"] == "calendar"
    assert p.effective("sleep_analysis")["daily"] is False and p.effective("steps")["daily"] is True
    assert p.effective("steps")["agg"] == "sum" and p.effective("body_mass")["agg"] == "last" and p.effective("heart_rate")["agg"] == "avg"
    assert p.effective("heart_rate")["corroboration"] is None and p.effective("heart_rate")["sample_context"] == "all_day"
    assert p.effective("heart_rate")["flag_rule"] == "none" and p.effective("resting_hr")["flag_rule"] == "abs_above_30d_avg >= 5"
    # explicit beats the default
    q = MetricPolicy(_with(_with(cfg, "steps", agg="max", day_basis="whoop_cycle", daily=False), "resting_hr", corroboration=[], direction="contextual"))
    assert q.effective("steps")["agg"] == "max" and q.effective("steps")["day_basis"] == "whoop_cycle" and q.daily("steps") is False
    assert q.effective("resting_hr")["corroboration"] == [] and q.direction("resting_hr") == "none"
    # overlay-merged: the fixture overlay replaces the priority list and leaves every other key to the default
    m = MetricPolicy()
    assert m.effective("heart_rate")["priority"] == ["zepp_helio", "apple_watch_ultra", "whoop", "apple_watch_6_legacy"]
    assert m.effective("heart_rate")["cadence_hours"] == 12 and m.effective("heart_rate")["unit"] == "count/min"
    assert m.sync_registry(db.connect_memory()) == len(m.metrics)


def test_registry_validation_and_the_real_fixture_files_load(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        SourceRegistry({"devices": [], "ignored_mode": "maybe"})
    with pytest.raises(PolicyError) as e:
        SourceRegistry({"devices": [{"key": "a", "pattern": ["A*"]}]})                # misspelt key
    assert "pattern" in _problems(e)
    with pytest.raises(PolicyError) as e:
        SourceRegistry({"devices": [{"key": "a"}, {"key": "a"}]})
    assert "duplicate" in _problems(e)
    # the loader validates the overlay files on disk before merging
    (tmp_path / "metric_policy.yaml").write_text("metrics:\n  steps:\n    priorty: [x]\n", encoding="utf-8")
    monkeypatch.setenv("HELIOS_HOME", str(tmp_path))
    with pytest.raises(PolicyError) as e:
        MetricPolicy()
    assert "overlay metric_policy.yaml" in str(e.value) and config.active_overlays() == ["metric_policy.yaml"]


# ---- the aggregation dispatcher ----

def _env():
    conn = db.connect_memory()
    policy = MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    return conn, policy, SourceRegistry()


def _steps(uuid, start, value):
    return {"hk_type": "HKQuantityTypeIdentifierStepCount", "value": value, "unit": "count",
            "start": start, "end": start, "source_name": AWU, "uuid": uuid}


def test_dispatcher_sum_avg_min_max_and_last_with_tie_order():
    conn, policy, reg = _env()
    d = date(2026, 7, 1)
    ingest_batch(conn, {"batch_id": "agg", "samples": [
        _steps("s1", "2026-07-01T04:00:00Z", 100), _steps("s2", "2026-07-01T05:00:00Z", 300), _steps("s3", "2026-07-01T06:00:00Z", 200)]},
        policy, reg)
    cfg = load_metric_policy()
    del cfg["metrics"]["steps"]["merge"]      # the aggregation dispatcher, not the steps merge (B11, a sum of its own)
    expect = {"sum": 600.0, "avg": 200.0, "min": 100.0, "max": 300.0, "last": 200.0}
    for agg, want in expect.items():
        p = MetricPolicy(_with(cfg, "steps", agg=agg), default_tz="Asia/Dubai")
        p.sync_registry(conn)
        compute_daily_values(conn, p, reg, d, d, as_of=d)
        assert db.fetchall(conn, "SELECT value FROM daily_values WHERE metric = 'steps' AND date = ?", [d])[0][0] == want, agg
    # last: ties at the same wall time resolve by sample_id, and legacy rows (NULL start_utc) order with new ones
    db.execute(conn, "INSERT INTO samples (sample_id, hk_uuid, metric, value, unit, start_ts, end_ts, source_name, device_key, sync_path) "
                     "VALUES ('ch2:legacy-tie', 'lt', 'body_mass', 80.0, 'kg', '2026-07-01 09:00', '2026-07-01 09:00', 'Zepp Life', 'zepp_life_scale', 'bridge')")
    ingest_batch(conn, {"batch_id": "mass", "samples": [
        {"hk_type": "HKQuantityTypeIdentifierBodyMass", "value": 81.0, "unit": "kg", "start": "2026-07-01T05:00:00Z", "end": "2026-07-01T05:00:00Z",
         "source_name": "Zepp Life", "uuid": "tie-a"},
        {"hk_type": "HKQuantityTypeIdentifierBodyMass", "value": 79.0, "unit": "kg", "start": "2026-07-01T03:00:00Z", "end": "2026-07-01T03:00:00Z",
         "source_name": "Zepp Life", "uuid": "tie-b"}]}, policy, reg)
    policy.sync_registry(conn)
    compute_daily_values(conn, policy, reg, d, d, as_of=d)
    # wall order: tie-b 07:00, then 09:00 shared by ch2:legacy-tie (80) and hk:tie-a (81); 'hk:' sorts after 'ch2:' so 81 wins
    assert db.fetchall(conn, "SELECT value FROM daily_values WHERE metric = 'body_mass' AND date = ?", [d])[0][0] == 81.0
