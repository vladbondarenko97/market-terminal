"""Silver Eagle premium, dealer flow and paper:physical history models. Offline, synthetic data."""
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401,E402  (temporary CME_Data; must come before importing config)

from core import metrics  # noqa: E402

CHICAGO = ZoneInfo("America/Chicago")


def reading(at, spot, cheap, avg=None):
    return {"ts": datetime.fromisoformat(at), "spot": spot, "cheap": cheap, "avg": avg}


def eagle_run(at, **sold):
    """A run at an ISO UTC time; `sold` maps an item id to its sold_qty (a listing per key)."""
    return {"run_at": datetime.fromisoformat(at), "listings": {
        k: {"title": k, "total_cost": 70.0, "premium_percent": 10.0, "status": "IN_STOCK", "sold_qty": v}
        for k, v in sold.items()}}


class EaglePremium(unittest.TestCase):
    def test_stats_use_the_last_reading_of_each_day(self):
        # day 1: 10% then 20% (counts as 20); day 2: 30%; day 3: 40%; every spot is 100
        hist = [reading("2026-10-01T09:00", 100, 110), reading("2026-10-01T15:00", 100, 120),
                reading("2026-10-02T15:00", 100, 130)]
        out = metrics.eagle_premium(hist, reading("2026-10-03T15:00", 100, 140, 150))
        s = out["stats"]
        self.assertEqual((out["status"], out["version"]), ("fresh", metrics.EAGLE_VERSION))
        self.assertEqual((s["n"], s["mean"], s["std"], s["median"], s["min"], s["max"]), (3, 30.0, 10.0, 30.0, 20.0, 40.0))
        self.assertEqual((s["zscore"], s["pct_rank"]), (None, None))      # under 20 days
        self.assertEqual((s["history"]["n"], s["history"]["days"], s["history"]["min"]), (4, 3, 10.0))
        self.assertEqual(s["changes"]["1d"]["ref_label"], "2026-10-02 15:00")
        self.assertEqual(s["changes"]["1d"]["pts"], 10.0)
        self.assertIsNone(s["changes"]["7d"])
        self.assertIsNone(s["changes"]["30d"])
        self.assertEqual(s["latest"]["label"], "2026-10-03 15:00")
        self.assertEqual((s["latest"]["cheapest_pct"], s["latest"]["avg_pct"], s["latest"]["avg_dollar"]), (40.0, 50.0, 50.0))
        json.dumps(out, allow_nan=False)

    def test_zscore_and_rank_with_twenty_days(self):
        rows = [reading(f"2026-10-{d:02d}T15:00", 100, 100 + d) for d in range(1, 21)]      # premiums 1..20
        s = metrics.eagle_premium(rows[:-1], rows[-1])["stats"]
        self.assertEqual((s["n"], s["mean"], s["median"]), (20, 10.5, 10.5))
        self.assertEqual(s["std"], 5.92)
        self.assertEqual(s["zscore"], 1.61)          # (20 - 10.5) / 5.916
        self.assertEqual(s["pct_rank"], 95.0)        # 19 of 20 days are strictly lower
        self.assertEqual(s["changes"]["7d"]["ref_label"], "2026-10-13 15:00")

    def test_unverified_readings_are_skipped(self):
        hist = [reading("2026-10-01T10:00", None, 55), reading("2026-10-01T11:00", 50, 0), reading("2026-10-01T12:00", 50, 55)]
        s = metrics.eagle_premium(hist, reading("2026-10-02T12:00", 40, 44))["stats"]
        self.assertEqual(s["history"]["n"], 2)
        self.assertIsNone(metrics.eagle_premium([], reading("2026-10-02T12:00", 40, 44))["stats"]["latest"]["avg_price"])

    def test_a_run_without_a_reading_describes_the_history(self):
        hist = [reading("2026-10-01T12:00", 50, 55), reading("2026-10-02T12:00", 40, 44, 48)]
        for cur in (None, reading("2026-10-03T12:00", 40, None), reading("2026-10-03T12:00", float("nan"), 44)):
            out = metrics.eagle_premium(hist, cur)
            self.assertEqual((out["status"], out["stats"]["latest"]["label"]), ("fresh", "2026-10-02 12:00"))
            self.assertIn("up to 2026-10-02 12:00", out["reason"])
        self.assertIsNone(metrics.eagle_premium(hist, reading("2026-10-03T12:00", 40, 44))["reason"])

    def test_no_verified_reading_is_missing(self):
        out = metrics.eagle_premium([reading("2026-10-01T12:00", None, 55)], None)
        self.assertEqual((out["status"], out["reason"], out["version"]),
                         ("missing", "no Silver Eagle premium recorded yet", metrics.EAGLE_VERSION))


