"""Phase 1b item (c): the history migration (design.md sections 3, 5, 6, 10;
adjudication-A points 1, 7, 8, 9, 12, 16, 22, 23, 28, 30, 36, 37). Every
expected instant and value below is computed by hand from the fixture, never
read back from the code under test.

Fixture clock rules (era-calibration.md): a legacy row's stored wall is the
UTC instant in era 1, UTC+05:30 in era 2 and UTC+04:00 in era 4; export rows
follow era 2. Asia/Dubai is UTC+04:00 all year."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import duckdb
import pytest

from heliosd import backup as bk
from heliosd.ingest import whoop
from heliosd.ingest.bridge import ingest_batch
import pathlib

from heliosd.migrate import rebase_history as rh
from heliosd.signals.baselines import compute_baselines, compute_daily_values
from heliosd.signals.markers import compute_signals
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry
from tests.test_whoop_records import cycle_rec, recovery_rec

AWU, IPHONE, SCALE, ZEPP = "Owner’s Ultra 1", "Owner's 16 Pro Max", "Zepp Life", "Zepp"
STEPS, HR, MASS, FAT, TEMP = ("HKQuantityTypeIdentifierStepCount", "HKQuantityTypeIdentifierHeartRate",
                              "HKQuantityTypeIdentifierBodyMass", "HKQuantityTypeIdentifierBodyFatPercentage",
                              "HKQuantityTypeIdentifierBodyTemperature")
ERA_INGEST = {1: "2026-07-21 15:00:00", 2: "2026-08-01 10:00:00", 4: "2026-09-20 12:00:00"}
ERA_WALL_OFFSET = {1: 0, 2: 330, 4: 240}
EXPORT_INGEST = "2026-07-25 07:49:00"
TODAY = date(2026, 10, 5)
DUBAI = timedelta(minutes=240)


def _env(tmp_path, name="store.duckdb"):
    path = tmp_path / name
    conn = duckdb.connect(str(path))
    db.init_schema(conn)
    policy = MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    return path, conn, policy, SourceRegistry()


def _legacy(conn, sid, uuid, metric, hk, utc: datetime, era: int, value, unit, source, device,
            end_utc: datetime | None = None, text=None, path="bridge", ingested=None):
    """A legacy row exactly as the old daemon wrote it: the instant rendered in
    that era's wall clock, ch2 id, every Phase 1a column NULL."""
    off = timedelta(minutes=ERA_WALL_OFFSET[era])
    end_utc = end_utc or utc
    conn.execute("INSERT INTO samples (sample_id, hk_uuid, metric, hk_type, value, text_value, unit, start_ts, end_ts, source_name, "
                 "device_key, sync_path, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [sid, uuid, metric, hk, value, text, unit, utc + off, end_utc + off, source, device, path,
                  ingested or (EXPORT_INGEST if path == "health_export" else ERA_INGEST[era])])


def _export(conn, sid, metric, hk, utc, value, unit, source, device, end_utc=None, text=None):
    _legacy(conn, sid, None, metric, hk, utc, 2, value, unit, source, device, end_utc=end_utc, text=text, path="health_export")


def _bridge_sample(uuid, utc: datetime, value, hk=STEPS, unit="count", source=AWU, end: datetime | None = None):
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    return {"hk_type": hk, "value": value, "unit": unit, "start": iso(utc), "end": iso(end or utc), "source_name": source, "uuid": uuid}


def _sample(conn, sid):
    rows = db.fetchdicts(conn, "SELECT * FROM samples WHERE sample_id = ?", [sid])
    return rows[0] if rows else None


