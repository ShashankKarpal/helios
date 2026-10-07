"""Phase 1b content-twin pass (owner decision 4d, 2026-10-07; design-twins.md,
adjudication-A-twins.md; heliosd/ingest/twins.py). Every expectation below is
written by hand from the fixture, never read back from the code under test.
Fixture clock rules as in test_rebase_history (era walls; Asia/Dubai is UTC+04:00)."""

from __future__ import annotations

import json
import pathlib
import random
from datetime import date, datetime, timedelta

import duckdb

from heliosd import backup as bk
from heliosd.ingest import twins as ctw
from heliosd.ingest.bridge import ingest_batch
from heliosd.migrate import rebase_history as rh
from heliosd.signals.baselines import compute_baselines, compute_daily_values
from heliosd.signals.markers import compute_signals
from heliosd.store import db
from tests.test_rebase_history import AWU, DUBAI, MASS, SCALE, STEPS, TODAY, ZEPP, _bridge_sample, _env, _export, _legacy, _sample, run

ENERGY = "HKQuantityTypeIdentifierActiveEnergyBurned"
DIET = "HKQuantityTypeIdentifierDietaryEnergyConsumed"
MFP = "MyFitnessPal"
T1 = datetime(2026, 5, 5, 6, 0)     # case 1: a confirmed legacy row and a native row, steps 100 for 5 min (Dubai 10:00, 2026-05-05)
T2 = datetime(2026, 5, 6, 6, 0)     # case 2: two native rows, active energy 5.5
T3 = datetime(2026, 5, 7, 6, 0)     # case 3: an UNCONFIRMED legacy row (no landing) and a native row, active energy 7.25
T5 = datetime(2026, 5, 8, 2, 0)     # case 5: a near twin, body mass (Dubai 06:00)
T6 = datetime(2026, 5, 9, 6, 0)     # case 6: legacy, native and an export row, steps 40
T4 = datetime(2026, 5, 10, 8, 0)    # case 4: an exempt dietary pair, 500 kcal twice
T7 = datetime(2026, 5, 11, 6, 0)    # case 7: a three-member group, confirmed legacy plus two natives, steps 60
T8 = datetime(2026, 5, 12, 2, 0)    # case 8: the three-row `last` case, body mass 70 (legacy, confirmed), 80 (native), 70 (native)
T9 = datetime(2026, 5, 13, 6, 0)    # case 9: two native rows of a source apple-health does not hold (Zepp), active energy 3.0
T20 = datetime(2026, 5, 20, 6, 0)   # plain confirmed legacy rows after every twin day (apple-health's last day is excluded as incomplete)
T21 = datetime(2026, 5, 21, 6, 0)
T22 = datetime(2026, 5, 22, 6, 0)   # the trailing day per metric that the check excludes as apple-health's incomplete last day
T23 = datetime(2026, 5, 23, 6, 0)
T10 = datetime(2026, 5, 14, 20, 0)  # case 10: two identical legacy sleep stages (unconfirmed), 420 min core, the night ends 05-15 (Dubai)
T11 = datetime(2026, 5, 16, 2, 0)   # case 11: body mass 10 (legacy, confirmed, lowest id), 20 (native), 10 (legacy, UNCONFIRMED, highest id): the winner
SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
NEAR = 80.1 + 5e-5                  # within 1e-6 relative of 80.1, not bit-equal


