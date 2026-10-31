"""P4 (2026-10-31): four read-only MCP tools over the rank 5 history routes:
list_workouts, get_workout, list_ecgs and get_ecg (no waveform). End to end:
the MCP module's HTTP client is a TestClient over a synthetic store, so each
tool runs through the real route. The wording rules of the web screens hold
for the tool text too: device-reported, "newer workout records", no route
coordinates or shape, no interpretation, no score or advice. Synthetic only."""

from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest

from .rank5_helpers import AUTH, LAT0, LON0, T0, add_ecg, add_raw, add_workout, client_for, new_store

# The web's judgement-word check (web/scripts/check-bundle.mjs JUDGEMENT), applied to the tool text.
JUDGEMENT = re.compile(r"\b(abnormal|normal|healthy|unhealthy|risk|risky|warning|danger|doctor|recommend\w*|"
                       r"improv\w*|good|bad|great|poor|concern\w*|irregular|afib|atrial)\b", re.I)
COORD_KEYS = {"outline", "lat", "lon", "latitude", "longitude", "width", "height"}
TOOLS = ("list_workouts", "get_workout", "list_ecgs", "get_ecg")


def _fill(conn):
    for i in range(60):
        add_workout(conn, i, T0 + timedelta(days=i), status="queued" if i == 7 else "single",
                    with_whoop=(i == 59), route_points=300 if i == 59 else 0)
    newest = T0 + timedelta(days=59)
    for k in range(3):
        add_raw(conn, "Workout", k, newest + timedelta(days=k + 1))
    for i in range(3):
        add_ecg(conn, T0 + timedelta(days=i))
    add_raw(conn, "Electrocardiogram", 5, T0 + timedelta(days=9))


@pytest.fixture()
def mcp(tmp_path, monkeypatch):
    from heliosd.mcp_server import server as mcp_server
    with client_for(tmp_path, monkeypatch, new_store(tmp_path, _fill)) as c:
        c.headers.update(AUTH)
        monkeypatch.setattr(mcp_server, "_client", c)
        yield mcp_server


def _wid(n: int) -> str:
    return "wk:xw:%024x:%d" % (n, n)


def _ecg(i: int) -> str:
    return "ecg:" + (T0 + timedelta(days=i)).strftime("%Y%m%dT%H%M%SZ")


def _keys(obj) -> set:
    if isinstance(obj, dict):
        return set(obj) | set().union(*(_keys(v) for v in obj.values()))
    if isinstance(obj, list):
        return set().union(*(_keys(v) for v in obj)) if obj else set()
    return set()


def test_list_workouts_pages_through_the_route_with_notes(mcp):
    first = json.loads(mcp.list_workouts(limit=50))
    assert first["total"] == 60 and first["newer_records"] == 3 and len(first["workouts"]) == 50
    assert first["archive"]["origin"] == "apple_health_export"
    rest = json.loads(mcp.list_workouts(limit=50, cursor=first["next_cursor"]))
    assert len(rest["workouts"]) == 10 and rest["next_cursor"] is None and rest["total"] is None
    notes = " ".join(first["notes"])
    assert "newer workout records" in notes and "device-reported" in notes and "matching undecided" in notes


def test_get_workout_keeps_counts_and_drops_route_shape_and_event_items(mcp):
    raw = json.loads(mcp._client.get(f"/api/workouts/{_wid(59)}").text)
    assert raw["route"]["outline"]                     # the route itself has a shape...
    out = json.loads(mcp.get_workout(_wid(59)))
    assert out["route"] == {"link_status": "linked", "n_points": 300}     # ...the tool sends none of it
    assert not (_keys(out) & COORD_KEYS)
    text = json.dumps(out)
    assert str(LAT0)[:6] not in text and str(LON0)[:6] not in text
    anchor = out["sources"][0]
    assert anchor["events"] == {"total": 4, "counts": {"HKWorkoutEventTypeSegment": 3, "HKWorkoutEventTypePause": 1}}
    assert anchor["metadata"] == {"total": 1, "items": [["HKIndoorWorkout", "0"]]}   # distinct pairs (S1)
    assert out["workout"]["strain"] == 9.375 and out["field_sources"]["strain"]["label"] == "Whoop"
    notes = " ".join(out["notes"])
    assert "device-reported" in notes and "strain" in notes and "coordinates" in notes


