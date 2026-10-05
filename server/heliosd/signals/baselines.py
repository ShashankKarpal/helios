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
# Sums run over exact DECIMAL casts: a floating-point SUM depends on the
# order DuckDB's parallel aggregate happens to add the rows in, and on the
# real store that flipped the second decimal of 9 sleep nights and 4 SDNN
# days between two identical runs (dry run, 2026-10-05). Rounding happens
# inside the dispatcher so every path rounds the same way.
_DEC = "CAST(value AS DECIMAL(30,6))"
_AGG_SQL = {"sum": f"CAST(ROUND(SUM({_DEC}), 3) AS DOUBLE)",
            "avg": f"ROUND(CAST(SUM({_DEC}) AS DOUBLE) / COUNT(value), 3)",
            "min": "ROUND(MIN(value), 3)", "max": "ROUND(MAX(value), 3)",
            "last": "ROUND(LAST(value ORDER BY start_ts, sample_id), 3)"}
_DEC_MIN = "CAST(SUM(CAST(CASE WHEN text_value IN ('core','deep','rem') THEN value ELSE 0 END AS DECIMAL(30,6))) AS DOUBLE) / 60.0"
_DEC_ASLEEP = "CAST(SUM(CAST(CASE WHEN text_value = 'asleep' THEN value ELSE 0 END AS DECIMAL(30,6))) AS DOUBLE) / 60.0"


