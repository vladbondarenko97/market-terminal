"""Scheduling rules: NYSE early closes, which scheduled fires are accepted, and the launchd fire times.

Pure date arithmetic, no network and no data folder.
"""
import shutil
import subprocess
import sys
import unittest
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401,E402  (temporary CME_Data, no credentials; must come before importing config)

from core.market_calendar import (CLOSE_ET, EARLY_CLOSE_ET, NEW_YORK, SCHEDULE_ET, early_close_days,  # noqa: E402
                                  is_trading_day, launchd_intervals, scheduled_run_skip_reason, session_close)

ROOT = Path(__file__).resolve().parents[1]


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=NEW_YORK)


class EarlyCloses(unittest.TestCase):
    def test_known_nyse_early_closes(self):
        # Published NYSE 13:00 ET closes. The day after Thanksgiving every year; July 3 and December 24 only when
        # they are trading days (2026: July 3 is the observed holiday; 2027: December 24 is the observed holiday;
        # 2023: December 24 is a Sunday).
        expected = {
            2023: [date(2023, 7, 3), date(2023, 11, 24)],
            2024: [date(2024, 7, 3), date(2024, 11, 29), date(2024, 12, 24)],
            2025: [date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)],
            2026: [date(2026, 11, 27), date(2026, 12, 24)],
            2027: [date(2027, 11, 26)],
        }
        for year, days in expected.items():
            self.assertEqual(sorted(early_close_days(year)), days, year)

    def test_rule_not_a_list(self):
        for year in range(2015, 2060):
            for d in early_close_days(year):
                self.assertTrue(is_trading_day(d), d)           # an early close is always a trading day
            thanksgiving_friday = [d for d in early_close_days(year) if d.month == 11]
            self.assertEqual(len(thanksgiving_friday), 1)
            self.assertEqual(thanksgiving_friday[0].weekday(), 4)
            # July 3 is an early close exactly when it is a trading day (July 4 on Tuesday to Friday)
            self.assertEqual(date(year, 7, 3) in early_close_days(year), is_trading_day(date(year, 7, 3)))
            self.assertEqual(date(year, 12, 24) in early_close_days(year), is_trading_day(date(year, 12, 24)))
        self.assertNotIn(date(2022, 7, 1), early_close_days(2022))       # July 4 on a Monday: no early close
        self.assertNotIn(date(2022, 12, 23), early_close_days(2022))     # December 24 on a Saturday

    def test_session_close(self):
        self.assertEqual(session_close(date(2026, 11, 27)), EARLY_CLOSE_ET)
        self.assertEqual(session_close(date(2026, 10, 6)), CLOSE_ET)

    def test_afternoon_run_skipped_on_early_close(self):
        self.assertIsNone(scheduled_run_skip_reason(at("2026-11-27T09:31")))
        for hhmm in ("13:01", "15:35", "15:45", "16:00"):
            reason = scheduled_run_skip_reason(at(f"2026-11-27T{hhmm}"))
            self.assertIn("after the 13:00 ET early close", reason, hhmm)
        self.assertIsNone(scheduled_run_skip_reason(at("2026-12-24T09:31")))
        self.assertIn("early close", scheduled_run_skip_reason(at("2026-12-24T15:45")))
        # the same time on an ordinary day runs
        self.assertIsNone(scheduled_run_skip_reason(at("2026-12-23T15:45")))


