"""Shared calculations. Each metric has exactly one owner here; SPY and SLV use the same definitions.

Conventions (recorded in snapshots as `derivation` versions):
- Missing inputs produce None plus a reason; they never silently become 0 or 1.
- IV is a fraction (0.25 == 25%); formatting to percent happens in rendering.
"""
import math

import numpy as np
import pandas as pd
from scipy.stats import norm

VMRI_VERSION = "vmri_v1"            # unchanged formula and tiers from tactical_ruling.py
GEX_VERSION = "gex_v2"              # one parameter set for every ticker (was ±15% scanner / ±10% reader)
ZERO_GAMMA_VERSION = "zero_gamma_v1"
FLOW_VERSION = "block_flow_v2"      # Databento side semantics corrected (A = sell aggressor, B = buy aggressor)
MAX_PAIN_VERSION = "max_pain_v2"    # contracts with unknown OI are excluded, coverage recorded
DIVERGENCE_VERSION = "oi_divergence_v2"
EXECUTION_VERSION = "execution_v2"  # legacy weights; missing inputs no longer vote as zero

GEX_PARAMS = {
    "expirations": 3, "risk_free_rate": 0.05, "multiplier": 100, "wall_window_pct": 0.10,
    "min_iv": 0.01, "min_T_years": 0.001,
    "convention": "dealer long calls / short puts: call GEX positive, put GEX negative",
    "units": "gamma x OI x multiplier x spot = USD of delta change per $1 underlying move",
}
BLOCK_MIN_SIZE = 10000
SENTIMENT_RATIO = 1.2

# Databento trades `side`: A = Ask (sell aggressor), B = Bid (buy aggressor), N = None/unknown.
DATABENTO_SIDE = {"A": "sell", "B": "buy", "N": "unknown"}


def safe_float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


# ---------------------------------------------------------------- VMRI (preserved)
def vmri(dxy, tnx, oas, vix):
    inputs = {"dxy": dxy, "tnx": tnx, "oas": oas, "vix": vix}
    missing = [k for k, v in inputs.items() if v is None]
    if missing:
        return {"score": None, "tier": "INCOMPLETE DATA", "components": None, "inputs": inputs,
                "status": "missing", "reason": f"missing inputs: {', '.join(missing)}", "version": VMRI_VERSION}
    base_stress = (dxy * tnx) / 1.61
    credit_multiplier = oas / 4.00
    vol_premium = vix / 20.00
    score = base_stress * credit_multiplier * vol_premium
    if score < 150:
        tier = "LOW RISK (Complacent / Squeeze Danger)"
    elif score < 250:
        tier = "MODERATE RISK (Standard Operating Environment)"
    elif score < 350:
        tier = "ELEVATED RISK (Hedge Triggers Active)"
    else:
        tier = "SYSTEMIC THREAT (Crash Dynamics Active)"
    return {"score": score, "tier": tier, "inputs": inputs, "status": "fresh", "reason": None,
            "components": {"base_stress": base_stress, "credit_multiplier": credit_multiplier,
                           "volatility_premium": vol_premium},
            "formula": "(DXY*TNX/1.61) * (OAS/4.00) * (VIX/20.00)", "version": VMRI_VERSION}


def paper_physical(silver_oi, registered_oz):
    if silver_oi is None or not registered_oz:
        return {"ratio": None, "status": "missing",
                "reason": "silver OI or registered inventory unavailable", "tier": "UNAVAILABLE"}
    paper = silver_oi * 5000
    ratio = paper / registered_oz
    tier = "NOMINAL"
    if ratio >= 40:
        tier = "CRITICAL"
    elif ratio >= 30:
        tier = "HIGH"
    elif ratio >= 15:
        tier = "ELEVATED"
    return {"paper_claims_oz": paper, "registered_oz": registered_oz, "ratio": ratio, "tier": tier,
            "status": "fresh", "reason": None, "contract_size_oz": 5000}


# ---------------------------------------------------------------- options
def bs_gamma(S, K, T, r, sigma):
    S, K, T, sigma = (np.asarray(x, dtype=float) for x in (S, K, T, sigma))
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        g = norm.pdf(d1) / (S * sigma * np.sqrt(T))
    return np.where((T > 0) & (sigma > 0), g, 0.0)


