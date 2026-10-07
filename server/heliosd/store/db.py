"""DuckDB access. One writer connection owned by the app; helpers everywhere."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path

import duckdb

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 3  # Phase 1b: rebase_era, re-read landing tables, migrations table
# Every schema version this code knows, oldest first. All of them are recorded
# in schema_version on a fresh store (the DDL is cumulative and idempotent), so
# the version history reads the same on a store that grew through them.
SCHEMA_NOTES = [
    (2, "phase 1a: utc instants, identity prefixes, tombstones, dirty journal, eligibility view"),
    (3, "phase 1b: history rebase, reread landing"),
]

_lock = threading.Lock()

# Columns every Phase 1a writer relies on. Asserted after the schema runs so an
# interrupted or partial upgrade fails loudly at startup instead of silently
# storing NULLs (adjudication-A point 19).
_REQUIRED = {
    "samples": {"start_utc", "end_utc", "src_offset_min", "time_source", "content_hash",
                "unit_rule", "score_state", "quality", "batch_id", "rebase_era"},
    "tombstones": {"tomb_id", "hk_uuid", "reason"},
    "dirty_dates": {"date", "reason"},
    "metric_registry": {"metric", "unit"},
    "whoop_records": {"record_key", "kind", "native_id"},
    "derived_generation": {"date", "generation"},
    # Phase 1b (adjudication-A point 16): the landing and migration tables.
    "sync_log": {"n_guarded", "n_landed", "guard_outcomes"},
    "hk_reread": {"hk_uuid", "hk_type", "metric", "value", "text_value", "unit", "start_utc", "end_utc", "start_raw",
                  "end_raw", "source_name", "device_key", "quality", "unit_rule", "existing_rows", "existing_time_source",
                  "time_source", "batches", "first_batch", "last_batch", "first_seen", "last_seen", "n_seen"},
    "hk_reread_variants": {"hk_uuid", "seq", "hk_type", "metric", "value", "text_value", "unit", "start_utc", "end_utc",
                           "start_raw", "end_raw", "source_name", "device_key", "quality", "unit_rule",
                           "existing_time_source", "time_source", "batch_id", "seen_at"},
    "migrations": {"name", "applied_at", "code_commit", "input_fingerprint", "summary"},
    "content_twins": {"sample_id", "survivor_id", "event", "source", "created_at"},
}
# Primary keys the writers rely on (INSERT OR IGNORE / ON CONFLICT semantics).
_PRIMARY_KEYS = {"samples": ["sample_id"], "hk_reread": ["hk_uuid"], "hk_reread_variants": ["hk_uuid", "seq"],
                 "migrations": ["name"], "tombstones": ["tomb_id"], "sample_aliases": ["old_id", "new_id"],
                 "content_twins": ["sample_id", "event", "source"]}


def connect(db_path: str | Path, allow_unverified: bool = False) -> duckdb.DuckDBPyConnection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(p))
    init_schema(conn, allow_unverified=allow_unverified)
    # Process-wide: no read_text/read_csv/COPY TO on arbitrary paths from any
    # query, including the tool endpoint. Nothing in heliosd reads files
    # through DuckDB; ingestion goes through Python (audit 2026-09-02).
    try:
        conn.execute("SET enable_external_access=false")
    except Exception:  # noqa: BLE001 - older DuckDB without the setting
        pass
    return conn


def connect_memory() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    init_schema(conn)
    return conn


def init_schema(conn: duckdb.DuckDBPyConnection, allow_unverified: bool = False) -> None:
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert_schema(conn)
    conn.executemany("INSERT OR IGNORE INTO schema_version (version, note) VALUES (?, ?)",
                     [[v, note] for v, note in SCHEMA_NOTES])
    # A data migration whose row says cutover_committed rewrote the samples
    # table but never passed its verification (reopen, rebuild, diff, oracle):
    # the daemon refuses to serve that store (checkpoint C point 29). Only the
    # migration tool opens it, to finish or inspect the verification.
    if not allow_unverified:
        pending = unverified_migrations(conn)
        if pending:
            raise RuntimeError(f"data migration committed but not verified: {pending}; finish it with "
                               "server/tools/rebase_history.py --resume-verify before starting heliosd")


def assert_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Fail at startup if any Phase 1a or 1b column or table is missing."""
    for table, cols in _REQUIRED.items():
        try:
            have = {r[0] for r in conn.execute(f"DESCRIBE {table}").fetchall()}
        except duckdb.CatalogException as e:
            raise RuntimeError(f"schema upgrade incomplete: table {table} missing") from e
        missing = cols - have
        if missing:
            raise RuntimeError(f"schema upgrade incomplete: {table} lacks {sorted(missing)}")
    pks = {}
    for table, cols in conn.execute(
            "SELECT table_name, constraint_column_names FROM duckdb_constraints() WHERE constraint_type = 'PRIMARY KEY'").fetchall():
        pks[table] = list(cols)
    for table, cols in _PRIMARY_KEYS.items():
        if pks.get(table) != cols:
            raise RuntimeError(f"schema upgrade incomplete: {table} primary key is {pks.get(table)}, expected {cols}")


