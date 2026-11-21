"""Forced-flow projection of card R (core/flowproj.py): price scenarios (signs, monotonic downside, CTA flip crossings, the gamma
model), the time paths (the roll-off date, the trading calendar), the rebuilt history, missing inputs, and its place in the regime
block and THE READ's facts. Offline; synthetic closes on fixed dates."""
import sys, unittest
from pathlib import Path

import support  # noqa: F401  (temporary CME_Data, no credentials; must come before importing config)

from datetime import date

import numpy as np
import pandas as pd

from core import flowproj as P
from core import regime as R
from core import market_calendar

TODAY = date(2026, 11, 20)          # a Friday; Thanksgiving (Nov 26) falls inside the 30-session window


def series(rets, end=TODAY, start=500.0):
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=len(rets) + 1)
    return pd.Series(start * np.cumprod(np.r_[1.0, 1 + np.asarray(rets)]), index=idx)


def calm_rets(n=400, a=0.005):
    return [(-a if i % 2 else a) + 0.0004 for i in range(n)]          # a slow uptrend: every CTA window LONG


def flows(gex=-50e6, zero=None, lev=1e9):
    f = {"status": "fresh", "leveraged_etfs": {"flow_per_1pct_move": lev, "funds": {"SPXL": {"aum": 1e9, "leverage": 3}}},
         "dealer_gamma": {"net_gex_usd_per_1pt": gex, "zero_gamma": zero}}
    if gex is None:
        del f["dealer_gamma"]
    return f


def build(c, fl=None, runs=None):
    return P.build(list(c.values), [d.date().isoformat() for d in c.index], float(c.iloc[-1]), fl or flows(), TODAY, runs)


class Scenarios(unittest.TestCase):
    def test_grid_signs_and_a_monotonic_downside(self):
        c = series(calm_rets())
        fp = build(c, flows(gex=-50e6, zero=float(c.iloc[-1]) * 1.02))
        self.assertEqual(fp["status"], "fresh")
        rows = fp["scenarios"]["rows"]
        self.assertEqual([r["move_pct"] for r in rows][:3], [-10.0, -9.5, -9.0])
        self.assertEqual((rows[0]["move_pct"], rows[-1]["move_pct"], len(rows)), (-10.0, 5.0, 31))
        flat = next(r for r in rows if r["move_pct"] == 0)
        for k in ("leveraged_etf", "cta", "dealer_gamma", "vol_control", "total"):
            self.assertEqual(abs(flat[k]), 0, k)                       # an unchanged close adds no flow
        down = [r for r in rows if r["move_pct"] <= 0]
        for k in ("leveraged_etf", "cta", "dealer_gamma", "vol_control", "total"):
            vals = [r[k] for r in down]
            self.assertTrue(all(a <= b for a, b in zip(vals, vals[1:])), (k, vals))     # a bigger drop never sells less
            self.assertLess(vals[0], 0, k)
        r10 = rows[0]
        self.assertEqual(r10["leveraged_etf"], -10e9)                  # AUM x L(L-1) per 1% x -10
        self.assertLessEqual(r10["total_low"], r10["total"])
        self.assertLessEqual(r10["total"], r10["total_high"])
        self.assertEqual(fp["scenarios"]["sources_in_total"], ["leveraged_etf", "cta", "dealer_gamma", "vol_control"])

    def test_cta_flip_crossings(self):
        rets = calm_rets()
        rets[-21:] = [0.0015] * 21                                     # the 1M window: +3.2%, so a ~3% drop flips it
        c = series(rets)
        sc = build(c)["scenarios"]
        lvl, spot = sc["cta_flip_levels"]["1m"], sc["spot"]
        self.assertAlmostEqual(lvl, float(c.iloc[-21]), places=2)
        cut = (lvl / spot - 1) * 100
        for r in sc["rows"]:
            self.assertEqual("1m" in r["cta_windows_flipped"], r["move_pct"] < cut, (r["move_pct"], cut))
        self.assertTrue(any(r["cta_windows_flipped"] for r in sc["rows"]))
        self.assertFalse(any("1m" in r["cta_windows_flipped"] for r in sc["rows"] if r["move_pct"] >= 0))

    def test_gamma_is_linear_through_the_zero_gamma_level(self):
        self.assertEqual(P.gamma_flow(-10.0, 100.0, 98.0, None), -20.0)            # no zero level: constant gamma, sold into the drop
        # negative gamma below a zero level at 104: dealers buy the rally up to 104, then sell it above
        up_to_zero = P.gamma_flow(-10.0, 100.0, 104.0, 104.0)
        self.assertGreater(up_to_zero, 0)
        self.assertAlmostEqual(up_to_zero, -(-10 / -4) * (0 - 16) / 2)
        self.assertLess(P.gamma_flow(-10.0, 100.0, 112.0, 104.0), P.gamma_flow(-10.0, 100.0, 108.0, 104.0) + 1e-9)
        sc = build(series(calm_rets()), flows(gex=-50e6, zero=None))["scenarios"]
        self.assertIn("constant", sc["gamma_model"])


