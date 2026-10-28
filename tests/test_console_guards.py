"""Console engine guards against answers given without fetching (seen with Qwen3.5-9B on 2026-10-24)."""
import os
import sys
import unittest
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "options_whale"))
import assistant  # noqa: E402


class ConsoleGuards(unittest.TestCase):
    def test_invented_number_is_flagged(self):
        shown = [{"role": "system", "content": "BRIEF Macro regime expansion, 13% recession odds, HY OAS 3.4"},
                 {"role": "user", "content": "whats the vmri rn"}]
        self.assertEqual(assistant.unsourced_numbers("The current VMRI score is 12.4, LOW RISK, 13% recession odds.", shown), ["12.4"])

    def test_numbers_from_the_brief_pass(self):
        shown = [{"role": "system", "content": "BRIEF VMRI 264.6 ELEVATED, VIX 19.74"}]
        self.assertEqual(assistant.unsourced_numbers("VMRI is 264.6 and VIX 19.74.", shown), [])

    def test_missing_data_claims_are_flagged(self):
        for text in ("Your brokerage is not configured.", "Your holdings are currently unavailable.", "I could not retrieve them."):
            self.assertTrue(assistant.UNFETCHED_CLAIM.search(text), text)
        self.assertFalse(assistant.UNFETCHED_CLAIM.search("You hold three option positions."))

    def test_my_holdings_route_to_brokerage_not_engine_tickets(self):
        # the model chose `positions` (engine tickets) for "my brokerage positions" on 2026-10-24
        self.assertIn("brokerage", assistant.SOURCES)
        self.assertIn("brokerage", assistant.PRIVATE_SOURCES)
        self.assertIn("Robinhood, spreads I own: use source brokerage, never positions", assistant.SYSTEM_PROMPT)
        self.assertIn("Not the engine tickets", assistant.SOURCES["brokerage"][2])
        self.assertIn("Engine positions", assistant.SOURCES["positions"][2])

    def test_vmri_twelve_from_the_signal_count_is_caught(self):
        # 2026-10-26: "The latest VMRI score is 12, which is in the Tier 2 range" while war_room said 255.72. The brief held no VMRI
        # but did hold "2 of 12 fired", so the number check passed; the answer still cited a source it never fetched.
        self.assertTrue(assistant.claims_unfetched_source("The latest VMRI score is 12.\n\nSources: brief, war_room", set()))
        self.assertFalse(assistant.claims_unfetched_source("The latest VMRI score is 255.72.\n\nSources: war_room", {"war_room"}))
        self.assertFalse(assistant.claims_unfetched_source("Nothing fired today.\n\n**Sources:** brief", set()))
        self.assertEqual(assistant.claims_unfetched_source("ON RRP is $809.4 billion.\n\nSources: guide, repo_ops", {"guide"}), ["repo_ops"])

    def test_shanghai_reading_states_the_vat_adjusted_gap(self):
        latest = {"date": "2026-10-22", "session": "evening", "shanghai": 61.02, "comex": 54.76, "raw_dollar": 6.26, "raw_pct": 11.43,
                  "adj_dollar": -0.76, "adj_pct": -1.39}
        out = assistant._shape_shanghai({"data": {"dated": {"status": "fresh", "vat": 0.13, "latest": latest, "stats": {}}}}, {})
        self.assertIn("$6.26 (11.43%) above COMEX before tax", out["reading"])
        self.assertIn("13% VAT taken out, Shanghai was $0.76 (1.39%) below COMEX", out["reading"])
        self.assertEqual(assistant._shape_shanghai({"data": {"dated": None}}, {})["status"], "missing")
        series = {"dates": ["2026-10-21", "2026-10-22"], "shanghai": [63.37, 61.02], "comex": [57.42, 54.76], "adj_pct": [-2.34, -1.39],
                  "raw_pct": [10.35, 11.43]}
        out = assistant._shape_shanghai({"data": {"dated": {"latest": latest, "series": series, "stats": {}}}}, {})
        self.assertIn("China's VAT taken out", out["reading"])                                            # no rate stored: none invented
        self.assertEqual(out["by_date"][-1], "2026-10-22: Shanghai $61.02, COMEX $54.76, VAT-adjusted -1.39%, raw +11.43%")

    def test_eia_reading_answers_days_of_supply_now_and_a_month_ago(self):
        """"how many days of supply left for diesel, SPR, and jet fuel? what was it at a month ago?" was answered from forecast SLV."""
        dates = [(date(2026, 10, 16) - timedelta(weeks=k)).isoformat() for k in range(39, -1, -1)]
        n = len(dates)
        obj = {"status": "success", "as_of": dates[-1], "dates": dates, "note": "weekly", "series": [
            {"key": "dist_stocks", "label": "Distillate / diesel", "mbbl": [100.0 + i for i in range(n)],
             "vs_5y_pct": [-9.0] * n, "days_of_supply": [20.0 + i / 10 for i in range(n)]},
            {"key": "jet_stocks", "label": "Jet fuel", "mbbl": [40.0] * n, "vs_5y_pct": [None] * n, "days_of_supply": [24.5] * n},
            {"key": "spr_stocks", "label": "Strategic Petroleum Reserve", "mbbl": [None] * 30 + [282.7] * 10, "vs_5y_pct": [None] * n}]}
        out = assistant._shape_eia(obj, {})
        self.assertEqual(out["week_ending"], dates[-1])
        dist = out["stocks"]["Distillate / diesel"]
        verdicts = out["terminal_verdicts_not_eia_ratings"]
        self.assertEqual(verdicts["Distillate / diesel"], "LOW for this time of year, 9.0% below the 5-year average")
        self.assertEqual(set(verdicts), {"rule", "Distillate / diesel"})                         # jet: no average; SPR: never
        self.assertTrue(dist[0].startswith("latest week"))
        self.assertEqual(dist[0], f"latest week ({dates[-1]}): 139.0 million barrels, 23.9 days of supply, -9.0% vs 5-year average")
        self.assertEqual(dist[2], f"a month ago (4 weeks) ({dates[-5]}): 135.0 million barrels, 23.5 days of supply, -9.0% vs 5-year average; "
                         "change since then +4.0 million barrels (+3.0%), +0.4 days")
        self.assertIn("a week ago", dist[1])
        self.assertEqual(out["stocks"]["Jet fuel"][2], f"a month ago (4 weeks) ({dates[-5]}): 40.0 million barrels, 24.5 days of supply; "
                         "change since then +0.0 million barrels (+0.0%), +0.0 days")
        spr = out["stocks"]["Strategic Petroleum Reserve"]
        self.assertNotIn("days of supply,", " ".join(spr))                                      # none invented for the SPR
        self.assertIn("EIA publishes none for the SPR", spr[-1])
        self.assertFalse(any("a year ago" in r for r in spr))                                     # before its first week: left out
        self.assertEqual(assistant._shape_eia({"status": "missing", "reason": "no EIA data"}, {})["status"], "missing")
        for pct, want in ((-4.96, "LOW for this time of year, 5.0% below"), (-4.94, "NORMAL for this time of year, 4.9% below"),
                          (5.0, "HIGH for this time of year, 5.0% above"), (2.0, "NORMAL for this time of year, 2.0% above"),
                          (-0.04, "NORMAL for this time of year, at the 5-year")):
            obj["series"][0]["vs_5y_pct"][-1] = pct
            self.assertTrue(assistant._shape_eia(obj, {})["terminal_verdicts_not_eia_ratings"]["Distillate / diesel"].startswith(want), pct)
        obj["series"][2]["vs_5y_pct"] = [-34.6] * n
        self.assertNotIn("Strategic Petroleum Reserve", assistant._shape_eia(obj, {})["terminal_verdicts_not_eia_ratings"])
        gap = dict(obj, dates=dates[:-1] + ["2026-10-17"])                                       # an extra date: still 4 calendar weeks
        self.assertIn(f"({dates[-5]})", assistant._shape_eia(gap, {})["stocks"]["Jet fuel"][2])
        obj["series"][1]["mbbl"][-5] = 0.0                                                      # a zero reading: no division
        self.assertIn("change since then +40.0 million barrels,", assistant._shape_eia(obj, {})["stocks"]["Jet fuel"][2])

    def test_what_is_questions_get_the_guide_entry(self):
        app = mock.Mock()
        with mock.patch.object(assistant, "fetch_source", return_value='{"sections":[{"heading":"Reverse repo"}]}') as f:
            self.assertIn("Reverse repo", assistant.guide_entry(app, "what is a reverse repo program?"))
            self.assertIn("reverse repo", f.call_args[0][2])
            self.assertIn("Reverse repo", assistant.guide_entry(app, "Explain bid to cover like I'm new"))
            self.assertIsNone(assistant.guide_entry(app, "How much is in the reverse repo today?"))
            self.assertIsNone(assistant.guide_entry(app, "What is the reverse repo balance today?"))      # live data, not a definition
            self.assertIsNone(assistant.guide_entry(app, "What do you think silver does this week?"))
            self.assertIsNotNone(assistant.guide_entry(app, "what is the VMRI?"))
            assistant.guide_entry(app, "What does it mean for my silver stack when the paper:physical ratio rises?")
            self.assertIn("paper physical ratio", f.call_args[0][2])                                     # filler dropped before the cut
        with mock.patch.object(assistant, "fetch_source", return_value='{"status":"missing"}'):
            self.assertIsNone(assistant.guide_entry(app, "what is zzz?"))

    def test_an_announced_answer_is_not_an_answer(self):
        self.assertTrue(assistant.unfinished("Now I have the comparison. Let me provide the answer.\n\nSources: treasury_buybacks"))
        self.assertTrue(assistant.unfinished("The user is asking for a comparison of buybacks. From the data I fetched: ..."))
        notes = "Now I have the data I need. Let me analyze the 30-Year Bond data.\n" + "- bid_to_cover_12m: 2.43\n" * 30 + "\nLet me provide a clear answer.\n\nSources: auction_history"
        self.assertTrue(assistant.unfinished(notes))
        offer = "2026 so far: **$230.235 billion**.\n" * 20 + "If you want, I'll compare this with 2024.\n\nSources: treasury_buybacks"
        self.assertFalse(assistant.unfinished(offer))
        self.assertFalse(assistant.unfinished("2026 so far: **$230.235 billion**; 2025: **$195.782 billion**.\n\nSources: treasury_buybacks"))

    def test_war_room_hides_the_chart_histogram(self):
        live = {"current": {"vmri": 255.72, "tier": "ELEVATED RISK"}, "hypothetical": {"vmri": 255.72}, "impact": {"vmri_delta": 0},
                "history": {"counts": [0, 11, 19], "lo": 130, "hi": 360, "min": 138.3, "max": 345.7, "pct_below_current": 83.7,
                            "pct_below_hypothetical": 83.7}}
        out = assistant._shape_war_room(live, {})
        self.assertEqual(set(out["history"]), {"min", "max", "pct_of_readings_below_current"})     # bin edges 130/360 were read as the record range
        self.assertNotIn("hypothetical", out)
        self.assertIn("hypothetical", assistant._shape_war_room(live, {"vix_shift_pct": "10"}))

    def test_prompt_forbids_substituting_the_latest_value_for_a_past_date(self):
        self.assertIn("no recorded value for that date", assistant.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
