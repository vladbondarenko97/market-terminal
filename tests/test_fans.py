"""Scenario fans (core/fans.py): shapes, quantiles, baselines, derived series, forecast rows and the two routes.

Offline, no torch: Kronos is replaced by a fake predictor. Dates are fixed."""
import copy
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401,E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core import fans, lake  # noqa: E402
from test_api import ApiCase  # noqa: E402

TODAY = date(2026, 3, 13)


def walk(seed, n=700, start=100.0, vol=0.01, end="2026-03-13"):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=end, periods=n)
    c = start * np.exp(np.cumsum(rng.normal(0, vol, n)))
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 1e6}, index=idx)


def weekly(seed, n=700, level=1000.0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(end="2026-03-13", periods=n, freq="7D")        # Fridays
    season = 20 * np.sin(2 * np.pi * np.arange(n) / 52)
    return pd.Series(level + np.cumsum(rng.normal(0, 5, n)) * 0.2 + season, index=idx)


def fake_predictor(df, x_ts, y_ts, n, steps, seed):
    rng = np.random.default_rng(seed)
    return df["close"].iloc[-1] * np.exp(np.cumsum(rng.normal(0, 0.01, (n, steps)), axis=1))


def inputs():
    daily = {sid: walk(i) for i, sid in enumerate(fans.DAILY)}
    wk = {eid: weekly(i) for i, eid in enumerate(fans.EIA_IDS)}
    options = {"SPY": {"gex": {"status": "fresh", "put_wall": 640.0, "call_wall": 700.0, "zero_gamma": 668.123}}}
    return {"today": TODAY, "daily": daily, "weekly": wk, "options": options}


KINFO = {"status": "fresh", "reason": None, "model": "Kronos-small", "params": "24.7M", "T": 1.0, "top_p": 0.9,
         "lookback": 400, "device": "cpu", "seconds": 0.0}


class Build(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = fans.build_fans(inputs(), predictor=fake_predictor, kinfo=KINFO)

    def test_every_contract_series_is_present_and_fresh(self):
        s = self.out["series"]
        self.assertEqual(set(s), set(fans.DAILY) | set(fans.WEEKLY) | set(fans.DAYS) | {"crack.diesel", "crack.321"})
        self.assertEqual(self.out["status"], "fresh")             # the derived series' missing Kronos fans are by design
        self.assertEqual(self.out["version"], "fans_v1")
        for sid in list(fans.DAILY) + list(fans.WEEKLY):
            self.assertEqual({m["status"] for m in s[sid]["models"].values()}, {"fresh"}, sid)

    def test_shapes_and_quantiles(self):
        spy = self.out["series"]["SPY"]
        self.assertEqual(len(spy["dates"]), 20)
        m = spy["models"]["kronos"]
        self.assertEqual((len(m["paths"]), len(m["paths"][0])), (100, 20))
        self.assertEqual(set(m["q"]), {"p10", "p25", "p50", "p75", "p90"})
        a = np.array(m["paths"])
        for k, q in (("p10", 10), ("p50", 50), ("p90", 90)):
            np.testing.assert_allclose(m["q"][k], np.percentile(a, q, axis=0), rtol=2e-3)
        self.assertTrue(all(lo <= hi for lo, hi in zip(m["q"]["p10"], m["q"]["p90"])))
        self.assertEqual(len(spy["hist"]["dates"]), 90)
        wk = self.out["series"]["eia.WDISTUS1"]
        self.assertEqual((len(wk["dates"]), len(wk["hist"]["dates"]), len(wk["models"]["baseline"]["paths"][0])), (8, 52, 8))
        self.assertEqual(wk["note"], fans.WEEKLY_NOTE)

    def test_four_significant_digits(self):
        for v in self.out["series"]["SPY"]["models"]["baseline"]["paths"][0]:
            self.assertEqual(v, float(f"{v:.4g}"))
        last = self.out["series"]["SPY"]["last"]          # the spot keeps full precision: forecasts are graded against it
        self.assertNotEqual(last, float(f"{last:.4g}"))
        self.assertEqual(fans.sig4(1234.5678), 1235.0)
        self.assertEqual(fans.sig4(0.00123456), 0.001235)

    def test_levels_come_from_this_runs_gex(self):
        lv = {x["name"]: x["v"] for x in self.out["series"]["SPY"]["levels"]}
        self.assertEqual(lv, {"Put wall (this run)": 640.0, "Zero gamma (this run)": 668.1, "Call wall (this run)": 700.0})
        self.assertEqual(self.out["series"]["SLV"]["levels"], [])
        stale = inputs()
        stale["options"]["SPY"]["gex"]["status"] = "missing"
        self.assertEqual(fans.build_fans(stale, predictor=fake_predictor, kinfo=KINFO)["series"]["SPY"]["levels"], [])

    def test_deterministic_per_run_date(self):
        again = fans.build_fans(inputs(), predictor=fake_predictor, kinfo=KINFO)
        for sid in ("SPY", "eia.WDISTUS1", "crack.321", "days.jet"):
            self.assertEqual(again["series"][sid]["models"]["baseline"]["paths"],
                             self.out["series"][sid]["models"]["baseline"]["paths"])
        other = inputs() | {"today": date(2026, 3, 14)}
        diff = fans.build_fans(other, predictor=fake_predictor, kinfo=KINFO)
        self.assertNotEqual(diff["series"]["SPY"]["models"]["baseline"]["paths"],
                            self.out["series"]["SPY"]["models"]["baseline"]["paths"])

    def test_weekly_baseline_is_seasonal_and_dates_are_weekly(self):
        s = self.out["series"]["eia.WCESTUS1"]
        self.assertEqual(s["models"]["baseline"]["kind"], "seasonal")
        self.assertEqual(s["dates"][0], "2026-03-20")
        self.assertEqual(s["dates"][-1], "2026-05-08")

    def test_derived_series_are_baseline_only(self):
        for sid in ("crack.diesel", "crack.321", "days.distillate", "days.gasoline", "days.jet"):
            m = self.out["series"][sid]["models"]
            self.assertEqual(m["kronos"], {"status": "missing", "reason": fans.REASON_JOINT}, sid)
            self.assertEqual((m["baseline"]["status"], m["baseline"]["kind"]), ("fresh", "joint_bootstrap"))
            self.assertEqual(len(m["baseline"]["paths"]), 100)

    def test_joint_bootstrap_keeps_the_legs_together(self):
        """HO and CL move together; a crack built from jointly resampled days is far narrower than from independent legs."""
        rng = np.random.default_rng(1)
        n = 700
        common = rng.normal(0, 0.02, n)
        px = lambda x: 80 * np.exp(np.cumsum(common + rng.normal(0, 0.002, n) * x))
        idx = pd.bdate_range(end="2026-03-13", periods=n)
        ho, rb, cl = (pd.DataFrame({"Open": p, "High": p, "Low": p, "Close": p, "Volume": 0.0}, index=idx)
                      for p in (px(1) / 42 * 1.4, px(1.1) / 42 * 1.3, px(1.2)))
        inp = {"today": TODAY, "daily": {"HO": ho, "RB": rb, "CL": cl}, "weekly": {}, "options": {}}
        joint = fans.build_fans(inp, predictor=fake_predictor, kinfo=KINFO)["series"]["crack.diesel"]["models"]["baseline"]["paths"]
        # independent legs: resample each leg's own days
        legs = pd.DataFrame({"HO": ho["Close"], "CL": cl["Close"]})
        ind = np.stack([fans.bootstrap_paths(legs[c].values, 20, seed) for c, seed in (("HO", 1), ("CL", 2))])
        indep = ind[0] * 42 - ind[1]
        self.assertLess(np.std(np.array(joint)[:, -1]), 0.5 * np.std(indep[:, -1]))

    def test_missing_inputs_never_fabricate(self):
        inp = inputs()
        del inp["daily"]["SLV"], inp["daily"]["HO"], inp["weekly"]["WDISTUS1"]
        out = fans.build_fans(inp, predictor=fake_predictor, kinfo=KINFO)["series"]
        self.assertEqual(out["SLV"]["models"]["kronos"]["status"], "missing")
        self.assertNotIn("paths", out["SLV"]["models"]["baseline"])
        self.assertEqual(out["crack.diesel"]["models"]["baseline"]["status"], "missing")
        self.assertEqual(out["eia.WDISTUS1"]["models"]["baseline"]["status"], "missing")
        self.assertEqual(out["days.distillate"]["models"]["baseline"]["status"], "missing")
        self.assertEqual(out["SPY"]["models"]["baseline"]["status"], "fresh")

    def test_kronos_unavailable_leaves_the_baseline(self):
        out = fans.build_fans(inputs(), predictor=None,
                              kinfo={**KINFO, "status": "missing", "reason": "torch not installed: pip install -r requirements-kronos.txt"})
        self.assertEqual(out["kronos"]["status"], "missing")
        self.assertIn("torch not installed", out["series"]["SPY"]["models"]["kronos"]["reason"])
        self.assertEqual(out["series"]["SPY"]["models"]["baseline"]["status"], "fresh")
        self.assertEqual(out["status"], "partial")

    def test_a_failing_predictor_is_an_error_not_a_crash(self):
        def boom(*a):
            raise RuntimeError("oom")
        out = fans.build_fans(inputs(), predictor=boom, kinfo=KINFO)
        self.assertEqual(out["kronos"]["status"], "error")
        self.assertEqual(out["series"]["SPY"]["models"]["kronos"]["status"], "error")
        self.assertEqual(out["series"]["SPY"]["models"]["baseline"]["status"], "fresh")

    def test_a_series_without_history_has_the_full_key_set(self):
        inp = inputs()
        del inp["daily"]["SLV"], inp["daily"]["HO"], inp["weekly"]["WDISTUS1"]
        s = fans.build_fans(inp, predictor=fake_predictor, kinfo=KINFO)["series"]
        want = set(s["SPY"])
        for sid in ("SLV", "crack.diesel", "eia.WDISTUS1", "days.distillate"):
            self.assertEqual(set(s[sid]), want, sid)
            self.assertEqual((s[sid]["hist"], s[sid]["dates"], s[sid]["last"], s[sid]["asof"]),
                             ({"dates": [], "values": []}, [], None, None))

    def test_kronos_load_failure_is_an_error_and_baselines_still_build(self):
        import types
        tdir = support.TMP / "kronos_w"
        for d in ("model", "tokenizer"):
            (tdir / d).mkdir(parents=True, exist_ok=True)
            (tdir / d / "config.json").write_text("{}")
        torch = types.SimpleNamespace(backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False)))
        boom = types.SimpleNamespace(Kronos=types.SimpleNamespace(from_pretrained=mock.Mock(side_effect=OSError("corrupt weights"))),
                                     KronosTokenizer=types.SimpleNamespace(from_pretrained=lambda p: None), KronosPredictor=None)
        mods = {"torch": torch, "einops": types.ModuleType("einops"), "huggingface_hub": types.ModuleType("huggingface_hub"),
                "third_party.kronos": boom}
        with mock.patch.object(fans.config, "KRONOS_DIR", tdir), mock.patch.object(fans.config, "KRONOS_ENABLED", True), \
                mock.patch.dict(sys.modules, mods):
            p, info = fans.load_kronos()
            out = fans.build_fans(inputs())
        self.assertIsNone(p)
        self.assertEqual((info["status"], info["reason"]), ("error", "OSError: corrupt weights"))
        self.assertEqual(out["kronos"]["status"], "error")
        self.assertEqual(out["series"]["SPY"]["models"]["baseline"]["status"], "fresh")

    def test_no_inputs_is_missing(self):
        out = fans.build_fans({"today": TODAY, "daily": {}, "weekly": {}}, predictor=None, kinfo=None)
        self.assertEqual(out["status"], "missing")

    def test_load_kronos_reports_why(self):
        with mock.patch.object(fans.config, "KRONOS_ENABLED", False):
            self.assertIn("KRONOS_ENABLED", fans.load_kronos()[1]["reason"])
        p, info = fans.load_kronos()
        self.assertIsNone(p)                                        # no torch in the test environment, or no weights
        self.assertEqual(info["status"], "missing")
        self.assertRegex(info["reason"], "torch not installed|weights not found")

    def test_trading_dates_skip_holidays(self):
        d = fans.trading_dates(date(2026, 4, 1), 5)                 # Good Friday 2026-04-03
        self.assertEqual([x.isoformat() for x in d], ["2026-04-02", "2026-04-06", "2026-04-07", "2026-04-08", "2026-04-09"])
        self.assertNotIn(date(2026, 11, 26), fans.trading_dates(date(2026, 11, 24), 4))      # Thanksgiving

    def test_scoring(self):
        x = np.linspace(95, 115, 101)
        self.assertLess(fans.crps(x, 105.0), fans.crps(x, 130.0))
        r = fans.score_origin(x, 100.0, 105.0)
        self.assertEqual((r["band"], r["dir"]), (True, True))
        self.assertFalse(fans.score_origin(x, 100.0, 130.0)["band"])
        s = fans.summarise_scores([{"origin": "2020-01-01", "5": r}, {"origin": "2020-02-01", "5": r}], (5, 20))
        self.assertEqual((s["by_horizon"]["5"]["n"], s["period"]), (2, ["2020-01-01", "2020-02-01"]))
        self.assertNotIn("20", s["by_horizon"])


