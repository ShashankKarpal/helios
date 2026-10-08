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
    # The export printed 642.38; the Bridge row is the same entry at full precision.
    _add(conn, "hk:d-1", "dietary_energy", "myfitnesspal", "bridge", _utc("08:00"), 642.37890625, "kcal")
    _add(conn, "xp:d-1", "dietary_energy", "myfitnesspal", "health_export", _utc("08:00"), 642.38, "kcal")
    assert _day_value(conn, policy, "dietary_energy") == 1284.759          # counted twice before the link
    out = xr.relink(conn, code_commit="test")
    assert out["counts"]["linked"] == 1 and out["counts"]["eligible_export_rows_after"] == 0
    assert _quality(conn, "xp:d-1") == "export_duplicate" and not _eligible(conn, "xp:d-1")
    assert _quality(conn, "hk:d-1") is None and _eligible(conn, "hk:d-1")
    assert _aliases(conn) == [("xp:d-1", "hk:d-1", "export_link_v2")]
    assert _day_value(conn, policy, "dietary_energy") == 642.379           # once


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


# ---------------------------------------------------------------- B13 (decision D5)

def _scale_rows(conn):
    """Phase 1b's ambiguous scale rows beside their Bridge rows (two candidates
    one way or the other), one ambiguous row of another metric, and an
    unmatched export row that stays eligible."""
    t1, t2 = _utc("03:00"), _utc("03:00", DAY + timedelta(days=7))
    for sid, metric, t, v, unit in (("hk:b-1", "bmi", t1, 31.2, "count"), ("hk:b-2", "bmi", t1, 31.2, "count"),
                                    ("hk:b-3", "bmi", t2, 31.0, "count"), ("hk:k-1", "body_mass", t1, 95.4, "kg"),
                                    ("hk:k-2", "body_mass", t1, 95.4, "kg")):
        _add(conn, sid, metric, "zepp_life_scale", "bridge", t, v, unit)
    for sid, metric, t, v, unit in (("xp:b-1", "bmi", t1, 31.2, "count"), ("xp:b-2", "bmi", t2, 31.0, "count"),
                                    ("xp:b-3", "bmi", t2, 31.0, "count"), ("xp:k-1", "body_mass", t1, 95.4, "kg")):
        _add(conn, sid, metric, "zepp_life_scale", "health_export", t, v, unit, quality="export_ambiguous")
    _add(conn, "xp:s-9", "steps", "apple_watch_ultra", "health_export", _utc("09:00"), 40.0, "count", end=_utc("09:10"),
         quality="export_ambiguous")
    _add(conn, "xp:a-9", "active_energy", "apple_watch_ultra", "health_export", _utc("10:00"), 12.0, "kcal", end=_utc("11:00"))


def test_ambiguous_rows_are_not_eligible():
    policy = _policy()
    conn = _store(policy)
    _scale_rows(conn)
    assert not any(_eligible(conn, s) for s in ("xp:b-1", "xp:b-2", "xp:b-3", "xp:k-1", "xp:s-9"))
    assert all(_eligible(conn, s) for s in ("hk:b-1", "hk:b-2", "hk:b-3", "hk:k-1", "hk:k-2", "xp:a-9"))
    assert _day_value(conn, policy, "bmi") == 31.2 and _day_value(conn, policy, "body_mass") == 95.4   # the Bridge rows fill the day


def test_d5_row_lists_the_ambiguous_scale_rows_and_changes_none():
    conn = _store()
    _scale_rows(conn)
    before = db.fetchall(conn, "SELECT * FROM samples ORDER BY sample_id")
    out = xr.record_d5_rows(conn, code_commit="test")
    assert out["phase"] == "verified" and all(out["checks"].values())
    assert out["decision"]["id"] == "D5" and out["decision"]["decided"] == "2026-10-08"
    assert [r["sample_id"] for r in out["rows"]] == ["xp:b-1", "xp:b-2", "xp:b-3", "xp:k-1"]
    assert out["by_metric_device"] == [
        {"metric": "bmi", "device_key": "zepp_life_scale", "n": 3, "first": "2026-05-03", "last": "2026-05-10"},
        {"metric": "body_mass", "device_key": "zepp_life_scale", "n": 1, "first": "2026-05-03", "last": "2026-05-03"}]
    assert out["eligible_bridge_rows_at_the_same_instants"] == [{"metric": "bmi", "bridge_rows": 3, "days": 2},
                                                                {"metric": "body_mass", "bridge_rows": 2, "days": 1}]
    assert out["export_ambiguous_outside_d5"] == [{"metric": "steps", "n": 1}]
    assert db.fetchall(conn, "SELECT * FROM samples ORDER BY sample_id") == before
    stored = json.loads(db.fetchall(conn, "SELECT summary FROM migrations WHERE name = 'wave2_d5_scale_rows'")[0][0])
    assert stored["rows"] == out["rows"] and db.migration_applied(conn, xr.D5_MIGRATION)
    assert xr.record_d5_rows(conn, code_commit="test") == {"migration": xr.D5_MIGRATION, "already_applied": True}


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from heliosd.config import Settings
    from heliosd.main import create_app
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><html><head></head><body></body></html>", encoding="utf-8")
    monkeypatch.setenv("HELIOS_WEB_DIST", str(dist))
    raw = {"server": {"ingest_token": "test-token-0123456789"}, "storage": {"db_path": str(tmp_path / "helios.duckdb")},
           "owner": {"timezone": "Asia/Dubai"}, "notifications": {"macos_alerts": False}}
    with TestClient(create_app(Settings(raw=raw))) as c:
        yield c


