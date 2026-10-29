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


# ---------- review round 1 (independent review and /code-review medium) ----------

def _bare(conn, wid: str, start):
    conn.execute("INSERT INTO workouts (workout_id, status, n_sources, activity_type, start_utc) VALUES (?, 'single', 1, "
                 "'HKWorkoutActivityTypeWalking', ?)", [wid, start])


def _walk(c, limit=2):
    seen, cursor = [], None
    while True:
        body = c.get("/api/workouts", params={"limit": limit} | ({"cursor": cursor} if cursor else {}),
                     headers=AUTH).json()
        seen += [w["workout_id"] for w in body["workouts"]]
        cursor = body["next_cursor"]
        if not cursor:
            return seen


def test_ties_and_null_starts_page_once_nulls_last(tmp_path, monkeypatch):
    def fill(conn):
        for i in range(5):
            _bare(conn, f"wk:tie-{i}", T0)
        for i in range(2):
            _bare(conn, f"wk:none-{i}", None)
    with client_for(tmp_path, monkeypatch, new_store(tmp_path, fill)) as c:
        seen = _walk(c, 2)
    assert len(seen) == 7 == len(set(seen))
    assert seen[-2:] == ["wk:none-1", "wk:none-0"]
    assert seen[:5] == [f"wk:tie-{i}" for i in (4, 3, 2, 1, 0)]


def test_sub_second_starts_page_once(tmp_path, monkeypatch):
    def fill(conn):
        for ms in (700, 500, 300):
            _bare(conn, f"wk:ms-{ms}", T0 + timedelta(milliseconds=ms))
    with client_for(tmp_path, monkeypatch, new_store(tmp_path, fill)) as c:
        assert _walk(c, 1) == ["wk:ms-700", "wk:ms-500", "wk:ms-300"]


def test_newer_records_ignore_a_same_second_bridge_copy(tmp_path, monkeypatch):
    """Bridge instants carry milliseconds; the export's are whole seconds. The Bridge copy of the
    archive's newest session is not a newer record."""
    def fill(conn):
        add_workout(conn, 1, T0)
        add_raw(conn, "Workout", 1, T0 + timedelta(milliseconds=250))
        add_raw(conn, "Workout", 2, T0 + timedelta(seconds=1))
    with client_for(tmp_path, monkeypatch, new_store(tmp_path, fill)) as c:
        assert c.get("/api/workouts", headers=AUTH).json()["newer_records"] == 1


def test_cursor_checks(client):
    ecg_cursor = base64.urlsafe_b64encode(json.dumps(["ecg", "2026-01-01T00:00:00Z", "ecg:20260101T000000Z"])
                                          .encode()).decode()
    with_offset = base64.urlsafe_b64encode(json.dumps(["wk", "2026-01-01T00:00:00+04:00", "wk:x"]).encode()).decode()
    bad_id = base64.urlsafe_b64encode(json.dumps(["wk", "2026-01-01T00:00:00Z", "x y"]).encode()).decode()
    for cur in (ecg_cursor, with_offset, bad_id):
        assert client.get("/api/workouts", params={"cursor": cur}, headers=AUTH).status_code == 422, cur


def test_later_pages_skip_the_totals(client):
    first = client.get("/api/workouts", headers=AUTH).json()
    second = client.get("/api/workouts", params={"cursor": first["next_cursor"]}, headers=AUTH).json()
    assert first["total"] == N and second["total"] is None
    assert second["archive"] is None and second["newer_records"] is None
    assert len(second["workouts"]) == 50


def test_a_zone_name_that_is_not_a_zone_falls_back(tmp_path, monkeypatch):
    def fill(conn):
        add_workout(conn, 1, T0)
        conn.execute("UPDATE workouts SET tz_name = 'America'")
    with client_for(tmp_path, monkeypatch, new_store(tmp_path, fill)) as c:
        r = c.get("/api/workouts", headers=AUTH)
        assert r.status_code == 200 and r.json()["workouts"][0]["tz"] == "UTC"


def _route_store(tmp_path, points):
    def fill(conn):
        add_workout(conn, 1, T0, route_points=1)
        conn.execute("DELETE FROM route_points")
        for seq, (la, lo) in enumerate(points):
            conn.execute("INSERT INTO route_points (route_id, seq, lat, lon) VALUES (?, ?, ?, ?)",
                         ["rt:%024x" % 1, seq, la, lo])
    return new_store(tmp_path, fill)


@pytest.mark.parametrize("points, n", [([(LAT0, LON0)], 1), ([(None, None), (None, None)], 0),
                                       ([(LAT0, LON0), (LAT0, LON0)], 2)])
def test_a_route_too_short_to_draw_says_so(tmp_path, monkeypatch, points, n):
    with client_for(tmp_path, monkeypatch, _route_store(tmp_path, points)) as c:
        route = c.get("/api/workouts/" + "wk:xw:%024x:%d" % (1, 1), headers=AUTH).json()["route"]
    assert route is not None and route["outline"] == [] and route["n_points"] == n


def test_a_north_south_line_keeps_its_shape(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch, _route_store(tmp_path, [(LAT0, LON0), (LAT0 + 0.01, LON0)])) as c:
        route = c.get("/api/workouts/" + "wk:xw:%024x:%d" % (1, 1), headers=AUTH).json()["route"]
    assert route["width"] == 0 and route["height"] == 1000 and route["outline"] == [[0, 1000], [0, 0]]


def test_an_excluded_device_reads_as_its_family(tmp_path, monkeypatch):
    def fill(conn):
        add_workout(conn, 1, T0)
        conn.execute("UPDATE workout_sources SET device_key = 'excluded'")
    with client_for(tmp_path, monkeypatch, new_store(tmp_path, fill)) as c:
        assert c.get("/api/workouts", headers=AUTH).json()["workouts"][0]["sources"] == ["apple"]
