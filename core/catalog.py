"""One catalog: metric definitions, template aliases, units, lineage and chart definitions.

`path` is the dotted location inside the committed report context (v2_snapshots.context_json).
The lineage overview (python main_pipeline.py catalog) is generated from these entries and a snapshot.
"""

# id, path in context, unit, source/formula, freshness, legacy mappings, template alias, consumers
METRICS = [
    # --- run
    ("run.generated_at", "run.generated_at", "ISO-8601 UTC", "coordinator clock", "per run",
     ["equities_darkpool_gex_ledger.Date"], "[SLV_SCAN_DATE]", ["report", "ledgers"]),
    # --- SLV / SPY block flow
    ("flow.SLV.observed_session", "flow.SLV.session", "date", "Databento trades window (T+1)", "1 session",
     [], "[SLV_FLOW_SESSION]", ["report"]),
    ("quote.SLV.price", "prices.slv.value", "USD/share", "Yahoo SLV daily close", "1 trading day",
     ["equities_darkpool_gex_ledger.Spot_Price", "volume_dashboard.txt market_prices/slv@price"], "[SLV_SPOT_PRICE]",
     ["report", "ledger", "html"]),
    ("flow.SLV.bias", "flow.SLV.bias", "classification", "buy vs sell aggressor block volume, 1.2x", "1 session",
     ["equities_darkpool_gex_ledger.DP_Sentiment"], "[SLV_DP_SENTIMENT]", ["report", "ledger"]),
    ("flow.SLV.block_vwap", "flow.SLV.block_vwap", "USD/share", "sum(price*size)/sum(size), size>=10,000",
     "1 session", ["equities_darkpool_gex_ledger.DP_VWAP"], "[SLV_DP_VWAP]", ["report", "ledger"]),
    ("flow.SLV.block_volume", "flow.SLV.block_volume", "shares", "sum(size) of qualifying blocks", "1 session",
     ["equities_darkpool_gex_ledger.DP_Total_Vol"], "[SLV_DP_TOTAL_VOL]", ["report", "ledger"]),
    ("flow.SLV.block_notional", "flow.SLV.block_notional", "USD", "sum(price*size)", "1 session",
     ["equities_darkpool_gex_ledger.DP_Notional_USD"], "[SLV_DP_NOTIONAL_USD]", ["report", "ledger"]),
    ("flow.SLV.largest_block", "flow.SLV.largest_block", "shares", "max(size)", "1 session",
     ["equities_darkpool_gex_ledger.DP_Largest_Block"], "[SLV_DP_LARGEST_BLOCK]", ["report", "ledger"]),
    ("flow.SLV.buy_aggressor_volume", "flow.SLV.buy_aggressor_volume", "shares", "Databento side=B (bid/buy aggressor)",
     "1 session", ["equities_darkpool_gex_ledger.DP_Bull_Vol"], "[SLV_DP_BULL_VOL]", ["report", "ledger"]),
    ("flow.SLV.sell_aggressor_volume", "flow.SLV.sell_aggressor_volume", "shares",
     "Databento side=A (ask/sell aggressor)", "1 session", ["equities_darkpool_gex_ledger.DP_Bear_Vol"],
     "[SLV_DP_BEAR_VOL]", ["report", "ledger"]),
    ("flow.SLV.unknown_side_volume", "flow.SLV.unknown_side_volume", "shares", "Databento side=N", "1 session",
     [], None, ["report"]),
    ("flow.SLV.status", "flow.SLV.status", "status", "coverage / limit / venue qualification", "per run", [],
     "[SLV_FLOW_STATUS]", ["report"]),
    ("options.SLV.call_wall", "options.SLV.gex.call_wall", "USD strike", "max net GEX strike within ±10%",
     "1 trading day", ["equities_darkpool_gex_ledger.GEX_Call_Wall"], "[SLV_GEX_CALL_WALL]", ["report", "ledger"]),
    ("options.SLV.put_wall", "options.SLV.gex.put_wall", "USD strike", "min net GEX strike within ±10%",
     "1 trading day", ["equities_darkpool_gex_ledger.GEX_Put_Wall"], "[SLV_GEX_PUT_WALL]", ["report", "ledger"]),
    ("options.SLV.zero_gamma", "options.SLV.gex.zero_gamma", "USD", "spot where net GEX changes sign (zero_gamma_v1); null if none",
     "1 trading day", ["equities_darkpool_gex_ledger.GEX_Zero_Gamma"], "[SLV_GEX_ZERO_GAMMA]", ["report", "ledger"]),
    ("quote.SPY.price", "prices.spy.value", "USD/share", "Yahoo SPY daily close", "1 trading day",
     ["institutional_ledger.Spot_Price", "equities_darkpool_gex_ledger.Spot_Price"], None, ["report", "ledger"]),
    ("flow.SPY.bias", "flow.SPY.bias", "classification", "buy vs sell aggressor block volume, 1.2x", "1 session",
     ["institutional_ledger.DP_Sentiment", "equities_darkpool_gex_ledger.DP_Sentiment"], None, ["report", "execution"]),
    ("flow.SPY.block_vwap", "flow.SPY.block_vwap", "USD/share", "sum(price*size)/sum(size)", "1 session",
     ["institutional_ledger.DP_VWAP"], None, ["report", "ledger"]),
    ("options.SPY.call_wall", "options.SPY.gex.call_wall", "USD strike", "gex_v2", "1 trading day",
     ["institutional_ledger.Call_Wall"], None, ["report", "execution"]),
    ("options.SPY.put_wall", "options.SPY.gex.put_wall", "USD strike", "gex_v2", "1 trading day",
     ["institutional_ledger.Put_Wall"], None, ["report", "execution"]),
    ("options.SPY.net_gex", "options.SPY.gex.net_gex", "USD per $1 move", "sum gamma*OI*100*spot (calls +, puts -)",
     "1 trading day", ["institutional_ledger.Net_Gamma"], None, ["report"]),
    ("options.SPY.max_pain", "options.SPY.max_pain.strike", "USD strike", "max_pain_v2 (front expiry, known OI)",
     "1 trading day", ["institutional_ledger.Max_Pain"], None, ["report", "execution"]),
    # --- macro
    ("macro.vmri.score", "vmri.score", "index", "vmri_v1: (DXY*TNX/1.61)*(OAS/4)*(VIX/20)", "per run",
     ["macro_master_ledger.VMRI_Score", "tactical_ruling.txt VLAD_MACRO_RISK_INDEX/score"], None, ["report", "ledger"]),
    ("macro.vmri.tier", "vmri.tier", "classification", "vmri_v1 tiers 150/250/350", "per run",
     ["macro_master_ledger.Threat_Tier"], None, ["report", "ledger"]),
    ("quote.DXY", "prices.dxy.value", "index", "Yahoo DX-Y.NYB", "1 trading day", ["macro_master_ledger.DXY"], None,
     ["vmri", "report"]),
    ("quote.TNX", "prices.tnx.value", "percent", "Yahoo ^TNX (10Y yield)", "1 trading day",
     ["macro_master_ledger.10Y_Yield"], None, ["vmri", "report"]),
    ("quote.ZN", "prices.zn.value", "USD per 100 face", "Yahoo ZN=F", "1 trading day",
     ["macro_master_ledger.ZN_Futures"], None, ["report"]),
    ("fred.BAMLH0A0HYM2", "macro.oas.latest", "percent", "FRED HY OAS", "1 business day",
     ["macro_master_ledger.High_Yield_OAS"], None, ["vmri", "report"]),
    ("quote.VIX", "prices.vix.value", "index points", "Yahoo ^VIX", "1 trading day", ["macro_master_ledger.VIX"],
     None, ["vmri", "execution", "report"]),
    ("quote.WTI", "prices.wti.value", "USD/bbl", "Yahoo CL=F", "1 trading day", ["macro_master_ledger.WTI_Crude"],
     None, ["report"]),
    ("quote.BRENT", "prices.brent.value", "USD/bbl", "Yahoo BZ=F", "1 trading day",
     ["macro_master_ledger.Brent_Crude"], None, ["report"]),
    ("fred.RRPONTSYD", "macro.rrp.latest", "USD bn", "FRED ON RRP", "1 business day",
     ["macro_master_ledger.Reverse_Repo_BN"], None, ["report"]),
    ("fred.WALCL", "macro.walcl.latest", "USD bn", "FRED WALCL / 1000", "weekly",
     ["macro_master_ledger.Fed_Balance_Sheet_BN"], None, ["report"]),
    # --- metals
    ("quote.GOLD", "prices.gold.value", "USD/oz", "Yahoo GC=F (continuous futures)", "1 trading day",
     ["macro_master_ledger.Gold_Price", "crypto_metrics_history.Gold_Price"], None, ["report", "charts"]),
    ("quote.SILVER_FUT", "prices.silver_futures.value", "USD/oz", "Yahoo SI=F (continuous futures)", "1 trading day",
     ["macro_master_ledger.COMEX_Silver", "crypto_metrics_history.Silver_Price"], None, ["report", "ebay benchmark"]),
    ("quote.SILVER_SPOT", "prices.silver_spot.value", "USD/oz", "GoldAPI XAG/USD spot", "intraday",
     ["volume_dashboard.txt market_prices/spot@price"], None, ["report", "html"]),
    ("ratio.gold_silver", "ratios.gold_silver", "ratio", "GC=F / SI=F", "per run",
     ["macro_master_ledger.Gold_Silver_Ratio"], None, ["report"]),
    ("quote.BTC", "prices.btc.value", "USD", "Yahoo BTC-USD", "per run", ["crypto_metrics_history.BTC_Price"], None,
     ["report", "charts"]),
    ("ratio.btc_silver_oz", "ratios.btc_silver_oz", "oz per BTC", "BTC / SI=F", "per run",
     ["crypto_metrics_history.Silver_BTC_Ratio"], None, ["report", "charts"]),
    ("ratio.btc_gold_oz", "ratios.btc_gold_oz", "oz per BTC", "BTC / GC=F", "per run",
     ["crypto_metrics_history.Gold_BTC_Ratio"], None, ["report", "charts"]),
    ("sge.silver_cny_kg", "shanghai.cny_per_kg", "CNY/kg", "SGE silver benchmark (akshare)", "1 SGE session",
     ["tactical_ruling.txt shfe_silver/cny_per_kg"], None, ["report"]),
    ("sge.silver_usd_oz", "shanghai.usd_per_oz", "USD/troy oz", "CNY/kg / USDCNY / 32.1507", "per run",
     ["macro_master_ledger.SHFE_Silver_USD"], None, ["report"]),
    ("sge.premium_tax_adj", "shanghai.premium_tax_adj", "USD/troy oz",
     "(CNY/kg / 1.13) / USDCNY / 32.1507 - SI=F  (13% VAT is a recorded assumption)", "per run",
     ["macro_master_ledger.SHFE_Premium"], None, ["report", "chart11"]),
    # --- COMEX
    ("comex.silver.registered", "inventory.registered", "troy oz", "Silver_stocks.xls TOTAL REGISTERED / TOTAL TODAY",
     "1 report day", ["comex_inventory_history.Registered"], None, ["report", "chart6", "paper/physical"]),
    ("comex.silver.eligible", "inventory.eligible", "troy oz", "TOTAL ELIGIBLE / TOTAL TODAY", "1 report day",
     ["comex_inventory_history.Eligible"], None, ["report", "chart6"]),
    ("comex.silver.total_change", "inventory.total_change", "troy oz", "COMBINED TOTAL / NET CHANGE (excl. adjustments)",
     "1 report day", ["comex_inventory_history.Total_Change"], None, ["report"]),
    ("comex.silver.report_date", "inventory.report_date", "date", "'Report Date:' cell inside workbook",
     "-", ["comex_inventory_history.Date (report date for v2 rows)"], None, ["report"]),
    ("cme.SI_F.open_interest", "cme_volume.latest.SI_F.open_interest", "contracts",
     "daily_volume.xlsx by product (SI, F)", "1 trade date", ["macro_master_ledger.Silver_OI"], None,
     ["paper/physical", "chart2", "chart3"]),
    ("comex.paper_physical_ratio", "paper_physical.ratio", "ratio", "SI OI * 5000 / registered oz", "per run",
     ["macro_master_ledger.Paper_Physical_Ratio"], None, ["report"]),
    ("ebay.cheapest_total", "ebay.cheapest", "USD", "min(price + shipping) across tracked listings", "per run",
     ["physical_arbitrage_ledger.Cheapest_Eagle", "macro_master_ledger.Retail_Silver_Cheapest"], None, ["report"]),
    ("ebay.benchmark", "ebay.benchmark.value", "USD/oz", "run's SI=F quote (no hardcoded fallback)", "per run",
     ["physical_arbitrage_ledger.COMEX_Spot"], None, ["report"]),
    ("calendar.events", "calendar.events", "events", "ForexFactory weekly XML (thisweek + nextweek)", "per run",
     ["tactical_ruling.txt upcoming_macro_events"], None, ["report", "ntfy"]),
    ("execution.score", "execution.total_score", "points", "execution_v2 (legacy weights)", "per run", [], None,
     ["report"]),
]