class LateFires(unittest.TestCase):
    def test_slot_windows(self):
        self.assertEqual(SCHEDULE_ET, ((9, 31), (12, 30), (15, 45)))
        run = lambda hhmm: scheduled_run_skip_reason(at(f"2026-10-06T{hhmm}"))      # an ordinary Tuesday
        for hhmm in ("09:21", "09:31", "09:50", "10:01", "12:20", "12:30", "13:00", "15:35", "15:45", "16:00"):
            self.assertIsNone(run(hhmm), hhmm)
        for hhmm in ("00:00", "09:20", "10:02", "11:00", "12:19", "13:01", "15:34"):
            self.assertIn("outside the scheduled run windows", run(hhmm), hhmm)
        self.assertIn("after the 16:00 ET close", run("16:01"))

    def test_mac_waking_from_sleep_is_skipped(self):
        # The 09:31 job missed while asleep and launched by launchd at 11:00 ET must not record a position.
        reason = scheduled_run_skip_reason(at("2026-10-06T11:00"))
        self.assertIn("11:00 ET", reason)
        # seconds do not matter, and neither does the time zone the instant is expressed in
        self.assertIsNone(scheduled_run_skip_reason(at("2026-10-06T10:01").replace(second=45)))
        utc = datetime(2026, 10, 6, 13, 31, tzinfo=timezone.utc)                    # 09:31 EDT
        self.assertIsNone(scheduled_run_skip_reason(utc))
        self.assertIn("outside", scheduled_run_skip_reason(utc + timedelta(hours=2)))

    def test_messages_state_the_real_rule(self):
        reason = scheduled_run_skip_reason(at("2026-10-06T09:20"))
        self.assertNotIn("09:30", reason)
        self.assertIn("09:21-10:01", reason)
        self.assertIn("15:35-16:00", reason)
        self.assertIn("10 minutes before", reason)
        self.assertIn("30 minutes after", reason)
        early = scheduled_run_skip_reason(at("2026-11-27T11:00"))                   # no afternoon window that day
        self.assertIn("09:21-10:01", early)
        self.assertNotIn("15:35", early)

    def test_not_a_trading_day(self):
        self.assertIn("not an NYSE trading day", scheduled_run_skip_reason(at("2026-10-03T09:31")))     # Saturday
        self.assertIn("not an NYSE trading day", scheduled_run_skip_reason(at("2026-11-26T09:31")))     # Thanksgiving
        self.assertIn("not an NYSE trading day", scheduled_run_skip_reason(at("2026-07-03T09:31")))     # July 4 observed


def accepted_runs(tz_name, start, days):
    """Fire every launchd interval on every local date and count what the run gate accepts per (ET date, slot)."""
    tz = ZoneInfo(tz_name)
    intervals = launchd_intervals(tz_name, days=days, start=start)
    counts = Counter()
    first_local = datetime.combine(start - timedelta(days=2), datetime.min.time())
    for i in range(days + 5):
        local_day = (first_local + timedelta(days=i)).date()
        for weekday, hour, minute in intervals:
            if (local_day.weekday() + 1) % 7 != weekday:
                continue
            fire = datetime(local_day.year, local_day.month, local_day.day, hour, minute, tzinfo=tz)
            eastern = fire.astimezone(NEW_YORK)
            if start <= eastern.date() < start + timedelta(days=days) and scheduled_run_skip_reason(fire) is None:
                counts[(eastern.date(), "open" if eastern.hour < 11 else "midday" if eastern.hour < 14 else "close")] += 1
    return counts


def wanted_runs(start, days):
    counts = Counter()
    for i in range(days):
        d = start + timedelta(days=i)
        if is_trading_day(d):
            counts[(d, "open")] = 1
            counts[(d, "midday")] = 1                             # 12:30 is before even a 13:00 early close
            if session_close(d) == CLOSE_ET:                      # no afternoon run on an early close
                counts[(d, "close")] = 1
    return counts


