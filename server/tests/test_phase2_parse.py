"""Phase 2 readers: export.xml, the ECG and GPX side files and the export manifest.

The synthetic export in tests/fixtures/phase2/june is tiny and made up. Every
expected number below is counted by hand from those files."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

import pytest

from heliosd.history import export_xml as ex
from heliosd.history import sidefiles as sf

from .phase2_helpers import FIXTURE, june, july_twin, write_manifest


def _all(path):
    return list(ex.iter_export(path))


def test_every_top_level_element_in_document_order():
    items = _all(FIXTURE / "export.xml")
    assert items[0] == (-1, "HealthData", {"locale": "en_US"})
    kinds = Counter(k for _, k, _ in items[1:])
    assert kinds == {"ExportDate": 1, "Me": 1, "Record": 17, "Correlation": 1, "VisionPrescription": 1, "Workout": 15,
                     "ActivitySummary": 3}
    assert [o for o, _, _ in items[1:]] == list(range(39))          # the ordinal is the top-level index
    assert items[1][2] == {"value": "2026-05-06 09:00:00 +0400"}
    assert items[2][2] == {"n_attributes": 5}                        # personal characteristics: counted, never kept


def test_a_record_keeps_every_field_verbatim_and_parses_the_instant():
    rec = _all(FIXTURE / "export.xml")[3][2]
    assert rec["hk_type"] == "HKQuantityTypeIdentifierHeartRate" and rec["value_raw"] == "71.25" and rec["value"] == 71.25
    assert rec["start_raw"] == "2026-05-01 10:05:00 +0400"
    assert rec["start_utc"] == datetime(2026, 5, 1, 6, 5) and rec["print_offset_min"] == 240
    assert rec["creation_utc"] == datetime(2026, 5, 1, 6, 6)
    assert json.loads(rec["metadata"]) == [["HKMetadataKeyHeartRateMotionContext", "1"]]
    assert rec["device"].startswith("<<HKDevice: 0x600000001>, name:Apple Watch")
    assert rec["children"] is None and len(rec["ident"]) == 24


def test_hrv_beats_are_kept_as_children_and_never_in_the_identity():
    rec = next(r for _, k, r in _all(FIXTURE / "export.xml") if k == "Record" and r["hk_type"].endswith("SDNN"))
    kids = json.loads(rec["children"])
    assert kids == [{"tag": "HeartRateVariabilityMetadataList",
                     "beats": [["07:00:01.25", "61"], ["07:00:02.20", "63"], ["07:00:03.18", "62"]]}]


def test_identical_records_share_an_ident_and_a_category_has_no_unit():
    recs = [r for _, k, r in _all(FIXTURE / "export.xml") if k == "Record"]
    steps = [r for r in recs if r["hk_type"].endswith("StepCount")]
    assert len(steps) == 2 and steps[0]["ident"] == steps[1]["ident"]
    sleep = [r for r in recs if r["hk_type"].endswith("SleepAnalysis")]
    assert [r["unit"] for r in sleep] == [None, None] and sleep[0]["value"] is None
    assert len({r["ident"] for r in recs}) == len(recs) - 1


def test_the_identity_survives_another_exporting_zone_and_device_address(tmp_path):
    """The same records printed at +0530 with other device addresses (what a later export looks like) keep
    their idents; a changed value does not."""
    text = (FIXTURE / "export.xml").read_text(encoding="utf-8")
    import re

    def shift(m):
        wall = datetime.fromisoformat(m.group(1))
        from datetime import timedelta
        return '"' + (wall + timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M:%S") + ' +0530"'
    moved = re.sub(r'"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \+0400"', shift, text).replace("0x600000001", "0x7fffabcd1")
    p = tmp_path / "export.xml"
    p.write_text(moved, encoding="utf-8")
    a = {(o, r["ident"]) for o, k, r in _all(FIXTURE / "export.xml") if k in ("Record", "Workout", "Correlation")}
    b = {(o, r["ident"]) for o, k, r in _all(p) if k in ("Record", "Workout", "Correlation")}
    assert a == b
    p.write_text(moved.replace('value="71.25"', 'value="71.5"'), encoding="utf-8")
    c = {(o, r["ident"]) for o, k, r in _all(p) if k in ("Record", "Workout", "Correlation")}
    assert len(a - c) == 1


def test_a_correlation_reads_its_members_with_the_idents_of_their_top_level_twins():
    items = _all(FIXTURE / "export.xml")
    corr = next(r for _, k, r in items if k == "Correlation")
    tops = {r["ident"] for _, k, r in items if k == "Record"}
    assert [m["hk_type"] for m in corr["members"]] == ["HKQuantityTypeIdentifierDietaryProtein",
                                                       "HKQuantityTypeIdentifierDietaryFatTotal"]
    assert all(m["ident"] in tops for m in corr["members"])
    assert json.loads(corr["metadata"]) == [["HKFoodMeal", "Lunch"]]


def test_a_workout_keeps_statistics_events_routes_and_every_metadata_entry():
    w = next(r for _, k, r in _all(FIXTURE / "export.xml") if k == "Workout")
    assert w["activity_type"] == "HKWorkoutActivityTypeWalking" and w["duration_s"] == 38 * 60
    assert w["start_utc"] == datetime(2026, 5, 1, 6, 0) and w["end_utc"] == datetime(2026, 5, 1, 6, 40)
    assert [k for k, _ in w["metadata"]] == ["HKIndoorWorkout", "HKTimeZone", "HKAverageMETs", "HKIndoorWorkout",
                                             "HKTimeZone"]
    assert w["tz_name"] == "Asia/Dubai"
    assert [s["hk_type"].removeprefix("HKQuantityTypeIdentifier") for s in w["statistics"]] == ["ActiveEnergyBurned", "BasalEnergyBurned",
                                                           "DistanceWalkingRunning", "HeartRate"]
    hr = w["statistics"][3]
    assert (hr["stat_avg"], hr["stat_min"], hr["stat_max"], hr["stat_sum"]) == (111.5, 91.0, 131.0, None)
    assert [e["event_type"].removeprefix("HKWorkoutEventType") for e in w["events"]] == ["Segment", "Pause", "Resume"]
    assert w["events"][0]["duration"] == 20.0 and w["events"][1]["duration"] is None
    assert w["routes"][0]["file_refs"] == ["/workout-routes/route_2026-05-01_10.00am.gpx"]
    whoop = [r for _, k, r in _all(FIXTURE / "export.xml") if k == "Workout"][1]
    assert whoop["sync_identifier"] == "synthetic-sync-0001" and whoop["sync_version"] == 1


def test_activity_summary_and_generic_elements():
    items = _all(FIXTURE / "export.xml")
    s = [r for _, k, r in items if k == "ActivitySummary"]
    assert [x["date_components"] for x in s] == ["2026-05-04", "2026-05-05", "2026-05-06"]
    assert s[0]["active_energy_goal"] == 500.0 and s[0]["stand_hours"] == 11.0
    v = next(r for _, k, r in items if k == "VisionPrescription")
    kids = json.loads(v["children"])
    assert kids["attrs"]["type"] == "HKVisionPrescriptionTypeGlasses"
    assert [p["tag"] for p in kids["parts"]] == ["RightEye", "LeftEye", "Attachment"]
    assert v["start_utc"] == datetime(2026, 3, 31, 20, 0)


@pytest.mark.parametrize("bad", [
    ('value="71.25"', 'value="71.25" surprise="1"'),
    ("<ActivitySummary dateComponents", "<Audiogram2 x=\"1\"/><ActivitySummary dateComponents"),
    ('<WorkoutEvent type="HKWorkoutEventTypePause"', '<WorkoutActivity uuid="u"/><WorkoutEvent type="HKWorkoutEventTypePause"'),
])
def test_a_shape_the_reader_does_not_know_stops_it(tmp_path, bad):
    p = tmp_path / "export.xml"
    p.write_text((FIXTURE / "export.xml").read_text(encoding="utf-8").replace(bad[0], bad[1], 1), encoding="utf-8")
    with pytest.raises(ex.UnknownShape):
        _all(p)


def test_parse_ts_and_number():
    assert ex.parse_ts("2026-01-02 03:04:05 -0330") == (datetime(2026, 1, 2, 6, 34, 5), -210)
    assert ex.parse_ts("2026-01-02T03:04:05Z") == (None, None)
    assert ex.parse_ts(None) == (None, None)
    assert [ex.number(s) for s in ("1", "-2.5", ".5", "1e3", "nan", "inf", "", "1,5", None)] == \
        [1.0, -2.5, 0.5, 1000.0, None, None, None, None, None]
    assert ex.device_for_ident("<<HKDevice: 0x6000abc>, name:x>") == "<<HKDevice: 0x>, name:x>"


def test_ecg_files_at_both_rates_without_the_name_or_birth_date():
    a = sf.read_ecg(FIXTURE / "electrocardiograms" / "ecg_2026-05-01.csv")
    b = sf.read_ecg(FIXTURE / "electrocardiograms" / "ecg_2026-05-02.csv")
    assert (a["sample_rate_hz"], a["n_samples"], b["sample_rate_hz"], b["n_samples"]) == (512.0, 24, 514.039, 26)
    assert a["ecg_id"] == "ecg:20260501T050000Z" and a["recorded_utc"] == datetime(2026, 5, 1, 5, 0)
    assert a["device"] == "Watch7,1" and a["lead"] == "Lead I" and a["unit"] == "µV" and a["symptoms"] == ""
    assert "Name" not in a["header_keys"] and "Date of Birth" not in a["header_keys"]
    assert "Synthetic Person" not in json.dumps({k: v for k, v in a.items() if k != "microvolts"}, default=str)
    assert len(a["microvolts"]) == 24 and len(a["voltage_sha"]) == 64


def test_the_same_ecg_printed_in_another_zone_is_the_same_recording(tmp_path):
    j = june(tmp_path)
    k = july_twin(j, tmp_path)
    a = sf.read_ecg(j / "electrocardiograms" / "ecg_2026-05-01.csv")
    b = sf.read_ecg(next((k / "electrocardiograms").glob("ecg_2026-05-01*")))
    assert b["recorded_raw"].endswith("+0530") and b["recorded_utc"] == a["recorded_utc"]
    assert (b["ecg_id"], b["voltage_sha"]) == (a["ecg_id"], a["voltage_sha"])


def test_an_ecg_with_an_unknown_line_or_rate_stops(tmp_path):
    src = (FIXTURE / "electrocardiograms" / "ecg_2026-05-01.csv").read_text(encoding="utf-8")
    p = tmp_path / "e.csv"
    for bad in (src.replace("512 hertz", "512 Hz"), src.replace("Lead,Lead I", "Lead,Lead I\nLead II,x"),
                src.replace("Unit,µV\n", "")):
        p.write_text(bad, encoding="utf-8")
        with pytest.raises(sf.UnknownShape):
            sf.read_ecg(p)


def test_gpx_points_and_a_content_identity():
    g = sf.read_gpx(FIXTURE / "workout-routes" / "route_2026-05-01_10.00am.gpx")
    assert g["n_points"] == 5 and g["route_id"].startswith("rt:") and len(g["route_id"]) == 27
    p0 = g["points"][0]
    assert (p0["lat"], p0["lon"], p0["ele"], p0["speed"], p0["course"], p0["h_acc"], p0["v_acc"]) == \
        (10.0, 20.0, 5.0, 1.2, 90.0, 3.5, 2.5)
    assert p0["time_utc"] == datetime(2026, 5, 1, 6, 0) and p0["time_raw"] == "2026-05-01T06:00:00Z"
    assert g["first_utc"] == datetime(2026, 5, 1, 6, 0) and g["last_utc"] == datetime(2026, 5, 1, 6, 4)
    other = sf.read_gpx(FIXTURE / "workout-routes" / "route_2026-05-03_7.15am.gpx")
    assert other["route_id"] != g["route_id"]


def test_the_manifest_is_verified_file_by_file(tmp_path):
    j = june(tmp_path)
    m = sf.verify_manifest(j)
    assert (m["listed"], m["ok"], m["mismatch"], m["missing"]) == (7, 7, [], [])
    (j / "workout-routes" / "route_2026-05-03_7.15am.gpx").write_text("changed", encoding="utf-8")
    (j / "attachments" / "SYNTHETIC-ATTACHMENT-1.jpeg").unlink()
    m = sf.verify_manifest(j)
    assert m["mismatch"] == ["workout-routes/route_2026-05-03_7.15am.gpx"]
    assert m["missing"] == ["attachments/SYNTHETIC-ATTACHMENT-1.jpeg"]
    assert sf.verify_manifest(tmp_path)["manifest"] is None and sf.verify_manifest(tmp_path)["digests"] == {}
    write_manifest(j)
    assert sf.verify_manifest(j)["ok"] == 6
