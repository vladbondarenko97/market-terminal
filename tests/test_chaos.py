"""Chaos / Peace index (Forecast Lab card 14): the pure model on seeded synthetic series, the four fetchers on saved
responses (requests.get mocked), the guarded collector on an offline session and the stored /api/chaos route."""
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

from core import chaos as CH, collect, lake, sources as src
from core.sources import SourceSession, SourceUnavailable
from test_api import ApiCase

FIX = support.FIX / "chaos"
TODAY = date(2026, 10, 9)          # a Friday and a trading day


def series(seed=1, n=800, end=TODAY, mean=20.0, sd=1.0, last_z=None):
    """Stationary noise on business days ending at `end`; `last_z` pins the final value to mean + last_z * sd."""
    rng = np.random.default_rng(seed)
    s = pd.Series(mean + sd * rng.standard_normal(n), index=pd.bdate_range(end=end, periods=n))
    if last_z is not None:
        s.iloc[-2:] = mean + last_z * sd             # the day before too, so a test controls both days it compares
    return s


def block(**kw):
    return CH.build({"today": TODAY, **kw})


class Scoring(unittest.TestCase):
    def test_no_data_is_missing_and_keeps_the_reasons(self):
        c = block(series={}, series_errors={"VIX": "yahoo: offline"})
        self.assertEqual(c["status"], "missing")
        self.assertIsNone(c["score"])
        self.assertEqual(c["components_used"], 0)
        self.assertEqual(c["components_total"], 12)
        self.assertEqual(len(c["components_missing"]), 12)
        self.assertIn("yahoo: offline", c["components_missing"]["vix"])
        self.assertIn("no data", c["components_missing"]["move"])

    def test_one_component_gives_a_score(self):
        c = block(series={"VIX": series(last_z=1.0)})
        self.assertEqual(c["status"], "fresh")
        self.assertEqual(c["components_used"], 1)
        self.assertEqual(c["components"]["vix"]["status"], "fresh")
        self.assertEqual(len(c["components_missing"]), 11)
        self.assertAlmostEqual(c["score"], 50 * c["mean_z"], delta=0.6)
        self.assertEqual(c["drivers"][0]["key"], "vix")
        self.assertEqual(c["as_of"], "2026-10-09")
        self.assertEqual(len(c["history"]), 60)

    def test_regime_thresholds(self):
        for score, name in ((-100, "peace"), (-30, "peace"), (-29.9, "neutral"), (30, "neutral"), (30.1, "stress"),
                            (60, "stress"), (60.1, "chaos"), (100, "chaos")):
            self.assertEqual(CH.regime_of(score), name, score)

    def test_regime_change_when_the_last_day_crosses_a_threshold(self):
        calm = block(series={"VIX": series(last_z=0.0)})
        self.assertFalse(calm["regime_changed"])
        self.assertEqual(calm["regime"], "neutral")
        s = series(last_z=0.0)
        s.iloc[-1] = 20.0 + 1.6                      # about +1.6 sd on the last day: score near 80
        c = block(series={"VIX": s})
        self.assertEqual(c["prev"]["regime"], "neutral")
        self.assertEqual(c["regime"], "chaos")
        self.assertTrue(c["regime_changed"])
        self.assertEqual(c["prev"]["date"], "2026-10-08")

    def test_short_or_stale_series_are_missing_with_the_reason(self):
        c = block(series={"VIX": series(n=300), "MOVE": series(n=100), "VVIX": series(end=date(2026, 9, 1)), "SKEW": series(seed=4)})
        self.assertRegex(c["components_missing"]["vix"], r"^history shorter than 3y \(300 observations since \d{4}-\d\d-\d\d\)$")
        self.assertRegex(c["components_missing"]["move"], r"^history shorter than 3y \(100 observations since ")
        self.assertEqual(c["components_missing"]["vvix"], "stale: last observation 2026-09-01")
        self.assertEqual(c["components"]["skew"]["status"], "fresh")
        self.assertEqual(c["components_used"], 1)

    def test_derived_components_and_orientation(self):
        spy = series(seed=2, n=1400, mean=500, sd=5)
        spy.iloc[-1] = spy.iloc[:-1].max() * 0.8                      # 20% below the high: drawdown +0.2, high z
        c = block(series={"SPY": spy, "VIX": series(), "VIX3M": series(seed=3, mean=22)})
        self.assertEqual(c["components"]["spy_drawdown"]["status"], "fresh")
        self.assertAlmostEqual(c["components"]["spy_drawdown"]["value"], 0.2, delta=0.01)
        self.assertGreater(c["components"]["spy_drawdown"]["z"], 2)
        self.assertEqual(c["components"]["spy_rv20"]["status"], "fresh")
        self.assertEqual(c["components"]["vix_vix3m"]["status"], "fresh")
        self.assertEqual(c["components_used"], 4)

    def test_election_counts_trading_days(self):
        e = CH._election(TODAY)
        self.assertEqual(e["date"], "2026-11-03")
        self.assertEqual(e["trading_days_to"], 17)                    # Oct 12..Nov 3 less no holiday: NYSE is open on Columbus Day
        self.assertTrue(e["tracking"])
        self.assertLess(CH._election(date(2026, 11, 5))["trading_days_to"], 0)
        self.assertEqual(CH._election(date(2027, 3, 1))["date"], "2028-11-07")      # the 2026 tracking window has ended


