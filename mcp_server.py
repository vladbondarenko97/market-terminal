"""MCP server for the terminal: lets Claude chat, ChatGPT and other MCP clients read its data. Standalone, not part of the pipeline.

Two endpoints, each behind its own secret path segment, so holdings can be shared with fewer clients than market data:
    /<MCP_MARKET_TOKEN>/mcp      list_sources, get_source (the console's read-only sources plus three more), search_docs, read_doc
                                 (docs and the model code in core/), calculate
    /<MCP_BROKERAGE_TOKEN>/mcp   get_brokerage_positions   (live SnapTrade holdings)
A blank token switches that endpoint off; a token under 24 characters, or the same token twice, stops the server. Only GETs of the whitelisted
sources are made. The secret path is the only guard (MCP clients cannot send our own header): treat each link like a password.
Run: .venv/bin/python mcp_server.py   (listens on 127.0.0.1:MCP_PORT; a Cloudflare Tunnel publishes it, see docs/operations.md)
"""
import asyncio
import contextlib
import functools
import hmac
import json
import re
import sys
import threading
import time
from collections import OrderedDict
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
LOG_PATH = Path(config.optional_env("MCP_LOG", default="") or config.DATA_DIR / "mcp_calls.jsonl")
_log_lock, _denied_at = threading.Lock(), {}


def log_call(entry):
    """One JSON line per request: when, which endpoint, what was asked, the outcome. Never the token or the path. Never raises."""
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry}
    try:
        with _log_lock, open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError as exc:
        print(f"mcp log write failed: {exc}", file=sys.stderr)