def build_fixture(conn, policy, reg, shuffle: int = 0):
    """The synthetic store. Returns the oracle: expected rows after the migration."""
    expect: dict[str, dict] = {}
    inserts = []

    def leg(*a, **k):
        inserts.append(("legacy", a, k))

    # Era 1 steps row at 20:30Z: Dubai day moves to the next calendar day.
    t1 = datetime(2026, 6, 1, 20, 30)
    leg("ch2:a1", "u-steps-1", "steps", STEPS, t1, 1, 120, "count", AWU, "apple_watch_ultra", end_utc=t1 + timedelta(minutes=5))
    # Its era-2 twin (330 minutes later on the wall): collapses onto the same instant.
    leg("ch2:a2", "u-steps-1", "steps", STEPS, t1, 2, 120, "count", AWU, "apple_watch_ultra", end_utc=t1 + timedelta(minutes=5))
    expect["hk:u-steps-1"] = {"start_utc": t1, "end_utc": t1 + timedelta(minutes=5), "start_ts": t1 + DUBAI, "rebase_era": 1,
                              "time_source": "era_rebase_v1", "value": 120.0, "quality": None}
    # Era 4 row (already Dubai wall) and its era-2 twin 90 minutes apart.
    t2 = datetime(2026, 9, 18, 5, 0)
    leg("ch2:b2", "u-steps-2", "steps", STEPS, t2, 2, 80, "count", AWU, "apple_watch_ultra")
    leg("ch2:b4", "u-steps-2", "steps", STEPS, t2, 4, 80, "count", AWU, "apple_watch_ultra")
    expect["hk:u-steps-2"] = {"start_utc": t2, "start_ts": t2 + DUBAI, "rebase_era": 2, "time_source": "era_rebase_v1", "value": 80.0}
    # Singletons in each era, resting HR and body mass (last metrics), same Dubai day.
    for era, hour, uuid, v in ((1, 3, "u-rhr-1", 55), (2, 4, "u-rhr-2", 56), (4, 5, "u-rhr-4", 57)):
        t = datetime(2026, 6, 10, hour, 0)
        leg(f"ch2:r{era}", uuid, "resting_hr", "HKQuantityTypeIdentifierRestingHeartRate", t, era, v, "count/min", AWU, "apple_watch_ultra")
        expect[f"hk:{uuid}"] = {"start_utc": t, "start_ts": t + DUBAI, "rebase_era": era, "value": float(v)}
    tm = datetime(2026, 6, 10, 2, 0)
    leg("ch2:m1", "u-mass-1", "body_mass", MASS, tm, 1, 80.5, "kg", SCALE, "zepp_life_scale")
    leg("ch2:m2", "u-mass-2", "body_mass", MASS, tm + timedelta(hours=1), 2, 80.1, "kg", SCALE, "zepp_life_scale")
    expect["hk:u-mass-2"] = {"start_utc": tm + timedelta(hours=1), "value": 80.1}
    # Body fat stored as a fraction (legacy); the re-read delivers the fraction again
    # and normalization makes it percent: an explained difference, the re-read wins.
    tf = datetime(2026, 6, 10, 2, 30)
    leg("ch2:f1", "u-fat-1", "body_fat_pct", FAT, tf, 2, 0.2534, "%", SCALE, "zepp_life_scale")
    expect["hk:u-fat-1"] = {"start_utc": tf, "value": 25.34, "unit_rule": "frac_to_pct_v1", "time_source": "bridge_reread_v1"}
    # Heart rate from the arm strap, era 2, with a matching export row (exact value)
    th = datetime(2026, 5, 20, 10, 0)
    leg("ch2:h1", "u-hr-1", "heart_rate", HR, th, 2, 72, "count/min", ZEPP, "zepp_helio")
    inserts.append(("export", ("ch2:x-hr-1", "heart_rate", HR, th, 72, "count/min", ZEPP, "zepp_helio"), {}))
    expect["hk:u-hr-1"] = {"start_utc": th, "value": 72.0, "quality": None}
    expect["ch2:x-hr-1"] = {"start_utc": th, "start_ts": th + DUBAI, "quality": "export_duplicate", "time_source": "export_linked_v1", "rebase_era": 2}
    # Body temperature: export rounds to 2 decimals, inside the 0.01 degC tolerance.
    tt = datetime(2026, 5, 21, 1, 0)
    leg("ch2:t1", "u-temp-1", "body_temp", TEMP, tt, 2, 36.70123, "degC", "TestCGM", "test_cgm")
    inserts.append(("export", ("ch2:x-temp-1", "body_temp", TEMP, tt, 36.70, "degC", "TestCGM", "test_cgm"), {}))
    expect["ch2:x-temp-1"] = {"quality": "export_duplicate", "time_source": "export_linked_v1"}
    # Ambiguous: two Bridge rows with the same key, one export row (and the reverse).
    ta = datetime(2026, 5, 22, 10, 0)
    leg("ch2:s1", "u-amb-1", "steps", STEPS, ta, 2, 10, "count", AWU, "apple_watch_ultra")
    leg("ch2:s2", "u-amb-2", "steps", STEPS, ta, 2, 10, "count", AWU, "apple_watch_ultra")
    inserts.append(("export", ("ch2:x-amb", "steps", STEPS, ta, 10, "count", AWU, "apple_watch_ultra"), {}))
    expect["ch2:x-amb"] = {"quality": "export_ambiguous", "time_source": "era_rebase_v1"}
    tb = datetime(2026, 5, 23, 10, 0)
    leg("ch2:s3", "u-rev-1", "steps", STEPS, tb, 2, 11, "count", AWU, "apple_watch_ultra")
    inserts.append(("export", ("ch2:x-rev-a", "steps", STEPS, tb, 11, "count", AWU, "apple_watch_ultra"), {}))
    inserts.append(("export", ("ch2:x-rev-b", "steps", STEPS, tb, 11, "count", AWU, "apple_watch_ultra"), {}))
    expect["ch2:x-rev-a"] = {"quality": "export_ambiguous"}
    expect["ch2:x-rev-b"] = {"quality": "export_ambiguous"}
    # Unmatched export row: stays eligible (owner decision 4c.1), rebased by the era-2 rule.
    tu = datetime(2026, 4, 1, 20, 30)
    inserts.append(("export", ("ch2:x-only", "steps", STEPS, tu, 500, "count", IPHONE, "iphone"), {}))
    expect["ch2:x-only"] = {"start_utc": tu, "start_ts": tu + DUBAI, "quality": None, "time_source": "era_rebase_v1", "rebase_era": 2}
    if shuffle:
        import random
        random.Random(shuffle).shuffle(inserts)
    for kind, a, k in inserts:
        (_legacy if kind == "legacy" else _export)(conn, *a, **k)
    # Whoop: a SCORED recovery record for 2026-07-10 (created 04:00Z = 08:00 Dubai) with
    # its samples, an UNSCORABLE cycle for 07-11, nothing for 07-12.
    with db.transaction(conn) as c:
        whoop.apply_record(c, "recovery", recovery_rec(900, 1900, "2026-07-10T04:00:00.000Z", score=66, hrv=45.2),
                           policy, datetime(2026, 10, 1), "pull-1")
        whoop.apply_record(c, "cycle", cycle_rec(701, "2026-07-11T00:00:00.000Z", "2026-07-11T23:00:00.000Z", state="UNSCORABLE"),
                           policy, datetime(2026, 10, 1), "pull-1")
    for sid, metric, utc, era, v in (("wh:recovery_score:2026-07-10", "recovery_score", datetime(2026, 7, 10, 4, 0), 1, 60.0),
                                     ("wh:strain:2026-07-11", "strain", datetime(2026, 7, 11, 0, 0), 2, 9.0),
                                     ("wh:sleep_need:2026-07-12", "sleep_need", datetime(2026, 7, 11, 20, 0), 2, 7.9)):
        conn.execute("INSERT INTO samples (sample_id, metric, value, unit, start_ts, end_ts, source_name, device_key, sync_path, ingested_at) "
                     "VALUES (?, ?, ?, 'x', ?, ?, 'WHOOP', 'whoop', 'whoop_live', ?)",
                     [sid, metric, v, utc + timedelta(minutes=ERA_WALL_OFFSET[era]), utc + timedelta(minutes=ERA_WALL_OFFSET[era]), ERA_INGEST[era]])
    expect["wh:recovery_score:2026-07-10"] = None                       # replaced by the record row
    expect["wh:strain:2026-07-11"] = None                               # superseded (tombstone)
    expect["wh:sleep_need:2026-07-12"] = {"quality": "legacy_whoop_unresolved", "time_source": None}
    # Post-1a rows (byte-identical afterwards), a deletion (tombstone) and the re-read.
    ingest_batch(conn, {"batch_id": "live-1", "samples": [_bridge_sample("n-1", datetime(2026, 10, 5, 4, 0), 300),
                                                          _bridge_sample("n-del", datetime(2026, 10, 5, 4, 10), 1)]}, policy, reg)
    ingest_batch(conn, {"batch_id": "live-2", "samples": [], "deleted": ["n-del"]}, policy, reg)
    # The re-read: u-steps-1 confirmed at its true instant, u-rhr-2 confirmed, u-fat-1 as a
    # fraction (explained), a landing for a uuid that is then deleted (ignored at apply).
    ingest_batch(conn, {"batch_id": "rr-1", "samples": [
        _bridge_sample("u-steps-1", t1, 120, end=t1 + timedelta(minutes=5)),
        _bridge_sample("u-rhr-2", datetime(2026, 6, 10, 4, 0), 56, hk="HKQuantityTypeIdentifierRestingHeartRate", unit="count/min"),
        _bridge_sample("u-fat-1", tf, 0.2534, hk=FAT, unit="%", source=SCALE),
        _bridge_sample("u-mass-1", tm, 80.5, hk=MASS, unit="kg", source=SCALE)]}, policy, reg)
    ingest_batch(conn, {"batch_id": "live-3", "samples": [], "deleted": ["u-mass-1"]}, policy, reg)   # land, then delete
    expect["hk:u-mass-1"] = None
    expect["hk:u-steps-1"]["time_source"] = "bridge_reread_v1"
    expect["hk:u-rhr-2"] = {**expect["hk:u-rhr-2"], "time_source": "bridge_reread_v1"}
    expect["hk:n-1"] = {"start_utc": datetime(2026, 10, 5, 4, 0), "time_source": "bridge_utc", "value": 300.0, "rebase_era": None}
    # The derived state the live store has before a migration: complete daily
    # values, baselines and signals (the step-6 diff compares against it).
    compute_daily_values(conn, policy, reg, date(2026, 4, 1), TODAY, as_of=TODAY)
    d = date(2026, 4, 1)
    while d <= TODAY:
        compute_baselines(conn, policy, d)
        compute_signals(conn, policy, d)
        d += timedelta(days=1)
    conn.execute("CHECKPOINT")
    return expect


