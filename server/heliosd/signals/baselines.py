"""Deterministic math: daily canonical values (trust-arbitrated) and personal
baselines (median + MAD). The LLM never touches this layer.

Reads the ONE eligibility view (eligible_samples): registered metrics only,
usable rows only, excluded devices out, Whoop scored records only, time-valid
rows only, unit rules applied. Never the raw samples table.
"""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta, timezone

from heliosd.store import db
from heliosd.trust import confidence as conf
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.schema import base_device
from heliosd.trust.registry import SourceRegistry

# Aggregation dispatcher (plan v2 4.2): sum, avg, last, min, max. `last` is
# the row that starts latest in the day; rows that share a start go to the one
# that ENDS latest, then to the greatest sample_id (the native id). The end
# rung is Wave 2 B4 (audit M2): Apple rewrites a day summary through the day
# under one start (versions ending in the morning, at midday and at night),
# and only the version that ends last is final; the sample_id rung alone
# (owner decision 2026-10-05, 4c.3, the interim rule) picked whichever
# version had the greater id. A same-instant tie (equal start and end, as
# content twins have) still falls to sample_id. The order is taken from the
# reporting-zone wall (start_ts, end_ts), not from the UTC instants: before
# the Phase 1b migration legacy rows carry no instant, and a comparator that
# put them first or last was shown at checkpoint B (point 22) to let a native
# row the re-read inserts into 2024 outrank every legacy row of that day. In
# the reporting zone (no daylight saving) the wall order equals the instant
# order for every row that has an instant, and the migration asserts the
# consistent rendering (wall = zone rendering of the instant) on every
# migrated row, so after it the instant order is realized exactly.
# Sums run over exact DECIMAL casts: a floating-point SUM depends on the
# order DuckDB's parallel aggregate happens to add the rows in, and on the
# real store that flipped the second decimal of 9 sleep nights and 4 SDNN
# days between two identical runs (dry run, 2026-10-05). Rounding happens
# inside the dispatcher so every path rounds the same way.
_DEC = "CAST(value AS DECIMAL(30,6))"
_AGG_SQL = {"sum": f"CAST(ROUND(SUM({_DEC}), 3) AS DOUBLE)",
            "avg": f"ROUND(CAST(SUM({_DEC}) AS DOUBLE) / COUNT(value), 3)",
            "min": "ROUND(MIN(value), 3)", "max": "ROUND(MAX(value), 3)",
            "last": "ROUND(LAST(value ORDER BY start_ts, end_ts, sample_id), 3)"}
_DEC_MIN = "CAST(SUM(CAST(CASE WHEN text_value IN ('core','deep','rem') THEN value ELSE 0 END AS DECIMAL(30,6))) AS DOUBLE) / 60.0"
_DEC_ASLEEP = "CAST(SUM(CAST(CASE WHEN text_value = 'asleep' THEN value ELSE 0 END AS DECIMAL(30,6))) AS DOUBLE) / 60.0"


def _daily_metrics(policy: MetricPolicy) -> list[str]:
    return [m for m in policy.metrics if policy.daily(m)]


# ---- Day rows: one dispatcher, four row functions (Wave 2 design 1.0 point 3) ----
#
# Every row function returns the rows of ONE metric over [start, end], at most
# one row per reporting day and key, as 5-tuples:
#   (day, key, value, n, detail)
#   day     the reporting date the row files under (a date);
#   key     the arbitration key: a registry device key, or a qualified key such
#           as whoop:healthkit (trust/schema.py); compute_daily_values takes the
#           first key of the metric's priority list that has a row that day;
#   value   that key's value for the day (a row whose value is None is skipped);
#   n       the samples behind it (the coverage part of the grade);
#   detail  None, a dict (stored as JSON with sorted keys, dates as ISO text) or
#           a JSON string: what the value alone cannot say (the night's window,
#           the devices a merged day adds up, an open cycle). Only the winning
#           key's detail is stored, in daily_values.detail.
# One worker group owns each function (design section 2): _rows_sleep group A;
# _rows_generic and _day_expr group B; _row_keys, _rows_merged, _rows_derived
# and _others group C. One query (or one Python pass) per metric keeps a full
# 10-year backfill recompute fast.

