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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import DATA, FIX, ROOT, TMP, build_installation  # noqa: E402,F401  (sets up the environment)

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
        status, _ = send_email.deliver(eml)          # no email settings at all: the channel is off, not failed
        self.assertEqual(status, "skipped")
        self.assertTrue(eml.exists())

        class FlakySMTP:
            def __init__(self, *a, **k): pass
            def ehlo(self): pass
            def starttls(self): pass
            def login(self, *a): pass
            def send_message(self, m): raise ConnectionResetError("dropped after DATA")
        with mock.patch.object(send_email, "EMAIL_SENDER", "a@b"), mock.patch.object(send_email, "EMAIL_PASSWORD", "x"), \
                mock.patch.object(send_email, "SMTP_SERVER", "smtp.invalid"), \
                mock.patch.object(send_email, "RECIPIENT_EMAIL", "c@d"), mock.patch("smtplib.SMTP", FlakySMTP):
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
        # a fire is only valid close to a run slot (10 minutes before to 30 after), and never after the close
        self.assertIn("outside the scheduled run windows", scheduled_run_skip_reason(at("2026-10-06T11:00")))
        self.assertIn("after the 16:00 ET close", scheduled_run_skip_reason(at("2026-10-06T19:00")))
        self.assertIn("not an NYSE trading day", scheduled_run_skip_reason(at("2026-10-03T09:31")))
        self.assertIn("not an NYSE trading day", scheduled_run_skip_reason(at("2026-11-26T09:31")))
        # the day after Thanksgiving closes at 13:00 ET: the morning run goes ahead, the afternoon run is skipped
        self.assertIsNone(scheduled_run_skip_reason(at("2026-11-27T09:31")))
        self.assertIn("after the 13:00 ET early close", scheduled_run_skip_reason(at("2026-11-27T15:45")))

    def test_scheduled_run_needs_opt_in(self):
        import main_pipeline
        with mock.patch.object(config, "SCHEDULED_RUNS", False), \
                mock.patch.object(main_pipeline, "RunLock") as lock:
            self.assertEqual(main_pipeline.run(trigger="scheduled", offline=True), 0)
            lock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
