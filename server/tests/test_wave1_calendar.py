"""Wave 1 (fix program 2026-10-08), owner decision D7: the shared calendar
helpers every partial-day fix is built on. These tests fail on the old code
because the helpers did not exist."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from heliosd.ingest.normalize import is_reporting_today, last_complete_day, reporting_today

DUBAI = ZoneInfo("Asia/Dubai")


def test_last_complete_day_is_yesterday_in_the_reporting_zone_not_the_mac_clock():
    # 22:30 UTC on Oct 7 is already 02:30 on Oct 8 in Dubai: today is Oct 8, the
    # last complete day is Oct 7, whatever the Mac's own zone says.
    now = datetime(2026, 10, 7, 22, 30, tzinfo=timezone.utc)
    assert reporting_today(DUBAI, now) == date(2026, 10, 8)
    assert last_complete_day(DUBAI, now) == date(2026, 10, 7)
    # New York zone at the same instant is still Oct 7, so its last complete day is Oct 6.
    assert last_complete_day(ZoneInfo("America/New_York"), now) == date(2026, 10, 6)


def test_last_complete_day_crosses_month_and_year_boundaries():
    assert last_complete_day(DUBAI, datetime(2026, 10, 1, 1, 0, tzinfo=DUBAI)) == date(2026, 9, 30)
    assert last_complete_day(DUBAI, datetime(2027, 1, 1, 0, 5, tzinfo=DUBAI)) == date(2026, 12, 31)


def test_is_reporting_today_matches_only_the_partial_day():
    now = datetime(2026, 10, 8, 6, 40, tzinfo=DUBAI)
    assert is_reporting_today(date(2026, 10, 8), DUBAI, now)
    assert not is_reporting_today(date(2026, 10, 7), DUBAI, now)
    assert not is_reporting_today(date(2026, 10, 9), DUBAI, now)


def test_naive_now_is_read_as_utc_like_reporting_today():
    naive = datetime(2026, 10, 7, 21, 0)  # 01:00 Oct 8 Dubai
    assert last_complete_day(DUBAI, naive) == date(2026, 10, 7)
