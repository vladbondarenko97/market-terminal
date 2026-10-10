"""Chaos / Peace index (Forecast Lab card 14): pure functions, data in and a dict out. No network, no database.

The index is the mean of twelve z-scores, each against its own 3-year history and oriented so that higher means more
market stress. It describes what moved. It says nothing about who moved it or why, and it recommends nothing.
"""
import math
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from core import market_calendar as MC
from core.watch import grade

CHAOS_VERSION = "chaos_v1"
WINDOW = "1096D"                  # 3 years of calendar days; a day-based window keeps the 7-days-a-week EPU series on 3 years too
MIN_PERIODS = 252
SPAN_DAYS = 3 * 365 - 30          # a series must span 3 years less a 30-day tolerance (FRED returns exactly 3 years for HY)
STALE_DAYS = 10
CLIP = 3.0
# ponytail: fixed, uncalibrated cut-offs. score = 50 * mean z (clipped to +-100); regimes split it at -30 / 30 / 60. Not fitted to any outcome.
SCALE = 50.0
REGIMES = ((-30, "peace"), (30, "neutral"), (60, "stress"))      # score <= bound -> name; above the last bound -> "chaos"
ELECTION = date(2026, 11, 3)
TRACK_TRADING_DAYS = 60
HISTORY_DAYS = 60
LABELS = {
    "vix": "VIX", "vix_vix3m": "VIX / VIX3M", "vvix": "VVIX", "skew": "SKEW", "move": "MOVE", "hy_spread": "High-yield spread",
    "spy_rv20": "SPY realised vol 20d", "spy_drawdown": "SPY drawdown from 252d high", "epu": "Policy uncertainty (EPU, 7d mean)",
    "equity_put_call": "Equity put/call", "vix_slope": "VIX9D / VIX6M", "short_volume_spy": "SPY short volume share"}
MODEL = ("Mean of up to 12 z-scores, each the latest value against its own trailing 3-year history (clipped to +-3), all oriented "
         "so that higher is more stress; score = 50 x mean z, limited to -100 (peace) .. +100 (chaos). The EPU daily series is "
         "published 7 days a week and is noisy, so it enters as a 7-day mean. A component with under 3 years of history or no "
         "observation in the last 10 days is left out and listed, and the score says how many components it used.")


def regime_of(score):
    for bound, name in REGIMES:
        if score <= bound:
            return name
    return "chaos"


def _clean(s):
    """Numeric, date-indexed (tz-naive, midnight), sorted, no NaN, one value per date."""
    if s is None or len(s) == 0:
        return None
    s = pd.to_numeric(pd.Series(s), errors="coerce").dropna()
    idx = pd.DatetimeIndex(s.index)
    s.index = (idx.tz_localize(None) if idx.tz is not None else idx).normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s if len(s) else None


def _ratio(a, b):
    return (a / b).replace([np.inf, -np.inf], np.nan).dropna()


def _drawdown(c):
    return (1 - c / c.rolling(252, min_periods=252).max()).dropna()


def _rv20(c):
    return (np.log(c / c.shift(1)).rolling(20).std() * math.sqrt(252)).dropna()


SPECS = {    # key -> (raw inputs, builder)
    "vix": (("VIX",), lambda r: r["VIX"]),
    "vix_vix3m": (("VIX", "VIX3M"), lambda r: _ratio(r["VIX"], r["VIX3M"])),
    "vvix": (("VVIX",), lambda r: r["VVIX"]),
    "skew": (("SKEW",), lambda r: r["SKEW"]),
    "move": (("MOVE",), lambda r: r["MOVE"]),
    "hy_spread": (("HY",), lambda r: r["HY"]),
    "spy_rv20": (("SPY",), lambda r: _rv20(r["SPY"])),
    "spy_drawdown": (("SPY",), lambda r: _drawdown(r["SPY"])),
    "epu": (("EPU",), lambda r: r["EPU"].rolling("7D", min_periods=5).mean().dropna()),
    "equity_put_call": (("PUTCALL",), lambda r: r["PUTCALL"]),
    "vix_slope": (("VIX9D", "VIX6M"), lambda r: _ratio(r["VIX9D"], r["VIX6M"])),
    "short_volume_spy": (("SHORTVOL",), lambda r: r["SHORTVOL"]),
}