def run(path, policy, reg, tmp_path, **kw):
    kw.setdefault("exceptions", ("budget:whoop", "budget:export"))      # the fixture breaches both budgets on purpose
    m = rh.Migration(path, policy, reg, tmp_path / "out", archive_dirs=[tmp_path / "arch1", tmp_path / "arch2"],
                     today=TODAY, label="test", **kw)
    return m.run()


EXPECTED_IDS_AFTER = {"hk:u-steps-1", "hk:u-steps-2", "hk:u-rhr-1", "hk:u-rhr-2", "hk:u-rhr-4", "hk:u-mass-2", "hk:u-fat-1", "hk:u-hr-1",
                      "hk:u-temp-1", "hk:u-amb-1", "hk:u-amb-2", "hk:u-rev-1",
                      "ch2:x-hr-1", "ch2:x-temp-1", "ch2:x-amb", "ch2:x-rev-a", "ch2:x-rev-b", "ch2:x-only",
                      "wh:sleep_need:2026-07-12", "wh:recovery_score:recovery:900", "wh:hrv_rmssd:recovery:900", "hk:n-1"}


def test_full_migration_matches_the_hand_oracle(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    expect = build_fixture(conn, policy, reg)
    before = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True, rebuild=True)
    assert R["ok"], (R["fails"], R["stopped"])
    f = R["facts"]
    assert f["twin_uuids"] == 2 and f["compare_classes"] == {"equal": 2, "explained_frac_to_pct": 1}
    assert f["reread_rows_tombstoned_ignored"] == 1
    assert f["export_link_totals"] == {"linked": 2, "ambiguous": 3, "unmatched": 1}
    assert sorted(f["whoop_day_rows_by_outcome"]) == [["recovery_score", "replaced", 1], ["sleep_need", "quarantined", 1], ["strain", "superseded", 1]]
    assert f["rows_after"] == before - 2 - 2
    assert f["whoop_day_rows_by_outcome_note"] == [["recovery_score", "replaced", "", 1], ["sleep_need", "quarantined", "no_record", 1], ["strain", "superseded", "", 1]]
    c = db.connect(path)
    try:
        # The complete identity set, not selected rows (checkpoint B point 26).
        assert {r[0] for r in db.fetchall(c, "SELECT sample_id FROM samples")} == EXPECTED_IDS_AFTER
        for sid, exp in expect.items():
            row = _sample(c, sid)
            if exp is None:
                assert row is None, sid
                continue
            assert row is not None, sid
            for k, v in exp.items():
                assert row[k] == v, (sid, k, row[k], v)
        # Lineage: aliases for both twin ids, the export link, the Whoop replacement; the tombstone for the superseded row.
        al = {(r["old_id"], r["new_id"], r["reason"]) for r in db.fetchdicts(c, "SELECT old_id, new_id, reason FROM sample_aliases")}
        assert ("ch2:a1", "hk:u-steps-1", "history_rebase_v1") in al and ("ch2:a2", "hk:u-steps-1", "twin_collapse_v1") in al
        assert ("ch2:x-hr-1", "hk:u-hr-1", "export_link_v1") in al
        assert ("wh:recovery_score:2026-07-10", "wh:recovery_score:recovery:900", whoop.ALIAS_REASON) in al
        assert db.fetchall(c, "SELECT reason, batch_id FROM tombstones WHERE tomb_id = 'wh:strain:2026-07-11'") == [("legacy_superseded", "migration:cycle:701")]
        # Consumed landing rows left hk_reread; the tombstoned one did not have a sample and stays.
        assert sorted(r[0] for r in db.fetchall(c, "SELECT hk_uuid FROM hk_reread")) == ["u-mass-1"]
        assert db.migration_applied(c, rh.MIGRATION)
        mig = json.loads(db.fetchall(c, "SELECT summary FROM migrations")[0][0])
        assert mig["input_fingerprint"] == f["input_fingerprint"] and mig["zone"] == "Asia/Dubai"
        # Only the uuid index survives the swap; the view is back; every migrated row renders Dubai = UTC + 4 h.
        assert [r[0] for r in db.fetchall(c, "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'samples' ORDER BY 1")] == ["idx_samples_hk_uuid"]
        assert db.fetchall(c, "SELECT COUNT(*) FROM samples WHERE rebase_era IS NOT NULL AND start_ts <> start_utc + INTERVAL 240 MINUTE")[0][0] == 0
        # Daily values rebuilt: resting HR 2026-06-10 is the LAST by instant (era 4 row, 57); steps on
        # 2026-06-02 (Dubai) hold the collapsed era-1 row once (120), the unmatched export row counts.
        dv = {(str(d), m): v for d, m, v in db.fetchall(c, "SELECT date, metric, value FROM daily_values")}
        assert dv[("2026-06-10", "resting_hr")] == 57 and dv[("2026-06-02", "steps")] == 120
        assert dv[("2026-04-02", "steps")] == 500 and dv[("2026-06-10", "body_fat_pct")] == 25.34
        assert dv[("2026-06-10", "body_mass")] == 80.1
    finally:
        c.close()
    o = R["facts"]["oracle"]
    assert o["mismatches"] == 0 and o["expected_cells"] > 0 and o["expected_cells"] == o["actual_cells"] == o["compared"]
    assert R["facts"]["derived_diff_unexplained_cells"] == 0
    assert R["facts"]["migration_phase"] == "verified"
    assert json.loads(db.fetchall(db.connect(path), "SELECT summary FROM migrations")[0][0])["phase"] == "verified"
    # The archive exists in both places (fingerprint-qualified folders) with identical manifests and holds every alias,
    # plus the final relation with its origin.
    p1, p2 = (pathlib.Path(x) for x in f["archive_places"])
    assert p1.parent == tmp_path / "arch1" and p1.name.startswith("phase1b_history_rebase_v1-")
    m1 = (p1 / "MANIFEST.sha256").read_text()
    assert m1 == (p2 / "MANIFEST.sha256").read_text() and "lineage_aliases.parquet" in m1 and "lineage_aliases_final.parquet" in m1
    a = duckdb.connect()
    n_arch = a.execute(f"SELECT COUNT(*) FROM read_parquet('{p1 / 'lineage_aliases.parquet'}')").fetchone()[0]
    assert n_arch == sum(f["aliases_staged"].values())
    assert a.execute(f"SELECT COUNT(*) FROM read_parquet('{p1 / 'lineage_aliases_final.parquet'}') WHERE origin = 'existing'").fetchone()[0] == 0
    assert a.execute(f"SELECT COUNT(*) FROM read_parquet('{p1 / 'lineage_aliases_final.parquet'}') WHERE origin = 'staged'").fetchone()[0] == sum(f["aliases_staged"].values())


