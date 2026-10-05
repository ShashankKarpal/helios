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

Checkpoint B (2026-10-05 evening) added: the migrations row carries a phase
(cutover_committed, then verified once reopen, rebuild, diff and oracle
passed; --resume-verify finishes an interrupted verification); exact
predicates for every explained compare class; an export candidate must share
device, unit and unit rule, be eligible on both sides and carry a value on
both sides (no NULL-as-zero); deterministic Whoop correspondence with an
ambiguity class and a payload check for "superseded"; gates over the FINAL
alias relation, both identity directions, the exact eligibility set and the
frozen per-type budgets; apply-mode evidence (a re-read, an anchor, a clean
tree, the expected input fingerprint and policy digest, two distinct archive
places); a fingerprint-qualified archive with the final alias relation; a
complete set-based oracle that recomputes every expected cell from samples
and compares both directions; counts only in every artifact.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import resource
import subprocess
import time
from decimal import Decimal
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

from heliosd.ingest import whoop as wh
from heliosd.ingest.normalize import KNOWN_STAGES, UNIT_ALIASES, reporting_today
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
                  "_lineage_whoop", "_lineage_landing_consumed", "_lineage_aliases", "_lineage_tombstones",
                  "_lineage_aliases_final")
DERIVED = ("daily_values", "baselines", "signals")
UNEXPLAINED = ("instant_differs", "content_differs", "ambiguous", "identity_differs")
# Compare classes that do not win and do not stop: the observation cannot
# confirm the instant (an offset-free input), so the row stays era_rebase_v1.
NON_WINNING = ("unconfirmed_time_source",)
BUDGET_PCT = 0.5          # plan v2 decision 8: at most 0.5 percent of rows per type for a quarantine class
EXCEPTIONS = ("budget:whoop", "budget:export", "no_reread", "no_anchor", "dirty_tree", "single_archive")
PHASE_CUTOVER, PHASE_VERIFIED = "cutover_committed", "verified"


class Stop(RuntimeError):
    """A gate failed or a precondition is unmet: nothing was written."""


class _Done(Exception):
    """Internal: the resume path finished."""


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