def build(conn, policy, reg, shuffle: int = 0, future_pair: bool = False):
    legacy = [(("ch2:L1", "u-L1", "steps", STEPS, T1, 2, 100, "count", AWU, "apple_watch_ultra"), {"end_utc": T1 + timedelta(minutes=5)}),
              (("ch2:L3", "u-L3", "active_energy", ENERGY, T3, 1, 7.25, "kcal", AWU, "apple_watch_ultra"), {}),
              (("ch2:L5", "u-L5", "body_mass", MASS, T5, 2, 80.1, "kg", SCALE, "zepp_life_scale"), {}),
              (("ch2:L6", "u-L6", "steps", STEPS, T6, 2, 40, "count", AWU, "apple_watch_ultra"), {}),
              (("ch2:D1", "u-D1", "dietary_energy", DIET, T4, 2, 500, "kcal", MFP, "myfitnesspal"), {}),
              (("ch2:D2", "u-D2", "dietary_energy", DIET, T4, 2, 500, "kcal", MFP, "myfitnesspal"), {}),
              (("ch2:L7", "u-L7", "steps", STEPS, T7, 2, 60, "count", AWU, "apple_watch_ultra"), {}),
              (("ch2:a", "u-a", "body_mass", MASS, T8, 2, 70, "kg", SCALE, "zepp_life_scale"), {}),
              (("ch2:L20", "u-L20", "steps", STEPS, T20, 2, 10, "count", AWU, "apple_watch_ultra"), {}),
              (("ch2:E21", "u-E21", "active_energy", ENERGY, T21, 2, 2.0, "kcal", AWU, "apple_watch_ultra"), {}),
              (("ch2:L22", "u-L22", "steps", STEPS, T22, 2, 11, "count", AWU, "apple_watch_ultra"), {}),
              (("ch2:E23", "u-E23", "active_energy", ENERGY, T23, 2, 2.5, "kcal", AWU, "apple_watch_ultra"), {}),
              (("ch2:S1", "u-S1", "sleep_analysis", SLEEP, T10, 2, 420, "min", AWU, "apple_watch_ultra"), {"end_utc": T10 + timedelta(hours=7), "text": "core"}),
              (("ch2:S2", "u-S2", "sleep_analysis", SLEEP, T10, 2, 420, "min", AWU, "apple_watch_ultra"), {"end_utc": T10 + timedelta(hours=7), "text": "core"}),
              (("ch2:aa", "u-aa", "body_mass", MASS, T11, 2, 10, "kg", SCALE, "zepp_life_scale"), {}),
              (("ch2:zz", "u-zz", "body_mass", MASS, T11, 2, 10, "kg", SCALE, "zepp_life_scale"), {})]
    natives = [_bridge_sample("u-N1", T1, 100, end=T1 + timedelta(minutes=5)),
               _bridge_sample("u-N2a", T2, 5.5, hk=ENERGY, unit="kcal"), _bridge_sample("u-N2b", T2, 5.5, hk=ENERGY, unit="kcal"),
               _bridge_sample("u-N3", T3, 7.25, hk=ENERGY, unit="kcal"),
               _bridge_sample("u-N5", T5, NEAR, hk=MASS, unit="kg", source=SCALE),
               _bridge_sample("u-N6", T6, 40),
               _bridge_sample("u-N7a", T7, 60), _bridge_sample("u-N7b", T7, 60),
               _bridge_sample("u-m", T8, 80, hk=MASS, unit="kg", source=SCALE), _bridge_sample("u-z", T8, 70, hk=MASS, unit="kg", source=SCALE),
               _bridge_sample("u-Z1", T9, 3.0, hk=ENERGY, unit="kcal", source=ZEPP), _bridge_sample("u-Z2", T9, 3.0, hk=ENERGY, unit="kcal", source=ZEPP),
               _bridge_sample("u-mm", T11, 20, hk=MASS, unit="kg", source=SCALE)]
    if future_pair:      # the same content twice, both beyond the future ceiling: ineligible, so neither is a member
        tf = datetime(2027, 1, 1, 6, 0)
        natives += [_bridge_sample("u-Fa", tf, 9), _bridge_sample("u-Fb", tf, 9)]
    if shuffle:
        random.Random(shuffle).shuffle(legacy)
        random.Random(shuffle + 1).shuffle(natives)
    for a, k in legacy:
        _legacy(conn, *a, **k)
    _export(conn, "ch2:x-E6", "steps", STEPS, T6, 40, "count", AWU, "apple_watch_ultra")
    ingest_batch(conn, {"batch_id": "live-1", "samples": natives}, policy, reg)
    # The re-read confirms L1, L6, L7 and a with the content they already have; L3 and L5 are never re-delivered.
    ingest_batch(conn, {"batch_id": "rr-1", "samples": [_bridge_sample("u-L1", T1, 100, end=T1 + timedelta(minutes=5)),
                                                        _bridge_sample("u-L6", T6, 40), _bridge_sample("u-L7", T7, 60),
                                                        _bridge_sample("u-a", T8, 70, hk=MASS, unit="kg", source=SCALE),
                                                        _bridge_sample("u-L20", T20, 10), _bridge_sample("u-E21", T21, 2.0, hk=ENERGY, unit="kcal"),
                                                        _bridge_sample("u-L22", T22, 11), _bridge_sample("u-E23", T23, 2.5, hk=ENERGY, unit="kcal"),
                                                        _bridge_sample("u-aa", T11, 10, hk=MASS, unit="kg", source=SCALE)]}, policy, reg)
    compute_daily_values(conn, policy, reg, date(2026, 4, 1), TODAY, as_of=TODAY)
    d = date(2026, 4, 1)
    while d <= TODAY:
        compute_baselines(conn, policy, d)
        compute_signals(conn, policy, d)
        d += timedelta(days=1)
    conn.execute("CHECKPOINT")


def ah_file(tmp_path, name: str, copies: int | dict) -> pathlib.Path:
    """An apple-health look-alike (records table, Dubai wall times) holding
    `copies` copies (an int, or per record type) of every Apple steps and
    energy sample, plus the body-mass rows for the anchor's nearest-match
    check. The Zepp energy rows are deliberately absent (a source apple-health
    does not hold)."""
    p = tmp_path / name
    c = duckdb.connect(str(p))
    c.execute("CREATE TABLE records (record_hash VARCHAR, record_type VARCHAR, value DOUBLE, unit VARCHAR, source_name VARCHAR, "
              "source_version VARCHAR, device VARCHAR, creation_date TIMESTAMP, start_date TIMESTAMP, end_date TIMESTAMP, import_id VARCHAR)")
    rows = [(STEPS, 100.0, "count", AWU, T1, T1 + timedelta(minutes=5)), (STEPS, 40.0, "count", AWU, T6, T6), (STEPS, 60.0, "count", AWU, T7, T7),
            (ENERGY, 5.5, "kcal", AWU, T2, T2), (ENERGY, 7.25, "kcal", AWU, T3, T3),
            (STEPS, 10.0, "count", AWU, T20, T20), (ENERGY, 2.0, "kcal", AWU, T21, T21),
            (STEPS, 11.0, "count", AWU, T22, T22), (ENERGY, 2.5, "kcal", AWU, T23, T23)]
    i = 0
    for hk, v, u, s, st, en in rows:
        k = copies if isinstance(copies, int) else copies[hk]
        for _ in range(k):
            c.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, 'test')", [f"h{i}", hk, v, u, s, st + DUBAI, en + DUBAI])
            i += 1
    c.execute("INSERT INTO records VALUES ('hm', ?, 80.1, 'kg', ?, NULL, NULL, NULL, ?, ?, 'test')", [MASS, SCALE, T5 + DUBAI, T5 + DUBAI])
    c.execute("INSERT INTO records VALUES ('hm2', ?, 70.0, 'kg', ?, NULL, NULL, NULL, ?, ?, 'test')", [MASS, SCALE, T8 + DUBAI, T8 + DUBAI])
    c.close()
    return p


