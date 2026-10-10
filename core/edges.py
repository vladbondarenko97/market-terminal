"""Edge lab: the published market edges, each tested on one ticker's own daily history and read off today's state.

Every edge answers two questions for a symbol: is it on right now, and did it actually pay on this symbol? The second
is a plain comparison of forward returns (signal days against normal days, or one state against the other) with the
same screen for all of them: a t-statistic of 2 or more, the same sign in both halves of the history and, for
multi-day holds, 5 points of up-rate. Only an edge that passes can drive a horizon verdict; the rest are context.

Edges: short-term reversal, post-earnings drift, turn of the month (short horizon); trend, 12-month momentum,
volatility premium (mid); overnight versus intraday (long). Network: Yahoo only. Price history comes from
watch.fetch_bars (one download per ticker per day, shared through a file); earnings dates and fund info are cached
per day in memory, quotes for a minute.
"""
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd

from config import DATA_DIR
from core import watch
from core.watch import SIZE as _SIZE, grade as _grade

TRACKED = DATA_DIR / "edges_tracked.json"
DEFAULT_TRACKED = ["SPY", "INTC", "SLV", "GOOGL"]
MAX_TRACKED = 20
MONTH = 21                                           # trading days in the mid-horizon hold
DRIFT_DAYS, DRIFT_MOVE = 20, 0.02                    # post-earnings drift: hold, and the reaction that counts as strong
VOL_INDEX = {"SPY": "^VIX", "QQQ": "^VXN", "DIA": "^VXD", "GLD": "^GVZ", "USO": "^OVX"}      # implied-vol history, where one exists


# ------------------------------------------------------------------ data (once per session day)
@lru_cache(maxsize=64)
def _earnings(symbol, day):
    """Past earnings announcements as naive New York timestamps (oldest first)."""
    import yfinance as yf
    idx = yf.Ticker(symbol).get_earnings_dates(limit=100).index          # a failure raises, so it is not cached
    return sorted(t.tz_convert(watch.NEW_YORK).tz_localize(None) for t in idx if t.date() < day)


@lru_cache(maxsize=64)
def _info(symbol, day):
    import yfinance as yf
    i = yf.Ticker(symbol).info or {}
    if not i.get("quoteType"):
        raise ValueError("no fund or company info")                     # raised, so an empty answer is not cached
    return {k: i.get(k) for k in ("shortName", "quoteType")}


@lru_cache(maxsize=16)
def _vol_index(symbol, day):
    import yfinance as yf
    v = yf.Ticker(VOL_INDEX[symbol]).history(period="max")["Close"].dropna()
    v.index = v.index.tz_localize(None).normalize()
    return v / 100


_LIVE = {}                                           # symbol -> (when, (price, iv)): quotes are reused for a minute


def _live(symbol):
    """(price, 30-day at-the-money implied volatility or None)."""
    import yfinance as yf
    hit = _LIVE.get(symbol)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    t = yf.Ticker(symbol)
    price, iv = float(t.fast_info["last_price"]), None
    try:
        exp = min(t.options, key=lambda e: abs((date.fromisoformat(e) - date.today()).days - 30))
        ch = t.option_chain(exp)
        ivs = [float(df.iloc[(df["strike"] - price).abs().argsort()[:1]]["impliedVolatility"].iloc[0]) for df in (ch.calls, ch.puts)]
        ivs = [x for x in ivs if 0.03 < x < 3]                       # Yahoo reports ~0 when a leg has no quote
        iv = sum(ivs) / len(ivs) if ivs else None
    except Exception:
        pass
    _LIVE[symbol] = (time.monotonic(), (price, iv))
    return price, iv


def refresh():
    for f in (_earnings, _info, _vol_index):
        f.cache_clear()
    _LIVE.clear()
    watch.refresh()


# ------------------------------------------------------------------ evidence
def _ev(g, after, otherwise="normally"):
    if g["p"] is None:
        return f"too few cases to test (n={g['n']})"
    return f"up {g['p']:.0%} {after} vs {g['base']:.0%} {otherwise}, avg {g['avg']:+.2f}% vs {g['bavg']:+.2f}%, t {g['t']:.1f}, n={g['n']}"


def _edge(key, label, name, horizon, source, what, now, trigger, active, bias, grade, evidence, plan, note=None):
    """One row of the card. A tested directional edge gives its plan; anything else that is on says why it is only context."""
    tradeable = active and grade in _SIZE and bias in ("bullish", "bearish")
    action = "Wait" if not active else f"{plan} · {_SIZE[grade]}" if tradeable else note or \
        ("Context: on, but it has no measured edge on this ticker" if grade == "none" else plan)
    return {"key": key, "label": label, "name": name, "horizon": horizon, "source": source, "what": what, "now": now,
            "trigger": trigger, "active": bool(active), "bias": bias if active else None, "edge": grade, "evidence": evidence,
            "action": action, "plan": plan}