class Rows(unittest.TestCase):
    def test_forecast_rows_and_idempotent_insert(self):
        out = fans.build_fans(inputs(), predictor=fake_predictor, kinfo=KINFO)
        ctx = {"run": {"run_id": "r1", "generated_at": "2026-03-13T20:00:00+00:00"}, "fans": out}
        rows = fans.fan_rows(ctx)
        # 12 daily series with a symbol? crack/eia/days have none: 12 symbols x 2 models x 2 horizons
        self.assertEqual(len(rows), 12 * 2 * 2)
        spy = {(r[3], r[4]): r for r in rows if r[2] == "SPY"}
        self.assertEqual(set(spy), {("kronos_fan_v1", "1w"), ("kronos_fan_v1", "1m"), ("baseline_fan_v1", "1w"),
                                    ("baseline_fan_v1", "1m")})
        r = spy[("baseline_fan_v1", "1w")]
        s = out["series"]["SPY"]
        self.assertEqual(r[5], s["dates"][4])
        self.assertEqual(spy[("baseline_fan_v1", "1m")][5], s["dates"][19])
        self.assertTrue(0 <= r[7] <= 1 and r[8] < r[9] and r[10] in ("UP", "DOWN") and r[11] > 0)
        conn = lake.connect(support.TMP / "fans_rows.db")
        lake.migrate(conn)
        self.assertEqual(fans.record_fans(conn, ctx), len(rows))
        fans.record_fans(conn, ctx)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM v2_forecasts WHERE model LIKE '%_fan_v1'").fetchone()[0], len(rows))
        conn.close()

    def test_no_rows_without_fans(self):
        self.assertEqual(fans.fan_rows({"run": {"run_id": "r", "generated_at": "x"}}), [])