def test_shuffled_input_gives_identical_output(tmp_path):
    outs = []
    for seed in (0, 5):
        path, conn, policy, reg = _env(tmp_path, f"s{seed}.duckdb")
        build_fixture(conn, policy, reg, shuffle=seed)
        conn.close()
        R = rh.Migration(path, policy, reg, tmp_path / f"out{seed}", today=TODAY, label="test", cutover=True,
                         exceptions=("budget:whoop", "budget:export")).run()
        assert R["ok"], R["fails"]
        c = duckdb.connect(str(path), read_only=True)
        cols = [r[0] for r in c.execute("DESCRIBE samples").fetchall()]
        rows = c.execute(f"SELECT {', '.join(x for x in cols if x != 'ingested_at')} FROM samples ORDER BY sample_id").fetchall()
        c.close()
        outs.append(rows)
    assert outs[0] == outs[1]


def test_dry_run_without_cutover_leaves_the_file_identical(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    build_fixture(conn, policy, reg)
    conn.close()
    R = run(path, policy, reg, tmp_path)
    assert R["ok"] and not R["flags"]["cutover"]
    c = duckdb.connect(str(path), read_only=True)
    assert c.execute("SELECT COUNT(*) FROM duckdb_tables() WHERE table_name LIKE '%rebased%' OR table_name LIKE '_lineage%'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM samples WHERE time_source IS NULL").fetchone()[0] == R["facts"]["samples_before"] - R["facts"]["native_rows"]
    assert c.execute("SELECT COUNT(*) FROM hk_reread").fetchone()[0] == R["facts"]["hk_reread_rows"]
    c.close()
    assert (pathlib.Path(R["facts"]["archive_places"][0]) / "lineage_compare.parquet").exists()


def test_second_run_is_refused_and_changes_nothing(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    build_fixture(conn, policy, reg)
    conn.close()
    assert run(path, policy, reg, tmp_path, cutover=True)["ok"]
    c = duckdb.connect(str(path), read_only=True)
    cols = [r[0] for r in c.execute("DESCRIBE samples").fetchall()]
    digest = c.execute(f"SELECT COUNT(*), CAST(bit_xor(hash({', '.join(cols)})) AS VARCHAR) FROM samples").fetchone()
    c.close()
    R = rh.Migration(path, policy, reg, tmp_path / "out2", today=TODAY, label="test", cutover=True).run()
    assert not R["ok"] and R["stopped"] and "migration_not_applied_yet" in R["fails"]
    c = duckdb.connect(str(path), read_only=True)
    assert c.execute(f"SELECT COUNT(*), CAST(bit_xor(hash({', '.join(cols)})) AS VARCHAR) FROM samples").fetchone() == digest
    assert c.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 1
    c.close()


def test_startup_twice_after_the_persisted_swap(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    build_fixture(conn, policy, reg)
    conn.close()
    # The daemon starts only on a VERIFIED migration (checkpoint C point 29): cutover plus rebuild.
    R = run(path, policy, reg, tmp_path, cutover=True, rebuild=True)
    assert R["ok"] and R["checks"]["startup_1_sees_the_rebuilt_table"]["ok"] and R["checks"]["startup_2_sees_the_rebuilt_table"]["ok"]
    assert R["facts"]["migration_phase"] == "verified"
    for i in range(2):
        c = db.connect(path)
        assert c.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == R["facts"]["rows_after"] + i
        assert c.execute("SELECT COUNT(*) FROM duckdb_views() WHERE view_name = 'eligible_samples'").fetchone()[0] == 1
        # The new code keeps working on the migrated store: an ingest, a deletion, a landing.
        r = ingest_batch(c, {"batch_id": f"after{i}", "samples": [_bridge_sample(f"post{i}", datetime(2026, 10, 6, 4, i), 5),
                                                                _bridge_sample("u-steps-1", datetime(2026, 6, 1, 20, 30), 120, end=datetime(2026, 6, 1, 20, 35))]}, policy, reg)
        assert r["accepted"] == 1 and r["guard_outcomes"].get("native_identical") == 1
        if i == 0:
            r2 = ingest_batch(c, {"batch_id": "after-dup", "samples": [_bridge_sample("post0", datetime(2026, 10, 6, 4, 0), 5)]}, policy, reg)
            assert r2["guard_outcomes"].get("native_identical") == 1
            # A real deletion on the migrated store: the row goes, the tombstone stays, a replay is refused.
            rd = ingest_batch(c, {"batch_id": "after-del", "samples": [], "deleted": ["post0"]}, policy, reg)
            assert rd["deleted"] == 1 and c.execute("SELECT COUNT(*) FROM samples WHERE hk_uuid = 'post0'").fetchone()[0] == 0
            assert c.execute("SELECT reason FROM tombstones WHERE hk_uuid = 'post0'").fetchone()[0] == "bridge_deleted"
            rr = ingest_batch(c, {"batch_id": "after-replay", "samples": [_bridge_sample("post0", datetime(2026, 10, 6, 4, 0), 5)]}, policy, reg)
            assert rr["guard_outcomes"] == {"tombstoned": 1}
            # The next loop sees one row more than rows_after: post0 was deleted, so re-add it under another id.
            ingest_batch(c, {"batch_id": "after-fill", "samples": [_bridge_sample("post0b", datetime(2026, 10, 6, 4, 30), 5)]}, policy, reg)
        c.close()


def _base(tmp_path, name):
    path, conn, policy, reg = _env(tmp_path, name)
    t = datetime(2026, 6, 1, 10, 0)
    _legacy(conn, "ch2:ok1", "ok-1", "steps", STEPS, t, 1, 5, "count", AWU, "apple_watch_ultra")
    return path, conn, policy, reg, t


@pytest.mark.parametrize("ingested, era", [
    ("2026-07-21 20:41:59.999999", 1), ("2026-07-22 04:41:00", 2), ("2026-09-11 09:47:59.999999", 2), ("2026-09-17 11:26:00", 4)])
def test_era_boundaries_are_half_open_at_full_precision(tmp_path, ingested, era):
    path, conn, policy, reg, _t = _base(tmp_path, "b.duckdb")
    t = datetime(2026, 6, 5, 12, 0)
    _legacy(conn, "ch2:edge", "edge", "steps", STEPS, t, era, 7, "count", AWU, "apple_watch_ultra", ingested=ingested)
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True)
    assert R["ok"], R["fails"]
    c = duckdb.connect(str(path), read_only=True)
    assert c.execute("SELECT rebase_era, start_utc FROM samples WHERE hk_uuid = 'edge'").fetchone() == (era, t)
    c.close()


@pytest.mark.parametrize("ingested", ["2026-07-21 20:42:00", "2026-07-22 04:40:59.999999", "2026-09-11 09:48:00", "2026-09-17 11:25:59.999999"])
def test_a_bridge_row_in_a_gap_stops_the_migration(tmp_path, ingested):
    path, conn, policy, reg, t = _base(tmp_path, "g.duckdb")
    _legacy(conn, "ch2:gap", "gap", "steps", STEPS, t, 1, 7, "count", AWU, "apple_watch_ultra", ingested=ingested)
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True)
    assert not R["ok"] and "no_bridge_row_in_an_era_gap" in R["fails"]
    c = duckdb.connect(str(path), read_only=True)
    assert c.execute("SELECT COUNT(*) FROM samples WHERE time_source IS NULL").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 0
    c.close()


def test_same_era_twins_are_a_conflict_that_stops(tmp_path):
    """Two rows of one uuid with the same offset rebase to different instants: a conflict, never a collapse."""
    path, conn, policy, reg, t = _base(tmp_path, "c.duckdb")
    _legacy(conn, "ch2:c1", "conf", "steps", STEPS, t, 2, 5, "count", AWU, "apple_watch_ultra")
    _legacy(conn, "ch2:c2", "conf", "steps", STEPS, t + timedelta(minutes=330), 2, 5, "count", AWU, "apple_watch_ultra")
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True)
    assert not R["ok"] and "twin_collapse_100_percent_by_instant" in R["fails"]
    assert R["facts"]["twin_conflicts_by_type"][0][:2] == [STEPS, "apple_watch_ultra"]


