"""Forced-flow projection for card R (Market Regime / Shock Reader): what the mechanical sellers of card 8 would add if SPY moved,
how vol-control and CTA exposure drift over the next 30 sessions as old days roll out of their lookbacks, and the daily estimate
rebuilt over past sessions. ESTIMATES built from stored data only (SPY daily closes, the run's leveraged-ETF AUM and dealer gamma,
the stored snapshots of past runs); nothing here fetches. A source without its inputs is missing with a reason, never zero.

Sources (sell = negative USD):
  leveraged ETFs   end-of-day rebalance = sum AUM x L(L-1) x the session's return (card 8's formula, applied per move)
  CTA              the replicated trend model of card 3 (mean of tanh(z) over 1/3/6/12 month windows) x CTA_NOTIONAL
  dealer gamma     net GEX is stored at spot only (no gamma-by-price profile), so gamma is taken as linear in price through zero
                   at the zero-gamma level; the hedge flow of a move is -integral of it (constant gamma when there is no zero-gamma level)
  vol-control      exposure = min(10% / max(1m, 3m realized vol), 1.5) x VOL_CONTROL_AUM, re-targeted with the move's return added
"""
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

from core import forecast as F
from core import market_calendar

FLOWPROJ_VERSION = "flowproj_v1"
MOVES = [round(-10 + 0.5 * i, 1) for i in range(31)]          # -10% .. +5% in 0.5% steps, from the latest spot
VC_AUM_BAND = (300e9, 500e9)                                    # the commonly cited range around forecast.VOL_CONTROL_AUM
CTA_NOTIONAL = 100e9                                            # USD of S&P futures a full (+1.0) replicated CTA position stands for
CTA_BAND = (50e9, 150e9)                                        # sell-side estimates of CTA equity length span roughly this range
SESSIONS = 30
PATHS = {"calm": ("calm: realized 0.8%/day", 0.008), "recent": ("same as the last 5 sessions", None), "stressed": ("stressed: realized 2%/day", 0.02)}
BUY_MIN = 5e9                                                   # re-leveraging = +$5B or more bought after the path's low in exposure
HISTORY_SESSIONS = 252
ASSUMPTIONS = [
    f"Vol-control AUM ${F.VOL_CONTROL_AUM / 1e9:.0f}B (band ${VC_AUM_BAND[0] / 1e9:.0f}-{VC_AUM_BAND[1] / 1e9:.0f}B), target vol {F.VOL_CONTROL_TARGET:.0%}, "
    f"leverage cap {F.VOL_CONTROL_CAP:g}x, realized vol = the larger of the 21- and 63-session standard deviation of daily returns x sqrt(252).",
    f"CTA: the replicated trend model (1/3/6/12-month momentum, tanh of each z-score, averaged; -1..+1) scaled to ${CTA_NOTIONAL / 1e9:.0f}B "
    f"for a full position (band ${CTA_BAND[0] / 1e9:.0f}-{CTA_BAND[1] / 1e9:.0f}B). A window flips when price crosses its flip level (the close it is measured from).",
    "Leveraged ETFs: this run's AUM of SSO, UPRO, SPXL, SH, SDS, SPXU, SPXS; rebalance = AUM x L(L-1) x the session's return.",
    "Dealer gamma: the stored net GEX is at spot only (no gamma by price), so gamma is assumed linear in price, zero at the zero-gamma level "
    "(sign flip there); hedge flow = minus its integral over the move. History days use the day's price change x that day's stored net GEX.",
    "Price scenarios: the next close at spot x (1 + move), relative to an unchanged close; ranges come from the AUM bands of vol-control and CTA only.",
    f"Time paths: daily moves alternate up and down at the stated size (calm 0.8%/day, the last 5 sessions' root-mean-square move, stressed 2%/day), "
    f"so price stays near spot; NYSE trading days only. Vol-control re-leveraging starts the session after the path's lowest exposure, "
    f"when it buys +${BUY_MIN / 1e9:g}B or more from there to the end of the {SESSIONS} sessions.",
    "Everything here is an estimate from public AUM and replicated models, not reported positions.",
]


def _missing(why):
    return {"status": "missing", "reason": why}


def _r(x, n=0):
    return None if x is None or not math.isfinite(x) else round(float(x), n)


def _base(closes, dates, spot, today):
    """Stored closes before today, then the latest spot as today's close when today is a trading day -> pd.Series by date."""
    s = pd.Series([float(c) for c in closes], index=pd.to_datetime(list(dates))).dropna()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s = s[s.index.date < today]
    if spot and market_calendar.is_trading_day(today):
        s = pd.concat([s, pd.Series([float(spot)], index=[pd.Timestamp(today)])])
    return s


