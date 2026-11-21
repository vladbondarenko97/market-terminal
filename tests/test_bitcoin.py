"""BITCOIN tab (core/bitcoin.py): the fetchers on saved responses (requests.get mocked), the maths on synthetic series, the
verdict rules, the collector offline and with every source faked, Signal Watch rows, the stored routes and the console source."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401  (temporary CME_Data, no credentials; must come before importing config)

import json
import tempfile
from datetime import date
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

from core import bitcoin as B, forecast, lake
from core import sources as src
from core.sources import SourceSession, SourceUnavailable
from test_api import ApiCase

FIX = support.FIX / "bitcoin"
TODAY = date(2026, 11, 20)
load = lambda name: json.loads((FIX / name).read_text())


def resp(payload=None, status=200, text=""):
    return SimpleNamespace(status_code=status, json=lambda: payload, text=text, content=text.encode(), raise_for_status=lambda: None)


def session(offline=False):
    conn = lake.connect(Path(tempfile.mkdtemp(prefix="btc_")) / "portfolio.db")
    lake.migrate(conn)
    lake.create_run(conn, "t", mode="live", trigger="manual", run_folder="Nov-20-26", code_version="t")
    return conn, SourceSession(conn, "t", offline=offline)


def daily(values, end="2026-11-19"):
    return pd.Series(values, index=pd.date_range(end=end, periods=len(values), freq="D"), dtype=float)


class Fetchers(unittest.TestCase):
    def test_coinmetrics_follows_pages_and_asks_only_the_free_metrics(self):
        conn, s = session()
        with mock.patch("requests.get", side_effect=[resp(load("coinmetrics_p1.json")), resp(load("coinmetrics_p2.json"))]) as g:
            rows, pid = src.coinmetrics_btc(s, B.CM_START)
        self.assertEqual([r["time"][:10] for r in rows], ["2026-11-18", "2026-11-19"])
        self.assertNotIn("CapRealUSD", g.call_args_list[0].kwargs["params"]["metrics"])
        self.assertIsNotNone(pid)
        df = B.cm_frame(rows)
        self.assertAlmostEqual(df["mvrv"].iloc[-1], 1.158746, 5)

    def test_coinmetrics_forbidden_is_an_unavailable_source_not_zero(self):
        _, s = session()
        with mock.patch("requests.get", return_value=resp(load("coinmetrics_forbidden.json"), status=403)):
            with self.assertRaises(SourceUnavailable) as e:
                src.coinmetrics_btc(s, B.CM_START)
        self.assertEqual(e.exception.outcome, "http_403")

    def test_deribit_ticker_funding_and_dvol(self):
        _, s = session()
        with mock.patch("requests.get", return_value=resp(load("deribit_ticker.json"))):
            tk, _ = src.deribit_ticker(s)
        self.assertEqual((tk["funding_8h"], tk["open_interest"]), (-8.085e-5, 900849340))
        with mock.patch("requests.get", return_value=resp(load("deribit_funding.json"))):
            rows, _ = src.deribit_funding_history(s, 1, 2)
        self.assertEqual(len(rows), 2)
        with mock.patch("requests.get", return_value=resp(load("deribit_dvol.json"))):
            rows, _ = src.deribit_dvol(s, 1, 2)
        self.assertEqual(rows[-1][4], 49.71)

    def test_ibit_page_parse_and_a_changed_page_is_unavailable(self):
        _, s = session()
        page = (FIX / "ibit_page.html").read_text()
        with mock.patch("requests.get", return_value=resp(text=page)):
            ib, _ = src.ishares_ibit(s)
        self.assertEqual(ib, {"shares_outstanding": 1360960000.0, "shares_as_of": "2026-11-20", "nav": 33.054267, "nav_as_of": "2026-11-20"})
        _, s2 = session()
        with mock.patch("requests.get", return_value=resp(text="<html>redesigned</html>")):
            with self.assertRaises(SourceUnavailable):
                src.ishares_ibit(s2)

    def test_exchange_tickers_stablecoins_and_cot(self):
        _, s = session()
        with mock.patch("requests.get", return_value=resp(load("coinbase.json"))):
            cb, _ = src.coinbase_btc_usd(s)
        with mock.patch("requests.get", return_value=resp(load("kraken.json"))):
            kr, _ = src.kraken_btc_usdt(s)
        self.assertEqual((cb["price"], kr["price"]), (55654.28, 55759.0))
        with mock.patch("requests.get", return_value=resp([{"date": "1795219200", "totalCirculatingUSD": {"peggedUSD": 5.0}}, {"date": "1795132800"}])):
            st, _ = src.defillama_stablecoin(s, 1)
        self.assertEqual(st, [{"date": "2026-11-21", "usd": 5.0}])          # a row with no value is dropped, not zero
        with mock.patch("requests.get", return_value=resp(load("cftc_cme_bitcoin.json"))) as g:
            rows, _ = src.cftc_cot(s, *B.COT_CONTRACT, "2023-11-20")
        self.assertIn("133741", g.call_args.kwargs["params"]["$where"])
        self.assertEqual(len(rows), 2)


class Maths(unittest.TestCase):
    def test_price_cycle_200_week_ma_mayer_and_drawdown(self):
        c = daily(list(range(1, 1501)))                       # 1..1500, the last close 1500
        p = B.price_cycle(c, TODAY, "t")
        self.assertEqual(p["ma_200w"], np.mean(range(101, 1501)))          # the last 1,400 closes
        self.assertEqual(p["ma_200d"], np.mean(range(1301, 1501)))
        self.assertAlmostEqual(p["mayer"], 1500 / np.mean(range(1301, 1501)))
        self.assertEqual((p["drawdown_pct"], p["return_30d_pct"] > 0), (0.0, True))
        self.assertEqual(p["halving"]["days_since"], (TODAY - date(2024, 4, 20)).days)
        self.assertEqual(p["halving"]["next_estimate"], "2028-04-19")

    def test_price_cycle_short_history_is_missing_with_the_reason_not_zero(self):
        p = B.price_cycle(daily(list(range(1, 301))), TODAY, "t")
        self.assertIsNone(p["ma_200w"])
        self.assertIn("300 daily closes", p["missing"]["ma_200w"])
        self.assertIsNotNone(p["mayer"])
        self.assertEqual(B.price_cycle(None, TODAY, "t")["status"], "missing")

    def test_mvrv_z_is_market_minus_realized_cap_over_the_stdev_of_market_cap(self):
        n = 400
        idx = pd.date_range(end="2026-11-19", periods=n, freq="D")
        df = pd.DataFrame({"mcap": np.arange(1, n + 1) * 1e9, "mvrv": 2.0, "supply": 1e6, "price": 1.0, "hash": 1.0}, index=idx)
        rcap, rprice, z = B.onchain_series(df)
        self.assertAlmostEqual(rcap.iloc[-1], 200e9)
        self.assertAlmostEqual(rprice.iloc[-1], 200e9 / 1e6)
        self.assertAlmostEqual(z.iloc[-1], 200e9 / (np.arange(1, n + 1) * 1e9).std(ddof=1))
        self.assertTrue(np.isnan(z.iloc[100]))                   # under 365 days of history: no Z, not 0
        o = B.onchain(df, TODAY)
        self.assertAlmostEqual(o["mvrv_z"], z.iloc[-1])
        self.assertEqual(o["status"], "fresh")
        self.assertIn("sopr", o["unavailable"])

    def test_mvrv_zero_or_missing_does_not_divide(self):
        idx = pd.date_range(end="2026-11-19", periods=3, freq="D")
        df = pd.DataFrame({"mcap": [1e9, 2e9, 3e9], "mvrv": [0.0, np.nan, 1.5], "supply": 1e6, "price": 1.0, "hash": 1.0}, index=idx)
        rcap, _, _ = B.onchain_series(df)
        self.assertTrue(rcap.iloc[:2].isna().all())

    def test_hash_ribbons_states(self):
        down = daily([100.0] * 120 + list(np.linspace(100, 60, 40)))
        up = daily([100.0] * 120 + list(np.linspace(100, 60, 40)) + list(np.linspace(60, 140, 40)))
        flat = daily([100.0] * 200)
        self.assertEqual(B.hash_ribbons(down)["state"], "capitulation")
        self.assertEqual(B.hash_ribbons(up)["state"], "recovery")
        self.assertEqual(B.hash_ribbons(flat)["state"], "expansion")
        self.assertEqual(B.hash_ribbons(daily([1.0] * 50))["status"], "missing")

    def test_funding_days_running_and_top_decile(self):
        def rows(daily_vals):                     # 24 hourly rows per UTC day, the last day being today (dropped: incomplete)
            out = []
            for i, v in enumerate(daily_vals):
                d = pd.Timestamp("2026-11-20") - pd.Timedelta(days=len(daily_vals) - i)
                out += [{"timestamp": int((d + pd.Timedelta(hours=h)).timestamp() * 1000), "interest_8h": v} for h in range(24)]
            return out
        tk = {"funding_8h": -8e-5, "open_interest": 9e8, "mark_price": 55000.0}
        neg = B.funding(tk, rows([1e-4] * 20 + [-1e-5, -2e-5, -3e-5]), TODAY)
        self.assertEqual(neg["negative_days_running"], 3)
        self.assertAlmostEqual(neg["annualized_pct"], -8e-5 * 3 * 365 * 100)
        hot = B.funding(tk, rows([1e-5] * 90 + [5e-4] * 7), TODAY)
        self.assertEqual((hot["high_days_running"], hot["negative_days_running"]), (7, 0))
        thin = B.funding(tk, rows([1e-4] * 3), TODAY)
        self.assertIn("need 7", thin["missing"]["history"])
        self.assertEqual(B.funding(None, [], TODAY)["status"], "missing")

    def test_ibit_flows_use_the_later_nav_and_skip_a_gap(self):
        obs = [{"date": "2026-11-12", "shares": 100.0, "nav": 10.0}, {"date": "2026-11-13", "shares": 110.0, "nav": 11.0},
               {"date": "2026-11-16", "shares": 130.0, "nav": 12.0},        # Fri -> Mon is a weekend, counts
               {"date": "2026-11-25", "shares": 100.0, "nav": 12.0}]        # nine days later: a gap, no flow computed
        f = B.ibit_flows(obs)
        self.assertEqual([x["net_flow_usd"] for x in f["days"]], [110.0, 240.0])
        self.assertEqual(f["positive_days_running"], 2)
        self.assertIn("streak", f["missing"])
        self.assertEqual(B.ibit_flows([])["status"], "missing")

    def test_premium_stablecoins_and_cot(self):
        self.assertAlmostEqual(B.premium({"price": 101.0}, {"price": 100.0})["premium_pct"], 1.0)
        self.assertEqual(B.premium(None, {"price": 1.0})["status"], "missing")
        a = [{"date": "2026-10-20", "usd": 100.0}, {"date": "2026-11-19", "usd": 110.0}]
        b = [{"date": "2026-10-20", "usd": 50.0}, {"date": "2026-11-19", "usd": 60.0}]
        s = B.stablecoins(a, b)
        self.assertEqual((s["usdt_usdc_usd"], s["change_30d_usd"]), (170.0, 20.0))
        c = B.cot(load("cftc_cme_bitcoin.json"))
        self.assertEqual((c["asset_managers"]["net"], c["asset_managers"]["change_1w"], c["leveraged_funds"]["net"]), (2285.0, 329.0, -1211.0))


def row(rid, side, status, graded=False):
    return {"id": rid, "side": side, "name": rid, "trigger": "", "status": status, "reading": None, "reason": "x" if status == "missing" else None,
            "graded": graded, "evidence": ""}


def rows(bottom, top, graded=True):
    return [row(f"b{i}", "bottom", s, graded) for i, s in enumerate(bottom)] + [row(f"t{i}", "top", s, graded) for i, s in enumerate(top)]


class Verdict(unittest.TestCase):
    F, W, M = "fired", "waiting", "missing"

    def test_buy_start_at_three_of_six_and_add_at_five(self):
        self.assertEqual(B.verdict(rows([self.F] * 3 + [self.W] * 3, [self.W] * 5))["call"], "BUY (start)")
        self.assertEqual(B.verdict(rows([self.F] * 2 + [self.W] * 4, [self.W] * 5))["call"], "HOLD")
        self.assertEqual(B.verdict(rows([self.F] * 5 + [self.W], [self.W] * 5))["call"], "BUY (add)")

    def test_sell_trim_at_three_of_five_and_a_conflict_is_hold(self):
        self.assertEqual(B.verdict(rows([self.W] * 6, [self.F] * 3 + [self.W] * 2))["call"], "SELL (trim)")
        v = B.verdict(rows([self.F] * 3 + [self.W] * 3, [self.F] * 3 + [self.W] * 2))
        self.assertEqual(v["call"], "HOLD")
        self.assertIn("both", v["text"])

    def test_missing_rows_shrink_the_denominator_and_never_vote_toward_the_count(self):
        v = B.verdict(rows([self.F, self.F, self.M, self.M, self.W, self.W], [self.W] * 5))
        self.assertEqual((v["available"], v["total"], v["call"]), (9, 11, "HOLD"))      # two fired is not three, and missing is not fired
        self.assertEqual(len(v["missing_rows"]), 2)

    def test_under_half_available_is_no_call_and_names_what_is_missing(self):
        v = B.verdict(rows([self.F] * 3 + [self.M] * 3, [self.M] * 5))
        self.assertEqual((v["call"], v["available"]), ("NO CALL", 3))
        self.assertIn("under half", v["text"])
        self.assertIn("b3", v["text"])
        self.assertEqual(B.verdict(rows([self.F] * 3 + [self.M] * 3, [self.M] * 2 + [self.W] * 3))["call"], "BUY (start)")       # 6 of 11 is half or more

    def test_untested_rows_show_but_do_not_vote(self):
        v = B.verdict(rows([self.F] * 6, [self.W] * 5, graded=False))
        self.assertEqual((v["call"], v["bottom_fired"], v["bottom_votes"]), ("HOLD", 6, 0))
        self.assertIn("not yet graded", v["text"])
        mixed = rows([self.F] * 6, [self.W] * 5, graded=False)
        for r in mixed[:3]:
            r["graded"] = True
        self.assertEqual(B.verdict(mixed)["call"], "BUY (start)")

    def test_nothing_is_graded_until_phase_4(self):
        self.assertEqual(B.GRADES, {})


def block(**over):
    n = 1500
    closes = daily(list(np.linspace(200, 100, n)))                # price far under its 200-week MA: bottom row 1 fires
    idx = pd.date_range(end="2026-11-19", periods=500, freq="D")
    mcap = np.linspace(2e9, 1e9, 500)
    df = pd.DataFrame({"mcap": mcap, "mvrv": 0.95, "supply": 1e7, "price": 100.0, "hash": [100.0] * 420 + list(np.linspace(100, 60, 40)) + list(np.linspace(60, 140, 40))}, index=idx)
    blk = {"price": B.price_cycle(closes, TODAY, "test"), "onchain": B.onchain(df, TODAY),
           "leverage": {"funding": {"status": "fresh", "negative_days_running": 4, "high_days_running": 0, "p90": 1e-4},
                        "dvol": {"status": "fresh"}},
           "flows": {"ibit": {"status": "fresh", "positive_days_running": 6, "negative_days_running": 0, "stored_days": 8}}}
    blk.update(over)
    return blk


class Rows(unittest.TestCase):
    def test_eleven_rows_each_with_a_status_and_none_graded(self):
        blk = block()
        rs = B.rows_of(blk, {"score": 10.0, "regime": "neutral", "prev": {"regime": "neutral"}}, 0.9)
        self.assertEqual(len(rs), 11)
        self.assertEqual({r["status"] for r in rs} - {"fired", "waiting", "missing"}, set())
        by = {r["id"]: r for r in rs}
        self.assertEqual((by["b1"]["status"], by["b3"]["status"], by["b4"]["status"], by["b5"]["status"], by["b6"]["status"]), ("fired",) * 5)
        self.assertEqual((by["t2"]["status"], by["t3"]["status"], by["t5"]["status"]), ("waiting",) * 3)
        self.assertTrue(all(not r["graded"] and r["evidence"] == B.NOT_GRADED for r in rs))
        v = B.verdict(rs)
        self.assertEqual(v["call"], "HOLD")                                        # five bottom rows fired, none graded: no BUY
        self.assertIn("cannot say BUY or SELL", v["text"])

    def test_a_missing_part_gives_missing_rows_with_its_reason(self):
        blk = block(onchain={"status": "missing", "reason": "coinmetrics: http_403"}, leverage={"funding": {"status": "missing", "reason": "deribit down"}}, flows={})
        rs = {r["id"]: r for r in B.rows_of(blk, None, None)}
        for rid in ("b1", "b2", "b3", "b4", "t1", "t3"):
            self.assertEqual(rs[rid]["status"], "missing", rid)
        self.assertIn("coinmetrics", rs["b2"]["reason"])
        self.assertIn("Chaos", rs["b6"]["reason"])
        self.assertEqual(B.verdict(list(rs.values()))["call"], "NO CALL")

    def test_chaos_turning_row_needs_the_previous_regime(self):
        blk = block()
        up = {r["id"]: r for r in B.rows_of(blk, {"score": 40.0, "regime": "stress", "prev": {"regime": "peace"}}, 1.1)}
        self.assertEqual((up["t5"]["status"], up["b6"]["status"]), ("fired", "waiting"))


class Collect(unittest.TestCase):
    def test_offline_every_part_is_missing_with_a_reason_and_the_verdict_is_no_call(self):
        conn, s = session(offline=True)
        blk = B.collect(conn, s, "t", TODAY)
        self.assertEqual(blk["status"], "missing")
        self.assertEqual(blk["verdict"]["call"], "NO CALL")
        self.assertEqual(len(blk["rows"]), 11)
        self.assertTrue(all(r["status"] == "missing" and r["reason"] for r in blk["rows"]))
        self.assertIn("coinmetrics", blk["source_errors"])
        self.assertIn("offline", blk["onchain"]["reason"])

    def test_the_pipeline_guard_returns_a_block_not_an_error(self):
        from core import collect
        conn, s = session(offline=True)
        blk = collect._guarded_bitcoin(conn, s, "t", TODAY, {"score": 1.0}, {"vix": {"value": 18.0}, "vix3m": {"value": 20.0}})
        self.assertEqual(blk["status"], "missing")
        self.assertEqual(blk["verdict"]["call"], "NO CALL")

    def faked(self, df_rows, closes):
        ticker, funding = load("deribit_ticker.json")["result"], load("deribit_funding.json")["result"]
        ib = {"shares_outstanding": 1360960000.0, "shares_as_of": "2026-11-20", "nav": 33.054267, "nav_as_of": "2026-11-20"}
        return [mock.patch.object(B, "_cm_rows", return_value=df_rows),
                mock.patch.object(src, "deribit_ticker", return_value=(ticker, 1)),
                mock.patch.object(src, "deribit_funding_history", return_value=(funding, 2)),
                mock.patch.object(src, "deribit_dvol", return_value=(load("deribit_dvol.json")["result"]["data"], 3)),
                mock.patch.object(src, "ishares_ibit", return_value=(ib, 4)),
                mock.patch.object(src, "coinbase_btc_usd", return_value=({"price": 55654.28}, 5)),
                mock.patch.object(src, "kraken_btc_usdt", return_value=({"price": 55759.0}, 6)),
                mock.patch.object(src, "defillama_stablecoin", return_value=([{"date": "2026-10-20", "usd": 1.0}, {"date": "2026-11-19", "usd": 2.0}], 7)),
                mock.patch.object(src, "cftc_cot", return_value=(load("cftc_cme_bitcoin.json"), 8))]

    def cm(self, n=500):
        d = pd.date_range(end="2026-11-19", periods=n, freq="D")
        return [{"time": f"{x.date()}T00:00:00.000000000Z", "PriceUSD": str(100 + i), "CapMrktCurUSD": str((100 + i) * 2e7), "CapMVRVCur": "1.2",
                 "SplyCur": "2e7", "HashRate": str(1e9 + i)} for i, x in enumerate(d)]

    def test_all_sources_answering_builds_the_block_and_stores_the_ibit_day_once(self):
        conn, s = session()
        patches = self.faked(self.cm(), None)
        mocks = []
        for p in patches:
            mocks.append(p.start())
            self.addCleanup(p.stop)
        blk = B.collect(conn, s, "t", TODAY, {"score": 5.0, "regime": "neutral", "prev": {"regime": "neutral"}}, 0.9)
        self.assertEqual(blk["version"], B.BITCOIN_VERSION)
        mocks[1].assert_called_once_with(s)                                      # every fetcher gets the SourceSession
        self.assertEqual(blk["source_errors"], {})
        self.assertEqual(blk["onchain"]["as_of"], "2026-11-19")
        self.assertAlmostEqual(blk["onchain"]["mvrv"], 1.2)
        self.assertEqual(blk["leverage"]["funding"]["open_interest_usd"], 900849340)
        self.assertEqual(blk["flows"]["ibit"]["stored_days"], 1)
        self.assertIn("streak", blk["flows"]["ibit"]["missing"])               # one stored day is not a 5-day streak
        self.assertAlmostEqual(blk["flows"]["coinbase_premium"]["premium_pct"], (55654.28 / 55759.0 - 1) * 100)
        self.assertEqual(blk["flows"]["cot"]["as_of"], "2026-11-17")
        self.assertEqual(blk["verdict"]["call"], "HOLD")
        self.assertEqual(len(blk["history"]["t"]), len(blk["history"]["mvrv_z"]))
        self.assertIn(blk["status"], ("fresh", "partial"))
        B.collect(conn, SourceSession(conn, "t2"), "t2", TODAY)
        n = conn.execute("SELECT COUNT(*) FROM v2_observations WHERE metric_id = ?", (B.IBIT_OBS,)).fetchone()[0]
        self.assertEqual(n, 1)                                                   # the same as-of day is not stored twice

    def test_a_failing_source_leaves_its_part_missing_and_the_rest_alone(self):
        conn, s = session()
        patches = self.faked(self.cm(), None)
        patches[1] = mock.patch.object(src, "deribit_ticker", side_effect=SourceUnavailable("deribit", "http_503", "down"))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        blk = B.collect(conn, s, "t", TODAY)
        self.assertEqual(blk["leverage"]["funding"]["status"], "missing")
        self.assertIn("deribit", blk["leverage"]["funding"]["reason"])
        self.assertEqual(blk["onchain"]["status"], "fresh")
        self.assertEqual(blk["status"], "partial")


class SignalWatch(unittest.TestCase):
    def test_one_row_per_checklist_and_waiting_while_nothing_is_graded(self):
        blk = {"verdict": B.verdict(B.rows_of(block(), {"score": 10.0, "regime": "neutral", "prev": {"regime": "neutral"}}, 0.9))}
        rs = [r for r in forecast.signal_watch({"bitcoin": blk}) if r["asset"] == "BTC"]
        self.assertEqual(sorted(r["name"] for r in rs), ["Bitcoin bottom checklist", "Bitcoin top checklist"])
        self.assertTrue(all(not r["fired"] and r["edge"] == "none" and r["action"] == "Wait" for r in rs))
        self.assertEqual([r for r in forecast.signal_watch({}) if r["asset"] == "BTC"], [])

    def test_a_graded_buy_fires_the_bottom_row_as_a_start(self):
        blk = {"verdict": {"call": "BUY (start)", "graded_rows": 3, "bottom_fired": 3, "bottom_total": 6, "top_fired": 0, "top_total": 5, "available": 11, "total": 11, "text": "t"}}
        r = next(r for r in forecast.signal_watch({"bitcoin": blk}) if r["name"] == "Bitcoin bottom checklist")
        self.assertTrue(r["fired"])
        self.assertTrue(r["action"].startswith("Start the DCA ladder"))


class Routes(ApiCase):
    def commit(self, blk):
        conn = lake.connect(self.tmp / "portfolio.db")
        lake.migrate(conn)
        lake.create_run(conn, "r1", mode="live", trigger="manual", run_folder="Nov-20-26", code_version="t")
        lake.commit_snapshot(conn, "r1", {"run": {"run_id": "r1"}, **({"bitcoin": blk} if blk is not None else {})})
        lake.update_run(conn, "r1", status="completed")
        conn.close()

    def test_503_before_the_first_run_and_404_without_a_block(self):
        self.assertEqual(self.client.get("/api/bitcoin").status_code, 503)
        self.assertEqual(self.client.get("/api/bitcoin/history").status_code, 503)
        self.commit(None)
        self.assertEqual(self.client.get("/api/bitcoin").status_code, 404)

    def test_the_stored_block_without_its_history_and_the_history_alone(self):
        blk = {"version": B.BITCOIN_VERSION, "as_of": "2026-11-20", "status": "partial", "verdict": {"call": "NO CALL", "text": "x"}, "rows": [],
               "history": {"t": [1, 2], "price": [1.0, 2.0], "ma_200w": [None, None], "realized_price": [None, None], "mvrv_z": [None, None]}}
        self.commit(blk)
        j = self.get_json("/api/bitcoin")
        self.assertEqual((j["status"], j["as_of"], j["data"]["verdict"]["call"]), ("success", "2026-11-20", "NO CALL"))
        self.assertNotIn("history", j["data"])
        self.assertEqual(self.get_json("/api/bitcoin/history")["data"]["price"], [1.0, 2.0])

    def test_a_failed_block_is_a_404_with_its_reason(self):
        self.commit({"status": "error", "reason": "boom"})
        r = self.client.get("/api/bitcoin")
        self.assertEqual((r.status_code, r.get_json()["message"]), (404, "boom"))

    def test_the_page_has_the_bitcoin_tab_right_after_chart(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertLess(html.index('id="tab-chart"'), html.index('id="tab-bitcoin"'))
        self.assertIn('id="bitcoin-tab"', html)
        self.assertIn("/static/bitcoin.js", html)


class Console(unittest.TestCase):
    def test_bitcoin_is_a_console_and_mcp_source_with_labelled_rows(self):
        import assistant
        import mcp_server
        self.assertIn("bitcoin", assistant.SOURCES)
        self.assertIn("bitcoin", mcp_server.SOURCES)
        blk = block()
        blk["rows"] = B.rows_of(blk, None, None)
        blk["verdict"] = B.verdict(blk["rows"])
        blk["leverage"] = blk["flows"] = {}              # the stubs of block() lack the fields a real part has
        out = assistant._shape_bitcoin({"as_of": "2026-11-20", "data": blk}, {})
        text = "\n".join(out["rows"])
        self.assertIn("Verdict: NO CALL", text) if blk["verdict"]["call"] == "NO CALL" else self.assertIn("Verdict:", text)
        self.assertIn("Price: $", text)
        self.assertIn("graded on history", text)
        empty = assistant._shape_bitcoin({"data": {}}, {})
        self.assertIn("Price: missing", "\n".join(empty["rows"]))


if __name__ == "__main__":
    unittest.main()
