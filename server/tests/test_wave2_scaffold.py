"""Wave 2 scaffold (S0, design 1.0): schema v4 is additive and upgrades a v3
store in place; a store recorded at a newer version still opens; the new
policy keys are accepted, read through accessors and refused when malformed;
the daily-value dispatcher routes by policy and gives the pre-Wave-2 values
(expected values are worked out here by hand from the synthetic rows, never
taken from the code under test); daily_values.detail round-trips None and
counts as a journaled field; the episode interface stub has the agreed fields.

The policy used here is self-contained (not the shipped files, which the Wave
2 groups edit), and the synthetic nights and days are ones on which the
pre-Wave-2 rules and the designed Wave 2 rules agree, so the later groups can
change their rules without rewriting this file. Synthetic data only."""

from __future__ import annotations

import copy
import json
from dataclasses import fields
from datetime import date, datetime

import duckdb
import pytest

from heliosd.config import load_metric_policy
from heliosd.signals import baselines as bl
from heliosd.signals import episodes
from heliosd.signals.baselines import compute_daily_values
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy, PolicyError
from heliosd.trust.registry import SourceRegistry
from heliosd.trust.schema import base_device, split_device_key, validate_policy

D0, D1 = date(2026, 6, 10), date(2026, 6, 11)
AS_OF = date(2026, 6, 12)          # both days are history: no running-total or freshness case

METRICS = {
    "steps": {"hk": "HKQuantityTypeIdentifierStepCount", "unit": "count", "agg": "sum",
              "priority": ["apple_watch_ultra", "iphone"]},
    "heart_rate": {"hk": "HKQuantityTypeIdentifierHeartRate", "unit": "count/min", "agg": "avg",
                   "priority": ["zepp_helio", "apple_watch_ultra", "whoop"]},
    "resting_hr": {"hk": "HKQuantityTypeIdentifierRestingHeartRate", "unit": "count/min", "agg": "last",
                   "priority": ["apple_watch_ultra", "whoop", "zepp_helio"]},
    "sleep_analysis": {"hk": "HKCategoryTypeIdentifierSleepAnalysis", "unit": "min",
                       "priority": ["whoop", "apple_watch_ultra", "zepp_helio"]},
    "sleep_duration": {"hk": None, "unit": "h", "priority": ["whoop", "apple_watch_ultra", "zepp_helio"]},
}
CONFIDENCE = {"weights": {"source_rank": 0.35, "freshness": 0.25, "coverage": 0.2, "agreement": 0.2},
              "grades": {"A": 0.85, "B": 0.7, "C": 0.5, "D": 0.0}, "agreement_tolerance_pct": 12}


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