def test_get_workout_without_route_and_unknown_ids(mcp):
    out = json.loads(mcp.get_workout(_wid(3)))
    assert out["route"] is None and out["workout"]["workout_id"] == _wid(3)
    err = json.loads(mcp.get_workout("wk:nope"))          # the right shape, unknown: the daemon's 404
    assert set(err) == {"error"} and "404" in err["error"]
    for bad in ("../tool/sql", "wk:a/../../ecgs", "wk:x?limit=1", "", "ecg:20260101T040000Z"):
        err = json.loads(mcp.get_workout(bad))                # any other shape never leaves the tool
        assert set(err) == {"error"} and "not a workout id" in err["error"]
    for bad in ("../workouts", "ecg:2026", "wk:xw:1:1"):
        err = json.loads(mcp.get_ecg(bad))
        assert set(err) == {"error"} and "not an ECG id" in err["error"]


def test_a_bad_id_never_reaches_the_daemon(monkeypatch):
    from heliosd.mcp_server import server as mcp_server

    class C:
        def get(self, path, params=None):
            pytest.fail(f"called {path}")
    monkeypatch.setattr(mcp_server, "_client", C())
    assert "error" in json.loads(mcp_server.get_workout("../tool/sql"))
    assert "error" in json.loads(mcp_server.get_ecg("ecg:x/../../tool/sql"))


def test_list_ecgs_and_get_ecg_carry_no_waveform(mcp):
    lst = json.loads(mcp.list_ecgs())
    assert lst["total"] == 3 and lst["newer_records"] == 1 and len(lst["ecgs"]) == 3
    assert "device-reported" in lst["classification_note"].lower()
    one = json.loads(mcp.get_ecg(_ecg(1)))
    assert one["ecg"]["ecg_id"] == _ecg(1) and one["ecg"]["classification"] == "Sinus Rhythm"
    assert one["ecg"] == {e["ecg_id"]: e for e in lst["ecgs"]}[_ecg(1)]
    for out in (lst, one):
        assert "microvolts" not in json.dumps(out)
        assert any("does not interpret" in n for n in out["notes"])
    err = json.loads(mcp.get_ecg("ecg:20990101T000000Z"))
    assert set(err) == {"error"} and "404" in err["error"]


def test_a_daemon_without_the_route_is_an_error_not_a_page(monkeypatch):
    # Before P4 GET /api/ecgs/{id} answered the app shell (HTML, 200).
    from heliosd.mcp_server import server as mcp_server

    class R:
        status_code, text = 200, "<!doctype html><div id=root></div>"

        def json(self):
            raise ValueError("not JSON")

    class C:
        def get(self, path, params=None):
            return R()
    monkeypatch.setattr(mcp_server, "_client", C())
    err = json.loads(mcp_server.get_ecg("ecg:20260101T040000Z"))
    assert set(err) == {"error"} and "not JSON" in err["error"]


def _texts(mcp_server) -> list[str]:
    out = [getattr(mcp_server, t).__doc__ or "" for t in TOOLS]
    out += [n for name in dir(mcp_server) if name.endswith("_NOTES") for n in getattr(mcp_server, name)]
    return out


def test_tool_text_says_device_reported_and_carries_no_judgement_word():
    from heliosd.mcp_server import server as mcp_server
    for t in TOOLS:
        assert "device-reported" in (getattr(mcp_server, t).__doc__ or ""), t
    texts = _texts(mcp_server)
    assert len(texts) >= 8
    hits = [m.group(0) for s in texts for m in JUDGEMENT.finditer(s)]
    assert hits == []
    assert "newer workout records" in " ".join(texts)


# ---------- review round 1 (2026-10-31) ----------

