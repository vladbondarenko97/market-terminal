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
STUDY_VERSION = "regime_study_v2"   # v2: days already above the start bound are not onsets or base days
WINDOW = "1096D"                  # 3 years of calendar days; a day-based window keeps the 7-days-a-week EPU series on 3 years too
MIN_PERIODS = 252
SPAN_DAYS = 3 * 365 - 30          # a series must span 3 years less a 30-day tolerance (FRED returns exactly 3 years for HY)
STALE_DAYS = 10
CLIP = 3.0
# ponytail: fixed, uncalibrated cut-offs. score = 50 * mean z (clipped to +-100); regimes split it at -30 / 30 / 60. Not fitted to any outcome.
SCALE = 50.0
REGIMES = ((-30, "peace"), (30, "neutral"), (60, "stress"))      # score <= bound -> name; above the last bound -> "chaos"
# ponytail: fixed episode rules, not fitted to any outcome: an episode starts on the 3rd consecutive day above 30 and ends on the 5th consecutive day below 15.
EPISODE_START_BOUND, EPISODE_START_DAYS = 30, 3
EPISODE_END_BOUND, EPISODE_END_DAYS = 15, 5
STUDY_MIN_COMPONENTS = 4          # study period: the days on which at least this many components counted
STUDY_MIN_DAYS = 500
HORIZONS = (20, 60, 120)
TRACK_TRADING_DAYS = 60
HISTORY_DAYS = 60
REVISE_TRADING_DAYS = 10          # a stored day may be rewritten only this close to the end of the frame: late components arrive within days,
                                  # while older days must not move when an input fetch fails or a 3-year window slides
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


def _event_study(gspc, vix, score=None):
    gspc, vix, score = _clean(gspc), _clean(vix), _clean(score)
    windows = {"-30..-1": (-31, -1), "0..+5": (-1, 5), "+6..+60": (5, 60), "0..+250": (-1, 250)}
    rows = []
    for kind, mod in (("midterm", 2), ("presidential", 0)):
        for name, ser, first in (("S&P 500", gspc, 1950), ("VIX", vix, 1990), ("Chaos / Peace score", score, 0)):
            if ser is None or (ser := ser[ser.index >= f"{max(first, 1900)}-01-01"]).empty:
                continue
            pts = name == "Chaos / Peace score"
            elect = []
            for y in range(max(first, ser.index[0].year), ser.index[-1].year + 1):
                if y % 4 == mod:
                    p = ser.index.searchsorted(pd.Timestamp(election_day(y)))
                    if p < len(ser) and (ser.index[p] - pd.Timestamp(election_day(y))).days < 7:
                        elect.append(ser.index[p])
            for wname, (a, b) in windows.items():
                ret = (ser.shift(-b) - ser.shift(-a)).dropna() if pts else (ser.shift(-b) / ser.shift(-a) - 1).dropna()
                sig = ret[ret.index.isin(elect)]
                g = grade(sig, ret[~ret.index.isin(elect)])
                sc = (lambda v: None if v is None else v / 100) if pts else (lambda v: v)     # grade() scales means by 100
                rows.append({"series": name, "window": wname, "n": g["n"], "mean_pct": sc(g["avg"]), "base_mean_pct": sc(g["bavg"]),
                             "hit_rate": g["p"], "base_hit_rate": g["base"], "t": g["t"], "grade": g["edge"], "kind": kind,
                             "unit": "pts" if pts else "pct"})
    if not rows:
        return {"status": "missing", "reason": "long ^GSPC / ^VIX history not available", "rows": []}
    nmax = lambda k: max([r["n"] for r in rows if r["kind"] == k] or [0])
    n, npres = nmax("midterm"), nmax("presidential")
    return {"status": "fresh", "rows": rows, "midterms": n, "presidentials": npres,
            "note": f"Descriptive. {n} midterm and {npres} presidential elections at most, so the best grade possible is 'thin'. Day 0 is the first "
                    "trading day on or after election day; windows are changes of the close in trading days (percent returns for prices, points "
                    "for the Chaos / Peace score). Only a higher mean than the other windows can earn a grade, "
                    "and neighbouring windows overlap, so the t-values are optimistic. It says nothing about why."}