def _chain_frame(chains, expirations, today):
    frames = []
    for exp in expirations:
        c = chains.get(exp)
        if not c:
            continue
        T = max((pd.Timestamp(exp) - pd.Timestamp(today)).days / 365.0, GEX_PARAMS["min_T_years"])
        for side, df in (("call", c.get("calls")), ("put", c.get("puts"))):
            if df is None or len(df) == 0:
                continue
            f = pd.DataFrame({"strike": pd.to_numeric(df["strike"], errors="coerce"),
                              "oi": pd.to_numeric(df.get("openInterest"), errors="coerce"),
                              "iv": pd.to_numeric(df.get("impliedVolatility"), errors="coerce")})
            f["side"] = side
            f["T"] = T
            f["expiration"] = exp
            frames.append(f)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def gex_profile(spot, chains, expirations, today):
    """Net GEX by strike over the first N expirations, walls within the shared window, and a defined
    zero-gamma level (spot where aggregate net GEX changes sign, IV held fixed)."""
    p = GEX_PARAMS
    base = {"params": p, "version": GEX_VERSION}
    if spot is None:
        return {**base, "status": "missing", "reason": "no spot price"}
    exps = list(expirations)[:p["expirations"]]
    df = _chain_frame(chains, exps, today)
    if df.empty:
        return {**base, "status": "missing", "reason": "no option chains"}
    total = len(df)
    usable = df[(df["oi"] > 0) & (df["iv"] > p["min_iv"])].copy()
    unknown_oi = int(df["oi"].isna().sum())
    if usable.empty:
        return {**base, "status": "missing", "reason": "no contracts with known OI and IV",
                "contracts_total": total, "contracts_unknown_oi": unknown_oi}
    sign = np.where(usable["side"] == "call", 1.0, -1.0)

    def net_by_strike(S):
        g = bs_gamma(S, usable["strike"].values, usable["T"].values, p["risk_free_rate"], usable["iv"].values)
        return sign * g * usable["oi"].values * p["multiplier"] * S

    contrib = net_by_strike(spot)
    by_strike = pd.Series(contrib).groupby(usable["strike"].values).sum()
    net_total = float(by_strike.sum())
    lo, hi = spot * (1 - p["wall_window_pct"]), spot * (1 + p["wall_window_pct"])
    window = by_strike[(by_strike.index >= lo) & (by_strike.index <= hi)]
    call_wall = float(window.idxmax()) if len(window) else None
    put_wall = float(window.idxmin()) if len(window) else None

    grid = np.linspace(lo, hi, 81)
    totals = np.array([net_by_strike(s).sum() for s in grid])
    zero_gamma, zg_reason = None, None
    crossings = np.where(np.sign(totals[:-1]) != np.sign(totals[1:]))[0]
    if len(crossings):
        # choose the crossing nearest to spot
        best = min(crossings, key=lambda i: abs(grid[i] - spot))
        x0, x1, y0, y1 = grid[best], grid[best + 1], totals[best], totals[best + 1]
        zero_gamma = float(x0 - y0 * (x1 - x0) / (y1 - y0)) if y1 != y0 else float(x0)
    else:
        zg_reason = f"net GEX does not change sign within ±{int(p['wall_window_pct']*100)}% of spot"
    return {**base, "status": "fresh", "reason": None, "spot": spot, "expirations_used": exps,
            "net_gex": net_total, "call_wall": call_wall, "put_wall": put_wall,
            "zero_gamma": zero_gamma, "zero_gamma_reason": zg_reason, "zero_gamma_version": ZERO_GAMMA_VERSION,
            "contracts_total": total, "contracts_used": int(len(usable)), "contracts_unknown_oi": unknown_oi,
            "by_strike_window": {str(k): float(v) for k, v in window.items()},
            "window": [lo, hi]}


def max_pain(chain):
    """Front-expiry max pain over contracts whose OI is known."""
    if not chain:
        return {"strike": None, "status": "missing", "reason": "no front-expiry chain", "version": MAX_PAIN_VERSION}
    calls, puts = chain.get("calls"), chain.get("puts")
    frames = []
    for side, df in (("call", calls), ("put", puts)):
        if df is not None and len(df):
            frames.append(pd.DataFrame({"strike": pd.to_numeric(df["strike"], errors="coerce"),
                                        "oi": pd.to_numeric(df.get("openInterest"), errors="coerce"),
                                        "side": side}))
    if not frames:
        return {"strike": None, "status": "missing", "reason": "empty chain", "version": MAX_PAIN_VERSION}
    df = pd.concat(frames, ignore_index=True)
    total = len(df)
    known = df[df["oi"].notna()]
    if known.empty or known["oi"].sum() <= 0:
        return {"strike": None, "status": "missing", "reason": "open interest unavailable for front expiry",
                "contracts_total": total, "contracts_known_oi": int(len(known)), "version": MAX_PAIN_VERSION}
    strikes = np.sort(known["strike"].unique())
    c = known[known.side == "call"]
    pt = known[known.side == "put"]
    losses = []
    for k in strikes:
        loss = (c["oi"] * np.maximum(0, k - c["strike"])).sum() + (pt["oi"] * np.maximum(0, pt["strike"] - k)).sum()
        losses.append(loss)
    best = float(strikes[int(np.argmin(losses))])
    coverage = len(known) / total
    return {"strike": best, "status": "fresh" if coverage >= 0.8 else "partial",
            "reason": None if coverage >= 0.8 else f"only {coverage:.0%} of contracts have known OI",
            "contracts_total": total, "contracts_known_oi": int(len(known)), "version": MAX_PAIN_VERSION}