# (sample_id, metric, device_key, sync_path, start, end, value, text_value); wall times in the reporting zone
ROWS = [
    # steps (sum): the watch owns D0, the iPhone alone fills D1; the strap is not in the list
    ("hk:s-w1", "steps", "apple_watch_ultra", "bridge", "2026-06-10 08:00", "2026-06-10 08:10", 1000, None),
    ("hk:s-w2", "steps", "apple_watch_ultra", "bridge", "2026-06-10 12:00", "2026-06-10 12:30", 234, None),
    ("hk:s-w3", "steps", "apple_watch_ultra", "bridge", "2026-06-10 23:50", "2026-06-11 00:10", 66, None),   # start date D0
    ("hk:s-p1", "steps", "iphone", "bridge", "2026-06-10 09:00", "2026-06-10 09:20", 500, None),
    ("hk:s-p2", "steps", "iphone", "bridge", "2026-06-10 18:00", "2026-06-10 18:00", 25, None),
    ("hk:s-p3", "steps", "iphone", "bridge", "2026-06-11 10:00", "2026-06-11 10:30", 800, None),
    ("hk:s-z1", "steps", "zepp_helio", "bridge", "2026-06-10 10:00", "2026-06-10 10:05", 9999, None),
    # heart_rate (avg): the strap owns D0, the watch alone fills D1
    ("hk:h-z1", "heart_rate", "zepp_helio", "bridge", "2026-06-10 07:00", "2026-06-10 07:00", 60, None),
    ("hk:h-z2", "heart_rate", "zepp_helio", "bridge", "2026-06-10 08:00", "2026-06-10 08:00", 70, None),
    ("hk:h-z3", "heart_rate", "zepp_helio", "bridge", "2026-06-10 09:00", "2026-06-10 09:00", 81, None),
    ("hk:h-a1", "heart_rate", "apple_watch_ultra", "bridge", "2026-06-10 07:30", "2026-06-10 07:30", 65, None),
    ("hk:h-a2", "heart_rate", "apple_watch_ultra", "bridge", "2026-06-10 08:30", "2026-06-10 08:30", 66, None),
    ("hk:h-w1", "heart_rate", "whoop", "bridge", "2026-06-10 10:00", "2026-06-10 10:00", 75, None),
    ("hk:h-a3", "heart_rate", "apple_watch_ultra", "bridge", "2026-06-11 07:00", "2026-06-11 07:00", 72, None),
    ("hk:h-a4", "heart_rate", "apple_watch_ultra", "bridge", "2026-06-11 08:00", "2026-06-11 08:00", 74, None),
    # resting_hr (last): distinct starts, so the latest start wins under every tie rule
    ("hk:r-a1", "resting_hr", "apple_watch_ultra", "bridge", "2026-06-10 06:00", "2026-06-10 06:00", 79, None),
    ("hk:r-a2", "resting_hr", "apple_watch_ultra", "bridge", "2026-06-10 12:00", "2026-06-10 12:00", 81, None),
    ("hk:r-a3", "resting_hr", "apple_watch_ultra", "bridge", "2026-06-10 21:00", "2026-06-10 21:00", 76, None),
    ("hk:r-w1", "resting_hr", "whoop", "bridge", "2026-06-10 05:00", "2026-06-10 05:00", 58, None),
    ("hk:r-a4", "resting_hr", "apple_watch_ultra", "bridge", "2026-06-11 09:00", "2026-06-11 09:00", 60, None),
    # sleep: Whoop's API night ends on D0; Apple's staged nights lie wholly after midnight, contiguous, over 3 h
    ("wh:sleep_duration:sleep:1001", "sleep_duration", "whoop", "whoop_live", "2026-06-09 23:00", "2026-06-10 06:30", 7.25, None),
    ("hk:l-a1", "sleep_analysis", "apple_watch_ultra", "bridge", "2026-06-10 00:30", "2026-06-10 02:30", 120, "core"),
    ("hk:l-a2", "sleep_analysis", "apple_watch_ultra", "bridge", "2026-06-10 02:30", "2026-06-10 03:30", 60, "deep"),
    ("hk:l-a3", "sleep_analysis", "apple_watch_ultra", "bridge", "2026-06-10 03:30", "2026-06-10 05:00", 90, "rem"),
    ("hk:l-a4", "sleep_analysis", "apple_watch_ultra", "bridge", "2026-06-10 05:00", "2026-06-10 07:00", 120, "core"),
    ("hk:l-a5", "sleep_analysis", "apple_watch_ultra", "bridge", "2026-06-10 00:00", "2026-06-10 07:30", 450, "in_bed"),  # never sleep
    ("hk:l-a6", "sleep_analysis", "apple_watch_ultra", "bridge", "2026-06-11 00:00", "2026-06-11 04:00", 240, "core"),
]

