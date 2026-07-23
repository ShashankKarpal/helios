"""Context flags that annotate signals instead of letting them false-alarm:
travel (sleep midpoint shift), heat (hot-season months), late_night (bedtime drift)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from heliosd.store import db

HEAT_MONTHS_DEFAULT = [5, 6, 7, 8, 9, 10]


def _sleep_window(conn, day: date) -> tuple[datetime, datetime] | None:
    """Main sleep session ending on `day` from the most data-rich device."""
    rows = db.fetchall(conn, """
        SELECT MIN(start_ts), MAX(end_ts) FROM samples
        WHERE metric = 'sleep_analysis' AND CAST(end_ts AS DATE) = ?
          AND text_value IN ('asleep','core','deep','rem')""", [day])
    if not rows or rows[0][0] is None:
        return None
    return rows[0][0], rows[0][1]


def _midpoint_hour(w: tuple[datetime, datetime]) -> float:
    mid = w[0] + (w[1] - w[0]) / 2
    return mid.hour + mid.minute / 60.0


def context_flags(conn, day: date, heat_months: list[int] | None = None) -> list[str]:
    flags: list[str] = []
    if day.month in (heat_months or HEAT_MONTHS_DEFAULT):
        flags.append("heat")
    last = _sleep_window(conn, day)
    if last:
        if last[0].hour >= 1 and last[0].hour < 12:  # fell asleep after 01:00
            flags.append("late_night")
        mids = []
        for i in range(1, 15):
            w = _sleep_window(conn, day - timedelta(days=i))
            if w:
                mids.append(_midpoint_hour(w))
        if len(mids) >= 5:
            mids.sort()
            typical = mids[len(mids) // 2]
            shift = abs(_midpoint_hour(last) - typical)
            shift = min(shift, 24 - shift)  # circular
            if shift >= 2.0:
                flags.append("travel_or_shifted_schedule")
    return flags
