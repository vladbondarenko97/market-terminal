"""MCP server for the terminal: lets Claude chat, ChatGPT and other MCP clients read its data. Standalone, not part of the pipeline.

Two endpoints, each behind its own secret path segment, so holdings can be shared with fewer clients than market data:
    /<MCP_MARKET_TOKEN>/mcp      list_sources, get_source  (the console's read-only sources; same shaping, see options_whale/assistant.py)
    /<MCP_BROKERAGE_TOKEN>/mcp   get_brokerage_positions   (live SnapTrade holdings)
A blank token switches that endpoint off. Writes are not exposed: only GETs of the whitelisted sources are made.
Run: .venv/bin/python mcp_server.py   (listens on 127.0.0.1:MCP_PORT; a Cloudflare Tunnel publishes it, see docs/operations.md)
"""
import contextlib
import hmac
import sys
from pathlib import Path
from types import SimpleNamespace

import requests
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

import config

sys.path.insert(0, str(Path(__file__).resolve().parent / "options_whale"))
import assistant  # noqa: E402  (the console's source list and shaping)

TERMINAL = config.optional_env("TERMINAL_URL", default=f"http://127.0.0.1:{config.OPTIONS_WHALE_PORT}").rstrip("/")
PORT = config.int_env("MCP_PORT", 8787, minimum=1, maximum=65535)


class _Terminal:
    """Just enough of Flask's test client for assistant.fetch_source: GET a route of the running terminal."""
    def get(self, route, query_string=None):
        r = requests.get(TERMINAL + route, params=query_string, timeout=60)
        return SimpleNamespace(status_code=r.status_code, content_type=r.headers.get("content-type", ""),
                               get_data=lambda as_text=True: r.text)


_APP = SimpleNamespace(test_client=lambda: _Terminal())


def market_server():
    mcp = MCPServer("market-terminal", instructions="Read-only market data from the operator's terminal (SPY/SLV options, dealer gamma, "
                    "Forecast Lab, VMRI, silver). Call list_sources first. Say the run time of stored data when quoting it.")

    @mcp.tool()
    def list_sources() -> str:
        """Every source get_source can read, with its parameters and what it holds."""
        return assistant.catalog_text()

    @mcp.tool()
    def get_source(source: str, query: str = "", path: str = "", last: int = 10) -> str:
        """Read one source. query like "ticker=SPY&limit=50"; path opens one part of a large result (e.g. data.implied); last = rows kept of a long list."""
        return assistant.fetch_source(_APP, source, query, path, last)

    return mcp


def brokerage_server():
    mcp = MCPServer("market-terminal-brokerage", instructions="The operator's live brokerage holdings (SnapTrade): stocks, and option "
                    "positions grouped into spreads with net mark and P/L. Private: do not repeat them anywhere shared.")

    @mcp.tool()
    def get_brokerage_positions(refresh: bool = False) -> str:
        """Stocks and option groups per account. status 'not_configured' or 'error' carries the reason. refresh=True bypasses the 60 s cache."""
        r = requests.get(TERMINAL + "/api/brokerage", params={"refresh": "1"} if refresh else None, timeout=60)
        return r.text

    return mcp


def build_app():
    """ASGI app: /<token>/mcp -> that profile's server. Unknown token or path: 404 with no hint."""
    off = TransportSecuritySettings(enable_dns_rebinding_protection=False)   # the secret path guards it; the Host is a tunnel hostname
    routes = {}
    for token, make in ((config.optional_env("MCP_MARKET_TOKEN", default=""), market_server),
                        (config.optional_env("MCP_BROKERAGE_TOKEN", default=""), brokerage_server)):
        if len(token) >= 24:   # shorter than 24 characters is not a secret; the endpoint stays off
            routes[token] = make().streamable_http_app(stateless_http=True, json_response=True, transport_security=off)
    if not routes:
        sys.exit("Set MCP_MARKET_TOKEN and/or MCP_BROKERAGE_TOKEN (24+ random characters, e.g. `openssl rand -hex 24`).")

    @contextlib.asynccontextmanager
    async def lifespan():
        async with contextlib.AsyncExitStack() as stack:
            for sub in routes.values():
                await stack.enter_async_context(sub.router.lifespan_context(sub))
            yield

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            async with lifespan():
                while True:
                    msg = await receive()
                    if msg["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif msg["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
        parts = scope["path"].split("/", 2)   # ["", token, "mcp"]
        sub = next((a for t, a in routes.items() if len(parts) == 3 and hmac.compare_digest(parts[1].encode(), t.encode())), None)
        if scope["type"] != "http" or sub is None or parts[2] != "mcp":
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        await sub({**scope, "path": "/mcp", "root_path": ""}, receive, send)

    return app


if __name__ == "__main__":
    uvicorn.run(build_app(), host="127.0.0.1", port=PORT, log_level="warning")
