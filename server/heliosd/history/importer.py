"""The Phase 2 history import (single-source plan v2 sections 2.5, 4.2, 4.5 and 5).

import_exports() reads one or more raw Apple Health export folders (export.xml,
electrocardiograms/, workout-routes/, attachments/ and the folder's sha256
manifest) into the schema v5 tables of an open store, checks the result in
both directions and returns the reconciliation report (counts, identifiers
and dates only, never a measured value).

Contract:
- The samples table and everything derived from it are never written. The
  importer fingerprints them (row count and a hash of every row) before and
  after and stops when anything differs.
- Registered types keep their existing path. Their records go to raw_records
  like every other record, and each is linked one to one to the samples row
  that already holds it with export_relink's rule (same metric, device,
  source, unit, text and instants; values within the rule's tolerance):
  first a Bridge row, then an earlier export row. What links nothing is
  listed with the reason (one of them: inside a longer stored row, a finer
  copy of time the store already covers); it changes no daily value.
- Idempotent: every identity is content based (raw_records, workout_sources)
  or native (an ECG's instant, a ring's date, a route's points), and a row
  whose identity is already stored is not written again. A second run of the
  same folders writes nothing.
- All or nothing: the writes, the checks and the migrations row
  phase2_history_import_v1 (phase verified) happen in one transaction; a
  failed check rolls everything back.
- An export folder whose manifest lists a file that is missing or differs
  stops the run before anything is written. The exports are only ever read.
- Ignored sources are stored with device_key 'excluded' (registry store mode,
  plan v2 4.2) whatever the registry's own ignored_mode says.

Bulk rows travel through CSV files in a private staging folder (mode 0700)
that is removed when the run ends; executemany is three orders of magnitude
slower. So the connection must allow DuckDB to read files (the daemon's
connection does not: open the store with duckdb.connect and db.init_schema).
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from heliosd.history import export_xml as ex
from heliosd.history import sidefiles as sf
from heliosd.history import workouts as wk
from heliosd.ingest.normalize import normalize_sample
from heliosd.migrate import export_relink as xr
from heliosd.store import db

MIGRATION = "phase2_history_import_v1"
PHASE_VERIFIED = "verified"
NULL = "\\N"
# Tables the import must leave exactly as they were (the samples path and everything derived from it).
PROTECTED_TABLES = ("samples", "daily_values", "baselines", "device_baselines", "signals", "tombstones",
                    "sample_aliases", "content_twins", "dirty_dates", "derived_generation", "whoop_records",
                    "whoop_cache", "hk_reread", "hk_reread_variants", "metric_registry", "narratives", "events",
                    "actions", "labs")
EXAMPLE_DATES = 5
# Five example instants of a group without pulling the group into Python.
QUANTILES = ("MIN({col}), quantile_disc({col}, 0.25), quantile_disc({col}, 0.5), quantile_disc({col}, 0.75), MAX({col})")


class Stop(Exception):
    """A check failed or an input is not what the contract expects: nothing is kept."""


# ---------------------------------------------------------------- staging

def _cell(v) -> str:
    if v is None:
        return NULL
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(repr(float(x)) for x in v) + "]"
    s = str(v)
    if s == NULL:
        raise Stop("a text value equals the staging null marker")
    return s


class Staged:
    """One staging CSV with declared DuckDB column types."""

    def __init__(self, root: Path, name: str, columns: list[tuple[str, str]]):
        self.path = root / f"{name}.csv"
        self.columns = columns
        self.n = 0
        self._f = open(self.path, "w", newline="", encoding="utf-8")
        self._w = csv.writer(self._f, lineterminator="\n")

    def add(self, values: list) -> None:
        self._w.writerow([_cell(v) for v in values])
        self.n += 1

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()

    def sql(self) -> str:
        """A relation over the staged rows. List columns are staged as text and cast by the caller."""
        self.close()
        cols = ", ".join(f"'{c}': '{'VARCHAR' if t.endswith('[]') else t}'" for c, t in self.columns)
        return (f"read_csv('{self.path}', header=false, delim=',', quote='\"', escape='\"', nullstr='{NULL}', "
                f"allow_quoted_nulls=false, auto_detect=false, new_line='\\n', columns={{{cols}}})")


class Stage:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.tables: list[Staged] = []

    def table(self, name: str, columns: list[tuple[str, str]]) -> Staged:
        t = Staged(self.root, name, columns)
        self.tables.append(t)
        return t

    def remove(self) -> None:
        for t in self.tables:
            t.close()
        shutil.rmtree(self.root, ignore_errors=True)


# ---------------------------------------------------------------- helpers

def _q(conn, sql: str, params=None) -> list[tuple]:
    return conn.execute(sql, params or []).fetchall()


def _one(conn, sql: str, params=None):
    return conn.execute(sql, params or []).fetchone()[0]


def table_digest(conn, table: str) -> list:
    cols = [r[0] for r in _q(conn, f'DESCRIBE "{table}"')]
    expr = ", ".join(f'"{c}"' for c in cols)
    n, h = conn.execute(f'SELECT COUNT(*), CAST(bit_xor(hash({expr})) AS VARCHAR) FROM "{table}"').fetchone()
    return [n, h]


def protected_digests(conn) -> dict[str, list]:
    have = {r[0] for r in _q(conn, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'")}
    return {t: table_digest(conn, t) for t in PROTECTED_TABLES if t in have}


def _dates(values, n: int = EXAMPLE_DATES) -> list[str]:
    out = sorted({v.date().isoformat() if isinstance(v, datetime) else str(v) for v in values if v is not None})
    if len(out) <= n:
        return out
    step = (len(out) - 1) / (n - 1)
    return [out[round(i * step)] for i in range(n)]


def _jdump(v) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str)


def _export_label(folder: Path) -> str:
    return folder.name


# ---------------------------------------------------------------- inputs

def survey(folder: Path) -> dict:
    """The export folder's files and their checksums; stops on a manifest mismatch or a missing listed file."""
    if not folder.is_dir():
        raise Stop(f"no export folder at {folder}")
    man = sf.verify_manifest(folder)
    if man["manifest"] and (man["mismatch"] or man["missing"]):
        raise Stop(f"{folder.name}: manifest {man['manifest']}: {len(man['mismatch'])} file(s) differ, "
                   f"{len(man['missing'])} missing; nothing was imported")
    files: list[dict] = []
    for kind, sub, pattern in (("export_xml", ".", "export.xml"), ("ecg_csv", "electrocardiograms", "*.csv"),
                               ("route_gpx", "workout-routes", "*.gpx"), ("attachment", "attachments", "*")):
        d = folder / sub
        if not d.is_dir():
            continue
        for p in sorted(d.glob(pattern)):
            if not p.is_file() or p.name.startswith("."):
                continue
            rel = p.relative_to(folder).as_posix()
            files.append({"kind": kind, "rel_path": rel, "path": p, "size_bytes": p.stat().st_size,
                          "file_sha": man["digests"].get(rel) or sf.sha256_file(p)})
    unlisted = [f["rel_path"] for f in files if man["manifest"] and f["rel_path"] not in man["digests"]]
    return {"label": _export_label(folder), "folder": str(folder), "manifest": man["manifest"],
            "manifest_listed": man["listed"], "manifest_ok": man["ok"], "unlisted": unlisted, "files": files}


# ---------------------------------------------------------------- the import

RAW_COLS = [("ident", "VARCHAR"), ("ordinal", "INTEGER"), ("element", "VARCHAR"), ("hk_type", "VARCHAR"),
            ("metric", "VARCHAR"), ("value_raw", "VARCHAR"), ("value", "DOUBLE"), ("unit", "VARCHAR"),
            ("source_name", "VARCHAR"), ("source_version", "VARCHAR"), ("device", "VARCHAR"), ("device_key", "VARCHAR"),
            ("creation_raw", "VARCHAR"), ("start_raw", "VARCHAR"), ("end_raw", "VARCHAR"),
            ("creation_utc", "TIMESTAMP"), ("start_utc", "TIMESTAMP"), ("end_utc", "TIMESTAMP"),
            ("print_offset_min", "INTEGER"), ("metadata", "VARCHAR"), ("children", "VARCHAR"),
            ("sync_identifier", "VARCHAR"), ("sync_version", "BIGINT")]