# Worked out by hand. Grade: 0.35 rank (1.0 first, 0.7 second) + 0.25 freshness
# (1.0 on a past day) + 0.2 coverage (1.0 for a sum, else min(1, n/3)) + 0.2
# agreement (share of others within 12 percent, 0.6 with none); A >= 0.85, B >= 0.7.
EXPECTED = {   # (metric, day): (value, device_key, n_samples, corroboration, confidence, grade)
    ("steps", D0): (1300.0, "apple_watch_ultra", 3, {"iphone": 525.0}, 0.8, "B"),       # 1000+234+66; 500+25 is 60% off
    ("steps", D1): (800.0, "iphone", 1, None, 0.815, "B"),                              # .245+.25+.2+.12
    ("heart_rate", D0): (70.333, "zepp_helio", 3, {"apple_watch_ultra": 65.5, "whoop": 75.0}, 1.0, "A"),   # 211/3; both within 7%
    ("heart_rate", D1): (73.0, "apple_watch_ultra", 2, None, 0.748, "B"),               # .245+.25+.2*2/3+.12
    ("resting_hr", D0): (76.0, "apple_watch_ultra", 3, {"whoop": 58.0}, 0.8, "B"),      # 21:00 is the latest; 58 is 24% off
    ("resting_hr", D1): (60.0, "apple_watch_ultra", 1, None, 0.787, "B"),               # .35+.25+.2/3+.12
}
SLEEP_EXPECTED = {   # day: (value, device_key, corroboration); 120+60+90+120 min = 6.5 h, 240 min = 4.0 h
    D0: (7.25, "whoop", {"apple_watch_ultra": 6.5}),
    D1: (4.0, "apple_watch_ultra", None),
}


def _policy(**patch) -> MetricPolicy:
    metrics = copy.deepcopy(METRICS)
    for metric, keys in patch.items():
        metrics[metric] = {**metrics[metric], **keys}
    return MetricPolicy({"metrics": metrics, "confidence": copy.deepcopy(CONFIDENCE)}, default_tz="Asia/Dubai")


def _insert(conn, rows) -> None:
    db.insert_batch(conn, "INSERT INTO samples (sample_id, metric, device_key, sync_path, start_ts, end_ts, value, text_value, "
                          "source_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [[sid, m, dk, path, _t(s), _t(e), v, tv, f"Synthetic {dk}"] for sid, m, dk, path, s, e, v, tv in rows])


def _store(path=None):
    conn = db.connect(path) if path else db.connect_memory()
    policy = _policy()
    policy.sync_registry(conn)
    _insert(conn, ROWS)
    return conn, policy


def _cols(conn, table) -> list[str]:
    return [r[0] for r in conn.execute(f"DESCRIBE {table}").fetchall()]


def _tables(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' "
                                       "AND table_type = 'BASE TABLE' ORDER BY 1").fetchall()]


def _pk(conn, table):
    rows = conn.execute("SELECT constraint_column_names FROM duckdb_constraints() WHERE table_name = ? "
                        "AND constraint_type = 'PRIMARY KEY'", [table]).fetchall()
    return list(rows[0][0]) if rows else None


def _snapshot(conn) -> dict[str, list[str]]:
    """Every row of every base table except schema_version (checked on its
    own), on the v3 columns: daily_values.detail is left out."""
    out = {}
    for t in _tables(conn):
        if t == "schema_version":
            continue
        cols = ", ".join(f'"{c}"' for c in _cols(conn, t) if (t, c) != ("daily_values", "detail"))
        out[t] = sorted(repr(r) for r in conn.execute(f"SELECT {cols} FROM {t}").fetchall())
    return out


# ---- schema v4 ----

OLD_STAMP = datetime(2026, 6, 5, 9, 0)


def _v3_store(path) -> None:
    """A store in the v3 shape with rows in it. v4 only adds daily_values.detail,
    the device_baselines table and the version-4 row, so a v4 store filled by the
    real writers and then stripped of exactly those three is a v3 store."""
    conn, policy = _store(path)
    compute_daily_values(conn, policy, SourceRegistry(), D0, D1, as_of=AS_OF)
    db.execute(conn, "INSERT INTO baselines (date, metric, window_days, median, mad, n_days) VALUES "
                     "(?, 'steps', 30, 1250.0, 120.5, 21), (?, 'heart_rate', 30, 66.0, 2.0, 25)", [D1, D1])
    db.execute(conn, "INSERT INTO signals (date, metric, state, value, unit, device_key, why) "
                     "VALUES (?, 'steps', 'neutral', 1300.0, 'count', 'apple_watch_ultra', 'synthetic')", [D0])
    db.execute(conn, "INSERT INTO events (event_id, kind, ts, payload) VALUES ('e1', 'note', ?, '{\"text\": \"synthetic\"}')",
               [_t("2026-06-10 12:00")])
    conn.execute("DROP TABLE device_baselines")
    conn.execute("ALTER TABLE daily_values DROP COLUMN detail")
    conn.execute("DELETE FROM schema_version WHERE version = 4")
    conn.execute("UPDATE schema_version SET applied_at = ?", [OLD_STAMP])
    conn.close()


