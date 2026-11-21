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

    def test_congress_and_companies_fields_hold_the_contract_and_are_optional(self):
        d = json.loads(FIXTURE.read_text())
        cd = d["congress_detail"]
        self.assertEqual({"member", "ticker", "amount_low", "amount_high", "date", "filed"} - set(cd["big_sales_30d"][0]), set())
        for o in cd["overlaps"]:
            self.assertEqual({"ticker", "members", "direction", "n"} - set(o), set())
            self.assertIn(o["direction"], ("buy", "sell", "mixed"))
        c = d["companies"][0]
        self.assertEqual({"slug", "name", "ticker", "cik", "as_of", "market_cap", "shares_outstanding", "price", "price_as_of", "treasury", "top_holders",
                          "insider_trades", "sources", "completeness"} - set(c), set())
        self.assertEqual({"asset", "amount", "value_est", "as_of", "source_url"} - set(c["treasury"][0]), set())
        self.assertEqual({"name", "shares", "pct", "change_shares", "as_of", "form"} - set(c["top_holders"][0]), set())
        # served untouched, and a file without them still serves (the views hide: the JS tests the keys)
        self.put()
        self.assertEqual(self.client.get("/api/people").get_json()["data"]["companies"][0]["ticker"], "SMG")
        del d["congress_detail"], d["companies"]
        (self.tmp / "people_portfolios.json").write_text(json.dumps(d))
        data = self.client.get("/api/people").get_json()["data"]
        self.assertNotIn("companies", data)
        self.assertNotIn("congress_detail", data)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_views_show_only_when_their_fields_exist_and_render_the_fixture(self):
        script = """
const fs = require('fs'); const vm = require('vm');
const d = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const els = {};
const el = id => els[id] = els[id] || { innerHTML: '', value: '', classList: { toggle(){}, add(){}, remove(){} }, dataset: {} };
const ctx = { document: { getElementById: el, querySelectorAll: () => [], addEventListener(){} }, window: {}, console, localStorage: {},
  fcEsc: s => String(s ?? ''), fcNum: (v, n) => Number(v).toFixed(n), fcPct: v => v + '%', fcCls: () => '', kpi: (l, v) => l + ':' + v };
vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[2], 'utf8') + ';this.F = { pplHasCong, pplHasComp, pplCongress, pplCompanies };', ctx);
const out = {};
out.has = [ctx.F.pplHasCong(d), ctx.F.pplHasComp(d)];
const bare = Object.assign({}, d); delete bare.congress_detail; delete bare.companies;
out.bare = [ctx.F.pplHasCong(bare), ctx.F.pplHasComp(bare)];
ctx.F.pplCongress(d); out.cong = els.pplCong.innerHTML;
ctx.F.pplCompanies(d); out.comp = els.pplComp.innerHTML;
ctx.F.pplCongress(bare); ctx.F.pplCompanies(bare); out.empty = [els.pplCong.innerHTML, els.pplComp.innerHTML];
console.log(JSON.stringify(out));
"""
        r = subprocess.run(["node", "-e", script, str(FIXTURE), str(JS)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        o = json.loads(r.stdout)
        self.assertEqual((o["has"], o["bare"], o["empty"]), ([True, True], [False, False], ["", ""]))
        for needle in ("RECENT BIG SALES", "Sample Senator", "MIXED", 'data-member="Sample Senator"', "5.00M", "OVERLAPS"):
            self.assertIn(needle, o["cong"])
        self.assertLess(o["cong"].index("AAPL"), o["cong"].index("TSLA</a></td><td>$100K"))   # big sales: highest amount_high first
        for needle in ("Sample Media Group", "bitcoin", "$1.00B", "Sample Founder Trust", "40.70%", "-500000", "SEC EDGAR", "Fixture: treasury"):
            self.assertIn(needle, o["comp"])

    def test_the_page_has_the_people_tab_right_after_bitcoin(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertLess(html.index('id="tab-bitcoin"'), html.index('id="tab-people"'))
        for needle in ('id="people-tab"', "/static/people.js", "https://search.vlad.yt/people", 'id="pplSearch"', 'data-kind="congress"'):
            self.assertIn(needle, html)
        for needle in ('data-view="congress"', 'data-view="companies"', 'id="pplCong"', 'id="pplComp"'):
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
