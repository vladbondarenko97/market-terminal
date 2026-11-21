"""R · Market Regime / Shock Reader (core/regime.py): each regime on synthetic inputs, forced-flow rows present and missing, the playbook,
the retest rule, THE READ (drivers mocked) and the block's place in the snapshot. Offline; fixed dates."""
import sys, unittest
from pathlib import Path

import support  # noqa: F401  (temporary CME_Data, no credentials; must come before importing config)

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

from core import regime as R
from test_api import ApiCase


def flows(**over):
    f = {"status": "fresh",
         "vol_control": {"exposure": 0.39, "exposure_5d_ago": 0.46, "flow_last_5d": -27e9, "flow_last_21d": -206e9, "flow_if_tomorrow": {"-2%": -3.2e9}, "aum_assumption": 400e9},
         "cta": {"exposure": -0.5, "exposure_after_move": {"-2%": -0.56, "+2%": -0.4}},
         "leveraged_etfs": {"last_session_rebalance": -3.7e9, "last_session_return_pct": -3.6, "flow_per_1pct_move": 1e9, "funds": {"SPXL": {}}},
         "dealer_gamma": {"net_gex_usd_per_1pt": -55e6, "zero_gamma": 704.0}}
    f.update(over)
    return f


def inputs(vix=36.0, prev_close=28.4, vix3m=31.0, vvix=138.0, oas=3.5, oas_3m=0.8, sahm=0.0, rec=0.16, liq_level=5800.0, liq_change=20.0,
           sofr=3.90, effr=3.88, srf=0.0, chaos=72.0, chaos_prev=53.0, prev=None, fl=None, closes=None, btc=None):
    return {"today": "2026-11-20",
            "prices": {"vix": {"value": vix, "prev_close": prev_close, "observed_at": "2026-11-20"}, "vix3m": {"value": vix3m}},
            "forecast": {"status": "fresh", "macro_regime": {"status": "fresh", "hy_oas": oas, "hy_oas_change_3m": oas_3m, "sahm": sahm, "recession_prob_12m": rec, "as_of": "2026-11-20"},
                         "SPY": {"spot": 690.0, "flows": fl or flows(), "trend": {"status": "fresh", "signals": {"1m": {"signal": "SHORT", "flip_level": 760.0, "flip_distance_pct": 10.2}}}},
                         "SLV": {"spot": 37.0, "flows": {"status": "missing", "reason": "x"}}},
            "chaos": {"score": chaos, "prev": {"date": "2026-11-19", "score": chaos_prev}, "as_of": "2026-11-20",
                      "components": {"vvix": {"value": vvix, "as_of": "2026-11-20"}},
                      "levers": {"rows": [{"key": "net_liquidity", "status": "fresh", "level": liq_level, "change_20d": liq_change, "as_of": "2026-11-18"}]}},
            "fed_ops": {"repo": {"last_60d": [{"date": "2026-11-19", "sofr": sofr, "effr": effr, "repo_bn": srf}]}},
            "bitcoin": btc, "spy_closes": closes, "prev": prev}


def key_of(**kw):
    b = R.build(inputs(**kw))
    return b["status"], b.get("key")


