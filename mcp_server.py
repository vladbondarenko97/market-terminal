"""MCP server for the terminal: lets Claude chat, ChatGPT and other MCP clients read its data. Standalone, not part of the pipeline.

Two endpoints, each behind its own secret path segment, so holdings can be shared with fewer clients than market data:
    /<MCP_MARKET_TOKEN>/mcp      list_sources, get_source  (the console's read-only sources; same shaping, see options_whale/assistant.py)
    /<MCP_BROKERAGE_TOKEN>/mcp   get_brokerage_positions   (live SnapTrade holdings)
A blank token switches that endpoint off; a token under 24 characters, or the same token twice, stops the server. Only GETs of the whitelisted
sources are made. The secret path is the only guard (MCP clients cannot send our own header): treat each link like a password.
Run: .venv/bin/python mcp_server.py   (listens on 127.0.0.1:MCP_PORT; a Cloudflare Tunnel publishes it, see docs/operations.md)
"""
import asyncio
import contextlib
import hmac
import json
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


class _Response:
    def __init__(self, r):
        self.status_code, self.content_type, self._text = r.status_code, r.headers.get("content-type", ""), r.text

    def get_data(self, as_text=True):
        return self._text if as_text else self._text.encode()


class _Terminal:
    """Just enough of Flask's test client for assistant.fetch_source: GET a route of the running terminal."""
    def get(self, route, query_string=None):
        return _Response(requests.get(TERMINAL + route, params=query_string, timeout=60))


_APP = SimpleNamespace(test_client=lambda: _Terminal())


def market_server():
    mcp = MCPServer("market-terminal", instructions="Read-only market data from the operator's terminal (SPY/SLV options, dealer gamma, "
                    "Forecast Lab, VMRI, silver). Call list_sources first. Say the run time of stored data when quoting it.")

    @mcp.tool()
    async def list_sources() -> str:
        """Every source get_source can read, with its parameters and what it holds."""
        return assistant.catalog_text()

    @mcp.tool()
    async def get_source(source: str, query: str = "", path: str = "", last: int = 10) -> str:
        """Read one source. query like "ticker=SPY&limit=50"; path opens one part of a large result (e.g. data.implied); last = rows kept of a long list."""
        return await asyncio.to_thread(assistant.fetch_source, _APP, source, query, path, last)   # blocking HTTP: keep it off the event loop

    return mcp


def brokerage_server():
    mcp = MCPServer("market-terminal-brokerage", instructions="The operator's live brokerage holdings (SnapTrade): stocks, and option "
                    "positions grouped into spreads with net mark and P/L. Private: do not repeat them anywhere shared.")

    @mcp.tool()
    async def get_brokerage_positions() -> str:
        """Stocks and option groups per account (the terminal caches SnapTrade for 60 s). status 'not_configured' or 'error' carries the reason."""
        def call():
            try:
                r = requests.get(TERMINAL + "/api/brokerage", timeout=60)
                r.raise_for_status()
                return r.text
            except requests.RequestException as exc:
                return json.dumps({"status": "error", "reason": f"terminal unreachable or failed: {type(exc).__name__}"})
        return await asyncio.to_thread(call)

    return mcp


def build_app():
    """ASGI app: /<token>/mcp -> that profile's server. Unknown token or path: 404 with no hint."""
    off = TransportSecuritySettings(enable_dns_rebinding_protection=False)   # the secret path guards it; the Host is a tunnel hostname
    tokens = {name: config.optional_env(name, default="") for name in ("MCP_MARKET_TOKEN", "MCP_BROKERAGE_TOKEN")}
    for name, token in tokens.items():
        if token and len(token) < 24:
            sys.exit(f"{name} is shorter than 24 characters: use `openssl rand -hex 24`, or leave it blank to switch that endpoint off.")
    if tokens["MCP_MARKET_TOKEN"] and tokens["MCP_MARKET_TOKEN"] == tokens["MCP_BROKERAGE_TOKEN"]:
        sys.exit("MCP_MARKET_TOKEN and MCP_BROKERAGE_TOKEN must differ: the market link would otherwise expose holdings.")
    servers = [(tokens[n], make()) for n, make in (("MCP_MARKET_TOKEN", market_server), ("MCP_BROKERAGE_TOKEN", brokerage_server)) if tokens[n]]
    if not servers:
        sys.exit("Set MCP_MARKET_TOKEN and/or MCP_BROKERAGE_TOKEN (24+ random characters, e.g. `openssl rand -hex 24`).")
    routes = [(t, m.streamable_http_app(stateless_http=True, json_response=True, transport_security=off)) for t, m in servers]

    async def not_found(scope, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close"})
        else:
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            try:
                async with contextlib.AsyncExitStack() as stack:
                    for _t, m in servers:
                        await stack.enter_async_context(m.session_manager.run())
                    while True:
                        msg = await receive()
                        if msg["type"] == "lifespan.startup":
                            await send({"type": "lifespan.startup.complete"})
                        elif msg["type"] == "lifespan.shutdown":
                            await send({"type": "lifespan.shutdown.complete"})
                            return
            except Exception as exc:
                await send({"type": "lifespan.startup.failed", "message": f"{type(exc).__name__}: {exc}"})
                raise
        parts = scope["path"].split("/", 2)   # ["", token, "mcp"]
        sub = None
        for t, a in routes:   # compare against every token so the time does not show which one matched
            if len(parts) == 3 and hmac.compare_digest(parts[1].encode(), t.encode()):
                sub = a
        if scope["type"] != "http" or sub is None or parts[2] != "mcp":
            return await not_found(scope, send)
        await sub({**scope, "path": "/mcp", "root_path": ""}, receive, send)

    return app


if __name__ == "__main__":
    uvicorn.run(build_app(), host="127.0.0.1", port=PORT, log_level="warning")
