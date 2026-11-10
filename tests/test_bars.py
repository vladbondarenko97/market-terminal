"""CHART tab: the bars store (core/bars.py), the Alpaca fetcher and the /api/chart/* routes. Offline, fixed dates.

Yahoo, Alpaca and the bars subprocess are replaced by fakes; every test gets its own CHART_STORE and portfolio.db.
"""
import io
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401,E402  (temporary CME_Data, no credentials; must come before importing config)

import pandas as pd  # noqa: E402

import config  # noqa: E402
from core import bars, lake, sources  # noqa: E402

sys.path.insert(0, str(support.ROOT / "options_whale"))
import api_router  # noqa: E402

NY = bars.NY
NOW = datetime(2026, 6, 1, 15, 0, tzinfo=NY)          # Monday 15:00: 2026-06-01 is still in session
LATER = datetime(2026, 6, 1, 20, 30, tzinfo=NY)


def intraday(day, start, n, freq="min", base=100.0):
    idx = pd.date_range(f"{day} {start}", periods=n, freq=freq, tz=NY, name="Datetime")
    return pd.DataFrame({"Open": base, "High": base + 1, "Low": base - 1, "Close": base + 0.5, "Volume": 10}, index=idx)


def daily(days, close=100.0):
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz=NY) for d in days], name="Date")
    return pd.DataFrame({"Open": 99.0, "High": 101.0, "Low": 98.0, "Close": close, "Volume": 1000, "Dividends": 0.0}, index=idx)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chart_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(config, "CHART_STORE", self.tmp / "chart")
        p.start()
        self.addCleanup(p.stop)
        self.conn = bars.connect()
        self.addCleanup(self.conn.close)
        bars._EIA_CACHE.clear()