class Classification(unittest.TestCase):
    def test_each_regime(self):
        self.assertEqual(key_of(vix=15, prev_close=15, vix3m=17, vvix=90, oas=3.0, oas_3m=0.1, chaos=10, chaos_prev=12), ("fresh", "CALM"))
        self.assertEqual(key_of(), ("fresh", "VOL_SHOCK"))
        self.assertEqual(key_of(oas=4.2), ("fresh", "VOL_SHOCK_WATCH"))                      # credit between 4 and 4.5: no longer "no confirmation"
        self.assertEqual(key_of(rec=0.35), ("fresh", "VOL_SHOCK_WATCH"))
        self.assertEqual(key_of(oas=4.6), ("fresh", "CREDIT_STRESS"))
        self.assertEqual(key_of(oas=3.9, oas_3m=1.1), ("fresh", "CREDIT_STRESS"))             # +1 pt in 3 months
        self.assertEqual(key_of(oas=4.6, sahm=0.6), ("fresh", "RECESSION_BEAR"))
        self.assertEqual(key_of(oas=4.6, rec=0.4), ("fresh", "RECESSION_BEAR"))
        self.assertEqual(key_of(liq_change=-200.0), ("fresh", "LIQUIDITY_SQUEEZE"))           # -3.3% of its level
        self.assertEqual(key_of(sofr=4.05), ("fresh", "LIQUIDITY_SQUEEZE"))                  # SOFR - EFFR = 0.17
        self.assertEqual(key_of(srf=12.0), ("fresh", "LIQUIDITY_SQUEEZE"))

    def test_panic_unwinding_needs_a_previous_shock_a_normal_curve_and_a_falling_chaos(self):
        calm_curve = dict(vix=24.0, prev_close=26.0, vix3m=26.0, vvix=130.0, chaos=60.0, chaos_prev=70.0)
        self.assertEqual(key_of(prev={"key": "VOL_SHOCK"}, **calm_curve), ("fresh", "PANIC_UNWINDING"))
        self.assertEqual(key_of(prev={"key": "PANIC_UNWINDING"}, **calm_curve), ("fresh", "PANIC_UNWINDING"))
        self.assertEqual(key_of(prev={"key": "CALM"}, **calm_curve)[1], "VOL_SHOCK")           # VVIX 130 is still a shock reading without a previous shock
        self.assertEqual(key_of(prev={"key": "VOL_SHOCK"}, **{**calm_curve, "chaos_prev": 61.0})[1], "VOL_SHOCK")   # chaos not falling
        self.assertEqual(key_of(prev={"key": "VOL_SHOCK"}, **{**calm_curve, "vix3m": 23.0})[1], "VOL_SHOCK")        # curve still inverted

    def test_credit_outranks_liquidity_and_a_shock(self):
        self.assertEqual(key_of(oas=4.7, liq_change=-300.0)[1], "CREDIT_STRESS")

    def test_missing_inputs_are_missing_with_a_reason_never_a_default(self):
        b = R.build({**inputs(), "forecast": {"status": "fresh", "macro_regime": {"status": "missing", "reason": "T10Y3M unavailable"}, "SPY": {}}})
        self.assertEqual(b["status"], "missing")
        self.assertIn("HY OAS is missing", b["reason"])
        self.assertIn("T10Y3M unavailable", b["reason"])
        b = R.build({**inputs(), "prices": {}, "chaos": {}})
        self.assertEqual(b["status"], "missing")
        self.assertIn("no volatility reading", b["reason"])
        self.assertNotIn("regime", b)

    def test_why_lines_print_the_reading_against_its_threshold(self):
        why = R.build(inputs())["why"]
        self.assertTrue(any(w.startswith("VIX / VIX3M 1.161 vs > 1 - MET") for w in why), why)
        self.assertTrue(any("HY OAS 3.5%" in w and "calm < 4.0%" in w for w in why), why)
        self.assertTrue(any(w.startswith("Sahm rule 0 vs >= 0.5 - not met") for w in why), why)

    def test_a_change_is_flagged_against_the_previous_run(self):
        b = R.build(inputs(prev={"key": "CALM", "regime": "CALM", "run_id": "r0"}))
        self.assertTrue(b["changed"])
        self.assertEqual(b["previous"]["regime"], "CALM")
        self.assertFalse(R.build(inputs(prev={"key": "VOL_SHOCK", "regime": "x", "run_id": "r0"}))["changed"])
        self.assertNotIn("changed", R.build({**inputs(), "prices": {}, "chaos": {}}))


class ForcedSelling(unittest.TestCase):
    def rows(self, **kw):
        return {r["key"]: r for r in R.build(inputs(**kw))["forced_selling"]["rows"]}

    def test_rows_present_with_value_direction_and_status(self):
        rows = self.rows()
        self.assertEqual((rows["vol_control"]["direction"], rows["vol_control"]["status"], rows["vol_control"]["value"]), ("SELL", "ACTIVE", -27e9))
        self.assertEqual((rows["leveraged_etf"]["direction"], rows["leveraged_etf"]["status"]), ("SELL", "ACTIVE"))
        self.assertEqual((rows["dealer_gamma"]["direction"], rows["dealer_gamma"]["status"]), ("SELL", "ACTIVE"))
        self.assertEqual((rows["cta"]["direction"], rows["cta"]["status"]), ("SELL", "quiet"))      # short, but the flip level is 10% away
        self.assertIn("flips 760", rows["cta"]["detail"])
        fs = R.build(inputs())["forced_selling"]
        self.assertEqual(fs["if_spy_down_2pct_usd"], -3.2e9 - 2e9)
        self.assertIn("Vol-control funds (est.)", fs["active_sellers"])

    def test_small_flows_are_quiet_and_a_positive_gamma_dampens(self):
        fl = flows(vol_control={**flows()["vol_control"], "flow_last_5d": -2e9}, leveraged_etfs={**flows()["leveraged_etfs"], "last_session_rebalance": 0.2e9},
                   dealer_gamma={"net_gex_usd_per_1pt": 30e6, "zero_gamma": 650.0})
        rows = self.rows(fl=fl)
        self.assertEqual([rows[k]["status"] for k in ("vol_control", "leveraged_etf", "dealer_gamma")], ["quiet"] * 3)
        self.assertEqual(rows["dealer_gamma"]["direction"], "NEUTRAL")

    def test_a_part_that_is_not_there_is_missing_with_its_reason(self):
        fl = flows()
        del fl["dealer_gamma"], fl["cta"]
        rows = self.rows(fl=fl)
        self.assertEqual((rows["dealer_gamma"]["status"], rows["dealer_gamma"]["value"]), ("missing", None))
        self.assertIn("option chain", rows["dealer_gamma"]["reason"])
        self.assertEqual(rows["cta"]["status"], "missing")
        self.assertEqual(rows["vol_control"]["status"], "ACTIVE")

    def test_no_flows_block_is_all_missing(self):
        fs = R.build(inputs(fl={"status": "missing", "reason": "insufficient history"}))["forced_selling"]
        self.assertEqual(fs["status"], "missing")
        self.assertTrue(all(r["status"] == "missing" and "insufficient history" in r["reason"] for r in fs["rows"]))


