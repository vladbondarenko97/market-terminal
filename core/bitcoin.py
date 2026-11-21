"""Bitcoin tab (B1 verdict, B2 price and cycle, B3 on-chain, B4 leverage and options, B5 flows).

The pipeline collects here (`collect`, every request through SourceSession.fetch()) and stores one `bitcoin` block in the
snapshot; the terminal only reads it. The maths (`price_cycle`, `onchain`, `funding`, `flows` ...) are pure functions of
rows, so the tests run them on fixtures. A row without its inputs is `{"status": "missing", "reason": ...}`, never zero.

The verdict (B1) is conditions, not a price call. Every rule row is untested until phase 4 grades it on history (GRADES is
empty), and only graded rows vote, so today the verdict is NO CALL or HOLD and says so.
"""
import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from core import lake
from core import sources as src

BITCOIN_VERSION = "bitcoin_v1"      # v1: MVRV Z = (market cap - realized cap) / expanding stdev of market cap; realized cap = market cap / MVRV
CM_START = "2009-01-03"             # the genesis block; Coin Metrics' daily history starts here
MA_WEEKS = 200
MA_DAYS = MA_WEEKS * 7              # 200-week MA = mean of the last 1,400 daily closes
HALVINGS = ("2012-11-28", "2016-07-09", "2020-05-11", "2024-04-20")
HALVING_SPACING_DAYS = 1460         # ~210,000 blocks; the next date is an estimate (block 1,050,000)
IBIT_OBS = "bitcoin.ibit_shares"
COT_CONTRACT = ("gpe5-46if", "133741")      # CFTC Traders in Financial Futures, CME Bitcoin (the Micro contract is 133742)
GRADES = {}      # row id -> {"hit_rate", "n"}; phase 4 fills it from Edge Lab runs. Until then no row votes.
NOT_GRADED = "not graded on history (phase 4)"


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v or v in (float("inf"), float("-inf")) else v


def missing(reason):
    return {"status": "missing", "reason": reason}


# ---------------------------------------------------------------- B2 price and cycle
def halving_clock(today):
    last = date.fromisoformat(HALVINGS[-1])
    nxt = last + timedelta(days=HALVING_SPACING_DAYS)
    return {"last": last.isoformat(), "days_since": (today - last).days, "next_estimate": nxt.isoformat(), "days_to_next": (nxt - today).days,
            "note": "next date is an estimate: 1,460 days after the last halving (block 1,050,000)"}


def price_cycle(closes, today, source):
    """closes: date-indexed daily Close Series. Price, drawdown from the all-time high, 200-week MA, Mayer multiple, halving clock."""
    closes = closes.dropna() if closes is not None else None
    if closes is None or not len(closes):
        return missing("no BTC-USD daily bars")
    px, ath = float(closes.iloc[-1]), float(closes.max())
    out = {"status": "fresh", "source": source, "as_of": closes.index[-1].date().isoformat(), "price": px, "ath": ath,
           "ath_date": closes.idxmax().date().isoformat(), "drawdown_pct": (px / ath - 1) * 100, "halving": halving_clock(today),
           "ma_200w": None, "ma_200d": None, "mayer": None, "price_vs_ma_200w_pct": None, "missing": {}}
    if len(closes) >= MA_DAYS:
        out["ma_200w"] = float(closes.iloc[-MA_DAYS:].mean())
        out["price_vs_ma_200w_pct"] = (px / out["ma_200w"] - 1) * 100
    else:
        out["missing"]["ma_200w"] = f"{len(closes)} daily closes stored; the 200-week MA needs {MA_DAYS}"
    if len(closes) >= 200:
        out["ma_200d"] = float(closes.iloc[-200:].mean())
        out["mayer"] = px / out["ma_200d"]
    else:
        out["missing"]["mayer"] = f"{len(closes)} daily closes stored; the 200-day MA needs 200"
    if len(closes) > 30:
        out["return_30d_pct"] = (px / float(closes.iloc[-31]) - 1) * 100
    return out


