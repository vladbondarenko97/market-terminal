"""The lake backup's timeline (scripts/backup_lake_hf.keep): which copies survive. No network, fixed dates."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import support  # noqa: F401  (offline environment, before config is imported)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import backup_lake_hf as b  # noqa: E402


def copies(start, days, runs=(13, 16, 19)):
    """Three runs a day (UTC hours) for `days` days ending at `start`."""
    return [(start - timedelta(days=d)).replace(hour=h).strftime(b.FMT) for d in range(days) for h in runs]


class Timeline(unittest.TestCase):
    def test_a_year_of_runs_keeps_runs_days_weeks_and_months(self):
        names = copies(datetime(2026, 11, 17), 365) + [".gitattributes", "notes.txt"]
        kept = b.keep(names)
        self.assertLessEqual(len(kept), 20)
        tiers = [t for v in kept.values() for t in v]
        self.assertEqual({t: tiers.count(t) for t in set(tiers)}, {"run": 3, "day": 7, "week": 4, "month": 6})
        self.assertIn("db/20261117T190000Z.db", kept)                       # the newest
        self.assertIn("db/20261117T130000Z.db", kept)                       # today's first run (one of the last 3)
        self.assertIn("db/20261111T190000Z.db", kept)                       # a week ago, the day's last run
        self.assertIn("db/20260630T190000Z.db", kept)                       # June's last copy (sixth month back)
        self.assertNotIn("db/20260531T190000Z.db", kept)                    # May: past the monthly tier
        self.assertNotIn(".gitattributes", kept)                            # not a copy: never kept or deleted

    def test_a_new_store_keeps_everything(self):
        names = copies(datetime(2026, 11, 17), 1)
        self.assertEqual(set(b.keep(names)), set(names))

    def test_a_gap_in_runs_still_keeps_the_last_seven_days_that_had_one(self):
        names = copies(datetime(2026, 11, 17), 2) + copies(datetime(2026, 10, 1), 10)   # six weeks with no run
        days = {n[3:11] for n, t in b.keep(names).items() if "day" in t}
        self.assertEqual(len(days), 7)
        self.assertIn("20260927", days)                                     # 2 recent days + the 5 before the gap
        self.assertNotIn("20260926", days)


if __name__ == "__main__":
    unittest.main()