class Store(Base):
    def test_lake_payloads_become_rows_and_day_files(self):
        main = self.tmp / "portfolio.db"
        m = lake.connect(main)
        lake.migrate(m)
        for iv, df in (("1d", daily(["2026-05-27", "2026-05-28"])), ("1m", intraday("2026-05-28", "09:30", 3))):
            buf = io.StringIO()
            df.to_csv(buf)
            lake.store_payload(m, source="yahoo", kind="ohlcv", content=buf.getvalue(), fmt="csv",
                               request={"fn": "history", "symbol": "SPY", "period": "7d", "interval": iv})
        m.close()
        res = bars.from_lake(self.conn, main_db=main, cache_dir=self.tmp / "none", now=NOW)
        self.assertEqual(res["payloads"], 2)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT ts FROM v2_bars ORDER BY ts")], ["2026-05-27", "2026-05-28"])
        self.assertEqual(bars.tracked(self.conn), ["SPY"])
        self.assertTrue((config.CHART_STORE / "bars/SPY/1m/2026-05-28.parquet").exists())
        self.assertEqual(len(bars.read_bars(self.conn, "SPY", "1m")), 3)

    def test_insert_or_ignore_keeps_the_first_copy(self):
        bars.ingest(self.conn, "SPY", "1d", daily(["2026-05-28"], close=100.0), "yahoo", now=NOW)
        n = bars.ingest(self.conn, "SPY", "1d", daily(["2026-05-28"], close=555.0), "yahoo", now=NOW)
        self.assertEqual(n, 0)
        self.assertEqual(self.conn.execute("SELECT close FROM v2_bars").fetchone()[0], 100.0)

    def test_an_unfinished_session_is_not_stored(self):
        for day in ("2026-05-29", "2026-06-01"):
            bars.ingest(self.conn, "SPY", "1m", intraday(day, "09:30", 3), "yahoo", now=NOW)
        bars.ingest(self.conn, "SPY", "1d", daily(["2026-05-29", "2026-06-01"]), "yahoo", now=NOW)
        d = config.CHART_STORE / "bars/SPY/1m"
        self.assertEqual([p.stem for p in d.glob("*.parquet")], ["2026-05-29"])
        self.assertEqual([r[0] for r in self.conn.execute("SELECT ts FROM v2_bars")], ["2026-05-29"])
        bars.ingest(self.conn, "SPY", "1m", intraday("2026-06-01", "09:30", 3), "yahoo", now=LATER)   # after 20:00 it is over
        self.assertTrue((d / "2026-06-01.parquet").exists())

    def test_a_day_file_is_written_once(self):
        bars.ingest(self.conn, "SPY", "1m", intraday("2026-05-29", "09:30", 3, base=100), "yahoo", now=NOW)
        bars.ingest(self.conn, "SPY", "1m", intraday("2026-05-29", "09:30", 3, base=900), "yahoo", now=NOW)
        self.assertEqual(bars.read_bars(self.conn, "SPY", "1m")["open"].iloc[0], 100.0)

    def test_resampling_never_spans_a_session(self):
        for day in ("2026-05-28", "2026-05-29"):
            bars.ingest(self.conn, "SPY", "1m", intraday(day, "09:30", 75), "yahoo", now=NOW)
            bars.ingest(self.conn, "SPY", "1h", intraday(day, "04:00", 16, freq="h"), "yahoo", now=NOW)
        m15 = bars.read_bars(self.conn, "SPY", "15m")
        self.assertEqual(len(m15), 10)                                              # 5 buckets a day
        self.assertTrue((m15["volume"] == 150).all())
        h4 = bars.read_bars(self.conn, "SPY", "4h")
        self.assertEqual(list(h4.index.tz_convert(NY).hour), [4, 8, 12, 16] * 2)
        self.assertTrue((h4["volume"] == 40).all())

    def test_symbol_validation(self):
        for bad in ("../x", "A;B", "..", ".x", "", "A" * 16):
            with self.assertRaises(ValueError):
                bars.norm_symbol(bad)
        self.assertEqual(bars.norm_symbol(" nke "), "NKE")                            # lower case is upper-cased
        self.assertEqual(bars._folder(bars.norm_symbol("^vix")), "_VIX")

    def test_collect_offline_records_errors_not_zeros(self):
        s = sources.SourceSession(self.conn, "bars-test", offline=True)
        res = bars.collect(self.conn, s, ["SPY"])
        self.assertEqual(res["added"], 0)
        self.assertEqual(len(res["errors"]), 4)
        self.assertIn("S&P 500", self.conn.execute("SELECT description FROM v2_chart_symbols").fetchone()[0])

    def test_collect_stores_what_yahoo_returns(self):
        s = sources.SourceSession(self.conn, "bars-test")
        with mock.patch.object(sources, "yf_history", return_value=(daily(["2026-05-28"]), 1)) as yh, \
                mock.patch("yfinance.Ticker") as tk:
            tk.return_value.info = {"longName": "Nike, Inc.", "quoteType": "EQUITY", "fullExchangeName": "NYSE",
                                    "longBusinessSummary": "Nike designs shoes. It sells them worldwide. " + "x" * 400}
            res = bars.collect(self.conn, s, ["nke"], now=NOW)
        self.assertEqual(res["errors"], [])
        self.assertEqual(yh.call_count, 4)
        row = self.conn.execute("SELECT name, kind, description FROM v2_chart_symbols WHERE symbol = 'NKE'").fetchone()
        self.assertEqual((row["name"], row["kind"]), ("Nike, Inc.", "EQUITY"))
        self.assertTrue(row["description"].startswith("Nike designs shoes. It sells them worldwide."))
        self.assertLessEqual(len(row["description"]), 220)
        self.assertEqual(bars.tracked(self.conn), ["NKE"])

    def test_backfill_1m_windows_stay_under_eight_days(self):
        s = sources.SourceSession(self.conn, "bars-test")
        with mock.patch.object(sources, "yf_history", return_value=(daily(["2026-05-28"]), 1)) as yh, mock.patch("yfinance.Ticker") as tk:
            tk.return_value.info = {}
            bars.collect(self.conn, s, ["SPY"], backfill=True, now=NOW)
        wins = [(c.kwargs.get("start"), c.kwargs.get("end")) for c in yh.call_args_list if c.kwargs.get("start")]
        self.assertEqual(len(wins), 4)
        for a, b in wins:                          # Yahoo refuses a 1m window of 8 days or more ("Only 8 days ...")
            self.assertEqual((pd.Timestamp(b) - pd.Timestamp(a)).days, 7)
        self.assertEqual(max(b for _, b in wins), NOW.date().isoformat())   # today (unfinished) is left out