REG_COLS = [("ordinal", "INTEGER"), ("metric", "VARCHAR"), ("device_key", "VARCHAR"), ("source_name", "VARCHAR"),
            ("unit", "VARCHAR"), ("text_value", "VARCHAR"), ("start_utc", "TIMESTAMP"), ("end_utc", "TIMESTAMP"),
            ("value", "DOUBLE"), ("quality", "VARCHAR")]
GROUP_KEY_SQL = ("metric", "device_key", "source_name", "unit", "text_value", "start_utc", "end_utc")


def _key_eq(a: str, b: str, b_unit: str = "unit") -> str:
    return (f"{a}.metric = {b}.metric AND {a}.device_key = {b}.device_key AND {a}.source_name = {b}.source_name "
            f"AND {a}.unit IS NOT DISTINCT FROM {b}.{b_unit} AND {a}.text_value IS NOT DISTINCT FROM {b}.text_value "
            f"AND {a}.start_utc = {b}.start_utc AND {a}.end_utc IS NOT DISTINCT FROM {b}.end_utc")


def _within_sql(a: str, b: str) -> str:
    exact = ", ".join(f"'{m}'" for m in xr.EXACT_METRICS)
    return (f"(({a}.value IS NULL AND {b}.value IS NULL) OR ({a}.value IS NOT NULL AND {b}.value IS NOT NULL AND "
            f"CASE WHEN {a}.metric IN ({exact}) THEN {a}.value = {b}.value "
            f"ELSE abs({a}.value - {b}.value) <= {xr.TOLERANCE!r} + 1e-9 END))")


def _link_registered(conn, log: Callable[[str], None]) -> dict:
    """Fill temp table p2_link(ordinal, link_status, linked_sample_id, link_reason) for every staged registered
    record (p2_reg), pass one against Bridge rows and pass two against earlier export rows, one to one per pass
    with export_relink's rule. Returns counts."""
    conn.execute("""CREATE TEMP TABLE p2_cand AS
        SELECT s.sample_id, s.sync_path, s.metric, s.device_key, s.source_name, s.unit AS raw_unit, s.text_value,
               s.start_utc, s.end_utc,
               CASE WHEN s.metric = 'body_fat_pct' AND s.unit_rule IS NULL AND s.value IS NOT NULL AND s.value <= 1.5
                    THEN s.value * 100.0 ELSE s.value END AS value
        FROM samples s
        WHERE s.start_utc IS NOT NULL AND s.sync_path IN ('bridge', 'health_export')
          AND EXISTS (SELECT 1 FROM p2_reg r WHERE r.metric = s.metric AND r.start_utc = s.start_utc)""")
    conn.execute("CREATE TEMP TABLE p2_link (ordinal INTEGER PRIMARY KEY, link_status VARCHAR, linked_sample_id VARCHAR, "
                 "link_reason VARCHAR)")
    conn.execute("INSERT INTO p2_link SELECT ordinal, 'none', NULL, 'ignored_source' FROM p2_reg WHERE device_key = 'excluded'")
    out = {}
    for path, status in (("bridge", "bridge"), ("health_export", "export")):
        t0 = time.time()
        conn.execute("DROP TABLE IF EXISTS p2_x")
        conn.execute("DROP TABLE IF EXISTS p2_b")
        conn.execute(f"""CREATE TEMP TABLE p2_x AS SELECT r.*, COUNT(*) OVER (PARTITION BY {', '.join(GROUP_KEY_SQL)}) AS nx
                         FROM p2_reg r WHERE r.ordinal NOT IN (SELECT ordinal FROM p2_link)""")
        conn.execute(f"""CREATE TEMP TABLE p2_b AS SELECT c.*, COUNT(*) OVER (PARTITION BY metric, device_key, source_name,
                             raw_unit, text_value, start_utc, end_utc) AS nb
                         FROM p2_cand c WHERE c.sync_path = '{path}'
                           AND c.sample_id NOT IN (SELECT linked_sample_id FROM p2_link WHERE linked_sample_id IS NOT NULL)
                           AND EXISTS (SELECT 1 FROM p2_x x WHERE {_key_eq('x', 'c', 'raw_unit')})""")
        # groups of one on each side: the rule reduces to "within tolerance"
        n1 = conn.execute(f"""INSERT INTO p2_link
            SELECT x.ordinal, '{status}', b.sample_id, NULL FROM p2_x x JOIN p2_b b ON {_key_eq('x', 'b', 'raw_unit')}
            WHERE x.nx = 1 AND b.nb = 1 AND {_within_sql('x', 'b')}""").fetchone()[0]
        # larger groups: export_relink.pair, the rule itself
        xs = [dict(zip(["sample_id", *GROUP_KEY_SQL, "value"], r)) for r in _q(conn, f"""
            SELECT CAST(x.ordinal AS VARCHAR), {', '.join('x.' + k for k in GROUP_KEY_SQL)}, x.value FROM p2_x x
            WHERE EXISTS (SELECT 1 FROM p2_b b WHERE {_key_eq('x', 'b', 'raw_unit')} AND (x.nx > 1 OR b.nb > 1))""")]
        for x in xs:
            x["raw_unit"] = x.pop("unit")
        bs = [dict(zip(["sample_id", "metric", "device_key", "source_name", "raw_unit", "text_value", "start_utc",
                        "end_utc", "value"], r)) | {"linked": False} for r in _q(conn, f"""
            SELECT b.sample_id, b.metric, b.device_key, b.source_name, b.raw_unit, b.text_value, b.start_utc, b.end_utc,
                   b.value FROM p2_b b
            WHERE EXISTS (SELECT 1 FROM p2_x x WHERE {_key_eq('x', 'b', 'raw_unit')} AND (x.nx > 1 OR b.nb > 1))""")]
        links, _unlinked = xr.pair(xs, bs)
        if links:
            conn.execute("INSERT INTO p2_link SELECT o, ?, s, NULL FROM (SELECT unnest(?) AS o, unnest(?) AS s)",
                         [status, [int(x_id) for x_id, _b, _d in links], [b_id for _x, b_id, _d in links]])
        out[path] = {"one_to_one_groups": n1, "larger_group_records": len(xs), "larger_group_links": len(links),
                     "seconds": round(time.time() - t0, 1)}
        log(f"  link pass {path}: {n1:,} single pairs, {len(links):,} of {len(xs):,} in larger groups ({out[path]['seconds']} s)")
    # the rest: why nothing holds them
    conn.execute(f"""INSERT INTO p2_link
        SELECT r.ordinal, 'none', NULL,
               CASE WHEN NOT EXISTS (SELECT 1 FROM p2_cand c WHERE {_key_eq('r', 'c', 'raw_unit')})
                    THEN CASE WHEN EXISTS (SELECT 1 FROM p2_cand c WHERE c.metric = r.metric AND c.source_name = r.source_name
                                           AND c.start_utc = r.start_utc AND c.end_utc IS NOT DISTINCT FROM r.end_utc)
                              THEN 'store_row_differs_in_unit_text_or_device'
                              WHEN EXISTS (SELECT 1 FROM tombstones t WHERE t.metric = r.metric AND t.start_utc = r.start_utc)
                              THEN 'deleted_in_store' ELSE 'no_store_row_at_the_same_instants' END
                    WHEN NOT EXISTS (SELECT 1 FROM p2_cand c WHERE {_key_eq('r', 'c', 'raw_unit')} AND {_within_sql('r', 'c')})
                    THEN 'no_value_within_tolerance'
                    ELSE 'store_rows_all_claimed' END
        FROM p2_reg r WHERE r.ordinal NOT IN (SELECT ordinal FROM p2_link)""")
    # A record with no row at its instants that lies inside a longer stored row of the same metric, device
    # and source is a finer copy of time the store already covers (seen 2026-10-20: workout-time energy slices
    # of 3 s that Apple Health later replaced with rows of minutes). Counting it again would double the time.
    n_inside = conn.execute("""
        UPDATE p2_link SET link_reason = 'inside_a_longer_store_row'
        WHERE link_reason = 'no_store_row_at_the_same_instants' AND ordinal IN (
            WITH u AS (SELECT r.ordinal, r.metric, r.device_key, r.source_name, r.start_utc, r.end_utc,
                              CAST(r.start_utc AS DATE) AS d
                       FROM p2_reg r JOIN p2_link l USING (ordinal)
                       WHERE l.link_reason = 'no_store_row_at_the_same_instants' AND r.end_utc IS NOT NULL),
                 s AS (SELECT s.metric, s.device_key, s.source_name, s.start_utc, s.end_utc, CAST(s.start_utc AS DATE) AS d
                       FROM samples s
                       WHERE s.sync_path IN ('bridge', 'health_export') AND s.start_utc IS NOT NULL AND s.end_utc IS NOT NULL
                         AND EXISTS (SELECT 1 FROM u WHERE u.metric = s.metric AND u.device_key = s.device_key
                                     AND u.source_name = s.source_name))
            SELECT u.ordinal FROM u JOIN s ON s.metric = u.metric AND s.device_key = u.device_key
                   AND s.source_name = u.source_name AND (s.d = u.d OR s.d = u.d - 1)
            WHERE s.start_utc <= u.start_utc AND s.end_utc >= u.end_utc
              AND (s.end_utc - s.start_utc) > (u.end_utc - u.start_utc))""").fetchone()[0]
    out["inside_a_longer_store_row"] = n_inside
    dup = _one(conn, "SELECT COUNT(*) - COUNT(DISTINCT linked_sample_id) FROM p2_link WHERE linked_sample_id IS NOT NULL")
    if dup:
        raise Stop(f"one to one broken: {dup} store rows claimed twice")
    return out


