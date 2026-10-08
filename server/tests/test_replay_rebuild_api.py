"""Phase 1a item 10: idempotence, delivery-order independence, deletion then
replay, full rebuild equals incremental, and API outputs asserted as tuples.
The oracle is computed here from the generated input (sums, last values,
medians), never read back from the code under test (checkpoint A, 30 to 34)."""

from __future__ import annotations

import asyncio
import statistics
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from heliosd import main
from heliosd.config import Settings
from heliosd.ingest import whoop
from heliosd.ingest.bridge import ingest_batch
from heliosd.signals import recompute as rc
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry
from tests.test_whoop_records import FakeClient, sleep_rec

AWU, IPHONE, SCALE = "Owner’s Ultra 1", "Owner's 16 Pro Max", "Zepp Life"
D0 = date(2026, 6, 1)
N_DAYS = 12
TODAY = D0 + timedelta(days=13)
NOW = datetime(2026, 6, 14, 5, 0, tzinfo=timezone.utc)      # 09:00 Dubai on TODAY


def _env():
    conn = db.connect_memory()
    policy = MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    return conn, policy, SourceRegistry()


def _q(hk, uuid, day, hour, value, source, unit):
    t = f"{day}T{hour:02d}:00:00Z"
    return {"hk_type": hk, "value": value, "unit": unit, "start": t, "end": t, "source_name": source, "uuid": uuid}


def _day_samples(i: int) -> list[dict]:
    d = D0 + timedelta(days=i)
    out = [_q("HKQuantityTypeIdentifierStepCount", f"aw-{i}-{k}", d, h, v, AWU, "count")
           for k, (h, v) in enumerate(((4, 1000 + 10 * i), (8, 2000 + 10 * i), (12, 500)))]
    out.append(_q("HKQuantityTypeIdentifierStepCount", f"ip-{i}", d, 6, 3000 + 10 * i, IPHONE, "count"))
    out.append(_q("HKQuantityTypeIdentifierRestingHeartRate", f"rh-{i}", d, 3, 55 + i % 4, AWU, "count/min"))
    if i == 2:   # two scale readings: the later wall time wins for a `last` metric
        out.append(_q("HKQuantityTypeIdentifierBodyMass", "bm-2-a", d, 3, 80.5, SCALE, "kg"))
        out.append(_q("HKQuantityTypeIdentifierBodyMass", "bm-2-b", d, 5, 80.1, SCALE, "kg"))
    if i == 9:
        out.append(_q("HKQuantityTypeIdentifierBodyMass", "bm-9", d, 4, 79.9, SCALE, "kg"))
    return out


def batches() -> dict[str, dict]:
    b = {f"B{j + 1}": {"batch_id": f"B{j + 1}", "samples": [s for i in range(3 * j, 3 * j + 3) for s in _day_samples(i)]}
         for j in range(4)}
    b["DEL"] = {"batch_id": "DEL", "samples": [], "deleted": ["rh-5", "aw-7-1"]}        # rh-5 is day 5's only input
    b["LATE"] = {"batch_id": "LATE", "samples": [_q("HKQuantityTypeIdentifierStepCount", "late-1", D0 + timedelta(days=1), 14, 300, AWU, "count")]}
    return b


NATURAL = ["B1", "B2", "B3", "B4", "DEL", "LATE"]
ORDERS = {"natural": NATURAL, "reversed": NATURAL[::-1], "interleaved": ["B3", "DEL", "B1", "LATE", "B4", "B2"]}


def oracle_daily() -> dict[tuple[date, str], tuple[float, str, str]]:
    """(date, metric) -> (value, unit, device), from the input minus the deletions."""
    out = {}
    for i in range(N_DAYS):
        d = D0 + timedelta(days=i)
        steps = (1000 + 10 * i) + (2000 + 10 * i) + 500 + (300 if i == 1 else 0) - ((2000 + 10 * i) if i == 7 else 0)
        # Owner decision D4 (B11): the iPhone's steps count where no watch sample covers them. The watch writes
        # instants here, which cover no time, so the iPhone's sample counts in full (its own total stays beside it).
        steps += 3000 + 10 * i
        out[(d, "steps")] = (float(steps), "count", "apple_watch_ultra")
        if i != 5:
            out[(d, "resting_hr")] = (float(55 + i % 4), "count/min", "apple_watch_ultra")
    out[(D0 + timedelta(days=2), "body_mass")] = (80.1, "kg", "zepp_life_scale")
    out[(D0 + timedelta(days=9), "body_mass")] = (79.9, "kg", "zepp_life_scale")
    return out