EXPECT = {
    "hk:u-L1": {"quality": None, "time_source": "bridge_reread_v1", "start_utc": T1, "value": 100.0},
    "hk:u-N1": {"quality": "hk_content_twin", "time_source": "bridge_utc", "start_utc": T1, "value": 100.0, "rebase_era": None},
    "hk:u-N2a": {"quality": None, "time_source": "bridge_utc"},
    "hk:u-N2b": {"quality": "hk_content_twin", "time_source": "bridge_utc", "value": 5.5},
    "hk:u-L3": {"quality": "hk_content_twin", "time_source": "era_rebase_v1", "rebase_era": 1, "start_utc": T3, "value": 7.25},
    "hk:u-N3": {"quality": None, "time_source": "bridge_utc"},
    "hk:u-L5": {"quality": None}, "hk:u-N5": {"quality": None, "value": NEAR},
    "hk:u-L6": {"quality": None, "time_source": "bridge_reread_v1"},
    "hk:u-N6": {"quality": "hk_content_twin", "time_source": "bridge_utc"},
    "ch2:x-E6": {"quality": "export_duplicate", "time_source": "export_linked_v1"},
    "hk:u-D1": {"quality": None}, "hk:u-D2": {"quality": None},
    "hk:u-L7": {"quality": None, "time_source": "bridge_reread_v1"},
    "hk:u-N7a": {"quality": "hk_content_twin"}, "hk:u-N7b": {"quality": "hk_content_twin"},
    # the `last` case: the highest confirmed id survives (what the same-instant tie rule picks), the legacy row is demoted
    "hk:u-a": {"quality": "hk_content_twin", "time_source": "bridge_reread_v1", "value": 70.0},
    "hk:u-z": {"quality": None, "value": 70.0}, "hk:u-m": {"quality": None, "value": 80.0},
    "hk:u-Z1": {"quality": None}, "hk:u-Z2": {"quality": "hk_content_twin"},
    # two unconfirmed legacy sleep stages: the lower id survives (sleep is not a `last` metric)
    "hk:u-S1": {"quality": None, "time_source": "era_rebase_v1"}, "hk:u-S2": {"quality": "hk_content_twin", "time_source": "era_rebase_v1"},
    # checkpoint B point 4: the winner (highest id, unconfirmed) survives over the confirmed lower id; the value stays 10
    "hk:u-zz": {"quality": None, "time_source": "era_rebase_v1", "value": 10.0}, "hk:u-aa": {"quality": "hk_content_twin", "time_source": "bridge_reread_v1"},
    "hk:u-mm": {"quality": None, "value": 20.0},
}
DEMOTED = {("hk:u-N1", "hk:u-L1"), ("hk:u-N2b", "hk:u-N2a"), ("hk:u-L3", "hk:u-N3"), ("hk:u-N6", "hk:u-L6"),
           ("hk:u-N7a", "hk:u-L7"), ("hk:u-N7b", "hk:u-L7"), ("hk:u-a", "hk:u-z"), ("hk:u-Z2", "hk:u-Z1"),
           ("hk:u-S2", "hk:u-S1"), ("hk:u-aa", "hk:u-zz")}
MIG = "migration:phase1b_history_rebase_v1"
# The key written out by hand (never ctw.key_exprs()): the "no eligible group remains" check in the test is independent of the code.
HAND_KEY = ("hk_type, metric, source_name, device_key, unit, unit_rule, text_value, value, writer_id, sync_identifier, sync_version, "
            "date_trunc('second', start_utc), date_trunc('second', end_utc)")


def _twins(c, event="demoted_v1"):
    return {(r["sample_id"], r["survivor_id"], r["source"]) for r in db.fetchdicts(c, "SELECT sample_id, survivor_id, source FROM content_twins WHERE event = ?", [event])}


def _digests(path) -> dict:
    """Whole-row digests of EVERY base table plus the catalog (tables, views, indexes), checkpoint B point 15."""
    c = duckdb.connect(str(path), read_only=True)
    try:
        out = {}
        for (t,) in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' AND table_type = 'BASE TABLE' ORDER BY 1").fetchall():
            cols = [r[0] for r in c.execute(f"DESCRIBE {t}").fetchall()]
            out[t] = c.execute(f"SELECT COUNT(*), CAST(bit_xor(hash({', '.join(cols)})) AS VARCHAR) FROM {t}").fetchone()
        out["_catalog"] = c.execute("SELECT list(table_name ORDER BY table_name) FROM information_schema.tables").fetchone()[0], \
            c.execute("SELECT list(index_name ORDER BY index_name) FROM duckdb_indexes()").fetchone()[0]
        return out
    finally:
        c.close()


