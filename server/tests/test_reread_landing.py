"""Phase 1b item (b): the HealthKit re-read landing (design.md section 5,
adjudication-A points 17 to 21, 37). Oracles are the instants and values
written into the payloads, never read back from the code under test."""

from __future__ import annotations

import json
import random
import time
from datetime import datetime

from heliosd.ingest.bridge import ingest_batch
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

AWU = "Owner’s Ultra 1"
STEPS = "HKQuantityTypeIdentifierStepCount"
MASS = "HKQuantityTypeIdentifierBodyMass"
FAT = "HKQuantityTypeIdentifierBodyFatPercentage"


def _env():
    conn = db.connect_memory()
    policy = MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    return conn, policy, SourceRegistry()


def _s(uuid, start, end=None, value=100, hk=STEPS, unit="count", source=AWU):
    return {"hk_type": hk, "value": value, "unit": unit, "start": start, "end": end or start,
            "source_name": source, "uuid": uuid}


def _legacy(conn, uuid, sid, wall, value=100, metric="steps", time_source=None, source=AWU, device="apple_watch_ultra"):
    """A legacy row as the live store holds it: ch2 id, Mac-local wall time,
    every Phase 1a column NULL (or the given time_source)."""
    db.execute(conn, "INSERT INTO samples (sample_id, hk_uuid, metric, hk_type, value, unit, start_ts, end_ts, source_name, "
                     "device_key, sync_path, time_source) VALUES (?, ?, ?, ?, ?, 'count', ?, ?, ?, ?, 'bridge', ?)",
               [sid, uuid, metric, STEPS, value, wall, wall, source, device, time_source])


def _reread(conn):
    return db.fetchdicts(conn, "SELECT * FROM hk_reread ORDER BY hk_uuid")


def _variants(conn):
    return db.fetchdicts(conn, "SELECT * FROM hk_reread_variants ORDER BY hk_uuid, seq")


