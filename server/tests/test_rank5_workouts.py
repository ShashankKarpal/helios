"""Rank 5 (2026-10-29): the workout history over the Phase 2 tables, read-only.
GET /api/workouts pages the canonical sessions newest first; GET
/api/workouts/{id} shows one with the source of every field, its source rows
and a route outline that carries no coordinates. Every route needs the token.
On the code before rank 5 an unknown /api GET answers the app shell with 200,
so every test reads the JSON shape, not only the status."""

from __future__ import annotations

import base64
import json
from datetime import timedelta

import pytest

from .rank5_helpers import AUTH, LAT0, LON0, T0, add_raw, add_workout, client_for, new_store

N = 120  # more than two pages of 50


def _fill(conn):
    for i in range(N):
        add_workout(conn, i, T0 + timedelta(days=i), status="queued" if i == 7 else ("matched" if i % 3 == 0 else "single"),
                    with_whoop=(i % 3 == 0), route_points=1200 if i == N - 1 else (40 if i == N - 2 else 0),
                    hr=(i != 5), distance=(i != 5))
    newest = T0 + timedelta(days=N - 1)
    for k in range(4):           # Bridge raw rows newer than the archive's newest session
        add_raw(conn, "Workout", k, newest + timedelta(days=k + 1))
    add_raw(conn, "Workout", 9, newest - timedelta(days=3))     # older: a copy of archive history
    add_raw(conn, "Electrocardiogram", 10, newest + timedelta(days=2))  # another element: not counted


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch, new_store(tmp_path, _fill)) as c:
        yield c