class Studies(unittest.TestCase):
    def prices(self, start, seed, plant=None):
        idx = pd.bdate_range(start, "2024-12-31")
        r = np.random.default_rng(seed).normal(0.0003, 0.01, len(idx))
        if plant:
            for y in range(1950, 2023):
                if y % 4 == 2:
                    p = idx.searchsorted(pd.Timestamp(CH.election_day(y)))
                    if p + 6 < len(idx):
                        r[p:p + 6] += plant
        return pd.Series(100 * np.exp(np.cumsum(r)), index=idx)

    def test_event_study_finds_a_planted_effect(self):
        st = CH._event_study(self.prices("1950-01-02", 7, plant=0.01), self.prices("1990-01-02", 8))
        row = next(r for r in st["rows"] if r["series"] == "S&P 500" and r["window"] == "0..+5")
        self.assertEqual(row["n"], 19)
        self.assertGreater(row["t"], 2)
        self.assertEqual(row["grade"], "thin")
        self.assertIn("thin", st["note"])
        vix = [r for r in st["rows"] if r["series"] == "VIX"]
        self.assertEqual({r["n"] for r in vix} - {9, 8}, set())

    def test_event_study_with_no_effect_is_not_graded(self):
        st = CH._event_study(self.prices("1950-01-02", 11), self.prices("1990-01-02", 12))
        self.assertEqual([r["grade"] for r in st["rows"] if r["window"] == "0..+5" and r["kind"] == "midterm"], ["none", "none"])

    def test_event_study_without_history_is_missing(self):
        self.assertEqual(CH._event_study(None, None)["status"], "missing")

    def test_lever_study_counts_years(self):
        rng = np.random.default_rng(5)
        w = pd.Series(4000 + np.cumsum(rng.normal(0, 20, 1200)), index=pd.date_range("2003-01-01", periods=1200, freq="W-WED"))
        w = w[w.index <= "2026-10-07"]
        st = CH._lever_study({"fred": {"WALCL": w}})
        row = next(r for r in st["rows"] if r["lever"].startswith("Fed assets"))
        self.assertEqual((row["n"], row["n_base"]), (11, 12))          # even years 2004..2024; odd years 2003..2025
        self.assertIn("up = easier", row["definition"])
        late = w.copy()
        late[pd.Timestamp("2026-10-28")] = late.iloc[-1]        # six days before the 2026 election: its window is not complete yet
        row = next(r for r in CH._lever_study({"fred": {"WALCL": late}})["rows"] if r["lever"].startswith("Fed assets"))
        self.assertEqual((row["n"], row["n_base"]), (11, 12))          # 2026 is not counted before its election day
        self.assertTrue(all(r["status"] == "missing" for r in st["rows"] if not r["lever"].startswith("Fed assets")))
        self.assertIn("Descriptive only", st["note"])

    def test_levers_units_and_missing_rows(self):
        idx = pd.date_range("2026-06-03", periods=18, freq="W-WED")
        walcl = pd.Series(6_700_000.0, index=idx)                    # USD millions
        tga = pd.Series(800_000.0, index=idx)                        # USD millions
        rrp = pd.Series(100.0, index=pd.bdate_range(idx[0], idx[-1]))        # USD billions
        lv = CH._levers({"fred": {"WALCL": walcl, "WTREGEN": tga, "RRPONTSYD": rrp}, "errors": {"treasury:auctions": "treasury: http_500"}}, TODAY)
        net = next(r for r in lv["rows"] if r["key"] == "net_liquidity")
        self.assertEqual(net["level"], 5800.0)                       # 6,700 - 800 - 100 bn
        self.assertEqual(net["change_20d"], 0.0)
        bills = next(r for r in lv["rows"] if r["key"] == "bills_offered")
        self.assertEqual((bills["status"], bills["reason"]), ("missing", "treasury: http_500"))
        self.assertEqual(lv["status"], "fresh")


