import support  # noqa: F401  (must come first: sets the offline environment)
import unittest
from unittest import mock

from core import brokerage


def opt(under, typ, strike, units, price=1.0, exp="2026-12-18"):
    return {"underlying": under, "type": typ, "strike": strike, "expiry": exp, "units": units, "price": price,
            "avg_price": None, "ticker": None}


class BrokerageTests(unittest.TestCase):
    def test_sign_is_stable(self):
        a = brokerage.sign("key", "/api/v1/accounts", "clientId=c&timestamp=1")
        self.assertEqual(a, brokerage.sign("key", "/api/v1/accounts", "clientId=c&timestamp=1"))
        self.assertNotEqual(a, brokerage.sign("key", "/api/v1/accounts", "clientId=c&timestamp=2"))

    def test_classify(self):
        self.assertEqual(brokerage.classify([opt("SPY", "CALL", 500, 1), opt("SPY", "CALL", 510, -1)]), "CALL DEBIT SPREAD")
        self.assertEqual(brokerage.classify([opt("SPY", "CALL", 500, -1), opt("SPY", "CALL", 510, 1)]), "CALL CREDIT SPREAD")
        self.assertEqual(brokerage.classify([opt("SPY", "PUT", 500, -1), opt("SPY", "PUT", 510, 1)]), "PUT DEBIT SPREAD")
        self.assertEqual(brokerage.classify([opt("SPY", "PUT", 500, 1), opt("SPY", "PUT", 510, -1)]), "PUT CREDIT SPREAD")
        self.assertEqual(brokerage.classify([opt("SPY", "PUT", 500, 2)]), "LONG PUT")
        self.assertEqual(brokerage.classify([opt("SPY", "PUT", 500, 1), opt("SPY", "CALL", 500, 1)]), "LONG STRADDLE")

    def test_group_net_mark_and_missing_price(self):
        g = brokerage.group_options([opt("SPY", "CALL", 500, 1, 3.0), opt("SPY", "CALL", 510, -1, 1.0), opt("QQQ", "PUT", 400, 1, 2.0)])
        spread = next(x for x in g if x["underlying"] == "SPY")
        self.assertEqual((spread["width"], spread["net_mark"]), (10, 200.0))
        g = brokerage.group_options([opt("SPY", "CALL", 500, 1, None), opt("SPY", "CALL", 510, -1, 1.0)])
        self.assertIsNone(g[0]["net_mark"])

    def test_not_configured_has_steps(self):
        with mock.patch.dict("os.environ", {k: "" for k in brokerage.SETTINGS}):
            r = brokerage.positions()
        self.assertEqual(r["status"], "not_configured")
        self.assertEqual(r["missing"], list(brokerage.SETTINGS))
        self.assertTrue(r["setup"])

    def test_route_not_configured(self):
        import api_router
        r = api_router.app.test_client().get("/api/brokerage")
        self.assertEqual((r.status_code, r.get_json()["status"]), (200, "not_configured"))


if __name__ == "__main__":
    unittest.main()