class _Fake:
    def __init__(self, body: dict, code: int = 200):
        self.body, self.code = body, code

    def get(self, path, params=None):
        body, code = self.body, self.code

        class R:
            status_code, text = code, json.dumps(body)

            def json(self):
                return body
        return R()


def test_get_ecg_notes_name_no_field_the_answer_lacks(mcp):
    one = json.loads(mcp.get_ecg(_ecg(1)))
    assert "newer_records" not in one
    assert not any("newer_records" in n for n in one["notes"])
    assert any("does not interpret" in n for n in one["notes"])


@pytest.mark.parametrize("bad", ["ecg:20260101T040000Z\n", "ecg:٢٠٢٦٠١٠١T040000Z"])
def test_a_trailing_newline_or_non_ascii_digits_never_leave_the_tool(monkeypatch, bad):
    from heliosd.mcp_server import server as mcp_server

    class C:
        def get(self, path, params=None):
            pytest.fail(f"called {path!r}")
    monkeypatch.setattr(mcp_server, "_client", C())
    assert "not an ECG id" in json.loads(mcp_server.get_ecg(bad))["error"]
    assert "not a workout id" in json.loads(mcp_server.get_workout("wk:xw:1:1\n"))["error"]


def test_the_tool_id_shapes_are_the_servers():
    from heliosd.history import views
    from heliosd.mcp_server import server as mcp_server
    assert mcp_server.WORKOUT_ID.pattern == views.WORKOUT_ID.pattern
    assert mcp_server.ECG_ID.pattern == views.ECG_ID.pattern
    assert views.ECG_ID.match("ecg:20260101T040000Z\n") is None
    assert views.WORKOUT_ID.match("wk:xw:1:1\n") is None


def test_daemon_notes_are_kept_ahead_of_the_wording_notes(monkeypatch):
    from heliosd.mcp_server import server as mcp_server
    monkeypatch.setattr(mcp_server, "_client", _Fake({"ecgs": [], "notes": ["synthetic daemon note"]}))
    out = json.loads(mcp_server.list_ecgs())
    assert out["notes"][0] == "synthetic daemon note" and len(out["notes"]) == 1 + len(mcp_server.ECG_NOTES)


def test_get_workout_sends_only_known_fields_and_no_location_like_metadata(monkeypatch):
    from heliosd.mcp_server import server as mcp_server
    detail = {"reporting_zone": "UTC", "workout": {"workout_id": "wk:x"}, "field_sources": {},
              "route": {"link_status": "linked", "n_points": 3, "outline": [[0, 0]], "start_point": [1, 2]},
              "bbox": {"north": 1}, "sources": [{
                  "source_id": "x", "events": {"total": 1, "counts": {"Pause": 1}, "items": [{"date_utc": "t"}]},
                  "metadata": {"total": 4, "items": [["HKIndoorWorkout", "0"], ["SyntheticGPSStart", "1,2"],
                                                     ["Synthetic Latitude", "1"], ["HKSwimmingLocationType", "1"]]}}]}
    monkeypatch.setattr(mcp_server, "_client", _Fake(detail))
    out = json.loads(mcp_server.get_workout("wk:xw:1:1"))
    assert set(out) == {"reporting_zone", "workout", "field_sources", "sources", "route", "notes"}
    assert out["route"] == {"link_status": "linked", "n_points": 3}
    assert out["sources"][0]["metadata"] == {"total": 2, "items": [["HKIndoorWorkout", "0"],
                                                                   ["HKSwimmingLocationType", "1"]]}
    assert out["sources"][0]["events"] == {"total": 1, "counts": {"Pause": 1}}


@pytest.mark.parametrize("call", [lambda m: m.list_workouts(cursor="not-a-cursor"), lambda m: m.list_workouts(limit=0),
                                  lambda m: m.list_ecgs(limit=201), lambda m: m.list_ecgs(cursor="bad")])
def test_bad_paging_comes_back_as_the_daemons_error_without_notes(mcp, call):
    out = json.loads(call(mcp))
    assert set(out) == {"error"} and "422" in out["error"]