# The day bases (trust/schema.py DAY_BASES) as SQL over eligible_samples
# (design B3). Wall times are already in the reporting zone, so no zone math.
_DAY_CALENDAR = "CAST(start_ts AS DATE)"   # the start wall date in the reporting zone
# interval_midpoint: the date that holds the interval's midpoint, so the day
# holding most of it. Exact microsecond arithmetic on the naive walls.
_DAY_MIDPOINT = "CAST(make_timestamp((epoch_us(start_ts) + epoch_us(COALESCE(end_ts, start_ts))) // 2) AS DATE)"
# sleep_end for an interval: its end date, the night's wake date. A point has
# no end of its own; _rows_generic files it on the wake date of the main sleep
# episode that holds it (signals/episodes.py point_wake_dates).
_DAY_END = "CAST(COALESCE(end_ts, start_ts) AS DATE)"


def _row_keys(policy: MetricPolicy, metric: str) -> list[str]:
    """The keys the row functions read rows for. S0 keeps the pre-Wave-2 set:
    the priority list. Wave 2 group C adds the metric's corroboration keys here
    (design 1.0 point 4), so a corroboration-only device has a row to show
    beside the value; it is never chosen, because compute_daily_values takes
    the value from the priority list only."""
    return policy.priority(metric)


def _metric_day_rows(conn, policy: MetricPolicy, metric: str,
                     start: date, end: date) -> list[tuple]:
    """The (day, key, value, n, detail) rows of one metric across the whole
    window, from the row function the policy selects. First match wins: no
    priority list, no rows; sleep_duration, _rows_sleep; `derive`,
    _rows_derived; `merge`, _rows_merged; every other metric, _rows_generic.
    No shipped policy sets derive or merge yet (S0)."""
    if not policy.priority(metric):
        return []
    if metric == "sleep_duration":
        return _rows_sleep(conn, policy, metric, start, end)
    if policy.derive(metric):
        return _rows_derived(conn, policy, metric, start, end)
    if policy.merge(metric):
        return _rows_merged(conn, policy, metric, start, end)
    return _rows_generic(conn, policy, metric, start, end)


