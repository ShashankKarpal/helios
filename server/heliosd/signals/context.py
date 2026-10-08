"""Context flags that annotate signals instead of letting them false-alarm:
travel (sleep midpoint shift), heat (hot-season months), late_night (bedtime drift)."""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta

from heliosd.store import db

HEAT_MONTHS_DEFAULT = [5, 6, 7, 8, 9, 10]
CONTEXT_DAYS = 14   # past nights the travel flag compares last night with (recompute.CONTEXT_DAYS)


def _sleep_windows(conn, first: date, last: date) -> dict[date, tuple[datetime, datetime]]:
    """{night: (start, end)} for the nights in [first, last] that have a
    sleep_duration value: the window of the night that value is (its detail,
    fix program B1): the owner's main sleep episode, first to last asleep
    instant, or the Whoop API record's in-bed edges. Before Wave 2 the window
    spanned every device's stage rows ending on the date, so each past date
    ran from late the evening before to late that evening and every morning
    read as a schedule shift. A date without a night value (or one stored
    before Wave 2, detail NULL) has no window, so an incomplete night never
    sets a flag."""
    out: dict[date, tuple[datetime, datetime]] = {}
    for d, detail in db.fetchall(conn, """
            SELECT date, detail FROM daily_values
            WHERE metric = 'sleep_duration' AND date BETWEEN ? AND ? AND detail IS NOT NULL""", [first, last]):
        try:
            de = json.loads(detail)
            out[d] = (datetime.fromisoformat(de["start"]), datetime.fromisoformat(de["end"]))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def _sleep_window(conn, day: date) -> tuple[datetime, datetime] | None:
    """The window of the night that ends on `day`, or None (see _sleep_windows)."""
    return _sleep_windows(conn, day, day).get(day)


def _midpoint_hour(w: tuple[datetime, datetime]) -> float:
    mid = w[0] + (w[1] - w[0]) / 2
    return mid.hour + mid.minute / 60.0


def context_flags(conn, day: date, heat_months: list[int] | None = None) -> list[str]:
    flags: list[str] = []
    if day.month in (heat_months or HEAT_MONTHS_DEFAULT):
        flags.append("heat")
    windows = _sleep_windows(conn, day - timedelta(days=CONTEXT_DAYS), day)   # one query for the 15 nights
    last = windows.get(day)
    if last:
        if last[0].hour >= 1 and last[0].hour < 12:  # fell asleep after 01:00
            flags.append("late_night")
        mids = [_midpoint_hour(windows[d]) for d in (day - timedelta(days=i) for i in range(1, CONTEXT_DAYS + 1))
                if d in windows]
        if len(mids) >= 5:
            typical = statistics.median(mids)  # the true median, not the upper middle value (S5)
            shift = abs(_midpoint_hour(last) - typical)
            shift = min(shift, 24 - shift)  # circular
            if shift >= 2.0:
                flags.append("travel_or_shifted_schedule")
    return flags