class ListingFlow(unittest.TestCase):
    def test_leaves_out_listings_without_a_delta(self):
        runs = [eagle_run("2026-10-01T12:00:00+00:00", a=100, b=50, c=None, d=10),
                eagle_run("2026-10-03T12:00:00+00:00", a=110, b=40, c=5, d=20),       # b went down, c had no count
                eagle_run("2026-10-09T12:00:00+00:00", a=130, c=9, d=25, e=3)]        # b gone, e new
        out = metrics.listing_flow(runs, CHICAGO)
        flow = out
        self.assertEqual((flow["status"], flow["reason"]), ("fresh", None))
        self.assertEqual(flow["sold_prev"], {"a": 20, "c": 4, "d": 5})
        self.assertEqual(flow["sold_7d"], {"a": 30, "d": 15})
        self.assertEqual((flow["prev_count"], flow["version"]), (4, metrics.EAGLE_VERSION))   # the run before had a, b, c, d
        self.assertEqual(flow["units_prev"], 29)
        # the 7-day reference is the 10-01 run (8 days back; the 10-03 run is only 6 days back)
        self.assertEqual(flow["rate_7d"]["ref_run_at"], "2026-10-01 07:00")
        self.assertEqual((flow["rate_7d"]["units"], flow["rate_7d"]["listings"]), (30 + 15, 2))
        self.assertIsNone(flow["rate_prior_7d"])
        # the daily credit goes to the later run's local date; pairs with no delta add nothing
        self.assertEqual(flow["daily"], [{"date": "2026-10-03", "units": 10 + 10}, {"date": "2026-10-09", "units": 20 + 4 + 5}])
        json.dumps(out, allow_nan=False)

    def test_one_run_has_no_flow(self):
        self.assertEqual(metrics.listing_flow([eagle_run("2026-10-01T12:00:00+00:00", a=1)], CHICAGO),
                         {"status": "missing", "reason": "no earlier run with listings", "version": metrics.EAGLE_VERSION})

    def test_card_stats_leave_the_listing_dicts_alone(self):
        from core import collect, lake
        conn = lake.connect(":memory:")
        lake.migrate(conn)
        self.addCleanup(conn.close)
        listings = [{"item_id": "a", "total_cost": 70.0, "premium_percent": 10.0, "status": "IN_STOCK", "sold_qty": 5}]
        before = [dict(l) for l in listings]
        ebay = {"listings": listings, "benchmark": {"value": 50.0}, "cheapest": 70.0, "average": 72.0}
        collect._card_stats(conn, None, {"generated_at": "2026-10-09T19:45:01+00:00"}, ebay, {})
        self.assertEqual(listings, before)
        self.assertEqual(ebay["flow"]["status"], "missing")
        self.assertEqual((ebay["book"]["count"], ebay["book"]["cheapest_pct"]), (1, 10.0))


class ListingRunsAsOf(unittest.TestCase):
    def test_an_old_cutoff_returns_the_runs_before_it(self):
        from core import history, lake
        conn = lake.connect(":memory:")
        lake.migrate(conn)
        self.addCleanup(conn.close)
        for i, day in enumerate(("2026-06-01", "2026-07-01", "2026-08-01", "2026-10-01")):
            at = f"{day}T12:00:00+00:00"
            lake.create_run(conn, f"r{i}", mode="live", trigger="test", run_folder="x", code_version="t")
            lake.update_run(conn, f"r{i}", status="committed")
            lake.record_observations(conn, [lake.obs("ebay.listing", at, entity="a", value={"item_id": "a"}, source="t")], run_id=f"r{i}")
        days = lambda runs: [r["run_at"].strftime("%m-%d") for r in runs]
        self.assertEqual(days(history.eagle_listing_runs(conn)), ["10-01"])
        # 60 days before the newest run: the runs of the 36 days before that cutoff (07-01 and 08-01, not 06-01)
        self.assertEqual(days(history.eagle_listing_runs(conn, "2026-08-02T00:00:00+00:00")), ["07-01", "08-01"])