# ---------------------------------------------------------------- B3 on-chain
def cm_frame(rows):
    """Coin Metrics rows -> date-indexed float frame (price, mcap, mvrv, supply, hash)."""
    df = pd.DataFrame([{"date": r["time"][:10], "price": _f(r.get("PriceUSD")), "mcap": _f(r.get("CapMrktCurUSD")), "mvrv": _f(r.get("CapMVRVCur")),
                        "supply": _f(r.get("SplyCur")), "hash": _f(r.get("HashRate"))} for r in rows])
    df.index = pd.to_datetime(df.pop("date"))
    return df[~df.index.duplicated(keep="last")].sort_index()


def onchain_series(df):
    """(realized cap, realized price, MVRV Z) as Series. Realized cap = market cap / MVRV (CapRealUSD itself needs a paid key); realized price =
    realized cap / supply; Z = (market cap - realized cap) / stdev of market cap over the history up to that day (expanding, no look-ahead)."""
    ok = df["mvrv"] > 0
    rcap = (df["mcap"] / df["mvrv"]).where(ok)
    rprice = rcap / df["supply"]
    z = (df["mcap"] - rcap) / df["mcap"].expanding(min_periods=365).std()
    return rcap, rprice, z


def hash_ribbons(hashrate, today_days=30):
    """30-day vs 60-day mean of the hash rate. capitulation: 30d under 60d; recovery: 30d crossed back above 60d in the last `today_days`
    days; expansion: above for longer."""
    h = hashrate.dropna()
    if len(h) < 60 + today_days:
        return missing(f"{len(h)} hash-rate days; ribbons need {60 + today_days}")
    a, b = h.rolling(30).mean(), h.rolling(60).mean()
    above = (a >= b).iloc[-(today_days + 1):]
    state = "capitulation" if not above.iloc[-1] else "recovery" if not above.all() else "expansion"
    return {"status": "fresh", "state": state, "hash_30d_eh": float(a.iloc[-1]) / 1e6, "hash_60d_eh": float(b.iloc[-1]) / 1e6,
            "recovery_window_days": today_days, "units": "EH/s (Coin Metrics HashRate is TH/s)"}


def onchain(df, today):
    if df is None or not len(df):
        return missing("no Coin Metrics rows")
    rcap, rprice, z = onchain_series(df)
    last = df.index[-1]
    zz = _f(z.iloc[-1])
    out = {"status": "fresh" if (today - last.date()).days <= 3 else "stale", "source": "coinmetrics.io community API v4", "as_of": last.date().isoformat(),
           "market_cap": _f(df["mcap"].iloc[-1]), "mvrv": _f(df["mvrv"].iloc[-1]), "realized_cap": _f(rcap.iloc[-1]), "realized_price": _f(rprice.iloc[-1]),
           "supply": _f(df["supply"].iloc[-1]), "mvrv_z": zz, "hash_ribbons": hash_ribbons(df["hash"]), "days": len(df),
           "formula": "MVRV Z = (market cap - realized cap) / stdev(market cap over all days to date); realized cap = market cap / MVRV",
           "unavailable": {"sopr": "no free keyless source", "sth_cost_basis": "no free keyless source", "exchange_balances": "no free keyless source"}}
    if zz is None:
        out["missing"] = {"mvrv_z": "needs 365 days of market cap and a positive MVRV"}
    if out["status"] == "stale":
        out["reason"] = f"newest Coin Metrics row is {last.date()}"
    return out


def history(closes, df, today):
    """Weekly points (every 7th day back from the newest, plus it) for the charts: price, 200-week MA, realized price, MVRV Z."""
    idx = df.index if df is not None and len(df) else closes.index
    cl = closes.reindex(idx) if closes is not None and len(closes) else pd.Series(dtype=float)
    out = {"t": [], "price": [], "ma_200w": [], "realized_price": [], "mvrv_z": []}
    if df is None or not len(df):
        return out
    _, rprice, z = onchain_series(df)
    ma = (closes.rolling(MA_DAYS).mean() if closes is not None and len(closes) else pd.Series(dtype=float)).reindex(idx)
    pick = idx[::-1][::7][::-1]
    for d in pick:
        out["t"].append((d.date() - date(1970, 1, 1)).days)
        out["price"].append(_f(cl.get(d)) if len(cl) else None)
        out["ma_200w"].append(_f(ma.get(d)) if len(ma) else None)
        out["realized_price"].append(_f(rprice.get(d)))
        out["mvrv_z"].append(_f(z.get(d)))
    return out