def _zscore(s):
    z = (s - s.rolling(WINDOW, min_periods=MIN_PERIODS).mean()) / s.rolling(WINDOW, min_periods=MIN_PERIODS).std()
    return z.replace([np.inf, -np.inf], np.nan).clip(-CLIP, CLIP)


def _trading_days(end, n):
    out, d = [], end
    while len(out) < n:
        if MC.is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def _missing(key, reason, n=0):
    return {"status": "missing", "label": LABELS[key], "value": None, "z": None, "as_of": None, "n": n, "reason": reason}


def _components(raw, raw_err, today, days):
    """-> (components, zday) where zday holds each fresh component's z on the given days (last observation at or before each)."""
    comps, zday = {}, {}
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in days])
    for key, (needs, build) in SPECS.items():
        absent = [n for n in needs if raw.get(n) is None]
        if absent:
            comps[key] = _missing(key, "; ".join(raw_err.get(n) or f"{n}: no data" for n in absent))
            continue
        s = build(raw)
        s = s.dropna() if s is not None else None
        if s is None or len(s) < MIN_PERIODS:
            comps[key] = _missing(key, f"history shorter than 3y ({0 if s is None else len(s)} observations"
                                       + (f" since {s.index[0]:%Y-%m-%d})" if s is not None and len(s) else ")"), 0 if s is None else len(s))
            continue
        last, first = s.index[-1], s.index[0]
        if (last - first).days < SPAN_DAYS:
            comps[key] = _missing(key, f"history shorter than 3y ({len(s)} observations since {first:%Y-%m-%d})", len(s))
            continue
        if (pd.Timestamp(today) - last).days > STALE_DAYS:
            comps[key] = _missing(key, f"stale: last observation {last:%Y-%m-%d}", len(s))
            continue
        z = _zscore(s).dropna()
        if z.empty:
            comps[key] = _missing(key, "no usable z-score (flat history)", len(s))
            continue
        zd = z.reindex(idx, method="ffill", tolerance=pd.Timedelta(days=STALE_DAYS))
        comps[key] = {"status": "fresh", "label": LABELS[key], "value": round(float(s.iloc[-1]), 4), "z": round(float(z.iloc[-1]), 2),
                      "as_of": f"{last:%Y-%m-%d}", "n": int((s.index > last - pd.Timedelta(WINDOW)).sum()), "reason": None}
        zday[key] = zd
    return comps, pd.DataFrame(zday, index=idx)