class ListingBook(unittest.TestCase):
    def test_counts_stock_and_takes_the_median(self):
        mk = lambda pct, status: {"premium_percent": pct, "status": status}
        out = metrics.listing_book([mk(10, "IN_STOCK"), mk(20, "LIMITED_STOCK"), mk(40, "OUT_OF_STOCK"), mk(30, "IN_STOCK")])
        self.assertEqual(out, {"version": metrics.EAGLE_VERSION, "count": 4, "in_stock": 3, "cheapest_pct": 10.0, "median_pct": 25.0})

    def test_no_premiums_gives_nulls(self):
        out = metrics.listing_book([{"premium_percent": None, "status": "IN_STOCK"}, {"status": "IN_STOCK"}])
        self.assertEqual((out["count"], out["cheapest_pct"], out["median_pct"]), (2, None, None))


def pp(day, ratio, oi=None, hour=15):
    ts = datetime(2026, 9, 1) + timedelta(days=day, hours=hour)
    return {"ts": ts, "label": ts.strftime("%Y-%m-%d %H:%M"), "ratio": ratio, "oi": oi}


class PaperPhysicalHistory(unittest.TestCase):
    def test_zero_and_null_ratios_are_excluded_and_counted(self):
        out = metrics.paper_physical_history([pp(0, 0), pp(1, None), pp(2, 6.0), pp(3, 8.0)], None)
        self.assertEqual((out["n"], out["excluded"], out["low"], out["high"], out["median"]), (2, 2, 6.0, 8.0, 7.0))
        self.assertEqual(out["first_label"], pp(2, 0)["label"])
        self.assertEqual(out["latest"], {"ratio": 8.0, "label": pp(3, 0)["label"]})
        self.assertEqual(out["version"], metrics.PAPER_PHYSICAL_HISTORY_VERSION)

    def test_no_reading_is_missing(self):
        out = metrics.paper_physical_history([pp(0, 0), pp(1, None)], None)
        self.assertEqual((out["status"], out["reason"]), ("missing", "no ratio recorded yet"))

    def test_percentile_side_and_value_under_twenty_readings_is_null(self):
        hist = [pp(d, float(d + 1)) for d in range(19)]               # ratios 1..19
        self.assertIsNone(metrics.paper_physical_history(hist, None)["percentile"])
        # readings 1..19 and a latest of 2: 17 of 20 (3..19) are strictly above -> below 85%
        low = metrics.paper_physical_history(hist, pp(19, 2.0))
        self.assertEqual(low["percentile"], {"side": "below", "pct": 85})
        # readings 1..20: the median 10.5 is under the latest, 19 of 20 strictly below -> above 95%
        high = metrics.paper_physical_history(hist, pp(19, 20.0))
        self.assertEqual(high["percentile"], {"side": "above", "pct": 95})

    def test_percentile_tie_rounds_up(self):
        hist = [pp(d, float(d + 1)) for d in range(39)]              # ratios 1..39
        out = metrics.paper_physical_history(hist, pp(39, 14.0))     # 25 of 40 are above 14: 62.5%
        self.assertEqual(out["percentile"], {"side": "below", "pct": 63})

    def test_d30_with_and_without_open_interest(self):
        hist = [pp(0, 5.0, 100.0), pp(1, 5.5, 100.0), pp(10, 6.0, None)]
        cur = pp(32, 4.0, 90.0)       # day 2 is 30 days back: the day-1 reading is the newest at least 30 days before
        d30 = metrics.paper_physical_history(hist, cur)["d30"]
        self.assertEqual((d30["label"], d30["ratio"], d30["ratio_chg_pct"]), (pp(1, 0)["label"], 5.5, -27.27))
        self.assertEqual((d30["oi_chg_pct"], d30["registered_chg_pct"]), (-10.0, 23.75))   # 90/4 over 100/5.5 less one
        # the reference without open interest: no open-interest changes
        d30 = metrics.paper_physical_history([pp(0, 5.0, None)], pp(31, 4.0, 90.0))["d30"]
        self.assertEqual((d30["oi_chg_pct"], d30["registered_chg_pct"]), (None, None))
        self.assertIsNone(metrics.paper_physical_history([pp(0, 5.0)], pp(29, 4.0))["d30"])

    def test_without_a_current_reading_the_newest_ledger_row_is_latest(self):
        out = metrics.paper_physical_history([pp(0, 5.0), pp(1, 6.0)], None)
        self.assertEqual(out["latest"]["ratio"], 6.0)
        json.dumps(out, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
