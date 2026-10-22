"""Phase 3: Bridge payload v2 into the raw envelope (heliosd/ingest/bridge_v2.py).

Synthetic payloads only. The oracles are the payloads themselves (what was
sent must be stored whole, once) and the v1 path (the samples rows of a v2
batch equal the samples rows of the same batch sent as v1)."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heliosd.config import Settings
from heliosd.ingest import bridge_v2
from heliosd.ingest.bridge import ingest_batch
from heliosd.main import create_app
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry
from tests.synth import synth_batch

AWU = "Owner’s Ultra 1"
TOKEN = "test-token-0123456789"
TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _env(tz="Asia/Dubai"):
    conn = db.connect_memory()
    policy = MetricPolicy(default_tz=tz)
    policy.sync_registry(conn)
    return conn, policy, SourceRegistry()


SOURCES = {"s1": {"name": AWU, "bundle_id": "com.apple.health.synthetic", "version": "11.0",
                  "product_type": "Watch6,18", "os_version": "11.0.0"}}
DEVICES = {"d1": {"desc": "<<HKDevice: 0x123abc>, name:Apple Watch, manufacturer:Apple Inc., model:Watch, hardware:Watch6,18>",
                  "name": "Apple Watch", "manufacturer": "Apple Inc.", "model": "Watch", "hardware": "Watch6,18"}}


def to_v2(batch: dict, **extra) -> dict:
    """The same records as a v1 batch, in the v2 shape the Bridge 1.1 sends."""
    out = {k: v for k, v in copy.deepcopy(batch).items() if k in bridge_v2.V2_KEYS}   # synth_batch extras are not wire keys
    out.update({"schema": 2, "sources": SOURCES, "devices": DEVICES,
                "bridge": {"app_version": "1.1", "build": "2", "reason": "test", "origin": "anchored",
                           "stream": "HKQuantityTypeIdentifierHeartRate"}})
    for s in out["samples"]:
        s.update({"kind": "category" if "Category" in s["hk_type"] else "quantity", "src": "s1", "dev": "d1",
                  "metadata": [["HKMetadataKeySyntheticKey", "1"]], "sync_identifier": f"sync-{s['uuid']}",
                  "sync_version": 3, "writer_id": "com.apple.health.synthetic"})
    out.update(extra)
    return out


def _sample(uuid, hk_type="HKQuantityTypeIdentifierHeartRate", value=61.5, unit="count/min",
            start="2026-10-21T01:02:03.456Z", end="2026-10-21T01:02:03.456Z", source=AWU, **kw):
    s = {"uuid": uuid, "hk_type": hk_type, "value": value, "unit": unit, "start": start, "end": end,
         "source_name": source, "kind": "quantity", "src": "s1", "dev": "d1"}
    s.update(kw)
    return s


def _v2(batch_id="b1", samples=(), **classes):
    return {"batch_id": batch_id, "device": "iphone", "sent_at": "2026-10-21T02:00:00.000Z", "sync_path": "bridge",
            "schema": 2, "bridge": {"reason": "test", "origin": "anchored"}, "sources": SOURCES, "devices": DEVICES,
            "samples": list(samples), "deleted": classes.pop("deleted", []), **classes}


def _raw(conn, where="TRUE"):
    return db.fetchdicts(conn, f"SELECT * FROM raw_records WHERE {where} ORDER BY record_id")


SAMPLE_COLS = ["sample_id", "hk_uuid", "metric", "hk_type", "value", "text_value", "unit", "start_ts", "end_ts",
               "source_name", "device_key", "sync_path", "start_utc", "end_utc", "src_offset_min", "time_source",
               "content_hash", "unit_rule", "score_state", "quality", "sync_identifier", "sync_version", "writer_id"]


def test_the_samples_rows_of_a_v2_batch_equal_those_of_the_same_batch_as_v1():
    v1 = synth_batch(days=3, seed=4, end_day=datetime(2026, 7, 15).date())
    conn1, policy, reg = _env()
    ingest_batch(conn1, v1, policy, reg)
    conn2, policy2, reg2 = _env()
    res = ingest_batch(conn2, to_v2(v1), policy2, reg2)
    q = f"SELECT {', '.join(SAMPLE_COLS)} FROM samples ORDER BY sample_id"
    a, b = db.fetchall(conn1, q), db.fetchall(conn2, q)
    assert len(a) > 50 and a == b, "v2 must not change the samples path (sync fields included: they stay in raw)"
    assert res["schema"] == 2 and res["raw"]["written"] == len(v1["samples"])
    assert db.fetchall(conn2, "SELECT COUNT(*) FROM samples WHERE sync_identifier IS NOT NULL")[0][0] == 0


def test_every_sample_lands_in_the_raw_envelope_with_its_fields():
    conn, policy, reg = _env()
    samples = [
        _sample("u-hr", metadata=[["HKMetadataKeyHeartRateMotionContext", "1"]], sync_identifier="sid", sync_version=7,
                writer_id="com.apple.health.synthetic"),
        _sample("u-stand", hk_type="HKCategoryTypeIdentifierAppleStandHour", value=0, unit="", kind="category",
                category_value=0),
        _sample("u-walk", hk_type="HKQuantityTypeIdentifierWalkingSpeed", value=4.2, unit="km/hr"),
        _sample("u-ign", source="Athlytic Synthetic"),
    ]
    res = ingest_batch(conn, _v2(samples=samples), policy, reg)
    assert res["raw"] == {"written": 4, "existing": 0} and res["raw_only"] == 2
    rows = {r["record_id"]: r for r in _raw(conn)}
    hr = rows["hk:u-hr"]
    assert hr["element"] == "Record" and hr["metric"] == "heart_rate" and hr["link_status"] == "bridge"
    assert hr["linked_sample_id"] == "hk:u-hr" and hr["value"] == 61.5 and hr["value_raw"] == "61.5"
    assert hr["start_raw"] == "2026-10-21T01:02:03.456Z"
    assert hr["start_utc"] == datetime(2026, 10, 21, 1, 2, 3, 456000), "milliseconds kept in the raw envelope"
    assert hr["device"].startswith("<<HKDevice: 0x123abc>") and hr["source_version"] == "11.0"
    assert json.loads(hr["metadata"]) == [["HKMetadataKeyHeartRateMotionContext", "1"]]
    assert hr["sync_identifier"] == "sid" and hr["sync_version"] == 7
    extra = json.loads(hr["children"])
    assert extra["bundle_id"] == "com.apple.health.synthetic" and extra["product_type"] == "Watch6,18"
    assert extra["writer_id"] == "com.apple.health.synthetic" and extra["device"]["hardware"] == "Watch6,18"
    assert hr["file_sha"] == "bridge" and hr["run_id"] == "bridge:b1"
    stand = rows["hk:u-stand"]
    assert stand["metric"] is None and stand["link_status"] is None
    assert stand["hk_type"] == "HKCategoryTypeIdentifierAppleStandHour", "the true category type"
    assert json.loads(stand["children"])["category_value"] == 0
    assert rows["hk:u-walk"]["unit"] == "km/hr" and rows["hk:u-walk"]["metric"] is None
    ign = rows["hk:u-ign"]
    assert ign["device_key"] == "excluded" and ign["link_status"] == "none" and ign["link_reason"] == "source_ignored"
    # The samples path took only the registered, not ignored, sample.
    assert db.fetchall(conn, "SELECT hk_uuid FROM samples") == [("u-hr",)]


def test_a_replay_changes_nothing():
    conn, policy, reg = _env()
    batch = _v2(samples=[_sample(f"u{i}") for i in range(20)],
                workouts=[{"uuid": "w1", "activity_type": 52, "activity_name": "HKWorkoutActivityTypeWalking",
                           "start": "2026-10-21T01:00:00.000Z", "end": "2026-10-21T01:30:00.000Z",
                           "duration_s": 1800.0, "source_name": AWU, "src": "s1", "statistics": [], "events": [],
                           "activities": [], "route_uuids": []}])
    first = ingest_batch(conn, batch, policy, reg)
    before = db.fetchall(conn, "SELECT COUNT(*) FROM raw_records")[0][0]
    again = ingest_batch(conn, batch, policy, reg)                       # an outbox retry after a lost ack
    other = ingest_batch(conn, {**batch, "batch_id": "b2"}, policy, reg)  # a sweep sending the same records
    assert first["raw"]["written"] == 21
    assert again["raw"] == {"written": 0, "existing": 21} and other["raw"]["written"] == 0
    assert db.fetchall(conn, "SELECT COUNT(*) FROM raw_records")[0][0] == before == 21


def test_workouts_routes_ecgs_summaries_and_daily_totals_land_whole():
    conn, policy, reg = _env()
    workout = {"uuid": "w1", "activity_type": 52, "activity_name": "HKWorkoutActivityTypeWalking",
               "start": "2026-10-21T01:00:00.000Z", "end": "2026-10-21T01:30:00.000Z", "duration_s": 1800.0,
               "source_name": AWU, "src": "s1", "dev": "d1", "metadata": [["HKIndoorWorkout", "0"]],
               "statistics": [{"hk_type": "HKQuantityTypeIdentifierHeartRate", "start": "s", "end": "e",
                               "avg": 112.0, "min": 90.0, "max": 141.0, "unit": "count/min"},
                              {"hk_type": "HKQuantityTypeIdentifierDistanceWalkingRunning", "start": "s", "end": "e",
                               "sum": 2.4, "unit": "km"}],
               "events": [{"type": 1, "type_name": "HKWorkoutEventTypePause", "start": "s", "end": "s"}],
               "activities": [], "route_uuids": ["r1"], "total_energy_kcal": 140.0, "total_distance_m": 2400.0}
    route = {"uuid": "r1", "workout_uuid": "w1", "start": "2026-10-21T01:00:00.000Z", "end": "2026-10-21T01:30:00.000Z",
             "source_name": AWU, "src": "s1", "points": [
                 ["2026-10-21T01:00:00.000Z", 10.0, 20.0, 5.0, 3.0, 4.0, 1.2, 90.0, 0.5, 10.0],
                 ["2026-10-21T01:00:01.000Z", 10.0001, 20.0001, None, 3.0, None, None, None, None, None]]}
    ecg = {"uuid": "e1", "start": "2026-10-21T03:00:00.000Z", "end": "2026-10-21T03:00:30.000Z", "classification": 1,
           "classification_name": "sinusRhythm", "symptoms_status": 1, "symptoms_name": "none", "average_hr": 63.0,
           "sampling_hz": 512.0, "n_voltages": 3, "source_name": AWU, "src": "s1", "lead": "appleWatchSimilarToLeadI",
           "unit": "mcV", "microvolts": [1.5, -2.25, None]}
    day = {"date": "2026-10-20", "active_energy": 400.0, "active_energy_goal": 600.0, "active_energy_unit": "kcal"}
    stat = {"hk_type": "HKQuantityTypeIdentifierStepCount", "day": "2026-10-20", "zone": "Asia/Dubai",
            "value": 5230.0, "unit": "count", "complete": True, "sources": [["Watch", "com.apple", "5230"]]}
    res = ingest_batch(conn, _v2(workouts=[workout], routes=[route], ecgs=[ecg], activity_summaries=[day],
                                 daily_statistics=[stat]), policy, reg)
    assert res["classes"]["workouts"] == 1 and res["points_written"] == 2
    rows = {r["record_id"]: r for r in _raw(conn)}
    w = rows["hk:w1"]
    assert w["element"] == "Workout" and w["value"] == 1800.0 and w["value_raw"] == "HKWorkoutActivityTypeWalking"
    wx = json.loads(w["children"])
    assert wx["route_uuids"] == ["r1"] and wx["statistics"][0]["avg"] == 112.0 and wx["total_distance_m"] == 2400.0
    r = rows["hk:r1"]
    assert r["element"] == "WorkoutRoute" and json.loads(r["children"])["workout_uuid"] == "w1"
    pts = db.fetchdicts(conn, "SELECT * FROM route_points WHERE route_id = 'rtb:r1' ORDER BY seq")
    assert [(p["lat"], p["lon"], p["ele"], p["speed"]) for p in pts] == [(10.0, 20.0, 5.0, 1.2), (10.0001, 20.0001, None, None)]
    assert pts[1]["time_utc"] == datetime(2026, 10, 21, 1, 0, 1)
    e = rows["hk:e1"]
    assert e["element"] == "Electrocardiogram" and json.loads(e["children"])["microvolts"] == [1.5, -2.25, None]
    assert json.loads(e["children"])["sampling_hz"] == 512.0
    summaries = [x for x in rows.values() if x["element"] == "ActivitySummary"]
    assert len(summaries) == 1 and summaries[0]["record_id"].startswith("hkas:2026-10-20:")
    stats = [x for x in rows.values() if x["element"] == "DailyStatistic"]
    assert stats[0]["value"] == 5230.0 and json.loads(stats[0]["children"])["zone"] == "Asia/Dubai"
    # A changed observation of the same day is kept beside the first; the same one again is not.
    later = {**day, "active_energy": 520.0}
    ingest_batch(conn, _v2("b2", activity_summaries=[later, day]), policy, reg)
    assert db.fetchall(conn, "SELECT COUNT(*) FROM raw_records WHERE element = 'ActivitySummary'")[0][0] == 2


def test_a_deletion_removes_the_raw_rows_and_a_replay_never_resurrects_them():
    conn, policy, reg = _env()
    route = {"uuid": "r1", "start": "2026-10-21T01:00:00.000Z", "end": "2026-10-21T01:30:00.000Z",
             "source_name": AWU, "points": [["2026-10-21T01:00:00.000Z", 1.0, 2.0]]}
    first = _v2(samples=[_sample("u1"), _sample("u2", hk_type="HKQuantityTypeIdentifierWalkingSpeed", unit="km/hr")],
                routes=[route])
    ingest_batch(conn, first, policy, reg)
    assert db.fetchall(conn, "SELECT COUNT(*) FROM raw_records")[0][0] == 3
    ingest_batch(conn, _v2("b2", deleted=["u1", "u2", "r1"]), policy, reg)
    assert db.fetchall(conn, "SELECT COUNT(*) FROM raw_records")[0][0] == 0
    assert db.fetchall(conn, "SELECT COUNT(*) FROM route_points")[0][0] == 0
    replay = ingest_batch(conn, first, policy, reg)                   # an old batch arriving late
    assert replay["raw"]["written"] == 0 and replay["classes"]["skipped_tombstoned"] == 3
    assert db.fetchall(conn, "SELECT COUNT(*) FROM raw_records")[0][0] == 0
    assert db.fetchall(conn, "SELECT COUNT(*) FROM samples")[0][0] == 0


def test_odd_values_never_fail_the_batch():
    """A batch that fails is retried forever by the relay: odd values are kept or
    left empty, never raised on."""
    conn, policy, reg = _env()
    odd = [_sample("u1", hk_type="HKQuantityTypeIdentifierWalkingSpeed", unit="km/hr", value="not a number",
                   sync_version=2 ** 70, start="garbage", end=None, metadata={"not": "pairs"}),
           _sample("u2", hk_type="HKQuantityTypeIdentifierWalkingSpeed", unit="km/hr", value=float("1e308") * 10,
                   sync_version="7", start="2026-10-21T01:02:03Z")]
    odd.append(_sample("u3", hk_type="HKQuantityTypeIdentifierWalkingSpeed", unit="km/hr", src=["not", "a", "key"],
                       dev={"also": "not"}))
    res = ingest_batch(conn, _v2(samples=odd, routes=[{"uuid": "r1", "points": [["t", "x", None], "bad"]}]),
                       policy, reg)
    assert res["ack"] is True and res["raw"]["written"] == 4
    rows = {r["record_id"]: r for r in _raw(conn)}
    assert rows["hk:u1"]["sync_version"] is None and rows["hk:u1"]["start_utc"] is None
    assert rows["hk:u1"]["start_raw"] == "garbage" and rows["hk:u1"]["value_raw"] == "not a number"
    assert rows["hk:u2"]["sync_version"] == 7 and rows["hk:u2"]["value"] is None


def test_fields_this_daemon_has_no_place_for_are_kept_never_dropped():
    conn, policy, reg = _env()
    sources = {"s1": {**SOURCES["s1"], "channel": "beta"}}
    sample = _sample("u1", future_field={"a": 1})
    workout = {"uuid": "w1", "start": "2026-10-21T01:00:00.000Z", "end": "2026-10-21T01:30:00.000Z",
               "source_name": AWU, "src": "s1", "effort_score": 7.5}
    route = {"uuid": "r1", "source_name": AWU, "surface": "trail", "points": [
        ["2026-10-21T01:00:00.000Z", 1.0, 2.0, None, None, None, None, None, None, None, "floor 3"],
        "not a point", ["2026-10-21T01:00:02.000Z", 1.0]]}
    ecg = {"uuid": "e1", "start": "2026-10-21T03:00:00.000Z", "source_name": AWU, "lead_ii": [1.0]}
    stat = {"hk_type": "HKQuantityTypeIdentifierStepCount", "day": "2026-10-20", "zone": "Asia/Dubai",
            "value": 10.0, "unit": "count", "basis": "watch only"}
    res = ingest_batch(conn, {**_v2(samples=[sample], workouts=[workout], routes=[route], ecgs=[ecg],
                                    daily_statistics=[stat]), "sources": sources}, policy, reg)
    assert res["ack"] is True
    rows = {r["record_id"]: json.loads(r["children"]) for r in _raw(conn)}
    assert rows["hk:u1"]["extra_fields"] == {"future_field": {"a": 1}}
    assert rows["hk:u1"]["source_extra"] == {"channel": "beta"}
    assert rows["hk:w1"]["extra_fields"] == {"effort_score": 7.5}
    assert rows["hk:r1"]["extra_fields"] == {"surface": "trail"}
    assert rows["hk:r1"]["point_extras"] == {"0": ["floor 3"]}
    assert rows["hk:r1"]["odd_points"] == {"1": "not a point", "2": ["2026-10-21T01:00:02.000Z", 1.0]}
    assert rows["hk:e1"]["extra_fields"] == {"lead_ii": [1.0]}
    (ds,) = [v for k, v in rows.items() if k.startswith("hkds:")]
    assert ds["extra_fields"] == {"basis": "watch only"}
    # A record of a known shape carries no extra_fields.
    conn2, policy2, reg2 = _env()
    ingest_batch(conn2, _v2(samples=[_sample("u2")]), policy2, reg2)
    assert "extra_fields" not in json.loads(_raw(conn2)[0]["children"])


def test_records_without_an_identity_are_counted_in_the_ack():
    conn, policy, reg = _env()
    nameless = {k: v for k, v in _sample("x").items() if k != "uuid"}
    res = ingest_batch(conn, _v2(samples=[nameless, _sample("u1")], workouts=[{"start": "s"}],
                                 activity_summaries=[{"active_energy": 1.0}],
                                 daily_statistics=[{"hk_type": "HKQuantityTypeIdentifierStepCount"}]), policy, reg)
    assert res["ack"] is True and res["classes"]["skipped_no_id"] == 4
    assert res["skipped_types"].get("no_uuid") == 1, "the samples path counts its own"
    assert [r["record_id"] for r in _raw(conn)] == ["hk:u1"]


def test_a_registered_sample_with_an_unreadable_time_is_counted_and_the_batch_goes_on():
    conn, policy, reg = _env()
    bad = _sample("u-bad", start="2026-13-45T00:00:00.000Z", end="garbage")
    res = ingest_batch(conn, _v2(samples=[bad, _sample("u-ok")]), policy, reg)
    assert res["ack"] is True and res["skipped_types"] == {"bad_record": 1}
    assert db.fetchall(conn, "SELECT hk_uuid FROM samples") == [("u-ok",)]
    rows = {r["record_id"]: r for r in _raw(conn)}
    assert rows["hk:u-bad"]["start_raw"] == "2026-13-45T00:00:00.000Z" and rows["hk:u-bad"]["start_utc"] is None
    assert rows["hk:u-bad"]["link_status"] == "none" and rows["hk:u-bad"]["link_reason"] == "bad_record"
    # A v1 batch is unchanged: the old path, the old behaviour.
    with pytest.raises(ValueError):
        ingest_batch(conn, {"batch_id": "v1", "samples": [bad], "deleted": []}, policy, reg)


def test_a_value_a_column_stores_as_null_keeps_its_original():
    conn, policy, reg = _env()
    route = {"uuid": "r1", "source_name": AWU, "points": [["2026-10-21T01:00:00.000Z", "NaN", "Infinity", None, 3.0]]}
    stat = {"hk_type": "HKQuantityTypeIdentifierStepCount", "day": "2026-10-20", "zone": "Asia/Dubai",
            "value": "-Infinity", "unit": "count"}
    res = ingest_batch(conn, _v2(samples=[_sample("u1", sync_version=2 ** 70), _sample("u2", value="NaN")],
                                 routes=[route], daily_statistics=[stat]), policy, reg)
    assert res["ack"] is True
    rows = {r["record_id"]: r for r in _raw(conn)}
    assert rows["hk:u1"]["sync_version"] is None and json.loads(rows["hk:u1"]["children"])["sync_version_raw"] == 2 ** 70
    assert rows["hk:u2"]["value"] is None and rows["hk:u2"]["value_raw"] == "NaN"
    assert db.fetchdicts(conn, "SELECT lat, lon FROM route_points WHERE route_id = 'rtb:r1'") == [{"lat": None, "lon": None}]
    assert json.loads(rows["hk:r1"]["children"])["coerced_points"] == {"0": route["points"][0]}
    (ds,) = [r for k, r in rows.items() if k.startswith("hkds:")]
    assert ds["value"] is None and json.loads(ds["children"])["value_raw"] == "-Infinity"
    # A NaN never becomes a samples value: the registered sample is kept as a bad value.
    assert db.fetchall(conn, "SELECT value, quality FROM samples WHERE hk_uuid = 'u2'") == [(None, "bad_value")]


def test_a_deletion_never_touches_export_rows():
    conn, policy, reg = _env()
    conn.execute("INSERT INTO raw_records (record_id, element, file_sha, ordinal) VALUES ('xr:abc:0', 'Record', 'f', 1)")
    ingest_batch(conn, _v2(deleted=["abc"]), policy, reg)
    assert db.fetchall(conn, "SELECT record_id FROM raw_records") == [("xr:abc:0",)]


@pytest.mark.parametrize("payload, why", [
    ({"batch_id": "x", "schema": 3, "samples": []}, "schema 3"),
    ({"batch_id": "x", "schema": "2", "samples": []}, "schema as text"),
    ({"batch_id": "x", "schema": 2, "samples": [], "heartbeats": []}, "an unknown v2 key"),
    ({"batch_id": "x", "schema": 2, "samples": {}}, "samples not a list"),
    ({"batch_id": "x", "schema": 2, "samples": [], "deleted": [["u1"]]}, "a deleted item not a uuid string"),
    ({"batch_id": "x", "schema": 2, "samples": [1]}, "a sample that is not an object"),
    ({"batch_id": "x", "schema": 2, "samples": [], "routes": ["r1"]}, "a route that is not an object"),
    ({"batch_id": "x", "schema": 2, "samples": [], "daily_statistics": [None]}, "a daily total that is not an object"),
    ({"batch_id": "x", "schema": 2, "samples": [_sample(["u1"])]}, "a uuid that is a list"),
    ({"batch_id": "x", "schema": 2, "samples": [_sample("u1", hk_type={"t": 1})]}, "a type that is an object"),
    ({"batch_id": "x", "schema": 2, "samples": [_sample("u1", source=["Watch"])]}, "a source name that is a list"),
    ({"batch_id": "x", "schema": 2, "samples": [], "routes": [{"uuid": "r1", "source_name": 5}]}, "a route source that is a number"),
    ({"batch_id": "x", "schema": 2, "samples": [], "activity_summaries": [{"date": ["2026-10-20"]}]}, "a summary day that is a list"),
    ({"batch_id": "x", "schema": 2, "samples": [], "daily_statistics": [{"hk_type": "HKQuantityTypeIdentifierStepCount",
                                                                          "day": 20261020}]}, "a daily total day that is a number"),
])
def test_a_shape_this_daemon_cannot_store_whole_is_refused_before_any_write(payload, why):
    conn, policy, reg = _env()
    with pytest.raises(bridge_v2.PayloadRefused):
        ingest_batch(conn, payload, policy, reg)
    assert db.fetchall(conn, "SELECT COUNT(*) FROM sync_log")[0][0] == 0, why


def test_the_route_refuses_with_422_and_ack_false_and_v1_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    raw = {"server": {"ingest_token": TOKEN}, "storage": {"db_path": str(tmp_path / "helios.duckdb")},
           "notifications": {"macos_alerts": False}}
    h = {"X-Helios-Token": TOKEN}
    with TestClient(create_app(Settings(raw=raw))) as c:
        r = c.post("/ingest", json={"batch_id": "x", "schema": 3, "samples": []}, headers=h)
        assert r.status_code == 422 and r.json()["ack"] is False
        r = c.post("/ingest", json={"batch_id": "v1", "samples": [_sample("u1")], "deleted": []}, headers=h)
        assert r.status_code == 200 and r.json()["ack"] is True and "schema" not in r.json()
        r = c.post("/ingest", json=_v2("v2", samples=[_sample("u2")]), headers=h)
        assert r.status_code == 200 and r.json()["ack"] is True and r.json()["raw"]["written"] == 1


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_relay_replays_a_v2_batch_twice_and_the_store_takes_it_once(tmp_path, monkeypatch):
    """Spool, replay, crash between the ack and the delete, replay again."""
    receiver, puller = _load("m1_spool_receiver"), _load("m4_spool_pull")
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    raw = {"server": {"ingest_token": TOKEN}, "storage": {"db_path": str(tmp_path / "helios.duckdb")},
           "notifications": {"macos_alerts": False}}
    spool = tmp_path / "spool"
    batch = _v2(samples=[_sample(f"u{i}") for i in range(30)],
                ecgs=[{"uuid": "e1", "start": "2026-10-21T03:00:00.000Z", "end": "2026-10-21T03:00:30.000Z",
                       "classification": 1, "classification_name": "sinusRhythm", "symptoms_status": 1,
                       "symptoms_name": "none", "n_voltages": 1, "source_name": AWU, "lead": "x", "unit": "mcV",
                       "microvolts": [1.0]}])
    assert receiver.accept_batch(json.dumps(batch).encode(), spool, 64 << 20, 1 << 30, 0.0)[0] == 200
    h = {"X-Helios-Token": TOKEN, "Content-Type": "application/json"}
    with TestClient(create_app(Settings(raw=raw))) as c:
        def post(body: bytes):
            r = c.post("/ingest", content=body, headers=h)
            return r.status_code, r.json()

        def count():
            return c.post("/api/tool/sql", json={"query": "SELECT COUNT(*) AS n FROM raw_records"},
                          headers={"X-Helios-Token": TOKEN}).json()[0]["n"]

        staging, delivered = spool / "inbox", tmp_path / "delivered"
        assert puller.replay(post, lambda n: False, staging, delivered)["acked"] == 1   # the delete fails
        n1 = count()
        for f in delivered.glob("*.json"):
            f.rename(staging / f.name)                                                  # replayed again
        assert puller.replay(post, lambda n: True, staging, delivered)["acked"] == 1
        assert count() == n1 == 31


def test_two_thousand_raw_rows_land_inside_the_budget():
    """The raw envelope goes in through one bound JSON value; executemany would
    cost seconds per page (the DuckDB client's per-value overhead)."""
    import os
    scale = float(os.environ.get("HELIOS_TEST_TIME_SCALE", "1"))
    conn, policy, reg = _env()
    samples = [_sample(f"u{i}", hk_type="HKQuantityTypeIdentifierPhysicalEffort", unit="kcal/hr·kg",
                       start=f"2026-10-21T01:{i // 60 % 60:02d}:{i % 60:02d}.000Z",
                       end=f"2026-10-21T01:{i // 60 % 60:02d}:{i % 60:02d}.500Z",
                       metadata=[["HKMetadataKeySyntheticKey", str(i)]]) for i in range(2000)]
    t = time.monotonic()
    res = ingest_batch(conn, _v2(samples=samples), policy, reg)
    took = time.monotonic() - t
    assert res["raw"]["written"] == 2000 and res["raw_only"] == 2000
    assert took < 2.0 * scale, f"{took:.2f} s for 2,000 raw rows"
