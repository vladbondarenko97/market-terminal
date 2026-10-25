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


if __name__ == "__main__":
    unittest.main()
