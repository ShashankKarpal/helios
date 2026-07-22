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


def _device_day_value(conn, metric: str, day: date, device_key: str, agg: str):
    """One value for one device for one day, plus sample count and coverage hint."""
    if metric == "sleep_duration":
        # Night attributed to wake date: sessions ending on `day`, asleep stages only.
        rows = db.fetchall(conn, """
            SELECT SUM(value) FROM samples
            WHERE metric = 'sleep_analysis' AND device_key = ?
              AND text_value IN ('asleep','core','deep','rem')
              AND CAST(end_ts AS DATE) = ?""", [device_key, day])
        mins = rows[0][0] if rows and rows[0][0] else None
        n = db.fetchall(conn, """
            SELECT COUNT(*) FROM samples WHERE metric='sleep_analysis'
              AND device_key=? AND CAST(end_ts AS DATE)=?""", [device_key, day])[0][0]
        return (round(mins / 60.0, 2) if mins else None), int(n)
    fn = {"sum": "SUM(value)", "avg": "AVG(value)", "last": "LAST(value ORDER BY start_ts)"}[agg]
    rows = db.fetchall(conn, f"""
        SELECT {fn}, COUNT(*) FROM samples
        WHERE metric = ? AND device_key = ? AND CAST(start_ts AS DATE) = ?
          AND value IS NOT NULL""", [metric, device_key, day])
    v, n = rows[0] if rows else (None, 0)
    return (round(v, 3) if v is not None else None), int(n or 0)


def compute_daily_values(conn, policy: MetricPolicy, registry: SourceRegistry,
                         start: date, end: date, now: datetime | None = None) -> int:
    """Arbitrate one canonical value per metric per day. Never cross-device averaged."""
    now = now or datetime.now()
    tol = float(policy.confidence.get("agreement_tolerance_pct", 12))
    written = 0
    day = start
    while day <= end:
        for metric in policy.metrics:
            if metric == "sleep_analysis":
                continue  # raw stage samples; sleep_duration is the daily metric
            per_device: dict[str, tuple[float, int]] = {}
            for dk in policy.priority(metric):
                v, n = _device_day_value(conn, metric, day, dk, policy.agg(metric))
                if v is not None:
                    per_device[dk] = (v, n)
            if not per_device:
                continue
            primary_key = next(dk for dk in policy.priority(metric) if dk in per_device)
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
        day += timedelta(days=1)
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