def fake_get(status=200, content=b"", payload=None):
    return SimpleNamespace(status_code=status, content=content, text=content.decode("utf-8", "replace") if isinstance(content, bytes) else content,
                           json=lambda: payload)


class Fetchers(unittest.TestCase):
    def session(self, offline=False):
        tmp = tempfile.mkdtemp(prefix="chaos_")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        conn = lake.connect(Path(tmp) / "portfolio.db")
        self.addCleanup(conn.close)
        lake.migrate(conn)
        lake.create_run(conn, "t", mode="live", trigger="manual", run_folder="Oct-09-26", code_version="t")
        return SourceSession(conn, "t", offline=offline)

    def test_cboe_parses_the_equity_ratio_and_reports_a_missing_day(self):
        body = (FIX / "cboe_daily.json").read_bytes()
        with mock.patch("requests.get", return_value=fake_get(200, body)) as g:
            ratio, pid = src.cboe_putcall(self.session(), "2026-10-09")
        self.assertEqual(ratio, 0.56)
        self.assertTrue(pid)
        self.assertIn("2026-10-09_daily_options", g.call_args.args[0])
        with mock.patch("requests.get", return_value=fake_get(403)):
            with self.assertRaises(SourceUnavailable) as cm:
                src.cboe_putcall(self.session(), "2026-10-10")
        self.assertEqual(cm.exception.outcome, "http_403")
        with mock.patch("requests.get", return_value=fake_get(200, json.dumps({"ratios": [{"name": "X", "value": "1"}]}).encode())):
            with self.assertRaises(SourceUnavailable) as cm:
                src.cboe_putcall(self.session(), "2026-10-08")
        self.assertEqual(cm.exception.outcome, "parse")

    def test_finra_parses_the_symbol_and_stores_only_its_line(self):
        text = (FIX / "finra.txt").read_bytes()
        s = self.session()
        with mock.patch("requests.get", return_value=fake_get(200, text)):
            ratio, pid = src.finra_short_volume(s, "2026-10-09", "SPY")
        self.assertAlmostEqual(ratio, 4289397.421129 / 8041004.654184)
        row, content = lake.load_payload(s.conn, pid)
        self.assertEqual(content.decode().splitlines()[1].split("|")[1], "SPY")
        self.assertEqual(len(content.decode().splitlines()), 2)
        self.assertEqual(json.loads(row["coverage_json"])["kept"], ["SPY"])
        with mock.patch("requests.get", return_value=fake_get(200, text)):
            with self.assertRaises(SourceUnavailable) as cm:
                src.finra_short_volume(self.session(), "2026-10-09", "ZZZZ")
        self.assertEqual(cm.exception.outcome, "parse")
        with mock.patch("requests.get", return_value=fake_get(404)):
            with self.assertRaises(SourceUnavailable):
                src.finra_short_volume(self.session(), "2026-10-10")

    def test_treasury_fetchers_keep_the_needed_fields(self):
        auc = json.loads((FIX / "td.json").read_text())
        with mock.patch("requests.get", return_value=fake_get(200, b"", auc)):
            rows, pid = src.treasury_auctions(self.session(), 60)
        self.assertLessEqual({"auctionDate", "securityType", "offeringAmount"}, set(rows[0]))
        self.assertEqual(rows[0]["auctionDate"], "2026-10-08")      # TreasuryDirect has no auctionDate: the competitive close date is used
        self.assertEqual(len(rows), len(auc))
        bb = json.loads((FIX / "bb.json").read_text())
        with mock.patch("requests.get", return_value=fake_get(200, b"", bb)):
            rows, pid = src.treasury_buybacks(self.session(), "2026-08-01")
        self.assertEqual(set(rows[0]), set(src.BUYBACK_KEYS))
        with mock.patch("requests.get", return_value=fake_get(500)):
            with self.assertRaises(SourceUnavailable):
                src.treasury_buybacks(self.session(), "2026-08-01")

    def test_offline_session_makes_no_request(self):
        with mock.patch("requests.get", side_effect=AssertionError("network")):
            for call in (lambda s: src.cboe_putcall(s, "2026-10-09"), lambda s: src.finra_short_volume(s, "2026-10-09"),
                         lambda s: src.treasury_auctions(s), lambda s: src.treasury_buybacks(s, "2026-08-01"),
                         lambda s: src.yf_long_history(s, "^GSPC")):
                with self.assertRaises(SourceUnavailable):
                    call(self.session(offline=True))

    def test_long_history_is_reused_when_stored(self):
        s = self.session()
        df = pd.DataFrame({"Close": [1.0, 2.0, 3.0]}, index=pd.DatetimeIndex(["2026-01-02", "2026-01-05", "2026-01-06"], tz="America/New_York"))
        with mock.patch("yfinance.Ticker") as t:
            t.return_value.history.return_value = df
            first = src.yf_long_history(s, "^GSPC")
            again = src.yf_long_history(SourceSession(s.conn, "t2"), "^GSPC")
        self.assertEqual(t.return_value.history.call_count, 1)             # the second read came from the stored payload
        self.assertEqual(list(again.values), list(first.values))
        self.assertEqual(again.index[0], pd.Timestamp("2026-01-02"))
        off = src.yf_long_history(SourceSession(s.conn, "t3", offline=True), "^GSPC")     # offline with a stored capture works
        self.assertEqual(len(off), 3)


