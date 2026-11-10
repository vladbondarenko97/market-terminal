"""CHART tab: the bars store (core/bars.py), the Alpaca fetcher and the /api/chart/* routes. Offline, fixed dates.

Yahoo, Alpaca and the bars subprocess are replaced by fakes; every test gets its own CHART_STORE and portfolio.db.
"""
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401,E402  (temporary CME_Data, no credentials; must come before importing config)

import pandas as pd  # noqa: E402

import config  # noqa: E402
from core import bars, chartfans, fans, headlines, lake, sources  # noqa: E402
import main_pipeline  # noqa: E402

sys.path.insert(0, str(support.ROOT / "options_whale"))
import api_router  # noqa: E402

NY = bars.NY
CH = {"X-Chart-Request": "1"}                         # the CHART tab's header (starting a download needs it)
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
        with lake.connect(main) as m:      # fetched mid-session on 2026-05-28: that day is not finished in the capture
            m.execute("UPDATE v2_payloads SET fetched_at = '2026-05-28T16:40:00+00:00'")
        res = bars.from_lake(self.conn, main_db=main, cache_dir=self.tmp / "none", now=NOW)
        self.assertEqual((res["payloads"], res["skipped"]), (1, 1))           # intraday captures are not imported
        self.assertEqual([r[0] for r in self.conn.execute("SELECT ts FROM v2_bars ORDER BY ts")], ["2026-05-27"])
        self.assertEqual(bars.tracked(self.conn), ["SPY"])
        self.assertFalse((config.CHART_STORE / "bars/SPY/1m").exists())

    def test_a_symbol_with_only_intraday_bars_is_tracked_and_gets_daily_bars(self):
        bars.ingest(self.conn, "10Y=F", "1h", intraday("2026-05-28", "09:30", 6, freq="h"), "yahoo", now=NOW)
        self.assertTrue(bars.has(self.conn, "10Y=F"))
        self.assertIn("10Y=F", bars.tracked(self.conn))
        d = bars.read_bars(self.conn, "10Y=F", "1d")
        self.assertEqual([str(x.date()) for x in d.index], ["2026-05-28"])
        self.assertEqual(len(bars.read_bars(self.conn, "10Y=F", "1mo")), 1)

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
        self.assertEqual(len(res["errors"]), 5)          # 1d, 1wk, 1m, 5m, 1h
        self.assertIn("S&P 500", self.conn.execute("SELECT description FROM v2_chart_symbols").fetchone()[0])

    def test_collect_stores_what_yahoo_returns(self):
        s = sources.SourceSession(self.conn, "bars-test")
        with mock.patch.object(sources, "yf_history", return_value=(daily(["2026-05-28"]), 1)) as yh, \
                mock.patch("yfinance.Ticker") as tk:
            tk.return_value.info = {"longName": "Nike, Inc.", "quoteType": "EQUITY", "fullExchangeName": "NYSE",
                                    "longBusinessSummary": "Nike designs shoes. It sells them worldwide. " + "x" * 400}
            res = bars.collect(self.conn, s, ["nke"], now=NOW)
        self.assertEqual(res["errors"], [])
        self.assertEqual(yh.call_count, 5)
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
        api_router._PER_RUN.clear()
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
            r = self.client.get("/api/chart/bars?symbol=nke&tf=1d", headers=CH)
            self.assertEqual((r.status_code, r.get_json()["status"]), (202, "loading"))
            self.assertEqual(self.client.get("/api/chart/bars?symbol=NKE", headers=CH).status_code, 202)
            self.assertEqual(popen.call_count, 1)                                  # still running: no second process
            self.assertEqual(popen.call_args.args[0][-3:], ["bars", "--symbol", "NKE"])
            proc.poll.return_value = 1                                              # it ended and left no bars
            self.assertEqual(self.client.get("/api/chart/bars?symbol=NKE", headers=CH).status_code, 404)
        self.assertEqual(self.client.get("/api/chart/bars?symbol=..%2Fx").status_code, 400)
        self.assertEqual(self.client.get("/api/chart/bars?symbol=SPY&tf=3m").status_code, 400)

    def test_a_download_needs_the_chart_header_and_at_most_4_run(self):
        with mock.patch.object(api_router.subprocess, "Popen") as popen:
            r = self.client.get("/api/chart/bars?symbol=AAAA")                  # e.g. an <img> on another site
            self.assertEqual(r.status_code, 403)
            self.assertEqual(self.client.get("/api/chart/day?date=2026-05-20").get_json()["headlines_status"], "none")
            popen.assert_not_called()
            for s in "ABCD":
                api_router._CHART_JOBS[s] = mock.Mock(**{"poll.return_value": None})
            self.assertEqual(self.client.get("/api/chart/bars?symbol=EEEE", headers=CH).status_code, 429)
            popen.assert_not_called()

    def test_futures_days_end_only_when_the_new_york_date_turns(self):
        late = datetime(2026, 6, 1, 20, 30, tzinfo=NY)
        self.assertTrue(bars.finished(date(2026, 6, 1), late, "SPY"))
        self.assertFalse(bars.finished(date(2026, 6, 1), late, "SI=F"))
        self.assertTrue(bars.finished(date(2026, 6, 1), datetime(2026, 6, 2, 0, 5, tzinfo=NY), "BTC-USD"))

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


