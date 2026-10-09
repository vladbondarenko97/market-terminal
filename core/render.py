"""Renderers. Input: a committed report context (+ chart manifest). No provider requests happen here."""
import csv
import html
import io
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from xml.dom import minidom

from core import metrics

NA = "unavailable"


def f2(v, fmt="{:.2f}", na=NA):
    return na if v is None else fmt.format(v)


def signed(v, na=NA):
    return na if v is None else f"{v:+.2f}"


def g(d, *path, default=None):
    for p in path:
        if not isinstance(d, dict):
            return default
        d = d.get(p)
    return default if d is None else d


# ============================================================ tactical_ruling.txt (XML, legacy structure)
def tactical_xml(ctx):
    P = ctx["prices"]
    root = ET.Element("tactical_ruling")
    root.set("generated", datetime.fromisoformat(ctx["run"]["generated_at"]).astimezone(
        __import__("zoneinfo").ZoneInfo("America/Chicago")).strftime("%Y-%m-%d %H:%M"))
    root.set("run_id", ctx["run"]["run_id"])

    macro = ET.SubElement(root, "macro_kill_switches")
    ET.SubElement(macro, "ten_year_treasury").text = f2(g(P, "tnx", "value"))
    ET.SubElement(macro, "zn_futures").text = str(g(P, "zn", "value", default=NA))

    crude = ET.SubElement(root, "crude_oil")
    for tag, key in (("wti", "wti"), ("brent", "brent")):
        n = ET.SubElement(crude, tag)
        ET.SubElement(n, "price").text = f2(g(P, key, "value"))
        ET.SubElement(n, "change").text = signed(g(P, key, "change"))

    cal = ctx["calendar"]
    cat = ET.SubElement(root, "catalyst_calendar")
    if cal["status"] == "missing":
        ET.SubElement(cat, "status").text = "unavailable"
        ET.SubElement(cat, "reason").text = cal.get("reason") or ""
    else:
        ET.SubElement(cat, "status").text = cal["catalyst_status"]
        for e in cal.get("today_tier1", []):
            t = ET.SubElement(cat, "tripwire")
            ET.SubElement(t, "time_est").text = e.get("time_et") or e["time"]
            ET.SubElement(t, "event").text = e["title"]

    oas = ctx["macro"]["oas"]
    cr = ET.SubElement(root, "credit_markets")
    if oas.get("latest") is not None:
        ET.SubElement(cr, "high_yield_oas_spread").text = f2(oas["latest"])
        for k in ("1D", "1W", "1M", "1Y"):
            ET.SubElement(cr, f"change_{k}").text = signed(oas.get(k))
    else:
        ET.SubElement(cr, "high_yield_oas_spread").text = NA

    liq = ET.SubElement(root, "liquidity_plumbing")
    for tag, key in (("reverse_repo_bn", "rrp"), ("fed_balance_sheet_bn", "walcl")):
        n = ET.SubElement(liq, tag)
        d = ctx["macro"][key]
        if d.get("latest") is not None:
            ET.SubElement(n, "latest").text = f2(d["latest"])
            ET.SubElement(n, "change_1D").text = signed(d.get("1D"))
            ET.SubElement(n, "change_1M").text = signed(d.get("1M"))

    for tag, key in (("vix", "vix"), ("dxy", "dxy")):
        n = ET.SubElement(root, tag)
        ET.SubElement(n, "value").text = f2(g(P, key, "value"))
        ET.SubElement(n, "change").text = signed(g(P, key, "change"))

    sh = ctx["shanghai"]
    shfe = ET.SubElement(root, "shfe_silver")
    shfe.set("source", "SGE silver benchmark (legacy node name)")
    if sh.get("usd_per_oz") is not None:
        ET.SubElement(shfe, "cny_per_kg").text = f"¥{sh['cny_per_kg']:,.2f}"
        ET.SubElement(shfe, "usd_per_kg").text = f"${sh['usd_per_kg']:,.2f}"
        ET.SubElement(shfe, "usd_per_oz").text = f"${sh['usd_per_oz']:,.2f}"
        ET.SubElement(shfe, "comex_spot").text = f"${sh['comex_ref']:,.2f}" if sh.get("comex_ref") else NA
        ET.SubElement(shfe, "premium").text = signed(sh.get("premium_tax_adj"))
    else:
        ET.SubElement(shfe, "status").text = NA

    r = ctx["ratios"]
    ET.SubElement(root, "gold_silver_ratio").text = f2(r.get("gold_silver"))
    ET.SubElement(root, "gold_price").text = f2(g(P, "gold", "value"))
    btc = ET.SubElement(root, "bitcoin_metrics")
    if r.get("btc_silver_oz") and r.get("btc_gold_oz"):
        ET.SubElement(btc, "btc_price").text = f"${P['btc']['value']:,.2f}"
        ET.SubElement(btc, "silver_btc_ratio").text = f2(r["btc_silver_oz"])
        ET.SubElement(btc, "gold_btc_ratio").text = f2(r["btc_gold_oz"])
    else:
        ET.SubElement(btc, "status").text = NA

    ET.SubElement(root, "gex").text = NA   # SqueezeMetrics feed retired (legacy node kept)
    ET.SubElement(root, "dix").text = NA

    vm = ctx["vmri"]
    v = ET.SubElement(root, "VLAD_MACRO_RISK_INDEX")
    v.set("formula_version", vm.get("version", ""))
    if vm.get("score") is not None:
        ET.SubElement(v, "score").text = f"{vm['score']:.2f}"
        ET.SubElement(v, "threat_level").text = vm["tier"]
        mech = ET.SubElement(v, "mechanics")
        c = vm["components"]
        ET.SubElement(mech, "base_stress").text = f"{c['base_stress']:.2f}"
        ET.SubElement(mech, "credit_multiplier").text = f"{c['credit_multiplier']:.2f}x"
        ET.SubElement(mech, "volatility_premium").text = f"{c['volatility_premium']:.2f}x"
    else:
        ET.SubElement(v, "score").text = "UNAVAILABLE"
        ET.SubElement(v, "reason").text = vm.get("reason") or ""

    eb = ctx["ebay"]
    bench = g(eb, "benchmark", "value")
    if eb.get("listings"):
        arb = ET.SubElement(root, "physical_arbitrage", comex_spot=f"${bench:.2f}" if bench else NA,
                            benchmark_source="SI=F (run quote)", timestamp=ctx["run"]["generated_local"],
                            item_count=str(len(eb["listings"])))
        for l in sorted(eb["listings"], key=lambda x: (x["premium_percent"] is None, x["premium_percent"] or 0)):
            n = ET.SubElement(arb, "listing")
            n.set("item_id", l["item_id"])
            n.set("name", l["title"][:60] + ("..." if len(l["title"]) > 60 else ""))
            n.set("total_cost", f"${l['total_cost']:.2f}" if l["total_cost"] is not None else NA)
            n.set("base_price", f"${l['base_price']:.2f}" if l["base_price"] is not None else NA)
            n.set("shipping", f"${l['shipping']:.2f}" if l["shipping"] is not None else NA)
            n.set("premium_dollars", f"${l['premium_dollars']:.2f}" if l["premium_dollars"] is not None else NA)
            n.set("premium_percent", f"{l['premium_percent']:.2f}%" if l["premium_percent"] is not None else NA)
            n.set("status", str(l.get("status") or "Unknown"))
            n.set("available_qty", str(l.get("available_qty") if l.get("available_qty") is not None else "0"))
            n.set("sold_qty", str(l.get("sold_qty") if l.get("sold_qty") is not None else "0"))
    else:
        ET.SubElement(root, "physical_arbitrage", status="unavailable_or_timeout", reason=eb.get("reason") or "")

    ev_root = ET.SubElement(root, "upcoming_macro_events")
    if cal["status"] == "missing":
        ET.SubElement(ev_root, "status").text = f"Calendar unavailable: {cal.get('reason')}"
    else:
        ev_root.set("coverage_to", g(cal, "coverage", "to", default=""))
        if cal["status"] == "partial":
            ev_root.set("coverage_note", cal.get("reason") or "")
        if not cal["upcoming"]:
            ET.SubElement(ev_root, "status").text = "No High/Medium impact USD events in the covered window."
        for e in cal["upcoming"]:
            n = ET.SubElement(ev_root, "event")
            for k in ("date", "time", "impact", "title", "forecast", "previous"):
                n.set(k, e.get(k) or "")
            n.set("tz", "UTC")
            if e.get("time_ct"):
                n.set("time_ct", e["time_ct"])

    pp = ctx["paper_physical"]
    risk = ET.SubElement(root, "comex_default_risk")
    risk.set("oi_trade_date", str(pp.get("oi_trade_date")))
    risk.set("inventory_report_date", str(pp.get("inventory_report_date")))
    ET.SubElement(risk, "paper_claims_oz").text = f"{pp['paper_claims_oz']:,.0f}" if pp.get("paper_claims_oz") else NA
    ET.SubElement(risk, "physical_registered_oz").text = \
        f"{pp['registered_oz']:,.0f}" if pp.get("registered_oz") else NA
    ET.SubElement(risk, "leverage_ratio").text = f"{pp['ratio']:.2f}:1" if pp.get("ratio") is not None else NA
    ET.SubElement(risk, "status").text = pp.get("tier") or NA

    model_features_xml(root, ctx)
    raw = ET.tostring(root, encoding="utf-8")
    return minidom.parseString(raw).toprettyxml(indent="  ")


# ============================================================ extended model features (appended section)
def _fmt(v, kind="num"):
    if v is None:
        return None
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        if kind == "pct":
            return f"{v * 100:.2f}"
        if kind == "int":
            return f"{v:.0f}"
        av = abs(v)
        return f"{v:.0f}" if av >= 1e5 else f"{v:.2f}" if av >= 10 else f"{v:.4f}"
    return str(v)


def _el(parent, tag, text=None, **attrs):
    """Create a child element; None-valued attributes are omitted (missing stays missing)."""
    e = ET.SubElement(parent, tag)
    for k, v in attrs.items():
        if isinstance(v, tuple):
            v = _fmt(*v)
        elif not isinstance(v, str):
            v = _fmt(v)
        if v is not None:
            e.set(k, v)
    if text is not None:
        e.text = str(text)
    return e


