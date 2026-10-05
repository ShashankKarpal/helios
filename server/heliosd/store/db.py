"""DuckDB access. One writer connection owned by the app; helpers everywhere."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

import duckdb

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 2  # Phase 1a additive columns, tombstones, journal, view

_lock = threading.Lock()

# Columns every Phase 1a writer relies on. Asserted after the schema runs so an
# interrupted or partial upgrade fails loudly at startup instead of silently
# storing NULLs (adjudication-A point 19).
_REQUIRED = {
    "samples": {"start_utc", "end_utc", "src_offset_min", "time_source", "content_hash",
                "unit_rule", "score_state", "quality", "batch_id"},
    "tombstones": {"tomb_id", "hk_uuid", "reason"},
    "dirty_dates": {"date", "reason"},
    "metric_registry": {"metric", "unit"},
    "whoop_records": {"record_key", "kind", "native_id"},
    "derived_generation": {"date", "generation"},
}


def connect(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(p))
    init_schema(conn)
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


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert_schema(conn)
    conn.execute("INSERT OR IGNORE INTO schema_version (version, note) VALUES (?, ?)",
                 [SCHEMA_VERSION, "phase 1a: utc instants, identity prefixes, tombstones, dirty journal, eligibility view"])


def assert_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Fail at startup if any Phase 1a column or table is missing."""
    for table, cols in _REQUIRED.items():
        have = {r[0] for r in conn.execute(f"DESCRIBE {table}").fetchall()}
        missing = cols - have
        if missing:
            raise RuntimeError(f"schema upgrade incomplete: {table} lacks {sorted(missing)}")


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


def _acquire(timeout: float | None, what: str) -> None:
    """Take the store lock, bounded when a timeout is given (the shutdown path
    must finish inside launchd's budget; checkpoint B, point 3)."""
    if not _lock.acquire(timeout=-1 if timeout is None else max(0.0, timeout)):
        raise TimeoutError(f"store lock busy: {what} skipped")


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


def attach_readonly(conn, path: str | Path, alias: str = "legacy") -> None:
    with _lock:
        conn.execute(f"ATTACH '{Path(path)}' AS {alias} (READ_ONLY)")
