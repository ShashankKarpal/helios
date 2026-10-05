"""Phase 1b item (f): the re-read progress report (owner gate completion)."""

from __future__ import annotations

from datetime import datetime

from heliosd.ingest.bridge import ingest_batch
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry
from tools.reread_progress import progress, render

AWU = "Owner’s Ultra 1"
STEPS, RHR = "HKQuantityTypeIdentifierStepCount", "HKQuantityTypeIdentifierRestingHeartRate"


def _env():
    conn = db.connect_memory()
    policy = MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    return conn, policy, SourceRegistry()


def _legacy(conn, uuid, hk, metric):
    db.execute(conn, "INSERT INTO samples (sample_id, hk_uuid, metric, hk_type, value, unit, start_ts, end_ts, source_name, device_key, sync_path) "
                     "VALUES (?, ?, ?, ?, 1, 'x', '2026-06-01 10:00', '2026-06-01 10:00', ?, 'apple_watch_ultra', 'bridge')",
               [f"ch2:{uuid}", uuid, metric, hk, AWU])


def _s(uuid, hk, iso="2026-06-01T06:00:00Z"):
    return {"hk_type": hk, "value": 1, "unit": "count" if hk == STEPS else "count/min", "start": iso, "end": iso, "source_name": AWU, "uuid": uuid}


def test_progress_counts_coverage_per_type_and_detects_completion():
    conn, policy, reg = _env()
    for i in range(4):
        _legacy(conn, f"s{i}", STEPS, "steps")
    _legacy(conn, "s0", STEPS, "steps") if False else None
    _legacy(conn, "r0", RHR, "resting_hr")
    q = lambda sql: db.fetchdicts(conn, sql)  # noqa: E731
    p = progress(q, since="2026-06-01 00:00:00", threshold=0.95, quiet_minutes=15)
    assert {t["hk_type"]: (t["legacy_uuids"], t["landed"], t["done"]) for t in p["types"]} == {STEPS: (4, 0, False), RHR: (1, 0, False)}
    assert p["complete"] is False and p["minutes_since_last_batch"] is None
    ingest_batch(conn, {"batch_id": "rr1", "samples": [_s(f"s{i}", STEPS) for i in range(4)] + [_s("r0", RHR), _s("brand-new", STEPS)]}, policy, reg)
    p = progress(q, since="2026-06-01 00:00:00", threshold=0.95, quiet_minutes=0)
    by = {t["hk_type"]: t for t in p["types"]}
    assert by[STEPS]["landed"] == 4 and by[STEPS]["coverage"] == 1.0 and by[STEPS]["new_rows"] == 1 and by[STEPS]["done"]
    assert by[RHR]["coverage"] == 1.0 and p["all_types_done"] and p["batches"]["batches_last_hour"] == 1
    assert p["complete"] is True                       # quiet window 0: complete as soon as every type is covered
    p = progress(q, since="2026-06-01 00:00:00", threshold=0.95, quiet_minutes=15)
    assert p["complete"] is False                      # the last batch is seconds old
    assert "types done 2 of 2" in render(p)