event_study = _event_study


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


def _track_until(d):
    until, k = d, 0
    while k < TRACK_TRADING_DAYS:
        until += timedelta(days=1)
        k += MC.is_trading_day(until)
    return until


def _election(today):
    """The US general election whose tracking window (TRACK_TRADING_DAYS after it) has not ended yet."""
    d = next(election_day(y) for y in range(today.year - today.year % 2 - 2, today.year + 6, 2) if _track_until(election_day(y)) >= today)
    if today <= d:
        n = sum(1 for i in range(1, (d - today).days + 1) if MC.is_trading_day(today + timedelta(days=i)))
    else:
        n = -sum(1 for i in range(1, (today - d).days + 1) if MC.is_trading_day(d + timedelta(days=i)))
    until = _track_until(d)
    return {"date": d.isoformat(), "kind": "presidential" if d.year % 4 == 0 else "midterm", "trading_days_to": n,
            "track_until": until.isoformat(), "tracking": today <= until}


def election_markers(first_year, today):
    """Every general election day from first_year up to and including the next one after `today`."""
    out, y = [], first_year + first_year % 2
    while True:
        d = election_day(y)
        out.append({"date": d.isoformat(), "kind": "presidential" if y % 4 == 0 else "midterm"})
        if d > today:
            return out
        y += 2


def long_history(series):
    """The score on every day SPY traded: same formula as build(), each component counted only from SPAN_DAYS after its series begins
    and while its last observation is at most STALE_DAYS old. -> DataFrame[score, used, hot, inverted] indexed by date."""
    raw = {k: _clean(v) for k, v in (series or {}).items()}
    if raw.get("SPY") is None:
        return pd.DataFrame(columns=["score", "used", "hot", "inverted"])
    idx, tol = raw["SPY"].index, pd.Timedelta(days=STALE_DAYS)
    zs = {}
    for key, (needs, build_) in SPECS.items():
        if any(raw.get(n) is None for n in needs):
            continue
        s = build_(raw).dropna()
        if s.empty:
            continue
        z = _zscore(s).dropna().reindex(idx, method="ffill", tolerance=tol)
        zs[key] = z.where(idx >= s.index[0] + pd.Timedelta(days=SPAN_DAYS))
    z = pd.DataFrame(zs, index=idx)
    used = z.notna().sum(axis=1)
    inv = None
    if raw.get("VIX") is not None and raw.get("VIX3M") is not None:
        inv = (_ratio(raw["VIX"], raw["VIX3M"]) >= 1).astype(float).reindex(idx, method="ffill", tolerance=tol)
    out = pd.DataFrame({"score": (SCALE * z.mean(axis=1)).clip(-100, 100), "used": used, "hot": (z >= 1).sum(axis=1),
                        "inverted": pd.Series([None if inv is None or pd.isna(v) else bool(v) for v in (inv if inv is not None else idx)], index=idx, dtype=object)})
    return out[out["used"] > 0].astype({"used": int, "hot": int})


def _episode_spans(sc):
    """-> [(start_i, end_i or None)] by the fixed rules above."""
    spans, above, below, cur = [], 0, 0, None
    for i, v in enumerate(sc):
        if cur is None:
            above = above + 1 if v > EPISODE_START_BOUND else 0
            if above == EPISODE_START_DAYS:
                cur, below = i, 0
        else:
            below = below + 1 if v < EPISODE_END_BOUND else 0
            if below == EPISODE_END_DAYS:
                spans.append((cur, i))
                cur, above = None, 0
    if cur is not None:
        spans.append((cur, None))
    return spans