# Chart families. `inputs` are context/DB datasets; `window` is ('sessions', n) or ('days', n).
CHARTS = [
    {"family": 1, "file": "chart1_es_conviction_{w}.png", "title": "S&P 500 E-mini (ES) Futures Volume & Open Interest",
     "windows": [("7d", ("sessions", 7)), ("30d", ("sessions", 30))], "inputs": ["cme:ES_F"], "min_points": 1,
     "email": "ES Conviction"},
    {"family": 2, "file": "chart2_si_conviction_{w}.png", "title": "COMEX Silver (SI) Futures Volume & Open Interest",
     "windows": [("7d", ("sessions", 7)), ("30d", ("sessions", 30))], "inputs": ["cme:SI_F"], "min_points": 1,
     "email": "Silver Conviction"},
    {"family": 3, "file": "chart3_silver_divergence_{w}.png",
     "title": "Silver OI Divergence: Standard (SI) vs Micro (SIL) — size proxy",
     "windows": [("7d", ("sessions", 7)), ("30d", ("sessions", 30))], "inputs": ["cme:SI_F", "cme:SIL_F"],
     "min_points": 2, "email": "Silver Divergence"},
    {"family": 4, "file": "chart4_spy_options_flow_{w}.png",
     "title": "CME E-mini S&P 500 (ES) Options: Call vs Put Volume",
     "windows": [("7d", ("sessions", 7)), ("30d", ("sessions", 30))], "inputs": ["cme:ES_C", "cme:ES_P"],
     "min_points": 1, "email": "ES Options Flow (CME; legacy 'SPY Options Flow')"},
    {"family": 5, "file": "chart5_macro_10y_yields_{w}.png",
     "title": "10Y Treasury Note Futures (ZN): Volume & Daily OI Change",
     "windows": [("7d", ("sessions", 7)), ("30d", ("sessions", 30))], "inputs": ["cme:ZN_F"], "min_points": 1,
     "email": "10Y Note Futures (legacy 'Macro 10Y Yields')"},
    {"family": 6, "file": "chart6_comex_inventory_{w}.png", "title": "COMEX Silver Inventory: Registered vs Eligible",
     "windows": [("30d", ("days", 30))], "inputs": ["inventory"], "min_points": 1, "email": "COMEX Inventory"},
    {"family": 7, "file": "chart7_crypto_ratios_{w}.png", "title": "Bitcoin priced in Metals (oz per BTC)",
     "windows": [("30d", ("days", 30)), ("1y", ("days", 365))], "inputs": ["crypto_metrics_history"],
     "min_points": 1, "email": "Bitcoin Ratios"},
    {"family": 8, "file": "chart8_metals_price_{w}.png", "title": "Gold & Silver Continuous Futures (GC=F, SI=F)",
     "windows": [("30d", ("days", 30)), ("1y", ("days", 365))], "inputs": ["crypto_metrics_history"],
     "min_points": 1, "email": "Metals Prices"},
    {"family": 9, "file": "chart9_es_divergence_{w}.png",
     "title": "ES OI Divergence: Standard (ES) vs Micro (MES) — size proxy",
     "windows": [("30d", ("sessions", 30))], "inputs": ["cme:ES_F", "cme:MES_F", "equities_darkpool_gex_ledger"],
     "min_points": 2, "email": "Smart Money vs Retail (ES) — size proxy"},
    {"family": 10, "file": "chart10_yield_contagion_{w}.png",
     "title": "Credit Stress Overlay: HY OAS, 10Y Yield, Reverse Repo (no composite score)",
     "windows": [("30d", ("days", 30))], "inputs": ["macro_master_ledger"], "min_points": 1,
     "email": "Yield Curve Contagion (series overlay)"},
    {"family": 11, "file": "chart11_physical_squeeze_{w}.png",
     "title": "Physical Squeeze: SGE Tax-Adj Premium vs Registered Change (5-report avg)",
     "windows": [("30d", ("days", 30))], "inputs": ["macro_master_ledger", "inventory"], "min_points": 1,
     "email": "Physical Squeeze Oscillator"},
]