def top_contracts(chains):
    """Highest volume / OI call and put across every captured expiration. Unknown values stay unknown
    (they are excluded from ranking, not treated as zero)."""
    out, coverage = {}, {}
    for side in ("calls", "puts"):
        frames = []
        for exp, c in chains.items():
            df = c.get(side)
            if df is not None and len(df):
                f = df.copy()
                f["expiration"] = exp
                frames.append(f)
        if not frames:
            continue
        df = pd.concat(frames, ignore_index=True)
        for field, label in (("volume", "vol"), ("openInterest", "oi")):
            vals = pd.to_numeric(df.get(field), errors="coerce")
            coverage[f"{side}_{field}_known"] = int(vals.notna().sum())
            coverage[f"{side}_total"] = int(len(df))
            if vals.notna().any():
                row = df.loc[vals.idxmax()]
                out[f"top_{label}_{side[:-1]}"] = {
                    "contract": row.get("contractSymbol"), "strike": safe_float(row.get("strike")),
                    "expiration": row.get("expiration"), "volume": safe_float(row.get("volume")),
                    "open_interest": safe_float(row.get("openInterest")), "last_price": safe_float(row.get("lastPrice")),
                    "bid": safe_float(row.get("bid")), "ask": safe_float(row.get("ask")),
                    "iv": safe_float(row.get("impliedVolatility")),
                    "last_trade": str(row.get("lastTradeDate")) if row.get("lastTradeDate") is not None else None,
                }
    return out, coverage


# ---------------------------------------------------------------- block flow
def normalize_side(raw):
    s = str(raw).strip().upper() if raw is not None else "N"
    return DATABENTO_SIDE.get(s, "unknown")


def block_flow(trades, *, min_size=BLOCK_MIN_SIZE, limit=None):
    """Summarize large prints from a Databento trades frame (index = ts_recv/ts_event).

    Aggressor volumes use provider semantics only. A separately named VWAP heuristic classifies
    unknown-side volume and is never mixed into the aggressor totals."""
    base = {"version": FLOW_VERSION, "min_block_size": min_size,
            "venue_evidence": "not verified: DBEQ.BASIC trades do not establish off-exchange provenance",
            "label": "block-flow proxy (legacy 'dark pool' section)"}
    if trades is None:
        return {**base, "status": "missing", "reason": "trade request failed"}
    n = len(trades)
    base.update({"trades_captured": n, "limit": limit, "limit_hit": bool(limit and n >= limit)})
    if n == 0:
        return {**base, "status": "fresh", "reason": "request succeeded; no trades in window",
                "blocks": 0, "block_volume": 0}
    blocks = trades[trades["size"] >= min_size].copy()
    if blocks.empty:
        return {**base, "status": "fresh", "reason": f"no prints >= {min_size:,} shares", "blocks": 0,
                "block_volume": 0}
    blocks["aggressor"] = blocks["side"].map(normalize_side)
    vol = float(blocks["size"].sum())
    notional = float((blocks["price"] * blocks["size"]).sum())
    vwap = notional / vol
    buy = float(blocks.loc[blocks.aggressor == "buy", "size"].sum())
    sell = float(blocks.loc[blocks.aggressor == "sell", "size"].sum())
    unknown = blocks[blocks.aggressor == "unknown"]
    unk_vol = float(unknown["size"].sum())
    heur_above = float(unknown.loc[unknown["price"] >= vwap, "size"].sum())
    heur_below = float(unknown.loc[unknown["price"] < vwap, "size"].sum())

    def classify(up, down):
        if up > down * SENTIMENT_RATIO:
            return "BULLISH"
        if down > up * SENTIMENT_RATIO:
            return "BEARISH"
        return "NEUTRAL"

    known = buy + sell
    share = known / vol
    aggressor_bias = classify(buy, sell) if known else "UNKNOWN"
    # Legacy strategy formula, side-corrected: aggressor volume where the provider supplies it, and the
    # legacy price-vs-VWAP rule only for unknown-side prints. Reported as a heuristic when it dominates.
    heuristic_bias = classify(buy + heur_above, sell + heur_below)
    if share >= 0.5:
        bias, method = aggressor_bias, "aggressor"
        confidence = "high" if share >= 0.8 else "medium"
    else:
        bias, method = heuristic_bias, "vwap_heuristic"
        confidence = "heuristic"
    reason = None if share >= 0.5 else (
        f"{share:.0%} of block volume has a provider aggressor side; bias uses the VWAP heuristic")
    status = "partial" if base["limit_hit"] else "fresh"
    if base["limit_hit"]:
        reason = (reason + "; " if reason else "") + f"request limit {limit:,} reached — window may be truncated"
    recent = []
    for ts, row in blocks.tail(5).iloc[::-1].iterrows():
        recent.append({"time": pd.Timestamp(ts).isoformat(), "price": safe_float(row["price"]),
                       "size": int(row["size"]), "side_raw": str(row["side"]), "aggressor": row["aggressor"]})
    idx = pd.to_datetime(trades.index)
    return {**base, "status": status, "reason": reason, "blocks": int(len(blocks)), "block_volume": vol,
            "block_notional": notional, "block_vwap": vwap, "largest_block": float(blocks["size"].max()),
            "buy_aggressor_volume": buy, "sell_aggressor_volume": sell, "unknown_side_volume": unk_vol,
            "bias": bias, "bias_confidence": confidence, "bias_method": method,
            "aggressor_bias": aggressor_bias, "heuristic_bias": heuristic_bias, "aggressor_known_share": share,
            "bias_rule": "1.2x volume imbalance; aggressor = Databento B (buy) vs A (sell); heuristic adds "
                         "unknown-side prints at/above VWAP to buy, below to sell",
            "vwap_heuristic_unknown_side": {"at_or_above_vwap": heur_above, "below_vwap": heur_below,
                                            "note": "derived heuristic, not aggressor data"},
            "first_trade": idx.min().isoformat(), "last_trade": idx.max().isoformat(), "recent_prints": recent}


