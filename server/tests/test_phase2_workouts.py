"""Phase 2 canonical workout sessions (plan v2 4.5): explicit links, one-to-one candidates,
the queue and the field owners. The fixture's 15 workouts are worked out by hand in the
comments below; the boundary tests use made-up records."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

import pytest

from heliosd.config import load_source_registry
from heliosd.history import export_xml as ex
from heliosd.history import workouts as wk
from heliosd.trust.registry import SourceRegistry

from .phase2_helpers import FIXTURE


def fixture_sources() -> list[dict]:
    reg = SourceRegistry(dict(load_source_registry(), ignored_mode="store"))
    out, seen = [], Counter()
    for _o, kind, r in ex.iter_export(FIXTURE / "export.xml"):
        if kind == "Workout":
            r["device_key"] = reg.resolve(r["source_name"])
            r["family"] = wk.source_family(r["device_key"])
            r["source_id"] = f"xw:{r['ident']}:{seen[r['ident']]}"
            seen[r["ident"]] += 1
            out.append(r)
    return out


def fixture_route_refs(src: list[dict]) -> dict:
    """As the importer builds them: W1 names route A alone; W4, its copy W5 and W6 name route B; W13 and
    W14 name route C; W15 names a file the export does not hold."""
    rid = {"route_2026-05-01_10.00am.gpx": "rt:a", "route_2026-05-03_7.15am.gpx": "rt:b",
           "route_2026-05-05_6.00pm.gpx": "rt:c"}
    n = Counter(p for w in src for r in w["routes"] for p in r["file_refs"])
    refs = {}
    for w in src:
        for r in w["routes"]:
            for p in r["file_refs"]:
                name = p.rsplit("/", 1)[1]
                refs.setdefault(w["source_id"], []).append(
                    {"route_ref_id": f"{w['source_id']}:route:{r['ord']}", "route_id": rid.get(name),
                     "link_status": "missing_file" if name not in rid else ("shared_file" if n[p] > 1 else "linked")})
    return refs


@pytest.fixture(scope="module")
def built():
    src = fixture_sources()
    return src, wk.build_sessions(src, fixture_route_refs(src))


def test_the_fixture_sources_and_their_families(built):
    src, _ = built
    assert len(src) == 15
    assert Counter(w["family"] for w in src) == {"apple": 5, "whoop": 4, "zepp": 3, "test_cgm": 3}
    assert src[3]["source_id"].endswith(":0") and src[4]["source_id"].endswith(":1")      # W4 and its copy W5
    assert src[3]["source_id"][:-2] == src[4]["source_id"][:-2]


def test_the_sessions_worked_out_by_hand(built):
    """W1 apple walk + W2 whoop + W3 zepp: one session. W4 test_cgm ride + its copy W5 + W6 zepp: one
    session (no apple or whoop, so test_cgm anchors). W7 apple walk and W8 whoop 'Other' overlap but differ
    in family: a near miss. W9 apple walk has two whoop candidates W10 and W11: both queued, W9 too. W12
    yoga alone: single. W13 zepp and W14 test_cgm walk overlap with a union too long: a near miss, and
    they name one route file from two sessions. W15 names a missing route file."""
    src, b = built
    W = {i + 1: w["source_id"] for i, w in enumerate(src)}
    sess = {s["workout_id"]: s for s in b["sessions"]}
    assert len(sess) == 11
    assert Counter(s["status"] for s in sess.values()) == {"matched": 2, "queued": 8, "single": 1}
    p = b["sources"]
    assert {p[W[i]]["workout_id"] for i in (1, 2, 3)} == {"wk:" + W[1]}
    assert (p[W[1]]["role"], p[W[2]]["role"], p[W[3]]["role"]) == ("anchor", "member", "member")
    assert p[W[2]]["link_rule"] == "candidate_v1" and p[W[2]]["match_detail"]["iou"] == 0.95
    assert {p[W[i]]["workout_id"] for i in (4, 5, 6)} == {"wk:" + W[4]}
    assert (p[W[5]]["role"], p[W[5]]["link_rule"], p[W[5]]["independence"]) == ("copy", "explicit_copy", "same_writer_copy")
    assert sess["wk:" + W[4]]["n_sources"] == 3 and sess["wk:" + W[1]]["n_sources"] == 3
    assert sess["wk:" + W[12]]["status"] == "single"
    for i in (7, 8, 9, 10, 11, 13, 14, 15):
        assert sess["wk:" + W[i]]["status"] == "queued", i
    q = Counter(x["kind"] for x in b["queue"])
    assert q == {"multiple_candidates": 2, "near_miss": 2, "shared_route_file": 1, "missing_route_file": 1}
    mc = [x for x in b["queue"] if x["kind"] == "multiple_candidates"]
    assert sorted(x["source_ids"][0] for x in mc) == sorted([W[10], W[11]]) and all(x["candidate_ids"] == [W[9]] for x in mc)
    nm = {tuple(x["source_ids"]): x for x in b["queue"] if x["kind"] == "near_miss"}
    assert set(nm) == {tuple(sorted([W[7], W[8]])), tuple(sorted([W[13], W[14]]))}
    assert nm[tuple(sorted([W[7], W[8]]))]["detail"]["failed"] == ["activity_family"]
    assert nm[tuple(sorted([W[13], W[14]]))]["detail"]["failed"] == ["overlap", "span_ratio"]


def test_field_owners_and_their_sources(built):
    src, b = built
    W = {i + 1: w["source_id"] for i, w in enumerate(src)}
    s1 = next(s for s in b["sessions"] if s["workout_id"] == "wk:" + W[1])
    assert (s1["activity_type"], s1["type_source"]) == ("HKWorkoutActivityTypeWalking", W[1])
    assert (s1["start_utc"], s1["end_utc"], s1["duration_s"], s1["timing_source"]) == \
        (datetime(2026, 5, 1, 6, 0), datetime(2026, 5, 1, 6, 40), 2280.0, W[1])
    assert (s1["tz_name"], s1["tz_source"]) == ("Asia/Dubai", W[1])
    assert (s1["distance"], s1["distance_unit"], s1["distance_type"], s1["distance_source"]) == \
        (3.125, "km", "HKQuantityTypeIdentifierDistanceWalkingRunning", W[1])
    assert (s1["energy_kcal"], s1["energy_source"]) == (151.5, W[1])            # apple's, not whoop's or zepp's
    assert (s1["hr_avg"], s1["hr_min"], s1["hr_max"], s1["hr_source"]) == (111.5, 91.0, 131.0, W[1])   # zepp has none
    assert (s1["strain"], s1["strain_source"]) == (8.5, W[2])
    assert (s1["route_id"], s1["route_source"]) == ("rt:a", W[1] + ":route:0")
    s2 = next(s for s in b["sessions"] if s["workout_id"] == "wk:" + W[4])
    assert (s2["activity_type"], s2["energy_kcal"], s2["energy_source"]) == ("HKWorkoutActivityTypeCycling", 201.5, W[4])
    assert (s2["distance"], s2["distance_type"]) == (12.25, "HKQuantityTypeIdentifierDistanceCycling")
    assert s2["strain"] is None and s2["hr_avg"] is None
    assert (s2["route_id"], s2["route_source"]) == ("rt:b", W[4] + ":route:0")   # every ref of B is in this session
    for i in (13, 14):                                                           # C is named from two sessions
        assert next(s for s in b["sessions"] if s["workout_id"] == "wk:" + W[i])["route_id"] is None
    s8 = next(s for s in b["sessions"] if s["workout_id"] == "wk:" + W[8])
    assert (s8["strain"], s8["strain_source"], s8["activity_type"]) == (6.25, W[8], "HKWorkoutActivityTypeOther")


def test_the_plan_v2_comparison_counts(built):
    _, b = built
    st = b["stats"]
    assert (st["representatives"], st["copies"], st["sessions"]) == (14, 1, 11)
    fam = st["by_family_against_apple"]
    # W2 and W10 start and end within a minute of W1 and W9; W11 starts two minutes late; W8 is 'Other'.
    assert fam["whoop"] == {"records": 4, "passing_an_apple_record": 3, "near_exact_copy_of_apple": 2,
                            "linked_to_an_apple_session": 1}
    assert fam["zepp"]["passing_an_apple_record"] == 1 and fam["zepp"]["linked_to_an_apple_session"] == 1


# ---- the rule's boundaries, on made-up records

T0 = datetime(2026, 1, 1, 8, 0)


def _w(sid, family, start_min, end_min, activity="HKWorkoutActivityTypeWalking", stats=(), meta=()):
    return {"source_id": sid, "family": family, "activity_type": activity, "start_utc": T0 + timedelta(minutes=start_min),
            "end_utc": T0 + timedelta(minutes=end_min), "duration_s": (end_min - start_min) * 60.0,
            "creation_utc": T0, "ordinal": int(sid[1:]), "source_name": f"Synthetic {family}", "tz_name": None,
            "metadata": list(meta), "statistics": list(stats), "total_distance_raw": None, "total_distance_unit": None,
            "total_energy_raw": None, "total_energy_unit": None}


def _linked(a, o) -> bool:
    b = wk.build_sessions([a, o])
    return b["sources"][o["source_id"]]["workout_id"] == "wk:" + a["source_id"]


@pytest.mark.parametrize("other,linked", [
    ((5, 40), True),                    # start 300 s later: inside the rule (overlap 0.875)
    ((5 + 1 / 60, 40), False),          # 301 s, nothing else failing
    ((0, 48), True),                    # span ratio 1.20 (overlap 0.833)
    ((0, 48.4), False),                 # span ratio 1.21 (overlap still 0.826)
    ((0, 32), True),                    # overlap over union exactly 0.80
    ((0, 31.9), False),                 # 0.7975
])
def test_the_rule_boundaries(other, linked):
    a = _w("a1", "apple", 0, 40)
    o = _w("b2", "whoop", *other)
    assert _linked(a, o) is linked


def test_a_different_activity_family_never_links():
    a = _w("a1", "apple", 0, 40)
    assert _linked(a, _w("b2", "whoop", 0, 40, activity="HKWorkoutActivityTypeRunning")) is False
    assert _linked(a, _w("b2", "whoop", 0, 40, activity="HKWorkoutActivityTypeHiking")) is True     # walk family


def test_exercise_heart_rate_order_is_strap_whoop_apple():
    """Owner decisions file, section 2 item 1: both upper-arm devices rank above the wrist."""
    hr = lambda v: {"hk_type": wk.HR_TYPE, "stat_avg": v, "stat_min": v - 10, "stat_max": v + 10, "unit": "count/min",
                    "stat_sum": None}
    a = _w("a1", "apple", 0, 40, stats=[hr(120.0)])
    w = _w("w2", "whoop", 0, 40, stats=[hr(122.0)])
    s = wk.build_sessions([a, w])["sessions"][0]
    assert (s["hr_avg"], s["hr_source"], s["timing_source"]) == (122.0, "w2", "a1")
    assert wk.FIELD_ORDER["hr"] == ("zepp", "whoop", "apple")


def test_exercise_heart_rate_prefers_the_strap_and_strain_comes_from_whoop_only():
    hr = lambda v: {"hk_type": wk.HR_TYPE, "stat_avg": v, "stat_min": v - 10, "stat_max": v + 10, "unit": "count/min",
                    "stat_sum": None}
    a = _w("a1", "apple", 0, 40, stats=[hr(120.0)], meta=[["WHOOP Strain", "9.0"]])
    z = _w("z2", "zepp", 0, 40, stats=[hr(125.0)])
    s = wk.build_sessions([a, z])["sessions"][0]
    assert (s["hr_avg"], s["hr_source"]) == (125.0, "z2")
    assert s["strain"] is None                       # only a whoop record's strain counts
    w = _w("w3", "whoop", 0, 40, stats=[hr(118.0)], meta=[["WHOOP Strain", "7.5"]])
    s = wk.build_sessions([w])["sessions"][0]
    assert (s["hr_avg"], s["strain"], s["status"]) == (118.0, 7.5, "single")


def test_two_records_of_one_family_that_overlap_are_kept_apart_and_queued():
    b = wk.build_sessions([_w("a1", "apple", 0, 40), _w("a2", "apple", 10, 50)])
    assert len(b["sessions"]) == 2 and {s["status"] for s in b["sessions"]} == {"queued"}
    assert [q["kind"] for q in b["queue"]] == ["same_family_overlap"]


def test_a_record_without_readable_times_is_queued_alone():
    bad = _w("a1", "apple", 0, 40)
    bad["start_utc"] = None
    b = wk.build_sessions([bad, _w("b2", "whoop", 0, 40)])
    assert {q["kind"] for q in b["queue"]} == {"untimed"}
    assert next(s for s in b["sessions"] if s["workout_id"] == "wk:a1")["status"] == "queued"


def test_activity_and_source_families():
    assert wk.activity_family("HKWorkoutActivityTypeStairClimbing") == "cardio"
    assert wk.activity_family("HKWorkoutActivityTypeCurling") == "curling"
    assert [wk.source_family(k) for k in ("apple_watch_ultra", "iphone", "whoop", "zepp_helio", "test_cgm", None)] == \
        ["apple", "apple", "whoop", "zepp", "test_cgm", "other"]
    assert wk.family_order({"zepp", "test_cgm", "whoop", "apple"}) == ["apple", "whoop", "test_cgm", "zepp"]
