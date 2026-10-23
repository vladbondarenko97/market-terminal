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

    def test_fetch_with_mocked_calls_and_bad_item(self):
        accts = [{"id": "a1", "name": "IRA", "institution_name": "B"}]
        stock = {"symbol": {"symbol": {"symbol": "AAPL"}}, "units": "2", "price": "10"}
        bad_leg = {"symbol": {"option_symbol": {"underlying_symbol": {"symbol": "SPY"}, "strike_price": "x", "expiration_date": "2026-12-18"}}}
        leg = lambda k, u: {"symbol": {"option_symbol": {"underlying_symbol": {"symbol": "SPY"}, "option_type": "CALL",
                                                        "strike_price": k, "expiration_date": "2026-12-18"}}, "units": u, "price": 1}
        def fake(method, path, creds, body=None):
            return accts if path.endswith("accounts") else [stock] if path.endswith("positions") else [bad_leg, leg(500, 1), leg(510, -1)]
        with mock.patch.object(brokerage, "_call", fake):
            r = brokerage.fetch({})
        self.assertEqual(r["stocks"][0]["value"], 20.0)
        self.assertEqual([(g["kind"], g["account"]) for g in r["options"]], [("CALL DEBIT SPREAD", "IRA")])

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