H = {"X-Helios-Token": "test-token-0123456789"}


def test_freshness_lists_unresolved_per_type(app_client):
    conn = app_client.app.state.conn
    _scale_rows(conn)
    _add(conn, "xp:a-8", "active_energy", "apple_watch_ultra", "health_export", _utc("11:00"), 15.0, "kcal", end=_utc("12:00"))
    _add(conn, "xp:a-7", "active_energy", "apple_watch_ultra", "health_export", _utc("12:00"), 9.0, "kcal", end=_utc("13:00"),
         quality="export_duplicate")                                   # linked: resolved, listed nowhere
    r = app_client.get("/api/freshness", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["unresolved"] == [
        {"metric": "active_energy", "device_key": "apple_watch_ultra", "export_ambiguous": 0, "export_unmatched": 2},
        {"metric": "bmi", "device_key": "zepp_life_scale", "export_ambiguous": 3, "export_unmatched": 0},
        {"metric": "body_mass", "device_key": "zepp_life_scale", "export_ambiguous": 1, "export_unmatched": 0},
        {"metric": "steps", "device_key": "apple_watch_ultra", "export_ambiguous": 1, "export_unmatched": 0}]


# ---------------------------------------------------------------- B15 raw points

def _points(conn):
    """Heart-rate points around DAY (Dubai walls in the comments)."""
    hr = ("heart_rate", "count/min")
    _add(conn, "hk:p-9", hr[0], "apple_watch_ultra", "bridge", _utc("19:59", DAY - timedelta(days=1)), 58.0, hr[1])  # 23:59 the day before
    _add(conn, "hk:p-2", hr[0], "apple_watch_ultra", "bridge", _utc("06:00"), 62.0, hr[1])        # 10:00, same instant as p-1
    _add(conn, "hk:p-1", hr[0], "apple_watch_ultra", "bridge", _utc("06:00"), 61.0, hr[1])        # 10:00
    _add(conn, "hk:p-3", hr[0], "zepp_helio", "bridge", _utc("19:30"), 70.0, hr[1], source="Synthetic Strap Arm")   # 23:30
    _add(conn, "hk:p-4", hr[0], "apple_watch_ultra", "bridge", _utc("20:30"), 66.0, hr[1])        # 00:30 the next day
    _add(conn, "hk:p-5", hr[0], "apple_watch_ultra", "bridge", _utc("06:00", DAY + timedelta(days=2)), 64.0, hr[1])
    # Not eligible: an excluded source, a flagged row, a linked export row.
    _add(conn, "hk:p-6", hr[0], "excluded", "bridge", _utc("07:00"), 200.0, hr[1], source="Synthetic Ignored App")
    _add(conn, "hk:p-7", hr[0], "apple_watch_ultra", "bridge", _utc("07:30"), 61.5, hr[1], quality="unit_mismatch")
    _add(conn, "xp:p-8", hr[0], "apple_watch_ultra", "health_export", _utc("06:00"), 61.0, hr[1], quality="export_duplicate")


def test_raw_points_route_returns_eligible_points(app_client):
    conn = app_client.app.state.conn
    _points(conn)

    def get(**params):
        return app_client.get("/api/samples", params=params, headers=H)
    r = get(metric="heart_rate", start=str(DAY), end=str(DAY + timedelta(days=1)))
    assert r.status_code == 200, r.text
    out = r.json()
    assert [p["id"] for p in out["points"]] == ["hk:p-1", "hk:p-2", "hk:p-3", "hk:p-4"]
    assert out["points"][0] == {"start": "2026-05-03T10:00:00+04:00", "end": "2026-05-03T10:00:00+04:00", "value": 61.0,
                                "text": None, "device": "apple_watch_ultra", "sync_path": "bridge", "id": "hk:p-1"}
    assert out["points"][3]["start"] == "2026-05-04T00:30:00+04:00"
    assert (out["count"], out["truncated"], out["days"], out["unit"], out["zone"]) == (4, False, 2, "count/min", "Asia/Dubai")
    one = get(metric="heart_rate", start=str(DAY), end=str(DAY + timedelta(days=1)), device="apple_watch_ultra").json()
    assert [p["id"] for p in one["points"]] == ["hk:p-1", "hk:p-2", "hk:p-4"] and one["devices"] == ["apple_watch_ultra"]
    cut = get(metric="heart_rate", start=str(DAY), end=str(DAY + timedelta(days=1)), limit=2).json()
    assert [p["id"] for p in cut["points"]] == ["hk:p-1", "hk:p-2"] and cut["truncated"] is True and cut["count"] == 2
    # Errors are errors, never an empty answer.
    for params, reason in ((dict(metric="nope", start=str(DAY), end=str(DAY)), "unknown metric"),
                           (dict(metric="heart_rate", start="2026-02-30", end=str(DAY)), "bad start date"),
                           (dict(metric="heart_rate", start=str(DAY), end=str(DAY + timedelta(days=92))), "at most 92 days"),
                           (dict(metric="heart_rate", start=str(DAY), end=str(DAY - timedelta(days=1))), "before start"),
                           (dict(metric="heart_rate", start=str(DAY), end=str(DAY), limit=0), "limit"),
                           (dict(metric="heart_rate", start=str(DAY), end=str(DAY), limit=10001), "limit"),
                           (dict(metric="heart_rate", start=str(DAY), end=str(DAY), device="whooop"), "unknown device")):
        r = get(**params)
        assert r.status_code == 400 and reason in r.json()["detail"], (params, r.text)
    assert get(metric="heart_rate", start=str(DAY), end=str(DAY + timedelta(days=91))).status_code == 200   # 92 days


def test_derived_metric_resolves_in_points_route(app_client):
    conn = app_client.app.state.conn
    policy = _policy(glucose={"hk": "HKQuantityTypeIdentifierBloodGlucose", "unit": "mg/dL", "priority": ["test_meter"]},
                     glucose_cgm={"unit": "mg/dL", "priority": ["test_cgm"],
                                  "derive": {"from": "glucose", "devices": ["test_cgm"]}})
    policy.sync_registry(conn)
    app_client.app.state.policy = policy
    for i, v in enumerate((101.0, 104.0, 99.0)):
        _add(conn, f"hk:g-c{i}", "glucose", "test_cgm", "bridge", _utc("05:00") + timedelta(minutes=15 * i), v, "mg/dL",
             source="TestCGM sensor")
    _add(conn, "hk:g-m1", "glucose", "test_meter", "bridge", _utc("05:10"), 97.0, "mg/dL", source="TestMeter strip")
    out = app_client.get("/api/samples", params={"metric": "glucose_cgm", "start": str(DAY), "end": str(DAY)}, headers=H).json()
    assert (out["metric"], out["source_metric"], out["devices"], out["unit"]) == ("glucose_cgm", "glucose", ["test_cgm"], "mg/dL")
    assert [(p["id"], p["value"]) for p in out["points"]] == [("hk:g-c0", 101.0), ("hk:g-c1", 104.0), ("hk:g-c2", 99.0)]
    parent = app_client.get("/api/samples", params={"metric": "glucose", "start": str(DAY), "end": str(DAY)}, headers=H).json()
    assert [p["id"] for p in parent["points"]] == ["hk:g-c0", "hk:g-m1", "hk:g-c1", "hk:g-c2"]   # the parent has every device
    r = app_client.get("/api/samples", params={"metric": "glucose_cgm", "start": str(DAY), "end": str(DAY), "device": "test_meter"},
                       headers=H)
    assert r.status_code == 400 and "not a source of 'glucose_cgm'" in r.json()["detail"]


def test_mcp_query_samples(monkeypatch):
    from heliosd.mcp_server import server as mcp_server
    seen = []

    class R:
        def __init__(self, code, body):
            self.status_code, self._body, self.text = code, body, json.dumps(body)

        def json(self):
            return self._body

    class C:
        def __init__(self, resp):
            self.resp = resp

        def get(self, path, params=None):
            seen.append((path, params))
            return self.resp
    body = {"metric": "heart_rate", "count": 1, "truncated": False, "points": [{"id": "hk:p-1", "value": 61.0}]}
    monkeypatch.setattr(mcp_server, "_client", C(R(200, body)))
    assert json.loads(mcp_server.query_samples("heart_rate", "2026-05-03", "2026-05-04")) == body
    assert seen[-1] == ("/api/samples", {"metric": "heart_rate", "start": "2026-05-03", "end": "2026-05-04", "device": "",
                                         "limit": 2000})
    monkeypatch.setattr(mcp_server, "_client", C(R(400, {"detail": "unknown metric 'nope'; known metrics: steps"})))
    out = json.loads(mcp_server.query_samples("nope", "2026-05-03", "2026-05-03", device="", limit=5))
    assert "400" in out["error"] and "unknown metric 'nope'" in out["error"] and "points" not in out
    assert seen[-1][1]["limit"] == 5