def test_collapse_keeps_every_row_and_one_eligible_row_per_group(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    build(conn, policy, reg)
    before = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True, rebuild=True, apple_health=str(ah_file(tmp_path, "ah1.duckdb", copies=1)))
    assert R["ok"], (R["fails"], R["stopped"])
    f = R["facts"]
    assert f["rows_after"] == before                                       # nothing dropped: no uuid twins, no Whoop rows
    assert f["content_twin_totals"] == {"groups": 9, "rows_in_groups": 19, "losers": 10, "largest_group": 3, "groups_with_a_legacy_row": 7,
                                        "groups_native_only": 2, "exempt_groups": 1, "exempt_surplus_rows": 1, "near_twin_pairs": 1, "unit_split_pairs": 0}
    assert f["export_link_totals"] == {"linked": 1}
    assert f["compare_classes"] == {"equal": 9}
    assert sorted(f["content_twin_survivor_rule_outcomes"]) == [["legacy", "bridge_reread_v1", "native", "bridge_utc", 4],
                                                                 ["legacy", "era_rebase_v1", "legacy", "bridge_reread_v1", 1],
                                                                 ["legacy", "era_rebase_v1", "legacy", "era_rebase_v1", 1],
                                                                 ["native", "bridge_utc", "legacy", "bridge_reread_v1", 1],
                                                                 ["native", "bridge_utc", "legacy", "era_rebase_v1", 1],
                                                                 ["native", "bridge_utc", "native", "bridge_utc", 2]]
    assert f["content_twin_exempt_by_source"] == [[DIET, MFP, 1, 1]]
    assert f["content_twin_near_not_collapsed_by_type_source"] == [[MASS, SCALE, 1, 2]]
    assert f["content_twin_last_winners_demoted"] == 0 and R["checks"]["content_twin_never_demotes_a_last_winner"]["ok"]
    assert f["content_twin_update"]["updated"] == 10 and f["content_twin_update"]["digest_before"] == f["content_twin_update"]["digest_after"]
    assert R["checks"]["content_twin_membership_equals_an_independent_rederivation"]["detail"] == {"missing": 0, "extra": 0}
    led = f["conservation_ledger"]["eligible"]
    assert led["minus_content_twins"] == 10 and led["minus_export_linked"] == 1 and led["arithmetic"] == led["after"]
    assert f["eligibility_set"]["marked_by_class"] == {"hk_content_twin": 10}
    assert R["checks"]["ah_agreement_populations_add_up"]["ok"] and R["checks"]["every_metric_with_demoted_rows_is_tested_against_ah_or_excused"]["ok"]
    assert R["checks"]["every_linked_export_row_has_an_eligible_replacement_in_staging"]["ok"]
    # The export row's only rejected time-key candidate is the demoted twin (checkpoint C point 19).
    assert f["export_candidate_rejections"] == [["steps", "bridge_content_twin", "", 1]]
    # Owner check 4d.2: apple-health holds one copy, so every touched Apple cell agrees only AFTER the collapse;
    # the Zepp energy losers are accounted as a source apple-health does not hold.
    st = f["ah_agreement_by_metric"]["steps"]
    assert (st["cells"], st["agree_before"], st["agree_after"], st["cells_touched_by_collapse"], st["cells_lost_agreement"], st["cells_error_grew"]) == (4, 1, 4, 3, 0, 0)
    assert (st["losers"], st["losers_tested"], st["losers_source_absent_in_ah"], st["worse"], st["untested"]) == (4, 4, 0, False, False)
    ae = f["ah_agreement_by_metric"]["active_energy"]
    assert (ae["cells"], ae["agree_before"], ae["agree_after"], ae["losers"], ae["losers_tested"], ae["losers_source_absent_in_ah"], ae["worse"]) == (3, 1, 3, 3, 2, 1, False)
    be = f["ah_agreement_by_metric"]["basal_energy"]
    assert be["cells"] == 0 and be["losers"] == 0 and not be["worse"] and not be["untested"]
    assert f["ah_steps_days"] == {"days_either_side": 5, "equal": 5, "days_with_helios_steps": 5}
    gc = f["gate_classes"]
    assert "independent_oracle_matches_every_rebuilt_daily_value_both_ways" in gc["independent_evidence"]
    assert "content_twin_collapse_does_not_worsen_ah_agreement_for_steps_or_energy" in gc["independent_evidence"]
    assert "rows_after_equal_rows_before_minus_twins_minus_removed_whoop_rows" in gc["self_consistency"]
    assert set(gc["independent_evidence"]).isdisjoint(gc["self_consistency"]) and len(gc["independent_evidence"]) + len(gc["self_consistency"]) == len(R["checks"])
    aw = f["apply_window"]
    assert aw["budget_seconds"] == 3600 and aw["seconds_wall_clock"] >= 0 and aw["within_budget"] and aw["complete"]
    assert "rebuild" in aw["steps"] and "rebuild_daily_values" not in aw["steps"] and "cutover_transaction" not in aw["steps"]
    assert f["content_twins_after"] == {"demoted_rows": 10, "marked_rows": 10, "expected": 10, "archived": 10, "missing": 0, "extra": 0}
    c = db.connect(path)
    try:
        for sid, exp in EXPECT.items():
            row = _sample(c, sid)
            assert row is not None, sid
            for k, v in exp.items():
                assert row[k] == v, (sid, k, row[k], v)
        assert {(a, b) for a, b, _s in _twins(c)} == DEMOTED and {s for _a, _b, s in _twins(c)} == {MIG}
        assert db.fetchall(c, "SELECT COUNT(*) FROM sample_aliases WHERE reason = 'content_twin_v1'")[0][0] == 0
        assert ("ch2:x-E6", "hk:u-L6") in {(r["old_id"], r["new_id"]) for r in db.fetchdicts(c, "SELECT old_id, new_id FROM sample_aliases WHERE reason = 'export_link_v1'")}
        # No eligible content-twin group is left (the key written by hand); the exempt pair and the near pair are still eligible.
        groups = db.fetchall(c, f"SELECT metric FROM samples WHERE sync_path = 'bridge' AND quality IS NULL AND device_key <> 'excluded' "
                                f"GROUP BY {HAND_KEY} HAVING COUNT(*) > 1")
        assert groups == [("dietary_energy",)]
        assert c.execute("SELECT COUNT(*) FROM eligible_samples WHERE hk_uuid IN ('u-D1', 'u-D2', 'u-L5', 'u-N5')").fetchone()[0] == 4
        dv = {(str(d), m): v for d, m, v in db.fetchall(c, "SELECT date, metric, value FROM daily_values")}
        assert dv[("2026-05-05", "steps")] == 100 and dv[("2026-05-09", "steps")] == 40 and dv[("2026-05-11", "steps")] == 60
        assert dv[("2026-05-06", "active_energy")] == 5.5 and dv[("2026-05-07", "active_energy")] == 7.25 and dv[("2026-05-13", "active_energy")] == 3.0
        assert dv[("2026-05-10", "dietary_energy")] == 1000 and dv[("2026-05-08", "body_mass")] == 80.1
        assert dv[("2026-05-12", "body_mass")] == 70        # the tie rule's winner (hk:u-z) survived the collapse
        assert dv[("2026-05-16", "body_mass")] == 10        # and so did the unconfirmed highest id (checkpoint B point 4)
        assert dv[("2026-05-15", "sleep_duration")] == 7.0  # one stage of 420 min, not two
        assert dv[("2026-05-20", "steps")] == 10 and dv[("2026-05-21", "active_energy")] == 2.0
        mig = json.loads(db.fetchall(c, "SELECT summary FROM migrations")[0][0])
        assert mig["phase"] == "verified" and mig["content_twins"]["losers"] == 10 and len(mig["archive_manifest_sha256"]) == 64
        assert mig["anchor_ran"] is True and mig["checks_before_cutover"]["content_twin_collapse_does_not_worsen_ah_agreement_for_steps_or_energy"] is True
    finally:
        c.close()
    assert f["derived_diff_unexplained_cells"] == 0
    reasons = {r[1] for r in f["derived_diff_daily_values"]}
    assert "content_twin" in reasons and f["content_twin_only_cells"]["off_by"] == 0 and f["content_twin_only_cells"]["cells"] >= 5
    assert f["content_twin_only_cells"]["sleep_cells_left_to_the_oracle"] == 1
    assert f["oracle"]["mismatches"] == 0 and f["oracle"]["expected_cells"] > 0
    p1 = pathlib.Path(f["archive_places"][0])
    assert "lineage_content_twins.parquet" in (p1 / "MANIFEST.sha256").read_text()
    assert bk.archive_manifest_digest(p1) == mig["archive_manifest_sha256"]
    a = duckdb.connect()
    rows = a.execute(f"SELECT loser_id, survivor_id, value FROM read_parquet('{p1 / 'lineage_content_twins.parquet'}')").fetchall()
    assert {(x, y) for x, y, _v in rows} == DEMOTED and all(v is not None for _x, _y, v in rows)
    # The nightly export carries the membership table and binds the archive (adjudication-A-twins point 20).
    c = db.connect(path)
    try:
        m = bk.export_tables(c, tmp_path / "nightly")
    finally:
        c.close()
    assert m["tables"]["content_twins"]["rows"] == 10
    assert m["migrations"][rh.MIGRATION]["archive_manifest_sha256"] == mig["archive_manifest_sha256"]
    assert bk.verify_archive(p1, mig["archive_manifest_sha256"]) == []
    assert bk.verify_archive(p1, "0" * 64) and bk.restore_test(tmp_path / "nightly", p1)["ok"]