def test_twins_with_different_content_stop(tmp_path):
    path, conn, policy, reg, t = _base(tmp_path, "d.duckdb")
    _legacy(conn, "ch2:v1", "val", "steps", STEPS, t, 1, 5, "count", AWU, "apple_watch_ultra")
    _legacy(conn, "ch2:v2", "val", "steps", STEPS, t, 2, 6, "count", AWU, "apple_watch_ultra")
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True)
    assert not R["ok"] and "twin_content_equal_null_safe_every_column" in R["fails"]
    assert R["checks"]["twin_content_equal_null_safe_every_column"]["detail"] == {"d_value": 1}


def test_three_rows_for_one_uuid_and_a_legacy_native_collision_stop(tmp_path):
    path, conn, policy, reg, t = _base(tmp_path, "e.duckdb")
    for i, era in enumerate((1, 2, 4)):
        _legacy(conn, f"ch2:tr{i}", "triple", "steps", STEPS, t, era, 5, "count", AWU, "apple_watch_ultra")
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True)
    assert not R["ok"] and "no_uuid_with_more_than_two_rows" in R["fails"]
    path2, conn2, policy, reg, t = _base(tmp_path, "e2.duckdb")
    ingest_batch(conn2, {"batch_id": "n", "samples": [_bridge_sample("nat-1", datetime(2026, 6, 1, 11, 0), 5)]}, policy, reg)
    conn2.execute("UPDATE samples SET hk_uuid = 'ok-1' WHERE sample_id = 'hk:nat-1'")   # force the collision the guard prevents
    conn2.close()
    R = rh.Migration(path2, policy, reg, tmp_path / "o2", today=TODAY, label="test", cutover=True).run()
    assert not R["ok"] and "no_legacy_plus_native_collision_on_one_uuid" in R["fails"]