DV_VALUES = ("SELECT date, metric, value, unit, device_key, n_samples, confidence, grade, corroboration FROM daily_values "
             "WHERE metric <> 'sleep_duration' ORDER BY 1, 2")      # sleep rows are group A's to change


def test_a_v3_store_upgrades_in_place_to_v4_and_keeps_every_row(tmp_path):
    path = tmp_path / "v3.duckdb"
    _v3_store(path)
    raw = duckdb.connect(str(path))
    assert "detail" not in _cols(raw, "daily_values") and "device_baselines" not in _tables(raw)
    assert raw.execute("SELECT version, applied_at FROM schema_version ORDER BY 1").fetchall() == [(2, OLD_STAMP), (3, OLD_STAMP)]
    before = _snapshot(raw)
    assert len(before["daily_values"]) == 8 and len(before["baselines"]) == 2 and len(before["samples"]) == len(ROWS)
    dv_before = raw.execute(DV_VALUES).fetchall()
    raw.close()
    v4_stamp = None
    for _ in range(2):                       # the upgrade, then a second start that changes nothing
        conn = db.connect(path)
        assert _cols(conn, "daily_values")[-1] == "detail"
        assert conn.execute("SELECT COUNT(*), COUNT(detail) FROM daily_values").fetchone() == (8, 0)    # NULL, never 'null'
        assert _cols(conn, "device_baselines") == ["date", "metric", "window_days", "device_key", "median", "mad", "n_days"]
        assert _pk(conn, "device_baselines") == ["date", "metric", "window_days", "device_key"]
        assert _pk(conn, "baselines") == ["date", "metric", "window_days"]          # unchanged
        assert _cols(conn, "baselines") == ["date", "metric", "window_days", "median", "mad", "n_days"]
        after = _snapshot(conn)
        assert after.pop("device_baselines") == []
        assert after == before                                                      # every row of every table kept
        versions = conn.execute("SELECT version, applied_at, note FROM schema_version ORDER BY 1").fetchall()
        assert [v[:2] for v in versions[:2]] == [(2, OLD_STAMP), (3, OLD_STAMP)]     # earlier stamps kept
        assert versions[2][0] == 4 and versions[2][1] > OLD_STAMP and "device_baselines" in versions[2][2]
        assert v4_stamp in (None, versions[2][1])
        v4_stamp = versions[2][1]
        conn.close()
    # The upgraded store works with this code: the same values again, detail written as NULL.
    conn = db.connect(path)
    compute_daily_values(conn, _policy(), SourceRegistry(), D0, D1, as_of=AS_OF)
    assert conn.execute(DV_VALUES).fetchall() == dv_before and len(dv_before) == 6
    assert conn.execute("SELECT COUNT(detail) FROM daily_values WHERE metric <> 'sleep_duration'").fetchone() == (0,)
    conn.close()