def _client(scope):
    h = {k.decode().lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    ip = h.get("x-forwarded-for", "").split(",")[0].strip() or (scope.get("client") or ("",))[0]
    return {"ip": ip[:64], "ua": h.get("user-agent", "")[:120]}


def _summary(req, resp, status):
    """What an MCP request asked for and how it went, from the JSON-RPC bodies (a batch is summarised by its first item)."""
    try:
        r = json.loads(req)
        r = r[0] if isinstance(r, list) and r else r
        method, params = r.get("method", "?"), r.get("params") or {}
    except (ValueError, AttributeError):
        method, params = "unparsed", {}
    out = {"rpc": method}
    if method == "tools/call":
        out["tool"] = params.get("name")
        out["args"] = json.dumps(params.get("arguments") or {}, separators=(",", ":"))[:200]
    try:
        d = json.loads(resp)
        d = d[0] if isinstance(d, list) and d else d
        res = d.get("result") or {}
        text = ((res.get("content") or [{}])[0].get("text") or "")[:20]
        failed = "error" in d or bool(res.get("isError")) or text.startswith(("error:", '{"status":"error"', '{"status": "error"'))   # the tools report problems as text
    except (ValueError, AttributeError):
        failed = status >= 400
    return {**out, "status": status, "ok": status < 400 and not failed, "bytes": len(resp)}



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

# Read routes the console does not list; the console's own SOURCES stays as it is.
SOURCES = OrderedDict(assistant.SOURCES)
SOURCES.update({
    "paper_physical": ("/api/paper_physical", (), "Paper-to-physical silver ratio of the latest run: ratio, tier, inputs and the recorded-ratio history."),
    "shanghai_spread": ("/api/shanghai_spread", (), "Shanghai (SGE) versus COMEX silver spread, VAT-adjusted: latest reading, history, how unusual it is."),
    "chaos_history": ("/api/chaos/history", ("from",), "Recorded Chaos / Peace score history; from=YYYY-MM-DD limits the start."),
})

# ---------------------------------------------------------------- the knowledge base: the repo's own docs and model code (public, no secrets)
ROOT = Path(__file__).resolve().parent
DOC_CAP = 30000
MODEL_FILES = ("metrics", "forecast", "refining", "chaos", "edges", "watch", "market_calendar")   # the models; not the broker, sources or run plumbing


def doc_files():
    """name -> path. The only files read_doc and search_docs can reach: docs/*.md, README, AGENTS and the model modules of core/."""
    files = {f"docs/{p.name}": p for p in sorted((ROOT / "docs").glob("*.md"))}
    files.update({"README.md": ROOT / "README.md", "AGENTS.md": ROOT / "AGENTS.md"})
    files.update({f"core/{m}.py": ROOT / "core" / f"{m}.py" for m in MODEL_FILES})
    return {n: p for n, p in files.items() if p.is_file()}


@functools.lru_cache(maxsize=64)
def _lines(path, mtime):
    return tuple(Path(path).read_text(errors="replace").splitlines())


def _read(path):
    return _lines(str(path), path.stat().st_mtime_ns)


_PY = re.compile(r"^(?:async def |def |class )(\w+)|^([A-Z][A-Z0-9_]*) *(?::[^=]+)?=")


def _blocks(name, lines):
    """(title, start, end, depth) of every heading (markdown, outside code fences) or top-level def/class/CONSTANT (python)."""
    marks, fence = [], False
    for i, line in enumerate(lines):
        if name.endswith(".md"):
            if line.lstrip().startswith(("```", "~~~")):
                fence = not fence
            m = None if fence else re.match(r"^(#+) +(.*)", line)
            if m:
                marks.append((m.group(2).strip(), i, len(m.group(1))))
        elif (m := _PY.match(line)):
            start = i
            while start > 0 and lines[start - 1].startswith("@"):   # decorators belong to the function below them
                start -= 1
            marks.append((m.group(1) or m.group(2), start, 1))
    out = []
    for k, (title, i, depth) in enumerate(marks):
        end = next((j for _t, j, d in marks[k + 1:] if d <= depth), len(lines))
        out.append((title, i, end, depth))
    return out


def _outline(blocks, depth=2, limit=40):
    names = [b[0] for b in blocks if b[3] <= depth]
    return "; ".join(names[:limit]) + (f"; ... {len(names) - limit} more" if len(names) > limit else "")


def read_doc_text(name, section=""):
    files = doc_files()
    if name not in files:
        return "error: name must be one of: " + ", ".join(files)
    lines = list(_read(files[name]))
    blocks = _blocks(name, lines)
    if section:
        want = section.strip().lower().lstrip("# ")
        exact = [b for b in blocks if b[0].lower() == want]
        near = exact or [b for b in blocks if want in b[0].lower()]
        if not near:
            return f"error: no section matching {section!r} in {name}. Sections: " + _outline(blocks, 99)
        if len(near) > 1 and not exact:
            return f"error: {section!r} matches {len(near)} sections in {name}; pass one of: " + _outline(near, 99)
        lines = lines[near[0][1]:near[0][2]]
    text = "\n".join(lines)
    if len(text) > DOC_CAP:
        return text[:DOC_CAP] + f"\n\n[cut at {DOC_CAP} characters; pass section=... to read one part. Sections: " + _outline(blocks) + "]"
    return text


def search_text(query, max_hits=25):
    words = [w for w in re.split(r"\s+", query.lower().strip()) if w]
    if not words:
        return "error: give a search query"
    hits = []
    for name, path in doc_files().items():
        for i, line in enumerate(_read(path), 1):
            low = line.lower()
            if all(w in low for w in words):
                hits.append(f"{name}:{i}: {line.strip()[:220]}")
    if not hits:
        return "no matches"
    more = f"\n[{len(hits) - max_hits} more; narrow the query]" if len(hits) > max_hits else ""
    return "\n".join(hits[:max_hits]) + more


def market_server():
    mcp = MCPServer("market-terminal", instructions="Read-only market data from the operator's terminal (SPY/SLV options, dealer gamma, "
                    "Forecast Lab, VMRI, silver). Call list_sources first. For how a number is defined or computed, use search_docs and read_doc "
                    "(the documentation and model code) instead of guessing. Say the run time of stored data when quoting it.")

    @mcp.tool()
    async def list_sources() -> str:
        """Every source get_source can read, with its parameters and what it holds."""
        return assistant.catalog_text(SOURCES)

    @mcp.tool()
    async def get_source(source: str, query: str = "", path: str = "", last: int = 10) -> str:
        """Read one source. query like "ticker=SPY&limit=50"; path opens one part of a large result (e.g. data.implied); last = rows kept of a long list."""
        return await asyncio.to_thread(assistant.fetch_source, _APP, source, query, path, last, SOURCES)   # blocking HTTP: keep it off the event loop

    @mcp.tool()
    async def search_docs(query: str) -> str:
        """Search the terminal's documentation and model code (words are ANDed, case-insensitive). Returns file:line hits; open them with read_doc."""
        return await asyncio.to_thread(search_text, query)

    @mcp.tool()
    async def read_doc(name: str = "", section: str = "") -> str:
        """Read a document or model source: docs/*.md (terminal, forecast-lab, data, api, operations, value-changes, known-issues), README.md, AGENTS.md and the model code
        core/{metrics,forecast,refining,chaos,edges,watch,market_calendar}.py (core/metrics.py has the VMRI and GEX formulas). Empty name lists them. section = a heading (markdown) or a def/class/CONSTANT name (python), e.g. name=core/metrics.py section=VMRI_VERSION."""
        if not name:
            return "error: pass name (and optionally section) from this list:\n" + "\n".join(doc_files()) if section else "\n".join(doc_files())
        return await asyncio.to_thread(read_doc_text, name, section)

    @mcp.tool()
    async def calculate(expression: str) -> str:
        """Plain arithmetic (+ - * / // % ** and abs, round, min, max, sqrt, log, exp), e.g. for a ratio or a percent move."""
        return assistant.calc(re.sub(r"(?<=\d),(?=\d{3}\b)", "", expression))   # 1,000 -> 1000 whether or not the expression has a call

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
    routes = [(t, m.streamable_http_app(stateless_http=True, json_response=True, transport_security=off), prof)
              for (t, m), prof in zip(servers, [n for n in ("market", "brokerage") if tokens["MCP_" + n.upper() + "_TOKEN"]])]

    async def not_found(scope, send):
        c = _client(scope)
        if time.time() - _denied_at.get(c["ip"], 0) > 60:   # a scanner is one line a minute, not a flood
            _denied_at[c["ip"]] = time.time()
            log_call({"profile": "denied", "method": scope.get("method", scope["type"]), "path_len": len(scope["path"]), **c})
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
        sub = profile = None
        for t, a, prof in routes:   # compare against every token so the time does not show which one matched
            if len(parts) == 3 and hmac.compare_digest(parts[1].encode(), t.encode()):
                sub, profile = a, prof
        if scope["type"] != "http" or sub is None or parts[2] != "mcp":
            return await not_found(scope, send)
        t0, req, resp, status = time.monotonic(), bytearray(), bytearray(), [500]

        async def recv():
            msg = await receive()
            if msg["type"] == "http.request" and len(req) < 65536:
                req.extend(msg.get("body", b""))
            return msg

        async def snd(msg):
            if msg["type"] == "http.response.start":
                status[0] = msg["status"]
            elif msg["type"] == "http.response.body" and len(resp) < 1048576:
                resp.extend(msg.get("body", b""))
            await send(msg)

        try:
            await sub({**scope, "path": "/mcp", "root_path": ""}, recv, snd)
        finally:
            log_call({"profile": profile, **_summary(bytes(req), bytes(resp), status[0]),
                      "ms": round((time.monotonic() - t0) * 1000), **_client(scope)})

    return app


if __name__ == "__main__":
    uvicorn.run(build_app(), host="127.0.0.1", port=PORT, log_level="warning")
