"""Deterministic math: daily canonical values (trust-arbitrated) and personal
baselines (median + MAD). The LLM never touches this layer.

Reads the ONE eligibility view (eligible_samples): registered metrics only,
usable rows only, excluded devices out, Whoop scored records only, time-valid
rows only, unit rules applied. Never the raw samples table.
"""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta

from heliosd.store import db
from heliosd.trust import confidence as conf
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

# Aggregation dispatcher (plan v2 4.2): sum, avg, last, min, max. Ties for
# last resolve by wall time then sample_id: both are NOT NULL on every row,
# legacy or new (start_utc is NULL on legacy rows, so it cannot order them).
_AGG_SQL = {"sum": "SUM(value)", "avg": "AVG(value)", "min": "MIN(value)", "max": "MAX(value)",
            "last": "LAST(value ORDER BY start_ts, sample_id)"}


def _metric_day_rows(conn, policy: MetricPolicy, metric: str,
                     start: date, end: date) -> list[tuple]:
    """(day, device_key, value, n) for one metric across the whole window,
    restricted to that metric's priority devices. One SQL query per metric,
    which keeps a full 10-year backfill recompute fast."""
    prio = policy.priority(metric)
    if not prio:
        return []
    ph = ", ".join(["?"] * len(prio))
    if metric == "sleep_duration":
        # One clean asleep-hours value per device per night, then arbitrated by
        # priority. Sources overlap and must NOT be summed: Whoop appears both as
        # a direct sleep_duration sample (from the puller) and as sleep_analysis
        # stage samples (its HealthKit export via the Bridge), and stage
        # sources write 'asleep' plus its core/deep/rem breakdown. Per device we
        # take the GREATEST of: the longest direct record of the night (two
        # direct records on one night are revisions or a split, never additive),
        # or core+deep+rem when staged, else plain asleep. 'in_bed' and 'awake'
        # are never counted as sleep.
        return db.fetchall(conn, f"""
            SELECT d, device_key, ROUND(v, 2) AS v, 1 AS n FROM (
              SELECT COALESCE(dr.d, st.d) AS d,
                     COALESCE(dr.device_key, st.device_key) AS device_key,
                     GREATEST(COALESCE(dr.hrs, 0),
                              CASE WHEN COALESCE(st.sub_hrs, 0) > 0 THEN st.sub_hrs
                                   ELSE COALESCE(st.asleep_hrs, 0) END) AS v
              FROM (
                SELECT CAST(end_ts AS DATE) AS d, device_key, MAX(value) AS hrs
                FROM eligible_samples WHERE metric = 'sleep_duration' AND value IS NOT NULL
                  AND device_key IN ({ph}) AND CAST(end_ts AS DATE) BETWEEN ? AND ?
                GROUP BY 1, 2
              ) dr
              FULL OUTER JOIN (
                -- Whoop's HealthKit sleep copy is excluded: its API duration
                -- (the direct branch) is authoritative for whoop, and the HK
                -- copy arrives with different day bucketing, which double-filed
                -- nights across two dates.
                SELECT CAST(end_ts AS DATE) AS d, device_key,
                       SUM(CASE WHEN text_value IN ('core','deep','rem') THEN value ELSE 0 END) / 60.0 AS sub_hrs,
                       SUM(CASE WHEN text_value = 'asleep' THEN value ELSE 0 END) / 60.0 AS asleep_hrs
                FROM eligible_samples WHERE metric = 'sleep_analysis'
                  AND device_key != 'whoop'
                  AND device_key IN ({ph}) AND CAST(end_ts AS DATE) BETWEEN ? AND ?
                GROUP BY 1, 2
              ) st ON dr.d = st.d AND dr.device_key = st.device_key
            ) WHERE v > 0""", [*prio, start, end, *prio, start, end])
    fn = _AGG_SQL[policy.agg(metric)]
    return db.fetchall(conn, f"""
        SELECT CAST(start_ts AS DATE) AS d, device_key,
               ROUND({fn}, 3) AS v, COUNT(*) AS n
        FROM eligible_samples
        WHERE metric = ? AND value IS NOT NULL
          AND device_key IN ({ph})
          AND CAST(start_ts AS DATE) BETWEEN ? AND ?
        GROUP BY 1, 2""", [metric, *prio, start, end])