def _sessions_after(d, n):
    out = []
    while len(out) < n:
        d += timedelta(days=1)
        if market_calendar.is_trading_day(d):
            out.append(d)
    return out


def gamma_flow(g0, s0, s1, zero):
    """Dealer hedge flow (USD) of a move s0 -> s1 with net gamma g0 (USD per 1 point) at s0, linear in price through 0 at `zero`."""
    if zero is None or abs(s0 - zero) < 1e-9:
        return -g0 * (s1 - s0)
    k = g0 / (s0 - zero)
    return -k * ((s1 - zero) ** 2 - (s0 - zero) ** 2) / 2


def _cta_parts(c, rv):
    """Per-window tanh(z) / n_windows (sums to the replicated exposure) for the last close of `c`."""
    _, zs = F._trend_exposure(c, rv=rv)
    return {k: math.tanh(z) / len(zs) for k, z in zs.items()}


# ============================================================ 1. price scenarios
def scenarios(c, fl):
    spot = float(c.iloc[-1])
    r = c.pct_change().dropna()
    lev, dg = fl.get("leveraged_etfs") or {}, fl.get("dealer_gamma") or {}
    tot = lev.get("flow_per_1pct_move")
    g0, zero = dg.get("net_gex_usd_per_1pt"), dg.get("zero_gamma")
    missing = {}
    if not (lev.get("funds") and tot is not None):
        missing["leveraged_etf"] = "no leveraged-ETF AUM in this run"
    if g0 is None:
        missing["dealer_gamma"] = "no dealer-gamma estimate in this run (the option chain was missing)"
    enough_vc, enough_cta = len(r) >= 63, len(c) > max(F.LOOKBACKS.values())
    if not enough_vc:
        missing["vol_control"] = f"needs 63+ daily returns, has {len(r)}"
    if not enough_cta:
        missing["cta"] = f"needs {max(F.LOOKBACKS.values()) + 1}+ daily closes, has {len(c)}"
    nxt = c.index[-1] + pd.Timedelta(days=1)
    rv60 = r.tail(60).std() * math.sqrt(252)
    vc0 = F.vol_control_exposure(pd.concat([r, pd.Series([0.0])], ignore_index=True))[0] if enough_vc else None
    cta0 = _cta_parts(pd.concat([c, pd.Series([spot], index=[nxt])]), rv60) if enough_cta else None
    flips = {k: float(c.iloc[-n]) for k, n in F.LOOKBACKS.items() if len(c) > n}
    src = {k: [] for k in ("leveraged_etf", "cta", "dealer_gamma", "vol_control")}
    windows = []
    for mv in MOVES:
        s1 = spot * (1 + mv / 100)
        src["leveraged_etf"].append(None if "leveraged_etf" in missing else tot * mv)
        src["dealer_gamma"].append(None if "dealer_gamma" in missing else gamma_flow(g0, spot, s1, zero))
        src["vol_control"].append(None if vc0 is None else
                                  (F.vol_control_exposure(pd.concat([r, pd.Series([mv / 100])], ignore_index=True))[0] - vc0) * F.VOL_CONTROL_AUM)
        if cta0 is None:
            src["cta"].append(None)
            windows.append(None)
            continue
        parts = _cta_parts(pd.concat([c, pd.Series([s1], index=[nxt])]), rv60)
        src["cta"].append(sum(parts[k] - cta0[k] for k in parts) * CTA_NOTIONAL)
        windows.append([k for k, lvl in flips.items() if (s1 - lvl) * (spot - lvl) < 0])
    have = [k for k in src if k not in missing]
    total = [sum(src[k][i] for k in have) if have else None for i in range(len(MOVES))]

    def band(i, j):                  # vol-control and CTA at the low / high end of their AUM bands
        return None if not have else sum(src[k][i] * ({"vol_control": VC_AUM_BAND[j] / F.VOL_CONTROL_AUM, "cta": CTA_BAND[j] / CTA_NOTIONAL}.get(k, 1.0)) for k in have)
    lo = [band(i, 0) for i in range(len(MOVES))]
    hi = [band(i, 1) for i in range(len(MOVES))]
    rows = [{"move_pct": mv, "price": _r(spot * (1 + mv / 100), 2), **{k: _r(src[k][i]) for k in src}, "total": _r(total[i]),
             "total_low": _r(min(lo[i], hi[i])) if lo[i] is not None else None, "total_high": _r(max(lo[i], hi[i])) if hi[i] is not None else None,
             "cta_windows_flipped": windows[i]} for i, mv in enumerate(MOVES)]
    return {"spot": _r(spot, 2), "rows": rows, "sources_in_total": have, "missing": missing,
            "cta_flip_levels": {k: _r(v, 2) for k, v in flips.items()}, "zero_gamma": _r(zero, 2) if zero else None,
            "gamma_model": "linear through the zero-gamma level" if zero else "constant (no zero-gamma level in this run)"}


