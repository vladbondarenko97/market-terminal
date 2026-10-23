import support  # noqa: F401  (must come first: sets the offline environment)
import unittest
from unittest import mock

from core import brokerage


def opt(under, typ, strike, units, price=1.0, exp="2026-12-18"):
    return {"multiplier": 100, "underlying": under, "type": typ, "strike": strike, "expiry": exp, "units": units, "price": price,
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

    def test_fetch_positions_all_with_bad_item(self):
        accts = [{"id": "a1", "name": "IRA", "institution_name": "B"}]
        stock = {"instrument": {"kind": "stock", "symbol": "AAPL"}, "units": "2", "price": "10", "cost_basis": "8"}
        def o(strike, units, price, cost):
            return {"instrument": {"kind": "option", "option_type": "CALL", "strike_price": strike, "expiration_date": "2026-12-18",
                                   "multiplier": 100, "underlying": {"symbol": "SPY"}}, "units": units, "price": price, "cost_basis": cost}
        bad = o("x", "1", "1", "1")
        def fake(method, path, creds, body=None):
            return accts if path.endswith("accounts") else {"results": [stock, bad, o("500", "1", "3", "2"), o("510", "-1", "1", "0.5")]}
        with mock.patch.object(brokerage, "_call", fake):
            r = brokerage.fetch({})
        self.assertEqual((r["stocks"][0]["value"], r["stocks"][0]["open_pnl"]), (20.0, 4.0))
        g = r["options"][0]
        self.assertEqual((g["kind"], g["account"], g["net_mark"], g["open_pnl"]), ("CALL DEBIT SPREAD", "IRA", 200.0, 50.0))

    def test_errors_are_cached_briefly(self):
        err = mock.Mock(status_code=401)
        with mock.patch.dict("os.environ", {"SNAPTRADE_CLIENT_ID": "c", "SNAPTRADE_CONSUMER_KEY": "k"}), \
             mock.patch.object(brokerage, "fetch", side_effect=brokerage.requests.HTTPError(response=err)) as f:
            brokerage._cache.update(at=0.0, data=None)
            first = brokerage.positions(); brokerage.positions()
        self.assertEqual((first["status"], f.call_count), ("error", 1))
        self.assertIn("client id", first["reason"])


if __name__ == "__main__":
    unittest.main()
