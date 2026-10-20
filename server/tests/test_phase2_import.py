"""Phase 2 importer end to end on the synthetic export: the v5 tables filled and checked in
both directions, registered records linked to the samples rows that already hold them, the
samples path untouched, a second run that writes nothing, the July-style side files read as
the same recordings, and an import that either completes and verifies or leaves the store as
it was. Every expected number is counted by hand from tests/fixtures/phase2/june."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import duckdb
import pytest

from heliosd.history import importer
from heliosd.store import db

from .phase2_helpers import june, july_twin, store

TOOLS = Path(__file__).resolve().parents[1] / "tools"
V5 = db.PHASE2_TABLES
DISTINCT_VALUES = ("71.25", "74.5", "79.75", "45.125", "3.515625", "151.5", "111.5", "171.5", "201.5", "12.25",
                   "450.5", "520.25", "Synthetic Person", "1990-01-01")


def _tool():
    spec = importlib.util.spec_from_file_location("import_history_phase2_under_test", TOOLS / "import_history_phase2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(tmp_path, folders, st, name="out", extra=()):
    tool = _tool()
    args = [str(st)] + [a for f in folders for a in ("--export", str(f))] + ["--out", str(tmp_path / name), *extra]
    rc = tool.main(args)
    p = tmp_path / name / "reconciliation.json"
    return rc, (json.loads(p.read_text()) if p.exists() else None), tool


def _q(path, sql, params=None):
    c = duckdb.connect(str(path), read_only=True)
    try:
        return c.execute(sql, params or []).fetchall()
    finally:
        c.close()


def _counts(path):
    return {t: _q(path, f"SELECT COUNT(*) FROM {t}")[0][0] for t in V5}


def _digests(path):
    c = duckdb.connect(str(path), read_only=True)
    try:
        tables = [r[0] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' "
                                          "AND table_type = 'BASE TABLE'").fetchall()]
        return {t: importer.table_digest(c, t) for t in tables}
    finally:
        c.close()


@pytest.fixture()
def imported(tmp_path):
    j = june(tmp_path)
    st = store(tmp_path)
    before = _digests(st)
    rc, rep, _ = _run(tmp_path, [j], st)
    return tmp_path, j, st, before, rc, rep


def test_a_full_import_reconciles_in_both_directions(imported):
    tmp_path, j, st, before, rc, rep = imported
    assert rc == 0, rep.get("stopped")
    assert rep["checks"] and all(v["ok"] for v in rep["checks"].values()), [k for k, v in rep["checks"].items() if not v["ok"]]
    assert _counts(st) == {"export_files": 7, "import_runs": 1, "raw_records": 19, "workout_sources": 15, "workouts": 11,
                           "workout_statistics": 11, "workout_events": 3, "workout_metadata": 15, "workout_routes": 7,
                           "route_points": 12, "activity_summaries": 3, "ecgs": 2, "ecg_waveforms": 2,
                           "workout_ambiguity": 6}
    assert dict(_q(st, "SELECT element, COUNT(*) FROM raw_records GROUP BY 1")) == \
        {"Record": 17, "Correlation": 1, "VisionPrescription": 1}
    rec = rep["reconciliation"]["export_xml"][0]
    assert rec["elements"] == {"ExportDate": 1, "Me": 1, "Record": 17, "Correlation": 1, "VisionPrescription": 1,
                               "Workout": 15, "ActivitySummary": 3}
    assert rec["me_not_imported"] == 1 and rec["unregistered_types"] == 5
    assert rec["correlations"] == {"n": 1, "members": 2, "members_found_top_level": 2}
    row = _q(st, "SELECT name, input_fingerprint, summary FROM migrations WHERE name = ?", [importer.MIGRATION])
    assert row and json.loads(row[0][2])["phase"] == "verified" and row[0][1] == rep["input_fingerprint"]


def test_the_samples_path_is_untouched(imported):
    tmp_path, j, st, before, rc, rep = imported
    after = _digests(st)
    for t, d in before.items():
        if t in V5 or t == "migrations":
            continue
        assert after[t] == d, t
    assert rep["checks"]["protected_tables_unchanged"]["ok"]


def test_registered_records_link_to_the_rows_that_hold_them(imported):
    tmp_path, j, st, before, rc, rep = imported
    got = _q(st, """SELECT hk_type, value_raw, device_key, link_status, linked_sample_id, link_reason FROM raw_records
                    WHERE metric IS NOT NULL ORDER BY ordinal""")
    hr, steps = "HKQuantityTypeIdentifierHeartRate", "HKQuantityTypeIdentifierStepCount"
    assert got == [
        (hr, "71.25", "apple_watch_ultra", "bridge", "hk:syn-hr-1", None),
        (hr, "74.5", "apple_watch_ultra", "bridge", "hk:syn-hr-2", None),
        (hr, "79.75", "apple_watch_ultra", "none", None, "store_row_differs_in_unit_text_or_device"),
        (steps, "121", "apple_watch_ultra", "bridge", "hk:syn-steps-1", None),
        (steps, "121", "apple_watch_ultra", "none", None, "store_rows_all_claimed"),       # the twin: one row only
        ("HKQuantityTypeIdentifierHeartRateVariabilitySDNN", "45.125", "apple_watch_ultra", "bridge", "hk:syn-sdnn-1", None),
        ("HKCategoryTypeIdentifierSleepAnalysis", "HKCategoryValueSleepAnalysisAsleepCore", "apple_watch_ultra", "bridge",
         "hk:syn-sleep-1", None),
        ("HKCategoryTypeIdentifierSleepAnalysis", "HKCategoryValueSleepAnalysisAsleepDeep", "whoop", "none", None,
         "deleted_in_store"),
        (hr, "60.5", "excluded", "none", None, "ignored_source"),
        (hr, "66.5", "apple_watch_ultra", "export", "ch2:syn-export-hr", None),
        ("HKQuantityTypeIdentifierActiveEnergyBurned", "3.515625", "apple_watch_ultra", "bridge", "hk:syn-energy-1", None),
        ("HKQuantityTypeIdentifierActiveEnergyBurned", "5.0", "apple_watch_ultra", "none", None, "no_value_within_tolerance"),
    ]
    assert _q(st, "SELECT COUNT(*) FROM raw_records WHERE metric IS NULL AND link_status IS NULL")[0][0] == 7
    ids = [r[0] for r in _q(st, "SELECT record_id FROM raw_records WHERE hk_type = ? ORDER BY ordinal", [steps])]
    assert ids[0][:-2] == ids[1][:-2] and (ids[0][-2:], ids[1][-2:]) == (":0", ":1")
    side = {(x["metric"], x["sync_path"], x["quality"]): (x["rows"], x["claimed"], x["covered_through_twin"], x["not_in_export"])
            for x in rep["reconciliation"]["export_xml"][0]["store_rows_before_the_export"]}
    assert side == {("heart_rate", "bridge", "eligible"): (3, 2, 0, 1),              # the late row is not in the export
                    ("heart_rate", "bridge", "unit_mismatch"): (1, 0, 0, 1),
                    ("heart_rate", "health_export", "eligible"): (1, 1, 0, 0),
                    ("heart_rate", "health_export", "export_duplicate"): (1, 0, 1, 0),   # its Bridge twin was claimed
                    ("steps", "bridge", "eligible"): (1, 1, 0, 0),
                    ("hrv_sdnn", "bridge", "eligible"): (1, 1, 0, 0),
                    ("sleep_analysis", "bridge", "eligible"): (1, 1, 0, 0),
                    ("active_energy", "bridge", "eligible"): (2, 1, 0, 1)}


def test_the_rest_of_the_envelope_rings_and_ecgs(imported):
    tmp_path, j, st, before, rc, rep = imported
    corr = _q(st, "SELECT children, metadata FROM raw_records WHERE element = 'Correlation'")[0]
    assert [m["hk_type"] for m in json.loads(corr[0])["members"]] == ["HKQuantityTypeIdentifierDietaryProtein",
                                                                      "HKQuantityTypeIdentifierDietaryFatTotal"]
    assert _q(st, "SELECT summary_id, partial FROM activity_summaries ORDER BY 1") == \
        [("as:2026-05-04", False), ("as:2026-05-05", False), ("as:2026-05-06", True)]
    assert _q(st, "SELECT ecg_id, e.sample_rate_hz, e.n_samples, len(w.microvolts) FROM ecgs e JOIN ecg_waveforms w "
                  "USING (ecg_id) ORDER BY 1") == [("ecg:20260501T050000Z", 512.0, 24, 24),
                                                   ("ecg:20260502T173000Z", 514.039, 26, 26)]
    files = dict(_q(st, "SELECT kind, COUNT(*) FROM export_files GROUP BY 1"))
    assert files == {"export_xml": 1, "ecg_csv": 2, "route_gpx": 3, "attachment": 1}
    assert _q(st, "SELECT export_date, n_items FROM export_files WHERE kind = 'export_xml'") == [("2026-05-06 09:00:00 +0400", 39)]


def test_workouts_routes_and_the_queue_in_the_store(imported):
    tmp_path, j, st, before, rc, rep = imported
    w = rep["reconciliation"]["workouts"]
    assert w["sessions_by_status"] == {"matched": 2, "queued": 8, "single": 1}
    assert w["queue"] == {"multiple_candidates": 2, "near_miss": 2, "shared_route_file": 1, "missing_route_file": 1}
    assert w["routes"]["by_status"] == {"linked": 1, "shared_file": 5, "missing_file": 1}
    assert w["metadata_repeats_same_value"] == 5 and w["metadata_repeats_different_value"] == 0
    assert _q(st, "SELECT COUNT(*) FROM workouts WHERE route_id IS NOT NULL")[0][0] == 2
    assert _q(st, "SELECT COUNT(DISTINCT route_id) FROM route_points")[0][0] == 3
    # every source row points at a stored session, every session field at a source row of its own session
    assert _q(st, "SELECT COUNT(*) FROM workout_sources s LEFT JOIN workouts w USING (workout_id) WHERE w.workout_id IS NULL")[0][0] == 0
    assert rep["checks"]["every canonical field names its source row"]["ok"]


def test_a_second_run_writes_nothing(imported):
    tmp_path, j, st, before, rc, rep = imported
    first = _digests(st)
    rc2, rep2, _ = _run(tmp_path, [j], st, name="out2")
    assert rc2 == 0, rep2.get("stopped")
    assert all(c["written"] == 0 for c in rep2["counts"].values()), rep2["counts"]
    assert rep2["counts"]["raw_records"]["already_present"] == 19 and rep2["migration_row"] == "already present"
    second = _digests(st)
    assert {t for t in first if first[t] != second[t]} == {"import_runs"}
    assert _q(st, "SELECT COUNT(*) FROM import_runs")[0][0] == 2


def test_the_july_side_files_are_the_same_recordings(tmp_path):
    j = june(tmp_path)
    k = july_twin(j, tmp_path)
    st = store(tmp_path)
    rc, rep, _ = _run(tmp_path, [j, k], st)
    assert rc == 0, rep.get("stopped")
    assert _q(st, "SELECT COUNT(*) FROM ecgs")[0][0] == 2 and _q(st, "SELECT COUNT(*) FROM route_points")[0][0] == 12
    assert rep["ecg"]["same_recording_in_another_file"] == 2 and rep["ecg"]["conflicts"] == 0
    pairs = _q(st, """SELECT a.content_id = b.content_id FROM export_files a JOIN export_files b
                      ON a.kind = b.kind AND a.export_label < b.export_label AND a.content_id = b.content_id""")
    assert len(pairs) == 5                                          # 2 ECGs and 3 routes, once in each export
    assert dict(_q(st, "SELECT export_label, COUNT(*) FROM export_files GROUP BY 1")) == {j.name: 7, k.name: 6}


def test_a_manifest_that_does_not_verify_stops_before_any_write(tmp_path):
    j = june(tmp_path)
    st = store(tmp_path)
    before = _digests(st)
    (j / "workout-routes" / "route_2026-05-05_6.00pm.gpx").write_text("tampered", encoding="utf-8")
    rc, rep, _ = _run(tmp_path, [j], st)
    assert rc == 1 and "manifest" in rep["stopped"]
    assert _digests(st) == before


def test_a_failed_check_rolls_everything_back(tmp_path, monkeypatch):
    j = june(tmp_path)
    st = store(tmp_path)
    before = _digests(st)
    real = importer.protected_digests
    calls = []

    def flaky(conn):
        calls.append(1)
        d = real(conn)
        if len(calls) == 2:
            d["samples"] = [d["samples"][0] + 1, "changed"]
        return d
    monkeypatch.setattr(importer, "protected_digests", flaky)
    rc, rep, _ = _run(tmp_path, [j], st)
    assert rc == 1 and "protected_tables_unchanged" in rep["stopped"]
    assert _digests(st) == before                                   # no v5 row, no migrations row, no run row
    assert not any(p.name.startswith(".p2-") for p in tmp_path.iterdir())     # staging removed


def test_an_unknown_shape_stops_and_keeps_nothing(tmp_path):
    j = june(tmp_path)
    x = j / "export.xml"
    x.write_text(x.read_text(encoding="utf-8").replace("<ActivitySummary dateComponents=\"2026-05-04\"",
                                                       "<Audiogram2/><ActivitySummary dateComponents=\"2026-05-04\"", 1),
                 encoding="utf-8")
    from .phase2_helpers import write_manifest
    write_manifest(j)
    st = store(tmp_path)
    before = _digests(st)
    rc, rep, _ = _run(tmp_path, [j], st)
    assert rc == 1 and "UnknownShape" in rep["stopped"]
    assert _digests(st) == before


def test_the_live_store_is_refused(tmp_path, monkeypatch):
    j = june(tmp_path)
    st = store(tmp_path)
    tool = _tool()
    monkeypatch.setattr(tool.rd, "LIVE_STORE", st)
    sha = hashlib.sha256(st.read_bytes()).hexdigest()
    assert tool.main([str(st), "--export", str(j), "--out", str(tmp_path / "o")]) == 2
    monkeypatch.setattr(tool.rd, "holders", lambda p: [4242])                  # the daemon still holds it
    assert tool.main([str(st), "--export", str(j), "--out", str(tmp_path / "o"), "--apply"]) == 2
    assert hashlib.sha256(st.read_bytes()).hexdigest() == sha
    monkeypatch.setattr(tool.rd, "holders", lambda p: [])                      # stopped: --apply may run
    assert tool.main([str(st), "--export", str(j), "--out", str(tmp_path / "o"), "--apply"]) == 0


def test_the_report_and_log_carry_no_measured_value(imported):
    tmp_path, j, st, before, rc, rep = imported
    for name in ("reconciliation.json", "run.log"):
        text = (tmp_path / "out" / name).read_text(encoding="utf-8")
        leaked = [v for v in DISTINCT_VALUES if v in text]
        assert not leaked, (name, leaked)


def test_the_daemon_opens_the_imported_store(imported):
    tmp_path, j, st, before, rc, rep = imported
    conn = db.connect(st)
    try:
        assert db.migration_applied(conn, importer.MIGRATION)
        assert db.unverified_migrations(conn) == []
    finally:
        conn.close()