def compute_daily_values(conn, policy: MetricPolicy, registry: SourceRegistry,
                         start: date, end: date, now: datetime | None = None,
                         as_of: date | None = None) -> int:
    """Arbitrate one canonical value per metric per day. Never cross-device
    averaged: the top-priority device present wins; the rest are stored as
    labeled corroboration. Set-based per metric so historical backfills scale.

    Reconciles: any (metric, date) inside [start, end] that this pass did not
    produce is deleted, so a day whose eligible inputs vanished loses its
    daily value instead of keeping a stale one. Freshness is judged against
    `as_of` (the reporting today), never the range boundary, so recomputing a
    date alone or inside a wide range yields the same confidence."""
    now = now or datetime.now()
    as_of = as_of or end
    tol = float(policy.confidence.get("agreement_tolerance_pct", 12))
    written = 0
    for metric in policy.metrics:
        if not policy.daily(metric):
            continue  # raw stage samples; sleep_duration is the daily metric
        prio = policy.priority(metric)
        by_day: dict[date, dict[str, tuple[float, int]]] = {}
        for d, dk, v, n in _metric_day_rows(conn, policy, metric, start, end):
            if v is not None:
                by_day.setdefault(d, {})[dk] = (float(v), int(n or 0))
        produced: list[date] = []
        for day, per_device in by_day.items():
            primary_key = next((dk for dk in prio if dk in per_device), None)
            if primary_key is None:
                continue
            value, n_samples = per_device[primary_key]
            others = {dk: v for dk, (v, _) in per_device.items() if dk != primary_key}
            agreement = conf.agreement_factor(value, list(others.values()), tol)
            age_h = max(0.0, (now - datetime.combine(day, datetime.min.time())).total_seconds() / 3600 - 24)
            fresh = age_h / policy.cadence_hours(metric) if policy.cadence_hours(metric) else 0
            coverage = min(1.0, n_samples / 3) if policy.agg(metric) != "sum" else 1.0
            # freshness only matters for the reporting today; history is settled.
            score, grade = conf.score(policy.confidence, policy.rank(metric, primary_key),
                                      fresh if day == as_of else 0.0, coverage, agreement)
            db.execute(conn, """
                INSERT OR REPLACE INTO daily_values
                  (date, metric, value, unit, device_key, n_samples, confidence, grade, corroboration)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [day, metric, value, policy.unit(metric), primary_key, n_samples,
                 score, grade, json.dumps(others) if others else None])
            produced.append(day)
            written += 1
        # Reconcile: nothing produced for a date in range means no eligible input.
        if produced:
            db.execute(conn, "DELETE FROM daily_values WHERE metric = ? AND date BETWEEN ? AND ? "
                             "AND date NOT IN (SELECT unnest(?))", [metric, start, end, produced])
        else:
            db.execute(conn, "DELETE FROM daily_values WHERE metric = ? AND date BETWEEN ? AND ?", [metric, start, end])
    return written


def compute_baselines(conn, policy: MetricPolicy, as_of: date) -> int:
    """Rolling median + MAD per metric per window, from canonical daily values
    strictly before `as_of` (today never contaminates its own baseline). A
    baseline that no longer reaches min_days is removed, not kept."""
    written = 0
    for metric in policy.metrics:
        if not policy.daily(metric):
            continue
        for window in policy.windows:
            rows = db.fetchall(conn, """
                SELECT value FROM daily_values
                WHERE metric = ? AND date >= ? AND date < ? AND value IS NOT NULL
                ORDER BY date""", [metric, as_of - timedelta(days=window), as_of])
            vals = [r[0] for r in rows]
            if len(vals) < policy.min_days:
                db.execute(conn, "DELETE FROM baselines WHERE date = ? AND metric = ? AND window_days = ?",
                           [as_of, metric, window])
                continue
            med = statistics.median(vals)
            mad = statistics.median([abs(v - med) for v in vals])
            db.execute(conn, """
                INSERT OR REPLACE INTO baselines (date, metric, window_days, median, mad, n_days)
                VALUES (?, ?, ?, ?, ?, ?)""", [as_of, metric, window, med, mad, len(vals)])
            written += 1
    return written


def get_baseline(conn, metric: str, as_of: date, window: int) -> dict | None:
    rows = db.fetchdicts(conn, """
        SELECT median, mad, n_days FROM baselines
        WHERE metric = ? AND date = ? AND window_days = ?""", [metric, as_of, window])
    return rows[0] if rows else None