# ---------------------------------------------------------------- B4 leverage and options
def funding(ticker, hist, today):
    """Perpetual funding. ticker: Deribit ticker dict; hist: hourly rows {timestamp ms, interest_8h}. The rate is per 8 hours."""
    if not ticker or ticker.get("funding_8h") is None:
        return missing("no Deribit ticker")
    out = {"status": "fresh", "source": "deribit.com public API", "funding_8h": _f(ticker["funding_8h"]),
           "annualized_pct": _f(ticker["funding_8h"]) * 3 * 365 * 100 if _f(ticker["funding_8h"]) is not None else None,
           "open_interest_usd": _f(ticker.get("open_interest")), "mark_price": _f(ticker.get("mark_price")),
           "liquidations": "missing: no free keyless liquidation feed (Binance and Bybit refuse US addresses)"}
    daily = {}
    for r in hist or []:
        d = datetime.fromtimestamp(r["timestamp"] / 1000, timezone.utc).date()
        if d < today and _f(r.get("interest_8h")) is not None:
            daily.setdefault(d, []).append(r["interest_8h"])
    means = pd.Series({d: float(np.mean(v)) for d, v in sorted(daily.items())})
    if len(means) >= 7:
        p90 = float(means.quantile(0.9))
        out.update({"avg_7d": float(means.iloc[-7:].mean()), "days": len(means), "p90": p90, "last_days": [[d.isoformat(), float(v)] for d, v in means.iloc[-7:].items()],
                    "negative_days_running": int(next((i for i, v in enumerate(means.iloc[::-1]) if v >= 0), len(means))),
                    "high_days_running": int(next((i for i, v in enumerate(means.iloc[::-1]) if v <= p90), len(means)))})
    else:
        out["missing"] = {"history": f"{len(means)} complete funding days fetched; the rows need 7"}
    return out


def dvol(rows):
    """DVOL daily candles [ts, o, h, l, c]: latest close, 30 days earlier, percentile of the latest in the rows' span."""
    closes = [(datetime.fromtimestamp(r[0] / 1000, timezone.utc).date(), _f(r[4])) for r in rows or [] if _f(r[4]) is not None]
    if not closes:
        return missing("no DVOL candles")
    vals = [v for _, v in closes]
    d, v = closes[-1]
    ago = next((x for dd, x in reversed(closes) if dd <= d - timedelta(days=30)), None)
    return {"status": "fresh", "source": "deribit.com public API", "as_of": d.isoformat(), "dvol": v, "dvol_30d_ago": ago,
            "percentile": sum(x <= v for x in vals) / len(vals) * 100, "days": len(vals),
            "skew_25d": "missing: not built (needs the option chain; not collected)"}


# ---------------------------------------------------------------- B5 flows
def ibit_flows(obs):
    """obs: [{date, shares, nav}] oldest first (one stored per as-of date). Daily net flow (USD) = change in shares x the later day's NAV.
    A day counts only when the previous stored day is under 5 calendar days earlier (a weekend, not a gap)."""
    if not obs:
        return missing("no IBIT shares-outstanding observation stored yet")
    last = obs[-1]
    out = {"status": "fresh", "source": "ishares.com (IBIT product page)", "as_of": last["date"], "shares_outstanding": last["shares"], "nav": last["nav"],
           "stored_days": len(obs), "days": []}
    for a, b in zip(obs, obs[1:]):
        if (date.fromisoformat(b["date"]) - date.fromisoformat(a["date"])).days < 5:
            out["days"].append({"date": b["date"], "net_flow_usd": (b["shares"] - a["shares"]) * b["nav"]})
    out["days"] = out["days"][-30:]
    run = lambda sign: next((i for i, d in enumerate(out["days"][::-1]) if d["net_flow_usd"] * sign <= 0), len(out["days"]))
    out["positive_days_running"], out["negative_days_running"] = run(1), run(-1)
    out["sum_5d_usd"] = sum(d["net_flow_usd"] for d in out["days"][-5:]) if len(out["days"]) >= 5 else None
    if len(out["days"]) < 5:
        out["missing"] = {"streak": f"{len(out['days'])} flow days from {len(obs)} stored days; a 5-day streak needs 5 (one stored per run, so it builds up from now)"}
    return out