def test_a_store_recorded_at_a_newer_version_still_opens(tmp_path):
    """No newer-version refusal (design 1.0 point 1): a code rollback runs older
    code on a newer, additive store (the Wave 1 code on a v4 store, this code on
    a future one), so init_schema accepts a version it does not know, a column
    and a table it does not know."""
    path = tmp_path / "newer.duckdb"
    conn = db.connect(path)
    conn.execute("INSERT INTO schema_version (version, note) VALUES (5, 'a future additive version')")
    conn.execute("ALTER TABLE daily_values ADD COLUMN future_note VARCHAR")
    conn.execute("CREATE TABLE future_table (x INTEGER)")
    conn.close()
    conn, policy = _store(path)
    assert compute_daily_values(conn, policy, SourceRegistry(), D0, D1, as_of=AS_OF) == 8
    assert [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY 1").fetchall()] == [2, 3, 4, 5]
    conn.close()


# ---- policy keys ----

WELL_FORMED = {
    "resting_hr": {"priority": ["whoop", "apple_watch_ultra", "zepp_helio"], "sync_paths": {"whoop": ["whoop_live"]},
                   "day_basis": "interval_midpoint"},
    "sleep_duration": {"priority": ["whoop", "whoop:healthkit", "apple_watch_ultra", "zepp_helio"],
                       "sync_paths": {"whoop": ["whoop_live"]}, "day_basis": "sleep_end", "baseline_scope": "source"},
    "respiratory_rate": {"day_basis": "sleep_end", "sync_paths": {"whoop": ["whoop_live"]}, "corroboration": ["whoop:healthkit"]},
    "strain": {"day_basis": "whoop_cycle"},
    "steps": {"merge": "interval", "day_basis": "calendar"},
    "spo2": {"corroboration": ["whoop", "zepp_helio"]},
    "hrv_sdnn": {"corroboration": []},
    "glucose_cgm": {"unit": "mg/dL", "priority": ["test_cgm"], "derive": {"from": "glucose", "devices": ["test_cgm"]},
                    "coverage": {"slot_min": 15, "min_fraction": 0.7}, "direction": "band", "optional": True},
}


def _shipped_with(**metrics) -> dict:
    """The shipped default merged with the fixture overlay, with keys patched into metrics."""
    cfg = load_metric_policy()
    for metric, keys in metrics.items():
        cfg["metrics"][metric] = {**cfg["metrics"].get(metric, {}), **keys}
    return cfg


def test_every_new_key_is_accepted_in_a_merged_policy_and_in_an_overlay_patch():
    p = MetricPolicy(_shipped_with(**copy.deepcopy(WELL_FORMED)), default_tz="Asia/Dubai")
    assert p.day_basis("resting_hr") == "interval_midpoint" and p.day_basis("strain") == "whoop_cycle"
    assert p.sync_paths("resting_hr") == {"whoop": ["whoop_live"]} and p.sync_paths("heart_rate") == {}
    assert p.merge("steps") == "interval" and p.merge("heart_rate") is None
    assert p.derive("glucose_cgm") == {"from": "glucose", "devices": ["test_cgm"]} and p.derive("glucose") is None
    assert p.corroboration("hrv_sdnn") == [] and p.corroboration("respiratory_rate") == ["whoop:healthkit"]
    assert p.corroboration("heart_rate") is None                      # absent: the pre-Wave-2 behaviour
    assert p.rank("sleep_duration", "whoop:healthkit") == 1           # a qualified key arbitrates as a key of its own
    eff = p.effective("resting_hr")
    assert eff["sync_paths"] == {"whoop": ["whoop_live"]} and eff["merge"] is None and eff["day_basis"] == "interval_midpoint"
    assert p.effective("glucose_cgm")["coverage"] == {"slot_min": 15, "min_fraction": 0.7}
    assert p.effective("sleep_duration")["baseline_scope"] == "source"
    # The same keys as an overlay patch (the overlay is where the priority lists live).
    validate_policy({"metrics": copy.deepcopy(WELL_FORMED)}, strict=False)
    assert split_device_key("whoop:healthkit") == ("whoop", "healthkit") and split_device_key("whoop") == ("whoop", None)
    assert base_device("whoop:healthkit") == base_device("whoop") == "whoop"


def test_interval_midpoint_reads_as_calendar_for_so_far():
    """The new basis is inert in S0: the reporting today's value is "so far"
    exactly as on calendar (D7), unlike the night bases."""
    for metric in ("steps", "resting_hr", "heart_rate"):
        cal = _policy(**{metric: {"day_basis": "calendar"}}).running_total(metric)
        assert cal is True
        assert _policy(**{metric: {"day_basis": "interval_midpoint"}}).running_total(metric) is cal
        assert _policy(**{metric: {"day_basis": "sleep_end"}}).running_total(metric) is False


@pytest.mark.parametrize("metric, keys, expect", [
    ("resting_hr", {"day_basis": "midpoint"}, "metrics.resting_hr.day_basis: 'midpoint' is not one of"),
    ("resting_hr", {"priority": ["whoop:apple", "apple_watch_ultra"]}, "'whoop:apple' has an unknown qualifier 'apple'"),
    ("spo2", {"corroboration": ["zepp_helio:healthkit:x"]}, "unknown qualifier 'healthkit:x'"),
    ("resting_hr", {"priority": [":healthkit"]}, "':healthkit' has no device before the colon"),
    ("resting_hr", {"sync_paths": ["whoop_live"]}, "metrics.resting_hr.sync_paths: ['whoop_live'] is not of type 'object'"),
    ("resting_hr", {"sync_paths": {"whoop": "whoop_live"}}, "metrics.resting_hr.sync_paths.whoop: 'whoop_live' is not of type 'array'"),
    ("resting_hr", {"sync_paths": {"whoop": []}}, "metrics.resting_hr.sync_paths.whoop: []"),
    ("resting_hr", {"sync_paths": {"whoop": ["whoop-live"]}}, "'whoop-live' is not one of"),
    ("resting_hr", {"sync_paths": {"whoop:healthkit": ["bridge"]}}, "sync_paths takes plain device keys"),
    ("glucose", {"derive": {"devices": ["test_cgm"]}}, "metrics.glucose.derive: 'from' is a required property"),
    ("glucose", {"derive": {"from": "glucose", "devices": []}}, "metrics.glucose.derive.devices: []"),
    ("glucose", {"derive": {"from": "glucose", "devices": ["test_cgm:healthkit"]}}, "derive.devices takes plain device keys"),
    ("steps", {"merge": "union"}, "metrics.steps.merge: 'union' is not one of"),
])
def test_a_malformed_new_key_is_refused_with_its_path(metric, keys, expect):
    with pytest.raises(PolicyError) as e:
        MetricPolicy(_shipped_with(**{metric: keys}), default_tz="Asia/Dubai")
    assert any(expect in p for p in e.value.problems), e.value.problems
    with pytest.raises(PolicyError) as e:                          # an overlay patch is refused the same way
        validate_policy({"metrics": {metric: keys}}, strict=False)
    assert any(expect in p for p in e.value.problems), e.value.problems


# ---- the dispatcher, the corroboration helper and the detail column ----

def test_the_dispatcher_routes_by_policy(monkeypatch):
    seen: list[tuple[str, str]] = []

    def spy(name):
        real = getattr(bl, name)

        def run(conn, policy, metric, start, end):
            seen.append((name, metric))
            return real(conn, policy, metric, start, end)
        return run
    for name in ("_rows_sleep", "_rows_generic", "_rows_merged", "_rows_derived"):
        monkeypatch.setattr(bl, name, spy(name))
    conn, _ = _store()
    policy = _policy(steps={"merge": "interval"}, heart_rate={"derive": {"from": "resting_hr", "devices": ["apple_watch_ultra"]}},
                     resting_hr={"merge": "interval", "derive": {"from": "heart_rate", "devices": ["zepp_helio"]}})
    route = {"sleep_duration": "_rows_sleep", "steps": "_rows_merged", "heart_rate": "_rows_derived",
             "resting_hr": "_rows_derived"}                       # derive before merge
    for metric, name in route.items():
        seen.clear()
        rows = bl._metric_day_rows(conn, policy, metric, D0, D1)
        assert seen[0] == (name, metric)
        assert all(len(r) == 5 for r in rows)
    seen.clear()
    rows = bl._metric_day_rows(conn, _policy(), "resting_hr", D0, D1)
    assert seen == [("_rows_generic", "resting_hr")]
    assert sorted(rows) == [(D0, "apple_watch_ultra", 76.0, 3, None), (D0, "whoop", 58.0, 1, None),
                            (D1, "apple_watch_ultra", 60.0, 1, None)]
    seen.clear()
    assert bl._metric_day_rows(conn, _policy(steps={"priority": []}), "steps", D0, D1) == [] and seen == []


def test_the_dispatcher_gives_the_pre_wave2_daily_values():
    conn, policy = _store()
    assert compute_daily_values(conn, policy, SourceRegistry(), D0, D1, as_of=AS_OF) == 8
    got = {(m, d): (v, dk, n, json.loads(co) if co else None, cf, g, de) for d, m, v, dk, n, cf, g, co, de in db.fetchall(
        conn, "SELECT date, metric, value, device_key, n_samples, confidence, grade, corroboration, detail FROM daily_values")}
    assert set(got) == set(EXPECTED) | {("sleep_duration", d) for d in SLEEP_EXPECTED}
    for key, (v, dk, n, co, cf, g) in EXPECTED.items():
        assert got[key][:4] == (v, dk, n, co), key
        assert got[key][4] == pytest.approx(cf, abs=1e-9) and got[key][5] == g, key
        assert got[key][6] is None, key
    for d, (v, dk, co) in SLEEP_EXPECTED.items():
        g = got[("sleep_duration", d)]
        assert (g[0], g[1], g[3]) == (v, dk, co), d


def test_others_keeps_every_other_key_present():
    policy = _policy()
    assert bl._row_keys(policy, "heart_rate") == ["zepp_helio", "apple_watch_ultra", "whoop"]   # no corroboration key: the list
    present = {"zepp_helio": (70.333, 3, None), "apple_watch_ultra": (65.5, 2, None), "whoop": (75.0, 1, None)}
    assert bl._others(policy, "heart_rate", "zepp_helio", present) == {"apple_watch_ultra": 65.5, "whoop": 75.0}
    assert bl._others(policy, "heart_rate", "whoop", {"whoop": (75.0, 1, None)}) == {}


def test_detail_round_trips_none_and_counts_as_a_changed_field(monkeypatch):
    conn, policy = _store()
    reg = SourceRegistry()
    compute_daily_values(conn, policy, reg, D0, D1, as_of=AS_OF)
    generic = "metric <> 'sleep_duration'"
    assert db.fetchall(conn, f"SELECT COUNT(*), COUNT(detail) FROM daily_values WHERE {generic}") == [(6, 0)]
    real = bl._rows_generic
    box = {"detail": {"z": 1, "a": {"day": D0}}}

    def with_detail(c, p, metric, start, end):
        return [(d, k, v, n, box["detail"] if (metric, d, k) == ("steps", D0, "apple_watch_ultra") else de)
                for d, k, v, n, de in real(c, p, metric, start, end)]
    monkeypatch.setattr(bl, "_rows_generic", with_detail)

    def run() -> tuple[set, object]:
        changed: set = set()
        compute_daily_values(conn, policy, reg, D0, D1, as_of=AS_OF, changed=changed)
        return changed, db.fetchall(conn, "SELECT detail FROM daily_values WHERE metric = 'steps' AND date = ?", [D0])[0][0]

    assert run() == ({D0}, '{"a": {"day": "2026-06-10"}, "z": 1}')   # sorted keys, ISO dates; only that row changed
    assert run() == (set(), '{"a": {"day": "2026-06-10"}, "z": 1}')   # the same facts: not a change
    box["detail"] = None
    assert run() == ({D0}, None)                                       # back to NULL, journaled as a change
    assert db.fetchall(conn, f"SELECT COUNT(detail) FROM daily_values WHERE {generic}") == [(0,)]


# ---- the episode interface (group A fills it in) ----

def test_the_episode_interface_stub_has_the_agreed_fields():
    assert [f.name for f in fields(episodes.Episode)] == [
        "device_key", "start", "end", "wake_date", "asleep_h", "deep_min", "rem_min", "core_min", "asleep_plain_min",
        "awake_min", "in_bed_start", "in_bed_end", "in_bed_h", "n_rows", "overlap_removed_min"]
    assert episodes.Episode.__dataclass_params__.frozen
    assert episodes.EPISODE_GAP_MIN == 60 and episodes.MIN_MAIN_HOURS == 3.0
    assert callable(episodes.main_sleep_episodes) and callable(episodes.point_wake_dates)
