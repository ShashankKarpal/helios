"""Wave 2 group D (fix program design, section 3): the full derived rebuild
shared with Phase 1b (signals/rebuild.py) and the rebuild tool
(server/tools/rebuild_derived.py), on tiny synthetic stores. Every expected
value is worked out by hand. Synthetic data only."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from heliosd.signals import baselines as bl
from heliosd.signals.rebuild import rebuild_all
from heliosd.store import db
from heliosd.trust.registry import SourceRegistry
from tests.test_wave2_export import DAY, _add, _policy, _utc

TOOLS = Path(__file__).resolve().parents[1] / "tools"
TODAY = DAY + timedelta(days=1)            # 2026-05-04
FIRST = DAY - timedelta(days=9)            # 2026-04-24, the oldest synthetic row


def _tool(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tiny_store(path: Path, policy=None) -> None:
    """Ten days of watch steps (1000 + 100 * i on day i), heart rate on the
    last two days, an export twin of a meal on DAY, and a Phase 1b ambiguous
    BMI export row beside its Bridge row."""
    conn = db.connect(path)
    (policy or _policy()).sync_registry(conn)
    for i in range(10):
        d = FIRST + timedelta(days=i)
        _add(conn, f"hk:st-{i}", "steps", "apple_watch_ultra", "bridge", _utc("06:00", d), 1000.0 + 100 * i, "count",
             end=_utc("07:00", d))
    for d in (DAY - timedelta(days=1), DAY):
        for j, v in enumerate((60.0, 64.0)):
            _add(conn, f"hk:hr-{d.day}-{j}", "heart_rate", "apple_watch_ultra", "bridge", _utc(f"0{4 + j}:00", d), v, "count/min")
    _add(conn, "hk:d-1", "dietary_energy", "myfitnesspal", "bridge", _utc("08:00"), 650.255, "kcal")
    _add(conn, "xp:d-1", "dietary_energy", "myfitnesspal", "health_export", _utc("08:00"), 650.26, "kcal")
    _add(conn, "hk:b-1", "bmi", "zepp_life_scale", "bridge", _utc("03:00"), 31.2, "count")
    _add(conn, "xp:b-1", "bmi", "zepp_life_scale", "health_export", _utc("03:00"), 31.2, "count", quality="export_ambiguous")
    conn.close()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _derived(path: Path) -> dict:
    c = duckdb.connect(str(path), read_only=True)
    try:
        return {"daily_values": c.execute("SELECT date, metric, value, device_key, n_samples, confidence, grade, corroboration, "
                                          "detail FROM daily_values ORDER BY 1, 2").fetchall(),
                "baselines": c.execute("SELECT * FROM baselines ORDER BY 1, 2, 3").fetchall(),
                "signals": c.execute("SELECT * FROM signals ORDER BY 1, 2").fetchall(),
                "quality": c.execute("SELECT sample_id, quality FROM samples ORDER BY 1").fetchall(),
                "aliases": c.execute("SELECT old_id, new_id, reason FROM sample_aliases ORDER BY 1, 2").fetchall()}
    finally:
        c.close()


# ---------------------------------------------------------------- rebuild_all (extracted from Phase 1b)

def test_rebuild_all_clears_every_derived_table_and_keeps_the_owners_actions(tmp_path, monkeypatch):
    monkeypatch.delattr(bl, "compute_baselines_range", raising=False)     # the per-date path (group C adds the range form)
    path = tmp_path / "s.duckdb"
    _tiny_store(path)
    conn = db.connect(path)
    policy = _policy()
    policy.sync_registry(conn)
    # Leftovers a rebuild must not keep: a day with no input, a device baseline, a narrative, a suggestion.
    db.execute(conn, "INSERT INTO daily_values (date, metric, value, unit, device_key) VALUES (?, 'steps', 5, 'count', 'iphone')",
               [FIRST - timedelta(days=30)])
    db.execute(conn, "INSERT INTO device_baselines VALUES (?, 'steps', 30, 'iphone', 1.0, 0.0, 9)", [DAY])
    db.execute(conn, "INSERT INTO narratives (date, narrative) VALUES (?, 'synthetic')", [DAY])
    db.execute(conn, "INSERT INTO derived_generation (date, generation) VALUES (?, 3)", [DAY])
    db.execute(conn, "INSERT INTO actions (action_id, date, text, status) VALUES ('a-1', ?, 'walk', 'suggested'), "
                     "('a-2', ?, 'sleep', 'adopted')", [DAY, DAY])
    out = rebuild_all(conn, policy, TODAY, registry=SourceRegistry())
    assert out["range"] == [FIRST, TODAY] and out["baselines_path"] == "per_date"
    steps = dict(db.fetchall(conn, "SELECT date, value FROM daily_values WHERE metric = 'steps'"))
    assert steps == {FIRST + timedelta(days=i): 1000.0 + 100 * i for i in range(10)}
    for table in ("device_baselines", "narratives", "derived_generation"):
        assert db.fetchall(conn, f"SELECT COUNT(*) FROM {table}")[0][0] == 0, table
    assert db.fetchall(conn, "SELECT action_id FROM actions") == [("a-2",)]
    # A steps baseline needs 7 earlier days: the first is on day 8 (median of 1000..1600).
    assert db.fetchall(conn, "SELECT date, median, n_days FROM baselines WHERE metric = 'steps' AND window_days = 30 "
                             "ORDER BY date LIMIT 1") == [(FIRST + timedelta(days=7), 1300.0, 7)]
    assert out["derived_after"]["daily_values"] == db.fetchall(conn, "SELECT COUNT(*) FROM daily_values")[0][0]
    conn.close()


def test_rebuild_all_uses_the_range_baselines_when_they_exist(tmp_path, monkeypatch):
    path = tmp_path / "s.duckdb"
    _tiny_store(path)
    conn = db.connect(path)
    policy = _policy()
    calls = []
    monkeypatch.setattr(bl, "compute_baselines_range", lambda c, p, start, end: calls.append((start, end)) or 42,
                        raising=False)
    monkeypatch.setattr(bl, "compute_baselines", lambda *a, **k: pytest.fail("the per-date path ran"))
    out = rebuild_all(conn, policy, TODAY, registry=SourceRegistry())
    assert calls == [(FIRST, TODAY)] and out["baselines"] == 42 and out["baselines_path"] == "range"
    assert out["signals"] > 0
    conn.close()


# ---------------------------------------------------------------- the rebuild tool

def test_rebuild_tool_refuses_the_live_path(tmp_path, monkeypatch):
    tool = _tool("rebuild_derived")
    live = tmp_path / "home" / "data" / "helios.duckdb"
    live.parent.mkdir(parents=True)
    _tiny_store(live)
    monkeypatch.setattr(tool, "LIVE_STORE", live)
    before = _sha(live)
    out = tmp_path / "out"
    assert tool.main([str(live), "--out", str(out), "--today", str(TODAY)]) == 2
    link = tmp_path / "alias.duckdb"
    os.symlink(live, link)
    assert tool.main([str(link), "--out", str(out), "--today", str(TODAY)]) == 2
    monkeypatch.setattr(tool, "holders", lambda p: [4242])               # the daemon still holds it
    assert tool.main([str(live), "--out", str(out), "--today", str(TODAY), "--apply"]) == 2
    assert _sha(live) == before and not out.exists()
    monkeypatch.setattr(tool, "holders", lambda p: [])                   # stopped: --apply may run
    assert tool.main([str(live), "--out", str(out), "--today", str(TODAY), "--apply"]) == 0
    assert json.loads((out / "summary.json").read_text())["apply"] is True


def test_holders_reads_lsof(tmp_path):
    if not (shutil.which("lsof") or os.path.exists("/usr/sbin/lsof")):
        pytest.skip("no lsof on this machine")
    tool = _tool("rebuild_derived")
    path = tmp_path / "held.duckdb"
    conn = duckdb.connect(str(path))
    try:
        assert os.getpid() in tool.holders(path)
    finally:
        conn.close()
    assert tool.holders(path) == []


def test_rebuild_tool_runs_the_wave2_migrations_and_is_idempotent(tmp_path, monkeypatch):
    tool = _tool("rebuild_derived")
    monkeypatch.delattr(tool.whoop, "rederive_all", raising=False)        # its own test below (group B adds it)
    path = tmp_path / "copy.duckdb"
    _tiny_store(path)
    policy = _policy()
    S1 = tool.run(path, TODAY, tmp_path / "out1", wave2_migrations=True, policy=policy, log=lambda m: None)
    assert S1["stopped"] is None
    assert S1["steps"] == ["init_schema", "sync_registry", "registry_check", "export_relink", "d5_rows", "rebuild_all", "checkpoint"]
    assert S1["results"]["export_relink"]["counts"]["linked"] == 1
    assert [r["sample_id"] for r in S1["results"]["d5_rows"]["rows"]] == ["xp:b-1"]
    assert {m["name"]: m["phase"] for m in S1["migrations"]} == {"wave2_d5_scale_rows": "verified",
                                                                 "wave2_export_relink_v1": "verified"}
    assert S1["counts"]["daily_values"] == S1["results"]["rebuild_all"]["derived_after"]["daily_values"] > 0
    assert S1["eligible_rows_outside_the_rebuilt_range"] == 0 and S1["peak_rss_mb"] > 0
    assert S1["unresolved_exports"] == [{"metric": "bmi", "device_key": "zepp_life_scale", "export_ambiguous": 1,
                                         "export_unmatched": 0}]
    assert json.loads((tmp_path / "out1" / "summary.json").read_text())["counts"] == S1["counts"]
    first = _derived(path)
    meal = [r for r in first["daily_values"] if r[1] == "dietary_energy"]
    assert [(r[0], r[2]) for r in meal] == [(DAY, 650.255)]                # the export twin no longer counts
    S2 = tool.run(path, TODAY, tmp_path / "out2", wave2_migrations=True, policy=policy, log=lambda m: None)
    assert S2["stopped"] is None and S2["results"]["export_relink"] == {
        "migration": "wave2_export_relink_v1", "already_applied": True, "linked": 0, "would_link": 0}
    assert S2["results"]["d5_rows"]["already_applied"] is True
    assert _derived(path) == first
    c = db.connect(path)                                                  # the daemon's own startup check passes
    assert db.unverified_migrations(c) == []
    c.close()


def test_rebuild_tool_stops_on_a_registry_problem_and_rebuilds_nothing(tmp_path, monkeypatch):
    tool = _tool("rebuild_derived")
    path = tmp_path / "copy.duckdb"
    _tiny_store(path)
    monkeypatch.setattr(tool.schema, "validate_policy_against_registry",
                        lambda policy, registry: ["metrics.steps.priority: 'whooop' is not a device of the registry"],
                        raising=False)
    S = tool.run(path, TODAY, tmp_path / "out", wave2_migrations=True, policy=_policy(), log=lambda m: None)
    assert "does not match the source registry" in S["stopped"] and "whooop" in S["stopped"]
    assert "export_relink" not in S["steps"] and "rebuild_all" not in S["steps"]
    assert S["counts"]["daily_values"] == 0 and S["migrations"] == []


def test_rebuild_tool_rederives_whoop_records_when_the_function_exists(tmp_path, monkeypatch):
    tool = _tool("rebuild_derived")
    path = tmp_path / "copy.duckdb"
    _tiny_store(path)
    seen = []

    def rederive_all(conn, policy):
        seen.append(policy)
        return {"records": 0, "samples": 0}
    monkeypatch.setattr(tool.whoop, "rederive_all", rederive_all, raising=False)
    policy = _policy()
    S = tool.run(path, TODAY, tmp_path / "out", wave2_migrations=True, policy=policy, log=lambda m: None)
    assert seen == [policy] and S["steps"][3:6] == ["export_relink", "d5_rows", "whoop_rederive"]
    assert S["results"]["whoop_rederive"] == {"ran": True, "result": {"records": 0, "samples": 0}}
    assert {m["name"]: m["phase"] for m in S["migrations"]}["wave2_whoop_rederive_v1"] == "verified"

