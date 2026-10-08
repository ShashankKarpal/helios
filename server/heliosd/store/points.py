"""Raw points behind a daily value (Wave 2 B15): GET /api/samples and the MCP
tool query_samples.

Eligible rows only (the eligibility view: registered metrics, usable rows,
excluded devices out, Whoop scored records only, units applied), so a point
listed here is a point the daily values could use. A derived metric (policy
key `derive`, for example glucose_cgm) reads its parent metric's rows from its
own devices. A day is a reporting-zone day: a point belongs to the day its
start wall time falls on, the date daily values bucket a calendar metric by.
Times come back as wall times with the zone's UTC offset. At most MAX_DAYS
days and MAX_POINTS points per call, oldest first; `truncated` says when more
points exist. Bad input raises PointQueryError with the reason, never an
empty answer.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from heliosd.store import db

MAX_DAYS = 92
MAX_POINTS = 10_000
DEFAULT_LIMIT = 2000


class PointQueryError(ValueError):
    """A request the point query cannot answer as asked; the message says why."""


def _day(name: str, s: str) -> date:
    try:
        return date.fromisoformat(str(s))
    except (TypeError, ValueError):
        raise PointQueryError(f"bad {name} date {s!r}: use YYYY-MM-DD") from None


def query_points(conn, policy, registry, metric: str, start: str, end: str, device: str = "",
                 limit: int = DEFAULT_LIMIT) -> dict:
    """The eligible points of `metric` whose start falls on a reporting day in
    [start, end], optionally of one device, oldest first, at most `limit`."""
    if metric not in policy.metrics:
        raise PointQueryError(f"unknown metric {metric!r}; known metrics: {', '.join(sorted(policy.metrics))}")
    derive = policy.derive(metric)
    source = derive["from"] if derive else metric
    devices = list(derive["devices"]) if derive else None
    if device:
        known = {d["key"] for d in registry.devices} | {registry.fallback}
        if device not in known:
            raise PointQueryError(f"unknown device {device!r}; known devices: {', '.join(sorted(known))}")
        if devices is not None and device not in devices:
            raise PointQueryError(f"device {device!r} is not a source of {metric!r}, which reads {source!r} "
                                  f"from {', '.join(devices)} only")
        devices = [device]
    first, last = _day("start", start), _day("end", end)
    if last < first:
        raise PointQueryError(f"end {last} is before start {first}")
    n_days = (last - first).days + 1
    if n_days > MAX_DAYS:
        raise PointQueryError(f"at most {MAX_DAYS} days per call; {first} to {last} is {n_days} days")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise PointQueryError(f"limit must be a whole number from 1 to {MAX_POINTS}, not {limit!r}") from None
    if not 1 <= limit <= MAX_POINTS:
        raise PointQueryError(f"limit must be from 1 to {MAX_POINTS}, not {limit}")
    lo = datetime.combine(first, time.min)
    hi = datetime.combine(last + timedelta(days=1), time.min)
    dev_sql = f" AND device_key IN ({', '.join('?' * len(devices))})" if devices else ""
    rows = db.fetchall(conn, f"""
        SELECT sample_id, start_ts, end_ts, value, text_value, device_key, sync_path FROM eligible_samples
        WHERE metric = ? AND start_ts >= ? AND start_ts < ?{dev_sql}
        ORDER BY start_ts, end_ts, sample_id LIMIT ?""", [source, lo, hi, *(devices or []), limit + 1])
    zone = policy.zone

    def wall(ts: datetime | None) -> str | None:
        return ts.replace(tzinfo=zone).isoformat() if ts is not None else None
    points = [{"start": wall(s), "end": wall(e), "value": v, "text": t, "device": dk, "sync_path": sp, "id": sid}
              for sid, s, e, v, t, dk, sp in rows[:limit]]
    return {"metric": metric, "source_metric": source, "devices": devices, "start": str(first), "end": str(last),
            "days": n_days, "zone": policy.reporting_timezone, "unit": policy.unit(source), "eligible_only": True,
            "limit": limit, "count": len(points), "truncated": len(rows) > limit, "points": points}
