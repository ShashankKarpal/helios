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

IRREPLACEABLE_TABLES = ("events", "labs", "narratives", "whoop_cache",
                        "actions", "chat_messages", "profile_facts")
MANIFEST = "manifest.json"
SCHEMA_VERSION = 1


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
        rows = db.fetchdicts(conn, f"SELECT * FROM {t}")
        path = dest / f"{t}.jsonl.gz"
        with gzip.open(path, "wt", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, default=_json_default, ensure_ascii=False) + "\n")
        manifest["tables"][t] = {"rows": len(rows), "file": path.name, "sha256": _sha256(path),
                                 "bytes": path.stat().st_size}
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def read_manifest(src: Path) -> dict:
    return json.loads((Path(src) / MANIFEST).read_text(encoding="utf-8"))


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


def restore_test(src: Path) -> dict:
    """Restore drill into an in-memory DuckDB. Returns {ok, tables: {t: {expected,
    loaded, restored}}, problems}. ok requires checksums good and every table's
    restored count == manifest count."""
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
    finally:
        conn.close()
    result["ok"] = not problems
    return result