def price_nodes(trades, *, min_size=BLOCK_MIN_SIZE, bucket=0.5, top=3):
    if trades is None or len(trades) == 0:
        return []
    b = trades[trades["size"] >= min_size]
    if b.empty:
        return []
    zones = (b["price"] / bucket).round() * bucket
    heat = b.groupby(zones)["size"].sum().sort_values(ascending=False).head(top)
    return [{"price": float(k), "shares": int(v)} for k, v in heat.items()]


# ---------------------------------------------------------------- OI divergence (proxy)
def oi_divergence(inst_series, retail_series):
    """inst/retail: list of {date, open_interest}. Joined by session; both indices share the first common
    date with valid OI as the baseline (=100)."""
    a = {r["date"]: r["open_interest"] for r in inst_series if r.get("open_interest")}
    b = {r["date"]: r["open_interest"] for r in retail_series if r.get("open_interest")}
    common = sorted(set(a) & set(b))
    if not common:
        return {"status": "missing", "reason": "no common sessions with OI for both products", "series": [],
                "version": DIVERGENCE_VERSION}
    base = common[0]
    series = [{"date": d, "institutions": a[d] / a[base] * 100, "retail": b[d] / b[base] * 100} for d in common]
    dropped = sorted((set(a) | set(b)) - set(common))
    return {"status": "fresh", "reason": None, "baseline_date": base, "series": series,
            "sessions_dropped": dropped, "version": DIVERGENCE_VERSION,
            "note": "standard vs micro contract OI is a size-based proxy, not identified investor ownership"}


# ---------------------------------------------------------------- technicals / weather / breadth
def _complete_bars(df):
    """Drop rows without a close (Yahoo emits today's unfinished bar with NaN close)."""
    return None if df is None else df.dropna(subset=["Close"])


