"""PEOPLE tab: /api/people serves people_portfolios.json (an input written by an external job) or answers missing, the tab is in the page,
the console and MCP source shapes it, and the fixture holds the contract. Offline, fixed dates."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401  (temporary CME_Data, no credentials; must come before importing config)

import json
import shutil
import subprocess

from test_api import ApiCase  # noqa: E402  (also puts options_whale on sys.path)
import assistant  # noqa: E402
import mcp_server  # noqa: E402

FIXTURE = support.FIX / "people" / "people_portfolios.json"
JS = support.ROOT / "options_whale" / "static" / "people.js"


class People(ApiCase):
    def put(self):
        shutil.copy(FIXTURE, self.tmp / "people_portfolios.json")

    def test_missing_file_is_404_missing_never_data(self):
        r = self.client.get("/api/people")
        j = r.get_json()
        self.assertEqual((r.status_code, j["status"]), (404, "missing"))
        self.assertIn("people_portfolios.json", j["reason"])
        self.assertNotIn("data", j)

    def test_unreadable_file_is_reported_not_invented(self):
        (self.tmp / "people_portfolios.json").write_text("{not json")
        r = self.client.get("/api/people")
        self.assertEqual((r.status_code, r.get_json()["status"]), (502, "missing"))

    def test_serves_the_fixture_and_filters_by_person(self):
        self.put()
        j = self.client.get("/api/people").get_json()
        self.assertEqual(j["status"], "success")
        self.assertEqual(j["as_of"], "2026-11-20T21:30:00+00:00")
        self.assertEqual([p["slug"] for p in j["data"]["people"]], ["sample-fund", "sample-member", "musk"])
        self.assertEqual(j["data"]["congress_summary"]["members_trading_30d"], 2)
        one = self.client.get("/api/people?person=MEMBER").get_json()["data"]
        self.assertEqual([p["slug"] for p in one["people"]], ["sample-member"])
        self.assertEqual(self.client.get("/api/people?person=nobody").status_code, 404)

    def test_the_fixture_holds_the_contract(self):
        d = json.loads(FIXTURE.read_text())
        self.assertEqual(d["version"], 1)
        keys = {"slug", "name", "role", "kind", "completeness", "totals", "value_history", "holdings", "transactions", "sources"}
        for p in d["people"]:
            self.assertEqual(keys - set(p), set())
            self.assertIn(p["kind"], ("sec", "congress"))
            self.assertEqual({"value_est", "value_low", "value_high", "n_positions", "change_1d_pct", "change_1m_pct", "change_ytd_pct", "vs_spy_since_start_pct"} - set(p["totals"]), set())
            for t in p["transactions"]:
                self.assertLessEqual(t["filed"], d["as_of"][:10])
        self.assertEqual({p["kind"] for p in d["people"]}, {"sec", "congress"})

    def test_the_page_has_the_people_tab_right_after_bitcoin(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertLess(html.index('id="tab-bitcoin"'), html.index('id="tab-people"'))
        for needle in ('id="people-tab"', "/static/people.js", "https://search.vlad.yt/people", 'id="pplSearch"', 'data-kind="congress"'):
            self.assertIn(needle, html)
        self.assertIn("/api/people", self.client.get("/help").get_data(as_text=True))

    def test_console_and_mcp_source(self):
        self.assertIn("people", assistant.SOURCES)
        self.assertIn("people", mcp_server.SOURCES)
        self.assertNotIn("people", assistant.PRIVATE_SOURCES)
        self.put()
        obj = self.client.get("/api/people").get_json()
        rows = assistant.SHAPERS["people"](obj, {})["rows"]
        text = "\n".join(rows)
        self.assertIn("estimated from disclosure ranges", text)
        self.assertIn("trade 2026-10-15 (filed 2026-11-19) BUY NVDA: $250,001-$500,000, spouse", text)
        self.assertIn("13F-HR", text)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_people_js_is_valid_javascript(self):
        r = subprocess.run(["node", "--check", str(JS)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