KRONOS_OFF = (None, {"status": "missing", "reason": "torch not installed", "model": "Kronos-small"})


def walk(n, end="2026-05-28", start_price=100.0):
    """n daily bars ending `end`: a gentle sine so the returns are not all zero."""
    idx = pd.bdate_range(end=end, periods=n, tz=NY, name="Date")
    c = [start_price * (1 + 0.02 * ((i % 7) - 3) / 3) for i in range(n)]
    return pd.DataFrame({"Open": c, "High": [x + 1 for x in c], "Low": [x - 1 for x in c], "Close": c, "Volume": 1000}, index=idx)


class ChartFans(Base):
    def build(self):
        bars.ingest(self.conn, "NKE", "1d", walk(120), "yahoo", now=NOW)
        bars.ingest(self.conn, "SPY", "1d", walk(120), "yahoo", now=NOW)           # the snapshot forecasts SPY: no chart 1d fan
        for day in ("2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29"):
            bars.ingest(self.conn, "SPY", "1h", intraday(day, "04:00", 16, freq="h"), "yahoo", now=NOW)
            bars.ingest(self.conn, "SPY", "5m", intraday(day, "04:00", 96, freq="5min"), "yahoo", now=NOW)
        with mock.patch.object(fans, "load_kronos", return_value=KRONOS_OFF):
            return chartfans.build_all(self.conn)

    def test_baseline_succeeds_and_a_missing_kronos_says_why(self):
        res = self.build()
        self.assertEqual(res["kronos"], "torch not installed")
        asof, last, dates, models = chartfans.read(self.conn, "NKE", "1d")
        self.assertEqual(asof, "2026-05-28")
        self.assertEqual(len(dates), 20)
        self.assertEqual(dates[0], "2026-05-29")
        self.assertEqual(models["kronos"], {"status": "missing", "reason": "torch not installed"})
        b = models["baseline"]
        self.assertEqual((b["status"], len(b["paths"]), len(b["paths"][0])), ("fresh", fans.N_PATHS, 20))
        self.assertEqual(sorted(b["q"]), ["p05", "p10", "p25", "p50", "p75", "p90", "p95"])
        self.assertIsNone(chartfans.read(self.conn, "SPY", "1d"))
        v = dict(self.conn.execute("SELECT model, version FROM v2_chart_fans WHERE item = 'NKE'").fetchall())
        self.assertEqual(v, {"kronos": "kronos_fan_v2", "baseline": "baseline_fan_v1"})

    def test_intraday_fans_for_spy_use_the_intraday_models(self):
        self.build()
        for tf, steps in (("1h", 24), ("5m", 48)):
            asof, last, dates, models = chartfans.read(self.conn, "SPY", tf)
            self.assertEqual(len(dates), steps)
            self.assertEqual(models["baseline"]["status"], "fresh")
            self.assertTrue(asof.endswith("+00:00"))
            self.assertGreater(pd.Timestamp(dates[0]), pd.Timestamp(asof))
            self.assertEqual(sorted(dates), dates)
        v = dict(self.conn.execute("SELECT model, version FROM v2_chart_fans WHERE tf = '1h'").fetchall())
        self.assertEqual(v, {"kronos": "kronos_fan_intraday_v1", "baseline": "baseline_fan_intraday_v1"})
        # the grid skips nights and weekends: Fri 2026-05-29 19:00 -> the next steps are Monday from 04:00
        nxt = chartfans.future_stamps(pd.Timestamp("2026-05-29 19:00", tz=NY), 60, 2)
        self.assertEqual([str(x) for x in nxt], ["2026-06-01 04:00:00-04:00", "2026-06-01 05:00:00-04:00"])

    def test_a_second_build_adds_nothing_and_a_short_history_is_recorded(self):
        self.build()
        with mock.patch.object(fans, "load_kronos", return_value=KRONOS_OFF):
            self.assertEqual(chartfans.build_all(self.conn)["added"], 0)
        bars.ingest(self.conn, "TINY", "1d", walk(10), "yahoo", now=NOW)             # under 60 bars: no fan at all, not a fake one
        with mock.patch.object(fans, "load_kronos", return_value=KRONOS_OFF):
            chartfans.build_all(self.conn)
        self.assertIsNone(chartfans.read(self.conn, "TINY", "1d"))

    def test_bars_command_fans_and_the_scheduled_gate(self):
        bars.ingest(self.conn, "NKE", "1d", walk(120), "yahoo", now=NOW)
        with mock.patch.object(fans, "load_kronos", return_value=KRONOS_OFF):
            self.assertEqual(main_pipeline.bars_command([], fans=True, offline=True), 0)
            self.assertIsNotNone(chartfans.read(self.conn, "NKE", "1d"))
            self.conn.execute("DELETE FROM v2_chart_fans")
            self.conn.commit()
            with mock.patch.object(chartfans, "due", return_value=False):
                main_pipeline.bars_command([], scheduled=True, offline=True)
            self.assertIsNone(chartfans.read(self.conn, "NKE", "1d"))
            with mock.patch.object(chartfans, "due", return_value=True):
                main_pipeline.bars_command([], scheduled=True, offline=True)
                self.assertTrue(chartfans.built_today(self.conn))
        self.assertTrue(chartfans.due(datetime(2026, 6, 1, 20, 15, tzinfo=NY)))
        self.assertFalse(chartfans.due(datetime(2026, 6, 1, 20, 14, tzinfo=NY)))
        self.assertFalse(chartfans.due(datetime(2026, 5, 30, 17, 0, tzinfo=NY)))        # a Saturday