class Collector(unittest.TestCase):
    def test_guarded_chaos_on_an_offline_empty_lake_is_missing_not_error(self):
        tmp = tempfile.mkdtemp(prefix="chaos_")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        conn = lake.connect(Path(tmp) / "portfolio.db")
        self.addCleanup(conn.close)
        lake.migrate(conn)
        lake.create_run(conn, "t", mode="offline", trigger="manual", run_folder="Oct-09-26", code_version="t")
        c = collect._guarded_chaos(conn, SourceSession(conn, "t", offline=True), "t", TODAY, {})
        self.assertEqual(c["status"], "missing")
        self.assertEqual(c["components_total"], 12)
        self.assertIn("equity_put_call", c["components_missing"])
        self.assertIn("no observations stored yet", c["components_missing"]["equity_put_call"])
        self.assertTrue(c["source_errors"])
        self.assertEqual(c["levers"]["status"], "missing")

    def test_guarded_chaos_turns_an_exception_into_an_error_block(self):
        with mock.patch.object(collect, "build_chaos", side_effect=RuntimeError("boom")):
            c = collect._guarded_chaos(None, None, "t", TODAY, {})
        self.assertEqual((c["status"], c["reason"]), ("error", "RuntimeError: boom"))

    def test_daily_observations_accumulate_once_per_day(self):
        tmp = tempfile.mkdtemp(prefix="chaos_")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        conn = lake.connect(Path(tmp) / "portfolio.db")
        self.addCleanup(conn.close)
        lake.migrate(conn)
        lake.create_run(conn, "t", mode="live", trigger="manual", run_folder="Oct-09-26", code_version="t")
        calls = []

        def pc(s, d):
            calls.append(str(d))
            if str(d) == "2026-10-07":
                raise SourceUnavailable("cboe", "http_403", str(d))
            return 0.6, None
        def finra(s, d):
            raise SourceUnavailable("finra", "http_404", str(d))
        # CHAOS_DAILY holds the fetch functions themselves, so both entries are replaced there; every other source the
        # collector calls is unavailable, so the test never leaves the machine
        down = [mock.patch.object(src, n, side_effect=SourceUnavailable(n, "offline_test"))
                for n in ("yf_history", "yf_long_history", "eia_weekly", "treasury_auctions", "treasury_buybacks")]
        with mock.patch.dict(collect.CHAOS_DAILY, {"PUTCALL": ("chaos.equity_put_call", "cboe", pc), "SHORTVOL": ("chaos.short_volume_spy", "finra", finra)}), \
                down[0], down[1], down[2], down[3], down[4]:
            c1 = collect.build_chaos(conn, SourceSession(conn, "t"), "t", TODAY, {})
            n = len(calls)
            collect.build_chaos(conn, SourceSession(conn, "t2"), "t2", TODAY, {})
        stored = [r[0] for r in conn.execute("SELECT observed_at FROM v2_observations WHERE metric_id = 'chaos.equity_put_call' ORDER BY 1")]
        self.assertEqual(stored, ["2026-10-02", "2026-10-05", "2026-10-06", "2026-10-08"])      # 5 trading days before the 9th, less the 403 day
        self.assertEqual(len(calls) - n, 1)                                                      # only the 403 day is asked again
        self.assertIn("cboe:2026-10-07", c1["source_errors"])
        self.assertRegex(c1["components_missing"]["equity_put_call"], r"^history shorter than 3y \(4 observations")