def _parse_export_xml(path: Path, file_sha: str, stage: Stage, policy, registry, log) -> dict:
    """Stream export.xml into staging (raw_records and the registered keys); keep the workouts and the
    summaries in memory (they are small). Returns what was read."""
    raw = stage.table(f"raw_{file_sha[:12]}", RAW_COLS)
    reg = stage.table(f"reg_{file_sha[:12]}", REG_COLS)
    wanted = policy.hk_to_metric
    kinds: Counter = Counter()
    types: Counter = Counter()
    workouts: list[dict] = []
    summaries: list[dict] = []
    correlations = {"n": 0, "members": 0, "member_idents": []}
    info = {"locale": None, "export_date": None, "me": 0}
    bad_time: Counter = Counter()
    t0 = time.time()
    seen = 0
    for ordinal, kind, row in ex.iter_export(path):
        kinds[kind] += 1
        seen += 1
        if kind == "Record" or kind == "Correlation" or kind in ex.GENERIC_ELEMENTS:
            dk = registry.resolve(row["source_name"]) if row.get("source_name") else None
            metric = wanted.get(row["hk_type"]) if kind == "Record" else None
            children = row.get("children")
            if kind == "Correlation":
                members = [{k: m[k] for k in ("ident", "hk_type", "unit", "value_raw", "source_name", "source_version",
                                              "device", "creation_raw", "start_raw", "end_raw", "metadata")}
                           for m in row["members"]]
                children = _jdump({"members": members})
                correlations["n"] += 1
                correlations["members"] += len(members)
                correlations["member_idents"].extend(m["ident"] for m in members)
            types[(row["element"], row["hk_type"])] += 1
            if kind in ("Record", "Correlation") and (row["start_utc"] is None or (row["end_raw"] and row["end_utc"] is None)):
                bad_time[row["hk_type"]] += 1
            raw.add([row["ident"], ordinal, row["element"], row["hk_type"], metric, row["value_raw"], row["value"],
                     row["unit"], row["source_name"], row["source_version"], row["device"], dk, row["creation_raw"],
                     row["start_raw"], row["end_raw"], row["creation_utc"], row["start_utc"], row["end_utc"],
                     row["print_offset_min"], row["metadata"], children, row["sync_identifier"], row["sync_version"]])
            if metric and row["start_utc"] is not None:
                n = normalize_sample({"hk_type": row["hk_type"], "source_name": row["source_name"], "unit": row["unit"],
                                      "value": row["value_raw"],
                                      "start": row["start_utc"].replace(tzinfo=timezone.utc),
                                      "end": row["end_utc"].replace(tzinfo=timezone.utc) if row["end_utc"] else None},
                                     policy, registry, "health_export")
                if n is not None:
                    reg.add([ordinal, n["metric"], n["device_key"], n["source_name"], n["unit"], n["text_value"],
                             n["start_utc"], n["end_utc"], n["value"], n["quality"]])
        elif kind == "Workout":
            row["device_key"] = registry.resolve(row["source_name"]) if row.get("source_name") else None
            workouts.append(row)
        elif kind == "ActivitySummary":
            summaries.append(row)
        elif kind == "ExportDate":
            info["export_date"] = row["value"]
        elif kind == "HealthData":
            info["locale"] = row["locale"]
        elif kind == "Me":
            info["me"] += 1
        if seen % 1_000_000 == 0:
            log(f"  read {seen:,} elements ({time.time() - t0:.0f} s)")
    log(f"  read {seen:,} elements in {time.time() - t0:.0f} s")
    return {"raw": raw, "reg": reg, "kinds": kinds, "types": types, "workouts": workouts, "summaries": summaries,
            "correlations": correlations, "info": info, "bad_time": bad_time, "seconds": round(time.time() - t0, 1)}