def test_legacy_uuid_lands_with_normalized_fields_and_raw_strings():
    conn, policy, reg = _env()
    _legacy(conn, "u1", "ch2:a", "2026-06-01 10:00")        # era 1 wall (UTC) ...
    _legacy(conn, "u1", "ch2:b", "2026-06-01 15:30")        # ... and its 330-minute twin
    res = ingest_batch(conn, {"batch_id": "rr-1", "samples": [
        _s("u1", "2026-06-01T10:00:00.250Z", "2026-06-01T10:05:00Z", value=100)]}, policy, reg)
    assert res["accepted"] == 0 and res["guarded"] == 1 and res["landed"] == 1
    assert res["guard_outcomes"]["landed_first"] == 1 and res["affected_dates"] == []
    rows = _reread(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["start_utc"] == datetime(2026, 6, 1, 10, 0) and r["end_utc"] == datetime(2026, 6, 1, 10, 5)
    assert r["start_raw"] == "2026-06-01T10:00:00.250Z" and r["end_raw"] == "2026-06-01T10:05:00Z"
    assert r["metric"] == "steps" and r["value"] == 100 and r["device_key"] == "apple_watch_ultra"
    assert r["existing_rows"] == 2 and r["existing_time_source"] == "legacy"
    assert r["n_seen"] == 1 and r["first_batch"] == "rr-1" and r["last_batch"] == "rr-1"
    # The legacy rows are untouched; the receipt carries the landing counts.
    assert db.fetchall(conn, "SELECT COUNT(*) FROM samples WHERE hk_uuid = 'u1'")[0][0] == 2
    log = db.fetchdicts(conn, "SELECT n_guarded, n_landed, guard_outcomes FROM sync_log WHERE batch_id = 'rr-1'")[0]
    assert log["n_guarded"] == 1 and log["n_landed"] == 1
    assert json.loads(log["guard_outcomes"])["landed_first"] == 1


def test_identical_repeat_bumps_n_seen_and_the_same_batch_is_a_no_op():
    conn, policy, reg = _env()
    _legacy(conn, "u1", "ch2:a", "2026-06-01 10:00")
    s = _s("u1", "2026-06-01T10:00:00Z")
    ingest_batch(conn, {"batch_id": "b1", "samples": [s]}, policy, reg)
    again = ingest_batch(conn, {"batch_id": "b1", "samples": [s]}, policy, reg)        # outbox retry
    assert again["guard_outcomes"] == {"landed_same_batch": 1}
    assert again["landed"] == 1                 # the batch's count, so its replaced receipt keeps it
    assert _reread(conn)[0]["n_seen"] == 1      # recorded once
    assert db.fetchall(conn, "SELECT n_landed FROM sync_log WHERE batch_id = 'b1'")[0][0] == 1
    other = ingest_batch(conn, {"batch_id": "b2", "samples": [s]}, policy, reg)        # the dated sweep
    assert other["landed"] == 1 and other["guard_outcomes"]["landed_repeat"] == 1
    r = _reread(conn)[0]
    assert r["n_seen"] == 2 and r["first_batch"] == "b1" and r["last_batch"] == "b2" and r["batches"] == ["b1", "b2"]
    # A non-adjacent retry of b1 (A, B, A) is still the same batch: nothing new.
    late = ingest_batch(conn, {"batch_id": "b1", "samples": [s]}, policy, reg)
    assert late["guard_outcomes"] == {"landed_same_batch": 1}
    assert _reread(conn)[0]["n_seen"] == 2 and _reread(conn)[0]["batches"] == ["b1", "b2"]
    assert _variants(conn) == []


def test_differing_content_becomes_a_variant_once_per_distinct_content():
    conn, policy, reg = _env()
    _legacy(conn, "u1", "ch2:a", "2026-06-01 10:00")
    ingest_batch(conn, {"batch_id": "b1", "samples": [_s("u1", "2026-06-01T10:00:00Z")]}, policy, reg)
    shifted = _s("u1", "2026-06-01T10:30:00Z")
    r1 = ingest_batch(conn, {"batch_id": "b2", "samples": [shifted]}, policy, reg)
    assert r1["guard_outcomes"] == {"landed_variant": 1} and r1["writes"] == {"variants_written": 1} and r1["landed"] == 1
    r2 = ingest_batch(conn, {"batch_id": "b2", "samples": [shifted]}, policy, reg)     # retry: no twin variant
    assert r2["guard_outcomes"] == {"landed_variant": 1} and r2["writes"] == {"variants_written": 0} and r2["landed"] == 1
    assert db.fetchall(conn, "SELECT COUNT(*) FROM hk_reread_variants")[0][0] == 1
    assert db.fetchall(conn, "SELECT n_landed FROM sync_log WHERE batch_id = 'b2'")[0][0] == 1   # the receipt keeps the batch's count
    ingest_batch(conn, {"batch_id": "b3", "samples": [_s("u1", "2026-06-01T10:00:00Z", value=101)]}, policy, reg)
    v = _variants(conn)
    assert [(x["seq"], x["start_utc"], x["value"]) for x in v] == [
        (1, datetime(2026, 6, 1, 10, 30), 100.0), (2, datetime(2026, 6, 1, 10, 0), 101.0)]
    assert v[0]["existing_time_source"] == "legacy" and v[0]["batch_id"] == "b2"
    first = _reread(conn)[0]
    assert first["start_utc"] == datetime(2026, 6, 1, 10, 0) and first["value"] == 100 and first["n_seen"] == 1


def test_native_row_lands_only_as_a_variant_and_only_when_different():
    conn, policy, reg = _env()
    s = _s("n1", "2026-10-05T04:00:00Z", value=250)
    ingest_batch(conn, {"batch_id": "live", "samples": [s]}, policy, reg)                 # bridge_utc row
    same = ingest_batch(conn, {"batch_id": "sweep", "samples": [s]}, policy, reg)         # the 48 h sweep
    assert same["guard_outcomes"]["native_identical"] == 1 and same["landed"] == 0
    assert _reread(conn) == [] and _variants(conn) == []
    diff = ingest_batch(conn, {"batch_id": "sweep2", "samples": [_s("n1", "2026-10-05T04:00:00Z", value=260)]}, policy, reg)
    assert diff["guard_outcomes"]["native_variant"] == 1 and diff["landed"] == 1
    v = _variants(conn)
    assert len(v) == 1 and v[0]["existing_time_source"] == "bridge_utc" and v[0]["value"] == 260
    assert _reread(conn) == []                                                            # never a first observation
    assert db.fetchall(conn, "SELECT value FROM samples WHERE hk_uuid = 'n1'")[0][0] == 250


def test_rebased_row_counts_as_legacy_and_reread_row_as_native():
    conn, policy, reg = _env()
    _legacy(conn, "r1", "hk:r1", "2026-06-01 14:00", time_source="era_rebase_v1")
    _legacy(conn, "c1", "hk:c1", "2026-06-01 14:00", time_source="bridge_reread_v1")
    res = ingest_batch(conn, {"batch_id": "b", "samples": [_s("r1", "2026-06-01T10:00:00Z"), _s("c1", "2026-06-01T10:00:00Z")]},
                       policy, reg)
    o = res["guard_outcomes"]
    assert o["landed_first"] == 1 and o.get("native_variant", 0) + o.get("native_identical", 0) == 1
    assert [r["hk_uuid"] for r in _reread(conn)] == ["r1"]
    assert _reread(conn)[0]["existing_time_source"] == "era_rebase_v1"


def test_tombstoned_and_deleted_in_batch_are_counted_never_landed():
    conn, policy, reg = _env()
    _legacy(conn, "t1", "ch2:t", "2026-06-01 10:00")
    ingest_batch(conn, {"batch_id": "del", "samples": [], "deleted": ["t1"]}, policy, reg)
    res = ingest_batch(conn, {"batch_id": "replay", "samples": [_s("t1", "2026-06-01T10:00:00Z")]}, policy, reg)
    assert res["guard_outcomes"] == {"tombstoned": 1} and res["landed"] == 0
    _legacy(conn, "d1", "ch2:d", "2026-06-02 10:00")
    res = ingest_batch(conn, {"batch_id": "both", "samples": [_s("d1", "2026-06-02T10:00:00Z")], "deleted": ["d1"]}, policy, reg)
    assert res["guard_outcomes"] == {"deleted_in_batch": 1} and res["deleted"] == 1
    assert _reread(conn) == [] and db.fetchall(conn, "SELECT COUNT(*) FROM samples WHERE hk_uuid IN ('t1', 'd1')")[0][0] == 0


def test_new_uuid_inserts_normally_and_lands_nothing():
    conn, policy, reg = _env()
    res = ingest_batch(conn, {"batch_id": "b", "samples": [_s("fresh", "2026-10-05T04:00:00Z")]}, policy, reg)
    assert res["accepted"] == 1 and res["guard_outcomes"] == {"new": 1} and res["landed"] == 0
    assert db.fetchall(conn, "SELECT n_landed FROM sync_log WHERE batch_id = 'b'")[0][0] == 0


def test_body_fat_fraction_legacy_row_lands_the_percent_observation():
    """A known normalization change (fraction to percent) is landed as
    delivered and classified by the migration, never scaled here."""
    conn, policy, reg = _env()
    db.execute(conn, "INSERT INTO samples (sample_id, hk_uuid, metric, hk_type, value, unit, start_ts, end_ts, source_name, device_key, sync_path) "
                     "VALUES ('ch2:f', 'f1', 'body_fat_pct', ?, 0.2534, '%', '2026-06-01 10:00', '2026-06-01 10:00', 'Zepp Life', 'zepp_life_scale', 'bridge')", [FAT])
    ingest_batch(conn, {"batch_id": "b", "samples": [_s("f1", "2026-06-01T06:00:00Z", value=0.2534, hk=FAT, unit="%", source="Zepp Life")]}, policy, reg)
    r = _reread(conn)[0]
    assert r["value"] == 25.34 and r["unit_rule"] == "frac_to_pct_v1" and r["metric"] == "body_fat_pct"
    assert r["time_source"] == "bridge_utc"
    assert db.fetchall(conn, "SELECT value FROM samples WHERE hk_uuid = 'f1'")[0][0] == 0.2534


def test_an_offset_free_observation_is_landed_with_its_provenance_not_as_a_confirmation():
    conn, policy, reg = _env()
    _legacy(conn, "u1", "ch2:a", "2026-06-01 10:00")
    ingest_batch(conn, {"batch_id": "b", "samples": [_s("u1", "2026-06-01T07:00:00")]}, policy, reg)   # no Z: read as reporting wall
    r = _reread(conn)[0]
    assert r["time_source"] == "assumed_reporting_wall" and r["start_utc"] == datetime(2026, 6, 1, 3, 0)


def test_two_contents_of_one_uuid_in_one_page_are_both_kept():
    """Checkpoint B point 9: a page carrying one uuid twice with different
    content keeps both observations, in either order; the set is the same."""
    sets = []
    for order in ((100, 999), (999, 100)):
        conn, policy, reg = _env()
        _legacy(conn, "u1", "ch2:a", "2026-06-01 10:00")
        res = ingest_batch(conn, {"batch_id": "p", "samples": [_s("u1", "2026-06-01T10:00:00Z", value=v) for v in order]}, policy, reg)
        assert res["guard_outcomes"] == {"landed_first": 1, "landed_variant": 1} and res["writes"] == {"variants_written": 1}
        assert res["landed"] == 2 and res["accepted"] == 0
        sets.append(_state(conn)["u1"])
        assert len(sets[-1]) == 2
    assert sets[0] == sets[1]
    # A NEW uuid twice with different content: the first inserts, the second is a variant.
    conn, policy, reg = _env()
    res = ingest_batch(conn, {"batch_id": "p", "samples": [_s("n1", "2026-06-01T10:00:00Z", value=5), _s("n1", "2026-06-01T10:00:00Z", value=6)]}, policy, reg)
    assert res["accepted"] == 1 and res["guard_outcomes"] == {"new": 1, "native_variant": 1} and res["writes"] == {"variants_written": 1}
    assert db.fetchall(conn, "SELECT value FROM samples WHERE hk_uuid = 'n1'")[0][0] == 5
    assert [v["value"] for v in _variants(conn)] == [6.0]
    # An identical repeat inside the page is counted and dropped, never a variant.
    res = ingest_batch(conn, {"batch_id": "q", "samples": [_s("n2", "2026-06-01T10:00:00Z", value=5), _s("n2", "2026-06-01T10:00:00Z", value=5)]}, policy, reg)
    assert res["accepted"] == 1 and res["skipped_types"] == {"dup_in_batch_identical": 1} and _variants(conn) == [_variants(conn)[0]]


def test_a_failure_after_the_landing_rolls_the_whole_batch_back_and_the_retry_lands(monkeypatch):
    from heliosd.ingest import bridge as bridge_mod
    conn, policy, reg = _env()
    _legacy(conn, "u1", "ch2:a", "2026-06-01 10:00")
    real = bridge_mod._journal

    def boom(*a, **k):
        raise RuntimeError("injected after the landing")
    monkeypatch.setattr(bridge_mod, "_journal", boom)
    try:
        ingest_batch(conn, {"batch_id": "b1", "samples": [_s("u1", "2026-06-01T10:00:00Z"), _s("fresh", "2026-06-01T11:00:00Z")]}, policy, reg)
        assert False, "expected the injected failure"
    except RuntimeError:
        pass
    assert _reread(conn) == [] and db.fetchall(conn, "SELECT COUNT(*) FROM sync_log")[0][0] == 0
    assert db.fetchall(conn, "SELECT COUNT(*) FROM samples WHERE hk_uuid = 'fresh'")[0][0] == 0
    monkeypatch.setattr(bridge_mod, "_journal", real)
    res = ingest_batch(conn, {"batch_id": "b1", "samples": [_s("u1", "2026-06-01T10:00:00Z"), _s("fresh", "2026-06-01T11:00:00Z")]}, policy, reg)
    assert res["accepted"] == 1 and res["guard_outcomes"] == {"new": 1, "landed_first": 1} and len(_reread(conn)) == 1


def _state(conn):
    """The set of distinct observations per uuid (first observation plus
    variants). WHICH observation is first depends on arrival order by design
    (the first wins); the set does not."""
    obs: dict[str, set] = {}
    for r in _reread(conn):
        obs.setdefault(r["hk_uuid"], set()).add((r["start_utc"], r["end_utc"], r["value"]))
    for v in _variants(conn):
        obs.setdefault(v["hk_uuid"], set()).add((v["start_utc"], v["end_utc"], v["value"]))
    return obs


def test_overlapping_batches_in_any_order_give_the_same_landing_state():
    """Delivery-order independence (point 37): the resume-point drain and the
    dated sweep overlap and may arrive in any order."""
    batches = {}
    for j in range(4):
        batches[f"p{j}"] = [_s(f"u{i}", f"2026-06-0{1 + (i % 5)}T10:0{i % 6}:00Z", value=100 + (i % 3)) for i in range(j * 10, j * 10 + 25)]
    batches["shift"] = [_s("u7", "2026-06-03T10:01:00Z", value=100 + 1 + 0), _s("u8", "2026-06-04T10:02:00Z", value=999)]   # u8 differs
    states = []
    for seed in (1, 2, 3):
        conn, policy, reg = _env()
        for i in range(40):
            _legacy(conn, f"u{i}", f"ch2:{i}", "2026-06-01 10:00", value=100 + (i % 3))
        order = list(batches)
        random.Random(seed).shuffle(order)
        for name in order:
            ingest_batch(conn, {"batch_id": name, "samples": batches[name]}, policy, reg)
        states.append(_state(conn))
    assert states[0] == states[1] == states[2]
    obs = states[0]
    assert len(obs) == 40 and sum(len(v) for v in obs.values()) == 41
    assert len(obs["u8"]) == 2 and 999.0 in {o[2] for o in obs["u8"]}


def test_two_thousand_guarded_rows_land_quickly():
    conn, policy, reg = _env()
    with db.transaction(conn) as c:
        c.executemany("INSERT INTO samples (sample_id, hk_uuid, metric, hk_type, value, unit, start_ts, end_ts, source_name, device_key, sync_path) "
                      "VALUES (?, ?, 'steps', ?, 1, 'count', '2026-06-01 10:00', '2026-06-01 10:00', ?, 'apple_watch_ultra', 'bridge')",
                      [[f"ch2:{i}", f"u{i}", STEPS, AWU] for i in range(2000)])
    batch = {"batch_id": "big", "samples": [_s(f"u{i}", "2026-06-01T06:00:00Z", value=1) for i in range(2000)]}
    t0 = time.monotonic()
    res = ingest_batch(conn, batch, policy, reg)
    assert res["landed"] == 2000 and time.monotonic() - t0 < 5.0
    t0 = time.monotonic()
    res = ingest_batch(conn, {**batch, "batch_id": "big2"}, policy, reg)
    assert res["guard_outcomes"]["landed_repeat"] == 2000 and time.monotonic() - t0 < 5.0