class TimePaths(unittest.TestCase):
    def test_the_roll_off_date_and_the_calendar(self):
        rets = calm_rets(400, 0.004)
        rets[-50] = -0.06                                              # one big day, 50 sessions ago: out of the 1M window, still in the 3M one
        c = series(rets)
        tp = build(c)["time_path"]
        days = [date.fromisoformat(d) for d in tp["dates"]]
        self.assertEqual(len(days), 30)
        self.assertNotIn(date(2026, 11, 26), days)                     # Thanksgiving
        self.assertTrue(all(market_calendar.is_trading_day(d) for d in days))
        self.assertEqual(days[0], date(2026, 11, 23))
        # the big day leaves the 63-session window on the 14th appended session: vol-control re-levers from then, under calm moves
        calm = tp["paths"]["calm"]
        self.assertEqual(calm["releverage_date"], days[13].isoformat())
        self.assertGreater(calm["flow"][13], 5e9)
        self.assertTrue(all(abs(f) < 2e9 for f in calm["flow"][:13]), calm["flow"][:13])
        self.assertLessEqual(calm["cumulative_low"], calm["cumulative"][-1])
        self.assertLessEqual(calm["cumulative"][-1], calm["cumulative_high"])
        # 2%/day is far above the 0.4% days of the history: vol-control sells, and it is still lower than today after 30 sessions
        self.assertLess(tp["paths"]["stressed"]["cumulative"][-1], 0)

    def test_cta_under_a_flat_price(self):
        rets = calm_rets()
        rets[-21:] = [0.001] * 21
        c = series(rets)
        cf = build(c)["time_path"]["cta_flat"]
        self.assertEqual(len(cf["exposure"]), 30)
        # a slow uptrend held flat: every window stays LONG (each anchor is a close below spot), so no flip date and no sign flip
        self.assertEqual(cf["window_flip_dates"], {"1m": None, "3m": None, "6m": None, "12m": None})
        self.assertGreater(cf["exposure_now"], 0)
        # a rally then a dip: the 1M window is still LONG, but held flat it is measured from the rally's closes above spot and flips
        rets = calm_rets()
        rets[-21:] = [0.01] * 11 + [-0.008] * 10
        c = series(rets)
        cf = build(c)["time_path"]["cta_flat"]
        days = build(c)["time_path"]["dates"]
        anchors = list(c.iloc[-21:].values)                            # session j (0-based) is measured from c[-21 + j]
        j = next(i for i, a in enumerate(anchors) if a > c.iloc[-1])
        self.assertEqual(cf["window_flip_dates"]["1m"], days[j])
        self.assertIsNone(cf["sign_flip_date"])


class History(unittest.TestCase):
    def test_sources_present_only_where_their_inputs_are(self):
        c = series(calm_rets())
        runs = [{"date": "2026-11-18", "lev_per_1pct": 1e9, "net_gex": -40e6}, {"date": "2026-11-19", "lev_per_1pct": 1e9, "net_gex": None}]
        h = build(c, runs=runs)["history"]
        self.assertEqual(len(h["dates"]), 252)
        self.assertEqual(h["dates"][-1], "2026-11-20")
        i19, i20, i18 = h["dates"].index("2026-11-19"), h["dates"].index("2026-11-20"), h["dates"].index("2026-11-18")
        lev = h["sources"]["leveraged_etf"]
        self.assertIsNotNone(lev[i19])                                 # from the run of the 18th
        self.assertIsNotNone(lev[i20])                                 # from the run of the 19th
        self.assertIsNone(lev[i19 - 5])                                # no stored run: missing, not zero
        self.assertIsNone(h["sources"]["dealer_gamma"][i20])           # the 19th's run had no gamma
        self.assertTrue(h["complete"][i19])
        self.assertFalse(h["complete"][i20])
        self.assertIsNotNone(h["total"][i18 - 10])                     # vol-control + CTA rebuilt from the closes alone
        r = c.pct_change()
        self.assertAlmostEqual(lev[i19], 1e9 * 100 * r.iloc[-2], places=-1)