class LaunchdIntervals(unittest.TestCase):
    START = date(2026, 10, 9)

    def test_shape(self):
        result = launchd_intervals(start=self.START)                 # this machine's zone, whatever it is
        self.assertEqual(result, sorted(set(result)))
        self.assertTrue(result)
        for weekday, hour, minute in result:
            self.assertIn(weekday, range(7))
            self.assertIn(hour, range(24))
            self.assertIn(minute, range(60))

    def test_us_central_is_constant_all_year(self):
        # Eastern and Central change on the same days, so one pair of clock times covers every day of the year.
        result = launchd_intervals("America/Chicago", start=self.START)
        self.assertEqual(result, sorted((wd, h, m) for wd in range(1, 6) for h, m in ((8, 31), (11, 30), (14, 45))))

    def test_days_argument(self):
        week = launchd_intervals("America/New_York", days=7, start=date(2026, 10, 5))   # a Monday
        self.assertEqual(week, sorted((wd, h, m) for wd in range(1, 6) for h, m in SCHEDULE_ET))

    def test_arizona_has_both_summer_and_winter_times(self):
        # Arizona never changes its clock; New York does. 09:31 is 06:31 in summer and 07:31 in winter there.
        times = {(h, m) for _, h, m in launchd_intervals("America/Phoenix", start=self.START)}
        self.assertEqual(times, {(6, 31), (7, 31), (9, 30), (10, 30), (12, 45), (13, 45)})

    def test_london_covers_the_weeks_when_the_clocks_differ(self):
        times = {(h, m) for _, h, m in launchd_intervals("Europe/London", start=self.START)}
        self.assertEqual(times, {(13, 31), (14, 31), (16, 30), (17, 30), (19, 45), (20, 45)})

    def test_tokyo_weekdays_cross_midnight(self):
        result = launchd_intervals("Asia/Tokyo", start=self.START)
        evening = {wd for wd, h, _ in result if h >= 22}                # 09:31 ET is 22:31 or 23:31 the same day
        morning = {wd for wd, h, _ in result if h < 12}                 # 12:30 and 15:45 ET fall after midnight, the next day
        self.assertEqual(evening, {1, 2, 3, 4, 5})
        self.assertEqual(morning, {2, 3, 4, 5, 6})
        self.assertEqual({(h, m) for _, h, m in result}, {(22, 31), (23, 31), (1, 30), (2, 30), (4, 45), (5, 45)})

    def test_wrong_side_of_a_clock_change_is_skipped(self):
        # Monday 2026-11-30 (New York on EST): Arizona's 06:31 is 08:31 ET, its 07:31 is 09:31 ET.
        mst = ZoneInfo("America/Phoenix")
        self.assertIn("outside", scheduled_run_skip_reason(datetime(2026, 11, 30, 6, 31, tzinfo=mst)))
        self.assertIsNone(scheduled_run_skip_reason(datetime(2026, 11, 30, 7, 31, tzinfo=mst)))
        # Monday 2026-10-12 (EDT): the other way round.
        self.assertIsNone(scheduled_run_skip_reason(datetime(2026, 10, 12, 6, 31, tzinfo=mst)))
        self.assertIn("outside", scheduled_run_skip_reason(datetime(2026, 10, 12, 7, 31, tzinfo=mst)))

    def test_every_slot_of_the_next_year_runs_exactly_once(self):
        # The point of the intervals: through every daylight-saving change (US, European, none) each trading day
        # gets its morning run, and its afternoon run unless the market closes early, once and only once.
        days = 371
        for tz_name in ("America/Chicago", "America/Phoenix", "Europe/London", "Asia/Tokyo"):
            for start in (self.START, date(2027, 2, 20)):
                with self.subTest(tz=tz_name, start=start):
                    self.assertEqual(accepted_runs(tz_name, start, days), wanted_runs(start, days))


class SetupScript(unittest.TestCase):
    def test_setup_sh_parses_and_uses_the_calendar(self):
        text = (ROOT / "setup.sh").read_text()
        self.assertIn("launchd_intervals", text)
        self.assertNotIn("for WD in 1 2 3 4 5", text)
        bash = shutil.which("bash")
        if bash:
            done = subprocess.run([bash, "-n", str(ROOT / "setup.sh")], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_uninstall_removes_only_our_plugin_link(self):
        text = (ROOT / "setup.sh").read_text()
        uninstall = text[text.index('if [ "$UNINSTALL" = 1 ]'):text.index("# --- 1. Homebrew")]
        self.assertIn('readlink "$LINK"', uninstall)                     # only a link that points at this repository
        self.assertIn('rm -f "$LINK"', uninstall)


if __name__ == "__main__":
    unittest.main()