def _rows_sleep(conn, policy: MetricPolicy, metric: str, start: date, end: date) -> list[tuple]:
    """sleep_duration (Wave 2 group A replaces this with the main-sleep episode
    builder, signals/episodes.py, design B1 and B2). The pre-Wave-2 rule,
    unchanged: one asleep-hours value per device (_row_keys) per night, filed
    on the end wall date, n = 1, detail None."""
    prio = _row_keys(policy, metric)
    ph = ", ".join(["?"] * len(prio))
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
            SELECT d, device_key, ROUND(v, 2) AS v, 1 AS n, NULL::VARCHAR AS detail FROM (
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


def _day_expr(policy: MetricPolicy, metric: str) -> str:
    """The SQL expression (over eligible_samples) for the reporting day a row
    of `metric` files under in _rows_generic (design B3):
    - calendar: the start wall date (sums keep Apple Health's start-date rule);
    - interval_midpoint: the date of the interval's midpoint, so the day that
      holds most of it (resting HR: Apple's day summary runs from about 22:30
      to 22:29 and belongs to the second day); _rows_generic first keeps only
      the latest-ending member of rows that share a start (one summary that
      the source rewrote), so an early interim version never lands on the
      previous day;
    - sleep_end: the end date of an interval (the night's wake date); points
      are mapped by _rows_generic to the wake date of the main sleep episode
      that holds them (_point_days).
    whoop_cycle (B7) is still the start date here."""
    basis = policy.day_basis(metric)
    if basis == "interval_midpoint":
        return _DAY_MIDPOINT
    if basis == "sleep_end":
        return _DAY_END
    return _DAY_CALENDAR


def _key_case(policy: MetricPolicy, metric: str, keys: list[str]) -> tuple[str, list]:
    """A SQL CASE (over eligible_samples) giving the arbitration key among
    `keys` a row counts under, NULL when it counts under none, with its
    parameters. A key is a registry device key: every row of that device."""
    parts: list[str] = []
    params: list = []
    for k in keys:
        parts.append("WHEN device_key = ? THEN ?")
        params += [k, k]
    return ("CASE " + " ".join(parts) + " END", params) if parts else ("NULL", [])


def _point_days(conn, policy: MetricPolicy, metric: str,
                points: list[tuple[str, str, datetime]]) -> dict[str, date | None]:
    """sleep_end points, (sample_id, key, wall instant) in instant order:
    {sample_id: the wake date of that key's main sleep episode holding the
    point}. A point no main episode holds files on its own date, or nowhere
    (None: dropped) when the metric is sample_context sleep_only (plan v2 4.2:
    a sleep-only reading outside the night is not the night's value). One call
    of the episode builder per key."""
    from heliosd.signals import episodes      # at call time: the builder may import this module
    sleep_only = policy.sample_context(metric) == "sleep_only"
    by_key: dict[str, list[tuple[str, datetime]]] = {}
    for sid, k, ts in points:
        by_key.setdefault(k, []).append((sid, ts))
    out: dict[str, date | None] = {}
    for k, items in by_key.items():
        instants = [ts for _, ts in items]
        try:
            wake = episodes.point_wake_dates(conn, policy, k, instants)
        except NotImplementedError:
            # Wave 2 integration: the S0 stub raises until group A's episode
            # builder lands; until then a point keeps its own date, as before
            # Wave 2. Remove this fallback with the stub.
            wake = [ts.date() for ts in instants]
        for (sid, ts), w in zip(items, wake, strict=True):
            out[sid] = w if w is not None else (None if sleep_only else ts.date())
    return out


def _rows_generic(conn, policy: MetricPolicy, metric: str, start: date, end: date) -> list[tuple]:
    """Every metric without a row function of its own: the policy's aggregation
    (_AGG_SQL) of each key's (_row_keys, _key_case) eligible samples per
    reporting day (_day_expr), detail None. On interval_midpoint only the
    latest-ending row of each same-start group of a key counts (ties by the
    greater sample_id). On sleep_end the day of each row is resolved first
    (intervals by their end date, points through _point_days) and the
    aggregation runs over that mapping; every other basis is one SQL query."""
    keys = _row_keys(policy, metric)
    if not keys:
        return []
    key_sql, key_params = _key_case(policy, metric, keys)
    devices = sorted({base_device(k) for k in keys})
    dev_ph = ", ".join(["?"] * len(devices))
    fn = _AGG_SQL[policy.agg(metric)]
    basis = policy.day_basis(metric)
    if basis == "sleep_end":
        return _rows_mapped(conn, policy, metric, start, end, key_sql, key_params, devices, fn)
    day = _day_expr(policy, metric)
    if basis == "interval_midpoint":
        # A row can file on [start, end] only if it starts by `end` and ends
        # on or after `start`; the members of a same-start group that end
        # before `start` could never be its latest-ending member.
        window, window_params = (f"{_DAY_CALENDAR} <= ? AND {_DAY_END} >= ?", [end, start])
        # The latest end is the one `last` would take: a NULL end sorts last in
        # its ascending order, so it sorts first here.
        latest = "QUALIFY ROW_NUMBER() OVER (PARTITION BY k, start_ts ORDER BY end_ts DESC NULLS FIRST, sample_id DESC) = 1"
    else:
        window, window_params = (f"{_DAY_CALENDAR} BETWEEN ? AND ?", [start, end])
        latest = ""
    return db.fetchall(conn, f"""
        WITH r AS (
          SELECT {key_sql} AS k, sample_id, value, start_ts, end_ts
          FROM eligible_samples
          WHERE metric = ? AND value IS NOT NULL AND device_key IN ({dev_ph}) AND {window}
        ), g AS (
          SELECT *, {day} AS d FROM r WHERE k IS NOT NULL {latest}
        )
        SELECT d, k, {fn} AS v, COUNT(*) AS n, NULL::VARCHAR AS detail
        FROM g WHERE d BETWEEN ? AND ?
        GROUP BY 1, 2""", [*key_params, metric, *devices, *window_params, start, end])


def _rows_mapped(conn, policy: MetricPolicy, metric: str, start: date, end: date,
                 key_sql: str, key_params: list, devices: list[str], fn: str) -> list[tuple]:
    """_rows_generic for a basis whose day is resolved per row in Python
    (sleep_end): read the candidate rows, give each its day (or none), then
    aggregate the rows of each (day, key) in SQL with the same _AGG_SQL, so
    every basis rounds and orders alike."""
    dev_ph = ", ".join(["?"] * len(devices))
    # Candidates: a point files on its own date or the next one (a pre-midnight
    # reading of a night that ends after midnight); an interval on its end date.
    rows = db.fetchall(conn, f"""
        SELECT * FROM (
          SELECT {key_sql} AS k, sample_id, start_ts, end_ts
          FROM eligible_samples
          WHERE metric = ? AND value IS NOT NULL AND device_key IN ({dev_ph}) AND {_DAY_END} BETWEEN ? AND ?
        ) WHERE k IS NOT NULL ORDER BY start_ts, sample_id""",
        [*key_params, metric, *devices, start - timedelta(days=1), end])
    day_of: dict[str, date | None] = {}
    points: list[tuple[str, str, datetime]] = []
    for k, sid, s, e in rows:
        if e is None or e == s:
            points.append((sid, k, s))
        else:
            day_of[sid] = e.date()
    day_of.update(_point_days(conn, policy, metric, points))
    key_of = {sid: k for k, sid, _s, _e in rows}
    kept = [(sid, d) for sid, d in day_of.items() if d is not None and start <= d <= end]
    if not kept:
        return []
    return db.fetchall(conn, f"""
        WITH m AS (SELECT unnest(?::VARCHAR[]) AS sid, unnest(?::DATE[]) AS d, unnest(?::VARCHAR[]) AS k)
        SELECT m.d, m.k, {fn} AS v, COUNT(*) AS n, NULL::VARCHAR AS detail
        FROM eligible_samples e JOIN m ON e.sample_id = m.sid
        WHERE e.metric = ? AND e.value IS NOT NULL
        GROUP BY 1, 2""", [[sid for sid, _ in kept], [d for _, d in kept], [key_of[sid] for sid, _ in kept], metric])


def _rows_merged(conn, policy: MetricPolicy, metric: str, start: date, end: date) -> list[tuple]:
    """`merge: interval` (steps, design B11, owner decision D4; Wave 2 group C).
    Contract: per reporting day, devices in priority order; a sample counts its
    value times the share of its interval no higher-priority device covered,
    then its interval joins the coverage; key = the highest-priority device
    that fed the day; detail = {"fed_by": {device: amount, ...}}. Until group C
    fills it in the key has no runtime effect: the generic rows (one device
    per day, as before Wave 2)."""
    return _rows_generic(conn, policy, metric, start, end)


def _rows_derived(conn, policy: MetricPolicy, metric: str, start: date, end: date) -> list[tuple]:
    """`derive: {from, devices}` (glucose_cgm, design B15; Wave 2 group C).
    Contract: the parent metric's eligible rows from the derive devices, on the
    parent's day basis, keeping only days that meet `coverage` (slot_min,
    min_fraction). Until group C fills it in the key has no runtime effect: the
    generic rows of the metric's own samples (before Wave 2 `derive` was
    accepted and ignored the same way)."""
    return _rows_generic(conn, policy, metric, start, end)


def _others(policy: MetricPolicy, metric: str, primary_key: str,
            per_device: dict[str, tuple]) -> dict[str, float]:
    """The day's corroboration: {key: value} stored beside the value and scored
    for agreement. `per_device` maps each key present that day to its
    (value, n, detail). S0 keeps the pre-Wave-2 rule: every other key present
    (_row_keys is the priority list, so only priority keys are present).
    Wave 2 puts the corroboration rule here (design 1.0 point 4, plan v2 4.2;
    group C, with group A's own-device line): corroboration absent, the other
    priority keys present; [], none; a list, the other priority keys present
    plus the listed keys present; and never another key of the owner's own
    device (trust.schema.base_device), so Whoop's HealthKit copy never
    corroborates the Whoop API record of the same night."""
    return {dk: row[0] for dk, row in per_device.items() if dk != primary_key}


def _iso(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"a daily-value detail holds a {type(o).__name__}, which is not JSON")


def _detail_json(detail) -> str | None:
    """daily_values.detail as stored: None stays NULL, a JSON string is kept,
    a dict or list becomes JSON with sorted keys (dates and times as ISO text),
    so the same facts always store the same text and an unchanged row is never
    journaled as changed."""
    if detail is None or isinstance(detail, str):
        return detail
    return json.dumps(detail, sort_keys=True, default=_iso)


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
    after. `changed`, when given, collects the changed dates for the caller.

    Rows come from _metric_day_rows as (day, key, value, n, detail); the
    winning key's detail is written to daily_values.detail (schema v4) and
    counts as a field for the change journal; the others come from _others."""
    # An aware `now` is rendered in the reporting zone (freshness is judged on
    # reporting dates); a naive one is taken as is; none means the reporting
    # zone's clock, never the Mac's own zone.
    if now is not None and now.tzinfo is not None:
        now = now.astimezone(policy.zone).replace(tzinfo=None)
    now = now or datetime.now(timezone.utc).astimezone(policy.zone).replace(tzinfo=None)
    # The reporting today defaults from that clock, never from the range end:
    # a historical range recomputed without as_of must not treat its last day
    # as the day in progress (fix program A4, Codex A point 1).
    as_of = as_of or now.date()
    tol = float(policy.confidence.get("agreement_tolerance_pct", 12))
    skip = journal_skip or set()
    prev: dict[tuple[date, str], tuple] = {}
    for d, m, v, u, dk, n, cf, g, co, de in db.fetchall(
            conn, "SELECT date, metric, value, unit, device_key, n_samples, confidence, grade, corroboration, detail "
                  "FROM daily_values WHERE date BETWEEN ? AND ?", [start, end]):
        prev[(d, m)] = (v, u, dk, n, cf, g, co, de)
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
        by_day: dict[date, dict[str, tuple[float, int, str | None]]] = {}
        # Read outside the transaction: the helpers take the store lock themselves.
        for d, dk, v, n, detail in _metric_day_rows(conn, policy, metric, start, end):
            if v is not None:
                by_day.setdefault(d, {})[dk] = (float(v), int(n or 0), _detail_json(detail))
        rows_out: list[list] = []
        moved: set[date] = set()
        for day, per_device in by_day.items():
            primary_key = next((dk for dk in prio if dk in per_device), None)
            if primary_key is None:
                continue
            value, n_samples, detail = per_device[primary_key]
            others = _others(policy, metric, primary_key, per_device)
            agreement = conf.agreement_factor(value, list(others.values()), tol)
            age_h = max(0.0, (now - datetime.combine(day, datetime.min.time())).total_seconds() / 3600 - 24)
            fresh = age_h / policy.cadence_hours(metric) if policy.cadence_hours(metric) else 0
            coverage = min(1.0, n_samples / 3) if policy.agg(metric) != "sum" else 1.0
            if day == as_of and policy.running_total(metric):
                # Owner decision D7 (fix program A4, audit T12): a running total
                # of the reporting today has no confidence and no grade until
                # the day closes (6 samples at 06:40 graded A). The first pass
                # after midnight grades it (recompute.leftover_dates).
                score, grade = None, None
            else:
                # freshness only matters for the reporting today; history is settled.
                score, grade = conf.score(policy.confidence, policy.rank(metric, primary_key),
                                          fresh if day == as_of else 0.0, coverage, agreement)
            corr = json.dumps(others, sort_keys=True) if others else None
            row = [day, metric, value, policy.unit(metric), primary_key, n_samples, score, grade, corr, detail, stamp]
            rows_out.append(row)
            p = prev.get((day, metric))
            if p is None or p != (value, policy.unit(metric), primary_key, n_samples, score, grade, corr, detail):
                moved.add(day)
        produced = {r[0] for r in rows_out}
        # Rows in range that this pass did not produce have no eligible input any more.
        gone = {d for (d, m) in prev if m == metric and d not in produced}
        with db.transaction(conn) as c:
            if rows_out:
                # computed_at is written explicitly: DuckDB keeps the column DEFAULT
                # of the replaced row on INSERT OR REPLACE, so the stamp never moved
                # on a rewrite (fix program A21, audit T18). detail likewise: a
                # column left out would keep the replaced row's old detail.
                c.executemany("""
                    INSERT OR REPLACE INTO daily_values
                      (date, metric, value, unit, device_key, n_samples, confidence, grade, corroboration, detail, computed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows_out)
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
