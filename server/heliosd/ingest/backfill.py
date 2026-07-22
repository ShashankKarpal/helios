"""Historical data. Preferred: the Bridge's own full HealthKit backfill.
This module covers (a) parity checks against the legacy vpetersson DuckDB and
(b) a fallback import from it when the Bridge is unavailable.

Never run import_legacy after a Bridge backfill of the same period: use
parity_report instead (it only reads)."""

from __future__ import annotations

from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

SLEEP_CASE = """CASE
    WHEN value LIKE '%Deep%' THEN 'deep'
    WHEN value LIKE '%REM%' THEN 'rem'
    WHEN value LIKE '%Core%' THEN 'core'
    WHEN value LIKE '%Awake%' THEN 'awake'
    WHEN value LIKE '%InBed%' OR value LIKE '%In Bed%' THEN 'in_bed'
    ELSE 'asleep' END"""


def parity_report(conn, policy: MetricPolicy) -> list[dict]:
    """Compare per-type counts: helios samples vs attached legacy db ('legacy')."""
    out = []
    for metric, m in policy.metrics.items():
        hk = m.get("hk")
        if not hk:
            continue
        ours = db.fetchall(conn, "SELECT COUNT(*) FROM samples WHERE metric = ?", [metric])[0][0]
        theirs = db.fetchall(conn, "SELECT COUNT(*) FROM legacy.records WHERE record_type = ?", [hk])[0][0]
        out.append({"metric": metric, "hk_type": hk, "helios": ours, "legacy": theirs,
                    "ratio": round(ours / theirs, 3) if theirs else None})
    return out


def import_legacy(conn, policy: MetricPolicy, registry: SourceRegistry,
                  metrics: list[str] | None = None) -> int:
    """Fallback bulk import from legacy.records into samples (sync_path legacy_import)."""
    sources = [r[0] for r in db.fetchall(conn, "SELECT DISTINCT source_name FROM legacy.records")]
    total = 0
    for metric, m in policy.metrics.items():
        if metrics and metric not in metrics:
            continue
        hk = m.get("hk")
        if not hk:
            continue
        is_sleep = metric == "sleep_analysis"
        for src in sources:
            device_key = registry.resolve(src)
            if device_key is None:
                continue
            value_expr = ("(epoch(end_date) - epoch(start_date)) / 60.0" if is_sleep
                          else "TRY_CAST(value AS DOUBLE)")
            text_expr = SLEEP_CASE if is_sleep else "NULL"
            db.execute(conn, f"""
                INSERT OR IGNORE INTO samples
                  (sample_id, metric, hk_type, value, text_value, unit, start_ts, end_ts,
                   source_name, device_key, sync_path)
                SELECT 'lg:' || md5(record_type || '|' || CAST(start_date AS VARCHAR) || '|' ||
                       CAST(end_date AS VARCHAR) || '|' || source_name || '|' || COALESCE(value, '')),
                       ?, record_type, {value_expr}, {text_expr}, unit, start_date, end_date,
                       source_name, ?, 'legacy_import'
                FROM legacy.records WHERE record_type = ? AND source_name = ?""",
                [metric, device_key, hk, src])
            total += db.fetchall(conn, "SELECT COUNT(*) FROM samples WHERE metric = ? AND sync_path = 'legacy_import'",
                                 [metric])[0][0]
    return total
