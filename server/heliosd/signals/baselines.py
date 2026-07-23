"""Deterministic math: daily canonical values (trust-arbitrated) and personal
baselines (median + MAD). The LLM never touches this layer."""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta

from heliosd.store import db
from heliosd.trust import confidence as conf
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry


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
        # 'asleep' samples (its HealthKit export via the Bridge), and stage
        # sources write 'asleep' plus its core/deep/rem breakdown. Summing all of
        # that turned a 6h night into 12h. Per device we take the GREATEST of: the
        # direct value, or core+deep+rem when staged, else plain asleep.
        # 'in_bed' and 'awake' are never counted as sleep.
        return db.fetchall(conn, f"""
            SELECT d, device_key, ROUND(v, 2) AS v, 1 AS n FROM (
              SELECT COALESCE(dr.d, st.d) AS d,
                     COALESCE(dr.device_key, st.device_key) AS device_key,
                     GREATEST(COALESCE(dr.hrs, 0),
                              CASE WHEN COALESCE(st.sub_hrs, 0) > 0 THEN st.sub_hrs
                                   ELSE COALESCE(st.asleep_hrs, 0) END) AS v
              FROM (
                SELECT CAST(end_ts AS DATE) AS d, device_key, SUM(value) AS hrs
                FROM samples WHERE metric = 'sleep_duration' AND value IS NOT NULL
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
                FROM samples WHERE metric = 'sleep_analysis'
                  AND device_key != 'whoop'
                  AND device_key IN ({ph}) AND CAST(end_ts AS DATE) BETWEEN ? AND ?
                GROUP BY 1, 2
              ) st ON dr.d = st.d AND dr.device_key = st.device_key
            ) WHERE v > 0""", [*prio, start, end, *prio, start, end])
    fn = {"sum": "SUM(value)", "avg": "AVG(value)",
          "last": "LAST(value ORDER BY start_ts)"}[policy.agg(metric)]
    return db.fetchall(conn, f"""
        SELECT CAST(start_ts AS DATE) AS d, device_key,
               ROUND({fn}, 3) AS v, COUNT(*) AS n
        FROM samples
        WHERE metric = ? AND value IS NOT NULL
          AND device_key IN ({ph})
          AND CAST(start_ts AS DATE) BETWEEN ? AND ?
        GROUP BY 1, 2""", [metric, *prio, start, end])


def compute_daily_values(conn, policy: MetricPolicy, registry: SourceRegistry,
                         start: date, end: date, now: datetime | None = None) -> int:
    """Arbitrate one canonical value per metric per day. Never cross-device
    averaged: the top-priority device present wins; the rest are stored as
    labeled corroboration. Set-based per metric so historical backfills scale."""
    now = now or datetime.now()
    tol = float(policy.confidence.get("agreement_tolerance_pct", 12))
    written = 0
    for metric in policy.metrics:
        if metric == "sleep_analysis":
            continue  # raw stage samples; sleep_duration is the daily metric
        prio = policy.priority(metric)
        by_day: dict[date, dict[str, tuple[float, int]]] = {}
        for d, dk, v, n in _metric_day_rows(conn, policy, metric, start, end):
            if v is not None:
                by_day.setdefault(d, {})[dk] = (float(v), int(n or 0))
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
            # freshness only matters for the most recent day; history is settled.
            score, grade = conf.score(policy.confidence, policy.rank(metric, primary_key),
                                      fresh if day == end else 0.0, coverage, agreement)
            db.execute(conn, """
                INSERT OR REPLACE INTO daily_values
                  (date, metric, value, unit, device_key, n_samples, confidence, grade, corroboration)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [day, metric, value, policy.unit(metric), primary_key, n_samples,
                 score, grade, json.dumps(others) if others else None])
            written += 1
    return written


def compute_baselines(conn, policy: MetricPolicy, as_of: date) -> int:
    """Rolling median + MAD per metric per window, from canonical daily values
    strictly before `as_of` (today never contaminates its own baseline)."""
    written = 0
    for metric in policy.metrics:
        if metric == "sleep_analysis":
            continue
        for window in policy.windows:
            rows = db.fetchall(conn, """
                SELECT value FROM daily_values
                WHERE metric = ? AND date >= ? AND date < ? AND value IS NOT NULL
                ORDER BY date""", [metric, as_of - timedelta(days=window), as_of])
            vals = [r[0] for r in rows]
            if len(vals) < policy.min_days:
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