def premium(cb, kr):
    if not cb or not kr or not cb.get("price") or not kr.get("price"):
        return missing("Coinbase or Kraken price not available")
    return {"status": "fresh", "source": "Coinbase Exchange BTC-USD vs Kraken BTC-USDT", "coinbase": cb["price"], "kraken_usdt": kr["price"],
            "premium_pct": (cb["price"] / kr["price"] - 1) * 100}


def stablecoins(usdt, usdc):
    """USDT + USDC supply on the newest date both have, and its change over 30 days."""
    if not usdt or not usdc:
        return missing("USDT or USDC supply not available")
    a, b = {r["date"]: r["usd"] for r in usdt}, {r["date"]: r["usd"] for r in usdc}
    days = sorted(set(a) & set(b))
    if not days:
        return missing("USDT and USDC have no common date")
    d = days[-1]
    prior = [x for x in days if x <= (date.fromisoformat(d) - timedelta(days=30)).isoformat()]
    out = {"status": "fresh", "source": "DefiLlama stablecoins API", "as_of": d, "usdt_usdc_usd": a[d] + b[d]}
    if prior:
        p = a[prior[-1]] + b[prior[-1]]
        out.update({"change_30d_usd": out["usdt_usdc_usd"] - p, "change_30d_pct": (out["usdt_usdc_usd"] / p - 1) * 100, "since": prior[-1]})
    else:
        out["missing"] = {"change_30d": "the captured history is shorter than 30 days"}
    return out


def cot(rows):
    """CFTC TFF rows (oldest first) of CME Bitcoin futures: asset managers and leveraged funds, net and weekly change."""
    if not rows:
        return missing("no CFTC rows")
    n = lambda r, k: _f(r.get(k))

    def net(r, who):
        lo, sh = n(r, f"{who}_positions_long"), n(r, f"{who}_positions_short")
        return None if lo is None or sh is None else lo - sh
    r, p = rows[-1], rows[-2] if len(rows) > 1 else None
    out = {"status": "fresh", "source": "CFTC TFF futures-only, CME Bitcoin (133741)", "as_of": r["report_date_as_yyyy_mm_dd"][:10], "open_interest": n(r, "open_interest_all")}
    for who, key in (("asset_mgr", "asset_managers"), ("lev_money", "leveraged_funds")):
        now = net(r, who)
        out[key] = {"net": now, "change_1w": None if now is None or p is None or net(p, who) is None else now - net(p, who)}
    return out


# ---------------------------------------------------------------- B1 rows and verdict
def _row(rid, side, name, trigger, status, reading=None, reason=None):
    g = GRADES.get(rid)
    return {"id": rid, "side": side, "name": name, "trigger": trigger, "status": status, "reading": reading, "reason": reason,
            "graded": bool(g), "evidence": f"{g['hit_rate']:.0%} hit, n={g['n']}" if g else NOT_GRADED}


def _need(rid, side, name, trigger, why):
    return _row(rid, side, name, trigger, "missing", reason=why)