# ------------------------------------------------------------------ the edges
def _reversal(sym, c, price):
    r = watch.dip_signal(c, price, True, False, sym)
    return _edge("reversal", "Reversal", "Short-term reversal (dip in an uptrend)", "short", "Lehmann 1990; Jegadeesh 1990; Nagel 2012",
                 r["what"], r["now"], r["trigger"].replace(" between 2:30 and 3:00 PM CT", " near the close"), r["fired"], "bullish",
                 r["edge"], r["evidence"], r["plan"])


def _earnings_drift(sym, c, announcements):
    if announcements is None:
        return _edge("earnings", "Earnings", "Post-earnings drift", "short", "Bernard & Thomas 1989",
                     "After a strong earnings reaction a stock tends to keep drifting the same way for weeks.",
                     "earnings data unavailable right now", "not checked", False, None, "n/a",
                     "Yahoo did not return this ticker's company info or earnings dates; try REFRESH", "Not checked")
    if not announcements:
        return _edge("earnings", "Earnings", "Post-earnings drift", "short", "Bernard & Thomas 1989",
                     "After a strong earnings reaction a stock tends to keep drifting the same way for weeks.",
                     "no earnings: this is a fund", "not applicable", False, None, "n/a", "funds do not report earnings", "Not applicable")
    events = []
    for t in announcements:                                           # reaction = first session that closes after the news
        i = c.index.searchsorted(t.normalize() + timedelta(days=t.hour >= 16))
        if 0 < i < len(c):
            events.append((i, c.iloc[i] / c.iloc[i - 1] - 1))
    fwd = c.shift(-DRIFT_DAYS) / c - 1
    g = _grade(fwd.iloc[[i for i, r in events if r >= DRIFT_MOVE]], fwd.iloc[::DRIFT_DAYS])
    last_i, last_r = events[-1] if events else (None, 0)
    age = len(c) - 1 - last_i if events else None
    on = events and last_r >= DRIFT_MOVE and age < DRIFT_DAYS
    return _edge("earnings", "Earnings", "Post-earnings drift", "short", "Bernard & Thomas 1989",
                 "After a strong earnings reaction a stock tends to keep drifting the same way for weeks.",
                 f"last report {c.index[last_i]:%b %-d}: {last_r * 100:+.1f}% reaction, {age} trading days ago" if events else "no report on record",
                 f"within {DRIFT_DAYS} trading days of a report that lifted the stock {DRIFT_MOVE:.0%} or more", on, "bullish", g["edge"],
                 f"after a +{DRIFT_MOVE:.0%} reaction: " + _ev(g, f"{DRIFT_DAYS} days later"),
                 f"Buy shares after an up reaction; hold until {DRIFT_DAYS} trading days after the report")


def _turn_of_month(sym, c, today):
    r = c.pct_change().dropna()
    month = pd.Series(r.index.month, index=r.index)
    last = month != month.shift(-1)                                   # last trading day of each month
    window = last | last.shift(1, fill_value=False) | last.shift(2, fill_value=False) | last.shift(3, fill_value=False)
    window.iloc[-1] = False                                           # the newest bar is not known to be a month end
    g = _grade(r[window], r[~window], min_up=0.0)
    sessions = [d for d in (today.replace(day=1) + timedelta(days=k) for k in range(31)) if d.month == today.month and watch.is_trading_day(d)]
    eom = sessions[-1]
    on = today >= eom or today in sessions[:3]
    return _edge("tom", "Month turn", "Turn of the month", "short", "Lakonishok & Smidt 1988; McConnell & Xu 2008",
                 "The last trading day of a month and the first three of the next, when pension and payroll money arrives.",
                 "inside the window now" if on else f"next window opens {eom:%b %-d}",
                 "last trading day of the month through the third of the next", on, "bullish", g["edge"],
                 _ev(g, "of window days", "of other days"), "Be long through the window; do not open shorts into it")


def _state(c, mask, key, label, name, source, what, now, trigger, plan_up, plan_down):
    """A state that is always either up or down (trend, momentum): the month after 'up' against the month after 'down'."""
    fwd = (c.shift(-MONTH) / c - 1).iloc[::MONTH]
    m = mask.reindex(fwd.index)
    g = _grade(fwd[m == True], fwd[m == False])                       # noqa: E712  (NaN = not yet defined, in neither set)
    up = bool(mask.iloc[-1])
    return _edge(key, label, name, "mid", source, what, now, trigger, True, "bullish" if up else "bearish", g["edge"],
                 _ev(g, "a month later when it is on", "when it is off"), plan_up if up else plan_down)


