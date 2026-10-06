"""NYSE trading days and the windows the scheduled runs use. Pure functions, no network.

Holidays follow NYSE rules: New Year's Day, MLK Day, Presidents' Day, Good Friday, Memorial Day, Juneteenth,
Independence Day, Labor Day, Thanksgiving, Christmas. A Saturday holiday is observed the Friday before (except
New Year's Day, which NYSE does not move into the prior year); a Sunday holiday the Monday after.
Unscheduled closures (national days of mourning) are not known in advance and are not listed.
"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
OPEN_ET = time(9, 30)
CLOSE_ET = time(16, 0)


def _nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year, month, weekday):
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year):
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d):
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year):
    days = {
        _nth_weekday(year, 1, 0, 3),                 # MLK Day
        _nth_weekday(year, 2, 0, 3),                 # Presidents' Day
        _easter(year) - timedelta(days=2),           # Good Friday
        _last_weekday(year, 5, 0),                   # Memorial Day
        _nth_weekday(year, 9, 0, 1),                 # Labor Day
        _nth_weekday(year, 11, 3, 4),                # Thanksgiving
        _observed(date(year, 7, 4)),
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:                      # Saturday: no holiday (would fall in the prior year)
        days.add(_observed(new_year))
    return days


def is_trading_day(d):
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def scheduled_run_skip_reason(now=None, *, early_minutes=10):
    """None when a scheduled run should go ahead now, otherwise why not. Scheduled runs belong to the regular
    session of a trading day (allowing `early_minutes` before the open); a job launchd fires late, after the
    Mac wakes from sleep, outside that window is skipped instead of recording a stale position."""
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    if not is_trading_day(now.date()):
        return f"{now:%a %Y-%m-%d} is not an NYSE trading day"
    start = datetime.combine(now.date(), OPEN_ET, NEW_YORK) - timedelta(minutes=early_minutes)
    end = datetime.combine(now.date(), CLOSE_ET, NEW_YORK)
    if not start <= now <= end:
        return f"{now:%H:%M} ET is outside the regular session ({OPEN_ET:%H:%M}-{CLOSE_ET:%H:%M} ET)"
    return None