def rows_of(blk, chaos=None, vix_ratio=None):
    """The 11 checklist rows (6 bottom, 5 top) from the block's parts. A part that is missing makes its rows missing with its reason."""
    pc, oc, lv, fl = blk.get("price") or {}, blk.get("onchain") or {}, blk.get("leverage") or {}, blk.get("flows") or {}
    fu, ib = lv.get("funding") or {}, fl.get("ibit") or {}
    rows = []

    def why(part, field=None):
        sub = (part.get("missing") or {}).get(field) if field else None
        return sub or part.get("reason") or "not available"

    def fire(rid, side, name, trigger, ok, reading, cond):
        rows.append(_row(rid, side, name, trigger, "fired" if cond else "waiting", reading) if ok else _need(rid, side, name, trigger, reading))

    px, rp, ma = pc.get("price"), oc.get("realized_price"), pc.get("ma_200w")
    ok = None not in (px, rp, ma)
    fire("b1", "bottom", "Price at or under realized price, or within 10% of the 200-week MA", "price <= realized price or price <= 1.10 x 200W MA", ok,
         f"price {px:,.0f} · realized {rp:,.0f} · 200W MA {ma:,.0f}" if ok else why(oc if rp is None else pc, "ma_200w" if rp is not None else None),
         ok and (px <= rp or px <= ma * 1.10))
    z = oc.get("mvrv_z")
    fire("b2", "bottom", "MVRV Z-score at or under 0.5", "MVRV Z <= 0.5", z is not None, f"MVRV Z {z:+.2f}" if z is not None else why(oc, "mvrv_z"), z is not None and z <= 0.5)
    nd = fu.get("negative_days_running")
    fire("b3", "bottom", "Funding negative 3+ days", "daily funding < 0 for 3 days (the liquidation-spike leg is not measured: no free feed)",
         nd is not None, f"{nd} negative days running" if nd is not None else why(fu, "history"), nd is not None and nd >= 3)
    pr = ib.get("positive_days_running")
    ok = pr is not None and not (ib.get("missing") or {}).get("streak")
    fire("b4", "bottom", "Spot ETF net flows positive 5 days running", "IBIT net flow > 0 for 5 days (IBIT only so far)", ok,
         f"{pr} positive days running" if ok else why(ib, "streak"), ok and pr >= 5)
    hr = oc.get("hash_ribbons") or {}
    fire("b5", "bottom", "Hash-ribbon recovery", "30-day hash rate crossed back above the 60-day in the last 30 days", hr.get("state") is not None,
         f"{hr.get('state')} (30d {hr.get('hash_30d_eh', 0):,.0f} vs 60d {hr.get('hash_60d_eh', 0):,.0f} EH/s)" if hr.get("state") else hr.get("reason") or why(oc),
         hr.get("state") == "recovery")
    cs = (chaos or {}).get("score")
    ok = cs is not None and vix_ratio is not None
    fire("b6", "bottom", "Chaos under 50 and VIX / VIX3M under 1", "Chaos score < 50 and VIX / VIX3M < 1", ok,
         f"Chaos {cs:+.0f} · VIX/VIX3M {vix_ratio:.2f}" if ok else "Chaos score or VIX / VIX3M not in this run", ok and cs < 50 and vix_ratio < 1)
    fire("t1", "top", "MVRV Z-score at or over 6", "MVRV Z >= 6", z is not None, f"MVRV Z {z:+.2f}" if z is not None else why(oc, "mvrv_z"), z is not None and z >= 6)
    my = pc.get("mayer")
    fire("t2", "top", "Mayer multiple at or over 2.4", "price / 200-day MA >= 2.4", my is not None, f"Mayer {my:.2f}" if my is not None else why(pc, "mayer"), my is not None and my >= 2.4)
    hd = fu.get("high_days_running")
    fire("t3", "top", "Funding persistently high", "daily funding in its top decile (of the fetched days) for 7 days", hd is not None,
         f"{hd} days running in the top decile (p90 {fu.get('p90', 0) * 100:.4f}% per 8h)" if hd is not None else why(fu, "history"), hd is not None and hd >= 7)
    nr, r30 = ib.get("negative_days_running"), pc.get("return_30d_pct")
    ok = nr is not None and not (ib.get("missing") or {}).get("streak") and r30 is not None
    fire("t4", "top", "ETF outflows 5 days running after a run-up", "IBIT net flow < 0 for 5 days and BTC 30-day return >= +20%", ok,
         f"{nr} outflow days running · 30d return {r30:+.1f}%" if ok else why(ib, "streak") if r30 is not None else "30-day return not available", ok and nr >= 5 and r30 >= 20)
    pv = ((chaos or {}).get("prev") or {}).get("regime")
    ok = cs is not None and pv is not None
    fire("t5", "top", "Chaos turning from peace to chaos", "previous regime peace or neutral and now stress or chaos", ok,
         f"{pv} -> {chaos.get('regime')}" if ok else "Chaos regime or previous day not in this run",
         ok and pv in ("peace", "neutral") and chaos.get("regime") in ("stress", "chaos"))
    return rows