def _levers(inp, today):
    t = pd.Timestamp(today)
    fred = {k: _clean(v) for k, v in (inp.get("fred") or {}).items()}
    errs = inp.get("errors") or {}
    rows = []

    def row(key, label, unit, level, as_of, change, reason=None):
        ok = level is not None
        rows.append({"key": key, "label": label, "unit": unit, "level": None if not ok else round(float(level), 3), "as_of": as_of,
                     "change_20d": None if change is None or not ok else round(float(change), 3),
                     "status": "fresh" if ok else "missing", "reason": None if ok else reason})

    def weekly(key, label, unit, s, why):
        if s is None or len(s) < 5:
            return row(key, label, unit, None, None, None, why)
        row(key, label, unit, s.iloc[-1], f"{s.index[-1]:%Y-%m-%d}", s.iloc[-1] - s.iloc[-5])     # 4 weekly observations = 4 weeks

    miss = lambda name: f"{name}: {errs.get(f'fred:{name}') or 'no data'}"
    w, tga, rrp = fred.get("WALCL"), fred.get("WTREGEN"), fred.get("RRPONTSYD")
    if w is None or tga is None or rrp is None:
        weekly("net_liquidity", "Net liquidity (Fed assets - TGA - reverse repo)", "USD bn", None,
               "; ".join(miss(n) for n, v in (("WALCL", w), ("WTREGEN", tga), ("RRPONTSYD", rrp)) if v is None))
    else:
        at = lambda s: s.reindex(w.index, method="ffill")           # the last value at or before each WALCL date
        net = (w / 1000 - at(tga) / 1000 - at(rrp)).dropna()
        weekly("net_liquidity", "Net liquidity (Fed assets - TGA - reverse repo)", "USD bn", net, "no overlapping observations")
    res = fred.get("WRESBAL")
    weekly("reserves", "Bank reserves (WRESBAL)", "USD bn", None if res is None else res / 1000, miss("WRESBAL"))
    spr = _clean(inp.get("spr"))
    weekly("spr", "Strategic Petroleum Reserve stocks", "thousand barrels", spr, errs.get("eia:WCSSTUS1") or "WCSSTUS1: no data")

    def flows(key, label, recs, field, pick, why):
        pts = []
        for r in recs or []:
            try:
                v, d = float(r[field]), pd.Timestamp(r[pick[0]][:10])
            except (KeyError, TypeError, ValueError):
                continue
            if pick[1](r):
                pts.append((d, v / 1e9))
        if not pts:
            return row(key, label, "USD bn", None, None, None, why)
        cur = sum(v for d, v in pts if t - pd.Timedelta(days=28) < d <= t)
        prev = sum(v for d, v in pts if t - pd.Timedelta(days=56) < d <= t - pd.Timedelta(days=28))
        row(key, label, "USD bn", cur, f"{max(d for d, _ in pts):%Y-%m-%d}", cur - prev)
    bills, coupons = ("Bill",),("Note", "Bond", "TIPS", "FRN")
    auc = inp.get("auctions")
    why = errs.get("treasury:auctions") or "no auction records"
    flows("bills_offered", "Treasury bills offered, last 28 days", auc, "offeringAmount", ("auctionDate", lambda r: r.get("securityType") in bills), why)
    flows("coupons_offered", "Treasury coupons offered, last 28 days", auc, "offeringAmount",
          ("auctionDate", lambda r: r.get("securityType") in coupons), why)
    flows("buybacks", "Treasury buybacks accepted, last 28 days", inp.get("buybacks"), "total_par_amt_accepted",
          ("operation_date", lambda r: True), errs.get("treasury:buybacks") or "no buyback records")
    return {"status": "fresh" if any(r["status"] == "fresh" for r in rows) else "missing", "rows": rows,
            "note": "Shown next to the score and not part of it. change_20d is the change over 4 weeks (flows: the last 28 days minus the 28 days before)."}


def election_day(year):
    """Tuesday after the first Monday of November."""
    d = date(year, 11, 1)
    d += timedelta(days=(0 - d.weekday()) % 7)
    return d + timedelta(days=1)


def _event_study(gspc, vix):
    gspc, vix = _clean(gspc), _clean(vix)
    windows = {"-30..-1": (-31, -1), "0..+5": (-1, 5), "+6..+60": (5, 60), "0..+250": (-1, 250)}
    rows = []
    for name, ser, first in (("S&P 500", gspc, 1950), ("VIX", vix, 1990)):
        if ser is None or (ser := ser[ser.index >= f"{first}-01-01"]).empty:
            continue
        mids = []
        for y in range(first, ser.index[-1].year + 1):
            if y % 4 == 2:
                p = ser.index.searchsorted(pd.Timestamp(election_day(y)))
                if p < len(ser) and (ser.index[p] - pd.Timestamp(election_day(y))).days < 7:
                    mids.append(ser.index[p])
        for wname, (a, b) in windows.items():
            ret = (ser.shift(-b) / ser.shift(-a) - 1).dropna()
            sig = ret[ret.index.isin(mids)]
            g = grade(sig, ret[~ret.index.isin(mids)])
            rows.append({"series": name, "window": wname, "n": g["n"], "mean_pct": g["avg"], "base_mean_pct": g["bavg"], "hit_rate": g["p"],
                         "base_hit_rate": g["base"], "t": g["t"], "grade": g["edge"]})
    if not rows:
        return {"status": "missing", "reason": "long ^GSPC / ^VIX history not available", "rows": []}
    n = max(r["n"] for r in rows)
    return {"status": "fresh", "rows": rows, "midterms": n,
            "note": f"Descriptive. {n} midterm elections at most, so the best grade possible is 'thin'. Day 0 is the first trading day on or after "
                    "election day; windows are returns of the close in trading days. Only a higher mean than the other windows can earn a grade, "
                    "and neighbouring windows overlap, so the t-values are optimistic. It says nothing about why."}


