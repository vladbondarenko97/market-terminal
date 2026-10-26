"""Console engine guards against answers given without fetching (seen with Qwen3.5-9B on 2026-10-24)."""
import os
import sys
import unittest

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
        self.assertTrue(assistant.claims_unfetched_source("The latest VMRI score is 12.\n\nSources: brief, war_room", 0))
        self.assertFalse(assistant.claims_unfetched_source("The latest VMRI score is 255.72.\n\nSources: war_room", 1))
        self.assertFalse(assistant.claims_unfetched_source("Nothing fired today.\n\n**Sources:** brief", 0))

    def test_an_announced_answer_is_not_an_answer(self):
        self.assertTrue(assistant.unfinished("Now I have the comparison. Let me provide the answer.\n\nSources: treasury_buybacks"))
        self.assertTrue(assistant.unfinished("The user is asking for a comparison of buybacks. From the data I fetched: ..."))
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