class Missing(unittest.TestCase):
    def test_missing_inputs_are_missing_with_a_reason(self):
        c = series(calm_rets())
        self.assertEqual(P.build([], [], 600.0, flows(), TODAY)["status"], "missing")
        self.assertIn("flows", P.build(list(c), [str(d.date()) for d in c.index], 600.0, {"status": "missing", "reason": "x"}, TODAY)["reason"])
        self.assertIn("spot", P.build(list(c), [str(d.date()) for d in c.index], None, flows(), TODAY)["reason"])
        sc = build(c, flows(gex=None, lev=None))["scenarios"]
        self.assertIn("dealer_gamma", sc["missing"])
        self.assertIn("leveraged_etf", sc["missing"])
        self.assertTrue(all(r["dealer_gamma"] is None and r["leveraged_etf"] is None for r in sc["rows"]))
        self.assertEqual(sc["sources_in_total"], ["cta", "vol_control"])
        short = series(calm_rets(100))                                 # enough for vol-control, not for the 12M window
        sc = build(short)["scenarios"]
        self.assertIn("cta", sc["missing"])
        self.assertTrue(all(r["cta"] is None for r in sc["rows"]))
        self.assertEqual(build(short)["time_path"]["cta_flat"]["status"], "missing")


class InTheRegime(unittest.TestCase):
    def test_block_summary_and_facts(self):
        import test_regime as T
        c = series(calm_rets())
        inp = T.inputs()
        inp["forecast"]["SPY"]["spot"] = float(c.iloc[-1])
        inp.update(spy_closes=list(c.values), spy_dates=[str(d.date()) for d in c.index])
        b = R.build(inp)
        fp = b["flow_projection"]
        self.assertEqual((fp["status"], fp["version"], fp["estimate"]), ("fresh", P.FLOWPROJ_VERSION, True))
        self.assertTrue(fp["summary"].startswith("Another -$"), fp["summary"])
        self.assertIn("if SPY -5%", fp["summary"])
        f = R.facts(b)
        self.assertIn("Forced-flow projection (estimate): Another", f)
        self.assertIn("Projection, SPY move -> est. forced flow: -2%", f)
        self.assertIn("Projection, vol-control turns net buyer: calm", f)
        row5 = next(r for r in fp["scenarios"]["rows"] if r["move_pct"] == -5.0)
        text = f"Volatility is high and vol-control funds are selling. Another {row5['total'] / 1e9:.1f}B of forced flow would follow if SPY fell 5%, an estimate."
        self.assertIsNone(R.valid_read(text, f))
        self.assertIsNotNone(R.valid_read(text.replace(f"{row5['total'] / 1e9:.1f}", "123.4"), f))
        # no dated closes (an older collect): missing, and the regime itself still stands
        b = R.build(T.inputs())
        self.assertEqual((b["status"], b["flow_projection"]["status"]), ("fresh", "missing"))
        self.assertIn("Forced-flow projection: missing", R.facts(b))


from test_api import ApiCase


class Routes(ApiCase):
    def block(self):
        import test_regime as T
        c = series(calm_rets())
        inp = T.inputs()
        inp["forecast"]["SPY"]["spot"] = float(c.iloc[-1])
        inp.update(spy_closes=list(c.values), spy_dates=[str(d.date()) for d in c.index])
        return R.build(inp)

    def commit(self, blk):
        from core import lake
        conn = lake.connect(self.tmp / "portfolio.db")
        lake.migrate(conn)
        lake.create_run(conn, "r1", mode="live", trigger="manual", run_folder="Nov-20-26", code_version="t")
        lake.commit_snapshot(conn, "r1", {"run": {"run_id": "r1"}, "regime": blk})
        lake.update_run(conn, "r1", status="completed")
        conn.close()

    def test_chart_series_console_and_mcp(self):
        blk = self.block()
        self.commit(blk)
        j = self.get_json("/api/chart/series?id=regime.forced_flow")
        h = blk["flow_projection"]["history"]
        self.assertEqual((j["status"], j["unit"]), ("success", "USD bn"))
        self.assertEqual(len(j["v"]), sum(v is not None for v in h["total"]))
        self.assertAlmostEqual(j["v"][-1], h["total"][-1] / 1e9, places=4)
        self.assertIn("regime.forced_flow", [s["id"] for s in self.get_json("/api/chart/catalog")["series"]])
        import assistant
        raw = self.get_json("/api/regime")
        short = assistant._shape_regime(raw, {})["data"]["flow_projection"]
        self.assertEqual([r["move_pct"] for r in short["scenarios"]["rows"]][:3], [-10.0, -9.0, -8.0])
        self.assertIn("releverage_date", short["time_path"]["paths"]["calm"])
        self.assertEqual(len(short["history_last_10"]), 10)
        self.assertNotIn("exposure", short["time_path"]["cta_flat"])
        self.assertEqual(raw["data"]["flow_projection"]["version"], P.FLOWPROJ_VERSION)     # the shaper copies; the route keeps the raw arrays


if __name__ == "__main__":
    unittest.main()
