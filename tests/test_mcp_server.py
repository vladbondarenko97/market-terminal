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
            self.assertEqual(self.tools(c, MARKET), ["list_sources", "get_source"])
            self.assertEqual(self.tools(c, BROKER), ["get_brokerage_positions"])
            self.assertEqual(rpc(c, "x" * 24, "tools/list").status_code, 404)
            self.assertEqual(c.post("/%s/other" % MARKET, headers=HEAD, content="{}").status_code, 404)
            self.assertEqual(c.post("/caf\u00e9/mcp", headers=HEAD, content="{}").status_code, 404)

    def test_bad_token_configs_stop_the_server(self):
        for env in ({"MCP_MARKET_TOKEN": "short"}, {"MCP_MARKET_TOKEN": MARKET, "MCP_BROKERAGE_TOKEN": MARKET}, {}):
            with self.subTest(env=env), self.assertRaises(SystemExit):
                self.client(**{"MCP_MARKET_TOKEN": "", "MCP_BROKERAGE_TOKEN": "", **env})

    def test_one_endpoint_can_be_off(self):
        with self.client(MCP_MARKET_TOKEN=MARKET, MCP_BROKERAGE_TOKEN="") as c:
            self.assertEqual(self.tools(c, MARKET), ["list_sources", "get_source"])
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

    def test_unknown_source_is_an_error_text_not_a_request(self):
        with mock.patch.object(mcp_server._Terminal, "get") as g:
            text = mcp_server.assistant.fetch_source(mcp_server._APP, "brokerage")
        self.assertTrue(text.startswith("error"))
        g.assert_not_called()


if __name__ == "__main__":
    unittest.main()