@pytest.mark.parametrize("field, kw, cls", [
    ("end", {"end": datetime(2026, 6, 1, 10, 7)}, "instant_differs"),
    ("start", {"start_shift": 30}, "instant_differs"),
    ("device", {"source": "Owner's 16 Pro Max"}, "identity_differs"),
    ("value", {"value": 6}, "content_differs")])
def test_reread_differences_are_classified_and_stop(tmp_path, field, kw, cls):
    path, conn, policy, reg, t = _base(tmp_path, f"r-{field}.duckdb")
    s = _bridge_sample("ok-1", t + timedelta(minutes=kw.pop("start_shift", 0)), kw.pop("value", 5), **kw)
    ingest_batch(conn, {"batch_id": "rr", "samples": [s]}, policy, reg)
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True)
    assert not R["ok"] and R["facts"]["compare_classes"] == {cls: 1}
    if cls == "instant_differs" and field == "start":
        assert R["facts"]["compare_delta_histogram"][0][2] == 30      # the delta itself is reported, not only a count
    c = duckdb.connect(str(path), read_only=True)
    assert c.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 0
    c.close()


def test_accepting_mismatches_makes_the_reread_win_and_records_the_flag(tmp_path):
    path, conn, policy, reg, t = _base(tmp_path, "acc.duckdb")
    ingest_batch(conn, {"batch_id": "rr", "samples": [_bridge_sample("ok-1", t + timedelta(minutes=30), 5)]}, policy, reg)
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True, accept_reread_mismatches=True)
    assert R["ok"] and R["flags"]["accepted_mismatches"] == 1
    c = db.connect(path, allow_unverified=True)       # cutover without rebuild: inspection through the tool's flag
    row = _sample(c, "hk:ok-1")
    assert row["start_utc"] == t + timedelta(minutes=30) and row["time_source"] == "bridge_reread_v1"
    assert json.loads(db.fetchall(c, "SELECT summary FROM migrations")[0][0])["flags"]["accepted_mismatches"] == 1
    c.close()


def test_ambiguous_landing_variants_always_stop(tmp_path):
    path, conn, policy, reg, t = _base(tmp_path, "amb.duckdb")
    ingest_batch(conn, {"batch_id": "rr1", "samples": [_bridge_sample("ok-1", t, 5)]}, policy, reg)
    ingest_batch(conn, {"batch_id": "rr2", "samples": [_bridge_sample("ok-1", t, 6)]}, policy, reg)   # a variant
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True, accept_reread_mismatches=True)
    assert not R["ok"] and R["facts"]["compare_classes"] == {"ambiguous": 1}


def test_cross_midnight_whoop_day_row_resolves_by_its_id_date(tmp_path):
    path, conn, policy, reg, t = _base(tmp_path, "w.duckdb")
    with db.transaction(conn) as c:
        whoop.apply_record(c, "recovery", recovery_rec(901, 1901, "2026-07-13T04:00:00.000Z", score=70, hrv=50.0), policy, datetime(2026, 10, 1), "p")
    # The day row files under 07-13 by id, but its start wall is the evening before (bed date).
    conn.execute("INSERT INTO samples (sample_id, metric, value, unit, start_ts, end_ts, source_name, device_key, sync_path, ingested_at) VALUES "
                 "('wh:recovery_score:2026-07-13', 'recovery_score', 61, '%', '2026-07-12 23:30', '2026-07-13 07:00', 'WHOOP', 'whoop', 'whoop_live', ?)", [ERA_INGEST[4]])
    conn.close()
    # Through the rebuild and the diff too (checkpoint C point 14): the old day row fed
    # the 07-12 cell (its start wall), the record feeds 07-13; the removed 07-12 cell
    # must be explained by the Whoop replacement, never fall to stale_before.
    R = run(path, policy, reg, tmp_path, cutover=True, rebuild=True, baseline_rebuild=True)
    assert R["ok"], (R["fails"], R["stopped"])
    assert R["facts"]["whoop_day_rows_by_outcome"] == [["recovery_score", "replaced", 1]]
    diff = {(k, r): n for k, r, n in R["facts"]["derived_diff_daily_values"]}
    assert not any(r == "stale_before" for (_k, r) in diff), diff
    assert any(k == "removed" and "whoop_replacement" in r for (k, r) in diff), diff
    assert R["facts"]["derived_diff_unexplained_cells"] == 0
    c = duckdb.connect(str(path), read_only=True)
    assert c.execute("SELECT new_id FROM sample_aliases WHERE old_id = 'wh:recovery_score:2026-07-13'").fetchone()[0] == "wh:recovery_score:recovery:901"
    assert c.execute("SELECT COUNT(*) FROM samples WHERE sync_path = 'whoop_live' AND time_source IS NULL").fetchone()[0] == 0
    c.close()