class Playbook(unittest.TestCase):
    def pb(self, **kw):
        return {p["key"]: p["status"] for p in R.build(inputs(**kw))["playbook"]}

    def test_conditions_fire_or_wait(self):
        self.assertEqual(self.pb(), {"tranche1": "waiting", "tranche2": "waiting", "tranche3": "missing", "stop": "waiting"})
        calm = self.pb(vix=24.0, vix3m=26.0, chaos=45.0, oas=3.6)
        self.assertEqual((calm["tranche1"], calm["tranche2"], calm["stop"]), ("FIRED", "FIRED", "waiting"))
        self.assertEqual(self.pb(chaos=45.0, oas=4.1)["tranche2"], "waiting")                   # chaos is low but credit is not
        self.assertEqual(self.pb(oas=4.6)["stop"], "FIRED")
        self.assertEqual(self.pb(oas=4.5)["stop"], "waiting")                                   # strictly above 4.5

    def test_third_tranche_by_retest_or_by_etf_flows(self):
        base = [100.0] * 5 + [90.0] + [95.0] * 3 + [96.0] * 21                                  # 30 closes, low 90 is 24 sessions old, rebound 6.7%
        self.assertEqual(R.retest(base)[0], False)                                              # last close 96 is 6.7% above the low: not a retest
        retest = [100.0] * 5 + [90.0] + [95.0] * 20 + [91.0] * 4                                # back within 1.1% of the low, no new low
        self.assertTrue(R.retest(retest)[0])
        self.assertFalse(R.retest(retest[:-1] + [89.0])[0])                                     # a new low is not a retest
        self.assertIsNone(R.retest([1.0] * 10)[0])
        self.assertEqual(self.pb(closes=retest)["tranche3"], "FIRED")
        flows_up = {"status": "fresh", "flows": {"ibit": {"status": "fresh", "sum_5d_usd": 250e6}}}
        self.assertEqual(self.pb(closes=base, btc=flows_up)["tranche3"], "FIRED")
        flows_dn = {"status": "fresh", "flows": {"ibit": {"status": "fresh", "sum_5d_usd": -250e6}}}
        self.assertEqual(self.pb(closes=base, btc=flows_dn)["tranche3"], "waiting")
        thin = {"status": "fresh", "flows": {"ibit": {"status": "fresh", "sum_5d_usd": None, "stored_days": 1}}}
        self.assertEqual(self.pb(closes=base, btc=thin)["tranche3"], "waiting")                 # one side false, the other unknown: not FIRED, and the detail says why
        self.assertIn("IBIT flows need 2+", next(p for p in R.build(inputs(closes=base, btc=thin))["playbook"] if p["key"] == "tranche3")["detail"])
        self.assertEqual(self.pb(closes=[1.0] * 10, btc=thin)["tranche3"], "missing")           # both unknown