def oracle_baselines(policy) -> dict[tuple[date, str, int], tuple[float, float, int]]:
    """Owner scope (Wave 2, design B5): a baseline reads only the days of the metric's owner, so Apple's
    resting HR, which stands in for Whoop's since decision 4h, has none here."""
    daily = oracle_daily()
    out = {}
    for metric in ("steps", "resting_hr", "body_mass"):
        owner = policy.priority(metric)[0]
        for k in range((TODAY - D0).days + 1):
            d = D0 + timedelta(days=k)
            for w in policy.windows:
                vals = [v for (dd, m), (v, _, dk) in sorted(daily.items())
                        if m == metric and dk == owner and d - timedelta(days=w) <= dd < d]
                if len(vals) >= policy.min_days:
                    med = statistics.median(vals)
                    out[(d, metric, w)] = (med, statistics.median(abs(v - med) for v in vals), len(vals))
    return out


def dump(conn) -> dict:
    return {
        "samples": db.fetchall(conn, "SELECT sample_id, metric, value, start_utc, start_ts, end_ts, device_key, quality FROM samples ORDER BY 1"),
        "daily": db.fetchall(conn, "SELECT date, metric, value, unit, device_key, n_samples, confidence, grade, corroboration FROM daily_values ORDER BY 1, 2"),
        "baselines": db.fetchall(conn, "SELECT date, metric, window_days, median, mad, n_days FROM baselines ORDER BY 1, 2, 3"),
        "signals": db.fetchall(conn, "SELECT date, metric, state, value, baseline_median, baseline_mad, delta_pct, device_key, context_flags, why FROM signals ORDER BY 1, 2"),
        "tombstones": db.fetchall(conn, "SELECT tomb_id, hk_uuid, reason FROM tombstones ORDER BY 1"),
    }


def run_incremental(order: list[str]):
    conn, policy, reg = _env()
    b = batches()
    for name in order:
        ingest_batch(conn, b[name], policy, reg)
        rc.drain_journal(conn, policy, reg, today=TODAY)
    return conn, policy, reg


def _daily_tuples(conn):
    return {(d, m): (v, u, dk) for d, m, v, u, dk in db.fetchall(conn, "SELECT date, metric, value, unit, device_key FROM daily_values")}


def test_every_delivery_order_reaches_the_same_state_and_that_state_is_the_oracle():
    dumps = {}
    for name, order in ORDERS.items():
        conn, policy, reg = run_incremental(order)
        dumps[name] = dump(conn)
        assert _daily_tuples(conn) == oracle_daily(), name
        assert db.fetchall(conn, "SELECT COUNT(*) FROM samples WHERE hk_uuid IN ('rh-5', 'aw-7-1')")[0][0] == 0, name
        assert dumps[name]["tombstones"] == [("hk:aw-7-1", "aw-7-1", "bridge_deleted"), ("hk:rh-5", "rh-5", "bridge_deleted")], name
        got = {(d, m, w): (med, mad, n) for d, m, w, med, mad, n in dumps[name]["baselines"]}
        assert got == pytest.approx(oracle_baselines(policy)), name
        # a signal exists exactly where a canonical value exists, inside the derived window
        have = {(d, m) for d, m, *_ in dumps[name]["signals"]}
        assert have == set(oracle_daily()), name
        assert (D0 + timedelta(days=5), "resting_hr") not in have
        # Apple's resting HR stands in for Whoop's (owner decision 4h, fix program D11): labelled, never judged
        assert {s for _, m, s, *_ in dumps[name]["signals"] if m == "resting_hr"} == {"fallback"}
        assert {s for _, m, s, *_ in dumps[name]["signals"] if m != "resting_hr"} <= {"favorable", "neutral", "flag", "insufficient"}
    assert dumps["reversed"] == dumps["natural"] and dumps["interleaved"] == dumps["natural"]


def test_full_rebuild_equals_incremental_row_by_row():
    conn_i, _, _ = run_incremental(NATURAL)
    conn, policy, reg = _env()
    b = batches()
    for name in ORDERS["interleaved"]:
        ingest_batch(conn, b[name], policy, reg)                       # no drain: the journal just accumulates
    out = rc.recompute_window(conn, policy, reg, days=(TODAY - D0).days, value_window=(TODAY - D0).days, now=NOW)
    assert out["daily_values"] == len(oracle_daily())
    rebuilt, inc = dump(conn), dump(conn_i)
    for table in ("samples", "daily", "baselines", "signals", "tombstones"):
        assert rebuilt[table] == inc[table], table
    assert _daily_tuples(conn) == oracle_daily()


def test_replaying_everything_changes_nothing_and_journals_nothing():
    conn, policy, reg = run_incremental(NATURAL)
    before = dump(conn)
    b = batches()
    for name in NATURAL:
        res = ingest_batch(conn, b[name], policy, reg)
        assert res["accepted"] == 0 and res["affected_dates"] == [], name
    assert rc.drain_journal(conn, policy, reg, today=TODAY) is None     # nothing was journaled
    assert dump(conn) == before


# ---- the API, as the PWA and the MCP server see it ----