def _trend(sym, c, price):
    sma = c.rolling(200).mean()
    level = (float(c.iloc[-199:].sum()) + price) / 200
    mask = (c > sma).where(sma.notna())
    mask.iloc[-1] = price > level
    return _state(c, mask, "trend", "Trend", "Trend (200-day average)", "Moskowitz, Ooi & Pedersen 2012; Faber 2007",
                  "Whether the price is above its 200-day average: the simplest trend filter.",
                  f"{(price / level - 1) * 100:+.1f}% vs the 200-day {level:.2f}", f"{sym} above {level:.2f}",
                  "Hold or buy shares while above the 200-day; review monthly", "Stand aside or hold less while below the 200-day")


def _momentum(sym, c):
    mom = c.shift(MONTH) / c.shift(252) - 1
    return _state(c, (mom > 0).where(mom.notna()), "momentum", "Momentum", "12-month momentum (skipping the last month)",
                  "Jegadeesh & Titman 1993", "The return from 12 months ago to 1 month ago. Winners tend to keep winning for a while.",
                  f"{mom.iloc[-1] * 100:+.0f}% over months 12 to 2", "12-to-1-month return above zero",
                  "Hold or buy shares while momentum is positive; review monthly", "Stand aside while momentum is negative")


def _vol_premium(sym, c, iv, day):
    r = np.log(c / c.shift(1))
    rv = float(r.iloc[-20:].std() * math.sqrt(252))
    grade, evidence = "n/a", "not testable here: no implied-volatility history for this ticker"
    if sym in VOL_INDEX:
        try:
            implied = _vol_index(sym, day)
            later = (r.rolling(MONTH).std() * math.sqrt(252)).shift(-MONTH)             # what the next month actually delivered
            gap = (implied.reindex(c.index) - later).dropna().iloc[::MONTH]
            t = float(gap.mean() / (gap.std() / math.sqrt(len(gap))))
            grade = "tested" if t >= 2 and len(gap) >= 30 else "none"
            evidence = (f"implied was above what followed {float((gap > 0).mean()):.0%} of months, by {gap.mean() * 100:.1f} vol points "
                        f"on average, t {t:.1f}, n={len(gap)} ({VOL_INDEX[sym]})")
        except Exception:
            pass
    if not iv:
        return _edge("volatility", "Vol premium", "Volatility premium", "mid", "Carr & Wu 2009",
                     "Options usually cost more than the moves that follow, which pays sellers and charges buyers.",
                     f"realised {rv:.0%}; no option quotes right now", "implied ÷ realised above 1.15 (rich) or below 0.90 (cheap)",
                     False, None, grade, evidence, "Use spreads when options are rich, outright options when cheap")
    ratio = iv / rv if rv else 0
    rich, cheap = ratio > 1.15, ratio < 0.9
    note = ("Options are rich: use spreads or shares rather than outright calls" + ("; selling a defined-risk put spread is the paid side" if grade == "tested" else "")
            if rich else "Options are cheap: an outright call or put costs less than usual")
    return _edge("volatility", "Vol premium", "Volatility premium", "mid", "Carr & Wu 2009",
                 "Options usually cost more than the moves that follow, which pays sellers and charges buyers.",
                 f"implied {iv:.0%} vs realised {rv:.0%} (ratio {ratio:.2f})", "implied ÷ realised above 1.15 (rich) or below 0.90 (cheap)",
                 rich or cheap, "neutral", grade, evidence, "Use spreads when options are rich, outright options when cheap", note)


def _overnight(sym, b):
    b = b[b.index >= "2005-01-01"]
    night, dayret = (b["Open"] / b["Close"].shift(1) - 1).dropna(), (b["Close"] / b["Open"] - 1).iloc[1:]
    g = _grade(night, dayret, min_up=0.0)
    better = g["avg"] is not None and g["avg"] > g["bavg"]
    return _edge("overnight", "Overnight", "Overnight versus intraday", "long", "Lou, Polk & Skouras 2019",
                 "How much of the return arrives between the close and the next open rather than during the session.",
                 f"overnight {night.mean() * 25200:+.1f}% a year vs intraday {dayret.mean() * 25200:+.1f}%", "structural: always on", True,
                 "neutral", g["edge"], _ev(g, "of nights", "of sessions"),
                 "Hold positions overnight rather than day trade",
                 "Hold positions overnight; day trading this ticker gives up most of its return" if g["edge"] in _SIZE and better
                 else "No reliable gap between overnight and intraday on this ticker")


