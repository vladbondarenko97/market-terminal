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
        self.assertEqual(out, {"max_60d": {"date": "2026-09-30", "accepted": "$11.539 billion"}, "rows": [{"rrp": "$0.200 billion", "sofr": 3.92, "repo": None}]})
        latest = assistant._shape_fed_latest({"data": {"yearly": [{"year": y, "accepted_bn": n} for y, n in (("2024", 1.0), ("2025", 2.0), ("2026", 3.0))]}})
        self.assertEqual(list(latest["data"]["yearly"]), ["2025", "2026"])
        self.assertEqual(latest["data"]["yearly"]["2025"]["whole_year"], {"accepted": "$2.000 billion"})
        y = assistant._shape_fed_latest({"data": {"yearly": [{"year": "2025", "accepted_bn": 9.0, "ytd_to": "10-26", "ytd": {"accepted_bn": 4.0}}]}})
        self.assertEqual(y["data"]["yearly"]["2025"]["jan_1_to_10-26"], {"accepted": "$4.000 billion"})
        self.assertEqual(assistant._shape_fed_latest({"data": {"by_tenor": {}, "t12m": {"auctions": 432}}}), {"data": {"by_tenor": {}}})


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
