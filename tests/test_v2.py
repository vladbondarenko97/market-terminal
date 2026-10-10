"""Acceptance checks for the v2 brief (Gherkin scenarios as plain unittest; no network, no credentials).

Run:  python -m unittest discover -s tests -v
Everything runs inside a temporary CME_Data copy built from tests/fixtures.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
TMP = Path(tempfile.mkdtemp(prefix="v2test_"))
DATA = TMP / "CME_Data"
os.environ["PORTFOLIO_DATA_DIR"] = str(DATA)
for k in ("DATABENTO_API_KEY", "DB_API_KEY", "EMAIL_SENDER", "EMAIL_PASSWORD"):
    os.environ[k] = ""
sys.path.insert(0, str(ROOT))


def build_installation():
    DATA.mkdir(parents=True, exist_ok=True)
    for f in FIX.iterdir():
        shutil.copy(f, DATA / f.name)
    # a download-dated duplicate like the real archive has (same bytes, different name)
    shutil.copy(FIX / "daily_volume_20260819.xlsx", DATA / "daily_volume_20260820.xlsx")
    (DATA / "state.json").write_text('{"cookies": [{"name": "secret"}]}')
    conn = sqlite3.connect(DATA / "portfolio.db")
    conn.execute('CREATE TABLE "comex_inventory_history" ("Date" TEXT, "Registered" REAL, "Eligible" REAL, '
                 '"Total" REAL, "Reg_Change" REAL, "Elig_Change" REAL, "Total_Change" REAL, "Legacy_Extra" TEXT)')
    conn.execute("INSERT INTO comex_inventory_history VALUES ('2026-09-24', 95494503.432, 235879562.0304, "
                 "331374065.4624, 9661.2, 597730.65, 607391.85, 'keep-me')")
    conn.execute('CREATE TABLE "crypto_metrics_history" ("Date" TEXT, "BTC_Price" REAL, "Silver_Price" REAL, '
                 '"Gold_Price" REAL, "Silver_BTC_Ratio" REAL, "Gold_BTC_Ratio" REAL)')
    import datetime as dt
    for i in range(40):
        d = (dt.date(2026, 9, 29) - dt.timedelta(days=i)).isoformat()
        conn.execute("INSERT INTO crypto_metrics_history VALUES (?,?,?,?,?,?)",
                     (d, 80000 + i * 100, 60 + i * 0.1, 4200 + i, (80000 + i * 100) / (60 + i * 0.1),
                      (80000 + i * 100) / (4200 + i)))
    conn.execute('CREATE TABLE "macro_master_ledger" ("Datetime" TEXT, "VMRI_Score" REAL, "SHFE_Premium" REAL, '
                 '"High_Yield_OAS" REAL, "10Y_Yield" REAL, "Reverse_Repo_BN" REAL)')
    conn.execute("INSERT INTO macro_master_ledger VALUES ('9/24/26 13:59', 172.7, 1.8, 2.73, 5.16, 0.63)")
    conn.commit()
    conn.close()


build_installation()

import config  # noqa: E402  (after env setup)
from core import cme, lake, metrics, render  # noqa: E402
from core.importer import import_history  # noqa: E402


def _conn():
    return lake.connect(config.DB_PATH)


class ResponseStub:
    def __init__(self, status, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}


class T01PreserveInstallation(unittest.TestCase):
    def test_import_twice_is_idempotent_and_preserves_tables(self):
        r1 = import_history(verbose=False)
        conn = _conn()
        obs1 = conn.execute("SELECT COUNT(*) FROM v2_observations").fetchone()[0]
        legacy_before = conn.execute("SELECT * FROM comex_inventory_history").fetchall()
        conn.close()
        r2 = import_history(verbose=False)
        conn = _conn()
        obs2 = conn.execute("SELECT COUNT(*) FROM v2_observations").fetchone()[0]
        legacy_after = conn.execute("SELECT * FROM comex_inventory_history").fetchall()
        self.assertEqual(obs1, obs2, "re-import duplicated observations")
        self.assertEqual([tuple(r) for r in legacy_before], [tuple(r) for r in legacy_after])
        self.assertIn("Legacy_Extra", lake.table_columns(conn, "comex_inventory_history"))
        self.assertEqual(r2["files"]["volume"], 0)
        # embedded dates win; the download-dated duplicate is flagged, not silently renamed
        conflicts = {c["path"]: c["conflicts"] for c in r1["conflicts"]}
        self.assertTrue(any(x["type"] == "filename_date_differs" for x in conflicts["daily_volume_20260819.xlsx"]))
        self.assertTrue(any(x["type"] == "duplicate_content" for x in conflicts["daily_volume_20260820.xlsx"]))
        # credentials file never captured
        self.assertIsNone(conn.execute("SELECT 1 FROM v2_payloads WHERE origin_path LIKE '%/state.json'").fetchone())
        self.assertTrue(any(n["path"] == "state.json" for n in r1["not_imported"]))
        conn.close()

    def test_credentials_are_never_stored(self):
        detail = "HTTPError: 502 for url: https://api.stlouisfed.org/fred/series/observations?series_id=X&api_key=abc123SECRET&file_type=json"
        self.assertNotIn("abc123SECRET", lake.redact(detail))
        conn = _conn()
        lake.migrate(conn)
        pid = lake.store_payload(conn, source="t", kind="t", content=f"<x>{detail}</x>", fmt="xml")
        _, body = lake.load_payload(conn, pid)
        self.assertNotIn(b"abc123SECRET", body)
        lake.log_fetch(conn, run_id="t", source="t", request={}, started_at="x", elapsed_ms=1, outcome="error", detail=detail)
        self.assertIsNone(conn.execute("SELECT 1 FROM v2_fetch_log WHERE detail LIKE '%abc123SECRET%'").fetchone())
        conn.close()

    def test_table_replacement_is_refused(self):
        from core.sqlite_layer import replace_df
        import pandas as pd
        with self.assertRaises(RuntimeError):
            replace_df("crypto_metrics_history", pd.DataFrame([{"Date": "x"}]))


class T02SourceFailures(unittest.TestCase):
    def test_cme_403_stops_within_attempt_limit(self):
        calls = []

        def get(url):
            calls.append(url)
            return ResponseStub(403, b"denied")
        r = cme.fetch_bounded("https://example/cme.xlsx", validate=cme.parse_volume, max_attempts=5, http_get=get)
        self.assertEqual(r.outcome, "access_denied")
        self.assertEqual(len(calls), 1)

    def test_429_is_bounded(self):
        calls, sleeps = [], []
        r = cme.fetch_bounded("u", validate=cme.parse_volume, max_attempts=2, cooldown=20,
                              http_get=lambda u: calls.append(u) or ResponseStub(429, headers={"Retry-After": "999"}),
                              sleep=sleeps.append)
        self.assertEqual(r.outcome, "rate_limited")
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [20])

    def test_html_login_page_rejected_and_archive_kept(self):
        page = b"<!DOCTYPE html><html><body>Please log in</body></html>"
        r = cme.fetch_bounded("u", validate=cme.parse_inventory, http_get=lambda u: ResponseStub(200, page))
        self.assertEqual(r.outcome, "rejected")
        from core.collect import _archive_name
        old = (FIX / "silver_stocks_2026-09-24.xls").read_bytes()
        target, is_new = _archive_name(DATA, "silver_stocks", "2026-09-24", ".xls", page)
        self.assertNotEqual(target.name, "silver_stocks_2026-09-24.xls")   # never overwrites the valid file
        self.assertEqual((DATA / "silver_stocks_2026-09-24.xls").read_bytes(), old)

    def test_calendar_failure_is_not_no_events(self):
        ctx = {"calendar": {"status": "missing", "reason": "forexfactory: http_429", "upcoming": []}}
        text = render.calendar_text(ctx)
        self.assertIn("unavailable", text.lower())
        self.assertNotIn("No High/Medium impact", text)


class T03TrustworthyValues(unittest.TestCase):
    def test_inventory_millions(self):
        self.assertEqual(metrics.fmt_moz(99207707.60), "99.21M oz")
        rec = lake.obs("comex.silver.registered", "2026-09-23", value=99207707.60, unit="troy_oz", source="t")
        self.assertEqual(rec["value_num"], 99207707.60)

    def test_inventory_parser_uses_headers_and_report_date(self):
        p = cme.parse_inventory((FIX / "silver_stocks_2026-09-24.xls").read_bytes())
        self.assertEqual(p["report_date"], "2026-09-23")       # file name says 09-24
        self.assertTrue(all(c["ok"] for c in p["checks"]))
        s = cme.inventory_summary(p)
        self.assertAlmostEqual(s["registered"], 95494503.432, places=2)
        self.assertAlmostEqual(s["reg_adjustment"], -2399527.002, places=2)

    def test_unknown_oi_stays_unknown(self):
        import pandas as pd
        calls = pd.DataFrame({"strike": [100.0, 105.0], "openInterest": [float("nan")] * 2,
                              "impliedVolatility": [0.2, 0.2], "volume": [float("nan"), 5.0],
                              "contractSymbol": ["C100", "C105"], "lastPrice": [1, 1]})
        puts = calls.assign(contractSymbol=["P100", "P105"])
        mp = metrics.max_pain({"calls": calls, "puts": puts})
        self.assertIsNone(mp["strike"])
        self.assertIn("open interest unavailable", mp["reason"])
        g = metrics.gex_profile(102.0, {"2026-10-16": {"calls": calls, "puts": puts}}, ["2026-10-16"], "2026-09-29")
        self.assertEqual(g["status"], "missing")
        top, cov = metrics.top_contracts({"2026-10-16": {"calls": calls, "puts": puts}})
        self.assertNotIn("top_oi_call", top)
        self.assertEqual(top["top_vol_call"]["contract"], "C105")

    def test_databento_side_semantics(self):
        import pandas as pd
        idx = pd.to_datetime(["2026-09-25T14:00Z", "2026-09-25T14:01Z", "2026-09-25T14:02Z"])
        df = pd.DataFrame({"price": [10.0, 10.0, 10.0], "size": [20000, 15000, 12000], "side": ["A", "B", "N"]},
                          index=idx)
        self.assertEqual(metrics.normalize_side("A"), "sell")
        self.assertEqual(metrics.normalize_side("B"), "buy")
        self.assertEqual(metrics.normalize_side("N"), "unknown")
        f = metrics.block_flow(df)
        self.assertEqual(f["sell_aggressor_volume"], 20000)
        self.assertEqual(f["buy_aggressor_volume"], 15000)
        self.assertEqual(f["unknown_side_volume"], 12000)
        self.assertEqual(f["aggressor_bias"], "BEARISH")   # sell 20,000 > buy 15,000 x 1.2
    test_databento_side_semantics.__doc__ = "Ask maps to sell aggressor, Bid to buy aggressor, N stays unknown"

    def test_unfinished_bar_does_not_become_price(self):
        import numpy as np
        import pandas as pd
        idx = pd.date_range("2025-01-01", periods=300, freq="B")
        d = pd.DataFrame({"Close": np.linspace(100, 200, 300), "Volume": 1e6}, index=idx)
        d.iloc[-1, 0] = np.nan                       # Yahoo's unfinished current-day row
        t = metrics.technicals(d, d, d)
        self.assertAlmostEqual(t["current_price"], d["Close"].iloc[-2])
        self.assertEqual(t["alignment"], "FULL ALIGNMENT (BULLISH)")
        e = metrics.execution_engine(vix=15, breadth_cond="BROAD SELLOFF", flow_bias="BEARISH", alignment="FULL ALIGNMENT (BEARISH)",
                                     price=float("nan"), call_wall=1.0, put_wall=1.0, max_pain_strike=1.0, nodes=[])
        self.assertIsNone(e["target_strike"])        # NaN price is missing, never a strike

    def test_zero_gamma_never_spot_placeholder(self):
        import pandas as pd
        calls = pd.DataFrame({"strike": [100.0], "openInterest": [10.0], "impliedVolatility": [0.3]})
        g = metrics.gex_profile(100.0, {"2026-10-16": {"calls": calls, "puts": calls.iloc[0:0]}}, ["2026-10-16"],
                                "2026-09-29")
        self.assertIsNone(g["zero_gamma"])          # all positive: no sign change -> null with a reason
        self.assertIsNotNone(g["zero_gamma_reason"])


class T04Outputs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import_history(verbose=False)
        import main_pipeline
        cls.mp = main_pipeline
        cls.rc = main_pipeline.run(offline=True, deliver=False)

    def _latest(self):
        conn = _conn()
        r = dict(conn.execute("SELECT * FROM v2_runs WHERE mode='offline' ORDER BY started_at DESC LIMIT 1").fetchone())
        conn.close()
        return r

    def test_offline_run_makes_zero_provider_calls_and_saves_everything(self):
        import json
        self.assertEqual(self.rc, 0)
        r = self._latest()
        self.assertEqual(json.loads(r["request_counts_json"])["calls"], {})
        out = Path(json.loads(r["artifacts_json"])["out_dir"])
        for name in ("tactical_ruling.txt", "volume_dashboard.txt", "volume_dashboard.html", "master_market_data.csv",
                     "daily_market_report.txt", "report_snapshot.json", "email.eml", "run_manifest.json"):
            self.assertTrue((out / name).exists(), name)
        import xml.etree.ElementTree as ET
        tac = ET.parse(out / "tactical_ruling.txt").getroot()
        tags = [c.tag for c in tac]
        for legacy in ("macro_kill_switches", "VLAD_MACRO_RISK_INDEX", "upcoming_macro_events", "comex_default_risk"):
            self.assertIn(legacy, tags)
        self.assertEqual(tags[-1], "model_features")          # extended data is appended, legacy paths unchanged
        self.assertIsNotNone(tac.find("model_features/data_quality"))
        html_text = (out / "volume_dashboard.html").read_text()
        self.assertLess(html_text.index('id="report-text"'), html_text.index("COMEX Physical Silver Inventory</h2>"))
        root = ET.parse(out / "volume_dashboard.txt").getroot()
        self.assertEqual(root.tag, "dashboard")
        self.assertIsNotNone(root.find("sp500"))
        self.assertIsNotNone(root.find("tactical_ruling"))

    def test_saved_report_equals_email_body_and_replay_is_identical(self):
        import json
        from send_email import load_eml, plain_body
        r = self._latest()
        out = Path(json.loads(r["artifacts_json"])["out_dir"])
        report = (out / "daily_market_report.txt").read_text()
        self.assertEqual(plain_body(load_eml(out / "email.eml")).rstrip("\n"), report.rstrip("\n"))
        replay_dir = TMP / "replay"
        with mock.patch("core.sources.SourceSession.fetch", side_effect=AssertionError("provider call in replay")):
            self.mp.replay(r["run_id"], out_dir=replay_dir)
        self.assertEqual((replay_dir / "daily_market_report.txt").read_text(), report)

    def test_missing_chart_inputs_are_explained_and_not_attached(self):
        import json
        r = self._latest()
        charts = {c["file"]: c for c in json.loads(r["artifacts_json"])["charts"]}
        self.assertEqual(charts["chart7_crypto_ratios_30d.png"]["status"], "generated")
        self.assertIn(charts["chart1_es_conviction_7d.png"]["status"], ("stale_input", "generated"))
        self.assertEqual(charts["chart6_comex_inventory_30d.png"]["status"], "stale_input")
        missing = [c for c in charts.values() if c["status"] == "missing_input"]
        for c in missing:
            self.assertTrue(c["reason"])
        from send_email import load_eml
        out = Path(json.loads(r["artifacts_json"])["out_dir"])
        msg = load_eml(out / "email.eml")
        attached = {p.get_filename() for p in msg.walk() if p.get_content_type() == "image/png"}
        ok = {c["file"] for c in charts.values() if c["status"] in ("generated", "stale_input")}
        self.assertEqual(attached, ok)

    def test_overlapping_runs_one_owner(self):
        lock = self.mp.RunLock()
        self.assertTrue(lock.acquire())
        try:
            self.assertEqual(self.mp.run(offline=True, deliver=False), self.mp.EXIT_BUSY)
        finally:
            lock.release()

    def test_email_failure_keeps_saved_message_and_ambiguous_send_not_retried(self):
        import json
        import send_email
        r = self._latest()
        eml = Path(json.loads(r["artifacts_json"])["out_dir"]) / "email.eml"
        status, _ = send_email.deliver(eml)          # no credentials configured
        self.assertEqual(status, "failed")
        self.assertTrue(eml.exists())

        class FlakySMTP:
            def __init__(self, *a, **k): pass
            def ehlo(self): pass
            def starttls(self): pass
            def login(self, *a): pass
            def send_message(self, m): raise ConnectionResetError("dropped after DATA")
        with mock.patch.object(send_email, "EMAIL_SENDER", "a@b"), mock.patch.object(send_email, "EMAIL_PASSWORD", "x"), \
                mock.patch("smtplib.SMTP", FlakySMTP):
            status, detail = send_email.deliver(eml)
        self.assertEqual(status, "outcome_unknown")
        self.assertIn("not retried", detail)


class T06ForecastLab(unittest.TestCase):
    def _chain(self, spot=100.0, iv=0.20, dte=30):
        import math
        import pandas as pd
        from core import forecast as F
        T = dte / 365
        rows = {"calls": [], "puts": []}
        for k in [spot * (0.7 + 0.01 * i) for i in range(61)]:
            for side, call in (("calls", True), ("puts", False)):
                p = F._bs(spot, k, T, iv, call)
                rows[side].append({"strike": k, "bid": p * 0.995, "ask": p * 1.005, "lastPrice": p, "impliedVolatility": iv})
        return {k: pd.DataFrame(v) for k, v in rows.items()}

    def test_implied_distribution_recovers_lognormal(self):
        import math
        from datetime import date, timedelta
        from core import forecast as F
        today = date(2026, 1, 5)
        exp = (today + timedelta(days=30)).isoformat()
        out = F.implied_distribution({exp: self._chain()}, 100.0, today)
        h = out["horizons"]["1m"]
        self.assertEqual(h["basis"], "smile")
        self.assertAlmostEqual(h["atm_iv"], 0.20, delta=0.01)
        sd = 0.20 * math.sqrt(30 / 365)
        self.assertAlmostEqual(h["range68"][0], 100 * math.exp(0.05 * 30 / 365 - sd * sd / 2 - sd), delta=0.6)
        self.assertAlmostEqual(h["range68"][1], 100 * math.exp(0.05 * 30 / 365 - sd * sd / 2 + sd), delta=0.6)
        self.assertTrue(0.45 < h["p_up"] < 0.55)

    def test_recession_probit_matches_ny_fed_formula(self):
        from scipy.stats import norm
        from core import forecast as F
        obs = [{"date": f"2026-09-{d:02d}", "value": 1.0} for d in range(28, 0, -1)]
        m = F.macro_regime({"T10Y3M": obs})
        self.assertAlmostEqual(m["recession_prob_12m"], norm.cdf(-0.5333 - 0.6330 * 1.0), places=6)

    def test_trend_flip_level_is_the_rolling_anchor(self):
        import numpy as np
        import pandas as pd
        from core import forecast as F
        idx = pd.bdate_range("2023-01-02", periods=400)
        c = pd.Series(np.linspace(100, 140, 400), index=idx)
        t = F.trend_model(pd.DataFrame({"Close": c}))
        self.assertEqual(t["direction"], "LONG")
        self.assertAlmostEqual(t["signals"]["1m"]["flip_level"], float(c.iloc[-21]))

    def test_signal_watch_fires_and_gates_on_evidence(self):
        from core import forecast as F
        fv = {"residual_z": -1.5, "price": 50.0, "fair_value": 58.0, "backtest": {"hit_rate": 0.61, "n": 49}}
        trend = {"signals": {"1m": {"signal": "LONG", "flip_level": 44.0, "flip_distance_pct": -2.0}},
                 "backtest": {"1m": {"up_rate_when_long": 0.56, "up_rate_when_short": 0.80, "effective_n": 46}}}
        rows = {r["name"]: r for r in F.signal_watch({"silver_fair_value": fv, "SLV": {"spot": 45.0, "trend": trend}})}
        fair, mom = rows["Fair-value reversion"], rows["1-month momentum"]
        self.assertTrue(fair["fired"] and fair["bias"] == "bullish" and fair["action"].startswith("Buy SLV calls"))
        self.assertTrue(mom["fired"] and mom["edge"] == "none" and mom["action"].startswith("No trade"))   # fired, but no edge
        fv["residual_z"] = 0.2
        base = {"silver_fair_value": fv, "SLV": {"spot": 45.0}}
        self.assertEqual(F.signal_watch(base)[0]["action"], "Wait")
        live = F.live_overlay(base, {"SI_F": 20.0})          # live silver far below fair value: the waiting row fires
        self.assertTrue(F.signal_watch(live)[0]["fired"] and F.signal_watch(base)[0]["fired"] is False)

    def test_live_watch_dip_rule_and_spread(self):
        import numpy as np
        import pandas as pd
        from datetime import date, datetime
        from core import watch as W
        rng = np.random.default_rng(7)
        closes = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0006, 0.01, 700))), index=pd.bdate_range("2023-01-02", periods=700))
        row = W.dip_signal(closes, float(closes.iloc[-1]))
        level = row["trigger_price"]                              # RSI(2) is exactly 10 at the stated trigger price
        self.assertAlmostEqual(W.dip_signal(closes, level)["rsi2"], 10.0, places=6)
        below = W.dip_signal(closes, level - 0.01)
        self.assertEqual(below["fired"], level - 0.01 > closes.iloc[-199:].mean())      # fires only above the 200-day
        self.assertFalse(W.dip_signal(closes, level - 0.01, in_window=False)["fired"])    # and only in the entry window
        self.assertFalse(W.dip_signal(closes, level - 0.01, has_open=True)["fired"])      # and never while one is open
        self.assertFalse(W.dip_signal(closes, level - 0.01, blocked="earnings")["fired"])  # or when blocked (earnings)
        deep = W.dip_signal(closes, level - 0.01, symbol="GOOGL")                         # GOOGL needs RSI(2) < 5 and trades shares
        self.assertTrue(deep["trigger_price"] < level and deep["plan"].startswith("Buy GOOGL shares"))
        ctx = W.context_rows(closes, float(closes.iloc[-19:].max()) + 1)                  # above the 20-day high: context only
        hi = next(r for r in ctx if r["name"] == "New 20-day closing high")
        self.assertTrue(hi["fired"] and not hi["action"].startswith("Buy") and len(ctx) == 4)
        wide = pd.DataFrame({"strike": [97.0, 98.0, 99.0, 101.0, 102.0, 103.0], "bid": [3.9, 3.1, 2.3, 1.0, 0.6, 0.3],
                             "ask": [4.1, 3.3, 2.5, 1.2, 0.8, 0.5], "lastPrice": [4, 3.2, 2.4, 1.1, 0.7, 0.4],
                             "contractSymbol": ["C97", "C98", "C99", "C101", "C102", "C103"]})
        self.assertEqual(W.pick_spread(wide, 100.0, max_debit=1.5)["long_contract"], "C99")  # 99/101 = 1.30 fits the budget
        # an added ticker only says Buy when its own history clears the gate; pure noise must not
        noise = W.dip_signal(closes, level - 0.01, symbol="NOISE")
        self.assertTrue(noise["edge"] == "none" and not noise["action"].startswith("Buy"))
        self.assertEqual(W.session_day(datetime(2026, 10, 6, 15, 59, tzinfo=W.NEW_YORK)), date(2026, 10, 6))
        self.assertEqual(W.session_day(datetime(2026, 10, 6, 16, 0, tzinfo=W.NEW_YORK)), date(2026, 10, 7))   # after the close
        with mock.patch.object(W, "WATCHLIST", Path(tempfile.mkdtemp()) / "wl.json"):
            self.assertEqual(W.watchlist(), ["SPY", "GOOGL", "SLV"])
            self.assertEqual(W.add_symbol("drop table;"), "not a valid ticker")
            self.assertIn("already", W.add_symbol("googl"))
            W._save({**W._extras(), "GILD": {"earnings": True}})
            W.remove_symbol("slv")
            self.assertEqual(W.watchlist(), ["SPY", "GOOGL", "GILD"])
        calls = pd.DataFrame({"strike": [98.0, 99.0, 100.0, 101.0, 102.0], "bid": [2.9, 2.2, 1.6, 1.1, 0.7],
                              "ask": [3.1, 2.4, 1.8, 1.3, 0.9], "lastPrice": [3, 2.3, 1.7, 1.2, 0.8],
                              "contractSymbol": ["C98", "C99", "C100", "C101", "C102"]})
        sp = W.pick_spread(calls, 100.0)
        self.assertEqual((sp["long_contract"], sp["short_contract"]), ("C99", "C101"))
        self.assertAlmostEqual(sp["entry_debit"], 1.1)
        self.assertEqual(W.exit_date(date(2026, 10, 6)), date(2026, 10, 13))             # 5 trading days, over a weekend
        self.assertEqual(W.pick_expiry(["2026-10-09", "2026-10-30", "2026-12-18"], date(2026, 10, 6)), "2026-10-30")

    def test_scorecard_grades_after_target_date(self):
        import sqlite3
        import pandas as pd
        from core import forecast as F
        from core import lake as L
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        L.migrate(c)
        c.execute("INSERT INTO v2_forecasts (run_id, created_at, symbol, model, horizon, target_date, spot, p_up, lower68, upper68, direction) "
                  "VALUES ('r', '2026-01-02', 'SPY', 'implied', '1d', '2026-01-05', 100, 0.6, 99, 101, 'UP')")
        closes = pd.Series([100.0, 100.5], index=pd.to_datetime(["2026-01-02", "2026-01-05"]))
        sc = F.scorecard(c, lambda s: closes, "SPY")
        g = sc["graded"][0]
        self.assertEqual((g["n"], g["hit_rate"], g["coverage68"]), (1, 1.0, 1.0))
        self.assertAlmostEqual(g["brier"], 0.16)


class T07Refining(unittest.TestCase):
    def test_crack_spread_math(self):
        import pandas as pd
        from core import refining as RF
        idx = pd.bdate_range("2024-01-01", periods=300)
        ho, rb, cl = (pd.Series(v, index=idx) for v in (3.0, 2.5, 80.0))
        m = RF.crack_spreads(ho, rb, cl)
        self.assertAlmostEqual(m["diesel"]["latest"], 3.0 * 42 - 80)
        self.assertAlmostEqual(m["gasoline"]["latest"], 2.5 * 42 - 80)
        self.assertAlmostEqual(m["three_two_one"]["latest"], (2 * 2.5 * 42 + 3.0 * 42 - 3 * 80) / 3)

    def test_event_classification_and_units(self):
        from core import refining as RF
        self.assertEqual(RF.classify_event("EMISSIONS EVENT"), "unplanned")
        self.assertEqual(RF.classify_event("SCHEDULED MAINTENANCE"), "planned_maintenance")
        self.assertEqual(RF.classify_event("STARTUP"), "startup_shutdown")
        units, w = RF.classify_units("Complex 1|FCCU|F-50", "")
        self.assertIn("FCC (gasoline & light cycle oil)", units)
        self.assertEqual(w, 3)
        self.assertAlmostEqual(RF._duration_hours("1 days, 2 hours, 30 minutes"), 26.5)

    def test_refinery_matching(self):
        import pandas as pd
        from core import refining as RF
        cap = pd.DataFrame([
            {"COMPANY_NAME": "DIAMOND SHAMROCK REFINING CO LP", "SITE": "SUNRAY", "STATE_NAME": "Texas", "PADD": 3, "capacity_bpd": 195000.0},
            {"COMPANY_NAME": "MOTIVA ENTERPRISES LLC", "SITE": "PORT ARTHUR", "STATE_NAME": "Texas", "PADD": 3, "capacity_bpd": 656400.0},
            {"COMPANY_NAME": "PREMCOR REFINING GROUP INC", "SITE": "PORT ARTHUR", "STATE_NAME": "Texas", "PADD": 3, "capacity_bpd": 385000.0},
            {"COMPANY_NAME": "DEER PARK REFINING LP", "SITE": "DEER PARK", "STATE_NAME": "Texas", "PADD": 3, "capacity_bpd": 312500.0}])
        r, _ = RF.match_refinery("VALERO MCKEE REFINERY", "6701 FM 119; SUNRAY, TX 79086", cap)
        self.assertEqual(r["SITE"], "SUNRAY")
        r, _ = RF.match_refinery("VALERO PORT ARTHUR REFINERY", "1801 GULFWAY DR; PORT ARTHUR, TX 77640", cap)
        self.assertEqual(r["COMPANY_NAME"], "PREMCOR REFINING GROUP INC")
        self.assertFalse(RF.looks_like_refinery("LUBRIZOL DEER PARK"))
        ev = RF.enrich_event({"event_id": "1", "name": "MOTIVA PORT ARTHUR REFINERY", "location": "X; PORT ARTHUR, TX 77640",
                              "event_type": "EMISSIONS EVENT", "units": "Crude Unit 1", "duration": "30 hours",
                              "start": "09/20/2026 01:00 PM"}, cap)
        self.assertTrue(ev["major"])
        self.assertEqual(ev["refinery"]["capacity_bpd"], 656400.0)

    def test_maintenance_outlook_finds_seasonal_dip(self):
        import numpy as np
        import pandas as pd
        from core import refining as RF
        idx = pd.date_range("2016-01-01", "2026-09-18", freq="W-FRI")
        wk = idx.isocalendar().week.values.astype(float)
        u = pd.Series(92 - 4 * np.exp(-((wk - 42) ** 2) / 8), index=idx)       # fall dip centered on ISO week 42
        mt = RF.maintenance_outlook(u)
        self.assertEqual(mt["status"], "fresh")
        self.assertTrue(40 <= mt["expected_trough"]["iso_week"] <= 44)

    def test_inventory_history_days_of_supply_and_alignment(self):
        import pandas as pd
        from core import refining as RF
        idx = pd.date_range("2000-01-07", "2012-12-28", freq="W-FRI")
        late = idx[idx >= "2005-01-01"]                                         # demand series starts later than stocks
        series = {"dist_stocks": pd.Series(120000.0, index=idx), "dist_supplied": pd.Series(4000.0, index=late),
                  "spr_stocks": pd.Series(700000.0, index=idx)}
        h = RF.inventory_history(series)
        self.assertEqual((h["status"], h["start"], h["as_of"], len(h["dates"])), ("fresh", "2000-01-07", "2012-12-28", len(idx)))
        dist, spr = (next(x for x in h["series"] if x["key"] == k) for k in ("dist_stocks", "spr_stocks"))
        i = h["dates"].index("2008-06-06")
        self.assertEqual(dist["days_of_supply"][i], 30.0)                       # 120,000 kbbl / 4,000 kb/d
        self.assertIsNone(dist["days_of_supply"][0])                            # before demand data: unknown, not 0
        self.assertAlmostEqual(dist["vs_5y_pct"][i], 0.0)
        self.assertIsNone(dist["vs_5y_pct"][52])                                # fewer than 3 prior years: unknown
        self.assertNotIn("days_of_supply", spr)                                 # no matching demand series
        self.assertEqual(RF.inventory_history({})["status"], "missing")

    def test_seasonal_average_fast_path_matches_reference(self):
        import numpy as np
        import pandas as pd
        from core import refining as RF
        idx = pd.date_range("2010-01-01", "2020-12-25", freq="W-FRI")
        s = pd.Series(100 + 10 * np.sin(np.arange(len(idx)) / 8.0) + np.arange(len(idx)) * 0.01, index=idx)
        fast = RF._seasonal_avg_series(s)
        for d in list(idx[:3]) + list(idx[150:160]) + list(idx[-60:]):         # includes ISO week 52/53 and 1
            slow = RF._seasonal_avg(s[s.index < d - pd.Timedelta(days=180)], d)
            if slow is None:
                self.assertTrue(pd.isna(fast[d]))
            else:
                self.assertAlmostEqual(fast[d], slow)

    def test_truncated_eia_capture_is_not_reused(self):
        import json
        from core import sources as src
        short = json.dumps({"response": {"total": "2296", "data": [{"period": "2026-09-25", "value": 1}]}}).encode()
        full = json.dumps({"response": {"total": "1", "data": [{"period": "2026-09-25", "value": 1}]}}).encode()
        self.assertTrue(src._eia_truncated(short))
        self.assertFalse(src._eia_truncated(full))
        self.assertFalse(src._eia_truncated(b"\xd0\xcf\x11\xe0 xls history file"))


class T10Assistant(unittest.TestCase):
    def _app(self):
        from flask import Flask, jsonify, request
        app = Flask(__name__)
        hits = []

        @app.route("/api/gex")
        def gex():
            hits.append(("GET", request.path, dict(request.args)))
            return jsonify({"data": {"spot": 769.43, "callWall": 775.0, "strikes": list(range(700, 801)), "gamma": list(range(101))}})

        @app.route("/run")
        def run():
            hits.append(("RUN", request.path, {}))
            return "started"
        return app, hits

    def test_only_listed_sources_and_parameters_reach_the_app(self):
        sys.path.insert(0, str(ROOT / "options_whale"))
        import assistant as A
        app, hits = self._app()
        self.assertIn("must be one of", A.fetch_source(app, "run"))
        self.assertIn("must be one of", A.fetch_source(app, "../run"))
        self.assertEqual(hits, [])                                              # nothing outside the list is called
        text = A.fetch_source(app, "gex", "ticker=SPY&evil=1&limit=5")
        self.assertEqual(hits, [("GET", "/api/gex", {"ticker": "SPY"})])        # unlisted parameters are dropped
        self.assertIn("769.43", text)
        self.assertIn("showing the middle 10 of 101", text)                     # strikes are cut around the centre

    def test_every_source_is_a_get_route_without_side_effects(self):
        sys.path.insert(0, str(ROOT / "options_whale"))
        import assistant as A
        import re
        src = (ROOT / "options_whale" / "api_router.py").read_text()
        for name, (route, _params, _desc) in A.SOURCES.items():
            m = re.search(r"@app\.route\('" + re.escape(route) + r"'(?:, methods=\[([^\]]*)\])?\)", src)
            self.assertIsNotNone(m, f"{name}: {route} is not a route")
            self.assertIn("GET", m.group(1) or "'GET'", f"{name}: {route} does not accept GET")
        for forbidden in ("/run", "/api/silver_eagle_prices"):
            self.assertNotIn(forbidden, [r for r, _p, _d in A.SOURCES.values()])  # a pipeline run and a 2-minute web scrape
        self.assertNotIn("claude", (ROOT / "options_whale" / "assistant.py").read_text().lower())   # one engine, on this machine

    def test_shape_outlines_large_data_and_opens_paths(self):
        sys.path.insert(0, str(ROOT / "options_whale"))
        import assistant as A
        import json
        big = {"a": {"rows": [{"i": i, "pad": "x" * 200} for i in range(400)]}, "b": {"deep": {"value": 3.14159265}}}
        big.update({f"part{n}": {"rows": [{"i": i, "pad": "y" * 200} for i in range(40)]} for n in range(12)})
        out = A.shape(big, max_chars=1500)
        self.assertLessEqual(len(out), 1500)
        self.assertIn("outline", out)
        self.assertEqual(A.shape(big, "b.deep.value"), "3.1416")
        self.assertEqual(json.loads(A.shape({"h": list(range(50))}, "h", last=3))[1:], [47, 48, 49])
        self.assertEqual(json.loads(A.shape({"h": list(range(50))}, "h", last=3, keep="first"))[1:], [0, 1, 2])
        with self.assertRaises(KeyError):
            A.shape(big, "a.nope")

    def test_v3_shaping_and_forgiving_calls(self):
        sys.path.insert(0, str(ROOT / "options_whale"))
        import assistant as A
        import json
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        rows = [{"created_at": now, "position_type": "CALL", "underlying": "SPY", "contract": "SPY261023C00779000", "strike": 779.0,
                 "expiration": "2026-10-23", "entry_bid": 0, "entry_ask": 0, "entry_mid": 6.0, "score": 4, "horizons": {}},
                {"created_at": "2026-10-01T13:31:00+00:00", "position_type": "CASH", "score": 1, "horizons": {}}]
        s = A._shape_positions({"data": rows}, {})
        self.assertEqual((s["counts"]["calls"], s["counts"]["cash"], len(s["trades"])), (1, 1, 1))
        self.assertEqual(s["today"][0]["position"], "SPY Oct 23 2026 $779 call")
        self.assertIn("not live", s["today"][0]["note"])                                   # zero bid/ask at entry is flagged
        g = A._shape_gex({"data": {"spot": 770.0, "callWall": 775.0, "putWall": 760.0, "zeroGamma": 765.0, "strikes": [760, 770], "gamma": [1, 2]}}, {})
        self.assertEqual(g["spot_minus_level"], {"call_wall": -5.0, "put_wall": 10.0, "zero_gamma": 5.0})   # worked out, not left to the model
        app, hits = self._app()
        self.assertEqual(A.fetch_source(app, "gex.spot", "ticker=SPY"), "769.43")            # source.path is accepted
        self.assertIn("must be one of", A.fetch_source(app, "", "", "spot"))                # a missing source says how to fix the call

    def test_v3_empty_reply_reloads_once_and_thinking_switch(self):
        sys.path.insert(0, str(ROOT / "options_whale"))
        import assistant as A
        import json
        sent, reloads = [], []

        class Stream:
            def __init__(self, chunks): self.status_code, self.chunks = 200, chunks
            def iter_lines(self): return iter(self.chunks)
            def close(self): pass
        replies = [[], [b'data: {"choices":[{"delta":{"content":"Done."}}]}', b"data: [DONE]"]]
        def post(url, json=None, **kw):
            sent.append(json)
            return Stream(replies.pop(0))
        app, _hits = self._app()
        with mock.patch.object(A.requests, "post", side_effect=post), mock.patch.object(A, "_ollama", lambda *a, **k: reloads.append(a)), \
             mock.patch.object(A, "brief", lambda app: "BRIEF TEXT"):
            events = list(A.run_local(app, {"turns": []}, "hi", "m", "auto"))
        self.assertEqual(len(reloads), 1)                                                   # dead model: reloaded once, asked again
        self.assertEqual([e["type"] for e in events][-1], "done")
        self.assertIn({"type": "delta", "text": "Done."}, events)
        self.assertEqual(sent[0]["chat_template_kwargs"], {"enable_thinking": False})        # auto: no thinking on the first call
        self.assertNotIn("now", sent[0]["messages"][1]["content"].lower())                  # the clock is not in the cached prefix
        self.assertIn("(Asked ", sent[0]["messages"][-1]["content"])

    def test_calc_is_arithmetic_only(self):
        sys.path.insert(0, str(ROOT / "options_whale"))
        import assistant as A
        self.assertEqual(A.calc("(775-769.43)/769.43*100"), "0.723913")
        self.assertEqual(A.calc("round(sqrt(16) + 2**3, 1)"), "12.0")
        for bad in ("__import__('os').system('id')", "open('/etc/passwd')", "a + 1", "(1).real", "9**999"):
            self.assertTrue(A.calc(bad).startswith("error"), bad)


class T05OfflineImports(unittest.TestCase):
    def test_no_credential_dependent_imports(self):
        env = dict(os.environ, DATABENTO_API_KEY="", DB_API_KEY="", PYTHONPATH=str(ROOT))
        code = ("import config, main_pipeline, visualize_volume, tactical_ruling, market_reader, dump_data, "
                "institutional_scanner, parse_volume, send_email, core.collect, core.importer; print('ok')")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])


class T08Schedule(unittest.TestCase):
    def test_nyse_holidays_and_session_window(self):
        from datetime import date, datetime
        from core.market_calendar import NEW_YORK, nyse_holidays, scheduled_run_skip_reason
        self.assertEqual(sorted(nyse_holidays(2026)), [
            date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
            date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)])
        at = lambda s: datetime.fromisoformat(s).replace(tzinfo=NEW_YORK)
        self.assertIsNone(scheduled_run_skip_reason(at("2026-10-06T09:31")))
        self.assertIsNone(scheduled_run_skip_reason(at("2026-10-06T15:45")))
        self.assertIn("outside the regular session", scheduled_run_skip_reason(at("2026-10-06T19:00")))
        self.assertIn("not an NYSE trading day", scheduled_run_skip_reason(at("2026-10-03T09:31")))
        self.assertIn("not an NYSE trading day", scheduled_run_skip_reason(at("2026-11-26T09:31")))

    def test_scheduled_run_needs_opt_in(self):
        import main_pipeline
        with mock.patch.object(config, "SCHEDULED_RUNS", False), \
                mock.patch.object(main_pipeline, "RunLock") as lock:
            self.assertEqual(main_pipeline.run(trigger="scheduled", offline=True), 0)
            lock.assert_not_called()


class T09EdgeLab(unittest.TestCase):
    @staticmethod
    def walk(n, seed=1, drift=0.0, vol=0.01, end="2026-10-05"):
        import numpy as np, pandas as pd
        idx = pd.bdate_range(end=end, periods=n)
        return pd.Series(100 * np.cumprod(1 + np.random.default_rng(seed).normal(drift, vol, n)), index=idx)

    @staticmethod
    def bars(c, seed=2):
        import numpy as np, pandas as pd
        o = c.shift(1).fillna(c.iloc[0]) * (1 + np.random.default_rng(seed).normal(0, 0.003, len(c)))
        return pd.DataFrame({"Open": o, "High": np.maximum(o, c) * 1.002, "Low": np.minimum(o, c) * 0.998, "Close": c})

    def test_grade_rules(self):
        import numpy as np, pandas as pd
        from core import edges
        rng = np.random.default_rng(3)
        base = pd.Series(rng.normal(0, 0.02, 400), index=pd.bdate_range("2015-01-01", periods=400))
        ix = base.index[::10]                                                  # 40 cases spread over both halves
        good = pd.Series(0.03 + rng.normal(0, 0.01, 40), index=ix)
        self.assertEqual(edges._grade(good, base)["edge"], "tested")
        self.assertEqual(edges._grade(good.iloc[:25], base)["edge"], "thin")   # same data, n < 30
        self.assertEqual(edges._grade(pd.Series(rng.normal(0, 0.02, 40), index=ix), base)["edge"], "none")   # pure noise
        flip = pd.Series(np.where(ix < base.index[200], 0.10, -0.005) + rng.normal(0, 0.005, 40), index=ix)
        g = edges._grade(flip, base)                                           # strong first half, worse second half
        self.assertGreaterEqual(g["t"], 2)
        self.assertEqual(g["edge"], "none")
        few = edges._grade(good.iloc[:7], base)
        self.assertIsNone(few["p"])
        self.assertEqual(few["edge"], "none")
        b2 = pd.Series(np.r_[np.abs(base.values[:200]), -np.abs(base.values[:200])][rng.permutation(400)], index=base.index)   # exactly 50% up
        hi_t = pd.Series(np.where(np.arange(40) % 2, -0.001, 0.05), index=ix)  # big mean, 50% up-rate
        g = edges._grade(hi_t, b2)
        self.assertGreaterEqual(g["t"], 2)
        self.assertLess(g["p"] - g["base"], 0.05)
        self.assertEqual(g["edge"], "none")
        self.assertIn(edges._grade(hi_t, b2, min_up=0.0)["edge"], ("tested", "thin"))

    def test_edge_action(self):
        from core import edges
        e = lambda active=True, grade="tested", bias="bullish", note=None: edges._edge(
            "k", "L", "N", "short", "S", "w", "n", "t", active, bias, grade, "ev", "Buy it", note)
        self.assertEqual((e(False)["action"], e(False)["bias"], e(False)["active"]), ("Wait", None, False))
        self.assertEqual(e()["action"], "Buy it · standard size")
        self.assertEqual(e(grade="thin")["action"], "Buy it · half size (small sample)")
        self.assertEqual(e(grade="none")["action"], "Context: on, but it has no measured edge on this ticker")
        self.assertEqual(e(bias="neutral", note="the note")["action"], "the note")
        self.assertEqual(e(grade="none", bias="neutral", note="the note")["action"], "the note")

    def test_verdict_counts_only_tested(self):
        from core import edges
        e = lambda key, name, h, bias, grade, active=True: edges._edge(key, "L", name, h, "S", "w", "n", "t", active, bias, grade, "ev", "plan")
        v = edges._verdict([e("a", "Dip", "short", "bullish", "tested"), e("b", "Noise", "short", "bullish", "none"),
                            e("c", "Off", "short", "bullish", "tested", False), e("d", "Trend", "mid", "bearish", "tested"),
                            e("f", "Ctx", "long", "bullish", "none")])
        self.assertEqual((v["short"]["bias"], v["short"]["score"], v["short"]["label"], v["short"]["edges"]), ("bullish", 1, "Bullish (1 tested)", ["Dip"]))
        self.assertEqual((v["mid"]["bias"], v["mid"]["score"], v["mid"]["label"]), ("bearish", -1, "Bearish (1 tested)"))
        self.assertEqual((v["long"]["bias"], v["long"]["score"], v["long"]["label"], v["long"]["edges"]), ("neutral", 0, "No tested edge on", []))

    def test_trend_and_momentum(self):
        from core import edges
        up, dn = self.walk(600, drift=0.002, vol=0.002), self.walk(600, drift=-0.002, vol=0.002)
        for c, bias, sign in ((up, "bullish", "+"), (dn, "bearish", "-")):
            t, m = edges._trend("X", c, float(c.iloc[-1]) * (1.01 if bias == "bullish" else 0.99)), edges._momentum("X", c)
            self.assertEqual((t["bias"], t["active"], t["horizon"], t["now"][0]), (bias, True, "mid", sign))
            self.assertEqual((m["bias"], m["active"], m["horizon"]), (bias, True, "mid"))
        c = up.copy()
        c.iloc[-15:] *= 0.8                                                    # a sharp last month does not flip 12-to-1-month momentum
        self.assertEqual(edges._momentum("X", c)["bias"], "bullish")
        self.assertEqual(edges._trend("X", c, float(c.iloc[-1]))["bias"], "bearish" if c.iloc[-1] < c.iloc[-200:].mean() else "bullish")

    def test_turn_of_month_window(self):
        from datetime import date
        from core import edges
        for today, on, now in ((date(2026, 10, 30), True, "inside"), (date(2026, 11, 3), True, "inside"), (date(2026, 10, 14), False, "Oct 30")):
            c = self.walk(1100, end=today - __import__("datetime").timedelta(days=1))
            r = edges._turn_of_month("X", c, today)
            self.assertEqual(r["active"], on, today)
            self.assertIn(now, r["now"])
        self.assertEqual(r["bias"], None)

    def test_earnings_drift_reaction_session(self):
        import numpy as np, pandas as pd
        from core import edges
        rng = np.random.default_rng(5)
        n = 250
        def closes(i):
            r = rng.normal(0, 0.002, n)
            r[i] = 0.05
            return pd.Series(100 * np.cumprod(1 + r), index=pd.bdate_range(end="2026-10-05", periods=n))
        e = edges._earnings_drift("X", closes(5), [])
        self.assertEqual((e["edge"], e["active"]), ("n/a", False))
        stamp = lambda c, k, hm: pd.Timestamp(f"{c.index[k]:%Y-%m-%d} {hm}")
        c = closes(n - 6)                                                      # jump on the session 5 before the last bar
        for k, hm in ((-7, "16:05"), (-6, "08:00")):                           # after the close -> next session; before the open -> same
            e = edges._earnings_drift("X", c, [stamp(c, k, hm)])
            self.assertIn("+5.0% reaction, 5 trading days ago", e["now"], hm)
            self.assertTrue(e["active"])
        c = closes(n - 31)
        e = edges._earnings_drift("X", c, [stamp(c, -32, "16:05")])
        self.assertIn("30 trading days ago", e["now"])
        self.assertFalse(e["active"])

    def test_overnight(self):
        import numpy as np, pandas as pd
        from core import edges
        n, rng = 4000, np.random.default_rng(7)
        idx = pd.bdate_range("2005-01-03", periods=n)
        def bars(night_mu):
            night, day = rng.normal(night_mu, 0.003, n), rng.normal(0, 0.005, n)
            o, c, prev = np.zeros(n), np.zeros(n), 100.0
            for i in range(n):
                o[i] = prev * (1 + night[i]); c[i] = o[i] * (1 + day[i]); prev = c[i]
            return pd.DataFrame({"Open": o, "High": np.maximum(o, c), "Low": np.minimum(o, c), "Close": c}, index=idx)
        e = edges._overnight("X", bars(0.001))
        self.assertEqual(e["edge"], "tested")
        self.assertTrue(e["action"].startswith("Hold positions overnight"))
        self.assertNotEqual(edges._overnight("X", bars(0.0))["edge"], "tested")

    def test_vol_premium(self):
        from core import edges
        c = self.walk(300)                                                     # ~16% realised
        e = edges._vol_premium("XYZ", c, 0.30, None)
        self.assertEqual(e["edge"], "n/a")
        self.assertEqual(e["bias"], "neutral")
        self.assertTrue(e["active"])
        self.assertIn("rich", e["action"])
        self.assertIn("cheap", edges._vol_premium("XYZ", c, 0.08, None)["action"])
        e = edges._vol_premium("XYZ", c, None, None)
        self.assertEqual((e["active"], e["edge"], e["bias"]), (False, "n/a", None))

    def test_tracked_list(self):
        from core import edges
        with mock.patch.object(edges, "TRACKED", Path(tempfile.mkdtemp()) / "t.json"):
            self.assertEqual(edges.tracked(), ["SPY", "INTC", "SLV", "GOOGL"])
            self.assertIn("already", edges.add_tracked("spy"))
            with mock.patch.object(edges, "analyze", side_effect=ValueError("X: no price history found")):
                self.assertEqual(edges.add_tracked("X"), "X: no price history found")
            self.assertFalse(edges.TRACKED.exists())
            with mock.patch.object(edges, "analyze", return_value={}):
                self.assertIsNone(edges.add_tracked("tsla"))
                self.assertEqual(edges.tracked(), ["SPY", "INTC", "SLV", "GOOGL", "TSLA"])
                self.assertEqual(__import__("json").loads(edges.TRACKED.read_text())[-1], "TSLA")
                edges.remove_tracked("tsla")
                self.assertEqual(edges.tracked(), ["SPY", "INTC", "SLV", "GOOGL"])
                edges._save([f"S{i}" for i in range(edges.MAX_TRACKED)])
                self.assertIn("full", edges.add_tracked("NEW"))

    def test_analyze_end_to_end_offline(self):
        import json
        import pandas as pd
        from core import edges
        b = self.bars(self.walk(2000, drift=0.0004))
        info = {"shortName": "Test Co", "quoteType": "EQUITY", "trailingPE": 20.0, "forwardPE": None, "returnOnEquity": 0.2, "beta": 1.1, "profitMargins": 0.1}
        ann = [pd.Timestamp(f"{b.index[k]:%Y-%m-%d} 16:05") for k in (-300, -100, -30)]
        with mock.patch.object(edges, "_bars", return_value=b), mock.patch.object(edges, "_live", return_value=(float(b["Close"].iloc[-1]), 0.30)), \
                mock.patch.object(edges, "_info", return_value=info), mock.patch.object(edges, "_earnings", return_value=ann), \
                mock.patch.object(edges, "_fomc_dates", return_value=[]):
            r = edges.analyze(" test ")
            self.assertTrue({"symbol", "name", "quote_type", "price", "history_since", "verdict", "edges"} <= set(r))
            self.assertEqual((r["symbol"], r["name"], r["quote_type"]), ("TEST", "Test Co", "EQUITY"))
            self.assertEqual([e["key"] for e in r["edges"]], ["reversal", "earnings", "tom", "fed", "trend", "momentum", "volatility", "overnight", "factors"])
            keys = {"key", "label", "name", "horizon", "source", "what", "now", "trigger", "active", "bias", "edge", "evidence", "action", "plan"}
            for e in r["edges"]:
                self.assertEqual(set(e), keys)
                self.assertIn(e["edge"], ("tested", "thin", "none", "n/a"))
            for h in ("short", "mid", "long"):
                self.assertEqual(set(r["verdict"][h]), {"score", "bias", "label", "edges"})
            self.assertTrue(r["verdict"]["summary"].startswith("TEST: "))
            json.dumps(r)
            with self.assertRaises(ValueError):
                edges.analyze("bad ticker!")
            with mock.patch.object(edges, "_bars", return_value=b.iloc[:299]), self.assertRaises(ValueError):
                edges.analyze("TEST")

    def test_payload(self):
        from core import edges
        def fake(s):
            if s in ("BBB", "DDD"):
                raise ValueError(f"{s}: no price history found")
            return {"symbol": s}
        with mock.patch.object(edges, "tracked", return_value=["AAA", "BBB"]), mock.patch.object(edges, "analyze", side_effect=fake):
            p = edges.payload()
            self.assertEqual((p["status"], p["symbols"], p["failed"], len(p["tracked"])), ("success", ["AAA", "BBB"], ["BBB"], 1))
            self.assertNotIn("query", p)
            self.assertEqual(edges.payload(query="CCC")["query"], {"symbol": "CCC"})
            with self.assertRaises(ValueError):
                edges.payload(query="DDD")


if __name__ == "__main__":
    unittest.main()