class TheRead(unittest.TestCase):
    GOOD = "A volatility shock with calm credit. Vol-control funds sold an estimated 27.0B over five sessions and dealers are short gamma. A VIX/VIX3M ratio under 1 would ease it."

    def block(self):
        return R.build(inputs())

    def test_local_model_first_and_its_name_is_stored(self):
        calls = []
        local = lambda s, u, mt, to: calls.append(("local", mt, to)) or self.GOOD
        r = R.write_read(self.block(), local, "gemma-test", lambda *a: self.fail("claude must not be called"), now=datetime(2026, 11, 20, tzinfo=timezone.utc))
        self.assertEqual((r["status"], r["engine"], r["model"]), ("fresh", "local", "gemma-test"))
        self.assertEqual(calls[0][1], 260)                                                      # capped tokens
        self.assertEqual(r["generated_at"], "2026-11-20T00:00:00+00:00")

    def test_falls_back_to_sonnet_when_the_local_model_fails_or_invents_a_number(self):
        def down(*a):
            raise RuntimeError("local model not reachable")
        claude = lambda s, u, mt, to: (self.GOOD, 0.02)
        r = R.write_read(self.block(), down, "gemma-test", claude)
        self.assertEqual((r["engine"], r["model"], r["cost_usd"]), ("claude", "sonnet", 0.02))
        invented = lambda *a: "The VIX is 77.7 and vol-control funds sold 27.0B while dealers sit short gamma, so the curve matters."
        r = R.write_read(self.block(), invented, "gemma-test", claude)
        self.assertEqual(r["engine"], "claude")

    def test_both_failing_is_missing_with_both_reasons(self):
        def down(*a):
            raise RuntimeError("nope")
        r = R.write_read(self.block(), down, "m", down)
        self.assertEqual(r["status"], "missing")
        self.assertIn("local: nope", r["reason"])
        self.assertIn("claude: nope", r["reason"])
        r = R.write_read(self.block(), None, "m", None)
        self.assertIn("local: not configured", r["reason"])

    def test_the_facts_carry_the_forced_rows_and_the_playbook(self):
        f = R.facts(self.block())
        self.assertIn("Regime: VOLATILITY SHOCK, NO CREDIT/RECESSION CONFIRMATION", f)
        self.assertIn("Forced flow, Vol-control funds (est.): SELL, ACTIVE", f)
        self.assertIn("Playbook, STOP", f)


class InThePipeline(unittest.TestCase):
    def test_the_collect_stage_never_raises_and_skips_the_model_offline(self):
        from core import collect
        called = []
        orig = R.write_read
        R.write_read = lambda *a, **k: called.append(1)
        try:
            parts = {k: inputs()[k] for k in ("prices", "forecast", "chaos", "fed_ops", "bitcoin")}
            blk = collect._guarded_regime(None, SimpleNamespace(offline=True), "2026-11-20", parts, {})
            self.assertEqual((blk["status"], blk["key"]), ("fresh", "VOL_SHOCK"))
            self.assertEqual(blk["read"]["status"], "missing")
            self.assertIn("offline", blk["read"]["reason"])
            self.assertEqual(called, [])
            bad = collect._guarded_regime(None, SimpleNamespace(offline=True), "2026-11-20", {"prices": 5}, {})
            self.assertIn(bad["status"], ("error", "missing"))
        finally:
            R.write_read = orig


class Routes(ApiCase):
    def commit(self, blk):
        from core import lake
        conn = lake.connect(self.tmp / "portfolio.db")
        lake.migrate(conn)
        lake.create_run(conn, "r1", mode="live", trigger="manual", run_folder="Nov-20-26", code_version="t")
        lake.commit_snapshot(conn, "r1", {"run": {"run_id": "r1"}, **({"regime": blk} if blk is not None else {})})
        lake.update_run(conn, "r1", status="completed")
        conn.close()

    def test_503_then_404_without_a_block(self):
        self.assertEqual(self.client.get("/api/regime").status_code, 503)
        self.commit(None)
        self.assertEqual(self.client.get("/api/regime").status_code, 404)

    def test_the_stored_block(self):
        self.commit(R.build(inputs()))
        j = self.get_json("/api/regime")
        self.assertEqual((j["status"], j["data"]["key"], j["as_of"]), ("success", "VOL_SHOCK", "2026-11-20"))
        self.assertEqual(j["data"]["forced_selling"]["rows"][0]["key"], "vol_control")

    def test_the_card_and_script_are_on_the_page(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertLess(html.index("R · MARKET REGIME"), html.index("0 · SIGNAL WATCH"))
        self.assertIn('id="fcR"', html)
        self.assertIn("/static/regime.js", html)

    def test_console_and_mcp_sources(self):
        import assistant, mcp_server
        for name in ("regime", "trackers"):
            self.assertIn(name, assistant.SOURCES)
            self.assertIn(name, mcp_server.SOURCES)
        self.assertEqual(assistant.SOURCES["regime"][0], "/api/regime")


if __name__ == "__main__":
    unittest.main()