def test_shuffled_input_gives_identical_twin_output(tmp_path):
    outs = []
    for seed in (0, 7):
        path, conn, policy, reg = _env(tmp_path, f"s{seed}.duckdb")
        build(conn, policy, reg, shuffle=seed)
        conn.close()
        R = rh.Migration(path, policy, reg, tmp_path / f"out{seed}", today=TODAY, label="test", cutover=True,
                         exceptions=("budget:whoop", "budget:export")).run()
        assert R["ok"], R["fails"]
        c = duckdb.connect(str(path), read_only=True)
        cols = [r[0] for r in c.execute("DESCRIBE samples").fetchall()]
        rows = c.execute(f"SELECT {', '.join(x for x in cols if x not in ('ingested_at', 'batch_id'))} FROM samples ORDER BY sample_id").fetchall()
        rows += c.execute("SELECT old_id, new_id, reason FROM sample_aliases ORDER BY 1, 2").fetchall()
        rows += c.execute("SELECT sample_id, survivor_id, event, source FROM content_twins ORDER BY 1, 2, 3").fetchall()
        c.close()
        outs.append(rows)
    assert outs[0] == outs[1]


def test_apple_health_with_both_copies_stops_the_migration_and_writes_nothing(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    build(conn, policy, reg)
    conn.close()
    before = _digests(path)
    R = run(path, policy, reg, tmp_path, apple_health=str(ah_file(tmp_path, "ah2.duckdb", copies=2)))
    gate = "content_twin_collapse_does_not_worsen_ah_agreement_for_steps_or_energy"
    assert not R["ok"] and R["stopped"] == f"gate failed: {gate}" and gate in R["fails"]
    by = R["facts"]["ah_agreement_by_metric"]
    # 05-05 and 05-09 agreed before (two copies each side) and are lost; 05-11 held THREE Helios copies against two, so it
    # never agreed and its error neither grew nor shrank; the plain 05-20 cell disagrees both ways and is not touched.
    assert by["steps"]["agree_before"] == 2 and by["steps"]["agree_after"] == 0 and by["steps"]["cells_lost_agreement"] == 2 and by["steps"]["worse"]
    assert by["steps"]["cells"] == 4 and by["steps"]["cells_error_grew"] == 2 and by["steps"]["cells_touched_by_collapse"] == 3
    assert by["active_energy"]["cells_lost_agreement"] == 2 and by["active_energy"]["cells_error_grew"] == 2
    assert R["checks"][gate]["detail"]["worse"] == ["active_energy", "steps"]
    c = duckdb.connect(str(path), read_only=True)
    assert c.execute("SELECT COUNT(*) FROM duckdb_tables() WHERE table_name LIKE '%rebased%' OR table_name LIKE '_lineage%'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 0
    c.close()
    assert _digests(path) == before                                        # whole-table digests, not counts


def test_a_mixed_reference_fails_on_the_regressing_metric_only(tmp_path):
    """One copy for steps (agreement improves), two for energy (agreement is lost): the gate names energy alone."""
    path, conn, policy, reg = _env(tmp_path)
    build(conn, policy, reg)
    conn.close()
    R = run(path, policy, reg, tmp_path, apple_health=str(ah_file(tmp_path, "ah3.duckdb", copies={STEPS: 1, ENERGY: 2})))
    gate = "content_twin_collapse_does_not_worsen_ah_agreement_for_steps_or_energy"
    assert not R["ok"] and gate in R["fails"] and R["checks"][gate]["detail"]["worse"] == ["active_energy"]
    by = R["facts"]["ah_agreement_by_metric"]
    assert by["steps"]["cells_lost_agreement"] == 0 and by["steps"]["agree_after"] == 4 and by["active_energy"]["cells_lost_agreement"] == 2


def test_a_member_with_a_landing_variant_stops_the_run(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    build(conn, policy, reg)
    # u-N1 (a native member of case 1) re-delivered with another value: a native variant, uncertain content.
    r = ingest_batch(conn, {"batch_id": "var", "samples": [_bridge_sample("u-N1", T1, 101, end=T1 + timedelta(minutes=5))]}, policy, reg)
    assert r["guard_outcomes"].get("native_variant") == 1
    conn.close()
    R = run(path, policy, reg, tmp_path)
    assert not R["ok"] and R["stopped"] == "gate failed: content_twin_members_have_no_landing_variants"


def test_a_future_dated_pair_is_ineligible_and_left_alone(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    build(conn, policy, reg, future_pair=True)
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True, rebuild=True)
    assert R["ok"], (R["fails"], R["stopped"])
    assert R["facts"]["content_twin_totals"]["losers"] == 10                # the future pair is not a group
    c = db.connect(path)
    try:
        assert [r["quality"] for r in db.fetchdicts(c, "SELECT quality FROM samples WHERE hk_uuid IN ('u-Fa', 'u-Fb') ORDER BY hk_uuid")] == [None, None]
        assert db.fetchall(c, "SELECT COUNT(*) FROM content_twins WHERE sample_id IN ('hk:u-Fa', 'hk:u-Fb')")[0][0] == 0
    finally:
        c.close()


def test_deleting_a_survivor_promotes_a_confirmed_twin_and_deleting_the_twin_removes_the_sample(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    build(conn, policy, reg)
    conn.close()
    assert run(path, policy, reg, tmp_path, cutover=True, rebuild=True)["ok"]
    c = db.connect(path)
    try:
        r = ingest_batch(c, {"batch_id": "del-1", "samples": [], "deleted": ["u-L1"]}, policy, reg)
        assert r["deleted"] == 1 and r["promoted"] == 1 and r["guard_outcomes"] == {"twin_promoted": 1}
        assert _sample(c, "hk:u-L1") is None and _sample(c, "hk:u-N1")["quality"] is None
        assert json.loads(db.fetchall(c, "SELECT guard_outcomes FROM sync_log WHERE batch_id = 'del-1'")[0][0]) == {"twin_promoted": 1}
        assert _twins(c, "promoted_v1") == {("hk:u-N1", "hk:u-L1", "batch:del-1")}
        assert r["affected_dates"] == ["2026-05-05"]
        compute_daily_values(c, policy, reg, date(2026, 5, 5), date(2026, 5, 5), as_of=TODAY)
        assert db.fetchall(c, "SELECT value, n_samples FROM daily_values WHERE date = DATE '2026-05-05' AND metric = 'steps'") == [(100.0, 1)]
        # A lost-ack retry of the same batch: nothing to promote, the receipt is replaced, the membership row persists.
        r2 = ingest_batch(c, {"batch_id": "del-1", "samples": [], "deleted": ["u-L1"]}, policy, reg)
        assert r2["promoted"] == 0 and r2["guard_outcomes"] == {} and _twins(c, "promoted_v1") == {("hk:u-N1", "hk:u-L1", "batch:del-1")}
        # A replay of the deleted survivor is refused; the promoted row keeps counting.
        rr = ingest_batch(c, {"batch_id": "replay", "samples": [_bridge_sample("u-L1", T1, 100, end=T1 + timedelta(minutes=5))]}, policy, reg)
        assert rr["guard_outcomes"] == {"tombstoned": 1} and _sample(c, "hk:u-N1")["quality"] is None
        # Deleting a DEMOTED row promotes nothing and leaves the survivor alone.
        r3 = ingest_batch(c, {"batch_id": "del-2", "samples": [], "deleted": ["u-N2b"]}, policy, reg)
        assert r3["promoted"] == 0 and _sample(c, "hk:u-N2a")["quality"] is None and _sample(c, "hk:u-N2b") is None
        # The unconfirmed legacy member (case 3) is never promoted: deleting its survivor removes the sample.
        r4 = ingest_batch(c, {"batch_id": "del-3", "samples": [], "deleted": ["u-N3"]}, policy, reg)
        assert r4["promoted"] == 0 and _sample(c, "hk:u-L3")["quality"] == "hk_content_twin"
        compute_daily_values(c, policy, reg, date(2026, 5, 7), date(2026, 5, 7), as_of=TODAY)
        assert db.fetchall(c, "SELECT value FROM daily_values WHERE date = DATE '2026-05-07' AND metric = 'active_energy'") == []
        # The three-member group hands eligibility down twice: the lowest native id first, then the other.
        r5 = ingest_batch(c, {"batch_id": "del-4", "samples": [], "deleted": ["u-L7"]}, policy, reg)
        assert r5["promoted"] == 1 and _sample(c, "hk:u-N7a")["quality"] is None and _sample(c, "hk:u-N7b")["quality"] == "hk_content_twin"
        r6 = ingest_batch(c, {"batch_id": "del-5", "samples": [], "deleted": ["u-N7a"]}, policy, reg)
        assert r6["promoted"] == 1 and _sample(c, "hk:u-N7b")["quality"] is None
        assert _twins(c, "promoted_v1") == {("hk:u-N1", "hk:u-L1", "batch:del-1"), ("hk:u-N7a", "hk:u-L7", "batch:del-4"), ("hk:u-N7b", "hk:u-N7a", "batch:del-5")}
        compute_daily_values(c, policy, reg, date(2026, 5, 11), date(2026, 5, 11), as_of=TODAY)
        assert db.fetchall(c, "SELECT value, n_samples FROM daily_values WHERE date = DATE '2026-05-11' AND metric = 'steps'") == [(60.0, 1)]
        # The `last` case: deleting the survivor (hk:u-z) promotes the confirmed legacy row; the tie rule then picks hk:u-m (80).
        r7 = ingest_batch(c, {"batch_id": "del-6", "samples": [], "deleted": ["u-z"]}, policy, reg)
        assert r7["promoted"] == 1 and _sample(c, "hk:u-a")["quality"] is None
        compute_daily_values(c, policy, reg, date(2026, 5, 12), date(2026, 5, 12), as_of=TODAY)
        assert db.fetchall(c, "SELECT value FROM daily_values WHERE date = DATE '2026-05-12' AND metric = 'body_mass'") == [(80.0,)]
        # Deleting the promoted row removes the sample for good.
        r8 = ingest_batch(c, {"batch_id": "del-7", "samples": [], "deleted": ["u-N1"]}, policy, reg)
        assert r8["promoted"] == 0 and _sample(c, "hk:u-N1") is None
        compute_daily_values(c, policy, reg, date(2026, 5, 5), date(2026, 5, 5), as_of=TODAY)
        assert db.fetchall(c, "SELECT value FROM daily_values WHERE date = DATE '2026-05-05' AND metric = 'steps'") == []
        # Known limitation (adjudication-A-twins point 2, measured 0 in 30 days): a deletion and an identical new uuid in
        # one batch leave the promoted row and the new row both eligible until the Phase 4 reconciliation.
        r9 = ingest_batch(c, {"batch_id": "del-8", "samples": [_bridge_sample("u-N6c", T6, 40)], "deleted": ["u-L6"]}, policy, reg)
        assert r9["promoted"] == 1 and r9["accepted"] == 1
        assert _sample(c, "hk:u-N6")["quality"] is None and _sample(c, "hk:u-N6c")["quality"] is None
    finally:
        c.close()


def test_a_restore_that_replays_a_survivors_tombstone_promotes_the_twin(tmp_path):
    """Adjudication-A-twins point 1: reconcile_tombstones hands eligibility down like the live deletion path."""
    path, conn, policy, reg = _env(tmp_path)
    build(conn, policy, reg)
    conn.close()
    assert run(path, policy, reg, tmp_path, cutover=True, rebuild=True)["ok"]
    c = db.connect(path)
    try:
        # The tombstone a nightly export would replay after the live deletion of u-L1 (the row itself restored from the capture).
        db.execute(c, "INSERT INTO tombstones (tomb_id, hk_uuid, metric, start_utc, reason, batch_id) VALUES ('hk:u-L1', 'u-L1', 'steps', ?, 'bridge_deleted', 'later')", [T1])
        res = bk.reconcile_tombstones(c)
        assert res == {"deleted": 1, "dates_journaled": 1, "live_tombstoned_left": 0, "promoted": 1}
        assert _sample(c, "hk:u-L1") is None and _sample(c, "hk:u-N1")["quality"] is None
        assert _twins(c, "promoted_v1") == {("hk:u-N1", "hk:u-L1", "restore")}
        assert bk.reconcile_tombstones(c) == {"deleted": 0, "dates_journaled": 0, "live_tombstoned_left": 0, "promoted": 0}
    finally:
        c.close()


def test_survivor_order_prefers_confirmed_then_legacy_then_lowest_id_and_highest_id_for_last_metrics():
    """The shared rule (twins.py) as SQL, on a scratch table: delivery order never matters."""
    c = duckdb.connect()
    c.execute("CREATE TABLE t (sample_id VARCHAR, time_source VARCHAR, rebase_era INTEGER, is_last BOOLEAN)")
    rows = [("hk:z-native", "bridge_utc", None), ("hk:a-legacy-unconfirmed", "era_rebase_v1", 2),
            ("hk:m-legacy-confirmed", "bridge_reread_v1", 1), ("hk:b-native", "bridge_utc", None)]
    c.executemany("INSERT INTO t VALUES (?, ?, ?, FALSE)", rows)
    order = ctw.survivor_order_sql("s", "s.rebase_era IS NOT NULL", "s.is_last")
    assert [r[0] for r in c.execute(f"SELECT s.sample_id FROM t s ORDER BY {order}").fetchall()] == [
        "hk:m-legacy-confirmed", "hk:b-native", "hk:z-native", "hk:a-legacy-unconfirmed"]
    c.execute("UPDATE t SET is_last = TRUE")
    assert [r[0] for r in c.execute(f"SELECT s.sample_id FROM t s ORDER BY {order}").fetchall()] == [
        "hk:z-native", "hk:m-legacy-confirmed", "hk:b-native", "hk:a-legacy-unconfirmed"]
    # The comparison form agrees with the ORDER BY form on every ordered pair, for both metric kinds.
    for last in (False, True):
        c.execute("UPDATE t SET is_last = ?", [last])
        ordered = [r[0] for r in c.execute(f"SELECT s.sample_id FROM t s ORDER BY {order}").fetchall()]
        before = ctw.sorts_before_sql("a", "b", "a.rebase_era IS NOT NULL", "b.rebase_era IS NOT NULL", "a.is_last")
        pairs = {(x, y) for x, y in c.execute(f"SELECT a.sample_id, b.sample_id FROM t a, t b WHERE a.sample_id <> b.sample_id AND {before}").fetchall()}
        assert pairs == {(x, y) for i, x in enumerate(ordered) for y in ordered[i + 1:]}


def test_a_native_row_without_utc_instants_stops_the_run(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    build(conn, policy, reg)
    conn.execute("UPDATE samples SET start_utc = NULL WHERE hk_uuid = 'u-N2a'")
    conn.close()
    R = run(path, policy, reg, tmp_path)
    assert not R["ok"] and R["stopped"] == "gate failed: no_native_bridge_row_without_utc_instants"


def test_a_conflicting_membership_row_stops_the_run(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    build(conn, policy, reg)
    conn.execute("INSERT INTO content_twins (sample_id, survivor_id, event, source) VALUES ('hk:u-N1', 'hk:wrong', 'promoted_v1', 'batch:old')")
    conn.close()
    R = run(path, policy, reg, tmp_path)
    assert not R["ok"] and R["stopped"] == "gate failed: content_twins_holds_no_prior_row_for_a_demoted_sample"
    c = duckdb.connect(str(path))
    c.execute("DELETE FROM content_twins")
    c.execute("INSERT INTO content_twins (sample_id, survivor_id, event, source) VALUES ('hk:x', 'hk:y', 'demoted_v1', 'migration:phase1b_history_rebase_v1')")
    c.close()
    R2 = rh.Migration(path, policy, reg, tmp_path / "out2", today=TODAY, label="test", exceptions=("budget:whoop", "budget:export")).run()
    assert not R2["ok"] and R2["stopped"] == "gate failed: content_twins_holds_no_row_of_this_migration"


def test_a_late_landing_confirms_a_demoted_legacy_row_for_promotion(tmp_path):
    """Checkpoint B point 6: the unconfirmed legacy loser (case 3) is re-delivered identically after the
    migration (it lands in hk_reread as evidence, the row stays era_rebase_v1); deleting its survivor then
    promotes it, because the landing confirms its content."""
    path, conn, policy, reg = _env(tmp_path)
    build(conn, policy, reg)
    conn.close()
    assert run(path, policy, reg, tmp_path, cutover=True, rebuild=True)["ok"]
    c = db.connect(path)
    try:
        r = ingest_batch(c, {"batch_id": "late", "samples": [_bridge_sample("u-L3", T3, 7.25, hk=ENERGY, unit="kcal")]}, policy, reg)
        assert r["guard_outcomes"].get("landed_first") == 1 and _sample(c, "hk:u-L3")["time_source"] == "era_rebase_v1"
        r2 = ingest_batch(c, {"batch_id": "del", "samples": [], "deleted": ["u-N3"]}, policy, reg)
        assert r2["promoted"] == 1 and _sample(c, "hk:u-L3")["quality"] is None
        compute_daily_values(c, policy, reg, date(2026, 5, 7), date(2026, 5, 7), as_of=TODAY)
        assert db.fetchall(c, "SELECT value FROM daily_values WHERE date = DATE '2026-05-07' AND metric = 'active_energy'") == [(7.25,)]
    finally:
        c.close()


def test_a_landing_variant_blocks_promotion(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    build(conn, policy, reg)
    conn.close()
    assert run(path, policy, reg, tmp_path, cutover=True, rebuild=True)["ok"]
    c = db.connect(path)
    try:
        v = ingest_batch(c, {"batch_id": "var", "samples": [_bridge_sample("u-N1", T1, 101, end=T1 + timedelta(minutes=5))]}, policy, reg)
        assert v["guard_outcomes"].get("native_variant") == 1
        r = ingest_batch(c, {"batch_id": "del", "samples": [], "deleted": ["u-L1"]}, policy, reg)
        assert r["promoted"] == 0 and _sample(c, "hk:u-N1")["quality"] == "hk_content_twin"
    finally:
        c.close()


def test_verify_archive_checks_every_listed_file(tmp_path):
    d = tmp_path / "arch"
    d.mkdir()
    (d / "lineage_aliases.parquet").write_bytes(b"a")
    (d / "lineage_content_twins.parquet").write_bytes(b"b")
    import hashlib
    good = [f"{hashlib.sha256(b'a').hexdigest()}  lineage_aliases.parquet", f"{hashlib.sha256(b'b').hexdigest()}  lineage_content_twins.parquet"]
    (d / "MANIFEST.sha256").write_text("\n".join(good) + "\n")
    assert bk.verify_archive(d) == []
    (d / "lineage_content_twins.parquet").write_bytes(b"tampered")
    assert any("lineage_content_twins.parquet" in p for p in bk.verify_archive(d))