def execute(conn: duckdb.DuckDBPyConnection, sql: str, params: list | tuple | None = None):
    """Serialized write/read helper. DuckDB connections are not thread-safe."""
    with _lock:
        return conn.execute(sql, params or [])


def insert_batch(conn, sql: str, rows: list) -> None:
    """Atomic bulk insert under a single lock acquisition. Keeps concurrent
    /ingest requests from interleaving BEGIN/COMMIT on the shared connection,
    and is far faster than executing thousands of single-row statements."""
    with _lock:
        conn.execute("BEGIN")
        try:
            conn.executemany(sql, rows)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


@contextmanager
def transaction(conn):
    """One lock acquisition and one transaction for a multi-statement unit of
    work (a whole ingest batch, a Whoop record replacement). Inside the block
    use `conn.execute` directly: the module helpers take the lock themselves
    and would deadlock. Commits on success, rolls back and re-raises on any
    exception, so a caller acknowledges only committed work."""
    with _lock:
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


class LockBusy(TimeoutError):
    """The store lock could not be taken inside the budget (distinct from an
    asyncio wait timeout, which in Python 3.11 is the same class as TimeoutError)."""


def _acquire(timeout: float | None, what: str) -> None:
    """Take the store lock, bounded when a timeout is given (the shutdown path
    must finish inside launchd's budget; checkpoint B, point 3)."""
    if not _lock.acquire(timeout=-1 if timeout is None else max(0.0, timeout)):
        raise LockBusy(f"store lock busy: {what} skipped")


def checkpoint(conn, timeout: float | None = None) -> None:
    """Flush the write-ahead log into the database file. Under the lock, so
    it never interleaves with a worker's statement."""
    _acquire(timeout, "checkpoint")
    try:
        conn.execute("CHECKPOINT")
    finally:
        _lock.release()


def close(conn, timeout: float | None = None) -> None:
    """Close the single writer connection under the lock (a worker mid-
    statement finishes first; its next statement fails loudly)."""
    _acquire(timeout, "close")
    try:
        conn.close()
    finally:
        _lock.release()


def fetchall(conn, sql: str, params=None) -> list[tuple]:
    with _lock:
        return conn.execute(sql, params or []).fetchall()


def fetchdicts(conn, sql: str, params=None) -> list[dict]:
    with _lock:
        cur = conn.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _phase_of(summary) -> str:
    try:
        phase = json.loads(summary or "{}").get("phase")
    except (ValueError, AttributeError):
        phase = None
    return phase or "unknown"


def migration_phase(conn, name: str) -> str | None:
    """The recorded phase of a data migration: None (no row), 'cutover_committed',
    'verified', or 'unknown' for a row without a readable phase."""
    with _lock:
        row = conn.execute("SELECT summary FROM migrations WHERE name = ?", [name]).fetchone()
    return None if row is None else _phase_of(row[0])


def migration_applied(conn, name: str) -> bool:
    """Whether a data migration completed AND passed its verification on this
    store. A cutover_committed row is not an applied migration (checkpoint C
    point 29); a row without a phase is not either."""
    return migration_phase(conn, name) == "verified"


def unverified_migrations(conn) -> list[str]:
    """Names of migrations whose row does not say verified: the data was
    rewritten but the verification has not passed (or the row is unreadable)."""
    with _lock:
        rows = conn.execute("SELECT name, summary FROM migrations ORDER BY name").fetchall()
    return [name for name, summary in rows if _phase_of(summary) != "verified"]


def attach_readonly(conn, path: str | Path, alias: str = "legacy") -> None:
    with _lock:
        conn.execute(f"ATTACH '{Path(path)}' AS {alias} (READ_ONLY)")