class Route(ApiCase):
    def test_api_chaos_serves_the_stored_block(self):
        conn = lake.connect(self.tmp / "portfolio.db")
        lake.migrate(conn)
        self.assertEqual(self.client.get("/api/chaos").status_code, 404)             # database, no snapshot
        block_ = block(series={"VIX": series(last_z=1.0)})
        lake.create_run(conn, "r1", mode="live", trigger="manual", run_folder="Oct-09-26", code_version="t")
        lake.commit_snapshot(conn, "r1", {"run": {"run_id": "r1"}, "chaos": block_})
        lake.update_run(conn, "r1", status="completed")
        conn.close()
        j = self.get_json("/api/chaos")
        self.assertEqual((j["status"], j["as_of"], j["run"]["run_id"]), ("success", "2026-10-09", "r1"))
        self.assertEqual(j["data"]["components_used"], 1)
        fc = self.get_json("/api/forecast?ticker=SPY")                               # no forecast key in this snapshot, chaos still rides along
        self.assertEqual(fc["chaos"]["score"], j["data"]["score"])

    def test_api_chaos_is_503_before_the_first_run_and_404_without_a_block(self):
        self.assertEqual(self.client.get("/api/chaos").status_code, 503)
        conn = lake.connect(self.tmp / "portfolio.db")
        lake.migrate(conn)
        lake.create_run(conn, "r1", mode="live", trigger="manual", run_folder="Oct-09-26", code_version="t")
        lake.commit_snapshot(conn, "r1", {"run": {"run_id": "r1"}})
        lake.update_run(conn, "r1", status="completed")
        conn.close()
        r = self.client.get("/api/chaos")
        self.assertEqual((r.status_code, r.get_json()["status"]), (404, "error"))


class Rendering(unittest.TestCase):
    def test_lines(self):
        from core import render
        self.assertEqual(render.chaos_line({}), "Chaos/Peace: not in this snapshot")
        self.assertIn("missing (no component", render.chaos_line({"chaos": block(series={})}))
        line = render.chaos_line({"chaos": block(series={"VIX": series(last_z=1.0)})})
        self.assertRegex(line, r"^Chaos/Peace: \+\d+ \w+ \(1 of 12 components\); top: VIX \+[\d.]+z; midterm in 17 trading days$")

    def test_the_word_before_the_countdown_follows_the_election_kind(self):
        from core import render
        mk = lambda el: {"chaos": {"score": 5.0, "regime": "neutral", "components_used": 3, "components_total": 12, "election": el}}
        self.assertTrue(render.chaos_line(mk({"trading_days_to": 5})).endswith("; midterm in 5 trading days"))
        self.assertTrue(render.chaos_line(mk({"kind": "presidential", "trading_days_to": -3})).endswith("; 3 trading days after the presidential"))

    def test_signal_watch_row_fires_on_a_regime_change_and_recommends_nothing(self):
        from core import forecast as F
        s = series(last_z=0.0)
        s.iloc[-1] = 21.6
        rows = F.signal_watch({"chaos": block(series={"VIX": s})})
        row = next(r for r in rows if r["name"] == "Chaos/Peace regime")
        self.assertTrue(row["fired"])
        self.assertEqual(row["action"], "Chaos/Peace regime changed: neutral -> chaos (a description of the reading, not a trade signal)")
        quiet = F.signal_watch({"chaos": block(series={"VIX": series(last_z=0.0)})})
        self.assertFalse(next(r for r in quiet if r["name"] == "Chaos/Peace regime")["fired"])
        self.assertEqual([r for r in F.signal_watch({}) if r["name"] == "Chaos/Peace regime"], [])


if __name__ == "__main__":
    unittest.main()