# ============================================================ 2. time paths
def time_paths(c, n=SESSIONS):
    r = c.pct_change().dropna()
    if len(r) < 63:
        return _missing(f"needs 63+ daily returns, has {len(r)}")
    last = c.index[-1].date()
    days = _sessions_after(last, n)
    e0 = F.vol_control_exposure(r)[0]
    rms5 = float(np.sqrt((r.tail(5) ** 2).mean()))
    out = {"dates": [d.isoformat() for d in days], "paths": {}}
    for key, (label, size) in PATHS.items():
        a = rms5 if size is None else size
        fut = [(-a if i % 2 == 0 else a) for i in range(n)]
        ex, prev, flow = [], e0, []
        for i in range(n):
            e = F.vol_control_exposure(pd.concat([r, pd.Series(fut[:i + 1])], ignore_index=True))[0]
            ex.append(e)
            flow.append((e - prev) * F.VOL_CONTROL_AUM)
            prev = e
        # single days are noisy (each one drops a different old day): re-leveraging starts the session after the path's lowest
        # exposure, when the buying from there to the end of the path is BUY_MIN or more
        lo = int(np.argmin([e0] + ex))
        first = days[lo].isoformat() if lo < n and (ex[-1] - ([e0] + ex)[lo]) * F.VOL_CONTROL_AUM >= BUY_MIN else None
        cum = list(np.cumsum(flow))
        out["paths"][key] = {"label": label, "daily_move_pct": round(a * 100, 3), "exposure": [round(float(x), 4) for x in ex],
                             "flow": [_r(x) for x in flow], "cumulative": [_r(x) for x in cum],
                             "cumulative_low": _r(cum[-1] * VC_AUM_BAND[0] / F.VOL_CONTROL_AUM), "cumulative_high": _r(cum[-1] * VC_AUM_BAND[1] / F.VOL_CONTROL_AUM),
                             "releverage_date": first}
    if len(c) > max(F.LOOKBACKS.values()):
        spot, ext, ex, sig = float(c.iloc[-1]), c.copy(), [], []
        e_prev = F._trend_exposure(c)[0]
        start = e_prev
        for d in days:
            ext = pd.concat([ext, pd.Series([spot], index=[pd.Timestamp(d)])])
            e = F._trend_exposure(ext)[0]
            ex.append(e)
            sig.append({k: np.sign(ext.iloc[-1] - ext.iloc[-1 - m]) for k, m in F.LOOKBACKS.items()})
        now = {k: np.sign(c.iloc[-1] - c.iloc[-1 - m]) for k, m in F.LOOKBACKS.items()}
        # a window flips when its return changes sign; a window measured from a close equal to spot (flat) has not flipped
        flips = {k: next((days[i].isoformat() for i, s in enumerate(sig) if s[k] == -now[k] != 0), None) for k in F.LOOKBACKS}
        flow = [(e - p) * CTA_NOTIONAL for e, p in zip(ex, [start] + ex[:-1])]
        out["cta_flat"] = {"label": "CTA, price flat at spot", "exposure_now": round(start, 4), "exposure": [round(x, 4) for x in ex],
                           "flow": [_r(x) for x in flow], "cumulative": [_r(x) for x in np.cumsum(flow)], "window_flip_dates": flips,
                           "sign_flip_date": next((days[i].isoformat() for i, e in enumerate(ex) if (e > 0) != (start > 0)), None)}
    else:
        out["cta_flat"] = _missing(f"needs {max(F.LOOKBACKS.values()) + 1}+ daily closes, has {len(c)}")
    return {"status": "fresh", **out}