def model_features_xml(root, ctx):
    run = ctx["run"]
    mf = _el(root, "model_features", version="features_v1", run_id=run["run_id"], generated_utc=run["generated_at"],
             note="Appended v2 section. Percent values carry the _pct suffix; missing values are omitted, "
                  "with status/reason attributes where known.")
    P = ctx["prices"]

    # 1. data quality: what is fresh, stale or missing this run
    dq = _el(mf, "data_quality")
    for name, sec in (("cme_volume_oi", ctx["cme_volume"]), ("comex_inventory", ctx["inventory"]),
                      ("macro_calendar", ctx["calendar"]), ("spy_block_flow", ctx["flow"]["SPY"]),
                      ("slv_block_flow", ctx["flow"]["SLV"]), ("spy_options", ctx["options"].get("SPY", {})),
                      ("slv_options", ctx["options"].get("SLV", {})), ("sge_shanghai", ctx["shanghai"]),
                      ("ebay_retail", ctx["ebay"]), ("technicals", ctx["technicals"]), ("vmri", ctx["vmri"])):
        _el(dq, "source", name=name, status=sec.get("status", "missing"), reason=sec.get("reason"),
            observed=str(sec.get("observed_at") or sec.get("report_date") or sec.get("latest_trade_date")
                         or sec.get("session") or "") or None)

    # 2. every quote with change and timestamp
    q = _el(mf, "cross_asset_quotes")
    for key, v in P.items():
        val, chg = v.get("value"), v.get("change")
        prev = v.get("prev_close")
        _el(q, "quote", key=key, symbol=v.get("symbol") or ("XAG/USD" if key == "silver_spot" else None), value=val,
            change=chg, change_pct=((chg / prev) if (chg is not None and prev) else None, "pct"),
            observed=str(v.get("observed_at") or "") or None, status=v.get("status"), reason=v.get("reason"),
            source=v.get("source"))

    # 3. trend & volatility per instrument
    tv = _el(mf, "price_trend_volatility")
    for key, st in (ctx.get("price_stats") or {}).items():
        if st.get("status") != "fresh":
            _el(tv, "instrument", key=key, symbol=st.get("symbol"), status=st.get("status"), reason=st.get("reason"))
            continue
        _el(tv, "instrument", key=key, symbol=st.get("symbol"), last=st["last"], last_bar=st["last_bar"],
            ret_1d_pct=(st["ret_1d"], "pct"), ret_5d_pct=(st["ret_5d"], "pct"), ret_20d_pct=(st["ret_20d"], "pct"),
            ret_60d_pct=(st["ret_60d"], "pct"), ret_252d_pct=(st["ret_252d"], "pct"),
            realized_vol_10d_pct=(st["rv_10d"], "pct"), realized_vol_20d_pct=(st["rv_20d"], "pct"),
            realized_vol_60d_pct=(st["rv_60d"], "pct"), atr_14=st["atr_14"], atr_14_pct=(st["atr_14_pct"], "pct"),
            rsi_14=st["rsi_14"], sma_20=st["sma_20"], sma_50=st["sma_50"], sma_200=st["sma_200"],
            dist_sma_50_pct=(st["dist_sma_50"], "pct"), dist_sma_200_pct=(st["dist_sma_200"], "pct"),
            zscore_20d=st["zscore_20d"], high_52w=st["high_52w"], low_52w=st["low_52w"],
            from_52w_high_pct=(st["pct_from_52w_high"], "pct"), from_52w_low_pct=(st["pct_from_52w_low"], "pct"),
            rel_volume_20d=st["rel_volume_20d"])

    # 4. volatility regime
    vr = ctx.get("vol_regime") or {}
    w = ctx["weather"]
    _el(mf, "volatility_regime", vix=w.get("vix"), vix3m=w.get("vix3m"), vix_minus_vix3m=w.get("spread"),
        vix_term_ratio=vr.get("vix_term_ratio"), term_structure=(w.get("term_structure") or "").split(" ")[0] or None,
        spy_implied_pct=(vr.get("spy_implied_vix"), "pct"), spy_realized_20d_pct=(vr.get("spy_realized_20d"), "pct"),
        spy_variance_risk_premium_pct=(vr.get("spy_variance_risk_premium"), "pct"),
        slv_atm_iv_30d_pct=(vr.get("slv_atm_iv_30d"), "pct"), slv_realized_20d_pct=(vr.get("slv_realized_20d"), "pct"),
        slv_iv_minus_rv_pct=(vr.get("slv_iv_minus_rv"), "pct"))

    # 5. where today sits versus its own history
    rc = ctx.get("regime_context") or {}
    rel = _el(mf, "regime_percentiles", ledger_window=rc.get("window"))
    for name, r in rc.items():
        if name == "window" or not r:
            continue
        _el(rel, "metric", name=name, latest=r["latest"], percentile=r["percentile"], zscore=r.get("zscore"),
            min=r["min"], max=r["max"], mean=r["mean"], observations=(r["observations"], "int"), window=r.get("window"))

    # 6. silver complex
    sc = _el(mf, "silver_complex")
    b = ctx.get("silver_basis") or {}
    _el(sc, "futures_spot_basis", futures_minus_spot=b.get("futures_minus_spot"),
        futures_minus_spot_pct=(b.get("futures_minus_spot_pct"), "pct"), slv_per_spot_oz=b.get("slv_per_spot_oz"),
        note=b.get("note"))
    sh = ctx["shanghai"]
    _el(sc, "shanghai_sge", sge_date=sh.get("sge_date"), session=sh.get("sge_session"), cny_per_kg=sh.get("cny_per_kg"),
        usdcny=sh.get("usdcny"), usd_per_oz=sh.get("usd_per_oz"), comex_ref=sh.get("comex_ref"),
        raw_spread_usd_oz=sh.get("raw_spread"), premium_tax_adj_usd_oz=sh.get("premium_tax_adj"),
        vat_assumption=sh.get("tax_assumption"), status=sh.get("status"))
    inv, it = ctx["inventory"], ctx.get("inventory_trend") or {}
    ie = _el(sc, "comex_inventory", unit="troy_oz", report_date=inv.get("report_date"), status=inv.get("status"),
             registered=(inv.get("registered"), "int"), eligible=(inv.get("eligible"), "int"),
             total=(inv.get("total"), "int"), registered_net_change=(inv.get("reg_change"), "int"),
             eligible_net_change=(inv.get("elig_change"), "int"), total_net_change=(inv.get("total_change"), "int"),
             registered_adjustment=(inv.get("reg_adjustment"), "int"), eligible_adjustment=(inv.get("elig_adjustment"), "int"),
             registered_share_pct=(it.get("registered_share_of_total"), "pct"),
             registered_change_5_reports=(it.get("registered_change_5_reports"), "int"),
             registered_change_20_reports=(it.get("registered_change_20_reports"), "int"),
             eligible_change_5_reports=(it.get("eligible_change_5_reports"), "int"),
             eligible_change_20_reports=(it.get("eligible_change_20_reports"), "int"),
             registered_days_cover_at_5_report_drain=(it.get("registered_coverage_days_at_5r_pace"), "int"))
    for d in it.get("top_depositories", []):
        _el(ie, "depository", name=d["depository"], registered=(d["registered"], "int"), eligible=(d["eligible"], "int"))
    pp = ctx["paper_physical"]
    _el(sc, "paper_physical", ratio=pp.get("ratio"), si_open_interest=(pp.get("paper_claims_oz") / 5000 if pp.get("paper_claims_oz") else None, "int"),
        paper_claims_oz=(pp.get("paper_claims_oz"), "int"), registered_oz=(pp.get("registered_oz"), "int"),
        oi_trade_date=pp.get("oi_trade_date"), inventory_report_date=pp.get("inventory_report_date"), tier=pp.get("tier"))
    so = ctx.get("silver_options_cme") or {}
    _el(sc, "cme_silver_options", trade_date=so.get("trade_date"), put_call_volume_ratio=so.get("put_call_volume_ratio"),
        put_call_oi_ratio=so.get("put_call_oi_ratio"))
    eb = ctx["ebay"]
    prem = [l["premium_percent"] for l in eb.get("listings", []) if l.get("premium_percent") is not None]
    _el(sc, "retail_physical_premium", benchmark=g(eb, "benchmark", "value"), cheapest=eb.get("cheapest"),
        average=eb.get("average"), min_premium_pct=min(prem) if prem else None,
        median_premium_pct=sorted(prem)[len(prem) // 2] if prem else None, listings=(len(eb.get("listings", [])), "int"),
        in_stock=(sum(1 for l in eb.get("listings", []) if l.get("status") == "IN_STOCK"), "int"))

    # 7. CME futures & options positioning
    cp = _el(mf, "cme_positioning", source="CME daily volume & OI report (trade dates)")
    for key, pos in (ctx.get("cme_positioning") or {}).items():
        if pos.get("status") != "fresh":
            continue
        _el(cp, "product", key=key, name=ctx["cme_volume"]["legacy_names"].get(key), trade_date=pos["trade_date"],
            volume=(pos["volume"], "int"), open_interest=(pos["open_interest"], "int"),
            oi_change_1d=(pos["oi_change_1d"], "int"), oi_change_5d=(pos["oi_change_5d"], "int"),
            oi_change_20d=(pos["oi_change_20d"], "int"), oi_change_5d_pct=(pos["oi_change_5d_pct"], "pct"),
            volume_vs_20d_avg=pos["volume_vs_20d_avg"])
    for grp, d in ctx["oi_divergence"].items():
        if d.get("series"):
            last = d["series"][-1]
            _el(cp, "oi_divergence", group=grp, baseline_date=d["baseline_date"], latest_date=last["date"],
                standard_index=last["institutions"], micro_index=last["retail"],
                spread=last["institutions"] - last["retail"], note="size proxy, not identified ownership")
    if ctx["es_put_call"]:
        r = ctx["es_put_call"][-1]
        _el(cp, "es_options_put_call", trade_date=r["date"], calls=(r["calls"], "int"), puts=(r["puts"], "int"),
            ratio=r["ratio"])

    # 8. listed options positioning (SPY / SLV)
    for sym in ("SPY", "SLV"):
        o = ctx["options"].get(sym) or {}
        gx, mp, sm = o.get("gex") or {}, o.get("max_pain") or {}, o.get("summary") or {}
        spot = o.get("spot")

        def dist(x):
            return ((x / spot - 1), "pct") if (x is not None and spot) else None
        oe = _el(mf, "options_positioning", ticker=sym, status=o.get("status"), reason=o.get("reason"), spot=spot,
                 expirations=(o.get("expirations_captured"), "int"), contracts=(o.get("contracts_captured"), "int"))
        _el(oe, "gamma", net_gex_usd_per_1pt=gx.get("net_gex"),
            regime=None if gx.get("net_gex") is None else ("long_gamma" if gx["net_gex"] > 0 else "short_gamma"),
            call_wall=gx.get("call_wall"), put_wall=gx.get("put_wall"), zero_gamma=gx.get("zero_gamma"),
            zero_gamma_reason=gx.get("zero_gamma_reason"), spot_vs_call_wall_pct=dist(gx.get("call_wall")),
            spot_vs_put_wall_pct=dist(gx.get("put_wall")), spot_vs_zero_gamma_pct=dist(gx.get("zero_gamma")),
            expirations_used=",".join(gx.get("expirations_used") or []) or None, convention=metrics.GEX_PARAMS["convention"])
        _el(oe, "max_pain", strike=mp.get("strike"), expiration=o.get("front_expiration"),
            spot_vs_max_pain_pct=dist(mp.get("strike")), status=mp.get("status"), reason=mp.get("reason"))
        near, month = sm.get("atm_iv_near") or {}, sm.get("atm_iv_30d") or {}
        _el(oe, "flow_and_iv", put_call_volume_ratio=sm.get("put_call_volume_ratio"),
            put_call_oi_ratio=sm.get("put_call_oi_ratio"), call_volume=(sm.get("call_volume"), "int"),
            put_volume=(sm.get("put_volume"), "int"), call_oi=(sm.get("call_oi"), "int"), put_oi=(sm.get("put_oi"), "int"),
            atm_iv_near_pct=(near.get("atm_iv"), "pct"), atm_iv_near_expiry=near.get("expiration"),
            atm_iv_30d_pct=(month.get("atm_iv"), "pct"), atm_iv_30d_expiry=month.get("expiration"),
            iv_term_slope_pct=(sm.get("iv_term_slope"), "pct"),
            put_skew_90_pct=(month.get("put_skew_90") if month else near.get("put_skew_90"), "pct"))
        for c in sm.get("oi_concentration_expirations", []):
            _el(oe, "oi_concentration", expiration=c["expiration"], open_interest=(c["open_interest"], "int"))
        for label, c in (o.get("top") or {}).items():
            _el(oe, "top_contract", rank=label, contract=c.get("contract"), strike=c.get("strike"),
                expiration=c.get("expiration"), volume=(c.get("volume"), "int"), open_interest=(c.get("open_interest"), "int"),
                iv_pct=(c.get("iv"), "pct"), last=c.get("last_price"), bid=c.get("bid"), ask=c.get("ask"))

    # 9. block flow (proxy)
    for key in ("SPY", "SLV", "SPY_5d"):
        f = ctx["flow"].get(key) or {}
        fe = _el(mf, "block_flow", key=key, session=f.get("session"), status=f.get("status"), reason=f.get("reason"),
                 dataset="DBEQ.BASIC", venue_evidence="not verified", min_block=(f.get("min_block_size"), "int"),
                 trades_captured=(f.get("trades_captured"), "int"), limit_hit=f.get("limit_hit"),
                 blocks=(f.get("blocks"), "int"), block_volume=(f.get("block_volume"), "int"),
                 block_notional=(f.get("block_notional"), "int"), block_vwap=f.get("block_vwap"),
                 largest_block=(f.get("largest_block"), "int"), buy_aggressor=(f.get("buy_aggressor_volume"), "int"),
                 sell_aggressor=(f.get("sell_aggressor_volume"), "int"), unknown_side=(f.get("unknown_side_volume"), "int"),
                 bias=f.get("bias"), bias_method=f.get("bias_method"),
                 heuristic_above_vwap=(g(f, "vwap_heuristic_unknown_side", "at_or_above_vwap"), "int"),
                 heuristic_below_vwap=(g(f, "vwap_heuristic_unknown_side", "below_vwap"), "int"))
        for n in f.get("nodes", []):
            _el(fe, "price_node", price=n["price"], shares=(n["shares"], "int"))
        for pr in f.get("recent_prints", []):
            _el(fe, "print", time=pr["time"], price=pr["price"], size=(pr["size"], "int"), side_raw=pr["side_raw"],
                aggressor=pr["aggressor"])

    # 10. SPY technicals & breadth
    t = ctx["technicals"]
    _el(mf, "spy_technicals", status=t.get("status"), price=t.get("current_price"), ema20_1h=t.get("ema20_1h"),
        ema20_daily=t.get("ema20_daily"), sma20_weekly=t.get("sma20_weekly"), sma50=t.get("sma50"), sma200=t.get("sma200"),
        poc_30d=t.get("poc_30d"), rsi14=t.get("rsi14"), trend_1h=t.get("trend_1h"), trend_daily=t.get("trend_daily"),
        trend_weekly=t.get("trend_weekly"), alignment=t.get("alignment"), last_bar=t.get("last_bar"))
    br = ctx["breadth"]
    be = _el(mf, "breadth", condition=br.get("condition"), status=br.get("status"))
    for k, v in (br.get("pct") or {}).items():
        _el(be, "daily_change", symbol=k, pct=v)

    # 11. execution engine detail
    e = ctx["execution"]
    ee = _el(mf, "execution_engine", version=e.get("version"), score=e.get("total_score"), bias=e.get("directional_bias"),
             regime=e.get("regime"), status=e.get("status"), reason=e.get("reason"), target_strike=e.get("target_strike"),
             dte_window="-".join(str(x) for x in e["dte_window"]) if e.get("dte_window") else None)
    for v in e.get("votes", []):
        _el(ee, "vote", name=v["vote"], points=v["points"] if v["points"] is not None else "missing", why=v["why"])
    if isinstance(e.get("ticket"), dict):
        tk = e["ticket"]
        _el(ee, "ticket", contract=tk.get("contract"), type=tk.get("type"), strike=tk.get("strike"),
            expiration=tk.get("expiration"), bid=tk.get("bid"), ask=tk.get("ask"), last=tk.get("last"),
            iv_pct=(tk.get("iv"), "pct"), volume=(tk.get("volume"), "int"), open_interest=(tk.get("open_interest"), "int"),
            allocation=tk.get("allocation"))

    # 12. event risk & liquidity
    cal = ctx["calendar"]
    ups = cal.get("upcoming", [])
    high = [x for x in ups if x["impact"] == "High"]
    nxt = high[0] if high else None
    _el(mf, "event_risk", status=cal.get("status"), coverage_to=g(cal, "coverage", "to"),
        high_impact_usd_next_7d=(len(high), "int"), medium_impact_usd_next_7d=(len(ups) - len(high), "int"),
        high_impact_today=(len(cal.get("today_tier1", [])), "int"),
        next_high_impact=nxt["title"] if nxt else None,
        next_high_impact_ct=f"{nxt.get('date_ct')} {nxt.get('time_ct') or nxt['time']}" if nxt else None)
    lq = _el(mf, "liquidity_credit")
    for key, d in ctx["macro"].items():
        _el(lq, "series", key=key, series_id=d.get("series_id"), latest=d.get("latest"), date=d.get("date"),
            change_1d=d.get("1D"), change_1w=d.get("1W"), change_1m=d.get("1M"), change_1y=d.get("1Y"),
            unit=d.get("unit"), status=d.get("status"), reason=d.get("reason"))
    forecast_xml(mf, ctx.get("forecast"))
    refining_xml(mf, ctx.get("refining"))
    vm = ctx["vmri"]
    _el(lq, "vmri_inputs", dxy=g(vm, "inputs", "dxy"), tnx=g(vm, "inputs", "tnx"), oas=g(vm, "inputs", "oas"),
        vix=g(vm, "inputs", "vix"), formula=vm.get("formula"), version=vm.get("version"))


def refining_xml(parent, rf):
    if not rf:
        return
    re_ = _el(parent, "diesel_refining", version=rf.get("version"), status=rf.get("status"))
    m = rf.get("margins") or {}
    if m.get("status") == "fresh":
        for k in ("diesel", "gasoline", "three_two_one"):
            x = m[k]
            _el(re_, "crack", name=k, usd_bbl=x["latest"], change_1w=x["change_1w"], change_1m=x["change_1m"],
                percentile_5y=x["percentile_5y"], zscore_5y=x["zscore_5y"], seasonal_avg=x["seasonal_avg_this_week"],
                vs_seasonal=x["vs_seasonal"])
        sc = m.get("diesel_seasonal_4w_change") or {}
        _el(re_, "diesel_crack_seasonal_4w", avg_change_usd_bbl=sc.get("avg_usd_bbl"), up_years=(sc.get("up_years"), "int"),
            years=(sc.get("n_years"), "int"))
    f = rf.get("fundamentals") or {}
    fe = _el(re_, "eia_weekly", status=f.get("status"), days_of_supply=f.get("distillate_days_of_supply"))
    for k, x in f.items():
        if isinstance(x, dict) and "latest" in x:
            _el(fe, "series", key=k, latest=x["latest"], date=x.get("date"), avg_5y=x.get("avg_5y"), min_5y=x.get("min_5y"),
                max_5y=x.get("max_5y"), vs_avg_5y_pct=x.get("vs_avg_5y_pct"), change_1w=x.get("change_1w"))
    mt = rf.get("maintenance") or {}
    if mt.get("status") == "fresh":
        me = _el(re_, "maintenance", season=mt["season"], current_utilization=mt["current_utilization"],
                 trough_week=mt["expected_trough"]["week_ending"], trough_utilization=mt["expected_trough"]["expected_utilization"])
        for p_ in mt["path"]:
            _el(me, "week", ending=p_["week_ending"], expected_utilization=p_["expected_utilization"])
    o = rf.get("outages") or {}
    oe = _el(re_, "refinery_outages", coverage=o.get("coverage"), events_7d=(o.get("events_7d"), "int"),
             unplanned_7d=(o.get("unplanned_7d"), "int"), major_7d=(o.get("major_7d"), "int"),
             capacity_hit_unplanned_7d_bpd=(o.get("capacity_hit_unplanned_7d_bpd"), "int"),
             capacity_hit_unplanned_30d_bpd=(o.get("capacity_hit_unplanned_30d_bpd"), "int"))
    for e in (o.get("events") or [])[:15]:
        r = e.get("refinery") or {}
        _el(oe, "event", id=e.get("event_id"), start=e.get("start"), refinery=f"{r.get('company')} {r.get('site')}",
            capacity_bpd=(r.get("capacity_bpd"), "int"), kind=e.get("kind"), type=e.get("event_type"),
            units="; ".join(e.get("units_classified") or []) or None, duration=e.get("duration"), major=e.get("major"),
            url=e.get("url"))
    inv = rf.get("inventories") or {}
    if inv.get("status") == "fresh":
        ie = _el(re_, "eia_inventories", as_of=inv.get("as_of"), units="million barrels", weeks=(inv.get("weeks"), "int"),
                 total_incl_spr=g(inv, "total_incl_spr", "latest_mbbl"), total_change_4w=g(inv, "total_incl_spr", "change_4w_mbbl"))
        for r in inv["table"]:
            _el(ie, "stock", key=r["key"], label=r["label"], latest=r["latest_mbbl"], change_1w=r["change_1w_mbbl"],
                change_4w=r["change_4w_mbbl"], change_26w=r["change_26w_mbbl"], vs_5y_pct=r["vs_5y_pct"],
                days_of_supply=r["days_of_supply"])
        for k, f in (inv.get("flows") or {}).items():
            _el(ie, "flow", key=k, label=f["label"], latest_kbd=f["latest_kbd"], avg_4w_kbd=f["avg_4w_kbd"],
                vs_5y_pct=f["vs_5y_pct"], change_1w_kbd=f["change_1w_kbd"])
    u = rf.get("ulsd_positioning") or {}
    if u.get("net") is not None:
        _el(re_, "ulsd_managed_money", date=u["latest_date"], net=(u["net"], "int"), cot_index=u["cot_index"], read=u["read"])


def ntfy_summary(ctx):
    """Lock-screen summary for the single daily push: (title, message, priority 1-5, tags). Plain text (phones do not
    render Markdown); ranked by what is actionable; well under ntfy's 4,096-byte message limit."""
    from zoneinfo import ZoneInfo
    run = ctx["run"]
    t = datetime.fromisoformat(run["generated_at"]).astimezone(ZoneInfo("America/Chicago"))
    P, vm, e, fc = ctx["prices"], ctx["vmri"], ctx["execution"], ctx.get("forecast") or {}
    lines, prio, tags = [], 3, []

    def price(k):
        v = g(P, k, "value")
        return f"{v:,.2f}" if v is not None else "n/a"
    # 1. risk regime
    vix, w = g(P, "vix", "value"), ctx.get("weather") or {}
    term = (w.get("term_structure") or "").split(" ")[0].lower()
    tier = (vm.get("tier") or "").split(" (")[0]
    head = f"VMRI {vm['score']:.0f} {tier.replace(' RISK', '')}" if vm.get("score") is not None else "VMRI n/a"
    lines.append(head + (f" · VIX {vix:.1f} {term}" if vix is not None else ""))
    if tier.startswith(("ELEVATED", "SYSTEMIC")):
        prio = max(prio, 5 if tier.startswith("SYSTEMIC") else 4)
    # 2. execution engine
    score = e.get("total_score")
    tk = e.get("ticket")
    if score is None:
        lines.append("Engine: unavailable")
    else:
        bias = (e.get("directional_bias") or "").split(" (")[0]
        if isinstance(tk, dict) and tk.get("contract"):
            mid = (tk["bid"] + tk["ask"]) / 2 if tk.get("bid") and tk.get("ask") else tk.get("last")
            lines.append(f"Engine: {bias} {score:+d} → {tk['type']} SPY {tk['strike']:g} {str(tk['expiration'])[5:].replace('-', '/')}"
                         + (f" (${mid:.2f})" if mid else ""))
            prio = max(prio, 4)
        else:
            lines.append(f"Engine: CASH ({score:+d}, conflicting signals)" if abs(score) < 3 else f"Engine: {bias} {score:+d} (no contract)")
        tags.append("chart_with_upwards_trend" if score > 0 else "chart_with_downwards_trend" if score < 0 else "scales")
    # 3. SPY / SLV: price, 1-week implied range, trend
    for sym, key in (("SPY", "spy"), ("SLV", "slv")):
        s = fc.get(sym) or {}
        wk = ((s.get("implied") or {}).get("horizons") or {}).get("1w") or {}
        tr = s.get("trend") or {}
        rng = (f" · 1W {wk['range68'][0]:,.1f}–{wk['range68'][1]:,.1f}" + ("*" if wk.get("price_basis", "live bid/ask mids") != "live bid/ask mids" else "")
               if wk.get("range68") else "")
        flip = ((tr.get("signals") or {}).get("1m") or {}).get("flip_level")
        trend = f" · trend {tr['direction']}" + (f" (1m flip {flip:,.1f})" if flip and tr.get("direction") != "FLAT" else "") \
            if tr.get("direction") else ""
        lines.append(f"{sym} {price(key)}{rng}{trend}")
    # 4. COMEX + paper/physical
    inv, pp = ctx["inventory"], ctx["paper_physical"]
    if inv.get("registered") is not None:
        lines.append(f"COMEX reg {inv['registered'] / 1e6:.2f}M oz ({metrics.fmt_oz_change(inv.get('reg_change'))})"
                     + (f" · paper:phys {pp['ratio']:.1f}:1" if pp.get("ratio") is not None else ""))
    # 5. diesel / refining
    rf = ctx.get("refining") or {}
    d = (rf.get("margins") or {}).get("diesel") or {}
    ds = (rf.get("fundamentals") or {}).get("dist_stocks") or {}
    if d.get("latest") is not None:
        lines.append(f"Diesel crack ${d['latest']:.1f} ({d['percentile_5y']:.0f}th pct)"
                     + (f" · distillate {ds['vs_avg_5y_pct']:+.0f}% vs 5y" if ds.get("vs_avg_5y_pct") is not None else ""))
    o = rf.get("outages") or {}
    if o.get("unplanned_7d"):
        lines.append(f"Refineries: {o['unplanned_7d']} unplanned TX event(s) 7d" + (f", {o['major_7d']} MAJOR" if o.get("major_7d") else ""))
        if o.get("new_major_this_run"):
            prio = 5
            tags.append("oil_drum")
    # 6. what's next on the calendar
    cal, nxt = ctx["calendar"], []
    today_hi = cal.get("today_tier1") or []
    if today_hi:
        by_time = {}
        for x in today_hi:
            by_time.setdefault(x.get("time_ct") or x.get("time") or "?", []).append(x["title"])
        lines.append("🚨 Today: " + " | ".join(f"{tm} {', '.join(ts)}" for tm, ts in by_time.items())[:180])
        prio = max(prio, 4)
    cn = ((fc.get("SPY") or {}).get("calendar") or {}).get("next") or {}
    for label, key in (("CPI", "cpi"), ("OPEX", "opex"), ("FOMC", "fomc")):
        if cn.get(key):
            nxt.append(f"{label} {cn[key][5:].replace('-', '/')}")
    if nxt:
        lines.append("⏰ Next: " + " · ".join(nxt))
    # 7. data health
    problems = []
    for name, sec in (("CME", ctx["cme_volume"]), ("COMEX", inv), ("calendar", cal), ("SPY flow", ctx["flow"]["SPY"]),
                      ("options", ctx["options"].get("SPY", {})), ("EIA", rf.get("fundamentals") or {})):
        st = sec.get("status")
        if st in ("stale", "missing", "error"):
            problems.append(f"{name} {st}")
    if problems:
        lines.append("⚠ Data: " + ", ".join(problems))
        prio = max(prio, 4)
        tags.append("warning")
    else:
        lines.append("✓ All data sources fresh")
    if any(((((fc.get(sym) or {}).get("implied") or {}).get("horizons") or {}).get("1w") or {}).get("price_basis", "live bid/ask mids")
           != "live bid/ask mids" for sym in ("SPY", "SLV")):
        lines.append("* range not from live quotes (prior-session prices or provider IV; live quotes not yet posted)")
    lines.append("📎 Full report attached (same as the email)")
    title = f"📈 Market Brief · {t:%a %b} {t.day} · {t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'} CT"
    return title, "\n".join(lines), prio, tags or ["chart_with_upwards_trend"]


def diesel_text(ctx):
    rf = ctx.get("refining") or {}
    m, f, mt, o = rf.get("margins") or {}, rf.get("fundamentals") or {}, rf.get("maintenance") or {}, rf.get("outages") or {}
    lines = ["🛢️ DIESEL & REFINING", "=" * 40]
    if m.get("status") == "fresh":
        d = m["diesel"]
        lines.append(f"  Diesel crack: ${d['latest']:.2f}/bbl ({d['percentile_5y']:.0f}th pct 5y; {d['vs_seasonal']:+.2f} vs seasonal)"
                     if d.get("vs_seasonal") is not None else f"  Diesel crack: ${d['latest']:.2f}/bbl")
        lines.append(f"  3-2-1 crack: ${m['three_two_one']['latest']:.2f}/bbl · ULSD ${m['prices']['ulsd_usd_gal']:.3f}/gal")
    else:
        lines.append(f"  Margins unavailable ({m.get('reason')})")
    if f.get("util_us"):
        lines.append(f"  Refinery utilization: {f['util_us']['latest']:.1f}% ({f['util_us'].get('vs_avg_5y', 0):+.1f} pts vs 5y same week)"
                     + (f" · Gulf Coast {f['util_p3']['latest']:.1f}%" if f.get("util_p3") else ""))
    if f.get("dist_stocks"):
        ds = f["dist_stocks"]
        lines.append(f"  Distillate stocks: {ds['latest'] / 1000:.1f}M bbl ({ds.get('vs_avg_5y_pct', 0):+.1f}% vs 5y)"
                     + (f" · {f['distillate_days_of_supply']:.1f} days of supply" if f.get("distillate_days_of_supply") else ""))
    if mt.get("status") == "fresh":
        lines.append(f"  Season: {mt['season']} · expected utilization low {mt['expected_trough']['expected_utilization']:.1f}% "
                     f"(week ending {mt['expected_trough']['week_ending']})")
    lines.append(f"  Texas refinery events 7d: {o.get('events_7d', 0)} ({o.get('unplanned_7d', 0)} unplanned, {o.get('major_7d', 0)} major)")
    for e in (o.get("events") or [])[:3]:
        r = e.get("refinery") or {}
        lines.append(f"   - {str(e.get('start'))[:16]} {r.get('site')} ({(r.get('capacity_bpd') or 0) / 1000:.0f}k bpd): "
                     f"{e.get('event_type')} · {', '.join(e.get('units_classified') or []) or 'unit n/a'}")
    return "\n".join(lines)


def forecast_xml(parent, fc):
    """Forecast Lab summary (full detail lives in report_snapshot.json / the terminal's Forecast Lab tab)."""
    if not fc or fc.get("status") == "error":
        _el(parent, "forecast_lab", status=(fc or {}).get("status", "missing"), reason=(fc or {}).get("reason"))
        return
    fl = _el(parent, "forecast_lab", version=fc.get("version"), status=fc.get("status"),
             note="implied = risk-neutral (options); har = realized-vol forecast; trend = CTA replication")
    for sym in ("SPY", "SLV"):
        s = fc.get(sym) or {}
        te = _el(fl, "ticker", symbol=sym, spot=s.get("spot"))
        for h in fc.get("horizons", []):
            k = h["key"]
            imp = (s.get("implied", {}).get("horizons") or {}).get(k) or {}
            vol = (s.get("vol_forecast", {}).get("horizons") or {}).get(k) or {}
            cond = (s.get("trend", {}).get("conditional") or {}).get(k) or {}
            r68 = imp.get("range68") or [None, None]
            r90 = imp.get("range90") or [None, None]
            _el(te, "horizon", key=k, expiration=imp.get("expiration"), implied_p_up=imp.get("p_up"),
                implied_median=imp.get("median"), range68_low=r68[0], range68_high=r68[1], range90_low=r90[0],
                range90_high=r90[1], implied_move_1sd_pct=imp.get("expected_move_1sd_pct"),
                atm_iv_pct=(imp.get("atm_iv"), "pct"), p_up_5pct=imp.get("p_up_5pct"), p_down_5pct=imp.get("p_down_5pct"),
                skew=imp.get("skew"), har_vol_pct=(vol.get("forecast_vol"), "pct"),
                har_move_1sd_pct=vol.get("expected_move_1sd_pct"), vrp_pct=(vol.get("vrp"), "pct"), vol_read=vol.get("read"),
                trend_hist_p_up=cond.get("p_up_historical"), trend_hist_avg_fwd_pct=cond.get("avg_fwd_pct_historical"))
        tr = s.get("trend") or {}
        if tr.get("status") == "fresh":
            tn = _el(te, "trend", direction=tr["direction"], exposure=tr["exposure"], position_vol_scaled=tr.get("position_vol_scaled"))
            for lb, sg in tr["signals"].items():
                _el(tn, "signal", lookback=lb, signal=sg["signal"], return_pct=(sg["return"], "pct"), flip_level=sg["flip_level"],
                    flip_distance_pct=sg["flip_distance_pct"])
        cal = s.get("calendar") or {}
        if cal.get("status") == "fresh":
            ce = _el(te, "calendar", next_fomc=cal["next"]["fomc"], next_cpi=cal["next"]["cpi"], next_opex=cal["next"]["opex"],
                     drift_next_7d_pct=cal.get("calendar_drift_next_7d_pct"))
            for name, e in cal["effects"].items():
                _el(ce, "effect", name=name, n=(e.get("n"), "int"), mean_pct=e.get("mean_pct"), hit_rate=e.get("hit_rate"),
                    t=e.get("t_vs_baseline"))
        fl_ = s.get("flows") or {}
        _el(te, "flows", leveraged_etf_flow_per_1pct=(g(fl_, "leveraged_etfs", "flow_per_1pct_move"), "int"),
            vol_control_exposure=g(fl_, "vol_control", "exposure"), vol_control_flow_5d=(g(fl_, "vol_control", "flow_last_5d"), "int"),
            dealer_hedge_flow_per_1pct=(g(fl_, "dealer_gamma", "hedge_flow_per_1pct"), "int"), cta_exposure=g(fl_, "cta", "exposure"))
    pos = fc.get("positioning") or {}
    pe = _el(fl, "positioning")
    for grp, keys in (("silver", ("managed_money", "producer_merchant", "swap_dealers")),
                      ("sp500", ("leveraged_funds", "asset_managers", "dealers"))):
        for k in keys:
            st = (pos.get(grp) or {}).get(k)
            if st and "net" in st:
                _el(pe, "cot", market=grp, group=k, date=st["latest_date"], net=(st["net"], "int"), net_pct_oi=st["net_pct_oi"],
                    change_1w=(st["change_1w"], "int"), cot_index=st["cot_index"], zscore=st["zscore"], read=st["read"])
    tr_ = pos.get("slv_trust") or {}
    _el(pe, "slv_trust", ounces=(tr_.get("ounces_in_trust"), "int"), as_of=tr_.get("as_of"),
        change_vs_prev=(tr_.get("change_vs_prev_oz"), "int"), premium_discount_pct=tr_.get("premium_discount_pct"))
    fv = fc.get("silver_fair_value") or {}
    _el(fl, "silver_fair_value", status=fv.get("status"), price=fv.get("price"), fair_value=fv.get("fair_value"),
        gap_pct=fv.get("gap_pct"), residual_z=fv.get("residual_z"), r2=fv.get("r2"), half_life_weeks=fv.get("half_life_weeks"),
        gsr_zscore_5y=g(fv, "gold_silver_ratio", "zscore_5y"))
    m = fc.get("macro_regime") or {}
    _el(fl, "macro_regime", regime=m.get("regime"), recession_prob_12m=m.get("recession_prob_12m"),
        spread_10y3m=m.get("spread_10y3m_monthly_avg"), nfci=m.get("nfci"), sahm=m.get("sahm"),
        real_yield_10y=m.get("real_yield_10y"), flags="; ".join(m.get("flags") or []) or None)


# ============================================================ volume_dashboard.txt (XML)
def dashboard_xml(ctx, tactical_text):
    root = ET.Element("dashboard")
    ET.SubElement(root, "last_updated").text = datetime.fromisoformat(ctx["run"]["generated_at"]).astimezone(
        __import__("zoneinfo").ZoneInfo("America/Chicago")).strftime("%Y-%m-%d %I:%M %p")
    cv = ctx["cme_volume"]
    root.set("run_id", ctx["run"]["run_id"])
    root.set("cme_latest_trade_date", str(cv.get("latest_trade_date")))
    root.set("cme_status", cv["status"])
    prods = cv["products"]
    if not prods.get("ES_F") and not prods.get("SI_F"):
        ET.SubElement(root, "error").text = cv.get("reason") or "CME volume history unavailable"
    for tag, key in (("sp500", "ES_F"), ("silver", "SI_F")):
        el = ET.SubElement(root, tag)
        for r in prods.get(key, []):
            ET.SubElement(el, "day", date=r["date"], volume=str(r["volume"]), open_interest=str(r["open_interest"]))
    div = ctx["oi_divergence"]["silver"]
    if div.get("series"):
        el = ET.SubElement(root, "silver_divergence", baseline_date=div["baseline_date"], basis="size proxy")
        for r in div["series"]:
            ET.SubElement(el, "day", date=r["date"], institutions=f"{r['institutions']:.2f}", retail=f"{r['retail']:.2f}")
    if ctx["es_put_call"]:
        el = ET.SubElement(root, "sp500_put_call_flow", source="CME ES options")
        for r in ctx["es_put_call"]:
            ET.SubElement(el, "day", date=r["date"], calls=str(r["calls"]), puts=str(r["puts"]),
                          ratio=f"{r['ratio']:.2f}" if r["ratio"] is not None else "")
    if prods.get("ZN_F"):
        el = ET.SubElement(root, "ten_year_note", source="CBOT 10Y note futures")
        for r in prods["ZN_F"]:
            ET.SubElement(el, "day", date=r["date"], volume=str(r["volume"]),
                          oi_change=str(r["oi_change"]) if r["oi_change"] is not None else "nan")
    inv = ctx["inventory"]
    if inv.get("series"):
        el = ET.SubElement(root, "comex_inventory", unit="troy_oz", date_basis="report_date")
        cutoff = inv["series"][-1]["date"]
        from datetime import date as _d, timedelta as _td
        lo = (_d.fromisoformat(cutoff) - _td(days=30)).isoformat()
        for r in inv["series"]:
            if r["date"] < lo or r.get("registered") is None:
                continue
            tc = r.get("total_net_change")
            ET.SubElement(el, "day", date=r["date"], eligible=f"{r['eligible']:.2f}", registered=f"{r['registered']:.2f}",
                          total_change=f"{tc:+.0f}" if tc is not None else "")
    P = ctx["prices"]
    mp = ET.SubElement(root, "market_prices")
    for tag, key in (("slv", "slv"), ("si_f", "silver_futures"), ("spot", "silver_spot")):
        q = P.get(key) or {}
        el = ET.SubElement(mp, tag)
        if q.get("value") is not None:
            el.set("price", str(q["value"]))
            el.set("change", str(q["change"]) if q.get("change") is not None else "")
        el.set("status", q.get("status") or "missing")
    sh = ctx["shanghai"]
    el = ET.SubElement(mp, "shfe", source="SGE benchmark CNY/kg")
    if sh.get("cny_per_kg") is not None:
        el.set("price", str(sh["cny_per_kg"]))
        if sh.get("usd_per_oz") is not None:
            el.set("usd_oz", str(sh["usd_per_oz"]))
    el.set("status", sh.get("status", "missing"))
    ET.SubElement(mp, "usdcny").text = str(g(P, "usdcny", "value", default=NA))
    if inv.get("registered") is not None:
        ET.SubElement(root, "latest_vaults", registered=f"{inv['registered']:.1f}", eligible=f"{inv['eligible']:.1f}",
                      daily_change=metrics.fmt_oz_change(inv.get("total_change")), unit="troy_oz",
                      report_date=inv["report_date"], status=inv["status"])
    if tactical_text:
        root.append(ET.fromstring(tactical_text.encode("utf-8")))
    buf = io.BytesIO()
    ET.ElementTree(root).write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue().decode("utf-8")


def master_market_csv(ctx):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Product", "Type", "Volume", "Open_Interest", "Date", "OI_Change"])
    rows = []
    for key, series in ctx["cme_volume"]["products"].items():
        name = ctx["cme_volume"]["legacy_names"][key]
        fo = "O" if key.endswith(("_C", "_P")) else "F"
        for r in series:
            rows.append([name, fo, r["volume"], r["open_interest"], r["date"],
                         r["oi_change"] if r["oi_change"] is not None else ""])
    for r in sorted(rows, key=lambda x: (x[0], x[4])):
        w.writerow(r)
    return buf.getvalue()


# ============================================================ report text
def calendar_text(ctx):
    cal = ctx["calendar"]
    out = ["📅 7-DAY MACRO OUTLOOK (USD)", "=" * 40]
    if cal["status"] == "missing":
        out.append(f"Calendar unavailable — {cal.get('reason')}.")
        out.append("This is NOT a statement that no events are scheduled.")
        return "\n".join(out)
    for e in cal["upcoming"]:
        icon = "🚨" if e["impact"] == "High" else "⚠️"
        when = f"{e['date_ct']} @ {e['time_ct']} CT" if e.get("time_ct") else f"{e['date']} @ {e['time'] or 'TBD'}"
        out += [when, f"{icon} [{e['impact']}] {e['title']}",
                f"   Est: {e.get('forecast') or 'N/A'} | Prev: {e.get('previous') or 'N/A'}"
                + (f" | Actual: {e['actual']}" if e.get("actual") else ""), "-" * 40]
    if not cal["upcoming"]:
        out.append("No High/Medium impact USD events in the covered window.")
    if cal["status"] == "partial":
        out.append(f"⚠️ Coverage incomplete: {cal['reason']}")
    return "\n".join(out)


def _money(v, fmt="{:,.2f}"):
    return NA if v is None else "$" + fmt.format(v)


def _num(v, fmt="{:,.0f}"):
    return NA if v is None else fmt.format(v)


def slv_flow_text(ctx, flow_history=None):
    f = ctx["flow"]["SLV"]
    o = ctx["options"].get("SLV") or {}
    gx = o.get("gex") or {}
    status = f.get("status", "missing")
    if f.get("reason"):
        status += f" — {f['reason']}"
    t = ["\n🦅 SLV INSTITUTIONAL DARK POOL FLOW  (block-flow proxy: venue not verified)", "=" * 45,
         f"  Scan Date:       {ctx['run']['generated_local']}",
         f"  Data Session:    {f.get('session', NA)}",
         f"  Spot Price:      {_money(g(ctx, 'prices', 'slv', 'value'))}",
         f"  DP Sentiment:    {f.get('bias', NA)}" + (
             f" (VWAP heuristic; {f['aggressor_known_share']:.0%} of block volume has a provider side)"
             if f.get("bias_method") == "vwap_heuristic" else
             f" (aggressor-based, confidence {f['bias_confidence']})" if f.get("bias_confidence") else ""),
         f"  DP VWAP:         {_money(f.get('block_vwap'))}",
         f"  Block Volume:    {_num(f.get('block_volume'))}",
         f"  Notional (USD):  {_money(f.get('block_notional'))}",
         f"  Largest Block:   {_num(f.get('largest_block'))}",
         f"  Buy-Aggressor:   {_num(f.get('buy_aggressor_volume'))}",
         f"  Sell-Aggressor:  {_num(f.get('sell_aggressor_volume'))}",
         f"  Unknown Side:    {_num(f.get('unknown_side_volume'))}",
         f"  Data Status:     {status}",
         "-" * 45,
         f"  GEX Call Wall:   {_money(gx.get('call_wall'))}",
         f"  GEX Put Wall:    {_money(gx.get('put_wall'))}",
         f"  GEX Zero Gamma:  {_money(gx.get('zero_gamma')) if gx.get('zero_gamma') is not None else 'n/a — ' + str(gx.get('zero_gamma_reason') or gx.get('reason') or 'unavailable')}"]
    if flow_history:
        t.append("\n  📊 RECENT SENTIMENT TREND (ledger, legacy rows used reversed Databento sides before v2):")
        for row in flow_history[-5:]:
            t.append(f"    {row['date']} → {row['sentiment']} (VWAP: {row['vwap']})")
    t.append("=" * 45)
    return "\n".join(t)


def _json_block(d):
    return json.dumps(d, indent=2, default=str)


def market_brief_text(ctx):
    P, inv, sh, r, vm = ctx["prices"], ctx["inventory"], ctx["shanghai"], ctx["ratios"], ctx["vmri"]
    cal = ctx["calendar"]
    if cal["status"] == "missing":
        events = f"Calendar unavailable ({cal.get('reason')})"
    else:
        events = "\n  - ".join(f"{e.get('date_ct')} @ {e.get('time_ct') or e['time']} CT [{e['impact']}] {e['title']} "
                               f"(Est: {e.get('forecast') or 'N/A'})" for e in cal["upcoming"]) or "None in covered window"
    pc = ctx["es_put_call"][-1] if ctx["es_put_call"] else None
    if pc and pc["ratio"] is not None:
        ratio = pc["ratio"]
        pc_status = ("EXTREME FEAR (Heavy Put Buying)" if ratio > 2.0 else "BEARISH (Downside Hedging)"
                     if ratio > 1.3 else "BULLISH / NEUTRAL")
        pc_line = f"{ratio:.2f} ({pc_status}) [CME ES options, trade date {pc['date']}]"
    else:
        pc_line = "unavailable"
    div = ctx["oi_divergence"]["silver"]
    if div.get("series"):
        last = div["series"][-1]
        inst = f"{last['institutions']:.2f} [{'HIGH CONVICTION (Accumulating)' if last['institutions'] >= 80 else 'DISTRIBUTING'}]"
        retail = f"{last['retail']:.2f} [{'CAPITULATION' if last['retail'] < 50 else 'BUYING'}]"
        div_note = f"  (size proxy, base {div['baseline_date']}=100, latest {last['date']})"
    else:
        inst = retail = NA
        div_note = ""
    inv_note = f" (report {inv.get('report_date')}, {inv.get('status')})" if inv.get("report_date") else ""
    cme_note = ""
    if ctx["cme_volume"]["status"] == "stale":
        cme_note = f"\n⚠️ CME volume/OI is STALE: latest trade date {ctx['cme_volume']['latest_trade_date']}"
    eagles = []
    for i, l in enumerate([x for x in ctx["ebay"].get("listings", []) if x["total_cost"] is not None][:3]):
        prem = f"{l['premium_percent']:.2f}%" if l["premium_percent"] is not None else "n/a"
        name = l["title"] if len(l["title"]) <= 42 else l["title"][:39] + "..."
        eagles.append(f"  {i + 1}. ${l['total_cost']:.2f} | {prem} Prem | {name}")
    eagles_str = "\n".join(eagles) or f"  - No listings available ({ctx['ebay'].get('reason')})"

    o = ctx["options"].get("SPY") or {}
    gx, mpain = o.get("gex") or {}, o.get("max_pain") or {}
    gex_payload = {"spot": o.get("spot"), "net_gex_usd_per_1pt": gx.get("net_gex"),
                   "net_gamma_state": (None if gx.get("net_gex") is None else
                                       ("LONG GAMMA [Suppressing Volatility / Market Makers Buying Dips]"
                                        if gx["net_gex"] > 0 else
                                        "SHORT GAMMA [Violent Swings / Market Makers Selling Rips]")),
                   "front_week_max_pain": mpain.get("strike"), "max_pain_status": mpain.get("status"),
                   "max_pain_reason": mpain.get("reason"),
                   "call_wall": gx.get("call_wall"), "put_wall": gx.get("put_wall"),
                   "zero_gamma": gx.get("zero_gamma"), "zero_gamma_reason": gx.get("zero_gamma_reason"),
                   "convention": metrics.GEX_PARAMS["convention"], "status": gx.get("status") or o.get("status", "missing"),
                   "reason": gx.get("reason") or o.get("reason")}
    f = ctx["flow"]["SPY"]
    dp_payload = {"status": f.get("status"), "reason": f.get("reason"), "label": f.get("label"),
                  "venue_evidence": f.get("venue_evidence"), "session": f.get("session"),
                  "data": {"ticker": "SPY", "total_block_volume": f.get("block_volume"),
                           "total_notional_usd": f.get("block_notional"), "largest_single_block": f.get("largest_block"),
                           "vwap_price": f.get("block_vwap"),
                           "sentiment": {"bias": f.get("bias"), "method": f.get("bias_method"),
                                         "aggressor_known_share": f.get("aggressor_known_share"),
                                         "buy_aggressor_volume": f.get("buy_aggressor_volume"),
                                         "sell_aggressor_volume": f.get("sell_aggressor_volume"),
                                         "unknown_side_volume": f.get("unknown_side_volume")},
                           "5_day_support_resistance_nodes": [f"${n['price']:.2f} ({n['shares']:,} shares)"
                                                               for n in ctx["flow"]["SPY_5d"].get("nodes", [])],
                           "recent_prints": f.get("recent_prints"), "trades_captured": f.get("trades_captured"),
                           "limit_hit": f.get("limit_hit")}}
    tech = ctx["technicals"]
    tech_payload = {"status": tech.get("status"), "reason": tech.get("reason"),
                    "data": {"current_price": tech.get("current_price"), "volume_profile_30d_poc": tech.get("poc_30d"),
                             "mtf_alignment": tech.get("alignment"),
                             "trend_matrix": {"1H": tech.get("trend_1h"), "Daily": tech.get("trend_daily"),
                                              "Weekly": tech.get("trend_weekly"), "vs_200_SMA": tech.get("vs_200_sma")},
                             "rsi_14d": tech.get("rsi14")}}
    w = ctx["weather"]
    b = ctx["breadth"]
    br_payload = {"status": b.get("status"), "reason": b.get("reason"),
                  "data": {"market_condition": b.get("condition"),
                           **{f"{k}_daily_change": (f"{v:+.2f}%" if v is not None else None)
                              for k, v in (b.get("pct") or {}).items()}}}
    e = ctx["execution"]
    eng_payload = {"status": e.get("status"), "reason": e.get("reason"),
                   "data": {"total_score": e.get("total_score"), "directional_bias": e.get("directional_bias"),
                            "regime": e.get("regime"),
                            "matrix_breakdown": [f"{v['vote']}: {'missing' if v['points'] is None else f'{v['points']:+d}'} ({v['why']})"
                                                 for v in e.get("votes", [])],
                            "optimal_strike": {"strike_price": e.get("target_strike"), "rationale": e.get("strike_rationale")},
                            "optimal_expiration": {"dte_window": e.get("dte_window"), "rationale": e.get("expiration_rationale")},
                            "live_trade_ticket": e.get("ticket")}}
    shfe_prem = signed(sh.get("premium_tax_adj"))
    return f"""🌅 MORNING MARKET BRIEF 🌅
{ctx['run']['generated_local']}{cme_note}

🚨 VLAD MACRO RISK INDEX (VMRI)
  - Score: {f2(vm.get('score'), na='UNAVAILABLE')}
  - Threat Level: {vm.get('tier')}{'' if vm.get('score') is not None else ' — ' + str(vm.get('reason'))}

⏰ CATALYST CALENDAR
  - Status: {cal.get('catalyst_status')}
  - Scheduled Events:
  - {events}

🏦 COMEX INVENTORY{inv_note}
  - Registered: {metrics.fmt_moz(inv.get('registered'))}
  - Eligible: {metrics.fmt_moz(inv.get('eligible'))}
  - Daily Change: {metrics.fmt_oz_change(inv.get('total_change'))} (net; adjustments {metrics.fmt_oz_change(inv.get('total_adjustment'))})

💰 PRECIOUS METALS & ARBS
  - Gold Price: {_money(g(P, 'gold', 'value'))} (GC=F)
  - Gold/Silver Ratio: {f2(r.get('gold_silver'))}
  - Spot Silver: {_money(g(P, 'silver_spot', 'value'))} (GoldAPI XAG{'' if g(P, 'silver_spot', 'value') is not None else ': ' + str(g(P, 'silver_spot', 'reason'))})
  - COMEX Silver Futures: {_money(g(P, 'silver_futures', 'value'))} (SI=F continuous)

🇨🇳 SHANGHAI PHYSICAL ARBITRAGE (SGE)
  - SGE Quote: {f"¥{sh['cny_per_kg']:,.2f}" if sh.get('cny_per_kg') else NA} CNY/kg ({sh.get('sge_date', NA)})
  - USD Equiv: {_money(sh.get('usd_per_kg'))}/kg ({_money(sh.get('usd_per_oz'))}/oz)
  - Tax-Adj Premium: {shfe_prem}/oz vs SI=F (assumes 13% VAT)

📉 S&P 500 OPTIONS FLOW
  - Put/Call Ratio: {pc_line}
  - VIX (Volatility): {f2(g(P, 'vix', 'value'))} ({signed(g(P, 'vix', 'change'))})

₿ DIGITAL GOLD & METALS RATIOS
  - Bitcoin Price: {_money(g(P, 'btc', 'value'))}
  - Silver/BTC Ratio: {f2(r.get('btc_silver_oz'))} oz per BTC
  - Gold/BTC Ratio: {f2(r.get('btc_gold_oz'))} oz per BTC

🧠 SILVER DIVERGENCE{div_note}
  - Institutions: {inst}
  - Retail: {retail}

🌍 MACRO & LIQUIDITY
  - 10Y Yield: {f2(g(P, 'tnx', 'value'))}%
  - DXY (Dollar): {f2(g(P, 'dxy', 'value'))}
  - High Yield OAS: {f2(ctx['macro']['oas'].get('latest'))}
  - Fed Reverse Repo: {_money(ctx['macro']['rrp'].get('latest'))}B

🦅 PHYSICAL ARBITRAGE (CHEAPEST EAGLES on eBay, benchmark {_money(g(ctx, 'ebay', 'benchmark', 'value'))} SI=F)
{eagles_str}

========================================
 🦅 INSTITUTIONAL FLOW ($SPY)
========================================
🧠 GEX DATA (Options Engine):
{_json_block(gex_payload)}

🌊 BLOCK-FLOW DATA (legacy "Dark Pool"; DBEQ.BASIC, venue not verified):
{_json_block(dp_payload)}

⚔️ THE BATTLEFIELD (Advanced Technicals):
{_json_block(tech_payload)}

🌪️ THE WEATHER (Volatility & Term Structure):
{_json_block({'status': w.get('status'), 'reason': w.get('reason'), 'data': {'spot_vix': w.get('vix'), 'vix_3m': w.get('vix3m'), 'term_structure': w.get('term_structure')}})}

🩻 UNDER THE HOOD (Market Breadth):
{_json_block(br_payload)}

🎯 ALGORITHMIC EXECUTION ENGINE:
{_json_block(eng_payload)}
"""


def _contract_block(c, title):
    if not c:
        return f"  [{title}] unavailable"
    iv = f"{c['iv'] * 100:.2f}%" if c.get("iv") is not None else "n/a"
    vol = f"{c['volume']:,.0f}" if c.get("volume") is not None else "unknown"
    oi = f"{c['open_interest']:,.0f}" if c.get("open_interest") is not None else "unknown"
    lp = f"${c['last_price']:.2f}" if c.get("last_price") is not None else "n/a"
    return (f"  [{title}] Strike: ${c['strike']} | Exp: {c['expiration']}\n"
            f"    - Volume: {vol} | Open Interest: {oi}\n"
            f"    - Last Price: {lp} | Implied Volatility: {iv}\n"
            f"    - Symbol: {c['contract']}")


def options_text(ctx):
    out = ["\n--- OPTIONS WHALE SCANNER ---", f"Time: {ctx['run']['generated_local']}\n"]
    for sym, label in (("SPY", " 🦅 SPY (S&P 500 ETF) OPTIONS"), ("SLV", " 🪙 SLV (SILVER TRUST) OPTIONS")):
        o = ctx["options"].get(sym) or {}
        out += ["\n" + "=" * 40, label, "=" * 40]
        if o.get("status") == "missing":
            out.append(f"  unavailable — {o.get('reason')}")
            continue
        t = o.get("top", {})
        out += [f"  Coverage: {o.get('expirations_captured')}/{len(o.get('expirations', []))} expirations, "
                f"{o.get('contracts_captured'):,} contracts captured", "🔥 CALLS:",
                _contract_block(t.get("top_vol_call"), "Highest Volume Call"),
                _contract_block(t.get("top_oi_call"), "Highest OI Call"), "\n🩸 PUTS:",
                _contract_block(t.get("top_vol_put"), "Highest Volume Put"),
                _contract_block(t.get("top_oi_put"), "Highest OI Put")]
    out.append("\n--- SCAN COMPLETE ---")
    return "\n".join(out)


def institutional_json(ctx, spy_row, slv_row):
    return json.dumps({"slv_institutional_latest": slv_row, "spy_institutional_latest": spy_row,
                       "run_id": ctx["run"]["run_id"], "system_timestamp": ctx["run"]["generated_at"]}, indent=2,
                      default=str)


def data_status_text(ctx, manifest):
    lines = ["📋 DATA STATUS (this run)", "=" * 40]
    for name, sec in (("CME volume/OI", ctx["cme_volume"]), ("COMEX inventory", ctx["inventory"]),
                      ("Calendar", ctx["calendar"]), ("SPY block flow", ctx["flow"]["SPY"]),
                      ("SLV block flow", ctx["flow"]["SLV"]), ("SPY options", ctx["options"].get("SPY", {})),
                      ("SLV options", ctx["options"].get("SLV", {})), ("SGE/Shanghai", ctx["shanghai"]),
                      ("eBay", ctx["ebay"]), ("VMRI", ctx["vmri"]), ("Execution", ctx["execution"])):
        lines.append(f"  {name:<16} {sec.get('status', 'missing'):<8} {sec.get('reason') or ''}"[:160])
    missing_q = [k for k, q in ctx["prices"].items() if q.get("value") is None]
    if missing_q:
        lines.append(f"  Quotes missing:  {', '.join(missing_q)}")
    bad = [c for c in manifest if c["status"] not in ("generated",)]
    lines.append(f"  Charts:          {sum(c['status'] == 'generated' for c in manifest)} generated, "
                 f"{len(bad)} other ({', '.join(c['file'] + '=' + c['status'] for c in bad)[:300]})")
    return "\n".join(lines)


def full_report(ctx, manifest, *, flow_history=None, spy_row=None, slv_row=None, appendix=""):
    parts = [calendar_text(ctx), slv_flow_text(ctx, flow_history), market_brief_text(ctx), options_text(ctx)]
    if ctx.get("refining"):
        parts.append(diesel_text(ctx))
    parts += [data_status_text(ctx, manifest),
             "========================================\n ⚡ RAW INSTITUTIONAL JSON SNAPSHOT\n"
             "========================================\n" + institutional_json(ctx, spy_row, slv_row)]
    text = "\n\n".join(parts)
    if appendix:
        text += appendix
    return text


# ============================================================ legacy ledger rows
def _v(x, rnd=None, na="N/A"):
    if x is None:
        return na
    return round(x, rnd) if rnd is not None else x


def ledger_rows(ctx):
    P, local = ctx["prices"], ctx["run"]["generated_local"]
    ts_min = datetime.fromisoformat(ctx["run"]["generated_at"]).astimezone(
        __import__("zoneinfo").ZoneInfo("America/Chicago"))
    stamp = ts_min.strftime("%Y-%m-%d %H:%M")
    rows = {}
    dp = []
    for sym in ("SPY", "SLV"):
        f = ctx["flow"][sym]
        gx = (ctx["options"].get(sym) or {}).get("gex") or {}
        dp.append({"Date": stamp, "Ticker": sym, "Spot_Price": _v(g(P, sym.lower(), "value"), 2),
                   "DP_Sentiment": f.get("bias") or "N/A", "DP_Total_Vol": _v(f.get("block_volume")),
                   "DP_Notional_USD": _v(f.get("block_notional")), "DP_Largest_Block": _v(f.get("largest_block")),
                   "DP_VWAP": _v(f.get("block_vwap"), 2), "DP_Bull_Vol": _v(f.get("buy_aggressor_volume")),
                   "DP_Bear_Vol": _v(f.get("sell_aggressor_volume")), "GEX_Call_Wall": _v(gx.get("call_wall")),
                   "GEX_Put_Wall": _v(gx.get("put_wall")), "GEX_Zero_Gamma": _v(gx.get("zero_gamma"), 2)})
    rows["equities_darkpool_gex_ledger"] = dp
    o, f, t, w, b = (ctx["options"].get("SPY") or {}), ctx["flow"]["SPY"], ctx["technicals"], ctx["weather"], ctx["breadth"]
    gx, pct = o.get("gex") or {}, (b.get("pct") or {})
    rows["institutional_ledger"] = [{
        "Date": ts_min.strftime("%Y-%m-%d %H:%M:%S"), "Ticker": "SPY", "Spot_Price": _v(o.get("spot"), 2),
        "Net_Gamma": _v(gx.get("net_gex")), "Max_Pain": _v(g(o, "max_pain", "strike")),
        "Call_Wall": _v(gx.get("call_wall")), "Put_Wall": _v(gx.get("put_wall")),
        "DP_Sentiment": f.get("bias") or "N/A", "DP_Volume": _v(f.get("block_volume")),
        "DP_Notional": _v(f.get("block_notional"), 2), "DP_VWAP": _v(f.get("block_vwap"), 2),
        "DP_Largest_Block": _v(f.get("largest_block")), "Tech_30d_POC": _v(t.get("poc_30d"), 2),
        "Tech_20_EMA": _v(t.get("ema20_daily"), 2), "Tech_50_SMA": _v(t.get("sma50"), 2),
        "Tech_200_SMA": _v(t.get("sma200"), 2), "Tech_14d_RSI": _v(t.get("rsi14"), 2), "Tech_Divergence": "N/A",
        "VIX_Spot": _v(w.get("vix"), 2), "VIX_3M": _v(w.get("vix3m"), 2),
        "VIX_Term_Struct": (w.get("term_structure") or "N/A").split(" ")[0],
        "Breadth_Condition": (b.get("condition") or "N/A").split(" ")[0],
        "SPY_Daily_Pct": _v(pct.get("SPY"), 2), "RSP_Daily_Pct": _v(pct.get("RSP"), 2),
        "NVDA_Pct": _v(pct.get("NVDA"), 2), "AAPL_Pct": _v(pct.get("AAPL"), 2), "MSFT_Pct": _v(pct.get("MSFT"), 2)}]
    vm, sh, m, pp, eb = ctx["vmri"], ctx["shanghai"], ctx["macro"], ctx["paper_physical"], ctx["ebay"]
    # GEX: SPY net dealer gamma, USD of delta change per $1 move (the same figure as institutional_ledger.Net_Gamma);
    # empty when the run could not compute it. DIX has no data source and stays empty.
    net_gex = gx.get("net_gex") if gx.get("status") == "fresh" else None
    rows["macro_master_ledger"] = [{
        "Datetime": stamp, "VMRI_Score": vm.get("score"), "Threat_Tier": vm.get("tier"),
        "DXY": g(P, "dxy", "value"), "DXY_Change": g(P, "dxy", "change"), "10Y_Yield": g(P, "tnx", "value"),
        "ZN_Futures": g(P, "zn", "value"), "High_Yield_OAS": m["oas"].get("latest"), "VIX": g(P, "vix", "value"),
        "VIX_Change": g(P, "vix", "change"), "WTI_Crude": g(P, "wti", "value"), "Brent_Crude": g(P, "brent", "value"),
        "Gold_Price": g(P, "gold", "value"), "Gold_Silver_Ratio": ctx["ratios"].get("gold_silver"),
        "SHFE_Silver_USD": sh.get("usd_per_oz"), "COMEX_Silver": g(P, "silver_futures", "value"),
        "SHFE_Premium": sh.get("premium_tax_adj"), "GEX": net_gex, "DIX": None,
        "Reverse_Repo_BN": m["rrp"].get("latest"), "Fed_Balance_Sheet_BN": m["walcl"].get("latest"),
        "Retail_Silver_Cheapest": eb.get("cheapest"), "Retail_Silver_Avg": eb.get("average"),
        "Silver_OI": pp.get("paper_claims_oz") / 5000 if pp.get("paper_claims_oz") else None,
        "Paper_Physical_Ratio": round(pp["ratio"], 2) if pp.get("ratio") is not None else None}]
    bench = g(eb, "benchmark", "value")
    if eb.get("cheapest") is not None and bench:
        ch, av = eb["cheapest"], eb["average"]
        rows["physical_arbitrage_ledger"] = [{
            "Datetime": stamp, "COMEX_Spot": round(bench, 2), "Cheapest_Eagle": round(ch, 2), "Average_Eagle": round(av, 2),
            "Cheapest_Premium_Dollars": round(ch - bench, 2), "Cheapest_Premium_Percent": round((ch - bench) / bench * 100, 2),
            "Average_Premium_Dollars": round(av - bench, 2), "Average_Premium_Percent": round((av - bench) / bench * 100, 2),
            "Dealers_Scanned": len(eb["listings"])}]
    r = ctx["ratios"]
    if r.get("btc_silver_oz") and r.get("btc_gold_oz"):
        rows["crypto_metrics_history"] = [{
            "Date": ts_min.strftime("%Y-%m-%d"), "BTC_Price": g(P, "btc", "value"),
            "Silver_Price": g(P, "silver_futures", "value"), "Gold_Price": g(P, "gold", "value"),
            "Silver_BTC_Ratio": r["btc_silver_oz"], "Gold_BTC_Ratio": r["btc_gold_oz"]}]
    return rows


# ============================================================ HTML dashboard
def _fmt_change(val, prefix="$"):
    if val is None:
        return "n/a", ""
    cls = "up" if val > 0 else "down" if val < 0 else ""
    return f"{'+' if val >= 0 else '-'}{prefix}{abs(val):,.2f}", cls


def report_card_html(report_text):
    """Copyable full email text (the saved daily_market_report.txt)."""
    return f"""
        <div class="card" style="border-top: 3px solid var(--accent-teal);">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
                <h2 style="margin:0;">Daily Market Report (email text)</h2>
                <div>
                    <span id="report-copy-status" style="font-size:.8rem;color:var(--accent-teal);margin-right:10px;"></span>
                    <button type="button" onclick="copyReportText()"
                        style="background:var(--accent-teal);color:#0b0c10;border:0;border-radius:6px;padding:8px 16px;font-weight:700;cursor:pointer;">Copy</button>
                </div>
            </div>
            <textarea id="report-text" readonly spellcheck="false"
                style="width:100%;box-sizing:border-box;height:520px;margin-top:14px;resize:vertical;background:#181c20;color:#c5c6c7;border:1px solid rgba(102,252,241,.15);border-radius:8px;padding:14px;font-family:'Fira Mono','Menlo','Consolas',monospace;font-size:.85rem;line-height:1.35;white-space:pre;">{html.escape(report_text or '')}</textarea>
            <script>
            function copyReportText() {{
                var t = document.getElementById('report-text'), s = document.getElementById('report-copy-status');
                function done(ok) {{ s.textContent = ok ? 'Copied ' + t.value.length.toLocaleString() + ' characters' : 'Select all (Cmd+A) and copy'; }}
                if (navigator.clipboard && window.isSecureContext) {{
                    navigator.clipboard.writeText(t.value).then(function () {{ done(true); }}, function () {{ fallback(); }});
                }} else {{ fallback(); }}
                function fallback() {{ t.focus(); t.select(); var ok = false; try {{ ok = document.execCommand('copy'); }} catch (e) {{}} done(ok); }}
            }}
            </script>
        </div>
"""


def dashboard_html(ctx, template, tactical_text, manifest, report_text=None):
    P, sh, inv = ctx["prices"], ctx["shanghai"], ctx["inventory"]
    h = template
    for key, ph in (("slv", "SLV"), ("silver_futures", "SIH26"), ("silver_spot", "SPOT")):
        q = P.get(key) or {}
        val, chg = q.get("value"), q.get("change")
        c, cls = _fmt_change(chg)
        h = h.replace(f"{{{{{ph}_PRICE}}}}", f"${val:,.2f}" if val is not None else "n/a")
        h = h.replace(f"{{{{{ph}_CHANGE}}}}", c).replace(f"{{{{{ph}_CLASS}}}}", cls)
        pct = (chg / (val - chg) * 100) if (val is not None and chg is not None and val - chg) else None
        h = h.replace(f"{{{{{ph}_PCT}}}}", f"{pct:+.2f}%" if pct is not None else "")
    spread = sh.get("raw_spread")
    s_str, s_cls = _fmt_change(spread, "$")
    h = h.replace("{{SHFE_PRICE}}", f"¥{sh['cny_per_kg']:,.0f}" if sh.get("cny_per_kg") else "n/a")
    h = h.replace("{{SHFE_CHANGE}}", f"<span class='{s_cls}' style='font-size:0.8rem'>Raw spread vs SI=F: {s_str}/oz</span>")
    h = h.replace("{{SHFE_CLASS}}", "").replace("{{SHFE_PCT}}", "")
    h = h.replace("COMEX (SIH26)</span>", "COMEX (SI=F cont.)</span>").replace(
        '<span class="price-label">SHFE (Shanghai)</span>', '<span class="price-label">SGE (Shanghai)</span>')
    h = h.replace("{{REG_OZ}}", metrics.fmt_moz(inv.get("registered")))
    h = h.replace("{{ELIG_OZ}}", metrics.fmt_moz(inv.get("eligible")))
    cv = inv.get("total_change")
    h = h.replace("{{DAILY_CHANGE}}", metrics.fmt_oz_change(cv) + (f" <small>(report {inv['report_date']})</small>"
                                                                    if inv.get("report_date") else ""))
    h = h.replace("{{CHANGE_COLOR}}", "down" if (cv or 0) < 0 else "up" if (cv or 0) > 0 else "")
    h = h.replace("{{LAST_UPDATED}}", html.escape(ctx["run"]["generated_local"]))
    h = h.replace("<h2>Macro Wrecking Ball (10Y Note Yields)</h2>",
                  "<h2>Macro Wrecking Ball — 10Y Note Futures (ZN) Volume &amp; OI Change</h2>")
    h = h.replace("<h2>S&P 500 (ES) Put/Call Options Flow</h2>", "<h2>CME E-mini S&amp;P 500 (ES) Options Put/Call Flow</h2>")
    h = h.replace("<h2>Silver: Smart Money vs. Dumb Money</h2>",
                  "<h2>Silver: Standard vs. Micro OI (size proxy for Smart vs. Retail)</h2>")

    by_file = {c["file"]: c for c in manifest}
    import re as _re

    def img_sub(m):
        fname = m.group(1)
        c = by_file.get(fname)
        if c and c["status"] in ("generated", "stale_input"):
            note = (f"<div style='color:#f5a623;font-size:.8rem'>⚠ {html.escape(c.get('reason') or 'stale input')}</div>"
                    if c["status"] == "stale_input" else "")
            return m.group(0) + note
        why = html.escape((c or {}).get("reason") or "chart not produced this run")
        return (f"<div style='padding:40px;border:1px dashed #555;border-radius:6px;color:#999'>"
                f"Chart unavailable: {why}</div>")
    h = _re.sub(r'<img src="(chart[^"]+\.png)"[^>]*>', img_sub, h)

    status_rows = "".join(
        f"<tr><td>{html.escape(n)}</td><td>{html.escape(str(s.get('status')))}</td>"
        f"<td>{html.escape(str(s.get('reason') or ''))}</td></tr>"
        for n, s in (("CME volume/OI", ctx["cme_volume"]), ("COMEX inventory", inv), ("Calendar", ctx["calendar"]),
                     ("SPY block flow", ctx["flow"]["SPY"]), ("SLV block flow", ctx["flow"]["SLV"]),
                     ("SGE / Shanghai", sh), ("eBay", ctx["ebay"]), ("VMRI", ctx["vmri"])))
    status_card = f"""
        <div class="card" style="border-top: 3px solid #f5a623;">
            <h2>Run Status — {html.escape(ctx['run']['run_id'])}</h2>
            <table style="width:100%;font-size:.85rem;border-collapse:collapse" cellpadding="4">
              <tr style="text-align:left;color:var(--accent-teal)"><th>Source</th><th>Status</th><th>Detail</th></tr>
              {status_rows}
            </table>
        </div>
"""
    extra = []
    for fam in (7, 8, 9, 10, 11):
        cells = []
        for c in [c for c in manifest if c["family"] == fam]:
            if c["status"] in ("generated", "stale_input"):
                cells.append(f"<div class='chart-box'><h3>{html.escape(c['window_label'])}</h3>"
                             f"<img src='{c['file']}' alt='{html.escape(c['title'])}'></div>")
            else:
                cells.append(f"<div class='chart-box'><h3>{html.escape(c['window_label'])}</h3>"
                             f"<div style='padding:40px;border:1px dashed #555;color:#999'>Chart unavailable: "
                             f"{html.escape(c.get('reason') or '')}</div></div>")
        title = next((c["title"] for c in manifest if c["family"] == fam), "")
        extra.append(f"<div class='card'><h2>{html.escape(title)}</h2><div class='chart-comparison'>{''.join(cells)}</div></div>")
    tactical_card = f"""
<div class="card" style="border-top: 3px solid var(--accent-blue); margin-top: 30px;">
  <h2 style="color: var(--accent-blue);">Tactical Ruling: Macro Debrief</h2>
  <pre style="font-family: 'Fira Mono', 'Consolas', 'Menlo', monospace; background: #181c20; color: #c5c6c7; padding: 18px; border-radius: 8px; font-size: 0.95rem; white-space: pre-wrap; margin: 0; overflow-x: auto;">{html.escape(tactical_text or '')}</pre>
</div>
"""
    h = h.replace('<div class="grid-container">', '<div class="grid-container">' + status_card, 1)
    if report_text is not None:
        anchor = '        <div class="card">\n            <h2>COMEX Physical Silver Inventory</h2>'
        if anchor in h:
            h = h.replace(anchor, report_card_html(report_text) + anchor, 1)
        else:  # template changed: still show it right after the status card
            h = h.replace(status_card, status_card + report_card_html(report_text), 1)
    h = h.replace("    </div>\n</body>", "\n".join(extra) + tactical_card + "\n    </div>\n</body>")
    return h
