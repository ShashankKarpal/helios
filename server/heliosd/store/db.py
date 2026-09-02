"""DuckDB access. One writer connection owned by the app; helpers everywhere."""

from __future__ import annotations

import threading
from pathlib import Path

import duckdb

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_lock = threading.Lock()


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