def _episode_rows(hist, spans):
    sc, last, rows = hist["score"].to_numpy(), len(hist) - 1, []
    for s, e in spans:
        stop = last if e is None else e
        pk = s + int(np.argmax(sc[s:stop + 1]))
        rows.append({"start": hist.index[s].date().isoformat(), "end": None if e is None else hist.index[e].date().isoformat(),
                     "days": stop - s + 1, "peak": round(float(sc[pk]), 1), "peak_date": hist.index[pk].date().isoformat(),
                     "peak_regime": regime_of(sc[pk]), "used": int(hist["used"].iloc[s])})
    return rows


def episodes(hist):
    return _episode_rows(hist, _episode_spans(hist["score"].to_numpy()))


SIGNALS = (
    ("momentum", "Score rose 15 points or more over 20 trading days"),
    ("near_bound", "Score between 15 and 30 (the upper half of neutral)"),
    ("breadth", "A third or more of the available components at z >= 1"),
    ("term_inversion", "VIX at or above VIX3M (front of the volatility curve inverted)"),
)


def regime_study(hist):
    h = hist[hist["used"] >= STUDY_MIN_COMPONENTS]
    n = len(h)
    if n < STUDY_MIN_DAYS:
        return {"status": "missing", "reason": f"under {STUDY_MIN_DAYS} days with {STUDY_MIN_COMPONENTS} or more components"}
    sc = h["score"]
    spans = _episode_spans(sc.to_numpy())
    in_ep = np.zeros(n, bool)
    starts = np.zeros(n)
    for s, e in spans:
        in_ep[s:(n if e is None else e + 1)] = True
        starts[s] = 1
    calm = ~in_ep
    # A day already above the start bound is not calm for the study: an episode starts on its 3rd day above the bound, so
    # counting days 1-2 as onsets (or in the base) scores near-certain hits. The card's "today" still reads `calm`.
    pre = calm & (sc <= EPISODE_START_BOUND).to_numpy()
    cs = np.concatenate([[0], np.cumsum(starts)])

    def outcome(hz):          # 1 when an episode starts within the next hz days; NaN where that is not known yet
        out = np.full(n, np.nan)
        i = np.arange(n - hz)
        out[i] = (cs[i + hz + 1] - cs[i + 1] > 0)
        return pd.Series(out, index=h.index)
    regime = sc.map(regime_of)
    base_rates = []
    for hz in HORIZONS:
        o = outcome(hz)
        row = {"horizon": hz}
        for name, m in (("all", pre), ("peace", pre & (regime == "peace").to_numpy()), ("neutral", pre & (regime == "neutral").to_numpy())):
            v = o[m].dropna()
            row[name] = {"p": round(float(v.mean()), 3) if len(v) else None, "n": len(v)}
        base_rates.append(row)
    chg = sc - sc.shift(20)
    conds = {"momentum": (chg >= 15).where(chg.notna()),
             "near_bound": ((sc >= EPISODE_END_BOUND) & (sc <= EPISODE_START_BOUND)).astype(float),
             "breadth": (h["hot"] / h["used"] >= 1 / 3).astype(float),
             "term_inversion": h["inverted"].map(lambda v: np.nan if pd.isna(v) else float(v)).astype(float)}
    isc = pd.Series(pre, index=h.index)
    signals = []
    for key, label in SIGNALS:
        c = conds[key].astype(float)
        onset = (c == 1) & (c.shift(1).rolling(20).max() == 0) & isc
        # a day already above the line is past the point the rates describe
        today = None if not pre[-1] or pd.isna(c.iloc[-1]) else bool(c.iloc[-1] == 1)
        on = (c == 1).to_numpy()
        k = 0
        while today and k < n and on[n - 1 - k]:
            k += 1
        sg = {"key": key, "label": label, "definition": label, "active": today, "days_on": None if today is None else k,
              "since": h.index[n - k].date().isoformat() if k else None}
        for hz in HORIZONS[:2]:
            o = outcome(hz)
            ok = o.notna() & isc & c.notna()
            g = grade(o[ok & onset], o[ok])
            sg[f"h{hz}"] = {"n": g["n"], "hit": g["p"], "base": g["base"], "t": g["t"], "grade": g["edge"]}
        signals.append(sg)
    ends = [e for _, e in spans if e is not None]
    gaps = [spans[i + 1][0] - spans[i][1] - 1 for i in range(len(spans) - 1)]
    med = lambda x: float(np.median(x)) if len(x) else None
    ep_in = bool(in_ep[-1])
    si = spans[-1][0] if ep_in else (min(spans[-1][1] + 1, n - 1) if spans else 0)
    now = float(sc.iloc[-1])
    state = {"in_episode": ep_in, "since": h.index[si].date().isoformat(), "days": n - si, "score": round(now, 1), "regime": regime_of(now),
             "change_20d": round(now - float(sc.iloc[-21]), 1) if n > 20 else None, "to_stress": None if ep_in else round(EPISODE_START_BOUND - now, 1)}
    return {"status": "fresh", "reason": None, "version": STUDY_VERSION,
            "period": {"first": h.index[0].date().isoformat(), "last": h.index[-1].date().isoformat(), "days": n, "min_components": STUDY_MIN_COMPONENTS},
            "state": state,
            "durations": {"episodes": len(ends), "median_episode_days": med([e - s + 1 for s, e in spans if e is not None]),
                          "median_calm_days": med(gaps)},
            "base_rates": base_rates, "signals": signals,
            "note": "Descriptive counts over one history. The conditions were fixed in code on 2026-10-10 and not tuned since, neighbouring days overlap "
                    "so a t-value is optimistic, and none of it says why an episode began. A score that is already near the line, or already has "
                    "several components elevated, needs less to cross it, so part of those two hit rates is arithmetic. A hit rate of 50% "
                    "also means the other half were false alarms. The rates describe the first day of a stretch, so a "
                    "condition that has been on for weeks is past the point they measure."}