def metric_rows():
    for (mid, path, unit, source, fresh, legacy, alias, consumers) in METRICS:
        yield {"id": mid, "path": path, "unit": unit, "source": source, "freshness": fresh,
               "legacy": legacy, "alias": alias, "consumers": consumers}


def resolve(ctx, path):
    cur = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _section_meta(ctx, path):
    """Nearest enclosing dict that carries status/source metadata."""
    parts = path.split(".")
    for i in range(len(parts) - 1, 0, -1):
        node = resolve(ctx, ".".join(parts[:i]))
        if isinstance(node, dict) and ("status" in node or "source" in node):
            return node
    return {}


def lineage(ctx):
    rows = []
    for m in metric_rows():
        val = resolve(ctx, m["path"])
        meta = _section_meta(ctx, m["path"])
        rows.append({**m, "value": val, "status": meta.get("status"), "reason": meta.get("reason"),
                     "observed_at": meta.get("observed_at") or meta.get("report_date") or meta.get("session")
                     or meta.get("date"),
                     "source_ref": meta.get("source") or meta.get("dataset"),
                     "payload_id": meta.get("payload_id")})
    return rows


def lineage_markdown(ctx):
    def fmt(v):
        if v is None:
            return "—"
        if isinstance(v, float):
            return f"{v:,.4f}".rstrip("0").rstrip(".")
        if isinstance(v, (list, dict)):
            return f"{type(v).__name__}[{len(v)}]"
        return str(v).replace("|", "/")[:60]
    run = ctx.get("run", {})
    out = [f"# Variable catalog & lineage — run {run.get('run_id')} ({run.get('generated_local')})", "",
           "| Metric | Alias | Value | Unit | Status | Observed | Source / formula | Legacy mappings | Consumers |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in lineage(ctx):
        status = r["status"] or "—"
        if r["reason"]:
            status += f" ({str(r['reason'])[:50]})"
        out.append(f"| `{r['id']}` | {r['alias'] or ''} | {fmt(r['value'])} | {r['unit']} | {status} | "
                   f"{fmt(r['observed_at'])} | {r['source']} | {', '.join(r['legacy'])} | {', '.join(r['consumers'])} |")
    out += ["", "## Charts", "", "| File | Status | Window | Dates | Reason |", "|---|---|---|---|---|"]
    for c in ctx.get("charts", []):
        out.append(f"| {c.get('file')} | {c.get('status')} | {c.get('window_label', '')} | "
                   f"{c.get('first_date', '')} → {c.get('last_date', '')} | {c.get('reason') or ''} |")
    return "\n".join(out)
