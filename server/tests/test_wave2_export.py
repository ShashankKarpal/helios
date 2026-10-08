"""Wave 2 group D (fix program design B9, B13 and B15 raw points): export twins
of Bridge rows linked one to one and taken out of eligibility, the owner's D5
list, and the raw point query.

Self-contained policy (not the shipped files, which other Wave 2 groups edit).
Every expected value below is worked out by hand from the synthetic rows.
Synthetic data only: invented source names, round instants, plausible values."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta

import pytest

from heliosd.migrate import export_relink as xr
from heliosd.signals.baselines import compute_daily_values
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

METRICS = {
    "steps": {"hk": "HKQuantityTypeIdentifierStepCount", "unit": "count", "agg": "sum", "priority": ["apple_watch_ultra", "iphone"]},
    "heart_rate": {"hk": "HKQuantityTypeIdentifierHeartRate", "unit": "count/min", "agg": "avg", "priority": ["apple_watch_ultra"]},
    "spo2": {"hk": "HKQuantityTypeIdentifierOxygenSaturation", "unit": "%", "agg": "avg", "priority": ["apple_watch_ultra", "whoop"]},
    "dietary_energy": {"hk": "HKQuantityTypeIdentifierDietaryEnergyConsumed", "unit": "kcal", "agg": "sum",
                       "priority": ["myfitnesspal"]},
    "active_energy": {"hk": "HKQuantityTypeIdentifierActiveEnergyBurned", "unit": "kcal", "agg": "sum",
                      "priority": ["apple_watch_ultra"]},
    "bmi": {"hk": "HKQuantityTypeIdentifierBodyMassIndex", "unit": "count", "agg": "last", "priority": ["zepp_life_scale"]},
    "body_mass": {"hk": "HKQuantityTypeIdentifierBodyMass", "unit": "kg", "agg": "last", "priority": ["zepp_life_scale"]},
}
DAY = date(2026, 5, 3)
AS_OF = date(2026, 5, 10)
DUBAI = timedelta(hours=4)
SRC = {"apple_watch_ultra": "Synthetic Ultra", "iphone": "Synthetic Phone", "myfitnesspal": "Synthetic Food Log",
       "whoop": "Synthetic Strap", "zepp_life_scale": "Synthetic Scale"}


def _policy(**extra) -> MetricPolicy:
    metrics = copy.deepcopy(METRICS)
    metrics.update(copy.deepcopy(extra))
    return MetricPolicy({"metrics": metrics}, default_tz="Asia/Dubai")


def _utc(hhmm: str, day: date = DAY) -> datetime:
    """A UTC instant on `day` (the Dubai wall is four hours later, same day for these hours)."""
    h, m = (int(p) for p in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m)


def _add(conn, sid, metric, device, path, start, value, unit, end=None, text=None, source=None, unit_rule=None,
         quality=None):
    """One samples row as the 1b-migrated store holds it: UTC instants and the Dubai wall beside them."""
    end = end or start
    ts = {"bridge": "bridge_utc", "health_export": "era_rebase_v1"}.get(path, "whoop_api")
    db.execute(conn, "INSERT INTO samples (sample_id, hk_uuid, metric, value, text_value, unit, start_ts, end_ts, start_utc, "
                     "end_utc, source_name, device_key, sync_path, time_source, unit_rule, quality) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               [sid, sid[3:] if sid.startswith("hk:") else None, metric, value, text, unit, start + DUBAI, end + DUBAI,
                start, end, source or SRC[device], device, path, ts, unit_rule, quality])


def _store(policy=None):
    conn = db.connect_memory()
    (policy or _policy()).sync_registry(conn)
    return conn


def _quality(conn, sid):
    return db.fetchall(conn, "SELECT quality FROM samples WHERE sample_id = ?", [sid])[0][0]


def _eligible(conn, sid) -> bool:
    return db.fetchall(conn, "SELECT COUNT(*) FROM eligible_samples WHERE sample_id = ?", [sid])[0][0] == 1


def _aliases(conn):
    return sorted(db.fetchall(conn, "SELECT old_id, new_id, reason FROM sample_aliases ORDER BY 1, 2"))


def _day_value(conn, policy, metric, day=DAY):
    compute_daily_values(conn, policy, SourceRegistry(), day, day, as_of=AS_OF)
    rows = db.fetchall(conn, "SELECT value FROM daily_values WHERE metric = ? AND date = ?", [metric, day])
    return rows[0][0] if rows else None


# ---------------------------------------------------------------- B9

def test_twin_within_rounding_is_linked_once():
    policy = _policy()
    conn = _store(policy)
    # The export printed 2680.57; the Bridge row is the same entry at full precision.
    _add(conn, "hk:d-1", "dietary_energy", "myfitnesspal", "bridge", _utc("08:00"), 2680.57421875, "kcal")
    _add(conn, "xp:d-1", "dietary_energy", "myfitnesspal", "health_export", _utc("08:00"), 2680.57, "kcal")
    assert _day_value(conn, policy, "dietary_energy") == 5361.144          # counted twice before the link
    out = xr.relink(conn, code_commit="test")
    assert out["counts"]["linked"] == 1 and out["counts"]["eligible_export_rows_after"] == 0
    assert _quality(conn, "xp:d-1") == "export_duplicate" and not _eligible(conn, "xp:d-1")
    assert _quality(conn, "hk:d-1") is None and _eligible(conn, "hk:d-1")
    assert _aliases(conn) == [("xp:d-1", "hk:d-1", "export_link_v2")]
    assert _day_value(conn, policy, "dietary_energy") == 2680.574          # once


def test_multi_entry_group_links_one_to_one():
    conn = _store()
    # Three entries of one meal time (two identical) and two export rows printed at 2 to 3 decimals.
    for sid, v in (("hk:m-1", 100.0), ("hk:m-2", 100.0), ("hk:m-3", 250.0)):
        _add(conn, sid, "dietary_energy", "myfitnesspal", "bridge", _utc("09:00"), v, "kcal")
    _add(conn, "xp:m-1", "dietary_energy", "myfitnesspal", "health_export", _utc("09:00"), 100.004, "kcal")
    _add(conn, "xp:m-2", "dietary_energy", "myfitnesspal", "health_export", _utc("09:00"), 250.003, "kcal")
    # Fewer export rows than Bridge rows, the export's value the larger one: ranks alone would pair 250.003 with 100.0.
    _add(conn, "hk:r-1", "dietary_energy", "myfitnesspal", "bridge", _utc("12:00"), 100.0, "kcal")
    _add(conn, "hk:r-2", "dietary_energy", "myfitnesspal", "bridge", _utc("12:00"), 250.0, "kcal")
    _add(conn, "xp:r-1", "dietary_energy", "myfitnesspal", "health_export", _utc("12:00"), 250.003, "kcal")
    out = xr.relink(conn, code_commit="test")
    assert out["counts"]["linked"] == 3
    assert _aliases(conn) == [("xp:m-1", "hk:m-1", "export_link_v2"), ("xp:m-2", "hk:m-3", "export_link_v2"),
                              ("xp:r-1", "hk:r-2", "export_link_v2")]
    assert all(_eligible(conn, b) for b in ("hk:m-1", "hk:m-2", "hk:m-3", "hk:r-1", "hk:r-2"))
    assert not any(_eligible(conn, x) for x in ("xp:m-1", "xp:m-2", "xp:r-1"))
    (cell,) = out["by_metric_device"]
    assert cell["metric"] == "dietary_energy" and cell["linked"] == 3 and cell["max_difference"] == 0.004


def test_unmatched_export_row_stays_eligible_and_listed():
    conn = _store()
    # No Bridge row at the export row's instants (the watch cut the hour differently).
    _add(conn, "hk:a-1", "active_energy", "apple_watch_ultra", "bridge", _utc("10:01"), 30.0, "kcal", end=_utc("11:00"))
    _add(conn, "xp:a-1", "active_energy", "apple_watch_ultra", "health_export", _utc("10:00"), 30.0, "kcal", end=_utc("11:00"))
    # Same instants, values too far apart; steps must be exact.
    _add(conn, "hk:h-1", "heart_rate", "apple_watch_ultra", "bridge", _utc("10:00"), 70.0, "count/min")
    _add(conn, "xp:h-1", "heart_rate", "apple_watch_ultra", "health_export", _utc("10:00"), 70.5, "count/min")
    _add(conn, "hk:s-1", "steps", "apple_watch_ultra", "bridge", _utc("11:00"), 120.0, "count", end=_utc("11:05"))
    _add(conn, "xp:s-1", "steps", "apple_watch_ultra", "health_export", _utc("11:00"), 121.0, "count", end=_utc("11:05"))
    _add(conn, "hk:s-2", "steps", "apple_watch_ultra", "bridge", _utc("12:00"), 80.0, "count", end=_utc("12:05"))
    _add(conn, "xp:s-2", "steps", "apple_watch_ultra", "health_export", _utc("12:00"), 80.0, "count", end=_utc("12:05"))
    # A Bridge row Phase 1b already linked to one export row: a second export copy never links to it.
    _add(conn, "hk:h-2", "heart_rate", "apple_watch_ultra", "bridge", _utc("13:00"), 64.0, "count/min")
    _add(conn, "xp:h-2a", "heart_rate", "apple_watch_ultra", "health_export", _utc("13:00"), 64.0, "count/min",
         quality="export_duplicate")
    db.execute(conn, "INSERT INTO sample_aliases (old_id, new_id, reason) VALUES ('xp:h-2a', 'hk:h-2', 'export_link_v1')")
    _add(conn, "xp:h-2b", "heart_rate", "apple_watch_ultra", "health_export", _utc("13:00"), 64.0, "count/min")
    # Another source with the same device key is another sample.
    _add(conn, "hk:h-3", "heart_rate", "apple_watch_ultra", "bridge", _utc("14:00"), 66.0, "count/min")
    _add(conn, "xp:h-3", "heart_rate", "apple_watch_ultra", "health_export", _utc("14:00"), 66.0, "count/min",
         source="Synthetic Ultra (renamed)")
    out = xr.relink(conn, code_commit="test")
    assert out["counts"]["linked"] == 1 and _aliases(conn)[-1] == ("xp:s-2", "hk:s-2", "export_link_v2")
    for sid in ("xp:a-1", "xp:h-1", "xp:s-1", "xp:h-2b", "xp:h-3"):
        assert _quality(conn, sid) is None and _eligible(conn, sid), sid
    assert out["counts"]["eligible_export_rows_before"] == 6 and out["counts"]["eligible_export_rows_after"] == 5
    assert out["counts"]["unlinked_by_reason"] == {"no_bridge_row_at_the_same_instants": 2, "bridge_row_already_linked": 1,
                                                   "no_value_within_tolerance": 2}
    cells = {(c["metric"], c["device_key"]): (c["linked"], c["unlinked"]) for c in out["by_metric_device"]}
    assert cells == {
        ("active_energy", "apple_watch_ultra"): (0, {"no_bridge_row_at_the_same_instants": 1, "bridge_row_already_linked": 0,
                                                     "no_value_within_tolerance": 0}),
        ("heart_rate", "apple_watch_ultra"): (0, {"no_bridge_row_at_the_same_instants": 1, "bridge_row_already_linked": 1,
                                                  "no_value_within_tolerance": 1}),
        ("steps", "apple_watch_ultra"): (1, {"no_bridge_row_at_the_same_instants": 0, "bridge_row_already_linked": 0,
                                             "no_value_within_tolerance": 1})}
    stored = json.loads(db.fetchall(conn, "SELECT summary FROM migrations WHERE name = ?", [xr.MIGRATION])[0][0])
    assert stored["by_metric_device"] == out["by_metric_device"]          # the list lives in the migrations row


def test_a_unit_rule_marker_alone_does_not_block_the_link():
    conn = _store()
    # Whoop's SpO2: the Bridge copy was normalised (frac_to_pct_v1), the export printed the same percent.
    _add(conn, "hk:o-1", "spo2", "whoop", "bridge", _utc("02:00"), 97.00004, "%", unit_rule="frac_to_pct_v1")
    _add(conn, "xp:o-1", "spo2", "whoop", "health_export", _utc("02:00"), 97.0, "%")
    # A value in another scale never falls inside the tolerance.
    _add(conn, "hk:o-2", "spo2", "whoop", "bridge", _utc("03:00"), 96.0, "%", unit_rule="frac_to_pct_v1")
    _add(conn, "xp:o-2", "spo2", "whoop", "health_export", _utc("03:00"), 0.96, "%")
    out = xr.relink(conn, code_commit="test")
    assert out["counts"]["linked"] == 1 and _aliases(conn) == [("xp:o-1", "hk:o-1", "export_link_v2")]
    assert _eligible(conn, "xp:o-2")


def test_relink_is_idempotent_and_verified(tmp_path):
    path = tmp_path / "store.duckdb"
    conn = db.connect(path)
    _policy().sync_registry(conn)
    _add(conn, "hk:d-1", "dietary_energy", "myfitnesspal", "bridge", _utc("08:00"), 410.25, "kcal")
    _add(conn, "xp:d-1", "dietary_energy", "myfitnesspal", "health_export", _utc("08:00"), 410.2512, "kcal")
    _add(conn, "xp:d-2", "dietary_energy", "myfitnesspal", "health_export", _utc("08:30"), 90.0, "kcal")
    first = xr.relink(conn, code_commit="test")
    assert first["phase"] == "verified" and all(first["checks"].values()) and first["counts"]["linked"] == 1
    snap = (db.fetchall(conn, "SELECT sample_id, quality FROM samples ORDER BY 1"), _aliases(conn),
            db.fetchall(conn, "SELECT name, applied_at, summary FROM migrations ORDER BY 1"))
    second = xr.relink(conn, code_commit="test")
    assert second == {"migration": xr.MIGRATION, "already_applied": True, "linked": 0, "would_link": 0}
    assert (db.fetchall(conn, "SELECT sample_id, quality FROM samples ORDER BY 1"), _aliases(conn),
            db.fetchall(conn, "SELECT name, applied_at, summary FROM migrations ORDER BY 1")) == snap
    conn.close()
    # The daemon's startup check passes: the row says verified.
    again = db.connect(path)
    assert db.migration_applied(again, xr.MIGRATION) and db.unverified_migrations(again) == []
    again.close()