def long_block(hist, today, rec, rec_reason):
    """The snapshot's `long` block from a long_history-shaped frame (fresh, or read back from the lake)."""
    eras = hist["used"].cummax()
    eras = [{"from": d.date().isoformat(), "used": int(k)} for d, k in eras[eras.diff().fillna(1) > 0].items()]
    return {"status": "fresh", "reason": None, "first": hist.index[0].date().isoformat(), "last": hist.index[-1].date().isoformat(),
            "days": len(hist), "eras": eras, "episodes": episodes(hist), "study": regime_study(hist),
            "markers": {"elections": election_markers(hist.index[0].year, today), "recessions": rec,
                        "recessions_reason": None if rec else rec_reason or "recession ranges not available"}}


def study_score(hist):
    return hist["score"][hist["used"] >= STUDY_MIN_COMPONENTS]


def _long_block(inp, today):
    """-> (block, frame, study-period score). Never raises: this block must not take the rest of the chaos block down."""
    L = inp.get("long")
    errs = (L or {}).get("errors") or {}
    if not L or not L.get("series"):
        return {"status": "missing", "reason": errs.get("long") or "no long history inputs"}, None, None
    try:
        hist = long_history(L.get("series"))
        if hist.empty:
            return {"status": "missing", "reason": "SPY history not available: " + str(errs.get("yahoo:SPY:max") or "no data")}, None, None
        return long_block(hist, today, L.get("recessions"), errs.get("fred:USREC")), hist, study_score(hist)
    except Exception as e:
        return {"status": "missing", "reason": f"{type(e).__name__}: {e}"}, None, None


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
    lg, frame, lscore = _long_block(inp, today)
    out["long"], out["_long_frame"] = lg, frame
    study = _event_study(inp.get("gspc"), inp.get("vix_long"), lscore)
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
