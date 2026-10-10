"""NYSE trading days, early closes and the windows the scheduled runs use. Pure functions, no network.

Holidays follow NYSE rules: New Year's Day, MLK Day, Presidents' Day, Good Friday, Memorial Day, Juneteenth,
Independence Day, Labor Day, Thanksgiving, Christmas. A Saturday holiday is observed the Friday before (except
New Year's Day, which NYSE does not move into the prior year); a Sunday holiday the Monday after.
Early closes (13:00 ET): the day after Thanksgiving, July 3 and December 24 when they are trading days.
Unscheduled closures (national days of mourning) are not known in advance and are not listed.

The scheduled runs happen at the `SCHEDULE_ET` slots. launchd can fire a job late (a sleeping Mac runs it on
wake-up), so `scheduled_run_skip_reason()` only lets a fire through close to its slot, and
`launchd_intervals()` lists the local clock times `setup.sh --schedule` must register so the slots stay right
through every daylight-saving change, in any time zone.
"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
CLOSE_ET = time(16, 0)
EARLY_CLOSE_ET = time(13, 0)

# The scheduled runs, as (hour, minute) Eastern: the open, midday and before the close. setup.sh registers their
# local equivalents with launchd.
SCHEDULE_ET = ((9, 31), (12, 30), (15, 45))
# A fire counts as "the slot's run" from this many minutes before to this many minutes after the slot. The
# lead absorbs clock drift; the tail lets a job that launchd runs late (a Mac that woke up) still count.
FIRE_LEAD_MINUTES = 10
FIRE_GRACE_MINUTES = 30


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


def early_close_days(year):
    """Days the NYSE closes at 13:00 ET: the day after Thanksgiving, and July 3 and December 24 when those
    are trading days (July 3 when July 4 falls Tuesday to Friday, December 24 on Monday to Thursday). When
    July 3 or December 24 is itself a holiday or a weekend there is no early close that year."""
    days = {_nth_weekday(year, 11, 3, 4) + timedelta(days=1)}
    for month, day in ((7, 3), (12, 24)):
        d = date(year, month, day)
        if is_trading_day(d):
            days.add(d)
    return days


def session_close(d):
    """Closing time (ET) of the regular session on trading day `d`: 13:00 on an early close, else 16:00."""
    return EARLY_CLOSE_ET if d in early_close_days(d.year) else CLOSE_ET


def _slot(d, hour, minute):
    return datetime.combine(d, time(hour, minute), NEW_YORK)


def scheduled_run_skip_reason(now=None, *, lead_minutes=FIRE_LEAD_MINUTES, grace_minutes=FIRE_GRACE_MINUTES):
    """None when a scheduled run should go ahead now, otherwise why not.

    A scheduled run belongs to a `SCHEDULE_ET` slot on an NYSE trading day. It goes ahead only when `now` is
    from `lead_minutes` before to `grace_minutes` after a slot, and not after the session close (13:00 ET on an
    early-close day, else 16:00 ET). A job launchd fires late, after the Mac wakes from sleep, is skipped instead
    of recording a position stamped at the wrong time. Compared to the minute."""
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    day = now.date()
    if not is_trading_day(day):
        return f"{now:%a %Y-%m-%d} is not an NYSE trading day"
    clock = now.replace(second=0, microsecond=0)
    close = session_close(day)
    close_at = datetime.combine(day, close, NEW_YORK)
    if clock > close_at:
        kind = "early close" if close == EARLY_CLOSE_ET else "close"
        return f"{now:%H:%M} ET is after the {close:%H:%M} ET {kind} on {now:%a %Y-%m-%d}"
    windows = []
    for hour, minute in SCHEDULE_ET:
        slot = _slot(day, hour, minute)
        if slot - timedelta(minutes=lead_minutes) <= close_at:      # the 15:45 slot does not exist on an early close
            windows.append((slot - timedelta(minutes=lead_minutes), min(slot + timedelta(minutes=grace_minutes), close_at)))
    if not any(a <= clock <= b for a, b in windows):
        spans = " and ".join(f"{a:%H:%M}-{b:%H:%M}" for a, b in windows)
        return (f"{now:%H:%M} ET is outside the scheduled run windows ({spans} ET: {lead_minutes} minutes before "
                f"to {grace_minutes} minutes after a run slot, never after the close)")
    return None


def launchd_intervals(tz=None, days=371, start=None):
    """The local fire times launchd needs to run every `SCHEDULE_ET` slot on every NYSE trading day of the
    coming `days` days (default a year plus a week), as a sorted list of distinct `(weekday, hour, minute)`.

    `tz` is a time-zone name, a tzinfo, or None for this machine's zone. The weekday uses launchd's numbering
    (0 = Sunday ... 6 = Saturday) and is the local weekday, which differs from the Eastern one when the slot
    crosses local midnight (Asia). Each day is converted with that day's own offsets, so a zone whose daylight
    saving changes on other dates than New York's (Europe, Australia) or never (Arizona, Asia) gets both clock
    times: the one that applies in each part of the year. `start` is the first Eastern date (default today).
    A fire that lands on the wrong side of a clock change is harmless: `scheduled_run_skip_reason()` skips it."""
    if isinstance(tz, str):
        tz = ZoneInfo(tz)
    first = start or datetime.now(NEW_YORK).date()
    fires = set()
    for offset in range(days):
        d = first + timedelta(days=offset)
        if not is_trading_day(d):
            continue
        for hour, minute in SCHEDULE_ET:
            local = _slot(d, hour, minute).astimezone(tz)
            fires.add(((local.weekday() + 1) % 7, local.hour, local.minute))
    return sorted(fires)