def test_permuted_column_order_is_mapped_by_name_not_position(tmp_path):
    """The staged table is built with explicit column lists; a store whose
    samples table grew in another column order migrates the same."""
    path = tmp_path / "perm.duckdb"
    raw = duckdb.connect(str(path))
    raw.execute("""CREATE TABLE samples (sample_id VARCHAR PRIMARY KEY, quality VARCHAR, start_utc TIMESTAMP, hk_uuid VARCHAR, metric VARCHAR NOT NULL,
        time_source VARCHAR, hk_type VARCHAR, value DOUBLE, text_value VARCHAR, unit VARCHAR, end_utc TIMESTAMP, start_ts TIMESTAMP NOT NULL, end_ts TIMESTAMP,
        source_name VARCHAR NOT NULL, device_key VARCHAR NOT NULL, sync_path VARCHAR NOT NULL, ingested_at TIMESTAMP DEFAULT current_timestamp)""")
    raw.close()
    conn = duckdb.connect(str(path))
    db.init_schema(conn)
    policy = MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    reg = SourceRegistry()
    t = datetime(2026, 6, 1, 20, 30)
    _legacy(conn, "ch2:p1", "perm-1", "steps", STEPS, t, 1, 42, "count", AWU, "apple_watch_ultra", text=None)
    _legacy(conn, "ch2:p2", "perm-1", "steps", STEPS, t, 2, 42, "count", AWU, "apple_watch_ultra", text=None)
    conn.close()
    R = rh.Migration(path, policy, reg, tmp_path / "o", today=TODAY, label="test", cutover=True).run()
    assert R["ok"], R["fails"]
    c = db.connect(path, allow_unverified=True)
    row = _sample(c, "hk:perm-1")
    assert (row["start_utc"], row["start_ts"], row["value"], row["quality"], row["rebase_era"], row["metric"]) == (t, t + DUBAI, 42.0, None, 1, "steps")
    c.close()


def test_backup_filter_plus_archive_restores_the_complete_alias_table(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    build_fixture(conn, policy, reg)
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True)
    assert R["ok"]
    archive = pathlib.Path(R["facts"]["archive_places"][0])
    c = db.connect(path, allow_unverified=True)
    m = bk.export_tables(c, tmp_path / "exp")
    live = {tuple(r) for r in db.fetchall(c, "SELECT old_id, new_id, reason FROM sample_aliases")}
    c.close()
    spec = m["tables"]["sample_aliases"]
    assert spec["filter"]["excluded_reasons"] == list(rh.MIGRATION_ALIAS_REASONS) and spec["rows"] == 1 and spec["rows_excluded"] == len(live) - 1
    # Without the archive the drill is NOT ok and says why; with it the table is complete, row by row.
    r0 = bk.restore_test(tmp_path / "exp")
    assert not r0["ok"] and any("archive" in p for p in r0["problems"])
    r1 = bk.restore_test(tmp_path / "exp", archive_dir=archive)
    assert r1["ok"], r1["problems"]
    assert r1["tables"]["sample_aliases"]["restored_with_archive"] == len(live)
    restored = db.connect_memory()
    bk.load_tables(restored, tmp_path / "exp")
    bk.load_archive_aliases(restored, archive)
    assert {tuple(r) for r in db.fetchall(restored, "SELECT old_id, new_id, reason FROM sample_aliases")} == live
    # A tampered archive is refused.
    (archive / "lineage_aliases.parquet").write_bytes(b"x")
    assert not bk.restore_test(tmp_path / "exp", archive_dir=archive)["ok"]


def test_budgets_stop_without_a_named_exception(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    build_fixture(conn, policy, reg)
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True, exceptions=())
    assert not R["ok"] and "export_ambiguous_within_budget_per_type" in R["fails"]
    assert R["facts"]["budget_export_ambiguous"][0][0] == "steps" and R["facts"]["budget_export_ambiguous"][0][3] > 0.5
    c = duckdb.connect(str(path), read_only=True)
    assert c.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 0
    c.close()


def test_a_pre_existing_conflicting_alias_stops_the_final_relation_gate(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    build_fixture(conn, policy, reg)
    # A stale alias that RESOLVES (its target lives after the migration) but disagrees with the staged mapping.
    conn.execute("INSERT INTO sample_aliases (old_id, new_id, reason) VALUES ('ch2:a1', 'hk:u-steps-2', 'stale')")
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True)
    assert not R["ok"] and "no_old_id_maps_to_two_targets_in_the_final_relation" in R["fails"]
    # A dangling pre-existing target is caught by the resolution gate.
    path2, conn2, policy, reg = _env(tmp_path, "dangling.duckdb")
    build_fixture(conn2, policy, reg)
    conn2.execute("INSERT INTO sample_aliases (old_id, new_id, reason) VALUES ('ch2:zz', 'hk:nowhere', 'stale')")
    conn2.close()
    R2 = rh.Migration(path2, policy, reg, tmp_path / "o2", archive_dirs=[tmp_path / "a1", tmp_path / "a2"], today=TODAY, label="test",
                      cutover=True, exceptions=("budget:whoop", "budget:export")).run()
    assert not R2["ok"] and "every_alias_in_the_final_relation_resolves_to_a_live_row_or_a_tombstone" in R2["fails"]