def verdict(rows):
    """HOLD / BUY (start) / BUY (add) / SELL (trim) / NO CALL. Only graded rows vote; fewer than half the rows available is NO CALL."""
    avail = [r for r in rows if r["status"] != "missing"]
    side = lambda s: [r for r in rows if r["side"] == s]
    votes = lambda s: sum(r["status"] == "fired" and r["graded"] for r in side(s))
    fired = lambda s: sum(r["status"] == "fired" for r in side(s))
    gradedn = sum(r["graded"] for r in rows)
    base = {"available": len(avail), "total": len(rows), "bottom_fired": fired("bottom"), "bottom_votes": votes("bottom"), "bottom_total": len(side("bottom")),
            "top_fired": fired("top"), "top_votes": votes("top"), "top_total": len(side("top")), "graded_rows": gradedn,
            "missing_rows": [r["name"] for r in rows if r["status"] == "missing"]}
    if len(avail) * 2 < len(rows):
        return {**base, "call": "NO CALL", "text": f"{len(avail)} of {len(rows)} rows available, under half. Missing: " + "; ".join(base["missing_rows"]) + "."}
    buy = "BUY (add)" if votes("bottom") >= 5 else "BUY (start)" if votes("bottom") >= 3 else None
    sell = votes("top") >= 3
    call = "HOLD" if (buy and sell) or not (buy or sell) else buy or "SELL (trim)"
    text = f"{len(avail)} of {len(rows)} rows available; {base['bottom_fired']} of {base['bottom_total']} bottom and {base['top_fired']} of {base['top_total']} top rows fired"
    if not gradedn:
        text += ". Rules not yet graded on history: untested rows show but do not vote, so this card cannot say BUY or SELL yet"
    elif buy and sell:
        text += ". Bottom and top rows both reached their counts: no call"
    need = max(0, 3 - votes("bottom"))
    base["flip"] = (f"BUY (start) needs {need} more graded bottom row(s) fired; SELL (trim) needs {max(0, 3 - votes('top'))} more graded top row(s) fired"
                    + ("; no row is graded yet" if not gradedn else ""))
    return {**base, "call": call, "text": text + "."}


# ---------------------------------------------------------------- collecting
def _group(parts):
    """A block of sub-parts with a status rolled up from them."""
    got = sum(p.get("status") != "missing" for p in parts.values())
    return {**parts, "status": "missing" if not got else "fresh" if got == len(parts) else "partial"}


