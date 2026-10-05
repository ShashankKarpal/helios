"""Phase 1b: migrate history (single-source plan v2; design.md sections 3, 5,
6, 10 and adjudication-A). The library behind server/tools/rebase_history.py.

What it does, on a store file with the daemon STOPPED (the same code on a
scratch copy and on the live file):

1. Preconditions and an input fingerprint; refuses when the migrations row
   exists (a second run is never a no-op by accident, it is refused).
2. Era assignment by the frozen table (half-open bounds at full precision),
   store-clock ingested_at; export rows are era 2 by rule. A Bridge row in a
   gap stops the migration.
3. Legacy Bridge rows: one row per UUID. A twin pair must rebase to the same
   instant with every other column equal (NULL-safe); any conflict, any uuid
   with more than two rows, any legacy plus native collision stops it. The
   survivor is re-keyed hk:<uuid>; both old ids become aliases.
4. The re-read (hk_reread) is compared uuid by uuid with the rebased instants:
   classes equal, explained (a known normalization change), instant differs,
   content differs, ambiguous (variants exist), identity differs. The re-read
   wins on the apply; unexplained classes stop it unless the operator passes
   the explicit acceptance flag, which the migrations row records.
5. Export rows: one-to-one link to a Bridge row on (metric, source, instants,
   text) within a frozen per-metric value tolerance; linked rows are marked
   export_duplicate (lineage kept as an alias), ambiguous ones export_ambiguous,
   unmatched rows stay eligible (owner decision 2026-10-05, 4c.1).
6. Whoop day-keyed rows: replaced by the native record of the same metric and
   projection day, superseded (tombstone) by a definitive record without that
   value, or quarantined legacy_whoop_unresolved. Zero residual asserted.
7. Staging: the final samples table is built ONCE (PK only, no secondary
   index) from the staged inputs; every gate runs on the staged table; the
   lineage archive is written (parquet, checksummed) BEFORE the cutover.
8. Cutover: ONE transaction (drop view, drop samples, rename the staging
   table, aliases, tombstones, consumed landing rows, migrations row), COMMIT,
   CHECKPOINT, close, reopen through the daemon's path twice, verify.
9. Full derived rebuild offline (clear, every date), diff against the
   snapshot taken before the cutover, every difference classified, a sample
   of cells checked by an independent oracle.

Aggregates only in the report. Never prints a value row. The owner's secrets
are never read (the policy comes through the normal loader).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

from heliosd.ingest import whoop as wh
from heliosd.ingest.normalize import UNIT_ALIASES, reporting_today
from heliosd.signals.baselines import compute_baselines, compute_daily_values
from heliosd.signals.markers import compute_signals
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

MIGRATION = "phase1b_history_rebase_v1"
ZONE = "Asia/Dubai"
# Frozen era table (docs/briefs/era-calibration.md); half-open at full
# timestamp precision (adjudication-A point 1). Store-clock ingest instants.
ERA_BOUNDS = {"era1_end": "2026-07-21 20:42:00", "era2_start": "2026-07-22 04:41:00",
              "era2_end": "2026-09-11 09:48:00", "era4_start": "2026-09-17 11:26:00"}
ERA_OFFSET_MIN = {1: 0, 2: 330, 4: 240}       # stored wall minus offset = UTC instant
EXPORT_ERA = 2
REPORTING_OFFSET_MIN = 240                     # asserted over the population; the zone rendering is authoritative
TS_REBASE, TS_REREAD, TS_EXPORT_LINKED = "era_rebase_v1", "bridge_reread_v1", "export_linked_v1"
Q_EXPORT_DUP, Q_EXPORT_AMBIG, Q_WHOOP_UNRESOLVED = "export_duplicate", "export_ambiguous", "legacy_whoop_unresolved"
ALIAS_REBASE, ALIAS_TWIN, ALIAS_EXPORT = "history_rebase_v1", "twin_collapse_v1", "export_link_v1"
MIGRATION_ALIAS_REASONS = (ALIAS_REBASE, ALIAS_TWIN, ALIAS_EXPORT)
FRAC_METRICS = ("body_fat_pct", "spo2")
# Frozen per-metric value tolerance for the export link, (absolute, relative):
# exact for counts and stages, 0.001 kcal for energies, 0.01 degC for
# temperatures, 0.1 percent for masses and BMI; everything else 0.0001, the
# export's own precision (it prints at most 4 decimals; measured 2026-10-05).
EXPORT_TOLERANCE = {"steps": (0.0, 0.0), "sleep_analysis": (0.0, 0.0),
                    "active_energy": (0.001, 0.0), "basal_energy": (0.001, 0.0), "dietary_energy": (0.001, 0.0),
                    "body_temp": (0.01, 0.0), "wrist_temp": (0.01, 0.0),
                    "body_mass": (0.0, 0.001), "lean_mass": (0.0, 0.001), "bmi": (0.0, 0.001)}
DEFAULT_TOLERANCE = (0.0001, 0.0)
LAST_METRICS = ("body_mass", "bmi", "body_fat_pct", "lean_mass", "vo2max", "resting_hr")
# Columns the rebase itself rewrites on a legacy row; everything else must be byte-identical.
REBASE_WHITELIST = ("sample_id", "start_ts", "end_ts", "start_utc", "end_utc", "time_source", "rebase_era")
# What the re-read overwrites on a survivor (the re-read wins), besides the instants.
REREAD_FIELDS = ("value", "unit", "text_value", "quality", "unit_rule")
LINEAGE_TABLES = ("_lineage_rebased", "_lineage_twins_dropped", "_lineage_compare", "_lineage_export_links",
                  "_lineage_whoop", "_lineage_landing_consumed", "_lineage_aliases", "_lineage_tombstones")
DERIVED = ("daily_values", "baselines", "signals")
UNEXPLAINED = ("instant_differs", "content_differs", "ambiguous", "identity_differs")


class Stop(RuntimeError):
    """A gate failed or a precondition is unmet: nothing was written."""


def era_sql(path_col: str = "sync_path", ts_col: str = "ingested_at") -> str:
    b = ERA_BOUNDS
    return (f"CASE WHEN {path_col} = 'health_export' THEN {EXPORT_ERA} "
            f"WHEN {ts_col} < TIMESTAMP '{b['era1_end']}' THEN 1 "
            f"WHEN {ts_col} >= TIMESTAMP '{b['era2_start']}' AND {ts_col} < TIMESTAMP '{b['era2_end']}' THEN 2 "
            f"WHEN {ts_col} >= TIMESTAMP '{b['era4_start']}' THEN 4 ELSE 0 END")


OFFSET_SQL = "CASE era " + " ".join(f"WHEN {e} THEN {o}" for e, o in ERA_OFFSET_MIN.items()) + " END"


def wall_sql(utc_expr: str, zone: str) -> str:
    """The instant rendered as reporting-zone wall time (zone rendering is authoritative)."""
    return f"timezone('{zone}', timezone('UTC', {utc_expr}))"


def tolerance_sql(xv: str = "x.value", bv: str = "b.value") -> str:
    """abs(xv - bv) <= greatest(abs tolerance, rel tolerance * abs(bv)), per metric."""
    abs_case = " ".join(f"WHEN '{m}' THEN {a}" for m, (a, _r) in EXPORT_TOLERANCE.items())
    rel_case = " ".join(f"WHEN '{m}' THEN {r}" for m, (_a, r) in EXPORT_TOLERANCE.items())
    return (f"abs(COALESCE({xv}, 0) - COALESCE({bv}, 0)) <= GREATEST("
            f"CASE x.metric {abs_case} ELSE {DEFAULT_TOLERANCE[0]} END, "
            f"CASE x.metric {rel_case} ELSE {DEFAULT_TOLERANCE[1]} END * abs(COALESCE({bv}, 0)))")


def _unit_alias_case(col: str) -> str:
    """Canonical unit spelling (normalize.UNIT_ALIASES) for a column, in SQL."""
    whens = " ".join(f"WHEN {k!r} THEN {v!r}" for k, v in UNIT_ALIASES.items())
    return f"CASE {col} {whens} ELSE {col} END"


def _code_commit(start: Path) -> str | None:
    """The git HEAD of the checkout that holds this code, read from .git without
    a subprocess (the tool may run where git is not on PATH)."""
    d = start.resolve()
    for _ in range(8):
        g = d / ".git"
        if g.is_file():                       # a worktree: "gitdir: <path>"
            d2 = Path(g.read_text().split(":", 1)[1].strip())
            head = (d2 / "HEAD").read_text().strip()
            common = (d2 / "commondir")
            root = (d2 / common.read_text().strip()).resolve() if common.exists() else d2
        elif g.is_dir():
            head = (g / "HEAD").read_text().strip()
            root = g
        else:
            d = d.parent
            continue
        if head.startswith("ref: "):
            ref = root / head[5:]
            if ref.exists():
                return ref.read_text().strip()
            packed = root / "packed-refs"
            if packed.exists():
                for line in packed.read_text().splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == head[5:]:
                        return parts[0]
            return None
        return head
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 24), b""):
            h.update(chunk)
    return h.hexdigest()


class Migration:
    """One run over one store file. Call run(); read .R (json-able) afterwards."""

    def __init__(self, path: str | Path, policy: MetricPolicy, registry: SourceRegistry, out_dir: str | Path,
                 archive_dirs: list[str | Path] | None = None, cutover: bool = False, rebuild: bool = False,
                 accept_reread_mismatches: bool = False, apple_health: str | Path | None = None,
                 code_commit: str | None = None, today: date | None = None, label: str = "dryrun",
                 log=None, oracle_cells: int = 200, baseline_rebuild: bool = False):
        self.path = Path(path)
        self.policy, self.registry = policy, registry
        self.zone = policy.reporting_timezone
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.archive_dirs = [Path(a) for a in (archive_dirs or [])] or [self.out / "lineage"]
        self.do_cutover, self.do_rebuild = cutover, rebuild and cutover
        self.accept = accept_reread_mismatches
        self.ah = Path(apple_health) if apple_health else None
        self.code_commit = code_commit or _code_commit(Path(__file__).parent)
        self.today = today or reporting_today(policy.zone)
        self.label = label
        self._log = log or (lambda m: None)
        self.oracle_cells = oracle_cells
        # A rehearsal on a capture whose derived tables an OLDER code wrote
        # (the 07:01 capture predates the Phase 1a deploy) first rebuilds them
        # with the current code on the un-migrated input, so the step-6 diff
        # measures the migration alone and not the older code's semantics.
        self.baseline_rebuild = baseline_rebuild
        self.R: dict = {"label": label, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "path": str(self.path),
                        "constants": {"zone": self.zone, "era_bounds": ERA_BOUNDS, "era_offset_min": ERA_OFFSET_MIN,
                                      "export_tolerance": EXPORT_TOLERANCE, "default_tolerance": DEFAULT_TOLERANCE,
                                      "reporting_offset_min": REPORTING_OFFSET_MIN},
                        "flags": {"cutover": self.do_cutover, "rebuild": self.do_rebuild,
                                  "accept_reread_mismatches": self.accept, "baseline_rebuild": baseline_rebuild},
                        "code_commit": self.code_commit, "steps": {}, "checks": {}, "facts": {}, "stopped": None}
        self.con: duckdb.DuckDBPyConnection | None = None
        self.COLS: list[str] = []

    # ---- plumbing ----
    def log(self, m: str) -> None:
        self._log(f"{time.strftime('%H:%M:%S')} {m}")

    def check(self, name: str, ok, detail=None, fatal: bool = True) -> bool:
        ok = bool(ok)
        self.R["checks"][name] = {"ok": ok, "detail": detail}
        self.log(f"check {name}: {'PASS' if ok else 'FAIL'} {json.dumps(detail, default=str)[:300]}")
        if not ok and fatal:
            raise Stop(f"gate failed: {name}")
        return ok

    def step(self, name: str):
        m = self

        class S:
            def __enter__(self):
                self.t = time.time()
                m.log(f"step {name} ...")

            def __exit__(self, *a):
                m.R["steps"][name] = round(time.time() - self.t, 2)
                m.log(f"step {name} done in {m.R['steps'][name]}s")
        return S()

    def one(self, sql: str, params=None):
        row = self.con.execute(sql, params or []).fetchone()
        return row[0] if row else None

    def rows(self, sql: str, params=None) -> list[list]:
        return [list(r) for r in self.con.execute(sql, params or []).fetchall()]

    def dicts(self, sql: str, params=None) -> list[dict]:
        cur = self.con.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    @staticmethod
    def rss_mb() -> float:
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024), 1)

    def open(self) -> None:
        # A .wal beside the file means the last writer did not close cleanly;
        # DuckDB replays it on open. Recorded before we open (our own DDL
        # writes a WAL entry), not fatal: a writer that is still running would
        # refuse us the file lock anyway.
        self.check("no_wal_beside_the_file_before_open", not self.path.with_name(self.path.name + ".wal").exists(), None, fatal=False)
        # A raw connection (external access stays on: the lineage archive is
        # written with COPY TO parquet); the daemon's DDL runs so the v3 shape
        # is present, exactly as a start of the daemon would leave it.
        self.con = duckdb.connect(str(self.path))
        db.init_schema(self.con)
        self.policy.sync_registry(self.con)

    def fingerprint(self) -> str:
        cols = ", ".join(self.COLS)
        n, h = self.con.execute(f"SELECT COUNT(*), CAST(bit_xor(hash({cols})) AS VARCHAR) FROM samples").fetchone()
        parts = [f"samples:{n}:{h}"]
        for t in ("hk_reread", "hk_reread_variants", "tombstones", "whoop_records", "sample_aliases"):
            cols_t = ", ".join(r[0] for r in self.con.execute(f"DESCRIBE {t}").fetchall())
            n, h = self.con.execute(f"SELECT COUNT(*), CAST(bit_xor(hash({cols_t})) AS VARCHAR) FROM {t}").fetchone()
            parts.append(f"{t}:{n}:{h}")
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def policy_digest(self) -> str:
        return hashlib.sha256(json.dumps({"metrics": self.policy.metrics, "zone": self.zone}, sort_keys=True,
                                         default=str).encode()).hexdigest()

    # ---- 1. preconditions ----
    def preconditions(self) -> None:
        f = self.R["facts"]
        f["duckdb"] = duckdb.__version__
        f["tzdata"] = (open("/usr/share/zoneinfo/+VERSION").read().strip()
                       if os.path.exists("/usr/share/zoneinfo/+VERSION") else None)
        self.check("zone_is_the_frozen_reporting_zone", self.zone == ZONE, self.zone)
        self.check("migration_not_applied_yet", not db.migration_applied(self.con, MIGRATION))
        self.COLS = [r[0] for r in self.con.execute("DESCRIBE samples").fetchall()]
        f["samples_columns"] = len(self.COLS)
        f["samples_before"] = self.one("SELECT COUNT(*) FROM samples")
        f["by_path_time_source_before"] = self.rows(
            "SELECT sync_path, COALESCE(time_source, 'NULL'), COUNT(*) FROM samples GROUP BY 1, 2 ORDER BY 1, 2")
        f["legacy_bridge_rows"] = self.one("SELECT COUNT(*) FROM samples WHERE time_source IS NULL AND sync_path = 'bridge'")
        f["legacy_export_rows"] = self.one("SELECT COUNT(*) FROM samples WHERE time_source IS NULL AND sync_path = 'health_export'")
        f["legacy_whoop_day_rows"] = self.one("SELECT COUNT(*) FROM samples WHERE time_source IS NULL AND sync_path = 'whoop_live'")
        f["legacy_other_rows"] = self.one("SELECT COUNT(*) FROM samples WHERE time_source IS NULL AND sync_path NOT IN ('bridge', 'health_export', 'whoop_live')")
        f["native_rows"] = self.one("SELECT COUNT(*) FROM samples WHERE time_source IS NOT NULL")
        f["hk_reread_rows"] = self.one("SELECT COUNT(*) FROM hk_reread")
        f["hk_reread_variants"] = self.one("SELECT COUNT(*) FROM hk_reread_variants")
        f["tombstones"] = self.one("SELECT COUNT(*) FROM tombstones")
        f["whoop_records"] = self.one("SELECT COUNT(*) FROM whoop_records")
        self.check("no_legacy_row_outside_the_three_known_paths", f["legacy_other_rows"] == 0, f["legacy_other_rows"])
        nonnull = self.one("""SELECT COUNT(*) FROM samples WHERE time_source IS NULL AND (start_utc IS NOT NULL OR end_utc IS NOT NULL
            OR src_offset_min IS NOT NULL OR content_hash IS NOT NULL OR unit_rule IS NOT NULL OR score_state IS NOT NULL
            OR quality IS NOT NULL OR batch_id IS NOT NULL OR sync_identifier IS NOT NULL OR sync_version IS NOT NULL
            OR writer_id IS NOT NULL OR rebase_era IS NOT NULL)""")
        self.check("phase1a_columns_null_on_every_legacy_row", nonnull == 0, nonnull)
        over = self.one("SELECT COUNT(*) FROM (SELECT hk_uuid FROM samples WHERE hk_uuid IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 2)")
        self.check("no_uuid_with_more_than_two_rows", over == 0, over)
        coll = self.one("""SELECT COUNT(*) FROM (SELECT hk_uuid FROM samples WHERE hk_uuid IS NOT NULL GROUP BY 1
                           HAVING bool_or(time_source IS NULL) AND bool_or(time_source IS NOT NULL))""")
        self.check("no_legacy_plus_native_collision_on_one_uuid", coll == 0, coll)
        no_uuid = self.one("SELECT COUNT(*) FROM samples WHERE sync_path = 'bridge' AND hk_uuid IS NULL")
        nulls = self.one("SELECT COUNT(*) FROM samples WHERE start_ts IS NULL OR end_ts IS NULL")
        self.check("no_bridge_row_without_uuid_and_no_null_endpoint", no_uuid == 0 and nulls == 0,
                   {"no_uuid": no_uuid, "null_endpoints": nulls})
        tomb_live = self.one("SELECT COUNT(*) FROM samples s WHERE s.hk_uuid IN (SELECT hk_uuid FROM tombstones WHERE hk_uuid IS NOT NULL)")
        self.check("no_sample_row_carries_a_tombstoned_uuid", tomb_live == 0, tomb_live)
        f["input_fingerprint"] = self.fingerprint()
        f["policy_digest"] = self.policy_digest()
        self.check("zone_rendering_available", self.one(f"SELECT {wall_sql('TIMESTAMP ' + repr('2026-07-21 20:00:00.5'), self.zone)}") is not None)

    # ---- 2. eras ----
    def eras(self) -> None:
        f = self.R["facts"]
        era_counts = self.rows(f"""SELECT sync_path, {era_sql()} AS era, COUNT(*), CAST(MIN(ingested_at) AS VARCHAR), CAST(MAX(ingested_at) AS VARCHAR)
                                   FROM samples WHERE time_source IS NULL GROUP BY 1, 2 ORDER BY 1, 2""")
        f["era_counts"] = era_counts
        gap_bridge = sum(r[2] for r in era_counts if r[0] == "bridge" and r[1] == 0)
        f["gap_rows_by_path"] = {r[0]: r[2] for r in era_counts if r[1] == 0}
        self.check("no_bridge_row_in_an_era_gap", gap_bridge == 0, {"bridge_gap_rows": gap_bridge, "gap_rows_by_path": f["gap_rows_by_path"]})

    # ---- 3. legacy candidates and twins ----
    def materialize(self) -> None:
        base = ", ".join(c for c in self.COLS if c not in ("start_ts", "end_ts", "start_utc", "end_utc", "time_source", "rebase_era"))
        self.base_cols = base
        self.con.execute(f"""CREATE TEMP TABLE lb AS
            SELECT {base}, start_ts AS start_old, end_ts AS end_old, era, {OFFSET_SQL} AS off,
                   start_ts - to_minutes({OFFSET_SQL}) AS su, end_ts - to_minutes({OFFSET_SQL}) AS eu,
                   row_number() OVER (PARTITION BY hk_uuid ORDER BY ingested_at, sample_id) AS rn
            FROM (SELECT *, {era_sql()} AS era FROM samples WHERE time_source IS NULL AND sync_path = 'bridge')""")
        self.con.execute(f"""CREATE TEMP TABLE lx AS
            SELECT {base}, start_ts AS start_old, end_ts AS end_old, era, {OFFSET_SQL} AS off,
                   start_ts - to_minutes({OFFSET_SQL}) AS su, end_ts - to_minutes({OFFSET_SQL}) AS eu
            FROM (SELECT *, {era_sql()} AS era FROM samples WHERE time_source IS NULL AND sync_path = 'health_export')""")
        f = self.R["facts"]
        f["lb_rows"] = self.one("SELECT COUNT(*) FROM lb")
        f["lx_rows"] = self.one("SELECT COUNT(*) FROM lx")
        f["twin_uuids"] = self.one("SELECT COUNT(*) FROM (SELECT hk_uuid FROM lb GROUP BY 1 HAVING COUNT(*) = 2)")

    def twins(self) -> None:
        f = self.R["facts"]
        cmp_cols = [c for c in self.COLS if c not in ("sample_id", "ingested_at", "start_ts", "end_ts", "start_utc", "end_utc", "time_source", "rebase_era")]
        cmp_cols = ["su", "eu", "era"] + cmp_cols
        sums = ", ".join(f"COALESCE(SUM(CASE WHEN a.{c} IS DISTINCT FROM b.{c} THEN 1 ELSE 0 END), 0) AS d_{c}" for c in cmp_cols)
        pair = self.con.execute(f"SELECT COUNT(*), {sums} FROM lb a JOIN lb b ON a.hk_uuid = b.hk_uuid AND a.rn = 1 AND b.rn = 2").fetchone()
        tw = dict(zip(["pairs"] + [f"d_{c}" for c in cmp_cols], pair))
        f["twin_pairs"] = tw
        f["twin_era_pairs"] = self.rows("SELECT a.era, b.era, COUNT(*) FROM lb a JOIN lb b ON a.hk_uuid = b.hk_uuid AND a.rn = 1 AND b.rn = 2 GROUP BY 1, 2 ORDER BY 1, 2")
        f["twin_spread_minutes_before"] = self.rows("""SELECT CAST(round((epoch(b.start_old) - epoch(a.start_old)) / 60.0) AS INTEGER), COUNT(*)
            FROM lb a JOIN lb b ON a.hk_uuid = b.hk_uuid AND a.rn = 1 AND b.rn = 2 GROUP BY 1 ORDER BY 2 DESC""")
        instant = tw["d_su"] + tw["d_eu"]
        content = sum(v for k, v in tw.items() if k.startswith("d_") and k not in ("d_su", "d_eu", "d_era"))
        self.check("twin_pairs_equal_twin_uuids", tw["pairs"] == f["twin_uuids"], {"pairs": tw["pairs"], "twin_uuids": f["twin_uuids"]})
        conflicts = self.rows("""SELECT a.hk_type, a.device_key, a.era, b.era, COUNT(*) FROM lb a JOIN lb b ON a.hk_uuid = b.hk_uuid AND a.rn = 1 AND b.rn = 2
            WHERE a.su IS DISTINCT FROM b.su OR a.eu IS DISTINCT FROM b.eu GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC LIMIT 50""")
        f["twin_conflicts_by_type"] = conflicts
        self.check("twin_collapse_100_percent_by_instant", instant == 0, {"instant_conflicts": instant, "pairs": tw["pairs"], "by_type": conflicts[:10]})
        self.check("twin_content_equal_null_safe_every_column", content == 0, {k: v for k, v in tw.items() if k not in ("pairs", "d_era") and v})
        self.check("twin_pairs_cross_eras_1_2_or_2_4_only", all((a, b) in ((1, 2), (2, 4)) for a, b, _ in f["twin_era_pairs"]), f["twin_era_pairs"])

    # ---- 4. the re-read compare ----
    def reread(self) -> None:
        f = self.R["facts"]
        ua = _unit_alias_case
        self.con.execute(f"""CREATE TEMP TABLE cmp AS
            SELECT s.hk_uuid, s.sample_id AS old_id, s.hk_type, s.source_name, s.device_key, s.sync_path, s.era, s.metric,
                   s.su AS rb_start, s.eu AS rb_end, s.value AS rb_value, s.unit AS rb_unit, s.text_value AS rb_text,
                   s.quality AS rb_quality, s.unit_rule AS rb_unit_rule,
                   h.start_utc AS rr_start, h.end_utc AS rr_end, h.value AS rr_value, h.unit AS rr_unit, h.text_value AS rr_text,
                   h.quality AS rr_quality, h.unit_rule AS rr_unit_rule, h.hk_type AS rr_hk_type, h.source_name AS rr_source,
                   h.device_key AS rr_device, h.metric AS rr_metric, h.n_seen, h.first_batch, h.last_batch,
                   (SELECT COUNT(*) FROM hk_reread_variants v WHERE v.hk_uuid = s.hk_uuid) AS n_variants,
                   CAST(round((epoch(h.start_utc) - epoch(s.su)) / 60.0) AS INTEGER) AS delta_start_min,
                   CAST(round((epoch(h.end_utc) - epoch(s.eu)) / 60.0) AS INTEGER) AS delta_end_min,
                   CASE
                     WHEN (SELECT COUNT(*) FROM hk_reread_variants v WHERE v.hk_uuid = s.hk_uuid) > 0 THEN 'ambiguous'
                     WHEN h.hk_type IS DISTINCT FROM s.hk_type OR h.metric IS DISTINCT FROM s.metric
                          OR h.source_name IS DISTINCT FROM s.source_name OR h.device_key IS DISTINCT FROM s.device_key THEN 'identity_differs'
                     WHEN h.start_utc IS DISTINCT FROM s.su OR h.end_utc IS DISTINCT FROM s.eu THEN 'instant_differs'
                     WHEN h.value IS NOT DISTINCT FROM s.value AND h.unit IS NOT DISTINCT FROM s.unit AND h.text_value IS NOT DISTINCT FROM s.text_value
                          AND h.quality IS NOT DISTINCT FROM s.quality AND h.unit_rule IS NOT DISTINCT FROM s.unit_rule THEN 'equal'
                     WHEN s.metric IN ({', '.join(repr(m) for m in FRAC_METRICS)}) AND s.unit_rule IS NULL AND h.unit_rule = 'frac_to_pct_v1'
                          AND s.value IS NOT NULL AND h.value IS NOT NULL AND abs(h.value - s.value * 100.0) < 1e-6
                          AND h.text_value IS NOT DISTINCT FROM s.text_value AND h.quality IS NULL THEN 'explained_frac_to_pct'
                     WHEN h.value IS NOT DISTINCT FROM s.value AND h.text_value IS NOT DISTINCT FROM s.text_value AND h.unit_rule IS NOT DISTINCT FROM s.unit_rule
                          AND h.unit IS DISTINCT FROM s.unit AND {ua('h.unit')} = {ua('s.unit')} AND h.quality IS NOT DISTINCT FROM s.quality THEN 'explained_unit_alias'
                     WHEN h.value IS NOT DISTINCT FROM s.value AND h.text_value IS NOT DISTINCT FROM s.text_value AND h.unit_rule IS NOT DISTINCT FROM s.unit_rule
                          AND h.unit IS NOT DISTINCT FROM s.unit AND s.quality IS NULL AND h.quality IS NOT NULL THEN 'explained_quality'
                     ELSE 'content_differs' END AS cls
            FROM lb s JOIN hk_reread h ON h.hk_uuid = s.hk_uuid WHERE s.rn = 1 AND s.era IN (1, 2, 4)""")
        f["compare_classes"] = dict((r[0], r[1]) for r in self.rows("SELECT cls, COUNT(*) FROM cmp GROUP BY 1 ORDER BY 1"))
        f["compare_by_type_source_path_era"] = self.rows("SELECT hk_type, device_key, sync_path, era, cls, COUNT(*) FROM cmp GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 3, 4, 5")
        f["compare_delta_histogram"] = self.rows("SELECT hk_type, era, delta_start_min, COUNT(*) FROM cmp WHERE cls = 'instant_differs' GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 100")
        f["compare_uuids_in_both"] = self.one("SELECT COUNT(*) FROM cmp")
        f["reread_rows_tombstoned_ignored"] = self.one("SELECT COUNT(*) FROM hk_reread h WHERE h.hk_uuid IN (SELECT hk_uuid FROM tombstones WHERE hk_uuid IS NOT NULL)")
        f["reread_rows_native_uuid"] = self.one("SELECT COUNT(*) FROM hk_reread h WHERE h.hk_uuid IN (SELECT hk_uuid FROM samples WHERE time_source IS NOT NULL)")
        f["reread_rows_without_any_row"] = self.one("SELECT COUNT(*) FROM hk_reread h WHERE h.hk_uuid NOT IN (SELECT hk_uuid FROM samples WHERE hk_uuid IS NOT NULL) AND h.hk_uuid NOT IN (SELECT hk_uuid FROM tombstones WHERE hk_uuid IS NOT NULL)")
        f["legacy_only_by_type_era"] = self.rows("""SELECT hk_type, device_key, era, COUNT(*) FROM lb WHERE rn = 1 AND era IN (1, 2, 4)
            AND hk_uuid NOT IN (SELECT hk_uuid FROM hk_reread) GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""")
        f["legacy_only_uuids"] = sum(r[3] for r in f["legacy_only_by_type_era"])
        f["new_from_reread_by_type"] = self.rows("SELECT hk_type, strftime(ingested_at, '%Y-%m'), COUNT(*) FROM samples WHERE sample_id LIKE 'hk:%' AND time_source = 'bridge_utc' GROUP BY 1, 2 ORDER BY 1, 2")
        unexplained = sum(v for k, v in f["compare_classes"].items() if k in UNEXPLAINED)
        hard = sum(v for k, v in f["compare_classes"].items() if k in ("ambiguous", "identity_differs"))
        f["compare_unexplained"] = unexplained
        self.check("reread_compare_no_ambiguous_or_identity_differences", hard == 0, {k: v for k, v in f["compare_classes"].items() if k in ("ambiguous", "identity_differs")})
        self.check("reread_compare_zero_unexplained_mismatches", unexplained == 0 or self.accept,
                   {"unexplained": unexplained, "classes": f["compare_classes"], "accepted_by_flag": self.accept and unexplained > 0})
        if self.accept and unexplained:
            self.R["flags"]["accepted_mismatches"] = unexplained
        winning = ["equal", "explained_frac_to_pct", "explained_unit_alias", "explained_quality"]
        if self.accept:
            winning += ["instant_differs", "content_differs"]
        self.winning = winning
        # Survivors with the re-read applied where it wins.
        win = ", ".join(repr(w) for w in winning)
        other = ", ".join(c for c in self.COLS if c not in ("start_ts", "end_ts", "start_utc", "end_utc", "time_source", "rebase_era", "sample_id", *REREAD_FIELDS))
        self.con.execute(f"""CREATE TEMP TABLE lb_final AS
            SELECT {', '.join('s.' + c for c in other.split(', '))},
                   'hk:' || s.hk_uuid AS new_id, s.sample_id AS old_id, s.era, s.start_old, s.end_old,
                   CASE WHEN c.cls IN ({win}) THEN c.rr_start ELSE s.su END AS su,
                   CASE WHEN c.cls IN ({win}) THEN c.rr_end ELSE s.eu END AS eu,
                   CASE WHEN c.cls IN ({win}) THEN c.rr_value ELSE s.value END AS value,
                   CASE WHEN c.cls IN ({win}) THEN c.rr_unit ELSE s.unit END AS unit,
                   CASE WHEN c.cls IN ({win}) THEN c.rr_text ELSE s.text_value END AS text_value,
                   CASE WHEN c.cls IN ({win}) THEN c.rr_quality ELSE s.quality END AS quality,
                   CASE WHEN c.cls IN ({win}) THEN c.rr_unit_rule ELSE s.unit_rule END AS unit_rule,
                   CASE WHEN c.cls IN ({win}) THEN '{TS_REREAD}' ELSE '{TS_REBASE}' END AS time_source,
                   c.cls
            FROM lb s LEFT JOIN cmp c ON c.hk_uuid = s.hk_uuid WHERE s.rn = 1 AND s.era IN (1, 2, 4)""")
        f["survivors_by_time_source"] = dict((r[0], r[1]) for r in self.rows("SELECT time_source, COUNT(*) FROM lb_final GROUP BY 1"))

    # ---- 5. export link ----
    def exports(self) -> None:
        f = self.R["facts"]
        # Every Bridge row that will exist after the migration is a candidate:
        # survivors (re-read applied) and the native hk rows (post-1a, new from the re-read).
        self.con.execute(f"""CREATE TEMP TABLE bridge_final AS
            SELECT new_id AS sample_id, metric, source_name, su, eu, value, text_value FROM lb_final
            UNION ALL
            SELECT sample_id, metric, source_name, start_utc AS su, end_utc AS eu, value, text_value FROM samples
            WHERE sync_path = 'bridge' AND time_source IS NOT NULL""")
        self.con.execute(f"""CREATE TEMP TABLE xcand AS
            SELECT x.sample_id AS x_id, b.sample_id AS b_id, x.metric, x.source_name, abs(COALESCE(x.value, 0) - COALESCE(b.value, 0)) AS delta
            FROM lx x JOIN bridge_final b ON b.metric = x.metric AND b.source_name = x.source_name
                 AND date_trunc('second', b.su) = date_trunc('second', x.su) AND date_trunc('second', b.eu) = date_trunc('second', x.eu)
                 AND b.text_value IS NOT DISTINCT FROM x.text_value
            WHERE {tolerance_sql()}""")
        self.con.execute("""CREATE TEMP TABLE xlink AS
            WITH fwd AS (SELECT x_id, COUNT(*) AS n_fwd, MIN(b_id) AS b_id, MIN(delta) AS delta FROM xcand GROUP BY 1),
                 rev AS (SELECT b_id, COUNT(*) AS n_rev FROM xcand GROUP BY 1)
            SELECT x.sample_id AS x_id, x.metric, x.source_name, x.device_key, fwd.b_id, fwd.delta,
                   CASE WHEN fwd.x_id IS NULL THEN 'unmatched'
                        WHEN fwd.n_fwd = 1 AND rev.n_rev = 1 THEN 'linked'
                        ELSE 'ambiguous' END AS outcome
            FROM lx x LEFT JOIN fwd ON fwd.x_id = x.sample_id LEFT JOIN rev ON rev.b_id = fwd.b_id""")
        f["export_link_by_metric_source"] = self.rows("SELECT metric, device_key, outcome, COUNT(*) FROM xlink GROUP BY 1, 2, 3 ORDER BY 1, 2, 3")
        f["export_link_totals"] = dict((r[0], r[1]) for r in self.rows("SELECT outcome, COUNT(*) FROM xlink GROUP BY 1"))
        # Owner decision 4c.1: unmatched export rows stay eligible. Suspected
        # double count: unmatched rows on (metric, source, Dubai day) cells that
        # also hold Bridge rows of the same source.
        f["export_unmatched_on_days_with_bridge_rows"] = self.rows(f"""
            WITH u AS (SELECT l.metric, l.source_name, l.device_key, CAST({wall_sql('x.su', self.zone)} AS DATE) AS d, COUNT(*) AS n
                       FROM xlink l JOIN lx x ON x.sample_id = l.x_id WHERE l.outcome = 'unmatched' GROUP BY 1, 2, 3, 4),
                 bd AS (SELECT DISTINCT metric, source_name, CAST({wall_sql('su', self.zone)} AS DATE) AS d FROM bridge_final)
            SELECT u.metric, u.device_key, SUM(u.n) AS unmatched_rows_on_overlap_days, COUNT(*) AS overlap_days
            FROM u JOIN bd ON bd.metric = u.metric AND bd.source_name = u.source_name AND bd.d = u.d GROUP BY 1, 2 ORDER BY 1, 2""")
        # Time-key-only preview (value ignored): the |delta| distribution per
        # metric among single time-key candidates, evidence for the tolerance table.
        f["export_timekey_delta_histogram"] = self.rows("""
            WITH tk AS (SELECT x.sample_id AS x_id, x.metric, abs(COALESCE(x.value, 0) - COALESCE(b.value, 0)) AS delta
                        FROM lx x JOIN bridge_final b ON b.metric = x.metric AND b.source_name = x.source_name
                             AND date_trunc('second', b.su) = date_trunc('second', x.su) AND date_trunc('second', b.eu) = date_trunc('second', x.eu)
                             AND b.text_value IS NOT DISTINCT FROM x.text_value
                        QUALIFY COUNT(*) OVER (PARTITION BY x.sample_id) = 1)
            SELECT metric, CASE WHEN delta = 0 THEN '0' WHEN delta <= 0.0001 THEN '<=1e-4' WHEN delta <= 0.01 THEN '<=1e-2'
                                WHEN delta <= 0.1 THEN '<=0.1' WHEN delta <= 1 THEN '<=1' ELSE '>1' END AS bucket, COUNT(*)
            FROM tk GROUP BY 1, 2 ORDER BY 1, 2""")
        both = self.one("SELECT COUNT(*) FROM (SELECT b_id FROM xlink WHERE outcome = 'linked' GROUP BY 1 HAVING COUNT(*) > 1)")
        self.check("export_links_one_to_one_both_ways", both == 0, both)
        other = ", ".join(c for c in self.COLS if c not in ("start_ts", "end_ts", "start_utc", "end_utc", "time_source", "rebase_era", "quality"))
        self.con.execute(f"""CREATE TEMP TABLE lx_final AS
            SELECT {', '.join('x.' + c for c in other.split(', '))}, x.era, x.start_old, x.end_old, x.su, x.eu,
                   CASE WHEN l.outcome = 'linked' THEN '{Q_EXPORT_DUP}' WHEN l.outcome = 'ambiguous' THEN '{Q_EXPORT_AMBIG}' ELSE x.quality END AS quality,
                   CASE WHEN l.outcome = 'linked' THEN '{TS_EXPORT_LINKED}' ELSE '{TS_REBASE}' END AS time_source,
                   l.outcome, l.b_id
            FROM lx x JOIN xlink l ON l.x_id = x.sample_id""")

    # ---- 6. Whoop day rows ----
    def whoop(self) -> None:
        f = self.R["facts"]
        zone = self.policy.zone
        day_rows = self.dicts("""SELECT sample_id, metric, value, start_ts, end_ts, ingested_at FROM samples
                                 WHERE time_source IS NULL AND sync_path = 'whoop_live' ORDER BY sample_id""")
        recs = self.dicts("SELECT record_key, kind, start_utc, end_utc, created_at, score_state, nap FROM whoop_records")
        by_day: dict[tuple[str, date], list[dict]] = {}
        for r in recs:
            if r["nap"]:
                continue
            d = wh.projection_date(r["kind"], r["start_utc"], r["end_utc"], r["created_at"], zone)
            if d is not None:
                by_day.setdefault((r["kind"], d), []).append(r)
        metric_kind = {m: k for k, ms in wh.KIND_METRICS.items() for m in ms}
        present = {r[0]: r[1] for r in self.rows("SELECT sample_id, value FROM samples WHERE sample_id LIKE 'wh:%' AND time_source IS NOT NULL")}
        out = []
        for r in day_rows:
            m = re.match(r"^wh:([a-z_]+):(\d{4}-\d{2}-\d{2})$", r["sample_id"])
            if not m or m.group(1) != r["metric"]:
                out.append([r["sample_id"], r["metric"], None, "quarantined", None, None, r["value"], None])
                continue
            metric, day = m.group(1), date.fromisoformat(m.group(2))
            kind = metric_kind.get(metric)
            cands = by_day.get((kind, day), []) if kind else []
            scored = [c for c in cands if c["score_state"] == "SCORED"]
            target = None
            for c in scored:
                sid = f"wh:{metric}:{c['record_key']}"
                if sid in present:
                    target = (sid, c["record_key"], present[sid])
                    break
            if target:
                out.append([r["sample_id"], metric, day, "replaced", target[0], target[1], r["value"], target[2]])
            elif any(c["score_state"] in ("SCORED", "UNSCORABLE") for c in cands):
                c = next(c for c in cands if c["score_state"] in ("SCORED", "UNSCORABLE"))
                out.append([r["sample_id"], metric, day, "superseded", None, c["record_key"], r["value"], None])
            else:
                out.append([r["sample_id"], metric, day, "quarantined", None, None, r["value"], None])
        self.con.execute("""CREATE TEMP TABLE lw_out (sample_id VARCHAR, metric VARCHAR, day DATE, outcome VARCHAR, target VARCHAR,
                            record_key VARCHAR, value_legacy DOUBLE, value_record DOUBLE)""")
        if out:
            self.con.executemany("INSERT INTO lw_out VALUES (?, ?, ?, ?, ?, ?, ?, ?)", out)
        f["whoop_day_rows_by_outcome"] = self.rows("SELECT metric, outcome, COUNT(*) FROM lw_out GROUP BY 1, 2 ORDER BY 1, 2")
        f["whoop_day_rows_by_era_outcome"] = self.rows(f"""SELECT {era_sql('s.sync_path', 's.ingested_at')} AS era, o.outcome, COUNT(*)
            FROM lw_out o JOIN samples s ON s.sample_id = o.sample_id GROUP BY 1, 2 ORDER BY 1, 2""")
        f["whoop_replaced_value_agreement"] = self.rows("""SELECT metric, COUNT(*), SUM(CASE WHEN abs(value_legacy - value_record) < 0.005 THEN 1 ELSE 0 END)
            FROM lw_out WHERE outcome = 'replaced' GROUP BY 1 ORDER BY 1""")
        f["whoop_day_rows_total"] = len(out)

    # ---- 7. staging ----
    def stage(self) -> None:
        f = self.R["facts"]
        con = self.con
        ddl = self.one("SELECT sql FROM duckdb_tables() WHERE table_name = 'samples'")
        self.check("samples_ddl_reconstructed_with_primary_key", "PRIMARY KEY" in ddl and "rebase_era" in ddl, ddl[:80])
        con.execute(ddl.replace("CREATE TABLE samples", "CREATE TABLE samples_rebased", 1))
        collist = ", ".join(self.COLS)
        zone = self.zone

        def expr(c: str, kind: str) -> str:
            if kind == "bridge":
                if c == "sample_id":
                    return "new_id"
                if c == "rebase_era":
                    return "era"
            if kind == "export":
                if c == "rebase_era":
                    return "era"
            if c == "start_ts":
                return wall_sql("su", zone)
            if c == "end_ts":
                return wall_sql("eu", zone)
            if c == "start_utc":
                return "su"
            if c == "end_utc":
                return "eu"
            return c
        sel_b = ", ".join(expr(c, "bridge") for c in self.COLS)
        sel_x = ", ".join(expr(c, "export") for c in self.COLS)
        con.execute(f"INSERT INTO samples_rebased ({collist}) SELECT {collist} FROM samples WHERE time_source IS NOT NULL")
        n_native = self.one("SELECT COUNT(*) FROM samples_rebased")
        sel_w = ", ".join(f"'{Q_WHOOP_UNRESOLVED}'" if c == "quality" else "s." + c for c in self.COLS)
        con.execute(f"""INSERT INTO samples_rebased ({collist}) SELECT {sel_w} FROM samples s JOIN lw_out o ON o.sample_id = s.sample_id WHERE o.outcome = 'quarantined'""")
        con.execute(f"INSERT INTO samples_rebased ({collist}) SELECT {sel_b} FROM lb_final")
        con.execute(f"INSERT INTO samples_rebased ({collist}) SELECT {sel_x} FROM lx_final")
        # Aliases and tombstones the cutover writes.
        con.execute(f"""CREATE TABLE _lineage_aliases AS
            SELECT sample_id AS old_id, 'hk:' || hk_uuid AS new_id, CASE WHEN rn = 1 THEN '{ALIAS_REBASE}' ELSE '{ALIAS_TWIN}' END AS reason FROM lb WHERE era IN (1, 2, 4)
            UNION ALL SELECT x_id, b_id, '{ALIAS_EXPORT}' FROM xlink WHERE outcome = 'linked'
            UNION ALL SELECT sample_id, target, '{wh.ALIAS_REASON}' FROM lw_out WHERE outcome = 'replaced'""")
        con.execute(f"""CREATE TABLE _lineage_tombstones AS
            SELECT o.sample_id AS tomb_id, CAST(NULL AS VARCHAR) AS hk_uuid, o.metric, s.start_utc, '{wh.SUPERSEDED}' AS reason,
                   'migration:' || o.record_key AS batch_id
            FROM lw_out o JOIN samples s ON s.sample_id = o.sample_id WHERE o.outcome = 'superseded'""")
        # Lineage: what every row was before.
        con.execute(f"""CREATE TABLE _lineage_rebased AS
            SELECT old_id, new_id, hk_uuid, era, start_old, end_old, su AS start_utc, eu AS end_utc, time_source, cls AS compare_class, metric, hk_type, device_key
            FROM lb_final
            UNION ALL
            SELECT sample_id, sample_id, NULL, era, start_old, end_old, su, eu, time_source, outcome, metric, hk_type, device_key FROM lx_final""")
        con.execute("CREATE TABLE _lineage_twins_dropped AS SELECT s.* FROM samples s JOIN lb b ON b.sample_id = s.sample_id WHERE b.rn = 2")
        con.execute("CREATE TABLE _lineage_compare AS SELECT * FROM cmp")
        con.execute("CREATE TABLE _lineage_export_links AS SELECT * FROM xlink WHERE outcome <> 'unmatched'")
        con.execute("CREATE TABLE _lineage_whoop AS SELECT * FROM lw_out")
        con.execute("CREATE TABLE _lineage_landing_consumed AS SELECT h.* FROM hk_reread h WHERE h.hk_uuid IN (SELECT hk_uuid FROM cmp)")
        f["rows_after"] = self.one("SELECT COUNT(*) FROM samples_rebased")
        f["native_rows_copied"] = n_native
        f["aliases_staged"] = dict((r[0], r[1]) for r in self.rows("SELECT reason, COUNT(*) FROM _lineage_aliases GROUP BY 1"))
        f["tombstones_staged"] = self.one("SELECT COUNT(*) FROM _lineage_tombstones")
        f["rss_mb_after_staging"] = self.rss_mb()

    # ---- 8. gates on the staged table ----
    def gates(self) -> None:
        f = self.R["facts"]
        zone = self.zone
        whoop_removed = self.one("SELECT COUNT(*) FROM lw_out WHERE outcome IN ('replaced', 'superseded')")
        expected = f["samples_before"] - f["twin_uuids"] - whoop_removed
        self.check("rows_after_equal_rows_before_minus_twins_minus_removed_whoop_rows", f["rows_after"] == expected,
                   {"before": f["samples_before"], "after": f["rows_after"], "twins": f["twin_uuids"], "whoop_removed": whoop_removed})
        dup_u = self.one("SELECT COUNT(*) FROM (SELECT hk_uuid FROM samples_rebased WHERE hk_uuid IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 1)")
        self.check("no_duplicate_uuid_in_staging", dup_u == 0, dup_u)
        sch = dict((r[0], r[1]) for r in self.rows("SELECT sync_path || '/' || split_part(sample_id, ':', 1), COUNT(*) FROM samples_rebased GROUP BY 1 ORDER BY 1"))
        f["scheme_after"] = sch
        self.check("every_bridge_row_is_hk_keyed", all(not k.startswith("bridge/") or k == "bridge/hk" for k in sch), sch)
        self.check("bridge_hk_ids_match_their_uuid", self.one("SELECT COUNT(*) FROM samples_rebased WHERE sync_path = 'bridge' AND sample_id <> 'hk:' || hk_uuid") == 0)
        n_al = self.one(f"SELECT COUNT(*) FROM _lineage_aliases WHERE reason IN ('{ALIAS_REBASE}', '{ALIAS_TWIN}')")
        self.check("rebase_alias_rows_equal_legacy_bridge_rows", n_al == f["lb_rows"], {"aliases": n_al, "legacy_bridge": f["lb_rows"]})
        self.check("alias_old_ids_unique", self.one("SELECT COUNT(*) - COUNT(DISTINCT old_id) FROM _lineage_aliases") == 0)
        unresolved = self.one("""SELECT COUNT(*) FROM _lineage_aliases a LEFT JOIN samples_rebased t ON t.sample_id = a.new_id
            WHERE t.sample_id IS NULL AND a.new_id NOT IN (SELECT tomb_id FROM tombstones) AND a.new_id NOT IN (SELECT tomb_id FROM _lineage_tombstones)""")
        self.check("every_alias_resolves_to_a_live_row_or_a_tombstone", unresolved == 0, unresolved)
        tomb = self.one("""SELECT COUNT(*) FROM samples_rebased s WHERE s.hk_uuid IN (SELECT hk_uuid FROM tombstones WHERE hk_uuid IS NOT NULL)
            OR s.sample_id IN (SELECT tomb_id FROM _lineage_tombstones)""")
        self.check("deletion_dominates_no_staged_row_is_tombstoned", tomb == 0, tomb)
        residual = self.one("SELECT COUNT(*) FROM samples_rebased WHERE time_source IS NULL AND quality IS NULL")
        self.check("no_legacy_row_left_without_an_explicit_quality", residual == 0, residual)
        wres = self.one("SELECT COUNT(*) FROM samples_rebased WHERE sync_path = 'whoop_live' AND time_source IS NULL AND quality IS DISTINCT FROM ?", [Q_WHOOP_UNRESOLVED])
        self.check("whoop_day_rows_zero_residual", wres == 0, wres)
        # Untouched columns on rows the rebase alone moved (era rule, no re-read, no link).
        untouched = [c for c in self.COLS if c not in REBASE_WHITELIST]
        dist = " OR ".join(f"s.{c} IS DISTINCT FROM t.{c}" for c in untouched)
        bad_b = self.one(f"""SELECT COUNT(*) FROM samples s JOIN _lineage_rebased r ON r.old_id = s.sample_id
            JOIN samples_rebased t ON t.sample_id = r.new_id WHERE r.time_source = '{TS_REBASE}' AND s.sync_path = 'bridge' AND ({dist})""")
        untouched_x = [c for c in untouched if c != "quality"]
        dist_x = " OR ".join(f"s.{c} IS DISTINCT FROM t.{c}" for c in untouched_x)
        bad_x = self.one(f"""SELECT COUNT(*) FROM samples s JOIN samples_rebased t ON t.sample_id = s.sample_id WHERE s.sync_path = 'health_export' AND ({dist_x})""")
        self.check("untouched_columns_identical_outside_whitelist", bad_b == 0 and bad_x == 0, {"bridge": bad_b, "export": bad_x, "whitelist": REBASE_WHITELIST})
        # Re-read rows carry exactly the landing's fields (the re-read wins, nothing blended).
        bad_r = self.one(f"""SELECT COUNT(*) FROM samples_rebased t JOIN hk_reread h ON h.hk_uuid = t.hk_uuid WHERE t.time_source = '{TS_REREAD}'
            AND (t.start_utc IS DISTINCT FROM h.start_utc OR t.end_utc IS DISTINCT FROM h.end_utc OR t.value IS DISTINCT FROM h.value
                 OR t.unit IS DISTINCT FROM h.unit OR t.text_value IS DISTINCT FROM h.text_value OR t.quality IS DISTINCT FROM h.quality
                 OR t.unit_rule IS DISTINCT FROM h.unit_rule)""")
        self.check("reread_rows_equal_the_landing_exactly", bad_r == 0, bad_r)
        # Post-1a rows byte-identical.
        dist_all = " OR ".join(f"s.{c} IS DISTINCT FROM t.{c}" for c in self.COLS)
        bad_n = self.one(f"SELECT COUNT(*) FROM samples s JOIN samples_rebased t ON t.sample_id = s.sample_id WHERE s.time_source IS NOT NULL AND ({dist_all})")
        self.check("native_rows_byte_identical", bad_n == 0, bad_n)
        bad_render = self.one(f"""SELECT COUNT(*) FROM samples_rebased WHERE time_source IN ('{TS_REBASE}', '{TS_REREAD}', '{TS_EXPORT_LINKED}')
            AND (start_ts IS DISTINCT FROM {wall_sql('start_utc', zone)} OR end_ts IS DISTINCT FROM {wall_sql('end_utc', zone)}
                 OR start_ts IS DISTINCT FROM start_utc + INTERVAL {REPORTING_OFFSET_MIN} MINUTE OR start_utc IS NULL OR end_utc IS NULL)""")
        self.check("reporting_wall_equals_zone_rendering_and_utc_plus_240_on_every_migrated_row", bad_render == 0, bad_render)
        bad_off = self.one(f"SELECT COUNT(*) FROM lb WHERE rn = 1 AND era IN (1, 2, 4) AND round((epoch(start_old) - epoch(su)) / 60.0) <> off")
        self.check("rebased_offsets_are_exactly_the_era_offsets", bad_off == 0, bad_off)
        f["day_changes_by_path_era"] = self.rows(f"""SELECT 'bridge' AS p, era, COUNT(*),
              SUM(CASE WHEN CAST(start_old AS DATE) IS DISTINCT FROM CAST({wall_sql('su', zone)} AS DATE) THEN 1 ELSE 0 END),
              SUM(CASE WHEN CAST(end_old AS DATE) IS DISTINCT FROM CAST({wall_sql('eu', zone)} AS DATE) THEN 1 ELSE 0 END) FROM lb_final GROUP BY 1, 2
            UNION ALL SELECT 'export', era, COUNT(*),
              SUM(CASE WHEN CAST(start_old AS DATE) IS DISTINCT FROM CAST({wall_sql('su', zone)} AS DATE) THEN 1 ELSE 0 END),
              SUM(CASE WHEN CAST(end_old AS DATE) IS DISTINCT FROM CAST({wall_sql('eu', zone)} AS DATE) THEN 1 ELSE 0 END) FROM lx_final GROUP BY 1, 2 ORDER BY 1, 2""")
        f["per_type_source_path_era_after"] = self.rows("SELECT hk_type, device_key, sync_path, rebase_era, time_source, COUNT(*) FROM samples_rebased WHERE rebase_era IS NOT NULL GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 3, 4, 5")
        f["last_tie_groups"] = self.rows(f"""SELECT metric, COUNT(*) FROM (SELECT metric, device_key FROM samples_rebased
            WHERE metric IN ({', '.join(repr(m) for m in LAST_METRICS)}) AND quality IS NULL
            QUALIFY COUNT(*) OVER (PARTITION BY metric, device_key, CAST(start_ts AS DATE), start_utc) > 1
                AND COUNT(DISTINCT value) OVER (PARTITION BY metric, device_key, CAST(start_ts AS DATE), start_utc) > 1) GROUP BY 1 ORDER BY 1""")
        cols_dig = ", ".join(c for c in self.COLS if c != "ingested_at")
        f["staged_digest"] = list(self.con.execute(f"SELECT COUNT(*), CAST(bit_xor(hash({cols_dig})) AS VARCHAR) FROM samples_rebased").fetchone())
        f["eligible_preview"] = {"before": self.one("SELECT COUNT(*) FROM eligible_samples"),
                                 "after": self.one("""SELECT COUNT(*) FROM samples_rebased s JOIN metric_registry m ON m.metric = s.metric
                                     WHERE s.quality IS NULL AND s.device_key <> 'excluded' AND (s.score_state IS NULL OR s.score_state = 'SCORED')""")}
        if self.ah and self.ah.exists():
            self.anchor()
        f["rss_mb_after_gates"] = self.rss_mb()

    def anchor(self) -> None:
        """Independent calibration against apple-health (read-only): nearest
        exact-value match per rebased Apple resting HR and body mass row must
        sit at 0 minutes, and Apple steps per Dubai day must equal."""
        f = self.R["facts"]
        self.con.execute(f"ATTACH '{self.ah}' AS ah (READ_ONLY)")
        try:
            nearest = self.rows("""WITH m AS (SELECT t.sample_id, t.rebase_era, CAST(round((epoch(r.start_date) - epoch(t.start_ts)) / 60.0) AS INTEGER) AS delta_min
                FROM samples_rebased t JOIN ah.records r ON r.record_type = t.hk_type AND r.source_name = t.source_name AND r.value = t.value
                     AND round(epoch(r.end_date) - epoch(r.start_date)) = round(epoch(t.end_ts) - epoch(t.start_ts)) AND abs(epoch(r.start_date) - epoch(t.start_ts)) <= 43200
                WHERE t.metric IN ('resting_hr', 'body_mass') AND t.sync_path = 'bridge' AND t.rebase_era IS NOT NULL)
                SELECT rebase_era, nearest, COUNT(*) FROM (SELECT sample_id, rebase_era, MIN(abs(delta_min)) AS nearest FROM m GROUP BY 1, 2) GROUP BY 1, 2 ORDER BY 1, 2""")
            f["ah_anchor_nearest_delta_by_era"] = nearest
            anchored = sum(c for _e, _n, c in nearest)
            nonzero = sum(c for _e, n, c in nearest if n != 0)
            self.check("ah_anchor_nearest_match_delta_zero_for_every_anchored_row", anchored > 0 and nonzero == 0,
                       {"anchored": anchored, "nonzero": nonzero, "by_era": nearest})
            dec = "CAST(ROUND(SUM(CAST(value AS DECIMAL(30,6))), 3) AS DOUBLE)"
            r = self.con.execute(f"""WITH h AS (SELECT CAST(start_ts AS DATE) d, COUNT(*) n, {dec} v FROM samples_rebased WHERE metric = 'steps' AND sync_path = 'bridge'
                    AND rebase_era IS NOT NULL AND quality IS NULL AND device_key IN ('apple_watch_ultra', 'apple_watch_6_legacy', 'iphone')
                    AND CAST(start_ts AS DATE) BETWEEN DATE '2025-07-01' AND DATE '2026-06-22' GROUP BY 1),
                 a AS (SELECT CAST(start_date AS DATE) d, COUNT(*) n, {dec} v FROM ah.records WHERE record_type = 'HKQuantityTypeIdentifierStepCount'
                    AND source_name IN (SELECT DISTINCT source_name FROM samples_rebased WHERE metric = 'steps' AND device_key IN ('apple_watch_ultra', 'apple_watch_6_legacy', 'iphone'))
                    AND CAST(start_date AS DATE) BETWEEN DATE '2025-07-01' AND DATE '2026-06-22' GROUP BY 1)
                 SELECT COUNT(*), COUNT(*) FILTER (WHERE h.n = a.n AND abs(COALESCE(h.v, 0) - COALESCE(a.v, 0)) < 0.01),
                        COUNT(*) FILTER (WHERE h.d IS NOT NULL) FROM h FULL OUTER JOIN a ON a.d = h.d""").fetchone()
            # Every day either side holds must match; the store's own day count
            # is the denominator (357 on the real store), so the check cannot
            # pass by matching nothing when the store has steps in the span.
            f["ah_steps_days"] = {"days_either_side": r[0], "equal": r[1], "days_with_helios_steps": r[2]}
            self.check("ah_steps_per_dubai_day_equal", r[0] == r[1] and r[0] >= r[2], f["ah_steps_days"], fatal=False)
        finally:
            self.con.execute("DETACH ah")

    # ---- 9. lineage archive (before the cutover) ----
    def archive(self) -> None:
        f = self.R["facts"]
        written = {}
        for d in self.archive_dirs:
            d.mkdir(parents=True, exist_ok=True)
            lines = []
            for t in LINEAGE_TABLES:
                p = d / f"{t.lstrip('_')}.parquet"
                self.con.execute(f"COPY (SELECT * FROM {t}) TO '{p}' (FORMAT PARQUET)")
                lines.append(f"{sha256_file(p)}  {p.name}")
            (d / "MANIFEST.sha256").write_text("\n".join(lines) + "\n")
            written[str(d)] = lines
        manifests = {k: sorted(v) for k, v in written.items()}
        self.check("lineage_archive_identical_in_every_place", len({json.dumps(v) for v in manifests.values()}) == 1, {"places": list(manifests)})
        f["lineage_rows"] = {t: self.one(f"SELECT COUNT(*) FROM {t}") for t in LINEAGE_TABLES}
        # Snapshot of the derived tables for the step-6 diff (parquet beside the report).
        for t in DERIVED:
            self.con.execute(f"COPY (SELECT * FROM {t}) TO '{self.out / f'before_{t}.parquet'}' (FORMAT PARQUET)")
        f["derived_before"] = {t: self.one(f"SELECT COUNT(*) FROM {t}") for t in DERIVED}

    def cleanup_staging(self) -> None:
        for t in ("samples_rebased", *LINEAGE_TABLES):
            self.con.execute(f"DROP TABLE IF EXISTS {t}")

    # ---- 10. cutover ----
    def cutover(self) -> None:
        f = self.R["facts"]
        con = self.con
        f["bytes_before_cutover"] = os.path.getsize(self.path)
        summary = {"label": self.label, "input_fingerprint": f["input_fingerprint"], "policy_digest": f["policy_digest"],
                   "zone": self.zone, "constants": self.R["constants"], "flags": self.R["flags"],
                   "rows_before": f["samples_before"], "rows_after": f["rows_after"], "twins": f["twin_uuids"],
                   "compare_classes": f.get("compare_classes"), "export_link_totals": f.get("export_link_totals"),
                   "whoop": f.get("whoop_day_rows_by_outcome"), "aliases": f["aliases_staged"], "duckdb": f["duckdb"]}
        reread_ddl = self.one("SELECT sql FROM duckdb_tables() WHERE table_name = 'hk_reread'")
        now = datetime.now()
        t0 = time.time()
        con.execute("BEGIN")
        try:
            con.execute("DROP VIEW IF EXISTS eligible_samples")
            con.execute("DROP TABLE samples")
            con.execute("ALTER TABLE samples_rebased RENAME TO samples")
            con.execute("INSERT OR IGNORE INTO sample_aliases (old_id, new_id, reason, created_at) SELECT old_id, new_id, reason, ? FROM _lineage_aliases", [now])
            con.execute("INSERT OR IGNORE INTO tombstones (tomb_id, hk_uuid, metric, start_utc, reason, batch_id, deleted_at) "
                        "SELECT tomb_id, hk_uuid, metric, start_utc, reason, batch_id, ? FROM _lineage_tombstones", [now])
            # Consumed landing rows leave hk_reread (they live in the archive); the
            # table is rebuilt rather than mass-deleted through its primary key.
            con.execute(reread_ddl.replace("CREATE TABLE hk_reread", "CREATE TABLE hk_reread_kept", 1))
            con.execute("INSERT INTO hk_reread_kept SELECT * FROM hk_reread WHERE hk_uuid NOT IN (SELECT hk_uuid FROM _lineage_landing_consumed)")
            con.execute("DROP TABLE hk_reread")
            con.execute("ALTER TABLE hk_reread_kept RENAME TO hk_reread")
            con.execute("INSERT INTO migrations (name, applied_at, code_commit, input_fingerprint, summary) VALUES (?, ?, ?, ?, ?)",
                        [MIGRATION, now, self.code_commit, f["input_fingerprint"], json.dumps(summary, default=str)])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        self.R["steps"]["cutover_transaction"] = round(time.time() - t0, 2)
        for t in LINEAGE_TABLES:
            con.execute(f"DROP TABLE IF EXISTS {t}")
        f["wal_bytes_before_checkpoint"] = os.path.getsize(str(self.path) + ".wal") if os.path.exists(str(self.path) + ".wal") else 0
        t0 = time.time()
        con.execute("CHECKPOINT")
        self.R["steps"]["checkpoint"] = round(time.time() - t0, 2)
        con.close()
        self.con = None
        f["bytes_after_cutover"] = os.path.getsize(self.path)
        f["wal_after_close"] = os.path.exists(str(self.path) + ".wal")

    def reopen_verify(self) -> None:
        """Reopen through the daemon's path twice (schema.sql recreates the view
        and the uuid index), verify the swap persisted and is terminal."""
        f = self.R["facts"]
        for i in (1, 2):
            t0 = time.time()
            c = db.connect(self.path)
            self.R["steps"][f"reopen_{i}"] = round(time.time() - t0, 2)
            try:
                n = c.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
                if i == 1:
                    idx = sorted(r[0] for r in c.execute("SELECT index_name FROM duckdb_indexes() WHERE table_name = 'samples'").fetchall())
                    f["indexes_after_reopen"] = idx
                    self.check("only_the_uuid_index_after_the_swap", idx == ["idx_samples_hk_uuid"], idx)
                    self.check("view_present_after_reopen", c.execute("SELECT COUNT(*) FROM duckdb_views() WHERE view_name = 'eligible_samples'").fetchone()[0] == 1)
                    cols = [r[0] for r in c.execute("DESCRIBE samples").fetchall()]
                    self.check("columns_equal_before_and_after", cols == self.COLS, {"after": len(cols), "before": len(self.COLS)})
                    cols_dig = ", ".join(x for x in self.COLS if x != "ingested_at")
                    dig = list(c.execute(f"SELECT COUNT(*), CAST(bit_xor(hash({cols_dig})) AS VARCHAR) FROM samples").fetchone())
                    self.check("staged_digest_survives_the_swap", dig == f["staged_digest"], {"after": dig, "staged": f["staged_digest"]})
                    self.check("migration_row_present", db.migration_applied(c, MIGRATION))
                    dup = c.execute("SELECT COUNT(*) FROM (SELECT hk_uuid FROM samples WHERE hk_uuid IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 1)").fetchone()[0]
                    self.check("no_duplicate_uuid_after_cutover", dup == 0, dup)
                    left = dict(c.execute("SELECT sync_path, COUNT(*) FROM samples WHERE time_source IS NULL GROUP BY 1").fetchall())
                    f["legacy_rows_left_by_path"] = left
                    self.check("legacy_rows_left_carry_an_explicit_quality",
                               c.execute("SELECT COUNT(*) FROM samples WHERE time_source IS NULL AND quality IS NULL").fetchone()[0] == 0)
                    f["eligible_rows_after"] = c.execute("SELECT COUNT(*) FROM eligible_samples").fetchone()[0]
                    f["aliases_after"] = dict(c.execute("SELECT reason, COUNT(*) FROM sample_aliases GROUP BY 1").fetchall())
                    f["hk_reread_rows_after"] = c.execute("SELECT COUNT(*) FROM hk_reread").fetchone()[0]
                self.check(f"startup_{i}_sees_the_rebuilt_table", n == f["rows_after"], {"samples": n, "expected": f["rows_after"]})
            finally:
                c.close()

    # ---- 11. full derived rebuild and diff ----
    def _rebuild_derived(self, con, prefix: str) -> dict:
        """Clear every derived table and rebuild every date from the first
        eligible one to today, offline. Shared by the baseline rebuild (on the
        un-migrated input) and the step-6 rebuild (after the cutover)."""
        t0 = time.time()
        with db.transaction(con) as c:
            for t in DERIVED + ("derived_generation", "narratives"):
                c.execute(f"DELETE FROM {t}")
            c.execute("DELETE FROM actions WHERE status = 'suggested'")
        first = con.execute("SELECT MIN(CAST(start_ts AS DATE)) FROM eligible_samples").fetchone()[0]
        today = self.today
        if first is None:
            first = today
        n_dv = 0
        start = first
        while start <= today:
            end = min(today, start + timedelta(days=365))
            n_dv += compute_daily_values(con, self.policy, self.registry, start, end, as_of=today)
            start = end + timedelta(days=1)
        self.R["steps"][f"{prefix}_daily_values"] = round(time.time() - t0, 2)
        t1 = time.time()
        n_bl = n_sg = 0
        d = first
        while d <= today:
            n_bl += compute_baselines(con, self.policy, d)
            n_sg += compute_signals(con, self.policy, d)
            d += timedelta(days=1)
        self.R["steps"][f"{prefix}_baselines_signals"] = round(time.time() - t1, 2)
        con.execute("CHECKPOINT")
        return {"range": [str(first), str(today)], "daily_values": n_dv, "baselines": n_bl, "signals": n_sg,
                "derived_after": {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in DERIVED}}

    def baseline(self) -> None:
        self.R["facts"]["baseline_rebuild"] = self._rebuild_derived(self.con, "baseline")

    def rebuild(self) -> None:
        f = self.R["facts"]
        con = duckdb.connect(str(self.path))
        db.init_schema(con)
        self.policy.sync_registry(con)
        self.con = con
        try:
            out = self._rebuild_derived(con, "rebuild")
            f["rebuild_range"] = out.pop("range")
            f["rebuild_counts"] = out
            self.diff()
            self.oracle()
        finally:
            con.close()
            self.con = None

    def diff(self) -> None:
        """Every difference between the snapshot and the rebuilt derived tables,
        classified by the lineage that explains it; unexplained cells are a gate.

        A lineage row explains the CELL it feeds: sleep_analysis rows feed the
        sleep_duration cell of their END day (the night is bucketed by its end),
        every other row feeds its own metric on its START day. Reasons are
        deliberately narrow: day_shift (the row's cell day moved), twin_collapse
        (a dropped twin or its survivor on that cell), export_dedupe (a linked or
        ambiguous export row on that cell), whoop_replacement (a day row of that
        cell), reread_update (a survivor whose fields the re-read changed),
        last_reorder (a `last` metric's rows from more than one era or beside
        native rows), last_tie_rekey (a `last` metric's same-instant tie, whose
        sample_id order the re-keying changed; adjudication-A point 28) and
        stale_before (a removed cell with no row of that metric on that old day
        anywhere, so the snapshot's cell had no eligible input to begin with)."""
        f = self.R["facts"]
        lin = self.archive_dirs[0]
        before = self.out / "before_daily_values.parquet"
        last_metrics = [m for m in self.policy.metrics if self.policy.agg(m) == "last"] or ["__none__"]
        last_in = ", ".join(repr(m) for m in last_metrics)
        off = f"INTERVAL {REPORTING_OFFSET_MIN} MINUTE"
        reb = f"read_parquet('{lin / 'lineage_rebased.parquet'}')"
        tw = f"read_parquet('{lin / 'lineage_twins_dropped.parquet'}')"
        wp = f"read_parquet('{lin / 'lineage_whoop.parquet'}')"
        # The cell a lineage row feeds: (cell metric, old day, new day).
        cm = "CASE WHEN metric = 'sleep_analysis' THEN 'sleep_duration' ELSE metric END"
        night = "metric IN ('sleep_analysis', 'sleep_duration')"
        old_day = f"CASE WHEN {night} THEN CAST(end_old AS DATE) ELSE CAST(start_old AS DATE) END"
        new_day = f"CASE WHEN {night} THEN CAST(end_utc + {off} AS DATE) ELSE CAST(start_utc + {off} AS DATE) END"
        tw_day = f"CASE WHEN {night} THEN CAST(end_ts AS DATE) ELSE CAST(start_ts AS DATE) END"
        s_day = f"CASE WHEN {night} THEN CAST(end_ts AS DATE) ELSE CAST(start_ts AS DATE) END"
        self.con.execute(f"""CREATE OR REPLACE TEMP TABLE dv_diff AS
            SELECT COALESCE(b.date, a.date) AS date, COALESCE(b.metric, a.metric) AS metric,
                   b.value AS v_before, a.value AS v_after, b.device_key AS dk_before, a.device_key AS dk_after, b.n_samples AS n_before, a.n_samples AS n_after,
                   CASE WHEN b.date IS NULL THEN 'added' WHEN a.date IS NULL THEN 'removed'
                        WHEN b.value IS DISTINCT FROM a.value OR b.device_key IS DISTINCT FROM a.device_key OR b.n_samples IS DISTINCT FROM a.n_samples THEN 'changed'
                        ELSE 'same' END AS kind
            FROM read_parquet('{before}') b FULL OUTER JOIN daily_values a ON a.date = b.date AND a.metric = b.metric""")
        self.con.execute(f"""CREATE OR REPLACE TEMP TABLE lineage_cells AS
            SELECT {cm} AS metric, {old_day} AS old_day, {new_day} AS new_day, time_source, compare_class, hk_uuid, 'rebased' AS src FROM {reb}
            UNION ALL SELECT {cm}, {tw_day}, NULL, NULL, NULL, hk_uuid, 'twin_dropped' FROM {tw}""")
        self.con.execute(f"""CREATE OR REPLACE TEMP TABLE reasons AS
            SELECT metric, old_day AS date, 'day_shift' AS reason FROM lineage_cells WHERE src = 'rebased' AND old_day <> new_day
            UNION SELECT metric, new_day, 'day_shift' FROM lineage_cells WHERE src = 'rebased' AND old_day <> new_day
            UNION SELECT metric, old_day, 'twin_collapse' FROM lineage_cells WHERE src = 'twin_dropped'
            UNION SELECT metric, new_day, 'twin_collapse' FROM lineage_cells WHERE src = 'rebased' AND hk_uuid IN (SELECT hk_uuid FROM {tw})
            UNION SELECT metric, old_day, 'export_dedupe' FROM lineage_cells WHERE compare_class IN ('linked', 'ambiguous')
            UNION SELECT metric, new_day, 'export_dedupe' FROM lineage_cells WHERE compare_class IN ('linked', 'ambiguous')
            UNION SELECT metric, day, 'whoop_replacement' FROM {wp} WHERE day IS NOT NULL
            UNION SELECT w.metric, {s_day.replace('metric', 's.metric').replace('end_ts', 's.end_ts').replace('start_ts', 's.start_ts')}, 'whoop_replacement' FROM {wp} w JOIN samples s ON s.sample_id = w.target
            UNION SELECT metric, old_day, 'reread_update' FROM lineage_cells WHERE time_source = '{TS_REREAD}' AND compare_class <> 'equal'
            UNION SELECT metric, new_day, 'reread_update' FROM lineage_cells WHERE time_source = '{TS_REREAD}' AND compare_class <> 'equal'
            UNION SELECT metric, new_day, 'last_reorder' FROM (
                  SELECT l.metric, l.new_day, r.era FROM lineage_cells l JOIN {reb} r ON r.hk_uuid IS NOT DISTINCT FROM l.hk_uuid AND l.src = 'rebased'
                  WHERE l.metric IN ({last_in})) GROUP BY 1, 2 HAVING COUNT(DISTINCT era) > 1
            UNION SELECT l.metric, l.new_day, 'last_reorder' FROM lineage_cells l WHERE l.src = 'rebased' AND l.metric IN ({last_in})
                  AND EXISTS (SELECT 1 FROM samples s WHERE s.metric = l.metric AND s.rebase_era IS NULL AND CAST(s.start_ts AS DATE) = l.new_day)
            UNION SELECT metric, d, 'last_tie_rekey' FROM (
                  SELECT metric, CAST(start_ts AS DATE) AS d FROM samples WHERE metric IN ({last_in}) AND quality IS NULL
                  QUALIFY COUNT(*) OVER (PARTITION BY metric, device_key, CAST(start_ts AS DATE), start_utc) > 1)""")
        self.con.execute("""CREATE OR REPLACE TEMP TABLE dv_classified AS
            SELECT d.*, COALESCE((SELECT array_to_string(list_sort(list_distinct(list(r.reason))), ',') FROM reasons r WHERE r.metric = d.metric AND r.date = d.date), '') AS reasons
            FROM dv_diff d WHERE d.kind <> 'same'""")
        # A removed cell that no row of that metric fed on that old day anywhere
        # (lineage and native rows together hold every row the old store had).
        self.con.execute(f"""UPDATE dv_classified SET reasons = 'stale_before' WHERE kind = 'removed' AND reasons = ''
            AND NOT EXISTS (SELECT 1 FROM lineage_cells l WHERE l.metric = dv_classified.metric AND l.old_day = dv_classified.date)
            AND NOT EXISTS (SELECT 1 FROM samples s WHERE s.rebase_era IS NULL AND s.time_source IS NOT NULL
                            AND {cm.replace('metric', 's.metric')} = dv_classified.metric AND {s_day.replace('metric', 's.metric').replace('end_ts', 's.end_ts').replace('start_ts', 's.start_ts')} = dv_classified.date)""")
        f["derived_diff_daily_values"] = self.rows("SELECT kind, reasons, COUNT(*) FROM dv_classified GROUP BY 1, 2 ORDER BY 3 DESC")
        f["derived_diff_daily_values_by_metric"] = self.rows("SELECT metric, kind, COUNT(*) FROM dv_classified GROUP BY 1, 2 ORDER BY 1, 2")
        same = self.one("SELECT COUNT(*) FROM dv_diff WHERE kind = 'same'")
        unexplained = self.one("SELECT COUNT(*) FROM dv_classified WHERE reasons = ''")
        f["derived_diff_unexplained_cells"] = unexplained
        f["derived_diff_same_cells"] = same
        f["derived_diff_unexplained_sample"] = self.rows("SELECT metric, CAST(date AS VARCHAR), kind FROM dv_classified WHERE reasons = '' ORDER BY 1, 2 LIMIT 40")
        f["derived_diff_unexplained_by_metric"] = self.rows("SELECT metric, kind, COUNT(*) FROM dv_classified WHERE reasons = '' GROUP BY 1, 2 ORDER BY 3 DESC")
        self.check("every_daily_value_difference_is_explained_by_lineage", unexplained == 0, {"unexplained": unexplained, "same": same})
        # Baselines and signals change only where a daily value of that metric changed inside the window.
        for t, key in (("baselines", "window_days"), ("signals", "state")):
            self.con.execute(f"""CREATE OR REPLACE TEMP TABLE dep_{t} AS
                SELECT COALESCE(b.date, a.date) AS date, COALESCE(b.metric, a.metric) AS metric
                FROM read_parquet('{self.out / f'before_{t}.parquet'}') b FULL OUTER JOIN {t} a ON a.date = b.date AND a.metric = b.metric
                     {'AND a.window_days = b.window_days' if t == 'baselines' else ''}
                WHERE b.date IS NULL OR a.date IS NULL OR {'b.median IS DISTINCT FROM a.median OR b.mad IS DISTINCT FROM a.mad OR b.n_days IS DISTINCT FROM a.n_days' if t == 'baselines' else 'b.state IS DISTINCT FROM a.state OR b.value IS DISTINCT FROM a.value OR b.delta_pct IS DISTINCT FROM a.delta_pct'}""")
            n_changed = self.one(f"SELECT COUNT(*) FROM dep_{t}")
            n_dep = self.one(f"""SELECT COUNT(*) FROM dep_{t} x WHERE EXISTS (SELECT 1 FROM dv_classified d WHERE d.metric = x.metric
                AND d.date BETWEEN x.date - INTERVAL {self.policy.max_window + 14} DAY AND x.date)""")
            f[f"derived_diff_{t}"] = {"changed": n_changed, "dependent_on_a_changed_daily_value": n_dep}
            self.check(f"{t}_change_only_with_a_changed_daily_value_in_window", n_changed == n_dep, f[f"derived_diff_{t}"], fatal=False)

    def oracle(self) -> None:
        """Independent numerical oracle: for a sample of (metric, day) cells,
        recompute the primary device's value straight from samples (not the
        view, not the dispatcher) and compare with daily_values."""
        f = self.R["facts"]
        pol = self.policy
        cells = self.rows("SELECT metric, date FROM daily_values ORDER BY hash(metric || CAST(date AS VARCHAR)) LIMIT ?", [self.oracle_cells])
        checked = mism = 0
        details = []
        for metric, day in cells:
            prio = pol.priority(metric)
            agg = pol.agg(metric)
            if metric == "sleep_duration" or not prio:
                continue
            per_dev = {}
            for dk in prio:
                if agg == "sum":
                    v = self.one("""SELECT CAST(ROUND(SUM(CAST(value AS DECIMAL(30,6))), 3) AS DOUBLE) FROM samples WHERE metric = ? AND device_key = ? AND quality IS NULL
                        AND (score_state IS NULL OR score_state = 'SCORED') AND CAST(start_ts AS DATE) = ? AND value IS NOT NULL""", [metric, dk, day])
                elif agg == "last":
                    v = self.one("""SELECT value FROM samples WHERE metric = ? AND device_key = ? AND quality IS NULL AND (score_state IS NULL OR score_state = 'SCORED')
                        AND CAST(start_ts AS DATE) = ? AND value IS NOT NULL ORDER BY start_utc DESC, sample_id DESC LIMIT 1""", [metric, dk, day])
                    v = round(v, 3) if v is not None else None
                elif agg == "avg":
                    v = self.one("""SELECT ROUND(CAST(SUM(CAST(value AS DECIMAL(30,6))) AS DOUBLE) / COUNT(value), 3) FROM samples WHERE metric = ? AND device_key = ? AND quality IS NULL
                        AND (score_state IS NULL OR score_state = 'SCORED') AND CAST(start_ts AS DATE) = ? AND value IS NOT NULL""", [metric, dk, day])
                else:
                    continue
                if v is not None:
                    per_dev[dk] = v
            if not per_dev:
                continue
            primary = next(dk for dk in prio if dk in per_dev)
            expect = per_dev[primary]
            if metric in FRAC_METRICS and metric == "body_fat_pct" and expect is not None and expect <= 1.5:
                expect = round(expect * 100.0, 3)      # the view's one scaling rule
            got = self.one("SELECT value FROM daily_values WHERE metric = ? AND date = ?", [metric, day])
            checked += 1
            if got is None or abs(got - expect) > 0.0015:
                mism += 1
                details.append([metric, str(day), expect, got])
        f["oracle"] = {"cells_checked": checked, "mismatches": mism, "sample": details[:20]}
        self.check("independent_oracle_agrees_with_rebuilt_daily_values", checked > 0 and mism == 0, f["oracle"])

    # ---- driver ----
    def run(self) -> dict:
        try:
            with self.step("open"):
                self.open()
            with self.step("preconditions"):
                self.preconditions()
            if self.baseline_rebuild:
                with self.step("baseline_rebuild"):
                    self.baseline()
            with self.step("eras"):
                self.eras()
            with self.step("materialize"):
                self.materialize()
            with self.step("twins"):
                self.twins()
            with self.step("reread_compare"):
                self.reread()
            with self.step("export_link"):
                self.exports()
            with self.step("whoop_reconcile"):
                self.whoop()
            with self.step("stage"):
                self.stage()
            with self.step("gates"):
                self.gates()
            with self.step("archive"):
                self.archive()
            if self.do_cutover:
                with self.step("cutover"):
                    self.cutover()
                with self.step("reopen_verify"):
                    self.reopen_verify()
                if self.do_rebuild:
                    with self.step("rebuild"):
                        self.rebuild()
            else:
                with self.step("cleanup_staging"):
                    self.cleanup_staging()
                    self.con.execute("CHECKPOINT")
        except Stop as e:
            self.R["stopped"] = str(e)
            self.log(f"STOP: {e}")
            if self.con is not None:
                try:
                    self.cleanup_staging()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            if self.con is not None:
                try:
                    self.con.close()
                except Exception:  # noqa: BLE001
                    pass
                self.con = None
        self.R["fails"] = [k for k, v in self.R["checks"].items() if not v["ok"]]
        self.R["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.R["rss_mb_final"] = self.rss_mb()
        self.R["ok"] = not self.R["fails"] and self.R["stopped"] is None
        (self.out / f"{self.label}.json").write_text(json.dumps(self.R, indent=2, default=str))
        return self.R


def render_markdown(R: dict) -> str:
    """The report as plain markdown: constants, every check, the aggregate
    facts and timings. Counts only (the facts hold no sample values)."""
    out = [f"# Phase 1b migration report: {R.get('label')} ({R.get('started')} to {R.get('finished')})", ""]
    out.append(f"Result: {'OK' if R.get('ok') else 'FAILED'}; stopped: {R.get('stopped')}; failed checks: {R.get('fails')}")
    out.append(f"Code commit: {R.get('code_commit')}; flags: {json.dumps(R.get('flags'))}")
    out.append("")
    out.append("## Constants")
    out.append("```json\n" + json.dumps(R.get("constants"), indent=1, default=str) + "\n```")
    out.append("")
    out.append("## Checks")
    out.append("| check | result | detail |")
    out.append("| --- | --- | --- |")
    for k, v in R.get("checks", {}).items():
        d = json.dumps(v.get("detail"), default=str)
        d = d if len(d) <= 300 else d[:300] + "..."
        out.append(f"| {k} | {'PASS' if v['ok'] else 'FAIL'} | {d.replace('|', '/')} |")
    out.append("")
    out.append("## Timings (seconds)")
    out.append("```json\n" + json.dumps(R.get("steps"), indent=1) + "\n```")
    out.append("")
    out.append("## Facts (aggregates)")
    for k, v in R.get("facts", {}).items():
        body = json.dumps(v, default=str)
        if len(body) > 4000:
            body = body[:4000] + " ...(truncated; full value in the json)"
        out.append(f"- {k}: {body}")
    return "\n".join(out) + "\n"