class Routes(ApiCase):
    def commit(self, ctx):
        conn = lake.connect(api_router_module()._V2_DB_PATH)
        lake.migrate(conn)
        lake.create_run(conn, "r1", mode="live", trigger="test", run_folder="x", code_version="t")
        lake.update_run(conn, "r1", status="committed")
        lake.commit_snapshot(conn, "r1", {"run": {"run_id": "r1", "generated_at": "2026-03-13T20:00:00+00:00"}, **ctx})
        return conn

    def test_fans_route(self):
        self.get_json("/api/fans", 503)                                         # no database yet
        out = fans.build_fans(inputs(), predictor=fake_predictor, kinfo=KINFO)
        conn = self.commit({"fans": out})
        conn.close()
        body = self.get_json("/api/fans")
        self.assertEqual((body["run_id"], body["generated_at"], body["version"]), ("r1", "2026-03-13T20:00:00+00:00", "fans_v1"))
        self.assertEqual(len(body["series"]["SPY"]["models"]["kronos"]["paths"]), 100)

    def test_fans_route_missing_when_the_snapshot_has_none(self):
        self.commit({}).close()
        body = self.get_json("/api/fans", 404)
        self.assertEqual(body["status"], "missing")

    def test_scorecard_route(self):
        out = fans.build_fans(inputs(), predictor=fake_predictor, kinfo=KINFO)
        ctx = {"fans": out}
        conn = self.commit(ctx)
        self.assertEqual(self.get_json("/api/fans/scorecard")["status"], "missing")
        summary = {"by_horizon": {"20": {"n": 5, "band_hit": 0.8, "crps_pct": 1.2, "dir_hit": 0.6}}, "period": ["2015-01-02", "2026-01-02"]}
        lake.record_observations(conn, [lake.obs("fans.backtest", "2026-01-02", entity="SPY", dims={"model": "kronos"},
                                                 value=summary, source="t")])
        # logged forecasts whose target date has passed, graded by the pipeline from the run's own closes
        run = {"run_id": "r1", "generated_at": "2026-03-13T20:00:00+00:00"}
        fans.record_fans(conn, {"run": run, "fans": out})
        later = {"run": {"run_id": "r2", "generated_at": "2026-05-01T20:00:00+00:00"}, "fans": copy.deepcopy(out)}
        idx = pd.bdate_range("2026-03-02", "2026-04-30")
        later["fans"]["series"]["SPY"]["hist"] = {"dates": [d.date().isoformat() for d in idx], "values": [100.0] * len(idx)}
        self.assertEqual(fans.grade_live(conn, later), 24)              # 12 symbols x 2 models; only SPY has closes past its targets
        conn.close()
        body = self.get_json("/api/fans/scorecard")                     # reads stored observations: tests/support.py blocks the network
        self.assertEqual(body["status"], "success")
        spy = body["series"]["SPY"]
        self.assertEqual(spy["kronos"]["backtest"], summary)
        live = spy["baseline"]["live"]
        self.assertEqual(set(live["by_horizon"]), {"1w", "1m"})
        self.assertEqual(live["by_horizon"]["1w"]["n"], 1)
        self.assertEqual(live["pending"], 0)
        self.assertEqual(live["graded_through"], "2026-04-30")
        self.assertNotIn("backtest", spy["baseline"])

    def test_scorecard_route_pending_is_always_present(self):
        out = fans.build_fans(inputs(), predictor=fake_predictor, kinfo=KINFO)
        conn = self.commit({"fans": out})
        ctx = {"run": {"run_id": "r1", "generated_at": "2026-03-13T20:00:00+00:00"}, "fans": out}
        fans.record_fans(conn, ctx)
        fans.grade_live(conn, ctx)                                      # nothing has matured yet
        conn.close()
        live = self.get_json("/api/fans/scorecard")["series"]["SPY"]["kronos"]["live"]
        self.assertEqual((live["by_horizon"], live["pending"]), ({}, 2))


class Grading(unittest.TestCase):
    def test_fan_models_stay_out_of_card_9_and_graded_alone(self):
        from core import forecast
        out = fans.build_fans(inputs(), predictor=fake_predictor, kinfo=KINFO)
        conn = lake.connect(support.TMP / "fans_grade.db")
        lake.migrate(conn)
        fans.record_fans(conn, {"run": {"run_id": "r1", "generated_at": "2026-03-13T20:00:00+00:00"}, "fans": out})
        closes = pd.Series(100.0, index=pd.bdate_range("2026-03-01", "2026-12-31"))
        card9 = forecast.scorecard(conn, lambda s: closes, "SPY")
        self.assertEqual((card9["graded"], card9["pending"], card9["logged"]), ([], 0, 0))
        fan = forecast.scorecard(conn, lambda s: closes, "SPY", only=tuple(fans.FAN_MODELS.values()))
        self.assertEqual({g["model"] for g in fan["graded"]}, set(fans.FAN_MODELS.values()))
        conn.close()


def api_router_module():
    import api_router
    return api_router


if __name__ == "__main__":
    unittest.main()