SNAP_FAN = {"asof": "2026-05-29", "last": 500.0, "dates": ["2026-06-01", "2026-06-02"],
            "models": {"kronos": {"status": "missing", "reason": "no torch"},
                       "baseline": {"status": "fresh", "reason": None, "paths": [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]], "q": {}}}}


class Phase234(Base):
    def setUp(self):
        super().setUp()
        self.client = api_router.app.test_client()
        api_router._CHART_JOBS.clear()
        api_router._PER_RUN.clear()
        self.main = self.tmp / "portfolio.db"
        self.m = lake.connect(self.main)
        lake.migrate(self.m)
        p = mock.patch.object(api_router, "_V2_DB_PATH", self.main)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.m.close)
        self.n = 0

    def snapshot(self, ctx, created="2026-06-01T20:00:00+00:00"):
        self.n += 1
        rid = f"r{self.n}-{id(self)}"
        ctx = {"run": {"run_id": rid, "generated_at": created}, **ctx}
        with self.m:
            self.m.execute("INSERT INTO v2_runs (run_id, mode, status, started_at) VALUES (?, 'live', 'committed', ?)", (rid, created))
            self.m.execute("INSERT INTO v2_snapshots (run_id, created_at, schema_version, context_json, context_sha256) VALUES (?,?,?,?,?)",
                           (rid, created, "v2.1", json.dumps(ctx), "x"))
        return rid

    def observe(self, metric, at, value=None, entity="", dims=None, run_id=None):
        lake.record_observations(self.m, [lake.obs(metric, at, entity=entity, dims=dims, value=value, source="test")], run_id=run_id)

    # ---- /api/chart/fan
    def test_fan_from_the_snapshot_with_quantiles_computed_from_paths(self):
        self.snapshot({"fans": {"series": {"SPY": SNAP_FAN, "VIX": dict(SNAP_FAN, asof=None)}}})
        j = self.client.get("/api/chart/fan?item=spy&tf=1d").get_json()
        self.assertEqual((j["status"], j["source"], j["asof"], j["last"], j["dates"]), ("success", "snapshot", "2026-05-29", 500.0, SNAP_FAN["dates"]))
        self.assertEqual(j["models"]["kronos"], {"status": "missing", "reason": "no torch"})
        self.assertEqual(j["models"]["baseline"]["q"]["p50"], [3.0, 4.0])
        self.assertIn("p05", j["models"]["baseline"]["q"])
        self.assertIn("baseline", j["verdict"])
        # a snapshot series with no data falls through to the chart store, which has nothing: missing, not zero
        j = self.client.get("/api/chart/fan?item=%5EVIX").get_json()
        self.assertEqual(j["status"], "missing")
        self.assertEqual(self.client.get("/api/chart/fan?item=SPY&tf=3m").status_code, 400)

    def test_fan_maps_series_ids_to_snapshot_ids(self):
        self.snapshot({"fans": {"series": {"days.distillate": SNAP_FAN, "eia.WDISTUS1": SNAP_FAN}}})
        for item in ("eia.days.distillate", "eia.WDISTUS1"):
            self.assertEqual(self.client.get(f"/api/chart/fan?item={item}").get_json()["source"], "snapshot")

    def test_diesel_kronos_comes_from_the_chart_store_when_the_snapshot_has_none(self):
        self.snapshot({"fans": {"series": {"days.distillate": SNAP_FAN}}})          # its kronos is missing
        paths = [[1.0, 2.0], [3.0, 4.0]]
        with self.conn:
            self.conn.execute("INSERT INTO v2_chart_fans VALUES (?,?,?,?,?,?,?)", ("eia.days.distillate", "1d", "2026-05-29", "kronos", "v", "t",
                              json.dumps({"last": 5.0, "dates": SNAP_FAN["dates"], "model": chartfans.pack({"status": "fresh", "paths": paths})})))
        j = self.client.get("/api/chart/fan?item=eia.days.distillate").get_json()
        self.assertEqual((j["source"], j["models"]["kronos"]["paths"]), ("snapshot", paths))
        self.assertEqual(j["models"]["baseline"]["q"]["p50"], [3.0, 4.0])

    def test_bounded_series_get_a_reason_not_a_fan(self):
        for item in ("chaos.score", "fedops.repo_day:rrp"):          # also: an id with "_" passes validation
            j = self.client.get(f"/api/chart/fan?item={item}").get_json()
            self.assertEqual(j["status"], "missing")
            self.assertIn("return-based fans", j["reason"])

    def test_fan_from_the_chart_store_and_missing(self):
        bars.ingest(self.conn, "NKE", "1d", walk(120), "yahoo", now=NOW)
        with mock.patch.object(fans, "load_kronos", return_value=KRONOS_OFF):
            chartfans.build_all(self.conn)
        j = self.client.get("/api/chart/fan?item=nke").get_json()
        self.assertEqual((j["status"], j["source"], j["asof"]), ("success", "chart", "2026-05-28"))
        self.assertEqual(j["models"]["kronos"]["status"], "missing")
        self.assertEqual(len(j["models"]["baseline"]["paths"]), 100)
        self.assertEqual(j["verdict"], fans.KRONOS_VERDICT)
        j = self.client.get("/api/chart/fan?item=ZZZ&tf=5m").get_json()
        self.assertEqual(j["status"], "missing")

    # ---- /api/chart/overlays
    def test_overlays_from_a_minimal_snapshot(self):
        self.snapshot({"options": {"SPY": {"gex": {"status": "fresh", "call_wall": 560.0, "put_wall": 540.0, "zero_gamma": 550.5},
                                           "max_pain": {"strike": 552.0}}}})
        with self.m:
            self.m.execute("INSERT INTO v2_trade_signals (run_id, created_at, underlying, position_type, strike, underlying_price) "
                           "VALUES ('t1', '2026-06-01', 'SPY', 'CALL', 565, 551.2)")
        self.observe("calendar.event", "2026-06-17T18:00:00+00:00", entity="USD", dims={"title": "FOMC Statement", "impact": "High"})
        self.observe("calendar.event", "2026-06-02T12:30:00+00:00", entity="USD", dims={"title": "JOLTS", "impact": "Medium"})
        self.observe("trade.block", "2026-06-01T15:00:00+00:00", entity="SPY", value={"price": 551.0, "size": 20000})
        j = self.client.get("/api/chart/overlays?symbol=SPY").get_json()
        self.assertEqual(j["status"], "success")
        kinds = {x["kind"]: x["price"] for x in j["levels"]}
        self.assertEqual(kinds, {"call_wall": 560.0, "put_wall": 540.0, "zero_gamma": 550.5, "max_pain": 552.0, "entry": 551.2, "target": 565.0})
        self.assertEqual([(z["kind"], z["lo"], z["hi"]) for z in j["zones"]], [("gex", 540.0, 560.0)])
        self.assertEqual([(m["kind"], m["label"]) for m in j["markers"]], [("fomc", "FOMC Statement")])
        self.assertEqual(j["markers"][0]["t"], int(pd.Timestamp("2026-06-17T18:00:00Z").timestamp()))
        self.assertEqual(j["blocks"][0]["notional"], 551.0 * 20000)
        q = self.client.get("/api/chart/overlays?symbol=NKE").get_json()
        self.assertEqual((q["status"], q["levels"], q["zones"], q["blocks"]), ("success", [], [], []))

    # ---- /api/chart/day
    def test_day_with_stored_prices_series_runs_calendar_and_headlines(self):
        bars.ingest(self.conn, "SPY", "1d", daily(["2026-05-27"], close=100.0), "yahoo", now=NOW)
        bars.ingest(self.conn, "SPY", "1d", daily(["2026-05-28"], close=110.0), "yahoo", now=NOW)
        rid = self.snapshot({}, created="2026-05-28T20:00:00+00:00")
        self.observe("options.SPY.gex_profile", "2026-05-28T20:00:00+00:00", entity="SPY", value={"put_wall": 540, "call_wall": 560}, run_id=rid)
        self.observe("macro.vmri.score", "2026-05-28T20:00:00+00:00", value=41.25, run_id=rid)
        self.observe("calendar.event", "2026-05-28T12:30:00+00:00", entity="USD", dims={"title": "GDP q/q", "impact": "High"})
        headlines._insert(self.conn, datetime(2026, 5, 28).date(), "wikipedia", [("Oil rises", "https://x.test/a", "Business and economy")])
        with mock.patch.object(api_router.subprocess, "Popen") as popen:
            j = self.client.get("/api/chart/day?date=2026-05-28&symbols=SPY,NKE&series=eia.WDISTUS1", headers=CH).get_json()
            self.assertEqual(popen.call_count, 0)                                   # stored headlines: no job
        self.assertEqual(j["status"], "success")
        self.assertEqual([(p["symbol"], p["c"], p["chg_pct"]) for p in j["prices"]], [("SPY", 110.0, 10.0)])   # NKE has no bars: left out
        self.assertEqual(j["series"], [])                                           # the fixture's EIA data starts in 2024
        self.assertEqual(len(j["runs"]), 1)
        self.assertEqual(j["runs"][0]["summary"], "SPY walls 540 / 560 · VMRI 41.2")
        self.assertEqual(j["calendar"], [{"time": "08:30", "title": "GDP q/q", "kind": "macro"}])
        self.assertEqual((j["headlines_status"], [h["title"] for h in j["headlines"]]), ("stored", ["Oil rises"]))
        self.assertEqual(self.client.get("/api/chart/day?date=nope").status_code, 400)
        self.assertEqual(self.client.get("/api/chart/day?date=2026-05-28&series=nope").status_code, 400)

    def test_day_series_value_as_of_the_date(self):
        for sid, vals in (("WDISTUS1", [100, 110, 120, 130, 140, 150]), ("WDIUPUS2", [4] * 6)):
            rows = ",".join('{"period": "%s", "value": "%s"}' % (d.date(), v)
                            for d, v in zip(pd.date_range("2024-01-05", periods=6, freq="7D"), vals))
            lake.store_payload(self.m, source="eia", kind="eia_weekly", content='{"response": {"data": [%s]}}' % rows, fmt="json",
                               request={"series": sid, "freq": "weekly"})
        j = self.client.get("/api/chart/day?date=2024-02-03&series=eia.WDISTUS1").get_json()
        self.assertEqual(j["series"][0]["value"], 140.0)
        self.assertEqual((j["series"][0]["asof"], j["series"][0]["wow"]), ("2024-02-02", 10.0))

    def test_day_starts_one_headline_job_per_date(self):
        proc = mock.Mock(**{"poll.return_value": None})
        with mock.patch.object(api_router.subprocess, "Popen", return_value=proc) as popen:
            for _ in range(2):
                j = self.client.get("/api/chart/day?date=2026-05-20", headers=CH).get_json()
                self.assertEqual((j["headlines_status"], j["headlines"]), ("loading", []))
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(popen.call_args.args[0][-3:], ["headlines", "--date", "2026-05-20"])
            proc.poll.return_value = 0                                              # it ended and found nothing
            self.assertEqual(self.client.get("/api/chart/day?date=2026-05-20", headers=CH).get_json()["headlines_status"], "none")
            self.assertEqual(popen.call_count, 1)
            proc.poll.return_value = 1                                              # a failed job is forgotten: the next request retries
            self.client.get("/api/chart/day?date=2026-05-20", headers=CH)
            self.client.get("/api/chart/day?date=2026-05-20", headers=CH)
            self.assertEqual(popen.call_count, 2)