def technicals(df_1h, df_daily, df_weekly):
    df_1h, df_daily, df_weekly = _complete_bars(df_1h), _complete_bars(df_daily), _complete_bars(df_weekly)
    if df_daily is None or df_daily.empty or df_1h is None or df_1h.empty:
        return {"status": "missing", "reason": "no hourly/daily history"}
    px = float(df_daily["Close"].iloc[-1])
    ema1h = float(df_1h["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
    emad = float(df_daily["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
    smaw = safe_float(df_weekly["Close"].rolling(20).mean().iloc[-1]) if df_weekly is not None and len(df_weekly) else None
    t1 = "BULLISH" if px > ema1h else "BEARISH"
    td = "BULLISH" if px > emad else "BEARISH"
    tw = None if smaw is None else ("BULLISH" if px > smaw else "BEARISH")
    if tw is None:
        align = "UNKNOWN (weekly history missing)"
    elif t1 == "BULLISH" and td == "BEARISH" and tw == "BEARISH":
        align = "BEAR MARKET RALLY (Short Squeeze Trap)"
    elif t1 == "BEARISH" and td == "BULLISH" and tw == "BULLISH":
        align = "BULL MARKET DIP (Buying Opportunity)"
    elif t1 == td == tw:
        align = f"FULL ALIGNMENT ({t1})"
    else:
        align = "MIXED / TRANSITION"
    sma50 = safe_float(df_daily["Close"].rolling(50).mean().iloc[-1])
    sma200 = safe_float(df_daily["Close"].rolling(200).mean().iloc[-1])
    zone = (df_daily["Close"] * 2).round() / 2
    tail = df_daily.tail(30)
    poc = float(tail.groupby(zone.loc[tail.index])["Volume"].sum().idxmax())
    delta = df_daily["Close"].diff()
    gain = delta.where(delta > 0, 0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = safe_float(100 - 100 / (1 + gain.iloc[-1] / loss.iloc[-1])) if loss.iloc[-1] else 100.0
    return {"status": "fresh", "reason": None, "current_price": px, "ema20_1h": ema1h, "ema20_daily": emad,
            "sma20_weekly": smaw, "sma50": sma50, "sma200": sma200, "poc_30d": poc,
            "poc_method": "30 daily bars, close rounded to $0.50, volume-weighted mode", "rsi14": rsi,
            "trend_1h": t1, "trend_daily": td, "trend_weekly": tw, "alignment": align,
            "vs_200_sma": None if sma200 is None else ("BULLISH" if px > sma200 else "BEARISH"),
            "last_bar": str(df_daily.index[-1])}


def weather(vix, vix3m):
    if vix is None or vix3m is None:
        return {"status": "missing", "reason": "VIX or VIX3M unavailable", "vix": vix, "vix3m": vix3m}
    d = vix - vix3m
    ts = (f"BACKWARDATION (VIX is {abs(d):.2f} pts HIGHER than 3M. Extreme Near-Term Panic / Selloff)" if vix > vix3m
          else f"CONTANGO (VIX is {abs(d):.2f} pts LOWER than 3M. Normal / Complacent Market)")
    return {"status": "fresh", "reason": None, "vix": vix, "vix3m": vix3m, "spread": d, "term_structure": ts}


def breadth(pct):
    need = ["SPY", "RSP"]
    if pct is None or any(pct.get(k) is None for k in need):
        return {"status": "missing", "reason": "SPY/RSP daily change unavailable"}
    s, r = pct["SPY"], pct["RSP"]
    if s > 0 and r > 0:
        cond = "BROAD RALLY (Healthy Participation)"
    elif s > 0 and r <= 0:
        cond = "NARROW RALLY (Top-Heavy / Fakeout)"
    elif s < 0 and r < 0:
        cond = "BROAD SELLOFF (True Market Weakness)"
    elif s < 0 and r >= 0:
        cond = "TECH ROTATION (Mega-caps bleeding, broader market holding)"
    else:
        cond = "FLAT / MIXED"
    return {"status": "fresh", "reason": None, "condition": cond, "pct": pct}


# ---------------------------------------------------------------- execution engine
def execution_engine(*, vix, breadth_cond, flow_bias, alignment, price, call_wall, put_wall, max_pain_strike,
                     nodes, flow_method=None):
    """Legacy confluence engine with identical weights. Each vote records its inputs; a vote whose inputs
    are missing is recorded as missing (not zero) and the verdict is flagged incomplete."""
    def finite(x):
        return x if (x is not None and isinstance(x, (int, float)) and math.isfinite(x)) else None
    vix, price, call_wall, put_wall, max_pain_strike = map(finite, (vix, price, call_wall, put_wall, max_pain_strike))
    nodes = [n for n in nodes if finite(n) is not None]
    votes, missing = [], []
    if vix is None:
        regime = None
        missing.append("vix")
        w_breadth = w_tech = w_gamma = w_pain = None
    elif vix > 20:
        regime = "HIGH VOLATILITY (Structural Dominance)"
        w_breadth, w_tech, w_gamma, w_pain = 1, 0, 2, 2
    else:
        regime = "LOW/NORMAL VOLATILITY (Trend Dominance)"
        w_breadth, w_tech, w_gamma, w_pain = 2, 1, 1, 1

    def vote(name, points, why, inputs):
        votes.append({"vote": name, "points": points, "why": why, "inputs": inputs})

    if regime is None:
        result = {"status": "missing", "reason": "VIX unavailable: regime weights undefined", "votes": [],
                  "total_score": None, "directional_bias": "UNAVAILABLE", "regime": None, "version": EXECUTION_VERSION}
        return result

    if breadth_cond is None:
        vote("breadth", None, "missing input", {"breadth": None}); missing.append("breadth")
    elif "BROAD RALLY" in breadth_cond:
        vote("breadth", w_breadth, "Broad Participation", {"breadth": breadth_cond})
    elif "NARROW RALLY" in breadth_cond or "BROAD SELLOFF" in breadth_cond:
        vote("breadth", -w_breadth, "Narrow/Weak Participation", {"breadth": breadth_cond})
    else:
        vote("breadth", 0, "Rotation / flat", {"breadth": breadth_cond})

    if flow_bias in (None, "UNKNOWN"):
        vote("block_flow", None, "missing input", {"bias": flow_bias}); missing.append("block_flow")
    elif flow_bias == "BULLISH":
        vote("block_flow", w_gamma, f"Block imbalance to buy side ({flow_method})", {"bias": flow_bias, "method": flow_method})
    elif flow_bias == "BEARISH":
        vote("block_flow", -w_gamma, f"Block imbalance to sell side ({flow_method})", {"bias": flow_bias, "method": flow_method})
    else:
        vote("block_flow", 0, f"Neutral Imbalance ({flow_method})", {"bias": flow_bias, "method": flow_method})

    if w_tech == 0:
        vote("trend", 0, "Muted due to High VIX", {"alignment": alignment})
    elif alignment is None or alignment.startswith("UNKNOWN"):
        vote("trend", None, "missing input", {"alignment": alignment}); missing.append("trend")
    elif "FULL ALIGNMENT (BULLISH)" in alignment:
        vote("trend", w_tech + 1, "Full MTF Bullish Alignment", {"alignment": alignment})
    elif "FULL ALIGNMENT (BEARISH)" in alignment:
        vote("trend", -(w_tech + 1), "Full MTF Bearish Alignment", {"alignment": alignment})
    elif "BEAR MARKET RALLY" in alignment:
        vote("trend", -1, "Trap - Bullish 1H in Bearish Macro", {"alignment": alignment})
    elif "BULL MARKET DIP" in alignment:
        vote("trend", 1, "Dip - Bearish 1H in Bullish Macro", {"alignment": alignment})
    else:
        vote("trend", 0, "Mixed/Non-Aligned", {"alignment": alignment})

    if price is None or call_wall is None or put_wall is None:
        vote("gamma", None, "missing input", {"price": price, "call_wall": call_wall, "put_wall": put_wall})
        missing.append("gamma")
    elif price > call_wall:
        vote("gamma", -w_gamma, "Overextended/MM Selling", {"price": price, "call_wall": call_wall})
    elif price < put_wall:
        vote("gamma", w_gamma, "Bounce Zone/MM Buying", {"price": price, "put_wall": put_wall})
    else:
        vote("gamma", 0, "Inside walls", {"price": price, "call_wall": call_wall, "put_wall": put_wall})

    if price is None or max_pain_strike is None:
        vote("max_pain", None, "missing input", {"price": price, "max_pain": max_pain_strike}); missing.append("max_pain")
    elif price > max_pain_strike + 2:
        vote("max_pain", -w_pain, "Downward Gravity", {"price": price, "max_pain": max_pain_strike})
    elif price < max_pain_strike - 2:
        vote("max_pain", w_pain, "Upward Gravity", {"price": price, "max_pain": max_pain_strike})
    else:
        vote("max_pain", 0, "Near max pain", {"price": price, "max_pain": max_pain_strike})

    score = sum(v["points"] for v in votes if v["points"] is not None)
    if score >= 3:
        bias = f"BULLISH (+{score}) [CALL TRIGGER ACTIVE]"
    elif score <= -3:
        bias = f"BEARISH ({score}) [PUT TRIGGER ACTIVE]"
    else:
        bias = f"CASH POSITION ({score}) [CONFLICTING SIGNALS - HOLD]"
    if missing:
        bias += f" [INCOMPLETE: {', '.join(missing)} missing]"

    target, rationale = None, "Cash Position - No strike required."
    if price is not None and score >= 3:
        mags = [n for n in nodes if n > price] + ([call_wall] if call_wall and call_wall > price else [])
        target, rationale = (min(mags), "Snapped to nearest overhead block-flow node or Call Wall.") if mags else \
            (round(price + 2, 0), "Defaulted to +$2 out-of-the-money (No clear overhead nodes).")
    elif price is not None and score <= -3:
        mags = [n for n in nodes if n < price] + ([max_pain_strike] if max_pain_strike and max_pain_strike < price else [])
        target, rationale = (max(mags), "Snapped to nearest underlying block-flow node or Max Pain.") if mags else \
            (round(price - 2, 0), "Defaulted to -$2 out-of-the-money (No clear support nodes).")

    dte, exp_rationale = None, "Cash Position - No expiration required."
    if abs(score) >= 3:
        if vix < 15:
            dte, exp_rationale = (14, 21), f"VIX is Low ({vix:.2f}). 14-21 DTE selected (Options are cheap)."
        elif vix <= 20:
            dte, exp_rationale = (21, 30), f"VIX is Normal ({vix:.2f}). 21-30 DTE selected."
        else:
            dte, exp_rationale = (30, 45), f"VIX is High ({vix:.2f}). 30-45 DTE selected to buffer IV Crush & Theta."
    abs_score = abs(score)
    risk = ("Conviction: LOW. Suggested Size: 1/3rd standard position." if abs_score == 3 else
            "Conviction: HIGH. Suggested Size: Standard swing position." if abs_score == 4 else
            "Conviction: EXTREME. Suggested Size: Max allocation / Add on dips." if abs_score >= 5 else None)
    return {"status": "partial" if missing else "fresh", "reason": f"missing votes: {', '.join(missing)}" if missing else None,
            "regime": regime, "weights": {"breadth": w_breadth, "trend": w_tech, "gamma": w_gamma, "max_pain": w_pain},
            "votes": votes, "total_score": score, "directional_bias": bias, "target_strike": target,
            "strike_rationale": rationale, "dte_window": dte, "expiration_rationale": exp_rationale,
            "allocation": risk, "version": EXECUTION_VERSION}


# ---------------------------------------------------------------- formatting helpers (display only)
def fmt_moz(oz):
    """Troy ounces -> 'xx.xxM oz' (the DB keeps unscaled ounces)."""
    return "unavailable" if oz is None else f"{oz / 1e6:,.2f}M oz"


def fmt_oz_change(oz):
    if oz is None:
        return "unavailable"
    return f"{oz / 1e6:+.2f}M oz" if abs(oz) >= 1e6 else f"{oz:+,.0f} oz"


# ---------------------------------------------------------------- extended features (tactical XML v2 sections)
FEATURES_VERSION = "features_v1"


def price_stats(df):
    """Trend / volatility features from a daily OHLCV frame (provider bars; no fills)."""
    df = _complete_bars(df)
    if df is None or len(df) < 2:
        return {"status": "missing", "reason": "insufficient daily history"}
    c = df["Close"].dropna()
    last = float(c.iloc[-1])

    def ret(n):
        return float(c.iloc[-1] / c.iloc[-1 - n] - 1) if len(c) > n else None
    lr = (c / c.shift(1)).apply(lambda x: math.log(x) if x and x > 0 else float("nan")).dropna()

    def rv(n):
        return float(lr.tail(n).std() * math.sqrt(252)) if len(lr) >= n else None
    hi = df["High"].dropna() if "High" in df else c
    lo = df["Low"].dropna() if "Low" in df else c
    tr = pd.concat([hi - lo, (hi - c.shift(1)).abs(), (lo - c.shift(1)).abs()], axis=1).max(axis=1)
    atr14 = safe_float(tr.tail(14).mean()) if len(tr) >= 14 else None
    w52 = c.tail(252)
    sma = {n: safe_float(c.rolling(n).mean().iloc[-1]) if len(c) >= n else None for n in (20, 50, 200)}
    sd20 = safe_float(c.tail(20).std()) if len(c) >= 20 else None
    delta = c.diff()
    gain = delta.where(delta > 0, 0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = safe_float(100 - 100 / (1 + gain.iloc[-1] / loss.iloc[-1])) if loss.iloc[-1] else 100.0
    vol = df["Volume"].dropna() if "Volume" in df else None
    rel_vol = safe_float(vol.iloc[-1] / vol.tail(20).mean()) if vol is not None and len(vol) >= 20 and vol.tail(20).mean() else None
    return {"status": "fresh", "reason": None, "last": last, "last_bar": str(c.index[-1])[:10], "bars": int(len(c)),
            "ret_1d": ret(1), "ret_5d": ret(5), "ret_20d": ret(20), "ret_60d": ret(60), "ret_252d": ret(252),
            "rv_10d": rv(10), "rv_20d": rv(20), "rv_60d": rv(60), "atr_14": atr14,
            "atr_14_pct": atr14 / last if atr14 else None, "rsi_14": rsi,
            "sma_20": sma[20], "sma_50": sma[50], "sma_200": sma[200],
            "dist_sma_50": last / sma[50] - 1 if sma[50] else None, "dist_sma_200": last / sma[200] - 1 if sma[200] else None,
            "zscore_20d": (last - sma[20]) / sd20 if sma[20] and sd20 else None,
            "high_52w": float(w52.max()), "low_52w": float(w52.min()),
            "pct_from_52w_high": last / float(w52.max()) - 1, "pct_from_52w_low": last / float(w52.min()) - 1,
            "rel_volume_20d": rel_vol, "version": FEATURES_VERSION}


def options_summary(chains, spot, today):
    """Chain-wide positioning: put/call volume & OI (known values only), ATM IV for the first expiry >= 7 DTE and
    the first >= 25 DTE (term structure), and 90%-moneyness put skew. IV is a fraction."""
    if not chains or spot is None:
        return {"status": "missing", "reason": "no chains or spot"}
    tot = {"call_volume": 0.0, "put_volume": 0.0, "call_oi": 0.0, "put_oi": 0.0}
    oi_by_exp = {}
    for exp, c in chains.items():
        for side, df in (("call", c.get("calls")), ("put", c.get("puts"))):
            if df is None or not len(df):
                continue
            v = pd.to_numeric(df.get("volume"), errors="coerce")
            o = pd.to_numeric(df.get("openInterest"), errors="coerce")
            tot[f"{side}_volume"] += float(v.sum(skipna=True))
            tot[f"{side}_oi"] += float(o.sum(skipna=True))
            oi_by_exp[exp] = oi_by_exp.get(exp, 0.0) + float(o.sum(skipna=True))

    def atm_iv(min_dte):
        for exp in sorted(chains):
            dte = (pd.Timestamp(exp) - pd.Timestamp(today)).days
            if dte < min_dte:
                continue
            ivs = []
            for side in ("calls", "puts"):
                df = chains[exp].get(side)
                if df is None or not len(df):
                    continue
                k = pd.to_numeric(df["strike"], errors="coerce")
                row = df.loc[(k - spot).abs().idxmin()]
                iv = safe_float(row.get("impliedVolatility"))
                if iv and iv > 0.01:
                    ivs.append(iv)
            if ivs:
                puts = chains[exp].get("puts")
                skew = None
                if puts is not None and len(puts):
                    k = pd.to_numeric(puts["strike"], errors="coerce")
                    r90 = puts.loc[(k - spot * 0.9).abs().idxmin()]
                    iv90 = safe_float(r90.get("impliedVolatility"))
                    skew = iv90 - sum(ivs) / len(ivs) if iv90 and iv90 > 0.01 else None
                return {"expiration": exp, "dte": dte, "atm_iv": sum(ivs) / len(ivs), "put_skew_90": skew}
        return None
    near, month = atm_iv(7), atm_iv(25)
    pc_vol = tot["put_volume"] / tot["call_volume"] if tot["call_volume"] else None
    pc_oi = tot["put_oi"] / tot["call_oi"] if tot["call_oi"] else None
    top_exp = sorted(oi_by_exp.items(), key=lambda kv: -kv[1])[:3]
    return {"status": "fresh", "reason": None, **tot, "put_call_volume_ratio": pc_vol, "put_call_oi_ratio": pc_oi,
            "atm_iv_near": near, "atm_iv_30d": month,
            "iv_term_slope": (month["atm_iv"] - near["atm_iv"]) if near and month else None,
            "oi_concentration_expirations": [{"expiration": e, "open_interest": v} for e, v in top_exp],
            "version": FEATURES_VERSION, "note": "totals exclude contracts with unknown volume/OI"}


def cme_positioning(series):
    """Per-product changes over sessions from the report-dated CME series."""
    if not series:
        return {"status": "missing", "reason": "no CME history"}
    last = series[-1]

    def chg(n, field):
        if len(series) > n and series[-1 - n].get(field) and last.get(field) is not None:
            return last[field] - series[-1 - n][field]
        return None
    vols = [r["volume"] for r in series[-20:] if r.get("volume") is not None]
    avg = sum(vols) / len(vols) if vols else None
    oi = last.get("open_interest")
    return {"status": "fresh", "trade_date": last["date"], "volume": last.get("volume"), "open_interest": oi,
            "oi_change_1d": last.get("oi_change"), "oi_change_5d": chg(5, "open_interest"),
            "oi_change_20d": chg(20, "open_interest"),
            "oi_change_5d_pct": chg(5, "open_interest") / series[-6]["open_interest"]
            if chg(5, "open_interest") is not None and series[-6].get("open_interest") else None,
            "volume_vs_20d_avg": last["volume"] / avg if avg and last.get("volume") is not None else None,
            "sessions": len(series)}


def percentile_context(values, latest):
    """Where the latest value sits in the available history (percentile rank, z-score)."""
    vals = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if latest is None or len(vals) < 20:
        return None
    s = pd.Series(vals)
    return {"latest": latest, "percentile": float((s < latest).mean() * 100),
            "zscore": float((latest - s.mean()) / s.std()) if s.std() else None,
            "min": float(s.min()), "max": float(s.max()), "mean": float(s.mean()), "observations": len(vals)}