# ------------------------------------------------------------------ one ticker
def _verdict(edges):
    out = {}
    for h in ("short", "mid", "long"):
        live = [e for e in edges if e["horizon"] == h and e["active"] and e["edge"] in _SIZE]
        bull, bear = [e for e in live if e["bias"] == "bullish"], [e for e in live if e["bias"] == "bearish"]
        bias = "bullish" if len(bull) > len(bear) else "bearish" if len(bear) > len(bull) else "neutral"
        hints = [("Hold, don't day trade" if e["action"].startswith("Hold") else None) if e["key"] == "overnight" else
                 ("Options rich: use spreads" if "rich" in e["action"] else "Options cheap") if e["key"] == "volatility" else None for e in live]
        label = (f"{bias.capitalize()} ({len(bull or bear)} tested)" if bias != "neutral" else "Mixed" if bull else
                 next((h_ for h_ in hints if h_), "No tested edge on"))
        out[h] = {"score": len(bull) - len(bear), "bias": bias, "label": label,
                  "edges": [e["name"] for e in live if e["bias"] != "neutral" or label != "No tested edge on"]}
    return out


def analyze(symbol):
    """Every edge for one ticker. Raises ValueError when Yahoo has no usable history for it."""
    sym, day = str(symbol or "").strip().upper(), watch.session_day()
    if not watch._SYMBOL.fullmatch(sym):
        raise ValueError("not a valid ticker")
    try:
        b = watch.fetch_bars(sym)
        price, iv = _live(sym)
    except Exception:
        raise ValueError(f"{sym}: no price history found")
    if len(b) < 300:
        raise ValueError(f"{sym}: less than about a year of price history")
    c, today = b["Close"], day                                   # after the close everything reads as the next session
    while not watch.is_trading_day(today):                           # a weekend or holiday reads as the session that follows it
        today += timedelta(days=1)

    def soft(fetch, *args):                                          # optional inputs: a failure means "unknown", not "none"
        try:
            return fetch(*args)
        except Exception:
            return None
    info = soft(_info, sym, day) or {}
    kind = info.get("quoteType")
    announcements = soft(_earnings, sym, day) if kind == "EQUITY" else None if kind is None else []
    edges = [_reversal(sym, c, price), _earnings_drift(sym, c, announcements), _turn_of_month(sym, c, today),
             _trend(sym, c, price), _momentum(sym, c), _vol_premium(sym, c, iv, day), _overnight(sym, b)]
    v = _verdict(edges)
    v["summary"] = f"{sym}: " + "; ".join(
        f"{h} term {v[h]['label'].lower()}" + (f" ({', '.join(v[h]['edges'])})" if v[h]["edges"] else "") for h in ("short", "mid", "long")) + "."
    return {"symbol": sym, "name": info.get("shortName") or sym, "quote_type": info.get("quoteType"), "price": price,
            "history_since": int(c.index[0].year), "verdict": v, "edges": edges}


# ------------------------------------------------------------------ tracked list + card payload
def tracked():
    try:
        return json.loads(TRACKED.read_text())
    except (OSError, ValueError):
        return list(DEFAULT_TRACKED)


def _save(symbols):
    tmp = TRACKED.with_suffix(".tmp")
    tmp.write_text(json.dumps(symbols))
    tmp.replace(TRACKED)


def add_tracked(symbol):
    """Returns an error message, or None when the ticker was analysed and saved."""
    sym, have = str(symbol or "").strip().upper(), tracked()
    if sym in have:
        return f"{sym} is already tracked"
    if len(have) >= MAX_TRACKED:
        return f"the list is full ({MAX_TRACKED} symbols); remove one first"
    try:
        analyze(sym)
    except ValueError as e:
        return str(e)
    _save(have + [sym])
    return None


def remove_tracked(symbol):
    have = tracked()
    if str(symbol).upper() in have:
        _save([s for s in have if s != str(symbol).upper()])


def payload(query=None, fresh=False):
    """What the card shows: every tracked ticker, plus one queried ticker that is not saved."""
    if fresh:
        refresh()

    def safe(sym):
        try:
            return analyze(sym)
        except Exception:
            return None
    symbols = tracked()
    with ThreadPoolExecutor(max_workers=6) as pool:                    # a cold ticker is ~6 Yahoo requests
        done = list(pool.map(safe, symbols))
    out = {"status": "success", "as_of": datetime.now().isoformat(timespec="seconds"), "symbols": symbols,
           "failed": [s for s, a in zip(symbols, done) if a is None], "max_symbols": MAX_TRACKED, "tracked": [a for a in done if a]}
    if query:
        out["query"] = analyze(query)                                 # ValueError carries the message for the card
    return out