def test_cutover_without_rebuild_is_not_verified_until_resumed(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    build_fixture(conn, policy, reg)
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True)
    assert R["ok"] and R["facts"]["migration_phase"] == "cutover_committed"
    c = duckdb.connect(str(path), read_only=True)
    assert json.loads(c.execute("SELECT summary FROM migrations").fetchone()[0])["phase"] == "cutover_committed"
    c.close()
    # A plain second run is refused; the resume finishes the verification and marks the row.
    assert "migration_not_applied_yet" in rh.Migration(path, policy, reg, tmp_path / "o2", today=TODAY, label="test", cutover=True).run()["fails"]
    R2 = rh.Migration(path, policy, reg, tmp_path / "out", archive_dirs=[tmp_path / "arch1", tmp_path / "arch2"], today=TODAY,
                      label="test", rebuild=True, cutover=True, resume_verify=True).run()
    assert R2["ok"], (R2["fails"], R2["stopped"])
    assert R2["facts"]["migration_phase"] == "verified" and R2["facts"]["oracle"]["mismatches"] == 0
    c = duckdb.connect(str(path), read_only=True)
    assert json.loads(c.execute("SELECT summary FROM migrations").fetchone()[0])["phase"] == "verified"
    c.close()
    R3 = rh.Migration(path, policy, reg, tmp_path / "o3", today=TODAY, label="test", rebuild=True, cutover=True, resume_verify=True).run()
    assert not R3["ok"] and "resume_requires_a_committed_unverified_migration" in R3["fails"]


def test_apply_mode_demands_its_evidence(tmp_path):
    path, conn, policy, reg = _env(tmp_path)
    build_fixture(conn, policy, reg)
    conn.close()
    R = rh.Migration(path, policy, reg, tmp_path / "out", archive_dirs=[tmp_path / "one"], today=TODAY, label="apply", cutover=True, rebuild=True,
                     exceptions=("dirty_tree",)).run()
    assert not R["ok"] and R["stopped"] == "gate failed: apply_evidence_complete"
    fails = set(R["fails"])
    assert {"apply_requires_an_anchor_path", "apply_requires_two_distinct_archive_places", "apply_requires_the_expected_fingerprints"} <= fails
    c = duckdb.connect(str(path), read_only=True)
    assert c.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 0
    c.close()


def test_same_era_identical_twins_are_not_a_known_mechanism_and_stop(tmp_path):
    path, conn, policy, reg, t = _base(tmp_path, "se.duckdb")
    _legacy(conn, "ch2:e1", "same", "steps", STEPS, t, 2, 5, "count", AWU, "apple_watch_ultra")
    _legacy(conn, "ch2:e2", "same", "steps", STEPS, t, 2, 5, "count", AWU, "apple_watch_ultra")
    conn.close()
    R = run(path, policy, reg, tmp_path, cutover=True)
    assert not R["ok"] and "twin_pairs_cross_eras_1_2_or_2_4_only" in R["fails"]


def test_apply_flag_is_enforced_whatever_the_label(tmp_path):
    """Checkpoint C point 25: --apply --label other still runs the evidence policy."""
    path, conn, policy, reg = _env(tmp_path)
    build_fixture(conn, policy, reg)
    conn.close()
    R = rh.Migration(path, policy, reg, tmp_path / "out", archive_dirs=[tmp_path / "one"], today=TODAY, label="other", cutover=True, rebuild=True,
                     apply=True, exceptions=("dirty_tree",)).run()
    assert R["flags"]["apply"] is True and R["facts"]["apply_mode"] is True
    assert not R["ok"] and R["stopped"] == "gate failed: apply_evidence_complete"
    assert {"apply_requires_an_anchor_path", "apply_requires_two_distinct_archive_places", "apply_requires_the_expected_fingerprints",
            "apply_requires_reread_coverage_per_type"} <= set(R["fails"])
    cov = R["facts"]["reread_coverage_by_type"]
    assert cov and any(r[3] is None or r[3] < rh.REREAD_COVERAGE_MIN for r in cov) and len(R["checks"]["apply_requires_reread_coverage_per_type"]["detail"]["below"]) > 0
    c = duckdb.connect(str(path), read_only=True)
    assert c.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 0
    c.close()
    # The named exception waives the coverage requirement and is recorded in the flags.
    R2 = rh.Migration(path, policy, reg, tmp_path / "out2", archive_dirs=[tmp_path / "one"], today=TODAY, label="other", cutover=True, rebuild=True,
                      apply=True, exceptions=("dirty_tree", "reread_coverage")).run()
    assert "apply_requires_reread_coverage_per_type" not in R2["fails"] and "reread_coverage" in R2["flags"]["exceptions"]


def test_verification_with_a_failed_check_leaves_the_row_cutover_committed(tmp_path):
    """Checkpoint C point 29: the verified marker needs every check green."""
    path, conn, policy, reg = _env(tmp_path)
    build_fixture(conn, policy, reg)
    conn.close()
    m = rh.Migration(path, policy, reg, tmp_path / "out", archive_dirs=[tmp_path / "arch1", tmp_path / "arch2"], today=TODAY, label="test",
                     cutover=True, rebuild=True, exceptions=("budget:whoop", "budget:export"))
    real_rebuild = m.rebuild

    def rebuild_with_a_failed_nonfatal_check():
        real_rebuild()
        m.check("injected_nonfatal_failure", False, "test", fatal=False)
    m.rebuild = rebuild_with_a_failed_nonfatal_check
    R = m.run()
    assert not R["ok"] and "injected_nonfatal_failure" in R["fails"] and R["facts"]["migration_phase"] == "cutover_committed"
    c = duckdb.connect(str(path), read_only=True)
    assert json.loads(c.execute("SELECT summary FROM migrations").fetchone()[0])["phase"] == "cutover_committed"
    c.close()
    import pytest
    with pytest.raises(RuntimeError, match="committed but not verified"):
        db.connect(path)