class Alpaca(Base):
    def test_pagination_and_missing_keys(self):
        s = sources.SourceSession(self.conn, "bars-test")
        with self.assertRaises(sources.SourceUnavailable) as cm:
            sources.alpaca_bars(s, "", "", "SPY", "2026-05-01", "2026-05-02")
        self.assertEqual(cm.exception.outcome, "not_configured")
        pages = [{"bars": {"SPY": [{"t": "2026-05-01T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9}]}, "next_page_token": "tok"},
                 {"bars": {"SPY": [{"t": "2026-05-01T13:31:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.6, "v": 8}]}, "next_page_token": None}]
        resp = [mock.Mock(status_code=200, **{"json.return_value": p}) for p in pages]
        with mock.patch("requests.get", side_effect=resp) as get:
            df, pid = sources.alpaca_bars(s, "K", "S", "SPY", "2026-05-01", "2026-05-02")
        self.assertEqual(len(df), 2)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["page_token"], "tok")
        self.assertEqual(get.call_args.kwargs["headers"]["APCA-API-KEY-ID"], "K")
        self.assertIsNotNone(pid)
        self.assertEqual(bars._norm(df, "1m")["close"].tolist(), [1.5, 1.6])


class Routes(Base):
    def setUp(self):
        super().setUp()
        self.client = api_router.app.test_client()
        api_router._CHART_JOBS.clear()
        api_router._LOOKUP_CACHE.clear()
        self.main = self.tmp / "portfolio.db"
        m = lake.connect(self.main)
        lake.migrate(m)
        for sid, vals in (("WDISTUS1", [100, 110, 120, 130, 140, 150]), ("WDIUPUS2", [4] * 6)):
            rows = ",".join('{"period": "%s", "value": "%s"}' % (d.date(), v)
                            for d, v in zip(pd.date_range("2024-01-05", periods=6, freq="7D"), vals))
            lake.store_payload(m, source="eia", kind="eia_weekly", content='{"response": {"data": [%s]}}' % rows, fmt="json",
                               request={"series": sid, "freq": "weekly"})
        m.close()
        p = mock.patch.object(api_router, "_V2_DB_PATH", self.main)
        p.start()
        self.addCleanup(p.stop)

    def test_unknown_symbol_starts_one_background_job(self):
        proc = mock.Mock(**{"poll.return_value": None})
        with mock.patch.object(api_router.subprocess, "Popen", return_value=proc) as popen:
            r = self.client.get("/api/chart/bars?symbol=nke&tf=1d")
            self.assertEqual((r.status_code, r.get_json()["status"]), (202, "loading"))
            self.assertEqual(self.client.get("/api/chart/bars?symbol=NKE").status_code, 202)
            self.assertEqual(popen.call_count, 1)                                  # still running: no second process
            self.assertEqual(popen.call_args.args[0][-3:], ["bars", "--symbol", "NKE"])
            proc.poll.return_value = 1                                              # it ended and left no bars
            self.assertEqual(self.client.get("/api/chart/bars?symbol=NKE").status_code, 404)
        self.assertEqual(self.client.get("/api/chart/bars?symbol=..%2Fx").status_code, 400)
        self.assertEqual(self.client.get("/api/chart/bars?symbol=SPY&tf=3m").status_code, 400)

    def test_loading_until_the_job_ends_even_once_daily_bars_exist(self):
        bars.ingest(self.conn, "NKE", "1d", daily(["2026-05-28"]), "yahoo", now=NOW)
        api_router._CHART_JOBS["NKE"] = proc = mock.Mock(**{"poll.return_value": None})
        self.assertEqual(self.client.get("/api/chart/bars?symbol=NKE&tf=15m").status_code, 202)
        proc.poll.return_value = 0
        self.assertEqual(self.client.get("/api/chart/bars?symbol=NKE&tf=1d").get_json()["status"], "success")

    def test_bars_and_catalog_from_the_store(self):
        bars.ingest(self.conn, "SPY", "1d", daily(["2026-05-27", "2026-05-28"]), "yahoo", now=NOW)
        j = self.client.get("/api/chart/bars?symbol=SPY&tf=1mo").get_json()
        self.assertEqual((j["status"], j["c"], j["v"]), ("success", [100.0], [2000]))
        j = self.client.get("/api/chart/bars?symbol=SPY&tf=1d&from=2026-05-28").get_json()
        self.assertEqual(len(j["t"]), 1)
        c = self.client.get("/api/chart/catalog").get_json()
        self.assertEqual([x["symbol"] for x in c["symbols"]], ["SPY"])
        self.assertIn("eia.days.distillate", {x["id"] for x in c["series"]})
        self.assertEqual({x["id"]: x["last"] for x in c["series"]}["eia.WDISTUS1"], "2024-02-09")

    def test_series_eia_days_of_supply(self):
        j = self.client.get("/api/chart/series?id=eia.days.distillate").get_json()
        self.assertEqual(j["status"], "success")
        self.assertEqual(len(j["v"]), 3)                                            # needs 4 weeks of supply to start
        self.assertEqual(j["v"][0], 130 / 4)
        self.assertIn("days", j["unit"])
        self.assertEqual(self.client.get("/api/chart/series?id=nope").status_code, 404)

    def test_lookup_merges_stored_and_yahoo_and_memoizes(self):
        bars.ingest(self.conn, "SPY", "1d", daily(["2026-05-28"]), "yahoo", now=NOW)
        quotes = [{"symbol": "SPYG", "shortname": "SPDR Growth", "quoteType": "ETF", "exchange": "PCX"},
                  {"symbol": "SPY", "shortname": "dup"}, {"symbol": "bad;sym"}]
        with mock.patch.object(api_router.yf, "Search") as search:
            search.return_value.quotes = quotes
            m = self.client.get("/api/chart/lookup?q=spy").get_json()["matches"]
            self.client.get("/api/chart/lookup?q=spy")
            self.assertEqual(search.call_count, 1)
        self.assertEqual([(x["symbol"], x["stored"]) for x in m], [("SPY", True), ("SPYG", False)])
        api_router._LOOKUP_CACHE.clear()
        with mock.patch.object(api_router.yf, "Search", side_effect=RuntimeError("down")):
            m = self.client.get("/api/chart/lookup?q=spy").get_json()
        self.assertEqual([x["symbol"] for x in m["matches"]], ["SPY"])

    def test_remote_forwards_every_chart_request(self):
        resp = mock.Mock(content=b'{"status": "success"}', status_code=200, headers={"Content-Type": "application/json"})
        with mock.patch.object(config, "CHART_REMOTE", "http://oracle:8082"), \
                mock.patch.object(api_router.requests, "get", return_value=resp) as get:
            r = self.client.get("/api/chart/bars?symbol=SPY&tf=1d")
        self.assertEqual(r.get_json(), {"status": "success"})
        self.assertEqual(get.call_args.args[0], "http://oracle:8082/api/chart/bars?symbol=SPY&tf=1d")


if __name__ == "__main__":
    unittest.main()
