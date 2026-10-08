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
from decimal import ROUND_HALF_UP, Decimal

from heliosd.signals import episodes
from heliosd.store import db
from heliosd.trust import confidence as conf
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.schema import base_device, split_device_key
from heliosd.trust.registry import SourceRegistry
from heliosd.trust.schema import base_device, split_device_key

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
# whoop_cycle (B7): a Whoop cycle runs from one sleep onset to the next, and
# its strain belongs to the day of the cycle's recovery (the wake that opens
# it), found in whoop_records by _cycle_days; without a recovery, the date 12
# hours after the cycle starts (the same day for every cycle that has one).
_CYCLE_NO_RECOVERY = timedelta(hours=12)


def detail_in_progress(detail) -> bool:
    """Whether a daily value's detail (a dict, or the JSON text stored in
    daily_values.detail) marks the value as still in progress: an open Whoop
    cycle's strain so far (B7). Such a value has no confidence and no grade,
    its signal is in_progress, and it is not a leftover of a closed day."""
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            return False
    return isinstance(detail, dict) and detail.get("in_progress") is True


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


def _hours2(h: float) -> float:
    """Hours to two decimals, half up on the decimal value (the stored
    precision of a night; Python's round() would send 2.675 down)."""
    return float(Decimal(repr(h)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _rows_sleep(conn, policy: MetricPolicy, metric: str, start: date, end: date) -> list[tuple]:
    """sleep_duration (design B1): one asleep-hours row per key per night,
    filed on the night's wake date, never summed across sources and never
    across a midnight. n = 1 (one record a night; the grade's coverage part
    stays n/3, owner question Q6). detail says what the value covers:
    {"start", "end", "window", "basis"}.

    - A device's own night record, a direct sleep_duration sample (the Whoop
      API night, wh:sleep_duration:sleep:<id>, already filed by its end): the
      longest of the night (two records on one night are revisions or a
      split, never additive); with `sync_paths` for the device only rows of
      those paths count. Its start and end are the record's in-bed edges:
      window "in_bed", basis "whoop_api".
    - Every other key: its main sleep episode (signals/episodes.py), the
      union of its asleep stages with the 60 minute gap rule, naps and
      fragments under 3 h left out: window "asleep" (first and last asleep
      instant), basis "episode". A device whose night comes from its API
      (episodes.HEALTHKIT_COPY_DEVICES: Whoop) builds its stage rows into the
      qualified key whoop:healthkit instead, so its HealthKit copy never
      stands in as its API night. 'in_bed' and 'awake' never count as sleep.
    - whoop:healthkit (design B2, owner decision D6): Whoop's HealthKit
      episode, only where a priority or corroboration list names the key. It
      arbitrates as a key of its own, so on a night with no API record it is
      the value labelled as a fallback (A6), and it never corroborates the
      API record of its own night (_others).
    The detail's start and end are the context window (signals/context.py)."""
    keys = _row_keys(policy, metric)
    paths = policy.sync_paths(metric)
    nights: dict[tuple[date, str], tuple] = {}
    plain = [k for k in keys if split_device_key(k)[1] is None]
    if plain:
        best: dict[tuple[date, str], tuple] = {}
        for d, dk, path, s, e, v, sid in db.fetchall(conn, f"""
                SELECT CAST(end_ts AS DATE), device_key, sync_path, start_ts, end_ts, ROUND(value, 2), sample_id
                FROM eligible_samples WHERE metric = 'sleep_duration' AND value IS NOT NULL
                  AND device_key IN ({", ".join(["?"] * len(plain))}) AND CAST(end_ts AS DATE) BETWEEN ? AND ?""",
                [*plain, start, end]):
            if dk in paths and path not in paths[dk]:
                continue
            if (d, dk) not in best or (v, e, sid) > best[(d, dk)][:3]:
                best[(d, dk)] = (v, e, sid, s)
        for (d, dk), (v, e, _sid, s) in best.items():
            nights[(d, dk)] = (d, dk, v, 1, {"start": s, "end": e, "window": "in_bed", "basis": "whoop_api"})
    built = [k for k in keys if episodes.episode_key(k) == k]
    if built:
        for (k, d), ep in episodes.main_sleep_episodes(conn, policy, start, end, devices=built).items():
            # A device's own API record of the night wins over its stage rows.
            nights.setdefault((d, k), (d, k, _hours2(ep.asleep_h), 1, {
                "start": ep.start, "end": ep.end, "window": "asleep", "basis": "episode"}))
    return list(nights.values())


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
      that holds them (_rows_sleep_end, _wake_runs);
    - whoop_cycle: the start date 12 hours on, the day of a cycle with no
      recovery; _rows_generic files a cycle that has a recovery on the
      recovery's day (_cycle_days)."""
    basis = policy.day_basis(metric)
    if basis == "interval_midpoint":
        return _DAY_MIDPOINT
    if basis == "sleep_end":
        return _DAY_END
    if basis == "whoop_cycle":
        return f"CAST(start_ts + INTERVAL {int(_CYCLE_NO_RECOVERY.total_seconds() // 3600)} HOUR AS DATE)"
    return _DAY_CALENDAR


def _key_case(policy: MetricPolicy, metric: str, keys: list[str]) -> tuple[str, list]:
    """A SQL CASE (over eligible_samples) giving the arbitration key among
    `keys` a row counts under, NULL when it counts under none, with its
    parameters (design 1.0 point 2, B5, B6):
    - a plain key without a sync_paths entry: every row of that device;
    - a plain key with `sync_paths: {device: [path, ...]}`: only that device's
      rows from those paths (whoop: [whoop_live] is Whoop's cloud value); its
      rows from other paths are stored and never arbitrated under it;
    - a qualified key `<device>:healthkit`: that device's rows from every path
      outside its sync_paths entry (Whoop's HealthKit copy), as a key of its
      own, only where a list names it. Without a sync_paths entry the plain
      key takes every path and the qualified key matches nothing (the
      registry check refuses such a policy)."""
    sp = policy.sync_paths(metric)
    parts: list[str] = []
    params: list = []
    for k in keys:
        base, qualifier = split_device_key(k)
        paths = sp.get(base)
        ph = ", ".join(["?"] * len(paths or []))
        if qualifier is None and not paths:
            parts.append("WHEN device_key = ? THEN ?")
            params += [base, k]
        elif qualifier is None:
            parts.append(f"WHEN device_key = ? AND sync_path IN ({ph}) THEN ?")
            params += [base, *paths, k]
        elif paths:
            parts.append(f"WHEN device_key = ? AND sync_path NOT IN ({ph}) THEN ?")
            params += [base, *paths, k]
    return ("CASE " + " ".join(parts) + " END", params) if parts else ("NULL", [])


def _wake_runs(conn, policy: MetricPolicy,
               points: list[tuple[str, datetime]]) -> list[tuple[str, datetime, datetime, date]]:
    """sleep_end points, (key, wall instant) sorted by key then instant: the
    wake date of the key's main sleep episode holding each point, from the
    episode builder (signals/episodes.py point_wake_dates, one call per key),
    compressed into runs of consecutive points that share a wake date:
    (key, first instant, last instant, wake date). A point no main episode
    holds is in no run. The result is a function of the instant, so the runs
    of a key are disjoint and SQL can range-join every point to its run: a
    year of watch and strap readings (about 140,000 points) is a few
    thousand runs, where per-point parameters took over 20 s to bind."""
    from heliosd.signals import episodes      # at call time: the builder may import this module
    by_key: dict[str, list[datetime]] = {}
    for k, ts in points:
        by_key.setdefault(k, []).append(ts)
    runs: list[tuple[str, datetime, datetime, date]] = []
    for k, instants in by_key.items():
        try:
            wake = episodes.point_wake_dates(conn, policy, k, instants)
        except NotImplementedError:
            # Wave 2 integration: the S0 stub raises until group A's episode
            # builder lands; until then a point keeps its own date, as before
            # Wave 2. Remove this fallback with the stub.
            wake = [ts.date() for ts in instants]
        cur: list | None = None
        for ts, w in zip(instants, wake, strict=True):
            if cur is not None and w == cur[3]:
                cur[2] = ts
                continue
            if cur is not None:
                runs.append((cur[0], cur[1], cur[2], cur[3]))
            cur = [k, ts, ts, w] if w is not None else None
        if cur is not None:
            runs.append((cur[0], cur[1], cur[2], cur[3]))
    return runs


def _rows_generic(conn, policy: MetricPolicy, metric: str, start: date, end: date) -> list[tuple]:
    """Every metric without a row function of its own: the policy's aggregation
    (_AGG_SQL) of each key's (_row_keys, _key_case) eligible samples per
    reporting day (_day_expr). On interval_midpoint only the latest-ending
    row of each same-start group of a key counts (ties by the greater
    sample_id). On sleep_end an interval files on its end date and a point on
    the wake date of its key's main sleep episode (_rows_sleep_end); on
    whoop_cycle a cycle files on its recovery's day (_rows_cycle). detail is
    None, except for a day that holds an open Whoop cycle: {"cycle_id",
    "in_progress": true}."""
    keys = _row_keys(policy, metric)
    if not keys:
        return []
    key_sql, key_params = _key_case(policy, metric, keys)
    devices = sorted({base_device(k) for k in keys})
    dev_ph = ", ".join(["?"] * len(devices))
    fn = _AGG_SQL[policy.agg(metric)]
    basis = policy.day_basis(metric)
    if basis == "sleep_end":
        return _rows_sleep_end(conn, policy, metric, start, end, key_sql, key_params, devices, fn)
    if basis == "whoop_cycle":
        return _rows_cycle(conn, policy, metric, start, end, key_sql, key_params, devices, fn)
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


def _cycle_days(conn, policy: MetricPolicy, metric: str) -> dict[str, tuple[date | None, str | None]]:
    """{sample id of a Whoop cycle's `metric` sample (wh:<metric>:cycle:<id>):
    (the reporting day of the cycle's recovery, None without one; the cycle
    id while the cycle is open (whoop_records.end_utc NULL), else None)}. A
    recovery files on its created_at rendered in the reporting zone: the wall
    date of its recovery_score sample, and still known when the recovery
    yields no sample. Read from whoop_records (one row per record)."""
    rows = db.fetchall(conn, "SELECT kind, native_id, cycle_id, end_utc, created_at FROM whoop_records "
                             "WHERE kind IN ('cycle', 'recovery')")
    recovery_day = {str(cyc if cyc is not None else nat): created.replace(tzinfo=timezone.utc).astimezone(policy.zone).date()
                    for kind, nat, cyc, _end, created in rows if kind == "recovery" and created is not None}
    return {f"wh:{metric}:cycle:{nat}": (recovery_day.get(str(nat)), None if end_utc is not None else str(nat))
            for kind, nat, _cyc, end_utc, _created in rows if kind == "cycle"}


def _rows_sleep_end(conn, policy: MetricPolicy, metric: str, start: date, end: date,
                    key_sql: str, key_params: list, devices: list[str], fn: str) -> list[tuple]:
    """_rows_generic on sleep_end: an interval files on its end date; a point
    on the wake date of its key's main sleep episode (_wake_runs), else on its
    own date, or nowhere when the metric is sample_context sleep_only (plan v2
    4.2: a sleep-only reading outside the night is not the night's value).
    The aggregation is the same _AGG_SQL as every basis."""
    dev_ph = ", ".join(["?"] * len(devices))
    # A point files on its own date or the next one (a pre-midnight reading of
    # a night that ends after midnight); an interval on its end date.
    lo = start - timedelta(days=1)
    points = db.fetchall(conn, f"""
        SELECT * FROM (
          SELECT {key_sql} AS k, start_ts
          FROM eligible_samples
          WHERE metric = ? AND value IS NOT NULL AND device_key IN ({dev_ph}) AND {_DAY_END} BETWEEN ? AND ?
            AND (end_ts IS NULL OR end_ts = start_ts)
        ) WHERE k IS NOT NULL ORDER BY k, start_ts""", [*key_params, metric, *devices, lo, end])
    runs = _wake_runs(conn, policy, points)
    outside = "NULL" if policy.sample_context(metric) == "sleep_only" else "CAST(r.start_ts AS DATE)"
    return db.fetchall(conn, f"""
        WITH w AS (SELECT unnest(?::VARCHAR[]) AS k, unnest(?::TIMESTAMP[]) AS lo, unnest(?::TIMESTAMP[]) AS hi,
                          unnest(?::DATE[]) AS wd),
        r AS (
          SELECT {key_sql} AS k, sample_id, value, start_ts, end_ts, (end_ts IS NULL OR end_ts = start_ts) AS point
          FROM eligible_samples
          WHERE metric = ? AND value IS NOT NULL AND device_key IN ({dev_ph}) AND {_DAY_END} BETWEEN ? AND ?
        ), g AS (
          SELECT r.sample_id, r.k, r.value, r.start_ts, r.end_ts,
                 CASE WHEN NOT r.point THEN CAST(r.end_ts AS DATE) ELSE COALESCE(w.wd, {outside}) END AS d
          FROM r LEFT JOIN w ON r.point AND w.k = r.k AND r.start_ts BETWEEN w.lo AND w.hi
          WHERE r.k IS NOT NULL
          QUALIFY ROW_NUMBER() OVER (PARTITION BY r.sample_id ORDER BY w.wd) = 1
        )
        SELECT d, k, {fn} AS v, COUNT(*) AS n, NULL::VARCHAR AS detail
        FROM g WHERE d BETWEEN ? AND ?
        GROUP BY 1, 2""", [[r[0] for r in runs], [r[1] for r in runs], [r[2] for r in runs], [r[3] for r in runs],
                           *key_params, metric, *devices, lo, end, start, end])


def _rows_cycle(conn, policy: MetricPolicy, metric: str, start: date, end: date,
                key_sql: str, key_params: list, devices: list[str], fn: str) -> list[tuple]:
    """_rows_generic on whoop_cycle: each row's day from its cycle
    (_cycle_days: the recovery's day, else 12 hours after the start), then
    the same _AGG_SQL per (day, key). A day whose rows include an open cycle
    carries {"cycle_id", "in_progress": true}. A handful of rows a year, so
    the day of each row is passed to SQL as a list."""
    dev_ph = ", ".join(["?"] * len(devices))
    # A cycle files on its recovery's day or 12 h after its start: never
    # before its start date, at most a day or so after it.
    rows = db.fetchall(conn, f"""
        SELECT * FROM (
          SELECT {key_sql} AS k, sample_id, start_ts
          FROM eligible_samples
          WHERE metric = ? AND value IS NOT NULL AND device_key IN ({dev_ph}) AND {_DAY_CALENDAR} BETWEEN ? AND ?
        ) WHERE k IS NOT NULL ORDER BY start_ts, sample_id""",
        [*key_params, metric, *devices, start - timedelta(days=2), end])
    cycles = _cycle_days(conn, policy, metric)
    kept: list[tuple[str, date, str, str | None]] = []
    for k, sid, s in rows:
        recovery_day, open_id = cycles.get(sid, (None, None))
        d = recovery_day or (s + _CYCLE_NO_RECOVERY).date()
        if start <= d <= end:
            kept.append((sid, d, k, open_id))
    if not kept:
        return []
    out = db.fetchall(conn, f"""
        WITH m AS (SELECT unnest(?::VARCHAR[]) AS sid, unnest(?::DATE[]) AS d, unnest(?::VARCHAR[]) AS k,
                          unnest(?::VARCHAR[]) AS open_id)
        SELECT m.d, m.k, {fn} AS v, COUNT(*) AS n, MAX(m.open_id) AS open_id
        FROM eligible_samples e JOIN m ON e.sample_id = m.sid
        WHERE e.metric = ? AND e.value IS NOT NULL
        GROUP BY 1, 2""", [[x[0] for x in kept], [x[1] for x in kept], [x[2] for x in kept], [x[3] for x in kept], metric])
    return [(d, k, v, n, {"cycle_id": open_id, "in_progress": True} if open_id is not None else None)
            for d, k, v, n, open_id in out]


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
    corroborates the Whoop API record of the same night. The own-device line
    is in (group A, design B2): a key is never another key's corroboration
    when both are one device, because the copy is the same data."""
    return {dk: row[0] for dk, row in per_device.items() if base_device(dk) != base_device(primary_key)}


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
            if (day == as_of and policy.running_total(metric, primary_key)) or detail_in_progress(detail):
                # Owner decision D7 (fix program A4, audit T12): a running total
                # of the reporting today has no confidence and no grade until
                # the day closes (6 samples at 06:40 graded A). The first pass
                # after midnight grades it (recompute.leftover_dates). Whether
                # a value is a running total can depend on its device (B5:
                # Whoop's cloud resting HR is final, Apple's is so far). An open
                # Whoop cycle's strain so far (B7) stays ungraded until the
                # pull that closes the cycle journals its day.
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