def _lever_study(inp):
    fred = {k: _clean(v) for k, v in (inp.get("fred") or {}).items()}
    w, tga, rrp = fred.get("WALCL"), fred.get("WTREGEN"), fred.get("RRPONTSYD")
    levers = {"WALCL": ("Fed assets (WALCL)", "up = easier", w, 1), "WTREGEN": ("Treasury cash (TGA, WTREGEN)", "down = easier", tga, -1),
              "WRESBAL": ("Bank reserves (WRESBAL)", "up = easier", fred.get("WRESBAL"), 1)}
    if w is not None and tga is not None and rrp is not None:
        levers["NETLIQ"] = ("Net liquidity", "up = easier", (w / 1000 - tga.reindex(w.index, method="ffill") / 1000
                                                           - rrp.reindex(w.index, method="ffill")).dropna(), 1)
    levers["SPR"] = ("Strategic Petroleum Reserve stocks", "down = easier", _clean(inp.get("spr")), -1)
    rows = []
    for key, (label, rule, s, sign) in levers.items():
        definition = f"percent change over the 60 calendar days ending on the first Tuesday after the first Monday of November; {rule}"
        if s is None or len(s) < 10:
            rows.append({"lever": label, "definition": definition, "status": "missing", "reason": "not enough history", "n": 0, "n_base": 0,
                         "mean_pct": None, "base_mean_pct": None, "median_pct": None, "base_median_pct": None, "t": None, "grade": "none"})
            continue
        sig, base = {}, {}
        for y in range(s.index[0].year, s.index[-1].year + 1):
            e = pd.Timestamp(election_day(y))
            if e > s.index[-1]:               # this year's election day is still ahead: its 60-day window is not complete
                continue
            a, b = s[s.index <= e - pd.Timedelta(days=60)], s[s.index <= e]
            if a.empty or b.empty or (e - b.index[-1]).days > 14 or (e - pd.Timedelta(days=60) - a.index[-1]).days > 14 or a.iloc[-1] <= 0:
                continue
            (sig if y % 2 == 0 else base)[e] = sign * (b.iloc[-1] / a.iloc[-1] - 1)
        sig, base = pd.Series(sig, dtype=float), pd.Series(base, dtype=float)
        g = grade(sig, base)
        pct = lambda x, f: round(float(f(x)) * 100, 2) if len(x) else None      # grade() gives no mean under 8 cases
        rows.append({"lever": label, "definition": definition, "status": "fresh", "reason": None, "n": len(sig), "n_base": len(base),
                     "mean_pct": pct(sig, np.mean), "base_mean_pct": pct(base, np.mean), "median_pct": pct(sig, np.median),
                     "base_median_pct": pct(base, np.median), "t": g["t"], "grade": g["edge"]})
    return {"status": "fresh" if any(r["status"] == "fresh" for r in rows) else "missing", "rows": rows,
            "note": "Descriptive only. Even years (election years) against the same dates in odd years; a handful of years each, so small n. "
                    "One extreme year can dominate a mean, so the median is shown beside it; under 8 years on a side there is no t. "
                    "It says nothing about why a lever moved."}


