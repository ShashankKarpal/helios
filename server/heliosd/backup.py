"""Durability for the irreplaceable tables.

Raw HealthKit samples can be re-backfilled from the phone, and every derived
table (daily_values, baselines, signals) recomputes from them. What cannot be
recovered is what the owner and the puller produced: events (captures), labs,
narratives, whoop_cache (the Whoop API only serves a trailing window), plus
the small owner-state tables (actions with their adopted/dismissed status,
chat_messages, profile_facts).

Shape (owner decision 2026-08-17, pre-answered as weekly full plus nightly
incremental; vetoed with evidence 2026-09-05): those tables total a few
hundred rows and grow by a handful a day, so a nightly FULL export is a few
kilobytes and an incremental scheme would add code paths without saving
anything. Nightly full it is. The heavy `samples` table is excluded on
purpose.

The export runs inside the daemon (single DuckDB writer; and the connection
has enable_external_access=false, so DuckDB itself never writes files):
rows are fetched through the normal helpers and written as gzipped JSONL, one
file per table, plus a manifest with row counts and SHA-256 of each file. The
companion CLI (server/tools/helios_backup.py) asks the daemon to export, syncs
the backup directory off the Mac, verifies the remote checksums, and writes
LAST_OK only when every step passed; the watchdog watches LAST_OK.

`restore_test` proves the export is usable: it loads every file into a fresh
schema and compares counts to the manifest. Run it on the copy, not the live
store.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from heliosd.store import db

# Since Phase 1a the deletion and identity protocol is irreplaceable too: a
# restore that forgot tombstones would let a replayed export resurrect deleted
# samples (adjudication-A point 17). The journal is tiny and makes a restored
# store finish its pending recompute.
# Wave 1 A26 (audit 2026-10-08, P18): four more tables change what a restored
# store serves and were missing from the export. hk_reread and hk_reread_variants
# are the re-read evidence twins.confirmed_sql and the promotion path read;
# metric_registry is what the eligibility view is defined against; migrations is
# the row that binds a restore to its lineage archive (the manifest carried only
# a projection of it). All four are small.
IRREPLACEABLE_TABLES = ("events", "labs", "narratives", "whoop_cache",
                        "actions", "chat_messages", "profile_facts",
                        "tombstones", "sample_aliases", "content_twins", "whoop_records", "dirty_dates",
                        "hk_reread", "hk_reread_variants", "metric_registry", "migrations")
MANIFEST = "manifest.json"
SCHEMA_VERSION = 3       # 2: per-table filters recorded in the manifest (Phase 1b); 3: the four A26 tables

# Phase 1b writes about 4.2 million alias rows (every legacy Bridge id to its
# hk id, every dropped twin, every linked export row). They are derivable from
# any cold capture plus the deterministic rule, and the migration tool writes
# them once more into the lineage archive beside the capture (parquet,
# checksummed, two places). The nightly export was designed for kilobytes, so
# those reasons are filtered out here and the manifest records the filter.
# NULL-safe (adjudication-A point 10): a row with no reason is kept.
# Restore dependency: a restore of the alias table from a nightly export is
# complete only together with the lineage archive of the capture that holds
# the migration (lineage_aliases.parquet, checksummed in the archive's
# MANIFEST.sha256). restore_test refuses to call a filtered table restored
# until load_archive_aliases has put those rows back from a verified archive.
ALIAS_EXCLUDED_REASONS = ("history_rebase_v1", "twin_collapse_v1", "export_link_v1")
ARCHIVE_ALIASES = "lineage_aliases.parquet"
ARCHIVE_MANIFEST = "MANIFEST.sha256"
_FILTERS = {"sample_aliases": {"where": "reason IS NULL OR reason NOT IN (" + ", ".join(f"'{r}'" for r in ALIAS_EXCLUDED_REASONS) + ")",
                               "excluded_reasons": list(ALIAS_EXCLUDED_REASONS),
                               "restore_dependency": ARCHIVE_ALIASES}}


def _json_default(v):
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return str(v)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export_tables(conn, dest: Path, tables: tuple[str, ...] = IRREPLACEABLE_TABLES,
                  now: datetime | None = None) -> dict:
    """Write <dest>/<table>.jsonl.gz for each table and <dest>/manifest.json.
    Returns the manifest. Idempotent: rerunning overwrites the same files."""
    now = now or datetime.now()
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": SCHEMA_VERSION, "exported_at": now.isoformat(),
                "tables": {}}
    for t in tables:
        flt = _FILTERS.get(t)
        rows = db.fetchdicts(conn, f"SELECT * FROM {t}" + (f" WHERE {flt['where']}" if flt else ""))
        path = dest / f"{t}.jsonl.gz"
        with gzip.open(path, "wt", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, default=_json_default, ensure_ascii=False) + "\n")
        manifest["tables"][t] = {"rows": len(rows), "file": path.name, "sha256": _sha256(path),
                                 "bytes": path.stat().st_size}
        if flt:
            manifest["tables"][t]["filter"] = dict(flt)
            manifest["tables"][t]["rows_excluded"] = db.fetchall(conn, f"SELECT COUNT(*) FROM {t} WHERE NOT ({flt['where']})")[0][0]
    # The migration this store carries (Phase 1b), so a restore can be bound to
    # the right lineage archive: the reviewed input fingerprint and the digest
    # of the archive's own manifest (adjudication-A-twins point 20).
    manifest["migrations"] = _migration_bindings(conn)
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _migration_bindings(conn) -> dict:
    out = {}
    try:
        rows = db.fetchdicts(conn, "SELECT name, input_fingerprint, summary FROM migrations")
    except Exception:  # noqa: BLE001 - a store without the table
        return out
    for r in rows:
        try:
            s = json.loads(r.get("summary") or "{}")
        except ValueError:
            s = {}
        out[r["name"]] = {"input_fingerprint": r.get("input_fingerprint"), "archive_manifest_sha256": s.get("archive_manifest_sha256"),
                          "archive_places": s.get("archive_places")}
    return out


def archive_manifest_digest(archive_dir: Path) -> str:
    """sha256 over the archive's MANIFEST.sha256 lines, sorted, newline-joined
    (the migration records the same digest in its summary)."""
    lines = sorted(l for l in (Path(archive_dir) / ARCHIVE_MANIFEST).read_text().splitlines() if l.strip())
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def read_manifest(src: Path) -> dict:
    return json.loads((Path(src) / MANIFEST).read_text(encoding="utf-8"))


def archive_dir_from_manifest(manifest: dict) -> Path | None:
    """The lineage archive a restore of this export depends on: the first of
    the places the migration recorded that exists on this machine right now
    (the local capture first, the SSD second). None when the manifest binds
    no migration or no place is reachable; restore_test then reports the
    filtered table as not restorable, which is the true state."""
    for b in (manifest.get("migrations") or {}).values():
        for place in b.get("archive_places") or []:
            if place and Path(place).is_dir():
                return Path(place)
    return None


def verify_files(src: Path) -> list[str]:
    """Checksum every file named in the manifest. Returns problems (empty = ok)."""
    src = Path(src)
    problems = []
    try:
        m = read_manifest(src)
    except (OSError, ValueError) as e:
        return [f"manifest unreadable: {e}"]
    for t, spec in m.get("tables", {}).items():
        p = src / spec["file"]
        if not p.is_file():
            problems.append(f"{t}: {spec['file']} missing")
        elif _sha256(p) != spec["sha256"]:
            problems.append(f"{t}: checksum mismatch")
    return problems


def _coerce(table: str, conn, row: dict) -> list:
    """Order values by the live schema's column order; DuckDB casts ISO strings
    to DATE/TIMESTAMP on insert."""
    cols = [c[0] for c in db.fetchall(conn, f"DESCRIBE {table}")]
    return [row.get(c) for c in cols]


def load_tables(conn, src: Path) -> dict[str, int]:
    """Load every exported table into `conn` (fresh schema expected). Returns
    rows inserted per table."""
    src = Path(src)
    m = read_manifest(src)
    out: dict[str, int] = {}
    for t, spec in m["tables"].items():
        cols = [c[0] for c in db.fetchall(conn, f"DESCRIBE {t}")]
        rows = []
        with gzip.open(src / spec["file"], "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    rows.append([r.get(c) for c in cols])
        if rows:
            db.insert_batch(conn, f"INSERT OR IGNORE INTO {t} ({', '.join(cols)}) "
                                  f"VALUES ({', '.join(['?'] * len(cols))})", rows)
        out[t] = len(rows)
    return out


def reconcile_tombstones(conn) -> dict:
    """Restore step (checkpoint C point 27). Loading a tombstone row never
    deletes the sample it names, so a capture restored from before a deletion
    and replayed with later tombstones would serve the deleted sample again
    (deletion is physical; the eligibility view has no tombstone anti-join).
    Delete every sample a tombstone names by uuid or native id, journal the
    touched reporting dates so the dependents are rebuilt before serving, and
    report counts. Idempotent; a second call deletes nothing."""
    from datetime import datetime
    from heliosd.ingest import twins
    promoted = 0
    with db.transaction(conn) as c:
        cur = c.execute("""SELECT * FROM samples s
            WHERE (s.hk_uuid IS NOT NULL AND s.hk_uuid IN (SELECT hk_uuid FROM tombstones WHERE hk_uuid IS NOT NULL))
               OR s.sample_id IN (SELECT tomb_id FROM tombstones)""")
        cols = [d[0] for d in cur.description]
        victims = [dict(zip(cols, r)) for r in cur.fetchall()]
        dates = sorted({d for v in victims for d in ((v["start_ts"].date() if v.get("start_ts") else None),
                                                      (v["end_ts"].date() if v.get("end_ts") else None)) if d is not None})
        if victims:
            c.execute("DELETE FROM samples WHERE sample_id IN (SELECT unnest(?))", [[v["sample_id"] for v in victims]])
            now = datetime.now()
            c.executemany("INSERT OR REPLACE INTO dirty_dates (date, reason, batch_id, enqueued_at) VALUES (?, ?, ?, ?)",
                          [[d, "restore_reconcile", "restore", now] for d in dates])
            # A replayed tombstone that removes a content-twin survivor hands
            # eligibility to a remaining confirmed member, as the live deletion
            # path does (adjudication-A-twins point 1). Same transaction.
            promoted = twins.promote_after_delete(c, victims, "restore", now)
        left = c.execute("SELECT COUNT(*) FROM samples s WHERE s.hk_uuid IN (SELECT hk_uuid FROM tombstones WHERE hk_uuid IS NOT NULL)"
                         " OR s.sample_id IN (SELECT tomb_id FROM tombstones)").fetchone()[0]
    return {"deleted": len(victims), "dates_journaled": len(dates), "live_tombstoned_left": int(left), "promoted": promoted}


def verify_archive(archive_dir: Path, expected_manifest_sha256: str | None = None) -> list[str]:
    """Checksum the alias archive against the archive's own manifest and, when
    the nightly manifest recorded one, the archive manifest's digest (a valid
    archive of another run is refused)."""
    archive_dir = Path(archive_dir)
    p = archive_dir / ARCHIVE_ALIASES
    man = archive_dir / ARCHIVE_MANIFEST
    if not p.is_file():
        return [f"archive: {ARCHIVE_ALIASES} missing in {archive_dir}"]
    if not man.is_file():
        return [f"archive: {ARCHIVE_MANIFEST} missing in {archive_dir}"]
    if expected_manifest_sha256 and archive_manifest_digest(archive_dir) != expected_manifest_sha256:
        return [f"archive: {ARCHIVE_MANIFEST} digest differs from the one the export recorded (another run's archive)"]
    # Every file the manifest lists, not only the aliases (checkpoint B on the twin diff, point 12).
    problems = []
    for line in man.read_text().splitlines():
        if not line.strip():
            continue
        digest, name = line.split()[0], line.split()[-1]
        q = archive_dir / name
        if not q.is_file():
            problems.append(f"archive: {name} missing")
        elif _sha256(q) != digest:
            problems.append(f"archive: {name} checksum mismatch")
    if problems:
        return problems
    want = {line.split()[-1]: line.split()[0] for line in man.read_text().splitlines() if line.strip()}
    if ARCHIVE_ALIASES not in want:
        return [f"archive: {ARCHIVE_ALIASES} not in {ARCHIVE_MANIFEST}"]
    if _sha256(p) != want[ARCHIVE_ALIASES]:
        return [f"archive: {ARCHIVE_ALIASES} checksum mismatch"]
    return []


def load_archive_aliases(conn, archive_dir: Path, expected_manifest_sha256: str | None = None) -> int:
    """Put the migration's alias rows (filtered out of the nightly export) back
    from a verified lineage archive. The parquet is read through a separate
    in-memory connection (the daemon's connection has external access off)
    and inserted through the normal helpers. Returns rows inserted."""
    problems = verify_archive(archive_dir, expected_manifest_sha256)
    if problems:
        raise ValueError("; ".join(problems))
    import duckdb
    reader = duckdb.connect()
    try:
        rows = reader.execute(f"SELECT old_id, new_id, reason, created_at FROM read_parquet('{Path(archive_dir) / ARCHIVE_ALIASES}')").fetchall()
    finally:
        reader.close()
    if rows:
        db.insert_batch(conn, "INSERT OR IGNORE INTO sample_aliases (old_id, new_id, reason, created_at) VALUES (?, ?, ?, ?)",
                        [list(r) for r in rows])
    return len(rows)


def restore_test(src: Path, archive_dir: Path | None = None) -> dict:
    """Restore drill into an in-memory DuckDB. Returns {ok, tables: {t: {expected,
    loaded, restored}}, problems}. ok requires checksums good, every table's
    restored count == manifest count, and, for a table the manifest says was
    filtered, the archive dependency present, verified and loaded so the
    restored count equals the live count (rows plus rows_excluded)."""
    problems = verify_files(src)
    result = {"ok": False, "tables": {}, "problems": problems}
    if problems:
        return result
    conn = db.connect_memory()
    try:
        m = read_manifest(src)
        loaded = load_tables(conn, src)
        for t, spec in m["tables"].items():
            n = db.fetchall(conn, f"SELECT COUNT(*) FROM {t}")[0][0]
            result["tables"][t] = {"expected": spec["rows"], "loaded": loaded.get(t, 0), "restored": n}
            if n != spec["rows"]:
                problems.append(f"{t}: restored {n} of {spec['rows']}")
            excluded = int(spec.get("rows_excluded") or 0)
            if spec.get("filter") and excluded:
                if archive_dir is None:
                    problems.append(f"{t}: {excluded} rows were excluded by the export filter; the archive "
                                    f"({spec['filter'].get('restore_dependency')}) is required to restore them")
                    continue
                expected = next((b.get("archive_manifest_sha256") for b in (m.get("migrations") or {}).values()
                                 if b.get("archive_manifest_sha256")), None)
                try:
                    from_archive = load_archive_aliases(conn, archive_dir, expected)
                except ValueError as e:
                    problems.append(f"{t}: {e}")
                    continue
                total = db.fetchall(conn, f"SELECT COUNT(*) FROM {t}")[0][0]
                result["tables"][t].update({"from_archive": from_archive, "restored_with_archive": total,
                                            "live": spec["rows"] + excluded})
                if total != spec["rows"] + excluded:
                    problems.append(f"{t}: restored {total} with the archive, live table had {spec['rows'] + excluded}")
    finally:
        conn.close()
    result["ok"] = not problems
    return result