def _code_dirty(start: Path) -> bool | None:
    """Whether the checkout holding this code has uncommitted changes (git
    status --porcelain); None when git is unavailable."""
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=str(start), capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
        return bool(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None


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
                 log=None, oracle_cells: int = 200, baseline_rebuild: bool = False,
                 exceptions: tuple[str, ...] | list[str] = (), expect_input_fingerprint: str | None = None,
                 expect_policy_digest: str | None = None, resume_verify: bool = False):
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
        self.code_dirty = _code_dirty(Path(__file__).parent)
        self.exceptions = set(exceptions)
        unknown = self.exceptions - set(EXCEPTIONS)
        if unknown:
            raise ValueError(f"unknown exceptions {sorted(unknown)}; known: {EXCEPTIONS}")
        self.expect_fp, self.expect_pd = expect_input_fingerprint, expect_policy_digest
        self.resume_verify = resume_verify
        self.archive_paths: list[Path] = []
        # One timestamp for everything this run writes (aliases, tombstones,
        # the migrations row), so the archive and the store agree byte for byte.
        self.stamp = datetime.now().replace(microsecond=0)
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
                                  "accept_reread_mismatches": self.accept, "baseline_rebuild": baseline_rebuild,
                                  "exceptions": sorted(self.exceptions), "resume_verify": resume_verify,
                                  "expect_input_fingerprint": expect_input_fingerprint,
                                  "expect_policy_digest": expect_policy_digest},
                        "code_commit": self.code_commit, "code_dirty": self.code_dirty,
                        "steps": {}, "checks": {}, "facts": {}, "stopped": None}
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
        # written with COPY TO parquet). The completion check runs BEFORE the
        # daemon's DDL and the registry mirror, so a refused run writes nothing
        # (checkpoint B point 24).
        self.con = duckdb.connect(str(self.path))
        has = self.con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'migrations'").fetchone()[0]
        row = self.con.execute("SELECT summary FROM migrations WHERE name = ?", [MIGRATION]).fetchone() if has else None
        self.applied_summary = json.loads(row[0]) if row and row[0] else ({} if row else None)
        if self.applied_summary is not None and not self.resume_verify:
            self.check("migration_not_applied_yet", False, {"phase": self.applied_summary.get("phase")})
        if self.resume_verify and (self.applied_summary is None or self.applied_summary.get("phase") == PHASE_VERIFIED):
            self.check("resume_requires_a_committed_unverified_migration", False,
                       {"phase": None if self.applied_summary is None else self.applied_summary.get("phase")})
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
        """Over the whole effective configuration the rebuild depends on."""
        pol = self.policy
        body = {"metrics": pol.metrics, "zone": self.zone, "baseline": pol.baseline, "confidence": pol.confidence,
                "blocks": pol.blocks, "sources": pol.sources, "windows": pol.windows, "min_days": pol.min_days}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    # ---- 1. preconditions ----
    def preconditions(self) -> None:
        f = self.R["facts"]
        f["duckdb"] = duckdb.__version__
        f["tzdata"] = (open("/usr/share/zoneinfo/+VERSION").read().strip()
                       if os.path.exists("/usr/share/zoneinfo/+VERSION") else None)
        self.check("zone_is_the_frozen_reporting_zone", self.zone == ZONE, self.zone)
        apply = self.label == "apply"
        f["apply_mode"] = apply
        if apply:
            # Evidence policy for the live store (checkpoint B points 17, 18, 19):
            # nothing vacuous, nothing optional, the input bound to the reviewed
            # run. Every requirement is reported, then one gate stops.
            reqs = [
                self.check("apply_requires_a_clean_checkout", self.code_dirty is False or "dirty_tree" in self.exceptions,
                           {"dirty": self.code_dirty, "exception": "dirty_tree" in self.exceptions}, fatal=False),
                self.check("apply_requires_the_rebuild", self.do_rebuild, None, fatal=False),
                self.check("apply_requires_an_anchor_path", (self.ah is not None and self.ah.exists()) or "no_anchor" in self.exceptions,
                           {"path": str(self.ah) if self.ah else None}, fatal=False),
                self.check("apply_requires_two_distinct_archive_places",
                           len({d.resolve() for d in self.archive_dirs}) >= 2 or "single_archive" in self.exceptions,
                           [str(d) for d in self.archive_dirs], fatal=False),
                self.check("apply_requires_the_expected_fingerprints", bool(self.expect_fp and self.expect_pd), None, fatal=False)]
            self.check("apply_evidence_complete", all(reqs), {"unmet": [k for k, v in self.R["checks"].items() if k.startswith("apply_requires") and not v["ok"]]})
        if self.ah is not None:
            self.check("anchor_path_exists_when_given", self.ah.exists(), str(self.ah))
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
        if self.expect_fp:
            self.check("input_fingerprint_equals_the_reviewed_one", f["input_fingerprint"] == self.expect_fp,
                       {"expected": self.expect_fp[:16], "actual": f["input_fingerprint"][:16]})
        if self.expect_pd:
            self.check("policy_digest_equals_the_reviewed_one", f["policy_digest"] == self.expect_pd,
                       {"expected": self.expect_pd[:16], "actual": f["policy_digest"][:16]})
        if apply:
            self.check("apply_requires_re_read_evidence", f["hk_reread_rows"] > 0 or "no_reread" in self.exceptions,
                       {"hk_reread_rows": f["hk_reread_rows"], "exception": "no_reread" in self.exceptions})
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
        content_cmp = " OR ".join(f"a.{c} IS DISTINCT FROM b.{c}" for c in cmp_cols if c not in ("su", "eu", "era"))
        conflicts = self.rows(f"""SELECT a.hk_type, a.device_key, a.era, b.era,
                COUNT(*) FILTER (WHERE a.su IS DISTINCT FROM b.su OR a.eu IS DISTINCT FROM b.eu) AS instant_conflict_uuids,
                COUNT(*) FILTER (WHERE {content_cmp}) AS content_conflict_uuids
            FROM lb a JOIN lb b ON a.hk_uuid = b.hk_uuid AND a.rn = 1 AND b.rn = 2
            WHERE a.su IS DISTINCT FROM b.su OR a.eu IS DISTINCT FROM b.eu OR {content_cmp}
            GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC, 6 DESC LIMIT 50""")
        f["twin_conflicts_by_type"] = conflicts
        f["twin_conflicting_uuids"] = {"instant": sum(r[4] for r in conflicts), "content": sum(r[5] for r in conflicts)}
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
                     WHEN h.time_source IS DISTINCT FROM 'bridge_utc' THEN 'unconfirmed_time_source'
                     WHEN h.start_utc IS DISTINCT FROM s.su OR h.end_utc IS DISTINCT FROM s.eu THEN 'instant_differs'
                     WHEN h.value IS NOT DISTINCT FROM s.value AND h.unit IS NOT DISTINCT FROM s.unit AND h.text_value IS NOT DISTINCT FROM s.text_value
                          AND h.quality IS NOT DISTINCT FROM s.quality AND h.unit_rule IS NOT DISTINCT FROM s.unit_rule THEN 'equal'
                     -- Exact transformation predicates (checkpoint B point 8): every other field equal.
                     WHEN s.metric IN ({', '.join(repr(m) for m in FRAC_METRICS)}) AND s.unit_rule IS NULL AND h.unit_rule = 'frac_to_pct_v1'
                          AND s.value IS NOT NULL AND h.value IS NOT NULL AND s.value >= 0 AND s.value <= 1.5
                          AND abs(h.value - s.value * 100.0) < 1e-6 AND {ua('h.unit')} = {ua('s.unit')}
                          AND h.text_value IS NOT DISTINCT FROM s.text_value AND h.quality IS NULL AND s.quality IS NULL THEN 'explained_frac_to_pct'
                     WHEN h.value IS NOT DISTINCT FROM s.value AND h.text_value IS NOT DISTINCT FROM s.text_value AND h.unit_rule IS NOT DISTINCT FROM s.unit_rule
                          AND h.unit IS DISTINCT FROM s.unit AND {ua('h.unit')} = {ua('s.unit')} AND h.quality IS NOT DISTINCT FROM s.quality THEN 'explained_unit_alias'
                     WHEN h.value IS NOT DISTINCT FROM s.value AND h.text_value IS NOT DISTINCT FROM s.text_value AND h.unit_rule IS NOT DISTINCT FROM s.unit_rule
                          AND h.unit IS NOT DISTINCT FROM s.unit AND s.quality IS NULL AND h.quality IS NOT NULL
                          AND ((h.quality = 'unit_mismatch' AND {ua('s.unit')} IS DISTINCT FROM m.unit)
                               OR (h.quality = 'bad_time' AND s.eu < s.su)
                               OR (h.quality = 'unknown_category' AND s.metric = 'sleep_analysis'
                                   AND (s.text_value IS NULL OR s.text_value NOT IN ({', '.join(repr(x) for x in sorted(KNOWN_STAGES))}))))
                          THEN 'explained_quality'
                     ELSE 'content_differs' END AS cls
            FROM lb s JOIN hk_reread h ON h.hk_uuid = s.hk_uuid LEFT JOIN metric_registry m ON m.metric = s.metric
            WHERE s.rn = 1 AND s.era IN (1, 2, 4)""")
        f["compare_classes"] = dict((r[0], r[1]) for r in self.rows("SELECT cls, COUNT(*) FROM cmp GROUP BY 1 ORDER BY 1"))
        f["compare_by_type_source_path_era"] = self.rows("SELECT hk_type, device_key, sync_path, era, cls, COUNT(*) FROM cmp GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 3, 4, 5")
        f["compare_delta_histogram"] = self.rows("SELECT hk_type, era, delta_start_min, delta_end_min, COUNT(*) FROM cmp WHERE cls = 'instant_differs' GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC LIMIT 100")
        f["compare_instant_differs_sub_minute"] = self.one("""SELECT COUNT(*) FROM cmp WHERE cls = 'instant_differs'
            AND abs(epoch(rr_start) - epoch(rb_start)) < 60 AND abs(epoch(rr_end) - epoch(rb_end)) < 60""")
        f["native_uuids_with_variants"] = self.one("""SELECT COUNT(DISTINCT v.hk_uuid) FROM hk_reread_variants v
            WHERE v.hk_uuid IN (SELECT hk_uuid FROM samples WHERE time_source IS NOT NULL)""")
        f["compare_uuids_in_both"] = self.one("SELECT COUNT(*) FROM cmp")
        f["reread_rows_tombstoned_ignored"] = self.one("SELECT COUNT(*) FROM hk_reread h WHERE h.hk_uuid IN (SELECT hk_uuid FROM tombstones WHERE hk_uuid IS NOT NULL)")
        f["reread_rows_native_uuid"] = self.one("SELECT COUNT(*) FROM hk_reread h WHERE h.hk_uuid IN (SELECT hk_uuid FROM samples WHERE time_source IS NOT NULL)")
        f["reread_rows_without_any_row"] = self.one("SELECT COUNT(*) FROM hk_reread h WHERE h.hk_uuid NOT IN (SELECT hk_uuid FROM samples WHERE hk_uuid IS NOT NULL) AND h.hk_uuid NOT IN (SELECT hk_uuid FROM tombstones WHERE hk_uuid IS NOT NULL)")
        f["legacy_only_by_type_era"] = self.rows("""SELECT hk_type, device_key, era, COUNT(*) FROM lb WHERE rn = 1 AND era IN (1, 2, 4)
            AND hk_uuid NOT IN (SELECT hk_uuid FROM hk_reread) GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""")
        f["legacy_only_uuids"] = sum(r[3] for r in f["legacy_only_by_type_era"])
        f["new_from_reread_by_type"] = self.rows("SELECT hk_type, strftime(ingested_at, '%Y-%m'), COUNT(*) FROM samples WHERE sample_id LIKE 'hk:%' AND time_source = 'bridge_utc' GROUP BY 1, 2 ORDER BY 1, 2")
        f["compare_non_winning"] = {k: v for k, v in f["compare_classes"].items() if k in NON_WINNING}
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
            SELECT new_id AS sample_id, metric, source_name, device_key, unit, unit_rule, quality, su, eu, value, text_value FROM lb_final
            UNION ALL
            SELECT sample_id, metric, source_name, device_key, unit, unit_rule, quality, start_utc AS su, end_utc AS eu, value, text_value FROM samples
            WHERE sync_path = 'bridge' AND time_source IS NOT NULL""")
        # A candidate replacement shares identity (metric, source, device), unit and
        # unit rule, is ELIGIBLE on both sides, and compares a value with a value:
        # two NULL values match only with equal text, a NULL never matches a number
        # (checkpoint B point 7).
        self.con.execute(f"""CREATE TEMP TABLE xcand AS
            SELECT x.sample_id AS x_id, b.sample_id AS b_id, x.metric, x.source_name, abs(x.value - b.value) AS delta
            FROM lx x JOIN bridge_final b ON b.metric = x.metric AND b.source_name = x.source_name AND b.device_key = x.device_key
                 AND b.unit IS NOT DISTINCT FROM x.unit AND b.unit_rule IS NOT DISTINCT FROM x.unit_rule
                 AND b.quality IS NULL AND x.quality IS NULL
                 AND date_trunc('second', b.su) = date_trunc('second', x.su) AND date_trunc('second', b.eu) = date_trunc('second', x.eu)
                 AND b.text_value IS NOT DISTINCT FROM x.text_value
            WHERE (x.value IS NULL AND b.value IS NULL)
               OR (x.value IS NOT NULL AND b.value IS NOT NULL AND {tolerance_sql()})""")
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
                        FROM lx x JOIN bridge_final b ON b.metric = x.metric AND b.source_name = x.source_name AND b.device_key = x.device_key
                             AND date_trunc('second', b.su) = date_trunc('second', x.su) AND date_trunc('second', b.eu) = date_trunc('second', x.eu)
                             AND b.text_value IS NOT DISTINCT FROM x.text_value AND x.value IS NOT NULL AND b.value IS NOT NULL
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
        recs = self.dicts("SELECT record_key, kind, start_utc, end_utc, created_at, score_state, nap, payload FROM whoop_records ORDER BY record_key")
        by_day: dict[tuple[str, date], list[dict]] = {}
        for r in recs:
            if r["nap"]:
                continue
            d = wh.projection_date(r["kind"], r["start_utc"], r["end_utc"], r["created_at"], zone)
            if d is not None:
                by_day.setdefault((r["kind"], d), []).append(r)
        metric_kind = {m: k for k, ms in wh.KIND_METRICS.items() for m in ms}
        present = {r[0]: r[1] for r in self.rows("SELECT sample_id, value FROM samples WHERE sample_id LIKE 'wh:%' AND time_source IS NOT NULL")}
        def yields(c: dict, metric: str) -> bool:
            """Whether the record's payload derives a sample of that metric."""
            try:
                return any(sp["metric"] == metric for sp in wh.derive_samples(c["kind"], json.loads(c["payload"] or "null") or {}))
            except (TypeError, ValueError):
                return False
        out = []
        for r in day_rows:
            m = re.match(r"^wh:([a-z_]+):(\d{4}-\d{2}-\d{2})$", r["sample_id"])
            base = [r["sample_id"], r["metric"], r["start_ts"], r["end_ts"]]
            if not m or m.group(1) != r["metric"]:
                out.append(base + [None, "quarantined", None, None, r["value"], None, "unparseable_id"])
                continue
            metric, day = m.group(1), date.fromisoformat(m.group(2))
            kind = metric_kind.get(metric)
            cands = by_day.get((kind, day), []) if kind else []          # sorted by record_key: deterministic
            # Correspondence: the SCORED record(s) of that day whose own sample of the metric exists.
            matches = [(f"wh:{metric}:{c['record_key']}", c) for c in cands
                       if c["score_state"] == "SCORED" and f"wh:{metric}:{c['record_key']}" in present]
            if len(matches) == 1:
                sid, c = matches[0]
                out.append(base + [day, "replaced", sid, c["record_key"], r["value"], present[sid], None])
            elif len(matches) > 1:
                out.append(base + [day, "quarantined", None, ",".join(c["record_key"] for _s, c in matches), r["value"], None, "ambiguous_records"])
            else:
                # Definitive without that value: UNSCORABLE, or SCORED whose payload does not derive the metric.
                definitive = [c for c in cands if c["score_state"] == "UNSCORABLE"
                              or (c["score_state"] == "SCORED" and not yields(c, metric))]
                if len(definitive) >= 1:
                    out.append(base + [day, "superseded", None, definitive[0]["record_key"], r["value"], None, None])
                else:
                    out.append(base + [day, "quarantined", None, None, r["value"], None,
                                       "no_record" if not cands else "record_not_definitive"])
        self.con.execute("""CREATE TEMP TABLE lw_out (sample_id VARCHAR, metric VARCHAR, start_old TIMESTAMP, end_old TIMESTAMP, day DATE,
                            outcome VARCHAR, target VARCHAR, record_key VARCHAR, value_legacy DOUBLE, value_record DOUBLE, note VARCHAR)""")
        if out:
            self.con.executemany("INSERT INTO lw_out VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", out)
        f["whoop_day_rows_by_outcome_note"] = self.rows("SELECT metric, outcome, COALESCE(note, ''), COUNT(*) FROM lw_out GROUP BY 1, 2, 3 ORDER BY 1, 2, 3")
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
            SELECT old_id, new_id, reason, TIMESTAMP '{self.stamp.isoformat(sep=' ')}' AS created_at FROM (
                SELECT sample_id AS old_id, 'hk:' || hk_uuid AS new_id, CASE WHEN rn = 1 THEN '{ALIAS_REBASE}' ELSE '{ALIAS_TWIN}' END AS reason FROM lb WHERE era IN (1, 2, 4)
                UNION ALL SELECT x_id, b_id, '{ALIAS_EXPORT}' FROM xlink WHERE outcome = 'linked'
                UNION ALL SELECT sample_id, target, '{wh.ALIAS_REASON}' FROM lw_out WHERE outcome = 'replaced')""")
        con.execute(f"""CREATE TABLE _lineage_tombstones AS
            SELECT o.sample_id AS tomb_id, CAST(NULL AS VARCHAR) AS hk_uuid, o.metric, s.start_utc, '{wh.SUPERSEDED}' AS reason,
                   'migration:' || o.record_key AS batch_id
            FROM lw_out o JOIN samples s ON s.sample_id = o.sample_id WHERE o.outcome = 'superseded'""")
        # The FINAL alias relation (existing rows and the staged ones), the
        # object the gates and the archive judge (checkpoint B points 15, 19).
        con.execute("""CREATE TABLE _lineage_aliases_final AS
            SELECT old_id, new_id, reason, created_at, 'existing' AS origin FROM sample_aliases
            UNION ALL SELECT old_id, new_id, reason, created_at, 'staged' FROM _lineage_aliases""")
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
        # The final alias relation: every target (existing or staged) resolves to
        # a live row or a tombstone (by id, or by uuid for hk targets); no old id
        # maps to two different targets.
        unresolved = self.one("""SELECT COUNT(*) FROM _lineage_aliases_final a LEFT JOIN samples_rebased t ON t.sample_id = a.new_id
            WHERE t.sample_id IS NULL AND a.new_id NOT IN (SELECT tomb_id FROM tombstones) AND a.new_id NOT IN (SELECT tomb_id FROM _lineage_tombstones)
              AND NOT (a.new_id LIKE 'hk:%' AND substr(a.new_id, 4) IN (SELECT hk_uuid FROM tombstones WHERE hk_uuid IS NOT NULL))""")
        self.check("every_alias_in_the_final_relation_resolves_to_a_live_row_or_a_tombstone", unresolved == 0, unresolved)
        conflicting = self.one("SELECT COUNT(*) FROM (SELECT old_id FROM _lineage_aliases_final GROUP BY 1 HAVING COUNT(DISTINCT new_id) > 1)")
        self.check("no_old_id_maps_to_two_targets_in_the_final_relation", conflicting == 0, conflicting)
        # Deletion dominates for both identities: the uuid (Bridge rows) and the
        # native id (Whoop record rows and day rows), existing and staged tombstones.
        tomb = self.one("""SELECT COUNT(*) FROM samples_rebased s WHERE s.hk_uuid IN (SELECT hk_uuid FROM tombstones WHERE hk_uuid IS NOT NULL)
            OR s.sample_id IN (SELECT tomb_id FROM tombstones) OR s.sample_id IN (SELECT tomb_id FROM _lineage_tombstones)""")
        self.check("deletion_dominates_no_staged_row_is_tombstoned_by_uuid_or_id", tomb == 0, tomb)
        # Identity sets, both directions: exactly the expected rows and nothing else.
        self.con.execute("""CREATE OR REPLACE TEMP TABLE expected_ids AS
            SELECT sample_id FROM samples WHERE time_source IS NOT NULL
            UNION ALL SELECT new_id FROM lb_final
            UNION ALL SELECT sample_id FROM lx_final
            UNION ALL SELECT sample_id FROM lw_out WHERE outcome = 'quarantined'""")
        missing = self.one("SELECT COUNT(*) FROM expected_ids e WHERE NOT EXISTS (SELECT 1 FROM samples_rebased t WHERE t.sample_id = e.sample_id)")
        extra = self.one("SELECT COUNT(*) FROM samples_rebased t WHERE NOT EXISTS (SELECT 1 FROM expected_ids e WHERE e.sample_id = t.sample_id)")
        dup_e = self.one("SELECT COUNT(*) - COUNT(DISTINCT sample_id) FROM expected_ids")
        self.check("staged_identity_set_equals_the_expected_set_both_ways", missing == 0 and extra == 0 and dup_e == 0,
                   {"missing": missing, "extra": extra, "duplicate_expected": dup_e})
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
        # Re-read rows carry exactly the landing's fields (the re-read wins, nothing
        # blended) and keep every identity field of the row they came from.
        bad_r = self.one(f"""SELECT COUNT(*) FROM samples_rebased t JOIN hk_reread h ON h.hk_uuid = t.hk_uuid WHERE t.time_source = '{TS_REREAD}'
            AND (t.start_utc IS DISTINCT FROM h.start_utc OR t.end_utc IS DISTINCT FROM h.end_utc OR t.value IS DISTINCT FROM h.value
                 OR t.unit IS DISTINCT FROM h.unit OR t.text_value IS DISTINCT FROM h.text_value OR t.quality IS DISTINCT FROM h.quality
                 OR t.unit_rule IS DISTINCT FROM h.unit_rule OR t.hk_type IS DISTINCT FROM h.hk_type OR t.metric IS DISTINCT FROM h.metric
                 OR t.source_name IS DISTINCT FROM h.source_name OR t.device_key IS DISTINCT FROM h.device_key)""")
        ident = [c for c in self.COLS if c not in (*REBASE_WHITELIST, *REREAD_FIELDS)]
        dist_i = " OR ".join(f"s.{c} IS DISTINCT FROM t.{c}" for c in ident)
        bad_ri = self.one(f"""SELECT COUNT(*) FROM samples s JOIN _lineage_rebased r ON r.old_id = s.sample_id
            JOIN samples_rebased t ON t.sample_id = r.new_id WHERE r.time_source = '{TS_REREAD}' AND ({dist_i})""")
        self.check("reread_rows_equal_the_landing_and_keep_their_identity", bad_r == 0 and bad_ri == 0, {"landing": bad_r, "identity": bad_ri})
        # Post-1a rows byte-identical.
        dist_all = " OR ".join(f"s.{c} IS DISTINCT FROM t.{c}" for c in self.COLS)
        bad_n = self.one(f"SELECT COUNT(*) FROM samples s JOIN samples_rebased t ON t.sample_id = s.sample_id WHERE s.time_source IS NOT NULL AND ({dist_all})")
        self.check("native_rows_byte_identical", bad_n == 0, bad_n)
        bad_render = self.one(f"""SELECT COUNT(*) FROM samples_rebased WHERE time_source IN ('{TS_REBASE}', '{TS_REREAD}', '{TS_EXPORT_LINKED}')
            AND (start_ts IS DISTINCT FROM {wall_sql('start_utc', zone)} OR end_ts IS DISTINCT FROM {wall_sql('end_utc', zone)}
                 OR start_ts IS DISTINCT FROM start_utc + INTERVAL {REPORTING_OFFSET_MIN} MINUTE OR start_utc IS NULL OR end_utc IS NULL)""")
        self.check("reporting_wall_equals_zone_rendering_and_utc_plus_240_on_every_migrated_row", bad_render == 0, bad_render)
        off_case = "CASE r.era " + " ".join(f"WHEN {e} THEN {o}" for e, o in ERA_OFFSET_MIN.items()) + " END"
        bad_off = self.one(f"""SELECT COUNT(*) FROM _lineage_rebased r JOIN samples_rebased t ON t.sample_id = r.new_id
            WHERE t.time_source = '{TS_REBASE}' AND (round((epoch(r.start_old) - epoch(t.start_utc)) / 60.0) <> {off_case}
                  OR round((epoch(r.end_old) - epoch(t.end_utc)) / 60.0) <> {off_case})""")
        self.check("staged_offsets_are_exactly_the_era_offsets_on_both_endpoints", bad_off == 0, bad_off)
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
        # The exact eligibility set (checkpoint B point 16): the view's full
        # predicate on the staged table must equal the input's eligible set mapped
        # through the aliases, minus rows the migration marked, nothing else.
        pred = """s.quality IS NULL AND s.device_key <> 'excluded' AND (s.score_state IS NULL OR s.score_state = 'SCORED')
                  AND s.start_ts IS NOT NULL
                  AND (CASE WHEN s.start_utc IS NOT NULL THEN (s.end_utc IS NULL OR s.end_utc >= s.start_utc) ELSE (s.end_ts IS NULL OR s.end_ts >= s.start_ts) END)
                  AND COALESCE(s.start_utc, s.start_ts) <= timezone('UTC', now()) + INTERVAL 1 DAY"""
        self.con.execute(f"""CREATE OR REPLACE TEMP TABLE elig_expected AS
            SELECT DISTINCT COALESCE(a.new_id, e.sample_id) AS sample_id
            FROM eligible_samples e LEFT JOIN _lineage_aliases a ON a.old_id = e.sample_id
            WHERE e.sample_id NOT IN (SELECT sample_id FROM lw_out WHERE outcome IN ('replaced', 'superseded', 'quarantined'))""")
        self.con.execute(f"""CREATE OR REPLACE TEMP TABLE elig_actual AS
            SELECT s.sample_id FROM samples_rebased s JOIN metric_registry m ON m.metric = s.metric WHERE {pred}""")
        # Rows the migration itself made ineligible (a quality from the re-read or
        # the export link) leave the expected set with their reason recorded.
        marked = self.one("""SELECT COUNT(*) FROM elig_expected x JOIN samples_rebased t ON t.sample_id = x.sample_id WHERE t.quality IS NOT NULL""")
        e_missing = self.one("""SELECT COUNT(*) FROM elig_expected x JOIN samples_rebased t ON t.sample_id = x.sample_id
            WHERE t.quality IS NULL AND NOT EXISTS (SELECT 1 FROM elig_actual a WHERE a.sample_id = x.sample_id)""")
        e_extra = self.one("SELECT COUNT(*) FROM elig_actual a WHERE NOT EXISTS (SELECT 1 FROM elig_expected x WHERE x.sample_id = a.sample_id)")
        f["eligibility_set"] = {"before": self.one("SELECT COUNT(*) FROM eligible_samples"), "expected_after": self.one("SELECT COUNT(*) FROM elig_expected"),
                                "actual_after": self.one("SELECT COUNT(*) FROM elig_actual"), "marked_by_migration": marked,
                                "marked_by_class": dict((r[0], r[1]) for r in self.rows("""SELECT t.quality, COUNT(*) FROM elig_expected x JOIN samples_rebased t
                                    ON t.sample_id = x.sample_id WHERE t.quality IS NOT NULL GROUP BY 1""")),
                                "missing": e_missing, "extra": e_extra}
        self.check("eligible_set_after_equals_the_mapped_set_before_minus_marked_rows", e_missing == 0 and e_extra == 0, f["eligibility_set"])
        # Frozen budgets (plan decision 8; checkpoint B point 14): a quarantine
        # class may not exceed 0.5 percent of its type unless an exception is named.
        xb = self.rows("""SELECT metric, COUNT(*) FILTER (WHERE outcome = 'ambiguous'), COUNT(*) FROM xlink GROUP BY 1 HAVING COUNT(*) FILTER (WHERE outcome = 'ambiguous') > 0 ORDER BY 1""")
        wb = self.rows("""SELECT metric, COUNT(*) FILTER (WHERE outcome = 'quarantined'), COUNT(*) FROM lw_out GROUP BY 1 HAVING COUNT(*) FILTER (WHERE outcome = 'quarantined') > 0 ORDER BY 1""")
        f["budget_export_ambiguous"] = [[m, n, tot, round(100.0 * n / tot, 3)] for m, n, tot in xb]
        f["budget_whoop_quarantined"] = [[m, n, tot, round(100.0 * n / tot, 3)] for m, n, tot in wb]
        x_breach = [r for r in f["budget_export_ambiguous"] if r[3] > BUDGET_PCT]
        w_breach = [r for r in f["budget_whoop_quarantined"] if r[3] > BUDGET_PCT]
        self.check("export_ambiguous_within_budget_per_type", not x_breach or "budget:export" in self.exceptions,
                   {"breaches": x_breach, "exception": "budget:export" in self.exceptions})
        self.check("whoop_quarantine_within_budget_per_type", not w_breach or "budget:whoop" in self.exceptions,
                   {"breaches": w_breach, "exception": "budget:whoop" in self.exceptions})
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
            pop = self.rows("""SELECT rebase_era, strftime(start_ts, '%Y-%m'), COUNT(*) FROM samples_rebased
                WHERE metric IN ('resting_hr', 'body_mass') AND sync_path = 'bridge' AND rebase_era IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2""")
            anchored_pm = self.rows("""SELECT t.rebase_era, strftime(t.start_ts, '%Y-%m'), COUNT(DISTINCT t.sample_id)
                FROM samples_rebased t JOIN ah.records r ON r.record_type = t.hk_type AND r.source_name = t.source_name AND r.value = t.value
                     AND round(epoch(r.end_date) - epoch(r.start_date)) = round(epoch(t.end_ts) - epoch(t.start_ts)) AND abs(epoch(r.start_date) - epoch(t.start_ts)) <= 43200
                WHERE t.metric IN ('resting_hr', 'body_mass') AND t.sync_path = 'bridge' AND t.rebase_era IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2""")
            am = {(e, ym): n for e, ym, n in anchored_pm}
            f["ah_anchor_coverage_by_era_month"] = [[e, ym, n, am.get((e, ym), 0)] for e, ym, n in pop]
            f["ah_anchor_unanchored_partitions"] = [[e, ym, n] for e, ym, n in pop if am.get((e, ym), 0) == 0]
            self.check("ah_anchor_nearest_match_delta_zero_for_every_anchored_row", anchored > 0 and nonzero == 0,
                       {"anchored": anchored, "nonzero": nonzero, "by_era": nearest, "unanchored_partitions": len(f["ah_anchor_unanchored_partitions"])})
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
            self.check("ah_steps_per_dubai_day_equal", r[0] == r[1] and r[0] >= r[2], f["ah_steps_days"], fatal=(self.label == "apply"))
        finally:
            self.con.execute("DETACH ah")

    # ---- 9. lineage archive (before the cutover) ----
    def archive(self) -> None:
        f = self.R["facts"]
        written = {}
        places = {d.resolve() for d in self.archive_dirs}
        self.check("archive_places_distinct", len(places) == len(self.archive_dirs), [str(d) for d in self.archive_dirs])
        # One immutable, fingerprint-qualified folder per place: a later run
        # cannot overwrite a reviewed archive (checkpoint B point 19).
        sub = f"{MIGRATION}-{f['input_fingerprint'][:12]}"
        self.archive_paths = []
        for d in self.archive_dirs:
            d = d / sub
            self.check(f"archive_folder_new_{len(self.archive_paths) + 1}", not d.exists(), str(d))
            d.mkdir(parents=True, exist_ok=False)
            self.archive_paths.append(d)
            lines = []
            for t in LINEAGE_TABLES:
                p = d / f"{t.lstrip('_')}.parquet"
                self.con.execute(f"COPY (SELECT * FROM {t}) TO '{p}' (FORMAT PARQUET)")
                lines.append(f"{sha256_file(p)}  {p.name}")
            (d / "MANIFEST.sha256").write_text("\n".join(lines) + "\n")
            written[str(d)] = lines
        manifests = {k: sorted(v) for k, v in written.items()}
        self.check("lineage_archive_identical_in_every_place", len({json.dumps(v) for v in manifests.values()}) == 1, {"places": list(manifests)})
        f["archive_places"] = [str(p) for p in self.archive_paths]
        f["archive_manifest"] = sorted(next(iter(manifests.values())))
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
        summary = {"label": self.label, "phase": PHASE_CUTOVER, "input_fingerprint": f["input_fingerprint"], "policy_digest": f["policy_digest"],
                   "zone": self.zone, "constants": self.R["constants"], "flags": self.R["flags"], "code_commit": self.code_commit,
                   "code_dirty": self.code_dirty, "rows_before": f["samples_before"], "rows_after": f["rows_after"],
                   "staged_digest": f["staged_digest"], "columns": self.COLS, "twins": f["twin_uuids"],
                   "compare_classes": f.get("compare_classes"), "export_link_totals": f.get("export_link_totals"),
                   "whoop": f.get("whoop_day_rows_by_outcome"), "aliases": f["aliases_staged"], "duckdb": f["duckdb"],
                   "archive_places": f.get("archive_places"), "archive_manifest": f.get("archive_manifest"),
                   "out_dir": str(self.out)}
        self.summary = summary
        reread_ddl = self.one("SELECT sql FROM duckdb_tables() WHERE table_name = 'hk_reread'")
        now = self.stamp
        t0 = time.time()
        con.execute("BEGIN")
        try:
            con.execute("DROP VIEW IF EXISTS eligible_samples")
            con.execute("DROP TABLE samples")
            con.execute("ALTER TABLE samples_rebased RENAME TO samples")
            con.execute("INSERT OR IGNORE INTO sample_aliases (old_id, new_id, reason, created_at) SELECT old_id, new_id, reason, created_at FROM _lineage_aliases")
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

    def mark_verified(self) -> None:
        """The verification (reopen checks, rebuild, diff, oracle) passed: the
        migrations row says so. Until then the row says cutover_committed and
        the daemon must not be started on this store (checkpoint B point 1)."""
        f = self.R["facts"]
        con = duckdb.connect(str(self.path))
        try:
            row = con.execute("SELECT summary FROM migrations WHERE name = ?", [MIGRATION]).fetchone()
            summary = json.loads(row[0]) if row and row[0] else {}
            summary.update({"phase": PHASE_VERIFIED, "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "verification": {"rebuild_counts": f.get("rebuild_counts"), "derived_diff_unexplained_cells": f.get("derived_diff_unexplained_cells"),
                                             "oracle": f.get("oracle"), "eligible_rows_after": f.get("eligible_rows_after")}})
            con.execute("UPDATE migrations SET summary = ? WHERE name = ?", [json.dumps(summary, default=str), MIGRATION])
            con.execute("CHECKPOINT")
        finally:
            con.close()
        f["migration_phase"] = PHASE_VERIFIED

    def reopen_verify(self) -> None:
        """Reopen through the daemon's path twice (schema.sql recreates the view
        and the uuid index), verify the swap persisted and is terminal."""
        f = self.R["facts"]
        if self.resume_verify:
            # Expectations come from the committed migrations row.
            f["rows_after"] = self.applied_summary["rows_after"]
            f["staged_digest"] = self.applied_summary["staged_digest"]
            self.COLS = self.applied_summary["columns"]
            self.archive_paths = [Path(p) for p in self.applied_summary["archive_places"]]
            f["archive_places"] = self.applied_summary["archive_places"]
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
        lin = self.archive_paths[0]
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
        """Independent numerical verification of EVERY rebuilt daily value
        (checkpoint B points 4, 5, 6): the expected cells are recomputed from
        samples with the eligibility contract spelled out here (never the view
        or the dispatcher), the view's one scaling rule applied per row before
        aggregation, exact decimal arithmetic in Python, the winner chosen by
        the policy priority, and compared with daily_values in both directions
        (missing, extra), on value (finite, within 0.0015), device and sample
        count. The report holds counts and a delta distribution only; cell
        identities of mismatches go to a separate private file."""
        f = self.R["facts"]
        pol = self.policy
        now_utc = datetime.utcnow()
        elig = """quality IS NULL AND device_key <> 'excluded' AND (score_state IS NULL OR score_state = 'SCORED')
                  AND start_ts IS NOT NULL
                  AND (CASE WHEN start_utc IS NOT NULL THEN (end_utc IS NULL OR end_utc >= start_utc) ELSE (end_ts IS NULL OR end_ts >= start_ts) END)
                  AND COALESCE(start_utc, start_ts) <= ?"""
        scaled = "CASE WHEN metric = 'body_fat_pct' AND unit_rule IS NULL AND value IS NOT NULL AND value <= 1.5 THEN value * 100.0 ELSE value END"
        registered = {r[0] for r in self.rows("SELECT metric FROM metric_registry")}
        expected: dict[tuple, tuple] = {}
        for metric in pol.metrics:
            if not pol.daily(metric) or metric not in registered:
                continue
            prio = pol.priority(metric)
            if not prio:
                continue
            per_day: dict[date, dict[str, tuple]] = {}
            if metric == "sleep_duration":
                direct = self.rows(f"""SELECT CAST(end_ts AS DATE), device_key, MAX(value) FROM samples WHERE metric = 'sleep_duration' AND value IS NOT NULL
                    AND {elig} AND device_key IN (SELECT unnest(?)) GROUP BY 1, 2""", [now_utc + timedelta(days=1), prio])
                staged = self.rows(f"""SELECT CAST(end_ts AS DATE), device_key,
                        list(CAST(value AS DECIMAL(30,6))) FILTER (WHERE text_value IN ('core', 'deep', 'rem')),
                        list(CAST(value AS DECIMAL(30,6))) FILTER (WHERE text_value = 'asleep')
                    FROM samples WHERE metric = 'sleep_analysis' AND device_key <> 'whoop' AND value IS NOT NULL
                    AND {elig} AND device_key IN (SELECT unnest(?)) GROUP BY 1, 2""", [now_utc + timedelta(days=1), prio])
                vals: dict[tuple, list] = {}
                for d, dk, hrs in direct:
                    vals.setdefault((d, dk), [Decimal(0), Decimal(0), Decimal(0)])[0] = Decimal(str(hrs))
                for d, dk, sub, asleep in staged:
                    v = vals.setdefault((d, dk), [Decimal(0), Decimal(0), Decimal(0)])
                    v[1] = sum((Decimal(str(x)) for x in (sub or [])), Decimal(0)) / Decimal(60)
                    v[2] = sum((Decimal(str(x)) for x in (asleep or [])), Decimal(0)) / Decimal(60)
                for (d, dk), (hrs, sub, asleep) in vals.items():
                    staged_v = sub if sub > 0 else asleep
                    v = max(hrs, staged_v)
                    if v > 0:
                        per_day.setdefault(d, {})[dk] = (round(float(v), 2), 1)
            else:
                agg = pol.agg(metric)
                rows = self.rows(f"""SELECT CAST(start_ts AS DATE), device_key, list(CAST({scaled} AS DECIMAL(30,6)) ORDER BY start_ts, sample_id)
                    FROM samples WHERE metric = ? AND value IS NOT NULL AND {elig} AND device_key IN (SELECT unnest(?)) GROUP BY 1, 2""",
                                 [metric, now_utc + timedelta(days=1), prio])
                for d, dk, lst in rows:
                    ds = [Decimal(str(x)) for x in lst]
                    if agg == "sum":
                        v = float(round(sum(ds, Decimal(0)), 3))
                    elif agg == "avg":
                        v = round(float(sum(ds, Decimal(0))) / len(ds), 3)
                    elif agg == "min":
                        v = round(float(min(ds)), 3)
                    elif agg == "max":
                        v = round(float(max(ds)), 3)
                    else:
                        v = round(float(ds[-1]), 3)
                    per_day.setdefault(d, {})[dk] = (v, len(ds))
            for d, per_dev in per_day.items():
                primary = next((dk for dk in prio if dk in per_dev), None)
                if primary is not None:
                    expected[(str(d), metric)] = (per_dev[primary][0], primary, per_dev[primary][1])
        actual = {(str(d), m): (v, dk, n) for d, m, v, dk, n in self.rows("SELECT date, metric, value, device_key, n_samples FROM daily_values")}
        missing = sorted(k for k in expected if k not in actual)
        extra = sorted(k for k in actual if k not in expected)
        value_mism, device_mism, n_mism, non_finite = [], [], [], []
        deltas = []
        for k, (ev, edk, en) in expected.items():
            if k not in actual:
                continue
            av, adk, an = actual[k]
            if av is None or not math.isfinite(av):
                non_finite.append(k)
                continue
            delta = abs(av - ev)
            deltas.append(delta)
            if delta > 0.0015:
                value_mism.append(k)
            if adk != edk:
                device_mism.append(k)
            if an != en:
                n_mism.append(k)
        buckets = {"0": 0, "<=1e-3": 0, "<=1e-2": 0, "<=1": 0, ">1": 0}
        for dl in deltas:
            buckets["0" if dl == 0 else "<=1e-3" if dl <= 1e-3 else "<=1e-2" if dl <= 1e-2 else "<=1" if dl <= 1 else ">1"] += 1
        bad_cells = missing + extra + value_mism + device_mism + n_mism + non_finite
        by_metric: dict[str, int] = {}
        for _d, mm in bad_cells:
            by_metric[mm] = by_metric.get(mm, 0) + 1
        bad = len(bad_cells)
        f["oracle"] = {"expected_cells": len(expected), "actual_cells": len(actual), "compared": len(deltas),
                       "missing": len(missing), "extra": len(extra), "value_mismatch": len(value_mism), "device_mismatch": len(device_mism),
                       "n_samples_mismatch": len(n_mism), "non_finite": len(non_finite), "mismatches": bad, "delta_distribution": buckets,
                       "by_metric_mismatch": dict(sorted(by_metric.items()))}
        # Cell identities (dates and metrics, no values) for the operator; not part of the report.
        (self.out / f"private-oracle-cells-{self.label}.json").write_text(json.dumps(
            {"missing": missing, "extra": extra, "value_mismatch": value_mism, "device_mismatch": device_mism,
             "n_samples_mismatch": n_mism, "non_finite": non_finite}, default=str))
        self.check("independent_oracle_matches_every_rebuilt_daily_value_both_ways", len(expected) > 0 and bad == 0,
                   {k: v for k, v in f["oracle"].items() if k != "by_metric_mismatch"})

    # ---- driver ----
    def run(self) -> dict:
        try:
            with self.step("open"):
                self.open()
            if self.resume_verify:
                self.COLS = [r[0] for r in self.con.execute("DESCRIBE samples").fetchall()]
                self.con.close()
                self.con = None
                with self.step("reopen_verify"):
                    self.reopen_verify()
                with self.step("rebuild"):
                    self.rebuild()
                self.mark_verified()
                raise _Done()
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
                    self.mark_verified()
                else:
                    self.R["facts"]["migration_phase"] = PHASE_CUTOVER
            else:
                with self.step("cleanup_staging"):
                    self.cleanup_staging()
                    self.con.execute("CHECKPOINT")
        except _Done:
            pass
        except Stop as e:
            self.R["stopped"] = str(e)
            self.log(f"STOP: {e}")
            if self.con is not None:
                try:
                    self.cleanup_staging()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            # Any other failure: recorded like a stop, staging cleaned, the
            # report still written, then re-raised for the log (checkpoint B 24).
            self.R["stopped"] = f"error: {type(e).__name__}: {str(e)[:300]}"
            self.log(f"ERROR: {self.R['stopped']}")
            if self.con is not None:
                try:
                    self.cleanup_staging()
                except Exception:  # noqa: BLE001
                    pass
            self.R["error"] = True
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
        if self.R.get("error"):
            raise RuntimeError(self.R["stopped"])
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
