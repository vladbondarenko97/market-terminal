import support  # noqa: F401  (must come first: sets the offline environment)
import json
import sys
import unittest
from unittest import mock

from starlette.testclient import TestClient

sys.path.insert(0, str(support.ROOT))
import mcp_server  # noqa: E402

MARKET, BROKER = "m" * 24, "b" * 24
HEAD = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(client, token, method, params=None, id=1):
    r = client.post(f"/{token}/mcp", headers=HEAD, content=json.dumps({"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}))
    return r


class McpTests(unittest.TestCase):
    def client(self, **env):
        with mock.patch.dict("os.environ", env):
            return TestClient(mcp_server.build_app())

    def tools(self, c, token):
        init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
        rpc(c, token, "initialize", init)
        return [t["name"] for t in rpc(c, token, "tools/list", id=2).json()["result"]["tools"]]

    def test_each_token_gets_only_its_tools_and_bad_paths_404(self):
        with self.client(MCP_MARKET_TOKEN=MARKET, MCP_BROKERAGE_TOKEN=BROKER) as c:
            self.assertEqual(self.tools(c, MARKET), ["list_sources", "get_source", "search_docs", "read_doc", "calculate"])
            self.assertEqual(self.tools(c, BROKER), ["get_brokerage_positions"])
            self.assertEqual(rpc(c, "x" * 24, "tools/list").status_code, 404)
            self.assertEqual(c.post("/%s/other" % MARKET, headers=HEAD, content="{}").status_code, 404)
            self.assertEqual(c.post("/caf\u00e9/mcp", headers=HEAD, content="{}").status_code, 404)

    def test_bad_token_configs_stop_the_server(self):
        for env in ({"MCP_MARKET_TOKEN": "short"}, {"MCP_MARKET_TOKEN": MARKET, "MCP_BROKERAGE_TOKEN": MARKET}, {}):
            with self.subTest(env=env), self.assertRaises(SystemExit):
                self.client(**{"MCP_MARKET_TOKEN": "", "MCP_BROKERAGE_TOKEN": "", **env})

    def test_every_request_is_logged_without_the_token(self):
        import tempfile
        from pathlib import Path
        log = Path(tempfile.mkdtemp()) / "calls.jsonl"
        fake = mock.Mock(status_code=200, content_type="application/json", get_data=lambda as_text=True: '{"status":"success","data":{"spot":1}}')
        with mock.patch.object(mcp_server, "LOG_PATH", log), mock.patch.object(mcp_server, "_denied", {"at": 0.0, "n": 0}), \
             mock.patch.object(mcp_server._Terminal, "get", return_value=fake), \
             self.client(MCP_MARKET_TOKEN=MARKET, MCP_BROKERAGE_TOKEN=BROKER) as c:
            rpc(c, MARKET, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
            rpc(c, MARKET, "tools/call", {"name": "get_source", "arguments": {"source": "gex", "query": "ticker=SPY"}}, id=2)
            rpc(c, MARKET, "tools/call", {"name": "get_source", "arguments": {"source": "nope"}}, id=3)
            rpc(c, BROKER, "tools/list", id=4)
            c.post("/" + "z" * 24 + "/mcp", headers={**HEAD, "X-Forwarded-For": "9.9.9.9, 1.1.1.1", "User-Agent": "scanner/1"}, content="{}")
            for i in range(5):   # a changing forwarded address does not get a fresh allowance
                c.post("/" + "y" * 24 + "/mcp", headers={**HEAD, "X-Forwarded-For": f"8.8.8.{i}"}, content="{}")
            rpc(c, MARKET, "tools/call", {"name": "get_source", "arguments": {"source": "gex"}}, id=5)
        import time as _t; _t.sleep(0.2)   # the writes run on a worker thread
        rows = [json.loads(l) for l in log.read_text().splitlines()]
        self.assertEqual([(r["profile"], r.get("rpc"), r.get("tool")) for r in rows if r["profile"] != "denied"],
                         [("market", "initialize", None), ("market", "tools/call", "get_source"), ("market", "tools/call", "get_source"), ("brokerage", "tools/list", None), ("market", "tools/call", "get_source")])
        self.assertIn("ticker=SPY", rows[1]["args"])
        self.assertTrue(rows[0]["ok"] and rows[1]["ok"]); self.assertFalse(rows[2]["ok"])   # unknown source: error text
        self.assertGreaterEqual(rows[1]["ms"], 0)
        denied = [r for r in rows if r["profile"] == "denied"]
        self.assertEqual((len(denied), denied[0]["ip"], denied[0]["ua"]), (1, "9.9.9.9", "scanner/1"))
        self.assertNotIn("path_len", text_all := log.read_text())
        text = log.read_text()
        self.assertNotIn(MARKET, text); self.assertNotIn(BROKER, text); self.assertNotIn("zzzz", text)

    def test_summary_survives_hostile_bodies_and_logs_batches(self):
        f = mcp_server._summary
        self.assertEqual(f(b'{"method":"tools/call","params":[1]}', b"{}", 200)["rpc"], "tools/call")   # params not a dict: no tool, no crash
        self.assertEqual(f(b"[1,2]", b"x", 200)["rpc"], "unparsed")
        batch = json.dumps([{"method": "tools/call", "params": {"name": "a", "arguments": {}}}, {"method": "tools/call", "params": {"name": "b", "arguments": {"q": 1}}}]).encode()
        resp = json.dumps([{"result": {"content": [{"text": "fine"}]}}, {"result": {"content": [{"text": "  error: boom"}]}}]).encode()
        out = f(batch, resp, 200)
        self.assertEqual((out["rpc"], out["tool"], out["ok"]), ("batch", "a,b", False))   # one failed item fails the batch
        self.assertTrue(f(b"{}", b"x" * 10, 200, cut=True)["cut"])

    def test_one_endpoint_can_be_off(self):
        with self.client(MCP_MARKET_TOKEN=MARKET, MCP_BROKERAGE_TOKEN="") as c:
            self.assertEqual(self.tools(c, MARKET), ["list_sources", "get_source", "search_docs", "read_doc", "calculate"])
            self.assertEqual(rpc(c, "b" * 24, "tools/list").status_code, 404)

    def test_brokerage_tool_reports_an_unreachable_terminal(self):
        with self.client(MCP_BROKERAGE_TOKEN=BROKER) as c, mock.patch.object(mcp_server.requests, "get", side_effect=mcp_server.requests.ConnectionError("http://127.0.0.1:80 down")):
            rpc(c, BROKER, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
            out = json.dumps(rpc(c, BROKER, "tools/call", {"name": "get_brokerage_positions", "arguments": {}}, id=4).json())
        self.assertIn("ConnectionError", out)
        self.assertNotIn("127.0.0.1", out)

    def test_get_source_reuses_console_fetch(self):
        fake = mock.Mock(status_code=200, content_type="application/json", get_data=lambda as_text=True: '{"status":"success","data":{"spot":1.5}}')
        with self.client(MCP_MARKET_TOKEN=MARKET) as c, mock.patch.object(mcp_server._Terminal, "get", return_value=fake) as g:
            rpc(c, MARKET, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
            out = rpc(c, MARKET, "tools/call", {"name": "get_source", "arguments": {"source": "gex", "query": "ticker=SPY"}}, id=3).json()
        self.assertIn("1.5", json.dumps(out))
        self.assertEqual(g.call_args.args[0], "/api/gex")
        self.assertEqual(g.call_args.kwargs["query_string"], {"ticker": "SPY"})

    def fixture_root(self):
        import tempfile
        from pathlib import Path
        root = Path(tempfile.mkdtemp())
        (root / "docs").mkdir(); (root / "core").mkdir()
        (root / "docs" / "t.md").write_text("# Top\nintro\n## Set up\n```bash\n# a shell comment\n```\nafter fence\n## Other\nx\n## Setup two\ny\n")
        (root / "core" / "metrics.py").write_text("X = 1\n\n@deco\ndef compute_vmri(a):\n    return a\n\ndef compute_vmri_history():\n    pass\n\nCFG: dict = {'k': 1}\n\nOTHER = 2\n")
        (root / "core" / "brokerage.py").write_text("SECRET_FLOW = 1\n")
        (root / "README.md").write_text("# R\nVMRI formula here\n")
        return root

    def test_knowledge_tools(self):
        with mock.patch.object(mcp_server, "ROOT", self.fixture_root()):
            files = mcp_server.doc_files()
            self.assertEqual(sorted(files), ["README.md", "core/metrics.py", "docs/t.md"])   # brokerage.py and everything else is out
            self.assertIn("README.md:2", mcp_server.search_text("vmri FORMULA"))
            self.assertEqual(mcp_server.search_text("zzz"), "no matches")
            self.assertEqual(mcp_server.search_text(""), "error: give a search query")
            sec = mcp_server.read_doc_text("docs/t.md", "set up")
            self.assertIn("after fence", sec); self.assertIn("# a shell comment", sec); self.assertNotIn("x", sec.split("after fence")[1])
            fn = mcp_server.read_doc_text("core/metrics.py", "compute_vmri")   # exact name beats compute_vmri_history; decorator included
            self.assertIn("@deco", fn); self.assertNotIn("history", fn)
            self.assertIn("CFG", mcp_server.read_doc_text("core/metrics.py", "CFG"))
            self.assertIn("matches 2 sections", mcp_server.read_doc_text("docs/t.md", "set"))
            self.assertIn("Sections:", mcp_server.read_doc_text("docs/t.md", "nope"))
            for bad in ("../.env", "/etc/passwd", "config.py", ".env", "docs/../.env", "core/brokerage.py"):
                self.assertTrue(mcp_server.read_doc_text(bad).startswith("error: name must be one of"), bad)

    def test_real_repo_knowledge_smoke(self):
        self.assertIn("core/metrics.py", " ".join(mcp_server.doc_files()))
        self.assertNotIn("core/brokerage.py", mcp_server.doc_files())
        self.assertIn("VMRI", mcp_server.search_text("vmri"))

    def test_extra_sources_only_in_the_mcp_copy(self):
        for name in ("paper_physical", "shanghai_spread", "chaos_history"):
            self.assertIn(name, mcp_server.SOURCES)
            self.assertNotIn(name, mcp_server.assistant.SOURCES)
        self.assertEqual(mcp_server.SOURCES["chaos_history"][1], ("from",))
        self.assertIn("brokerage", mcp_server.assistant.SOURCES)       # the console reads holdings; the public market link must not
        self.assertNotIn("brokerage", mcp_server.SOURCES)
        self.assertIn("chaos_history", mcp_server.assistant.catalog_text(mcp_server.SOURCES))
        self.assertTrue(mcp_server.assistant.fetch_source(mcp_server._APP, "paper_physical", sources={}).startswith("error"))

    def test_calculate_thousands_separators(self):
        import asyncio
        fn = mcp_server.market_server()
        res = asyncio.run(fn.call_tool("calculate", {"expression": "round(1,000*2)"}))
        self.assertIn("2000", str(res))

    def test_unknown_source_is_an_error_text_not_a_request(self):
        with mock.patch.object(mcp_server._Terminal, "get") as g:
            text = mcp_server.assistant.fetch_source(mcp_server._APP, "brokerage", sources=mcp_server.SOURCES)
        self.assertTrue(text.startswith("error"))
        g.assert_not_called()


if __name__ == "__main__":
    unittest.main()