def _election(today):
    d = ELECTION
    if today <= d:
        n = sum(1 for i in range(1, (d - today).days + 1) if MC.is_trading_day(today + timedelta(days=i)))
    else:
        n = -sum(1 for i in range(1, (today - d).days + 1) if MC.is_trading_day(d + timedelta(days=i)))
    until, k = d, 0
    while k < TRACK_TRADING_DAYS:
        until += timedelta(days=1)
        k += MC.is_trading_day(until)
    return {"date": d.isoformat(), "trading_days_to": n, "track_until": until.isoformat(), "tracking": today <= until}


def build(inp):
    """inp: today (date); series {VIX, VIX3M, VIX9D, VIX6M, VVIX, SKEW, MOVE, SPY, HY, EPU, PUTCALL, SHORTVOL} (date-indexed, a missing
    one absent or None); series_errors {name: reason}; fred {WALCL, WTREGEN, WRESBAL, RRPONTSYD}; spr; auctions; buybacks; gspc;
    vix_long; errors {source key: message}. Returns the snapshot's `chaos` block."""
    today = inp["today"]
    raw = {k: _clean(v) for k, v in (inp.get("series") or {}).items()}
    as_of = _trading_days(today, 1)[0]
    days = _trading_days(as_of, HISTORY_DAYS)
    comps, zday = _components(raw, inp.get("series_errors") or {}, today, days)
    out = {"version": CHAOS_VERSION, "model": MODEL, "components": comps, "components_total": len(SPECS),
           "components_missing": {k: c["reason"] for k, c in comps.items() if c["status"] != "fresh"},
           "election": _election(today), "source_errors": dict(inp.get("errors") or {})}
    fresh = {k: c for k, c in comps.items() if c["status"] == "fresh"}
    study = _event_study(inp.get("gspc"), inp.get("vix_long"))
    if not fresh or not zday.iloc[-1].notna().any():
        first = next(iter(out["components_missing"].items()), None)
        return {"status": "missing", "reason": "no component has 3 years of recent data (0 of 12 usable" + (f"; {first[0]}: {first[1]}" if first else "") + ")",
                **out, "as_of": None, "score": None, "mean_z": None, "regime": None, "prev": None, "regime_changed": False, "drivers": [],
                "components_used": 0, "history": [], "record": None, "levers": _levers(inp, today), "event_study": study,
                "lever_study": _lever_study(inp)}
    mean = zday.mean(axis=1, skipna=True)
    used = zday.notna().sum(axis=1)
    score = (SCALE * mean).clip(-100, 100)
    hist = [[d.isoformat(), round(float(sc), 1), int(u)] for d, sc, u in zip(days, score, used) if u]
    prev = {"date": hist[-2][0], "score": hist[-2][1], "regime": regime_of(hist[-2][1])} if len(hist) > 1 and hist[-1][0] == as_of.isoformat() else None
    sc, mz = float(score.iloc[-1]), float(mean.iloc[-1])
    regime = regime_of(sc)
    drivers = sorted(fresh.items(), key=lambda kv: -abs(kv[1]["z"]))[:3]
    scores = [h[1] for h in hist]
    hi, lo = max(hist, key=lambda h: h[1]), min(hist, key=lambda h: h[1])
    return {**out, "status": "fresh", "reason": None, "as_of": as_of.isoformat(), "score": round(sc, 1), "mean_z": round(mz, 2), "regime": regime,
            "prev": prev, "regime_changed": bool(prev and prev["regime"] != regime),
            "drivers": [{"key": k, "label": c["label"], "z": c["z"], "value": c["value"]} for k, c in drivers],
            "components_used": int(used.iloc[-1]), "history": hist,
            "record": {"high": {"date": hi[0], "score": hi[1]}, "low": {"date": lo[0], "score": lo[1]}, "days": len(scores)},
            "levers": _levers(inp, today), "event_study": study, "lever_study": _lever_study(inp)}
