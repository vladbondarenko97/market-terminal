"""Fed / Treasury operations (card 15): the fetchers on saved responses (requests.get mocked), the stored history (idempotent,
the latest fetch wins), the summaries, stale labelling, the backfill's year chunks and the stored routes."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401  (temporary CME_Data, no credentials; must come before importing config)

import json
import tempfile
from datetime import date
from unittest import mock

from core import fedops, lake
from core import sources as src
from core.sources import SourceSession, SourceUnavailable
from test_api import ApiCase
from test_chaos import fake_get

FIX = support.FIX / "fedops"
TODAY = date(2026, 10, 26)
load = lambda name: json.loads((FIX / name).read_text())


def fresh_conn():
    conn = lake.connect(Path(tempfile.mkdtemp(prefix="fedops_")) / "portfolio.db")
    lake.migrate(conn)
    return conn


def fetch(fn, payload, *args):
    with mock.patch("requests.get", return_value=fake_get(200, b"", payload)):
        return fn(SourceSession(fresh_conn(), "t"), *args)


class Fetchers(unittest.TestCase):
    def test_repo_days_sum_the_operations_and_keep_each_market(self):
        ops, _ = fetch(src.nyfed_repo_ops, {"repo": {"operations": load("rp.json")}}, "2026-10-20", "2026-10-23")
        rows = {(r["entity"], r["observed_at"]): json.loads(r["value_json"]) for r in fedops.repo_records(ops)}
        rrp = rows[("rrp", "2026-10-22")]
        self.assertEqual((rrp["accepted_bn"], rrp["rate"], rrp["counterparties"], rrp["ops"]), (0.351, 3.75, 2, 1))
        repo = rows[("repo", "2026-10-22")]                     # morning (0) and afternoon (1 mn) operations, minimum bid rate 4%
        self.assertEqual((repo["accepted_bn"], repo["ops"], repo["rate"], repo["counterparties"]), (0.001, 2, 4.0, None))
        self.assertEqual(rows[("repo_term", "2026-10-20")]["accepted_bn"], 0.067)
        self.assertNotIn(("repo_term", "2026-10-21"), rows)       # no operation is no row, never a zero

    def test_missing_amounts_and_rates_never_overwrite_or_become_zero(self):
        ops = load("rp.json")
        morning, afternoon = [dict(o) for o in ops if o["operationDate"] == "2026-10-22" and o["operationType"] == "Repo"]
        afternoon["details"] = [{"securityType": "Treasury", "amtSubmitted": 0, "amtAccepted": 0}]       # no rate field at all
        v = json.loads(fedops.repo_records([morning, afternoon])[0]["value_json"])
        self.assertEqual(v["rate"], 4.0)
        afternoon["totalAmtAccepted"] = None
        v = json.loads(fedops.repo_records([morning, afternoon])[0]["value_json"])
        self.assertIsNone(v["accepted_bn"])

    def test_rates_keep_the_four_reference_rates(self):
        rates, _ = fetch(src.nyfed_ref_rates, {"refRates": load("rates.json")}, "2026-10-22", "2026-10-23")
        recs = fedops.rate_records(rates)
        self.assertEqual({r["entity"] for r in recs}, set(fedops.RATES))
        effr = json.loads(next(r for r in recs if r["entity"] == "EFFR")["value_json"])
        self.assertIn("target_high", effr)

    def test_buybacks_keep_null_as_missing(self):
        rows, _ = fetch(src.treasury_buybacks, load("bb.json"), "2026-10-01")
        vals = [json.loads(r["value_json"]) for r in fedops.buyback_records(rows)]
        scheduled = next(v for v in vals if v["offered_bn"] is None)
        self.assertEqual((scheduled["accepted_bn"], scheduled["max_bn"]), (None, 6.0))
        self.assertTrue(any(v["accepted_bn"] for v in vals))
        with mock.patch("requests.get", return_value=fake_get(200, b"", {"data": [], "meta": {"total-pages": 2}})):
            with self.assertRaises(SourceUnavailable):           # never a silently cut history
                src.treasury_buybacks(SourceSession(fresh_conn(), "t"), "2000-01-01")

    def test_auction_rows(self):
        bill, bond = [src.auction_row(x) for x in load("td.json")]
        self.assertEqual((bill["date"], bill["auctionDate"], bill["term"], bill["high_rate"], bill["high_yield"]), ("2026-10-22", "2026-10-22", "4-Week", 3.945, None))
        self.assertEqual((bond["term"], bond["high_yield"], bond["bid_to_cover"]), ("20-Year", 5.469, 2.73))   # a reopening keeps its original term
        self.assertAlmostEqual(bond["dealer_pct"] + bond["direct_pct"] + bond["indirect_pct"], 100, places=1)
        up = [src.auction_row(x) for x in load("upcoming.json")]
        self.assertEqual(fedops.auction_records(up), [])          # announced, no results yet: not history

    def test_offline_session_makes_no_request(self):
        s = SourceSession(fresh_conn(), "t", offline=True)
        with mock.patch("requests.get", side_effect=AssertionError("network")):
            for call in (lambda: src.nyfed_repo_ops(s, "2026-10-01", "2026-10-02"), lambda: src.nyfed_ref_rates(s, "2026-10-01", "2026-10-02"),
                         lambda: src.treasury_auction_search(s, "2026-01-01", "2026-12-31"), lambda: src.treasury_upcoming(s)):
                with self.assertRaises(SourceUnavailable):
                    call()


def buyback(day, accepted, max_bn=6.0, offered=20.0, sec="Nominal Coupons"):
    return {"operation_date": day, "security_type": sec, "maturity_bucket": "10Y to 20Y", "max_par_amt_redeemed": str(max_bn * 1e9),
            "total_par_amt_offered": "null" if accepted is None else str(offered * 1e9),
            "total_par_amt_accepted": "null" if accepted is None else str(accepted * 1e9)}


class History(unittest.TestCase):
    def test_store_is_idempotent_and_the_latest_fetch_wins(self):
        conn = fresh_conn()
        self.assertEqual(lake.record_observations(conn, fedops.buyback_records([buyback("2026-10-27", None)])), 1)
        self.assertEqual(lake.record_observations(conn, fedops.buyback_records([buyback("2026-10-27", None)])), 0)
        lake.record_observations(conn, fedops.buyback_records([buyback("2026-10-27", 4.08)]))      # the results came out
        rows = fedops.read(conn, fedops.BUYBACK)
        self.assertEqual([(r["date"], r["accepted_bn"]) for r in rows], [("2026-10-27", 4.08)])

    def test_two_operation_types_on_one_day_are_two_rows(self):
        conn = fresh_conn()
        a, b = buyback("2026-10-27", 1.0), buyback("2026-10-27", 2.0)
        a["operation_type"], b["operation_type"] = "Liquidity Support", "Cash Management"
        lake.record_observations(conn, fedops.buyback_records([a, b]))
        self.assertEqual(sorted(r["accepted_bn"] for r in fedops.read(conn, fedops.BUYBACK)), [1.0, 2.0])

    def test_a_buyback_without_security_type_does_not_break_the_summary(self):
        rows = [dict(date="2026-10-01", security_type=None, max_bn=1.0, offered_bn=2.0, accepted_bn=1.0),
                dict(date="2026-10-02", security_type="TIPS", max_bn=1.0, offered_bn=2.0, accepted_bn=0.5)]
        self.assertEqual(set(fedops.buyback_summary(rows, TODAY)["t12m_by_security_type"]), {"None", "TIPS"})

    def test_buyback_summary_year_over_year(self):
        rows = [dict(r, date=r["operation_date"], **json.loads(rec["value_json"])) for r, rec in
                ((b, fedops.buyback_records([b])[0]) for b in (buyback("2024-10-02", 2.0), buyback("2025-03-05", 3.0), buyback("2025-11-05", 4.0),
                                                                 buyback("2026-02-04", 5.0, sec="TIPS"), buyback("2026-10-21", 6.0), buyback("2026-11-04", None)))]
        s = fedops.buyback_summary(rows, TODAY)
        self.assertEqual((s["t12m"]["ops"], s["t12m"]["accepted_bn"]), (3, 15.0))           # 2025-11-05, 2026-02-04, 2026-10-21
        self.assertEqual((s["prior_t12m"]["ops"], s["prior_t12m"]["accepted_bn"]), (1, 3.0))      # 2025-03-05; 2024-10-02 is older
        y = {r["year"]: r for r in s["yearly"]}
        self.assertEqual((y["2025"]["accepted_bn"], y["2025"]["ytd"]["accepted_bn"], y["2026"]["ytd"]["accepted_bn"]), (7.0, 3.0, 11.0))
        self.assertEqual(s["t12m_by_security_type"]["TIPS"]["accepted_bn"], 5.0)
        self.assertEqual([m["period"] for m in s["quarterly"]], ["2024-Q4", "2025-Q1", "2025-Q4", "2026-Q1", "2026-Q4"])

    def test_build_labels_stale_and_missing(self):
        conn = fresh_conn()
        empty = fedops.build(conn, TODAY)
        self.assertEqual((empty["repo"]["status"], empty["buybacks"]["status"]), ("missing", "missing"))
        lake.record_observations(conn, fedops.repo_records(load("rp.json")))
        lake.record_observations(conn, fedops.auction_records([src.auction_row(x) for x in load("td.json")]))
        self.assertEqual(fedops.build(conn, TODAY)["auctions"]["upcoming_status"]["status"], "missing")       # no calendar captured
        lake.store_payload(conn, source="treasury", kind="treasury_upcoming", content=(FIX / "upcoming.json").read_bytes(), fmt="json",
                           fetched_at="2026-10-25T12:00:00+00:00")
        sec = fedops.build(conn, TODAY, {"nyfed:rp": "nyfed: http_500 rp", "treasury:upcoming": "treasury: http_500 upcoming"})
        self.assertEqual((sec["repo"]["status"], sec["repo"]["as_of"]), ("stale", "2026-10-23"))
        self.assertIn("as of 2026-10-23", sec["repo"]["reason"])
        self.assertEqual(sec["repo"]["markets"]["rrp"]["latest"]["accepted_bn"], 0.2)
        self.assertEqual(sec["auctions"]["status"], "fresh")
        self.assertEqual([u["date"] for u in sec["auctions"]["upcoming"]], ["2026-10-26", "2026-10-27"])     # stored: there even when today's fetch failed
        self.assertEqual((sec["auctions"]["upcoming_status"]["status"], sec["auctions"]["upcoming_status"]["as_of"]), ("stale", "2026-10-25"))
        lake.store_payload(conn, source="treasury", kind="treasury_upcoming", content=json.dumps(load("upcoming.json")[1:]).encode(), fmt="json",
                           fetched_at="2026-10-26T12:00:00+00:00")
        self.assertEqual([u["term"] for u in fedops.build(conn, TODAY)["auctions"]["upcoming"]], ["52-Week"])   # dropped from the newest calendar: gone
        self.assertEqual(fedops.build(conn, date(2026, 10, 27))["auctions"]["upcoming"][0]["term"], "52-Week")
        self.assertEqual(sec["auctions"]["by_tenor"]["20-Year Bond"]["by_year"]["2026"]["bid_to_cover_avg"], 2.73)
        self.assertEqual(sec["auctions"]["tail"]["status"], "missing")
        self.assertEqual(set(sec["auctions"]["by_tenor"]), {"4-Week Bill", "20-Year Bond"})
        json.dumps(sec, allow_nan=False)


class Matching(unittest.TestCase):
    def test_question_wording_matches_tenors(self):
        names = ("30-Year Bond", "30-Year TIPS", "10-Year Note", "13-Week Bill", "20-Year Bond", "2-Year Note", "6-Week Bill", "26-Week Bill", "52-Week Bill")
        pick = lambda q: [n for n in names if fedops.tenor_matches(q, n)]
        self.assertEqual(pick("30 year bond"), ["30-Year Bond"])
        self.assertEqual(pick("30yr"), ["30-Year Bond", "30-Year TIPS"])
        self.assertEqual(pick("30 years treasury auctions"), ["30-Year Bond", "30-Year TIPS"])
        self.assertEqual(pick("bonds"), ["30-Year Bond", "20-Year Bond"])
        self.assertEqual(pick("10 year"), ["10-Year Note"])
        self.assertEqual(pick("13 weeks"), ["13-Week Bill"])
        self.assertEqual(pick("6 week"), ["6-Week Bill"])               # not the 26-week
        self.assertEqual(pick("2 week"), [])                            # not the 52-week
        self.assertEqual(pick(""), list(names))

    def test_console_sees_amounts_with_their_unit(self):
        sys.path.insert(0, str(support.ROOT / "options_whale"))
        import assistant
        out = assistant._shape_fed({"max_60d": {"date": "2026-09-30", "accepted_bn": 11.539}, "rows": [{"rrp_bn": 0.2, "sofr": 3.92, "repo_bn": None}]})
        self.assertEqual(out, {"max_60d": {"date": "2026-09-30 (Wednesday)", "accepted": "$11.539 billion"}, "rows": [{"rrp": "$0.200 billion", "sofr": 3.92, "repo": None}]})
        latest = assistant._shape_fed_latest({"data": {"last_8": [], "yearly": [{"year": y, "accepted_bn": n} for y, n in (("2024", 1.0), ("2025", 2.0), ("2026", 3.0))]}})
        self.assertEqual(list(latest["data"]["yearly"]), ["2025", "2026"])
        self.assertEqual(latest["data"]["yearly"]["2025"]["whole_year"], {"accepted": "$2.000 billion"})
        y = assistant._shape_fed_latest({"data": {"last_8": [], "yearly": [{"year": "2025", "accepted_bn": 9.0, "ytd_to": "10-26", "ytd": {"accepted_bn": 4.0}}]}})
        self.assertEqual(y["data"]["yearly"]["2025"]["jan_1_to_10-26"], {"accepted": "$4.000 billion"})
        self.assertEqual(assistant._shape_fed_latest({"data": {"by_tenor": {}, "t12m": {"auctions": 432}}}), {"data": {"by_tenor": {}}})


class Round2(unittest.TestCase):
    """The six answers the iMessage assistant still got wrong on 2026-10-26 after PR 27."""

    def test_monthly_levels_are_averages_highs_and_month_ends(self):
        daily = [{"date": "2026-09-29", "rrp_bn": 2.0, "sofr": 4.0}, {"date": "2026-09-30", "rrp_bn": 11.539, "sofr": 4.2},
                 {"date": "2026-10-01", "rrp_bn": 0.5, "sofr": 3.9}]
        sep = fedops.repo_monthly(daily)[0]
        self.assertEqual((sep["rrp_avg_bn"], sep["rrp_max_bn"], sep["rrp_last_bn"], sep["rrp_last_date"], sep["sofr_avg"]),
                         (6.7695, 11.539, 11.539, "2026-09-30", 4.1))

    def test_year_ago_reading_in_one_call(self):
        rows = [{"date": "2025-10-23", "rate": 4.22}, {"date": "2026-07-24", "rate": 3.95}, {"date": "2026-09-23", "rate": 3.9},
                {"date": "2026-10-23", "rate": 3.92}]
        self.assertEqual(fedops.ago(rows, "rate"), {"1m": {"date": "2026-09-23", "rate": 3.9}, "3m": {"date": "2026-07-24", "rate": 3.95},
                                                     "1y": {"date": "2025-10-23", "rate": 4.22}})
        self.assertEqual(fedops.ago(rows[-1:], "rate")["1y"], None)        # history too short: missing, not the latest

    def test_same_date_comparison_comes_first(self):
        rows = [dict(date=d, operation_type="Liquidity Support", security_type="Nominal Coupons", max_bn=6.0, offered_bn=9.0, accepted_bn=a)
                for d, a in (("2025-03-05", 3.0), ("2025-11-05", 4.0), ("2026-02-04", 5.0), ("2026-10-21", 6.0))]
        s = fedops.buyback_summary(rows, TODAY)
        same = s["this_year_vs_last_year_same_dates"]
        self.assertEqual((same["2026"]["accepted_bn"], same["2025"]["accepted_bn"], same["accepted_change_pct"]), (11.0, 3.0, 266.7))
        self.assertIn("the same dates", same["compares"])

    def test_console_keeps_the_soonest_auctions_and_a_month_table(self):
        sys.path.insert(0, str(support.ROOT / "options_whale"))
        import assistant
        up = [{"date": f"2026-10-{d}", "term": f"t{d}"} for d in range(27, 38)]
        out = assistant._shape_fed_latest({"data": {"by_tenor": {}, "upcoming": up}})
        up = [{"date": "2026-10-27", "term": "52-Week", "type": "Bill", "size_bn": 54.0}, {"date": "2026-10-27", "term": "6-Week", "type": "Bill", "size_bn": 95.0},
              {"date": "2026-10-27", "term": "5-Year", "type": "Note", "size_bn": 70.0}] + up[1:]
        out = assistant._shape_fed_latest({"data": {"by_tenor": {}, "upcoming": up}})
        first = out["data"]["upcoming"]["1. 2026-10-27 (Tuesday)"]                    # every auction of the soonest day, together
        self.assertEqual(first, {"52-Week Bill": "$54.000 billion, new issue", "6-Week Bill": "$95.000 billion, new issue", "5-Year Note": "$70.000 billion, new issue"})
        twin = assistant._shape_fed_latest({"data": {"by_tenor": {}, "upcoming": [{"date": "2026-10-27", "term": "6-Week", "type": "CMB", "size_bn": 10.0},
                                                                                   {"date": "2026-10-27", "term": "6-Week", "type": "CMB", "size_bn": 20.0, "reopening": True}]}})
        self.assertEqual(list(twin["data"]["upcoming"]["1. 2026-10-27 (Tuesday)"].values()), ["$10.000 billion, new issue", "$20.000 billion, reopening"])
        self.assertEqual(len(out["data"]["upcoming"]), 4)
        self.assertIn("2026-10-27 (Tuesday)", assistant.shape(out, last=3))          # the generic cut trims lists, never these dicts
        self.assertEqual(assistant._shape_fed({"accepted_change_bn": -12.5}), {"accepted_change": "-$12.500 billion"})
        bb = assistant._shape_fed_latest({"data": {"last_8": [], "monthly": [{"period": "2026-09", "ops": 7, "accepted_bn": 37.755}]}})
        self.assertEqual(bb["data"]["monthly_totals_last_13_months"]["2026-09"], {"ops": 7, "total_accepted": "$37.755 billion"})


def fixed_now(assistant, when):
    """Pin assistant.datetime.now() (the today line and "so far" labels read it)."""
    p = mock.patch.object(assistant, "datetime", wraps=assistant.datetime)
    dt = p.start()
    dt.now.return_value = __import__("datetime").datetime(*when)
    return p


class HistoryShaping(unittest.TestCase):
    def test_auction_by_year_follows_the_asked_years(self):
        sys.path.insert(0, str(support.ROOT / "options_whale"))
        import assistant
        self.addCleanup(fixed_now(assistant, (2026, 10, 26, 9, 0)).stop)
        body = {"rows": [], "summary": {"by_tenor": {"30-Year Bond": {"by_year": {y: {"auctions": 12, "bid_to_cover_avg": 2.4} for y in ("2021", "2022", "2023", "2025", "2026")}}}}}
        app = mock.Mock()
        app.test_client.return_value.get.return_value = mock.Mock(status_code=200, content_type="application/json",
                                                                  get_data=lambda as_text=True: json.dumps(body))
        out = json.loads(assistant.fetch_source(app, "auction_history", "from=2022-01-01&to=2023-12-31"))
        self.assertIn(". 2023 vs 2022", out["summary"]["by_tenor"]["30-Year Bond"])
        out = json.loads(assistant.fetch_source(app, "auction_history", ""))
        self.assertIn(". 2026 so far (to 10-26) vs all of 2025", out["summary"]["by_tenor"]["30-Year Bond"])


class Round3(unittest.TestCase):
    """The five wording errors of 2026-10-26 round 3: the data now says it in words."""

    def setUp(self):
        sys.path.insert(0, str(support.ROOT / "options_whale"))
        import assistant
        self.A = assistant

    def test_trillions_are_written_out(self):
        self.assertEqual(fedops.money(2553.716), "$2,553.7 billion ($2.55 trillion)")
        self.assertEqual(fedops.money(10.3), "$10.300 billion")
        self.assertEqual(fedops.money(-12.5), "-$12.500 billion")

    def test_peaks_and_trend_sentence(self):
        rows = [{"date": d, "entity": "rrp", "accepted_bn": v} for d, v in (("2022-12-30", 2553.716), ("2025-10-15", 11.07), ("2025-12-31", 105.993),
                                                                          ("2026-10-23", 0.2))]
        m = {"rrp": {"earliest": "2014-01-02", "latest": rows[-1], "peak_12m": fedops._peak(rows, "2025-10-26"), "peak_all_time": fedops._peak(rows)}}
        months = fedops.repo_monthly([{"date": r["date"], "rrp_bn": r["accepted_bn"]} for r in rows])
        s = fedops.rrp_trend(m, months, TODAY)
        self.assertIn("$105.993 billion on 2025-12-31 (Wednesday)", s)
        self.assertIn("$2,553.7 billion ($2.55 trillion) on 2022-12-30 (Friday)", s)
        self.assertIn("against $11.070 billion a day in 2025-10", s)
        # run early in November before any November operation: the year-ago month follows the latest month, not today's
        self.assertIn("in 2026-10 against $11.070 billion a day in 2025-10", fedops.rrp_trend(m, months, date(2026, 11, 2)))

    def test_one_auction_bidder_shares_sum_to_100(self):
        out = self.A._shape_fed({"last_dealer_pct": 2.54, "last_direct_pct": 17.12, "last_indirect_pct": 80.34, "dealer_pct_12m": 8.9})
        self.assertEqual(out["last_winning_bid_shares"], "primary dealers 2.54% / direct bidders 17.12% / indirect bidders 80.34% (one auction, total 100.0%)")
        self.assertEqual(out["dealer_pct_12m"], 8.9)
        self.assertNotIn("last_dealer_pct", out)

    def test_weekdays_and_latest_available(self):
        out = self.A._shape_fed({"latest": {"date": "2026-10-23", "rate": 3.92}, "as_of": "2026-10-27"})
        self.assertEqual(out, {"latest": {"date": "2026-10-23 (Friday)", "rate": 3.92}, "as_of": "2026-10-27 (Tuesday)"})     # the key stays: paths use it
        p = fixed_now(self.A, (2026, 10, 26, 9, 0))
        self.assertTrue(self.A._today_line().startswith("Monday 2026-10-26; the last Fed business day before today was Friday 2026-10-23"))
        p.stop()
        p = fixed_now(self.A, (2026, 11, 12, 9, 0))              # Veterans Day: the NYSE trades, the Fed does not
        self.assertIn("was Tuesday 2026-11-10", self.A._today_line())
        p.stop()

    def test_year_on_year_direction_is_written(self):
        self.addCleanup(fixed_now(self.A, (2027, 3, 1, 9, 0)).stop)       # both years complete
        s = self.A._year_vs_year({"2025": {"bid_to_cover_avg": 2.3708, "auctions": 12, "size_bn": 276.0},
                                  "2026": {"bid_to_cover_avg": 2.453, "auctions": 10, "size_bn": 229.0}})
        self.assertTrue(s.startswith("2026 vs 2025: mean bid-to-cover 2.453 vs 2.371 (higher by 0.082)"))


class Round4(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(support.ROOT / "options_whale"))
        import assistant
        self.A = assistant

    def test_each_repo_month_is_one_labelled_line(self):
        row = {"month": "2025-10", "rrp_avg_bn": 11.07, "rrp_max_bn": 51.802, "rrp_last_bn": 51.802, "rrp_last_date": "2025-10-31",
               "repo_avg_bn": 4.834, "repo_max_bn": 50.35, "repo_last_bn": 50.35, "repo_last_date": "2025-10-31",
               "sofr_avg": 4.2009, "effr_avg": 4.0823, "tgcr_avg": 4.1682, "bgcr_avg": 4.1682, "sofr_volume_avg_bn": 3007.2}
        summary = {"monthly": [row], "yearly": [dict(row, year="2025", rrp_avg_bn=102.973)]}
        self.A._repo_periods(summary)
        line = summary["monthly_levels"]["2025-10"]
        self.assertTrue(line.startswith("ON RRP averaged $11.070 billion a day (highest day $51.802 billion, last day $51.802 billion on 2025-10-31 (Friday))"))
        self.assertNotIn("trillion", line)                        # SOFR volume (trillions) is not beside the ON RRP average any more
        self.assertIn("ON RRP averaged $102.973 billion a day", summary["yearly_levels"]["2025"])
        self.assertNotIn("monthly", summary)

    def test_earlier_readings_are_single_days(self):
        out = self.A._shape_fed({"ago": {"1m": {"date": "2026-09-25", "accepted_bn": 0.576}, "1y": {"date": "2025-10-23", "rate": 4.24}, "3m": None}})
        e = out["single_day_readings_earlier"]
        self.assertEqual(e["about 1 month before"], "$0.576 billion on 2026-09-25 (Friday) (one day, not a total)")
        self.assertEqual(e["about 1 year before"], "4.24% on 2025-10-23 (Thursday) (one day, not a total)")
        self.assertIsNone(e["about 3 months before"])

    def test_buyback_totals_say_they_are_sums(self):
        out = self.A._shape_fed({"ops": 64, "max_bn": 273.78, "offered_bn": 1255.197, "accepted_bn": 230.235})
        self.assertEqual(set(out), {"ops", "sum_of_announced_maximums", "total_offered_by_dealers", "total_accepted"})
        op = self.A._shape_fed({"date": "2026-10-21", "operation_type": "Liquidity Support", "max_bn": 0.75, "offered_bn": 4.751, "accepted_bn": 0.75})
        self.assertEqual(op["announced_maximum"], "$0.750 billion")
        self.assertEqual(op["offered_by_dealers"], "$4.751 billion")


class ConsoleRouting(unittest.TestCase):
    def test_a_period_asked_of_a_latest_source_points_to_its_history(self):
        sys.path.insert(0, str(support.ROOT / "options_whale"))
        import assistant
        app = mock.Mock()
        out = assistant.fetch_source(app, "treasury_buybacks", "from=2026-09-01&to=2026-09-30")
        self.assertIn("call buyback_history", out)
        self.assertIn("from=2026-09-01&to=2026-09-30", out)
        self.assertIn("auction_history", assistant.fetch_source(app, "treasury_auctions", "tenor=30 year"))
        app.test_client.assert_not_called()


class Backfill(unittest.TestCase):
    def test_years_and_resume(self):
        sys.path.insert(0, str(support.ROOT / "scripts"))
        import backfill_fed_ops as B
        self.assertEqual(B.years("2024-01-01", date(2026, 3, 5))[-1], (date(2026, 1, 1), date(2026, 3, 5)))
        self.assertEqual(len(B.years("2024-01-01", date(2026, 3, 5))), 3)
        conn, asked = fresh_conn(), []

        def repo(session, start, end):
            asked.append(start)
            return [op for op in load("rp.json") if start[:4] == "2026"], None
        with mock.patch.dict(B.JOBS, {fedops.REPO: (repo, fedops.repo_records)}, clear=True), \
             mock.patch.object(B.src, "treasury_buybacks", return_value=([], None)), \
             mock.patch.object(B.src, "treasury_upcoming", return_value=([], None)), mock.patch.object(B.fedops, "EARLIEST", {**fedops.EARLIEST, fedops.REPO: "2025-01-01"}):
            lake.record_observations(conn, fedops.repo_records([dict(load("rp.json")[0], operationDate="2025-12-30")]), run_id="topup")
            B.work_for(date(2027, 2, 1))(conn, None, "backfill-fed_ops-1")
            self.assertEqual(asked, ["2025-01-01", "2026-01-01", "2027-01-01"])
            asked.clear()
            B.work_for(date(2027, 2, 1))(conn, None, "backfill-fed_ops-2")
            # 2026 was stored by a backfill and is finished; 2025 holds only a top-up row, so it is asked again, as is the open year
            self.assertEqual(asked, ["2025-01-01", "2027-01-01"])


class Routes(ApiCase):
    def test_fed_ops_routes(self):
        self.assertEqual(self.client.get("/api/fed_ops").status_code, 503)
        conn = lake.connect(self.tmp / "portfolio.db")
        lake.migrate(conn)
        lake.record_observations(conn, fedops.repo_records(load("rp.json")))
        lake.record_observations(conn, fedops.rate_records(load("rates.json")))
        lake.create_run(conn, "r1", mode="live", trigger="manual", run_folder="Oct-26-26", code_version="t")
        lake.commit_snapshot(conn, "r1", {"run": {"run_id": "r1"}, "fed_ops": fedops.build(conn, TODAY)})
        lake.update_run(conn, "r1", status="completed")
        conn.close()
        j = self.get_json("/api/fed_ops/repo")
        self.assertEqual((j["status"], j["data"]["markets"]["rrp"]["latest"]["date"]), ("success", "2026-10-23"))
        self.assertIn("buybacks", self.get_json("/api/fed_ops")["data"])
        h = self.get_json("/api/fed_ops/history/repo?from=2026-10-21&limit=2")
        self.assertEqual((h["earliest_stored"], h["rows_in_range"], [r["date"] for r in h["rows"]]), ("2026-10-20", 3, ["2026-10-22", "2026-10-23"]))
        self.assertEqual((h["rows"][-1]["rrp_bn"], h["rows"][-1]["sofr"]), (0.2, 3.92))
        self.assertEqual(self.client.get("/api/fed_ops/history/repo?from=nope").status_code, 400)
        self.assertEqual(self.client.get("/api/fed_ops/history/auctions").status_code, 404)      # nothing stored yet

    def test_a_block_from_an_older_version_is_rebuilt_from_the_stored_rows(self):
        conn = lake.connect(self.tmp / "portfolio.db")
        lake.migrate(conn)
        lake.record_observations(conn, fedops.repo_records(load("rp.json")))
        lake.create_run(conn, "r1", mode="live", trigger="manual", run_folder="Oct-26-26", code_version="t")
        lake.commit_snapshot(conn, "r1", {"run": {"run_id": "r1"}, "fed_ops": {"version": "fed_ops_v3", "repo": {"status": "fresh"}}})
        lake.update_run(conn, "r1", status="completed")
        conn.close()
        d = self.get_json("/api/fed_ops")["data"]
        self.assertEqual((d["version"], d["repo"]["status"]), (fedops.FEDOPS_VERSION, "fresh"))
        self.assertIn("rebuilt from stored rows", d["source"])
        self.assertIn("yearly", d["repo"])                    # a v4 key, whatever today's date

    def test_an_outdated_block_keeps_what_its_run_could_not_refresh(self):
        conn = lake.connect(self.tmp / "portfolio.db")
        lake.migrate(conn)
        lake.record_observations(conn, fedops.repo_records(load("rp.json")))
        lake.record_observations(conn, fedops.buyback_records(json.loads((FIX / "bb.json").read_text())["data"]))
        lake.create_run(conn, "r1", mode="live", trigger="manual", run_folder="Oct-26-26", code_version="t")
        lake.commit_snapshot(conn, "r1", {"run": {"run_id": "r1"}, "fed_ops": {"version": "fed_ops_v3", "repo": {"status": "fresh"},
                                                                              "buybacks": {"status": "stale", "reason": "treasury: http_500 buybacks"}}})
        lake.update_run(conn, "r1", status="completed")
        conn.close()
        d = self.get_json("/api/fed_ops")["data"]
        self.assertEqual((d["repo"]["status"], d["buybacks"]["status"]), ("fresh", "stale"))
        self.assertIn("http_500", d["buybacks"]["reason"])

    def test_before_the_first_run_the_routes_summarise_the_stored_history(self):
        conn = lake.connect(self.tmp / "portfolio.db")
        lake.migrate(conn)
        lake.record_observations(conn, fedops.auction_records([src.auction_row(x) for x in load("td.json")]))
        lake.record_observations(conn, fedops.buyback_records(json.loads((FIX / "bb.json").read_text())["data"]))
        lake.record_observations(conn, fedops.buyback_records([buyback("2026-02-10", 1.0), buyback("2026-09-10", 2.0)]))
        lake.create_run(conn, "r1", mode="live", trigger="manual", run_folder="Oct-26-26", code_version="t")
        lake.commit_snapshot(conn, "r1", {"run": {"run_id": "r1"}})                  # a snapshot from before card 15
        lake.update_run(conn, "r1", status="completed")
        conn.close()
        j = self.get_json("/api/fed_ops/buybacks")
        self.assertEqual((j["status"], j["data"]["status"]), ("success", "stale"))     # nothing was fetched: never "fresh"
        self.assertIn("no pipeline run has refreshed it yet", j["data"]["reason"])
        self.assertTrue(j["data"]["last_8"])
        q = self.get_json("/api/fed_ops/history/buybacks?from=2026-09-01&to=2026-09-30")["summary"]
        self.assertEqual([r["period"] for r in q["quarterly"]], ["2026-Q3"])           # not every quarter of 2026
        self.assertEqual([r["period"] for r in q["monthly"]], ["2026-09"])
        self.assertIn("stored history", self.get_json("/api/fed_ops")["data"]["source"])
        h = self.get_json("/api/fed_ops/history/auctions?tenor=20 year")
        self.assertEqual([r["tenor"] for r in h["rows"]], ["20-Year Bond"])
        self.assertIn("20-Year Bond", h["summary"]["by_tenor"])
        self.assertNotIn("t12m", h["summary"])                                          # market-wide totals are not the tenor's


if __name__ == "__main__":
    unittest.main()