# ============================================================ history (the daily estimate rebuilt over past sessions)
def history(c, runs, n=HISTORY_SESSIONS):
    """Per past session: vol-control and CTA re-estimated from the closes alone; leveraged ETFs and dealer gamma only on days a
    stored run carries their inputs (`runs`: [{date, lev_per_1pct, net_gex}], oldest first). A day without an input is None."""
    r = c.pct_change()
    rv = pd.concat([r.rolling(21).std(), r.rolling(63).std()], axis=1).max(axis=1, skipna=False) * math.sqrt(252)
    vc = (F.VOL_CONTROL_TARGET / rv).clip(upper=F.VOL_CONTROL_CAP)
    rv63 = r.rolling(63).std() * math.sqrt(252)
    cta = pd.concat([np.tanh((c / c.shift(m) - 1) / (rv63 * math.sqrt(m / 252))) for m in F.LOOKBACKS.values()], axis=1).mean(axis=1, skipna=False)
    by_day = {}
    for x in runs or []:
        by_day.setdefault(str(x["date"])[:10], []).append(x)
    idx = list(c.index[-n:])
    pos = {t: i for i, t in enumerate(c.index)}
    out = {k: [] for k in ("vol_control", "cta", "leveraged_etf", "dealer_gamma")}
    for t in idx:
        i = pos[t]
        prev = c.index[i - 1] if i else None
        out["vol_control"].append(_r((vc.iloc[i] - vc.iloc[i - 1]) * F.VOL_CONTROL_AUM) if i else None)
        out["cta"].append(_r((cta.iloc[i] - cta.iloc[i - 1]) * CTA_NOTIONAL) if i else None)
        # the inputs known going into the session: the last run dated the previous session, else the session's own first run
        run = (by_day.get(prev.date().isoformat()) or [None])[-1] if prev is not None else None
        run = run or (by_day.get(t.date().isoformat()) or [None])[0]
        lev, ng = (run or {}).get("lev_per_1pct"), (run or {}).get("net_gex")
        out["leveraged_etf"].append(_r(lev * 100 * r.iloc[i]) if lev is not None and i else None)
        out["dealer_gamma"].append(_r(-ng * (c.iloc[i] - c.iloc[i - 1])) if ng is not None and i else None)
    total = [None if v is None or k is None else v + k + (l or 0) + (g or 0)
             for v, k, l, g in zip(out["vol_control"], out["cta"], out["leveraged_etf"], out["dealer_gamma"])]
    complete = [all(out[k][j] is not None for k in out) for j in range(len(idx))]
    return {"dates": [t.date().isoformat() for t in idx], "close": [_r(float(c[t]), 2) for t in idx], "sources": out, "total": total, "complete": complete,
            "note": "total = vol-control + CTA every day, plus leveraged ETFs and dealer gamma on the days a stored run has their inputs "
                    "(their own rows are empty otherwise: missing, not zero)"}


# ============================================================ assembly
def _usd(v):
    return f"{'-' if v < 0 else '+'}${abs(v) / 1e9:.1f}B"


def summary(sc, tp):
    row = next((x for x in sc["rows"] if x["move_pct"] == -5.0), None) if sc else None
    a = f"Another {_usd(row['total'])} (range {_usd(row['total_low'])} to {_usd(row['total_high'])}) of forced flow if SPY -5%" if row and row["total"] is not None else "No -5% scenario (inputs missing)"
    calm = ((tp or {}).get("paths") or {}).get("calm") or {}
    d = calm.get("releverage_date")
    b = (f"under calm markets vol-control funds turn buyers around {date.fromisoformat(d):%a %b %-d %Y}" if d else
         f"under calm markets vol-control funds do not turn buyers within {SESSIONS} sessions") if calm else "no vol-control time path"
    return f"{a}; {b} (estimate)."


def build(closes, dates, spot, flows, today, runs=None):
    """The `flow_projection` block of the regime: scenarios, time paths, history, summary. Missing with a reason when the SPY
    closes, dates, spot or flows are not there."""
    if not (flows or {}).get("status") == "fresh":
        return _missing(f"SPY flows {(flows or {}).get('status', 'missing')}: {(flows or {}).get('reason', 'not in this run')}")
    if not spot:
        return _missing("no SPY spot in this run")
    if not closes or not dates or len(closes) != len(dates):
        return _missing("no dated SPY daily closes in this run")
    c = _base(closes, dates, spot, today)
    if len(c) < 64:
        return _missing(f"needs 64+ SPY daily closes, has {len(c)}")
    sc = scenarios(c, flows)
    tp = time_paths(c)
    return {"status": "fresh", "version": FLOWPROJ_VERSION, "estimate": True, "as_of": str(today), "last_close_date": c.index[-1].date().isoformat(),
            "assumptions": ASSUMPTIONS, "scenarios": sc, "time_path": tp, "history": history(c, runs), "summary": summary(sc, tp)}