# the real page's shape: a <p> category heading, then a <ul> of stand-alone events and topics whose leaves are events
WIKI_HTML = """<div class="current-events-content"><p><b>Armed conflicts and attacks</b></p><ul>
<li><a href="/wiki/War">Some war</a><ul><li>A town is shelled. <a href="/wiki/Town">Town</a></li></ul></li></ul>
<p><b>Business and economy</b></p><ul>
<li>Oil rises 3%. <a class="external text" href="https://news.test/oil">(Reuters)</a></li>
<li><a href="/wiki/Markets">Markets</a><ul><li>Nested detail <a class="external text" href="https://news.test/nested">(AP)</a></li></ul></li></ul></div>"""
GDELT = {"articles": [{"url": "https://a.test/1", "title": " Fed holds rates ", "domain": "a.test"}, {"url": "", "title": "no link"},
                      {"url": "https://a.test/2", "title": "", "domain": "a.test"}]}


class Headlines(Base):
    D = datetime(2026, 5, 28).date()

    def test_parsers(self):
        items = headlines.parse_wikipedia(WIKI_HTML, self.D)
        self.assertEqual([(t, u.split("#")[0], c) for t, u, c in items], [
            ("Oil rises 3%. (Reuters)", "https://news.test/oil", "Business and economy"),
            ("Nested detail (AP)", "https://news.test/nested", "Business and economy"),
            ("A town is shelled. Town", "https://en.wikipedia.org/wiki/Portal:Current_events/2026_May_28", "Armed conflicts and attacks")])
        self.assertEqual(headlines.parse_gdelt(GDELT), [("Fed holds rates", "https://a.test/1", "a.test")])
        self.assertEqual(headlines.parse_gdelt({}), [])

    def reply(self, body, text=None):
        return mock.Mock(status_code=200, text=text if text is not None else json.dumps(body), **{"json.return_value": body})

    def test_collect_day_stores_once_and_records_empty_answers(self):
        s = sources.SourceSession(self.conn, "headlines-test")
        replies = [self.reply({"parse": {"text": WIKI_HTML}}), self.reply(GDELT)]
        with mock.patch.object(headlines.requests, "get", side_effect=replies) as get, mock.patch.object(headlines.time, "sleep"):
            res = headlines.collect_day(self.conn, s, self.D)
            self.assertEqual((res["added"], res["errors"]), (4, []))
            self.assertTrue(headlines.is_done(self.conn, self.D))
            self.assertEqual(headlines.collect_day(self.conn, s, self.D)["added"], 0)
            self.assertEqual(get.call_count, 2)                                     # not fetched again
        got = headlines.stored(self.conn, self.D)
        self.assertEqual(sorted(g["source"] for g in got), ["gdelt", "wikipedia", "wikipedia", "wikipedia"])   # market words rank first
        self.assertEqual(self.conn.execute("SELECT DISTINCT first_seen_run FROM v2_news_items").fetchone()[0], "chart:2026-05-28")
        # a day with no Wikipedia page and no GDELT matches is an answer too: recorded, never refetched
        other = datetime(2026, 5, 27).date()
        replies = [self.reply({"error": {"code": "missingtitle"}}), self.reply(None, text="")]
        with mock.patch.object(headlines.requests, "get", side_effect=replies), mock.patch.object(headlines.time, "sleep"):
            self.assertEqual(headlines.collect_day(self.conn, sources.SourceSession(self.conn, "t2"), other), {"added": 0, "errors": []})
        self.assertTrue(headlines.is_done(self.conn, other))

    def test_gdelt_is_skipped_before_2017_and_failures_are_not_marked_done(self):
        old = datetime(2010, 3, 1).date()
        with mock.patch.object(headlines.requests, "get", return_value=self.reply({"parse": {"text": WIKI_HTML}})) as get:
            headlines.collect_day(self.conn, sources.SourceSession(self.conn, "t"), old)
            self.assertEqual(get.call_count, 1)
        self.assertTrue(headlines.is_done(self.conn, old))
        bad = mock.Mock(status_code=429, text="slow down")
        with mock.patch.object(headlines.requests, "get", side_effect=[self.reply({"parse": {"text": WIKI_HTML}}), bad]), \
                mock.patch.object(headlines.time, "sleep"):
            res = headlines.collect_day(self.conn, sources.SourceSession(self.conn, "t3"), self.D)
        self.assertEqual(len(res["errors"]), 1)
        self.assertFalse(headlines.is_done(self.conn, self.D))

    def test_headlines_command_offline_fetches_nothing(self):
        with mock.patch.object(headlines.requests, "get") as get:
            self.assertEqual(main_pipeline.headlines_command("2026-05-28", offline=True), 1)
            get.assert_not_called()
        self.assertEqual(main_pipeline.headlines_command(None), 2)


if __name__ == "__main__":
    unittest.main()