def _daily_metrics(policy: MetricPolicy) -> list[str]:
    return [m for m in policy.metrics if policy.daily(m)]


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
                       {_DEC_MIN} AS sub_hrs,
                       {_DEC_ASLEEP} AS asleep_hrs
                FROM eligible_samples WHERE metric = 'sleep_analysis'
                  AND device_key != 'whoop'
                  AND device_key IN ({ph}) AND CAST(end_ts AS DATE) BETWEEN ? AND ?
                GROUP BY 1, 2
              ) st ON dr.d = st.d AND dr.device_key = st.device_key
            ) WHERE v > 0""", [*prio, start, end, *prio, start, end])
    fn = _AGG_SQL[policy.agg(metric)]
    return db.fetchall(conn, f"""
        SELECT CAST(start_ts AS DATE) AS d, device_key,
               {fn} AS v, COUNT(*) AS n
        FROM eligible_samples
        WHERE metric = ? AND value IS NOT NULL
          AND device_key IN ({ph})
          AND CAST(start_ts AS DATE) BETWEEN ? AND ?
        GROUP BY 1, 2""", [metric, *prio, start, end])


def journal_dates(c, reason: str, batch_id: str, dates: set[date], stamp: datetime) -> None:
    """Write dirty_dates rows on the raw connection inside the caller's
    transaction (module level so a test can inject a failure here and prove
    the daily values of that metric rolled back with it)."""
    c.executemany("INSERT OR REPLACE INTO dirty_dates (date, reason, batch_id, enqueued_at) VALUES (?, ?, ?, ?)",
                  [[d, reason, batch_id, stamp] for d in sorted(dates)])


def compute_daily_values(conn, policy: MetricPolicy, registry: SourceRegistry,
                         start: date, end: date, now: datetime | None = None,
                         as_of: date | None = None, changed: set[date] | None = None,
                         journal: str | None = None, journal_skip: set[date] | None = None) -> int:
    """Arbitrate one canonical value per metric per day. Never cross-device
    averaged: the top-priority device present wins; the rest are stored as
    labeled corroboration. Set-based per metric so historical backfills scale.

    Reconciles: any (metric, date) inside [start, end] that this pass did not
    produce is deleted, so a day whose eligible inputs vanished loses its
    daily value instead of keeping a stale one; rows of metrics that are no
    longer daily metrics of the policy go too. Freshness is judged against
    `as_of` (the reporting today), never the range boundary, so recomputing a
    date alone or inside a wide range yields the same confidence.

    Durability (checkpoint C, points 10 and 11): each metric's writes, its
    reconcile deletes and, when `journal` names a reason, the dirty_dates rows
    for the dates whose stored row changed in ANY field commit in one
    transaction, so a change and its dependency intent can never be separated
    by a crash. `journal_skip` holds dates the caller rebuilds itself right
    after. `changed`, when given, collects the changed dates for the caller."""
    # An aware `now` is rendered in the reporting zone (freshness is judged on
    # reporting dates); a naive one is taken as is; none means the Mac clock,
    # which equals the reporting zone today (checkpoint A point 9, Phase 4).
    if now is not None and now.tzinfo is not None:
        now = now.astimezone(policy.zone).replace(tzinfo=None)
    now = now or datetime.now()
    as_of = as_of or end
    tol = float(policy.confidence.get("agreement_tolerance_pct", 12))
    skip = journal_skip or set()
    prev: dict[tuple[date, str], tuple] = {}
    for d, m, v, u, dk, n, cf, g, co in db.fetchall(
            conn, "SELECT date, metric, value, unit, device_key, n_samples, confidence, grade, corroboration "
                  "FROM daily_values WHERE date BETWEEN ? AND ?", [start, end]):
        prev[(d, m)] = (v, u, dk, n, cf, g, co)
    daily_metrics = _daily_metrics(policy)
    written = 0
    stamp = datetime.now()

    def record_change(c, metric: str, dates: set[date]) -> None:
        if changed is not None:
            changed.update(dates)
        if journal and dates - skip:
            journal_dates(c, journal, f"daily:{metric}", dates - skip, stamp)

    for metric in daily_metrics:
        prio = policy.priority(metric)
        by_day: dict[date, dict[str, tuple[float, int]]] = {}
        # Read outside the transaction: the helpers take the store lock themselves.
        for d, dk, v, n in _metric_day_rows(conn, policy, metric, start, end):
            if v is not None:
                by_day.setdefault(d, {})[dk] = (float(v), int(n or 0))
        rows_out: list[list] = []
        moved: set[date] = set()
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
            corr = json.dumps(others, sort_keys=True) if others else None
            row = [day, metric, value, policy.unit(metric), primary_key, n_samples, score, grade, corr]
            rows_out.append(row)
            p = prev.get((day, metric))
            if p is None or p != (value, policy.unit(metric), primary_key, n_samples, score, grade, corr):
                moved.add(day)
        produced = {r[0] for r in rows_out}
        # Rows in range that this pass did not produce have no eligible input any more.
        gone = {d for (d, m) in prev if m == metric and d not in produced}
        with db.transaction(conn) as c:
            if rows_out:
                c.executemany("""
                    INSERT OR REPLACE INTO daily_values
                      (date, metric, value, unit, device_key, n_samples, confidence, grade, corroboration)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows_out)
                c.execute("DELETE FROM daily_values WHERE metric = ? AND date BETWEEN ? AND ? "
                          "AND date NOT IN (SELECT unnest(?))", [metric, start, end, sorted(produced)])
            else:
                c.execute("DELETE FROM daily_values WHERE metric = ? AND date BETWEEN ? AND ?", [metric, start, end])
            record_change(c, metric, moved | gone)
        written += len(rows_out)
    # Metrics that are not daily metrics of the current policy (daily false,
    # or removed) keep no derived rows (checkpoint B, point 16).
    stale_metric_dates = {d for (d, m) in prev if m not in daily_metrics}
    with db.transaction(conn) as c:
        c.execute("DELETE FROM daily_values WHERE date BETWEEN ? AND ? AND metric NOT IN (SELECT unnest(?))",
                  [start, end, daily_metrics])
        record_change(c, "policy", stale_metric_dates)
    return written


def compute_baselines(conn, policy: MetricPolicy, as_of: date) -> int:
    """Rolling median + MAD per metric per window, from canonical daily values
    strictly before `as_of` (today never contaminates its own baseline). A
    baseline that no longer reaches min_days is removed, not kept."""
    written = 0
    daily_metrics = _daily_metrics(policy)
    for metric in daily_metrics:
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
    # Baselines of metrics or windows the policy no longer has are not kept.
    db.execute(conn, "DELETE FROM baselines WHERE date = ? AND (metric NOT IN (SELECT unnest(?)) "
                     "OR window_days NOT IN (SELECT unnest(?)))", [as_of, daily_metrics, list(policy.windows)])
    return written


def get_baseline(conn, metric: str, as_of: date, window: int) -> dict | None:
    rows = db.fetchdicts(conn, """
        SELECT median, mad, n_days FROM baselines
        WHERE metric = ? AND date = ? AND window_days = ?""", [metric, as_of, window])
    return rows[0] if rows else None