def _ms(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _record_ibit(conn, run_id, ib, pid):
    lake.record_observations(conn, [lake.obs(IBIT_OBS, ib["shares_as_of"], value=ib["shares_outstanding"], dims={"nav": ib["nav"], "nav_as_of": ib["nav_as_of"]},
                                             source="ishares", payload_id=pid, time_quality="as_of_date")], run_id=run_id)


def _stored_ibit(conn):
    with lake._WRITE_LOCK:
        rows = conn.execute("SELECT observed_at, value_num, dims_json FROM v2_observations WHERE metric_id = ? AND value_num IS NOT NULL ORDER BY observed_at, obs_id",
                            (IBIT_OBS,)).fetchall()
    latest = {r[0][:10]: {"date": r[0][:10], "shares": r[1], "nav": json.loads(r[2])["nav"]} for r in rows}      # a revised day: the later fetch wins
    return [latest[d] for d in sorted(latest)]


def _cm_rows(session, today, errors):
    """Full history once a month, a 21-day tail each run on top of it (the free API serves ~5,700 rows; a full capture each run would be 1.5 MB)."""
    base = src._stored_payload(session, "coinmetrics_full", 30)
    try:
        if base:
            rows = {r["time"][:10]: r for r in json.loads(base[1])}
            tail, _ = src.coinmetrics_btc(session, (today - timedelta(days=21)).isoformat())
            rows.update({r["time"][:10]: r for r in tail})
            return [rows[k] for k in sorted(rows)]
        return src.coinmetrics_btc(session, CM_START)[0]
    except src.SourceUnavailable as e:
        errors["coinmetrics"] = str(e)
        return None


def collect(conn, session, run_id, today, chaos=None, vix_ratio=None):
    """Fetch every Bitcoin source (a failure becomes an entry in `errors` and a missing part, never an exception), compute the block."""
    errors, blk = {}, {"version": BITCOIN_VERSION, "as_of": today.isoformat()}

    def get(key, fn, *a):
        try:
            return fn(session, *a)[0]
        except Exception as e:                   # SourceUnavailable, or a provider answering something unparseable
            errors[key] = lake.redact(f"{type(e).__name__}: {e}")
            return None

    cm = _cm_rows(session, today, errors)
    df = cm_frame(cm) if cm else None
    closes, source = None, None
    try:
        from core import bars
        bc = bars.connect_readonly()
        if bc is not None:
            b = bars.read_bars(bc, "BTC-USD", "1d")
            if len(b):
                closes = pd.Series(b["close"].values, index=pd.DatetimeIndex(b.index).tz_localize(None).normalize())
                source = "stored BTC-USD daily bars (chart store)"
            bc.close()
    except Exception as e:
        errors["bars"] = f"{type(e).__name__}: {e}"
    if (closes is None or len(closes) < MA_DAYS) and df is not None and df["price"].notna().any():
        closes, source = df["price"].dropna(), "Coin Metrics PriceUSD (stored BTC-USD bars are missing or shorter than 1,400 days)"
    blk["price"] = price_cycle(closes, today, source) if closes is not None else missing(errors.get("coinmetrics") or "no BTC-USD daily bars stored and Coin Metrics unavailable")
    blk["onchain"] = onchain(df, today) if df is not None else missing(errors.get("coinmetrics", "Coin Metrics unavailable"))

    tk = get("deribit:ticker", src.deribit_ticker)
    hist, d0 = [], today
    for _ in range(6):                               # 744 hourly rows (31 days) a call: six calls is about 6 months for the top-decile test
        part = get("deribit:funding", src.deribit_funding_history, _ms(d0 - timedelta(days=31)), _ms(d0))
        hist = (part or []) + hist
        d0 -= timedelta(days=31)
        if part is None:
            break
    dv = get("deribit:dvol", src.deribit_dvol, _ms(today - timedelta(days=365)), _ms(today + timedelta(days=1)))
    blk["leverage"] = _group({"funding": funding(tk, hist, today) if tk else missing(errors.get("deribit:ticker", "no Deribit ticker")),
                              "dvol": dvol(dv) if dv else missing(errors.get("deribit:dvol", "no DVOL candles"))})

    ib = None
    try:
        ib, pid = src.ishares_ibit(session)
        _record_ibit(conn, run_id, ib, pid)
    except Exception as e:
        errors["ishares:IBIT"] = lake.redact(f"{type(e).__name__}: {e}")
    stored = _stored_ibit(conn)
    flows_ibit = ibit_flows(stored)
    if flows_ibit.get("status") == "missing" and "ishares:IBIT" in errors:
        flows_ibit["reason"] = errors["ishares:IBIT"]
    sc = get("coinbase", src.coinbase_btc_usd), get("kraken", src.kraken_btc_usdt)
    ust, usc = get("defillama:usdt", src.defillama_stablecoin, 1), get("defillama:usdc", src.defillama_stablecoin, 2)
    cot_rows = get("cftc", src.cftc_cot, *COT_CONTRACT, (today - timedelta(days=3 * 365)).isoformat())
    blk["flows"] = _group({"ibit": flows_ibit, "coinbase_premium": premium(*sc) if all(sc) else missing(
        "; ".join(errors[k] for k in ("coinbase", "kraken") if k in errors) or "price not available"),
        "stablecoins": stablecoins(ust, usc) if ust and usc else missing("; ".join(errors[k] for k in ("defillama:usdt", "defillama:usdc") if k in errors) or "not available"),
        "cot": cot(cot_rows) if cot_rows else missing(errors.get("cftc", "no CFTC rows"))})
    blk["rows"] = rows_of(blk, chaos, vix_ratio)
    blk["verdict"] = verdict(blk["rows"])
    blk["history"] = history(closes, df, today) if closes is not None and df is not None else {}
    parts = [blk["price"], blk["onchain"], *(v for g in ("leverage", "flows") for k, v in blk[g].items() if k != "status")]
    got = sum(p.get("status") != "missing" for p in parts)
    blk["status"] = "missing" if not got else "fresh" if got == len(parts) else "partial"
    blk["reason"] = None if got else "no Bitcoin source answered"
    blk["source_errors"] = errors
    blk["note"] = "Conditions and their measured history, not a trade call or financial advice."
    return blk