def test_list_is_json_newest_first_with_archive_and_newer_records(client):
    r = client.get("/api/workouts", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["total"] == N
    assert body["reporting_zone"] == "UTC"
    assert body["archive"] == {"origin": "apple_health_export",
                               "oldest_start_utc": "2026-01-01T04:00:00Z",
                               "newest_start_utc": (T0 + timedelta(days=N - 1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    # raw Bridge Workout records after the archive's newest start, counted, never listed
    assert body["newer_records"] == 4
    rows = body["workouts"]
    assert len(rows) == 50
    starts = [w["start_utc"] for w in rows]
    assert starts == sorted(starts, reverse=True)
    assert rows[0]["start_utc"] == body["archive"]["newest_start_utc"]


def test_row_fields(client):
    rows = client.get("/api/workouts", params={"limit": 200}, headers=AUTH).json()["workouts"]
    by_id = {w["workout_id"]: w for w in rows}
    w = by_id["wk:xw:%024x:%d" % (6, 6)]
    assert w["status"] == "matched" and w["n_sources"] == 2
    assert w["activity_type"] == "HKWorkoutActivityTypeWalking" and w["activity_label"] == "Walking"
    assert w["start_local"] == "2026-01-07T08:00:00+04:00" and w["tz"] == "Asia/Dubai"
    assert w["duration_s"] == 2520 and w["distance"] == 3.125 and w["distance_unit"] == "km"
    assert w["energy_kcal"] == 186.5 and w["hr_avg"] == 118.5 and w["hr_unit"] == "count/min"
    assert w["sources"] == ["Apple Watch Ultra", "Whoop"]
    assert w["has_route"] is False
    assert "strain" not in w            # the strain is on the detail only, labelled there
    # a missing value is null, never zero
    w5 = by_id["wk:xw:%024x:%d" % (5, 5)]
    assert w5["hr_avg"] is None and w5["distance"] is None
    # the queued session is listed, with its status
    assert by_id["wk:xw:%024x:%d" % (7, 7)]["status"] == "queued"
    assert by_id["wk:xw:%024x:%d" % (N - 1, N - 1)]["has_route"] is True


def test_label_is_mechanical():
    from heliosd.history.views import activity_label
    assert activity_label("HKWorkoutActivityTypeTraditionalStrengthTraining") == "Traditional strength training"
    assert activity_label("HKWorkoutActivityTypeHIIT") == "HIIT"
    assert activity_label(None) == "Workout"
    assert activity_label("SomethingElse") == "Something else"


def test_paging_walks_every_row_once(client):
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 50} | ({"cursor": cursor} if cursor else {})
        body = client.get("/api/workouts", params=params, headers=AUTH).json()
        seen += [w["workout_id"] for w in body["workouts"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3 and len(seen) == N and len(set(seen)) == N


@pytest.mark.parametrize("limit", [0, 201, -1])
def test_limit_bounds(client, limit):
    assert client.get("/api/workouts", params={"limit": limit}, headers=AUTH).status_code == 422


@pytest.mark.parametrize("cursor", ["not-base64!", base64.urlsafe_b64encode(b"[1]").decode(),
                                    base64.urlsafe_b64encode(b'["2026-01-01T00:00:00Z"]').decode(),
                                    base64.urlsafe_b64encode(b'["not a time", "wk:x"]').decode()])
def test_bad_cursor_is_422(client, cursor):
    assert client.get("/api/workouts", params={"cursor": cursor}, headers=AUTH).status_code == 422


def test_detail_names_every_source_and_lists_source_rows(client):
    wid = "wk:xw:%024x:%d" % (6, 6)
    r = client.get(f"/api/workouts/{wid}", headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    d = r.json()
    w = d["workout"]
    assert w["workout_id"] == wid and w["hr_min"] == 92.0 and w["hr_max"] == 141.0
    assert w["strain"] == 9.375
    assert w["distance_type"] == "HKQuantityTypeIdentifierDistanceWalkingRunning"
    fs = d["field_sources"]
    assert fs["hr"]["label"] == "Apple Watch Ultra" and fs["strain"]["label"] == "Whoop"
    assert fs["timing"]["source_id"] == "xw:%024x:%d" % (6, 6)
    assert [s["role"] for s in d["sources"]] == ["anchor", "member"]
    anchor = d["sources"][0]
    assert anchor["statistics"][0]["hk_type"] == "HKQuantityTypeIdentifierActiveEnergyBurned"
    assert anchor["events"]["counts"] == {"HKWorkoutEventTypeSegment": 3, "HKWorkoutEventTypePause": 1}
    assert anchor["events"]["total"] == 4 and len(anchor["events"]["items"]) == 4
    # sync identifier keys left out; the repeated key kept as the export repeats it
    assert anchor["metadata"]["items"] == [["HKIndoorWorkout", "0"], ["HKIndoorWorkout", "0"]]
    for s in d["sources"]:
        for hidden in ("file_sha", "run_id", "match_detail", "sync_identifier", "ordinal"):
            assert hidden not in s
    assert d["route"] is None


def test_detail_route_outline_has_no_coordinates(client):
    wid = "wk:xw:%024x:%d" % (N - 1, N - 1)
    r = client.get(f"/api/workouts/{wid}", headers=AUTH)
    route = r.json()["route"]
    assert route["link_status"] == "linked" and route["n_points"] == 1200
    pts = route["outline"]
    assert len(pts) == 500
    assert all(isinstance(v, int) and 0 <= v <= 1000 for p in pts for v in p)
    assert max(max(p[0] for p in pts), max(p[1] for p in pts)) == 1000
    assert route["width"] == 1000 and 0 < route["height"] <= 1000
    text = r.text
    for needle in ('"lat"', '"lon"', str(LAT0)[:6], str(LON0)[:6], "0.01", "0.02"):
        assert needle not in text, needle
    # a short route keeps every point
    short = client.get("/api/workouts/" + "wk:xw:%024x:%d" % (N - 2, N - 2), headers=AUTH).json()["route"]
    assert short["n_points"] == 40 and len(short["outline"]) == 40


@pytest.mark.parametrize("wid", ["wk:xw:%024x:%d" % (999, 999), "nope", "wk:" + "a" * 300, "wk:x y"])
def test_detail_unknown_or_malformed_is_404(client, wid):
    r = client.get(f"/api/workouts/{wid}", headers=AUTH)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("path", ["/api/workouts", "/api/workouts/wk:xw:%024x:%d" % (6, 6)])
def test_needs_the_token(client, path):
    assert client.get(path).status_code == 401


def test_empty_store(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch, new_store(tmp_path, lambda c: add_raw(c, "Workout", 1, T0))) as c:
        body = c.get("/api/workouts", headers=AUTH).json()
    assert body["total"] == 0 and body["workouts"] == [] and body["next_cursor"] is None
    assert body["archive"] == {"origin": "apple_health_export", "oldest_start_utc": None, "newest_start_utc": None}
    assert body["newer_records"] == 1   # no archive: every Bridge workout record is newer


def test_read_only(client):
    def counts():
        return {t: client.app.state.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("workouts", "workout_sources", "workout_routes", "route_points", "raw_records")}
    before = counts()
    client.get("/api/workouts", headers=AUTH)
    client.get("/api/workouts/" + "wk:xw:%024x:%d" % (N - 1, N - 1), headers=AUTH)
    assert counts() == before


def test_importer_output_reads_back(tmp_path, monkeypatch):
    """The real Phase 2 importer on the tracked synthetic export: every session it wrote is
    listed once, and every detail answers."""
    import importlib.util
    from pathlib import Path

    from .phase2_helpers import june, store
    tools = Path(__file__).resolve().parents[1] / "tools"
    spec = importlib.util.spec_from_file_location("import_history_phase2_rank5", tools / "import_history_phase2.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    st = store(tmp_path)
    assert tool.main([str(st), "--export", str(june(tmp_path)), "--out", str(tmp_path / "out")]) == 0
    import duckdb
    with duckdb.connect(str(st), read_only=True) as ro:
        ids = {r[0] for r in ro.execute("SELECT workout_id FROM workouts").fetchall()}
    assert ids
    with client_for(tmp_path, monkeypatch, st) as c:
        body = c.get("/api/workouts", params={"limit": 200}, headers=AUTH).json()
        assert body["total"] == len(ids) and {w["workout_id"] for w in body["workouts"]} == ids
        for wid in ids:
            d = c.get(f"/api/workouts/{wid}", headers=AUTH)
            assert d.status_code == 200, wid
            json.dumps(d.json())