TOKEN = "test-token-0123456789"
H = {"X-Helios-Token": TOKEN}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    async def _idle(app):
        await asyncio.sleep(0)
    # The periodic loops would race the test's own drains on one connection.
    monkeypatch.setattr(main, "_recompute_loop", _idle)
    monkeypatch.setattr(main, "_background_loop", _idle)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><head></head><body></body></html>", encoding="utf-8")
    monkeypatch.setenv("HELIOS_WEB_DIST", str(dist))
    settings = Settings(raw={"server": {"ingest_token": TOKEN}, "owner": {"timezone": "Asia/Dubai"},
                             "storage": {"db_path": str(tmp_path / "helios.duckdb")},
                             "notifications": {"macos_alerts": False}})
    with TestClient(main.create_app(settings)) as c:
        yield c


def _drain(client):
    st = client.app.state
    return rc.drain_journal(st.conn, st.policy, st.registry, today=TODAY)


def test_api_outputs_are_the_oracle_tuples_and_survive_delete_then_replay(client, tmp_path):
    b = batches()
    for name in NATURAL:
        r = client.post("/ingest", json=b[name], headers=H)
        assert r.status_code == 200 and r.json()["ack"] is True, name
    assert client.post("/ingest", json=b["B2"], headers=H).json()["guarded"] == 15   # same batch twice
    _drain(client)
    # /api/metrics: (date, value, unit, device) per day equals the oracle
    series = client.get("/api/metrics/steps?days=4000", headers=H).json()
    got = [(r["date"], r["value"], r["unit"], r["device_key"]) for r in series["series"]]
    want = [(str(d), v, u, dk) for (d, m), (v, u, dk) in sorted(oracle_daily().items()) if m == "steps"]
    assert got == want and series["baselines"]
    assert all(r["corroboration"] == {"iphone": 3000.0 + 10 * i} for i, r in enumerate(series["series"]))
    rhr = client.get("/api/metrics/resting_hr?days=4000", headers=H).json()["series"]
    assert [r["date"] for r in rhr] == [str(D0 + timedelta(days=i)) for i in range(N_DAYS) if i != 5]
    # /api/health and /api/freshness say they read raw rows; the raw count is every stored row
    health = client.get("/api/health").json()
    assert health["raw"] is True and health["samples"] == 12 * 5 + 3 + 1 - 2
    fresh = client.get("/api/freshness", headers=H).json()
    assert fresh["raw"] is True
    per = {(m["metric"], m["device_key"]): m["n"] for m in fresh["metrics"]}
    assert per[("steps", "apple_watch_ultra")] == 36 + 1 - 1 and per[("steps", "iphone")] == 12 and per[("resting_hr", "apple_watch_ultra")] == 11
    # /api/sleep: a Whoop night through the native puller and its stage copy through the Bridge
    st = client.app.state
    night_end = "2026-06-10T02:40:00.000Z"
    whoop.pull(st.conn, FakeClient(tmp_path, sleep=[sleep_rec("s1", "2026-06-09T19:30:00.000Z", night_end)]), st.policy,
               now=datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc))
    _drain(client)
    rep = client.get("/api/sleep?days=200", headers=H).json()
    night = rep["nights"][-1]
    assert (night["date"], night["asleep_h"], night["device"], night["stage_source"]) == ("2026-06-10", 6.33, "whoop", "whoop")
    assert night["stages"] == {"deep_min": 60, "rem_min": 80, "light_min": 240, "awake_min": 20}
    assert night["fell_asleep"] == "23:30" and night["woke"] == "06:40" and night["efficiency_pct"] == 90.0
    # deletion through the API, then the old batch replayed: nothing comes back
    day3 = str(D0 + timedelta(days=3))
    r = client.post("/ingest", json={"batch_id": "api-del", "samples": [], "deleted": ["rh-3"]}, headers=H).json()
    assert r["deleted"] == 1 and r["affected_dates"] == [day3]
    _drain(client)
    assert day3 not in [r["date"] for r in client.get("/api/metrics/resting_hr?days=4000", headers=H).json()["series"]]
    replay = client.post("/ingest", json=b["B2"], headers=H).json()
    assert replay["accepted"] == 0 and replay["guarded"] == 15 and replay["affected_dates"] == []
    assert _drain(client) is None
    assert day3 not in [r["date"] for r in client.get("/api/metrics/resting_hr?days=4000", headers=H).json()["series"]]
    sig = client.get(f"/api/tool/signals?day={day3}", headers=H).json()
    metrics = {s["metric"] for s in sig["signals"]}
    assert sig["date"] == day3 and "steps" in metrics and "resting_hr" not in metrics
    # the sql tool sees the eligibility view
    r = client.post("/api/tool/sql", json={"query": "SELECT COUNT(*) AS n FROM eligible_samples WHERE metric = 'resting_hr'"}, headers=H)
    assert r.status_code == 200 and r.json() == [{"n": 10}]