def import_exports(conn, folders: list[Path], policy, registry, stage_root: Path, run_id: str,
                   code_commit: str | None = None, log: Callable[[str], None] = print) -> dict:
    """Import the export folders into the open store; return the report. Raises Stop (after a rollback) when an
    input or a check is not right. `registry` must resolve ignored sources to 'excluded' (store mode)."""
    t_all = time.time()
    rep: dict = {"run_id": run_id, "code_commit": code_commit, "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "migration": MIGRATION, "inputs": [], "checks": {}, "stopped": None}
    inputs = [survey(Path(f)) for f in folders]
    labels = [i["label"] for i in inputs]
    if len(set(labels)) != len(labels):
        raise Stop("two export folders with the same name")
    for i in inputs:
        rep["inputs"].append({k: v for k, v in i.items() if k != "files"} |
                             {"files": Counter(f["kind"] for f in i["files"])})
        if i["unlisted"]:
            raise Stop(f"{i['label']}: {len(i['unlisted'])} file(s) not in the manifest")
    fingerprint = hashlib.sha256("\n".join(sorted(f"{i['label']}/{f['rel_path']}:{f['file_sha']}"
                                                   for i in inputs for f in i["files"])).encode()).hexdigest()
    rep["input_fingerprint"] = fingerprint
    stage = Stage(stage_root)
    conn.execute("BEGIN")
    try:
        before = protected_digests(conn)
        rep["protected_tables"] = {t: v[0] for t, v in before.items()}
        log(f"protected tables fingerprinted ({len(before)})")
        counts: dict[str, dict] = defaultdict(lambda: {"written": 0, "already_present": 0})
        xml_reads: list[dict] = []
        workout_pool: dict[str, dict] = {}
        gpx_by_label: dict[str, dict[str, dict]] = {}
        # ---- export.xml files
        for i in inputs:
            for f in (f for f in i["files"] if f["kind"] == "export_xml"):
                log(f"{i['label']}: reading export.xml")
                r = _parse_export_xml(f["path"], f["file_sha"], stage, policy, registry, log)
                r["label"], r["file_sha"] = i["label"], f["file_sha"]
                xml_reads.append(r)
        # ---- route files (all exports), so workouts can name them
        route_points: dict[str, dict] = {}
        for i in inputs:
            gpx_by_label[i["label"]] = {}
            for f in (f for f in i["files"] if f["kind"] == "route_gpx"):
                g = sf.read_gpx(f["path"])
                gpx_by_label[i["label"]][f["rel_path"]] = g | {"file_sha": f["file_sha"]}
                route_points.setdefault(g["route_id"], g)
        # ---- raw records, with the registered links
        for r in xml_reads:
            log(f"{r['label']}: staging {r['raw'].n:,} raw rows, {r['reg'].n:,} registered")
            r["tmp"] = tmp = f"p2_raw_{r['file_sha'][:12]}"
            conn.execute("DROP TABLE IF EXISTS p2_reg")
            conn.execute(f"""CREATE TEMP TABLE {tmp} AS
                SELECT 'xr:' || ident || ':' || CAST(row_number() OVER (PARTITION BY ident ORDER BY ordinal) - 1 AS VARCHAR)
                       AS record_id, * FROM {r['raw'].sql()}""")
            conn.execute(f"CREATE TEMP TABLE p2_reg AS SELECT * FROM {r['reg'].sql()}")
            new = _one(conn, f"SELECT COUNT(*) FROM {tmp} WHERE record_id NOT IN (SELECT record_id FROM raw_records)")
            if new:
                r["link"] = _link_registered(conn, log)
            else:
                conn.execute("CREATE TEMP TABLE p2_link (ordinal INTEGER, link_status VARCHAR, linked_sample_id VARCHAR, "
                             "link_reason VARCHAR)")
                r["link"] = {}
            n = conn.execute(f"""INSERT INTO raw_records
                SELECT p.record_id, p.element, p.hk_type, p.metric, p.value_raw, p.value, p.unit, p.source_name,
                       p.source_version, p.device, p.device_key, p.creation_raw, p.start_raw, p.end_raw, p.creation_utc,
                       p.start_utc, p.end_utc, p.print_offset_min, p.metadata, p.children, p.sync_identifier,
                       p.sync_version,
                       CASE WHEN p.metric IS NULL THEN NULL ELSE COALESCE(l.link_status, 'none') END,
                       l.linked_sample_id,
                       CASE WHEN p.metric IS NULL THEN NULL
                            WHEN l.ordinal IS NULL THEN 'not_normalized' ELSE l.link_reason END,
                       '{r['file_sha']}', p.ordinal, '{run_id}'
                FROM {tmp} p LEFT JOIN p2_link l ON l.ordinal = p.ordinal
                WHERE p.record_id NOT IN (SELECT record_id FROM raw_records)""").fetchone()[0]
            counts["raw_records"]["written"] += n
            counts["raw_records"]["already_present"] += _one(conn, f"SELECT COUNT(*) FROM {tmp}") - n
            r["registered_link_summary"] = _registered_summary(conn, r)
            r["correlation_members_found"] = _one(conn, f"""SELECT COUNT(*) FROM (SELECT unnest(?) AS ident) m
                WHERE m.ident IN (SELECT ident FROM {tmp} WHERE element = 'Record')""",
                                                  [r["correlations"]["member_idents"]]) if r["correlations"]["members"] else 0
            conn.execute("DROP TABLE IF EXISTS p2_link")
            conn.execute("DROP TABLE IF EXISTS p2_cand")
            log(f"  raw_records: {n:,} written")
            # workouts of this file: content identity, numbered in document order
            seen: Counter = Counter()
            for w in r["workouts"]:
                k = seen[w["ident"]]
                seen[w["ident"]] += 1
                w["source_id"] = f"xw:{w['ident']}:{k}"
                w["file_sha"], w["label"] = r["file_sha"], r["label"]
                w["family"] = wk.source_family(w.get("device_key"))
                workout_pool.setdefault(w["source_id"], w)
        # ---- workouts: sources, routes, sessions, queue
        stored = {x[0] for x in _q(conn, "SELECT source_id FROM workout_sources")}
        if stored - set(workout_pool):
            raise Stop(f"{len(stored - set(workout_pool))} stored workout source rows are not in these exports: run the "
                       "importer with every export folder at once so the sessions can be rebuilt")
        route_refs: dict[str, list[dict]] = {}
        ref_count: Counter = Counter()
        for w in workout_pool.values():
            for rt in w["routes"]:
                for path in rt["file_refs"]:
                    ref_count[(w["label"], path.lstrip("/"))] += 1
        for w in workout_pool.values():
            for rt in w["routes"]:
                refs = rt["file_refs"] or [None]
                for j, path in enumerate(refs):
                    rel = path.lstrip("/") if path else None
                    g = gpx_by_label.get(w["label"], {}).get(rel) if rel else None
                    status = "missing_file" if g is None else ("shared_file" if ref_count[(w["label"], rel)] > 1 else "linked")
                    suffix = f":{j}" if len(refs) > 1 else ""
                    route_refs.setdefault(w["source_id"], []).append(
                        {"route_ref_id": f"{w['source_id']}:route:{rt['ord']}{suffix}", "ord": rt["ord"], "file_ref": path,
                         "route_id": g["route_id"] if g else None, "link_status": status, **{k: rt[k] for k in (
                             "source_name", "source_version", "device", "creation_raw", "start_raw", "end_raw",
                             "start_utc", "end_utc", "metadata")}})
        built = wk.build_sessions(list(workout_pool.values()), route_refs)
        _write_workouts(conn, stage, workout_pool, route_refs, built, run_id, counts)
        # ---- route points
        st = stage.table("route_points", [("route_id", "VARCHAR"), ("seq", "INTEGER"), ("time_raw", "VARCHAR"),
                                          ("time_utc", "TIMESTAMP"), ("lat", "DOUBLE"), ("lon", "DOUBLE"),
                                          ("ele", "DOUBLE"), ("speed", "DOUBLE"), ("course", "DOUBLE"),
                                          ("h_acc", "DOUBLE"), ("v_acc", "DOUBLE")])
        have_routes = {x[0] for x in _q(conn, "SELECT DISTINCT route_id FROM route_points")}
        for rid, g in route_points.items():
            if rid in have_routes:
                counts["route_points"]["already_present"] += g["n_points"]
                continue
            for p in g["points"]:
                st.add([rid, p["seq"], p["time_raw"], p["time_utc"], p["lat"], p["lon"], p["ele"], p["speed"],
                        p["course"], p["h_acc"], p["v_acc"]])
        if st.n:
            counts["route_points"]["written"] += conn.execute(f"INSERT INTO route_points SELECT * FROM {st.sql()}").fetchone()[0]
        # ---- activity summaries
        _write_summaries(conn, stage, xml_reads, run_id, counts, rep)
        # ---- ECGs
        ecg_files = _write_ecgs(conn, stage, inputs, run_id, counts, rep)
        # ---- export_files
        _write_files(conn, inputs, xml_reads, gpx_by_label, ecg_files, run_id, counts)
        # ---- checks, both directions
        log("checks")
        after = protected_digests(conn)
        rep["checks"] = _checks(conn, inputs, xml_reads, workout_pool, route_refs, route_points, built, before, after)
        failed = [k for k, v in rep["checks"].items() if not v.get("ok")]
        rep["reconciliation"] = _reconciliation(conn, xml_reads, workout_pool, route_refs, built, rep)
        rep["counts"] = {k: dict(v) for k, v in counts.items()}
        rep["seconds"] = round(time.time() - t_all, 1)
        if failed:
            raise Stop(f"checks failed: {failed}")
        conn.execute("INSERT INTO import_runs VALUES (?, ?, ?, ?, ?, ?, 'complete')",
                     [run_id, datetime.fromisoformat(rep["started"]).replace(tzinfo=None), datetime.now(timezone.utc).replace(tzinfo=None),
                      code_commit, _jdump(rep["inputs"]), _jdump(rep["counts"])])
        if not _q(conn, "SELECT 1 FROM migrations WHERE name = ?", [MIGRATION]):
            summary = {"phase": PHASE_VERIFIED, "run_id": run_id, "counts": rep["counts"],
                       "checks": sorted(rep["checks"]), "exports": labels}
            conn.execute("INSERT INTO migrations (name, applied_at, code_commit, input_fingerprint, summary) VALUES (?, ?, ?, ?, ?)",
                         [MIGRATION, datetime.now(timezone.utc).replace(tzinfo=None), code_commit, fingerprint, _jdump(summary)])
            rep["migration_row"] = "written"
        else:
            rep["migration_row"] = "already present"
        conn.execute("COMMIT")
    except BaseException as e:
        try:
            conn.execute("ROLLBACK")
        except Exception as rb:                          # noqa: BLE001 - recorded beside the first error
            rep["rollback_error"] = f"{type(rb).__name__}: {str(rb)[:200]}"
        rep["stopped"] = f"{type(e).__name__}: {str(e)[:500]}"
        rep["seconds"] = round(time.time() - t_all, 1)
        if isinstance(e, Stop):
            e.report = rep            # type: ignore[attr-defined]
        raise
    finally:
        stage.remove()
    rep["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return rep


def _registered_summary(conn, r: dict) -> dict:
    """Per metric and device: records, linked to a Bridge row, to an earlier export row, and not linked by reason,
    as stored (a re-run reads what the first run linked)."""
    out: dict = {}
    tmp = r["tmp"]
    for metric, dk, status, reason, n in _q(conn, f"""
            SELECT s.metric, s.device_key, s.link_status, s.link_reason, COUNT(*)
            FROM {tmp} p JOIN raw_records s USING (record_id) WHERE s.metric IS NOT NULL GROUP BY ALL"""):
        c = out.setdefault(f"{metric}|{dk}", {"metric": metric, "device_key": dk, "records": 0, "bridge": 0, "export": 0,
                                              "none": {}, "none_dates": []})
        c["records"] += n
        if status in ("bridge", "export"):
            c[status] += n
        else:
            c["none"][reason or "unknown"] = c["none"].get(reason or "unknown", 0) + n
    for metric, dk, *starts in _q(conn, f"""
            SELECT s.metric, s.device_key, {QUANTILES.format(col='s.start_utc')} FROM {tmp} p
            JOIN raw_records s USING (record_id) WHERE s.link_status = 'none' GROUP BY ALL"""):
        out[f"{metric}|{dk}"]["none_dates"] = _dates(starts)
    return dict(sorted(out.items()))


def _write_workouts(conn, stage: Stage, pool: dict, route_refs: dict, built: dict, run_id: str, counts) -> None:
    have = {x[0] for x in _q(conn, "SELECT source_id FROM workout_sources")}
    src = stage.table("workout_sources", [(c, t) for c, t in (
        ("source_id", "VARCHAR"), ("workout_id", "VARCHAR"), ("role", "VARCHAR"), ("link_rule", "VARCHAR"),
        ("family", "VARCHAR"), ("activity_type", "VARCHAR"), ("activity_family", "VARCHAR"), ("duration_raw", "VARCHAR"),
        ("duration_unit", "VARCHAR"), ("duration_s", "DOUBLE"), ("total_distance_raw", "VARCHAR"),
        ("total_distance_unit", "VARCHAR"), ("total_energy_raw", "VARCHAR"), ("total_energy_unit", "VARCHAR"),
        ("source_name", "VARCHAR"), ("source_version", "VARCHAR"), ("device", "VARCHAR"), ("device_key", "VARCHAR"),
        ("creation_raw", "VARCHAR"), ("start_raw", "VARCHAR"), ("end_raw", "VARCHAR"), ("creation_utc", "TIMESTAMP"),
        ("start_utc", "TIMESTAMP"), ("end_utc", "TIMESTAMP"), ("print_offset_min", "INTEGER"), ("tz_name", "VARCHAR"),
        ("sync_identifier", "VARCHAR"), ("sync_version", "BIGINT"), ("match_detail", "VARCHAR"),
        ("independence", "VARCHAR"), ("file_sha", "VARCHAR"), ("ordinal", "INTEGER"), ("run_id", "VARCHAR"))])
    stats = stage.table("workout_statistics", [("source_id", "VARCHAR"), ("ord", "INTEGER"), ("hk_type", "VARCHAR"),
                                               ("start_raw", "VARCHAR"), ("end_raw", "VARCHAR"), ("start_utc", "TIMESTAMP"),
                                               ("end_utc", "TIMESTAMP"), ("stat_sum", "DOUBLE"), ("stat_avg", "DOUBLE"),
                                               ("stat_min", "DOUBLE"), ("stat_max", "DOUBLE"), ("unit", "VARCHAR")])
    events = stage.table("workout_events", [("source_id", "VARCHAR"), ("ord", "INTEGER"), ("event_type", "VARCHAR"),
                                            ("date_raw", "VARCHAR"), ("date_utc", "TIMESTAMP"), ("duration", "DOUBLE"),
                                            ("duration_unit", "VARCHAR"), ("metadata", "VARCHAR")])
    meta = stage.table("workout_metadata", [("source_id", "VARCHAR"), ("ord", "INTEGER"), ("meta_key", "VARCHAR"),
                                            ("meta_value", "VARCHAR")])
    routes = stage.table("workout_routes", [("route_ref_id", "VARCHAR"), ("source_id", "VARCHAR"), ("ord", "INTEGER"),
                                            ("file_ref", "VARCHAR"), ("route_id", "VARCHAR"), ("link_status", "VARCHAR"),
                                            ("source_name", "VARCHAR"), ("source_version", "VARCHAR"),
                                            ("device", "VARCHAR"), ("creation_raw", "VARCHAR"), ("start_raw", "VARCHAR"),
                                            ("end_raw", "VARCHAR"), ("start_utc", "TIMESTAMP"), ("end_utc", "TIMESTAMP"),
                                            ("metadata", "VARCHAR"), ("run_id", "VARCHAR")])
    for sid, w in pool.items():
        if sid in have:
            counts["workout_sources"]["already_present"] += 1
            continue
        p = built["sources"][sid]
        src.add([sid, p["workout_id"], p["role"], p["link_rule"], w["family"], w["activity_type"],
                 wk.activity_family(w["activity_type"]), w["duration_raw"], w["duration_unit"], w["duration_s"],
                 w["total_distance_raw"], w["total_distance_unit"], w["total_energy_raw"], w["total_energy_unit"],
                 w["source_name"], w["source_version"], w["device"], w.get("device_key"), w["creation_raw"],
                 w["start_raw"], w["end_raw"], w["creation_utc"], w["start_utc"], w["end_utc"], w["print_offset_min"],
                 w["tz_name"], w["sync_identifier"], w["sync_version"], _jdump(p["match_detail"]), p["independence"],
                 w["file_sha"], w["ordinal"], run_id])
        for s in w["statistics"]:
            stats.add([sid, s["ord"], s["hk_type"], s["start_raw"], s["end_raw"], s["start_utc"], s["end_utc"],
                       s["stat_sum"], s["stat_avg"], s["stat_min"], s["stat_max"], s["unit"]])
        for e in w["events"]:
            events.add([sid, e["ord"], e["event_type"], e["date_raw"], e["date_utc"], e["duration"], e["duration_unit"],
                        e["metadata"]])
        for j, (k, v) in enumerate(w["metadata"]):
            meta.add([sid, j, k, v])
        for rr in route_refs.get(sid, []):
            routes.add([rr["route_ref_id"], sid, rr["ord"], rr["file_ref"], rr["route_id"], rr["link_status"],
                        rr["source_name"], rr["source_version"], rr["device"], rr["creation_raw"], rr["start_raw"],
                        rr["end_raw"], rr["start_utc"], rr["end_utc"], rr["metadata"], run_id])
    for name, t in (("workout_sources", src), ("workout_statistics", stats), ("workout_events", events),
                    ("workout_metadata", meta), ("workout_routes", routes)):
        if t.n:
            counts[name]["written"] += conn.execute(f"INSERT INTO {name} SELECT * FROM {t.sql()}").fetchone()[0]
    have_w = {x[0] for x in _q(conn, "SELECT workout_id FROM workouts")}
    ws = stage.table("workouts", [(c, t) for c, t in (
        ("workout_id", "VARCHAR"), ("status", "VARCHAR"), ("n_sources", "INTEGER"), ("activity_type", "VARCHAR"),
        ("activity_family", "VARCHAR"), ("type_source", "VARCHAR"), ("start_utc", "TIMESTAMP"), ("end_utc", "TIMESTAMP"),
        ("duration_s", "DOUBLE"), ("timing_source", "VARCHAR"), ("tz_name", "VARCHAR"), ("tz_source", "VARCHAR"),
        ("distance", "DOUBLE"), ("distance_unit", "VARCHAR"), ("distance_type", "VARCHAR"),
        ("distance_source", "VARCHAR"), ("energy_kcal", "DOUBLE"), ("energy_unit", "VARCHAR"),
        ("energy_source", "VARCHAR"), ("hr_avg", "DOUBLE"), ("hr_min", "DOUBLE"), ("hr_max", "DOUBLE"),
        ("hr_unit", "VARCHAR"), ("hr_source", "VARCHAR"), ("strain", "DOUBLE"), ("strain_source", "VARCHAR"),
        ("route_id", "VARCHAR"), ("route_source", "VARCHAR"), ("rule_version", "VARCHAR"), ("run_id", "VARCHAR"))])
    for s in built["sessions"]:
        if s["workout_id"] in have_w:
            counts["workouts"]["already_present"] += 1
            continue
        ws.add([s["workout_id"], s["status"], s["n_sources"], s["activity_type"], s["activity_family"], s["type_source"],
                s["start_utc"], s["end_utc"], s["duration_s"], s["timing_source"], s["tz_name"], s["tz_source"],
                s["distance"], s["distance_unit"], s["distance_type"], s["distance_source"], s["energy_kcal"],
                s["energy_unit"], s["energy_source"], s["hr_avg"], s["hr_min"], s["hr_max"], s["hr_unit"],
                s["hr_source"], s["strain"], s["strain_source"], s["route_id"], s["route_source"], s["rule_version"],
                run_id])
    if ws.n:
        counts["workouts"]["written"] += conn.execute(f"INSERT INTO workouts SELECT * FROM {ws.sql()}").fetchone()[0]
    have_q = {x[0] for x in _q(conn, "SELECT item_id FROM workout_ambiguity")}
    qs = stage.table("workout_ambiguity", [("item_id", "VARCHAR"), ("kind", "VARCHAR"), ("source_ids", "VARCHAR"),
                                           ("candidate_ids", "VARCHAR"), ("reason", "VARCHAR"), ("detail", "VARCHAR"),
                                           ("status", "VARCHAR"), ("run_id", "VARCHAR")])
    for q in built["queue"]:
        if q["item_id"] in have_q:
            counts["workout_ambiguity"]["already_present"] += 1
            continue
        qs.add([q["item_id"], q["kind"], _jdump(q["source_ids"]), _jdump(q["candidate_ids"]), q["reason"],
                _jdump(q["detail"]), "open", run_id])
    if qs.n:
        counts["workout_ambiguity"]["written"] += conn.execute(f"INSERT INTO workout_ambiguity SELECT * FROM {qs.sql()}").fetchone()[0]


def _write_summaries(conn, stage: Stage, xml_reads: list[dict], run_id: str, counts, rep: dict) -> None:
    have = {r[0]: r[1:] for r in _q(conn, "SELECT summary_id, active_energy, move_time, exercise_time, stand_hours "
                                          "FROM activity_summaries")}
    t = stage.table("activity_summaries", [("summary_id", "VARCHAR"), ("date_components", "VARCHAR"), ("day", "DATE"),
                                           ("active_energy", "DOUBLE"), ("active_energy_goal", "DOUBLE"),
                                           ("active_energy_unit", "VARCHAR"), ("move_time", "DOUBLE"),
                                           ("move_time_goal", "DOUBLE"), ("exercise_time", "DOUBLE"),
                                           ("exercise_time_goal", "DOUBLE"), ("stand_hours", "DOUBLE"),
                                           ("stand_hours_goal", "DOUBLE"), ("partial", "BOOLEAN"), ("file_sha", "VARCHAR"),
                                           ("ordinal", "INTEGER"), ("run_id", "VARCHAR")])
    conflicts, seen = [], set()
    for r in xml_reads:
        export_day = (r["info"]["export_date"] or "")[:10]
        for s in r["summaries"]:
            sid = f"as:{s['date_components']}"
            if sid in have or sid in seen:
                old = have.get(sid)
                if old is not None and tuple(old) != (s["active_energy"], s["move_time"], s["exercise_time"], s["stand_hours"]):
                    conflicts.append(s["date_components"])
                counts["activity_summaries"]["already_present"] += 1
                continue
            seen.add(sid)
            t.add([sid, s["date_components"], s["day"], s["active_energy"], s["active_energy_goal"], s["active_energy_unit"],
                   s["move_time"], s["move_time_goal"], s["exercise_time"], s["exercise_time_goal"], s["stand_hours"],
                   s["stand_hours_goal"], s["date_components"] == export_day, r["file_sha"], s["ordinal"], run_id])
    if t.n:
        counts["activity_summaries"]["written"] += conn.execute(f"INSERT INTO activity_summaries SELECT * FROM {t.sql()}").fetchone()[0]
    rep["summary_conflicts"] = {"n": len(conflicts), "dates": _dates(conflicts)}


def _write_ecgs(conn, stage: Stage, inputs: list[dict], run_id: str, counts, rep: dict) -> dict:
    have = {r[0]: r[1] for r in _q(conn, "SELECT ecg_id, voltage_sha FROM ecgs")}
    head = stage.table("ecgs", [(c, t) for c, t in (
        ("ecg_id", "VARCHAR"), ("recorded_raw", "VARCHAR"), ("recorded_utc", "TIMESTAMP"), ("print_offset_min", "INTEGER"),
        ("classification", "VARCHAR"), ("symptoms", "VARCHAR"), ("software_version", "VARCHAR"), ("device", "VARCHAR"),
        ("sample_rate_hz", "DOUBLE"), ("sample_rate_raw", "VARCHAR"), ("lead", "VARCHAR"), ("unit", "VARCHAR"),
        ("n_samples", "INTEGER"), ("voltage_sha", "VARCHAR"), ("export_label", "VARCHAR"), ("rel_path", "VARCHAR"),
        ("file_sha", "VARCHAR"), ("run_id", "VARCHAR"))])
    wave = stage.table("ecg_waveforms", [("ecg_id", "VARCHAR"), ("lead", "VARCHAR"), ("unit", "VARCHAR"),
                                         ("sample_rate_hz", "DOUBLE"), ("n_samples", "INTEGER"), ("microvolts", "DOUBLE[]")])
    files: dict[tuple[str, str], dict] = {}
    conflicts, rates, twins = [], Counter(), 0
    for i in inputs:
        for f in (f for f in i["files"] if f["kind"] == "ecg_csv"):
            e = sf.read_ecg(f["path"])
            files[(i["label"], f["rel_path"])] = e
            if e["ecg_id"] in have:
                if have[e["ecg_id"]] != e["voltage_sha"]:
                    conflicts.append(e["recorded_utc"])
                else:
                    twins += 1
                counts["ecgs"]["already_present"] += 1
                continue
            have[e["ecg_id"]] = e["voltage_sha"]
            rates[e["sample_rate_hz"]] += 1
            head.add([e["ecg_id"], e["recorded_raw"], e["recorded_utc"], e["print_offset_min"], e["classification"],
                      e["symptoms"], e["software_version"], e["device"], e["sample_rate_hz"], e["sample_rate_raw"],
                      e["lead"], e["unit"], e["n_samples"], e["voltage_sha"], i["label"], f["rel_path"], f["file_sha"],
                      run_id])
            wave.add([e["ecg_id"], e["lead"], e["unit"], e["sample_rate_hz"], e["n_samples"], e["microvolts"]])
    if head.n:
        counts["ecgs"]["written"] += conn.execute(f"INSERT INTO ecgs SELECT * FROM {head.sql()}").fetchone()[0]
        counts["ecg_waveforms"]["written"] += conn.execute(
            f"INSERT INTO ecg_waveforms SELECT ecg_id, lead, unit, sample_rate_hz, n_samples, CAST(microvolts AS DOUBLE[]) "
            f"FROM {wave.sql()}").fetchone()[0]
    rep["ecg"] = {"files": len(files), "new_by_rate_hz": {str(k): v for k, v in sorted(rates.items())},
                  "same_recording_in_another_file": twins, "conflicts": len(conflicts), "conflict_dates": _dates(conflicts)}
    return files


def _write_files(conn, inputs, xml_reads, gpx_by_label, ecg_files, run_id, counts) -> None:
    have = {(r[0], r[1]) for r in _q(conn, "SELECT export_label, rel_path FROM export_files")}
    xml_info = {(r["label"], "export.xml"): r for r in xml_reads}
    rows = []
    for i in inputs:
        for f in i["files"]:
            key = (i["label"], f["rel_path"])
            content_id = n_items = export_date = None
            if f["kind"] == "export_xml":
                r = xml_info[key]
                n_items, export_date = sum(v for k, v in r["kinds"].items() if k != "HealthData"), r["info"]["export_date"]
            elif f["kind"] == "ecg_csv":
                e = ecg_files[key]
                content_id, n_items = e["ecg_id"], e["n_samples"]
            elif f["kind"] == "route_gpx":
                g = gpx_by_label[i["label"]][f["rel_path"]]
                content_id, n_items = g["route_id"], g["n_points"]
            if key in have:
                counts["export_files"]["already_present"] += 1
                continue
            rows.append([i["label"], f["rel_path"], f["file_sha"], f["kind"], f["size_bytes"], content_id, n_items,
                         export_date, run_id])
    if rows:
        conn.executemany("INSERT INTO export_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        counts["export_files"]["written"] += len(rows)


# ---------------------------------------------------------------- checks and the report

def _check(ok: bool, **detail) -> dict:
    return {"ok": bool(ok), **detail}


def _checks(conn, inputs, xml_reads, pool, route_refs, route_points, built, before, after) -> dict:
    c: dict[str, dict] = {}
    changed = sorted(t for t in before if before[t] != after.get(t))
    c["protected_tables_unchanged"] = _check(not changed, changed=changed, tables=len(before))
    for r in xml_reads:
        lab = r["label"]
        parsed = sum(n for (el, _t), n in r["types"].items())
        stored = _one(conn, "SELECT COUNT(*) FROM raw_records WHERE file_sha = ?", [r["file_sha"]])
        tmp = r["tmp"]
        missing = _one(conn, f"SELECT COUNT(*) FROM {tmp} WHERE record_id NOT IN (SELECT record_id FROM raw_records)")
        extra = _one(conn, f"SELECT COUNT(*) FROM raw_records WHERE file_sha = ? AND record_id NOT IN "
                           f"(SELECT record_id FROM {tmp})", [r["file_sha"]])
        c[f"{lab}: raw records, export to store"] = _check(missing == 0, parsed=parsed, missing_in_store=missing)
        c[f"{lab}: raw records, store to export"] = _check(extra == 0 and stored + _one(conn,
            f"SELECT COUNT(*) FROM {tmp} p JOIN raw_records s USING (record_id) WHERE s.file_sha <> ?", [r["file_sha"]])
            == parsed, stored_for_this_file=stored, not_in_export=extra)
        by_type = {(el, t): n for el, t, n in _q(conn, f"SELECT element, hk_type, COUNT(*) FROM {tmp} GROUP BY ALL")}
        c[f"{lab}: every type counted the same in both directions"] = _check(
            by_type == {k: v for k, v in r["types"].items()}, types=len(by_type))
        n_bad = sum(r["bad_time"].values())
        c[f"{lab}: every instant readable"] = _check(n_bad == 0, unreadable=n_bad)
        regs = _one(conn, f"SELECT COUNT(*) FROM {tmp} WHERE metric IS NOT NULL")
        linked = _one(conn, "SELECT COUNT(*) FROM raw_records WHERE file_sha = ? AND metric IS NOT NULL AND link_status IS NOT NULL",
                      [r["file_sha"]])
        c[f"{lab}: every registered record has a link status"] = _check(
            linked == _one(conn, "SELECT COUNT(*) FROM raw_records WHERE file_sha = ? AND metric IS NOT NULL", [r["file_sha"]]),
            registered=regs)
        m = r["correlations"]["members"]
        c[f"{lab}: every correlation member is also a top-level record"] = _check(
            r.get("correlation_members_found", 0) == m, members=m, found=r.get("correlation_members_found", 0))
    # workouts
    ids = set(pool)
    st_ids = {x[0] for x in _q(conn, "SELECT source_id FROM workout_sources")}
    c["workout sources, both directions"] = _check(ids == st_ids, parsed=len(ids), stored=len(st_ids))
    for table, key in (("workout_statistics", "statistics"), ("workout_events", "events"), ("workout_metadata", "metadata")):
        parsed = sum(len(w[key]) for w in pool.values())
        stored = _one(conn, f"SELECT COUNT(*) FROM {table}")
        c[f"{table}, both directions"] = _check(parsed == stored, parsed=parsed, stored=stored)
    parsed_refs = sum(len(v) for v in route_refs.values())
    c["workout_routes, both directions"] = _check(parsed_refs == _one(conn, "SELECT COUNT(*) FROM workout_routes"),
                                                  parsed=parsed_refs)
    pts = sum(g["n_points"] for g in route_points.values())
    c["route_points, both directions"] = _check(
        pts == _one(conn, "SELECT COUNT(*) FROM route_points")
        and set(route_points) == {x[0] for x in _q(conn, "SELECT DISTINCT route_id FROM route_points")},
        points=pts, routes=len(route_points))
    sess = {s["workout_id"] for s in built["sessions"]}
    c["every source in a stored session, every session with a source"] = _check(
        sess == {x[0] for x in _q(conn, "SELECT workout_id FROM workouts")}
        and _one(conn, "SELECT COUNT(*) FROM workout_sources s WHERE s.workout_id NOT IN (SELECT workout_id FROM workouts)") == 0
        and _one(conn, "SELECT COUNT(*) FROM workouts w WHERE w.workout_id NOT IN (SELECT workout_id FROM workout_sources)") == 0,
        sessions=len(sess))
    prov = []
    for field, src in (("activity_type", "type_source"), ("start_utc", "timing_source"), ("tz_name", "tz_source"),
                       ("distance", "distance_source"), ("energy_kcal", "energy_source"), ("hr_avg", "hr_source"),
                       ("hr_max", "hr_source"), ("strain", "strain_source")):
        bad = _one(conn, f"""SELECT COUNT(*) FROM workouts w WHERE w.{field} IS NOT NULL AND (w.{src} IS NULL OR w.{src}
                             NOT IN (SELECT source_id FROM workout_sources s WHERE s.workout_id = w.workout_id))""")
        if bad:
            prov.append(f"{field}:{bad}")
    bad = _one(conn, """SELECT COUNT(*) FROM workouts w WHERE w.route_id IS NOT NULL AND (w.route_source IS NULL OR
                        w.route_source NOT IN (SELECT r.route_ref_id FROM workout_routes r JOIN workout_sources s
                        USING (source_id) WHERE s.workout_id = w.workout_id AND r.route_id = w.route_id))""")
    if bad:
        prov.append(f"route_id:{bad}")
    c["every canonical field names its source row"] = _check(not prov, missing=prov)
    # rings and ECGs
    for r in xml_reads:
        n = len(r["summaries"])
        stored = _one(conn, "SELECT COUNT(*) FROM activity_summaries WHERE file_sha = ?", [r["file_sha"]])
        present = _one(conn, "SELECT COUNT(*) FROM activity_summaries WHERE summary_id IN (SELECT unnest(?))",
                       [[f"as:{s['date_components']}" for s in r["summaries"]]])
        c[f"{r['label']}: activity summaries, both directions"] = _check(present == n and stored <= n, parsed=n, stored=stored)
    ecg_ids = _one(conn, "SELECT COUNT(*) FROM ecgs")
    c["ecg waveforms match their records"] = _check(
        _one(conn, "SELECT COUNT(*) FROM ecgs e JOIN ecg_waveforms w USING (ecg_id) WHERE w.n_samples = e.n_samples "
                   "AND len(w.microvolts) = e.n_samples AND w.sample_rate_hz = e.sample_rate_hz") == ecg_ids, ecgs=ecg_ids)
    files = sum(len(i["files"]) for i in inputs)
    keys = [[i["label"], f["rel_path"]] for i in inputs for f in i["files"]]
    present = sum(1 for k in keys if _q(conn, "SELECT 1 FROM export_files WHERE export_label = ? AND rel_path = ?", k))
    c["every input file recorded"] = _check(present == files, files=files)
    return c


def _reconciliation(conn, xml_reads, pool, route_refs, built, rep) -> dict:
    out: dict = {"export_xml": []}
    for r in xml_reads:
        export_instant, _ = ex.parse_ts(r["info"]["export_date"])
        kinds = {k: v for k, v in r["kinds"].items() if k != "HealthData"}
        types = defaultdict(int)
        for (el, t), n in r["types"].items():
            types[f"{el}:{t}"] += n
        registered = {t for t, n in _q(conn, f"SELECT hk_type, COUNT(*) FROM {r['tmp']} WHERE metric IS NOT NULL GROUP BY 1")}
        store_side = []
        if export_instant is not None:
            # Every stored row of a registered HealthKit type from before the export: claimed by a record of
            # this export, covered through its Bridge twin (an earlier export's copy whose Bridge row a record
            # claimed), or not in this export. Example dates are of the rows not in it.
            unq = "s.sample_id NOT IN (SELECT sid FROM claimed) AND s.sample_id NOT IN (SELECT sid FROM twin)"
            qd = ", ".join(f"{fn} FILTER (WHERE {unq})" for fn in (
                "MIN(s.start_utc)", "quantile_disc(s.start_utc, 0.25)", "quantile_disc(s.start_utc, 0.5)",
                "quantile_disc(s.start_utc, 0.75)", "MAX(s.start_utc)"))
            for metric, path, quality, dk, n, cl, tw, *d in _q(conn, f"""
                    WITH claimed AS (SELECT linked_sample_id AS sid FROM raw_records WHERE file_sha = ?
                                     AND linked_sample_id IS NOT NULL),
                         twin AS (SELECT a.old_id AS sid FROM sample_aliases a
                                  WHERE a.reason IN ('export_link_v1', 'export_link_v2')
                                    AND a.new_id IN (SELECT sid FROM claimed))
                    SELECT s.metric, s.sync_path, COALESCE(s.quality, 'eligible'), s.device_key, COUNT(*),
                           COUNT(*) FILTER (WHERE s.sample_id IN (SELECT sid FROM claimed)),
                           COUNT(*) FILTER (WHERE s.sample_id IN (SELECT sid FROM twin)
                                            AND s.sample_id NOT IN (SELECT sid FROM claimed)), {qd}
                    FROM samples s JOIN metric_registry m USING (metric)
                    WHERE s.sync_path IN ('bridge', 'health_export') AND s.start_utc < ? AND m.hk IS NOT NULL
                    GROUP BY ALL ORDER BY 1, 2, 3, 4""", [r["file_sha"], export_instant]):
                store_side.append({"metric": metric, "sync_path": path, "quality": quality, "device_key": dk, "rows": n,
                                   "claimed": cl, "covered_through_twin": tw, "not_in_export": n - cl - tw,
                                   "dates_not_in_export": _dates(d)})
        out["export_xml"].append({
            "export": r["label"], "export_date": r["info"]["export_date"], "locale": r["info"]["locale"],
            "elements": kinds, "records_by_type": dict(sorted(types.items())),
            "registered_types": sorted(registered), "unregistered_types": len({t for (el, t) in r["types"]
                                                                              if el == "Record"} - registered),
            "me_not_imported": r["info"]["me"],
            "correlations": {"n": r["correlations"]["n"], "members": r["correlations"]["members"],
                             "members_found_top_level": r.get("correlation_members_found")},
            "registered_links": r.get("registered_link_summary", {}), "link_passes": r.get("link", {}),
            "store_rows_before_the_export": store_side,
            "seconds_to_read": r["seconds"]})
    sess = built["sessions"]
    status = Counter(s["status"] for s in sess)
    fam_by_session: dict[str, set] = defaultdict(set)
    for sid, p in built["sources"].items():
        fam_by_session[p["workout_id"]].add(pool[sid]["family"])
    combos = Counter("+".join(sorted(fam_by_session[s["workout_id"]])) for s in sess)
    examples: dict[str, list] = defaultdict(list)
    for s in sess:
        examples[s["status"]].append(s["start_utc"])
    queue = Counter(q["kind"] for q in built["queue"])
    qdates: dict[str, list] = defaultdict(list)
    for q in built["queue"]:
        qdates[q["kind"]].extend(pool[sid]["start_utc"] for sid in q["source_ids"] if sid in pool)
    fields = {f: sum(1 for s in sess if s.get(f) is not None) for f in ("activity_type", "start_utc", "tz_name", "distance",
                                                                       "energy_kcal", "hr_avg", "strain", "route_id")}
    owners: dict[str, Counter] = {}
    for f, srcf in (("activity_type", "type_source"), ("start_utc", "timing_source"), ("distance", "distance_source"),
                    ("energy_kcal", "energy_source"), ("hr_avg", "hr_source"), ("strain", "strain_source")):
        owners[f] = Counter(pool[s[srcf]]["family"] for s in sess if s.get(srcf))
    refs = [x for v in route_refs.values() for x in v]
    out["workouts"] = {
        "source_records": len(pool), "by_family": dict(Counter(w["family"] for w in pool.values())),
        "by_activity_type": dict(Counter(w["activity_type"] for w in pool.values()).most_common()),
        "sessions": len(sess), "sessions_by_status": dict(status), "sessions_by_family_combination": dict(combos.most_common()),
        "examples_by_status": {k: _dates(v) for k, v in examples.items()},
        "queue": dict(queue), "queue_examples": {k: _dates(v) for k, v in qdates.items()},
        "fields_filled": fields, "field_owner_families": {k: dict(v) for k, v in owners.items()},
        "matching_stats": built["stats"],
        "statistics": sum(len(w["statistics"]) for w in pool.values()),
        "events": dict(Counter(e["event_type"] for w in pool.values() for e in w["events"])),
        "metadata_entries": sum(len(w["metadata"]) for w in pool.values()),
        "metadata_repeats_same_value": sum(1 for w in pool.values() for k, n in Counter(m[0] for m in w["metadata"]).items()
                                           if n > 1 and len({v for kk, v in w["metadata"] if kk == k}) == 1),
        "metadata_repeats_different_value": sum(1 for w in pool.values() for k, n in Counter(m[0] for m in w["metadata"]).items()
                                                if n > 1 and len({v for kk, v in w["metadata"] if kk == k}) > 1),
        "routes": {"references": len(refs), "by_status": dict(Counter(x["link_status"] for x in refs)),
                   "distinct_files_named": len({x["route_id"] for x in refs if x["route_id"]}),
                   "sessions_with_a_route": fields["route_id"]},
    }
    out["activity_summaries"] = {r["label"]: {"n": len(r["summaries"]),
                                              "first": min((s["date_components"] for s in r["summaries"]), default=None),
                                              "last": max((s["date_components"] for s in r["summaries"]), default=None),
                                              "partial_days": sum(1 for s in r["summaries"]
                                                                  if s["date_components"] == (r["info"]["export_date"] or "")[:10])}
                                 for r in xml_reads}
    out["ecg"] = rep.get("ecg")
    out["summary_conflicts"] = rep.get("summary_conflicts")
    return out


def code_commit_of(start: Path) -> str | None:
    from heliosd.migrate.rebase_history import _code_commit
    return _code_commit(start)
