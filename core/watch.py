"""Live watch: a rule-based SPY setup judged on live quotes, and the paper positions it opens and closes.

Rule ("dip in an uptrend"): 2-day RSI under 10 (SPY) or 5 (GOOGL) with the price above its 200-day average, judged in the last 30 minutes of
the session (the rule is tested on closing prices). Structure: a narrow call debit spread around spot, ~25 DTE, sold
after 5 trading days. Everything here is plain arithmetic on closes and quotes; the only network calls are fetch_bars and the earnings date.
"""
import json
import math
import re
from datetime import date, datetime, time, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd

from config import DATA_DIR
from core import lake
from core.forecast import _SIZE as SIZE, _signal_row   # noqa: F401  (SIZE is shared with core/edges.py)
from core.market_calendar import NEW_YORK, is_trading_day, session_close
from core.positions import _mid

RULE = "spy_dip"
RSI_ENTRY = 10            # 2-day RSI below this (SPY)
# Researched symbols: the RSI(2) level that tested positive in every sub-period since 2005, and how to trade it.
# GOOGL needs a deeper washout, and its option bid/ask eats most of a narrow spread's edge, so it trades as shares.
# Any other symbol on the watchlist trades as shares at whichever of 10 / 5 tested better on its own history.
SPECS = {"SPY": {"rsi": RSI_ENTRY, "trade": "spread"}, "GOOGL": {"rsi": 5, "trade": "shares"}}
WATCHLIST = DATA_DIR / "scanner_watchlist.json"          # symbols added from the card: {symbol: {"earnings": bool}}
DEFAULTS = {"GOOGL": {"earnings": True}, "SLV": {"earnings": False}}
MAX_SYMBOLS = 20                                          # every symbol costs a few Yahoo requests per 5-minute check
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}")
MAX_DEBIT = 3.00          # budget: at most $300 per spread
HOLD_DAYS = 5             # trading days until the exit alert
HALF_WIDTH = 0.003        # long strike ~0.3% under spot, short strike ~0.3% over
DTE_MIN, DTE_MAX, DTE_TARGET = 18, 35, 25
ENTRY_MINUTES = 30        # the rule is judged in the last minutes of the session
# When the alert job checks, Eastern: every 15 minutes from 09:35 to 15:50, so the entry window gets 15:35 and 15:50
# (12:35 and 12:50 on an early close). setup.sh turns these into weekday launchd times; nothing fires on a weekend.
ALERT_SLOTS_ET = tuple((h, m) for h in range(9, 16) for m in (5, 20, 35, 50) if (h, m) >= (9, 35))
CACHE = DATA_DIR / "cache"
OPEN_ET = time(9, 30)     # the regular session's open (its close comes from market_calendar.session_close)


def session_day(now=None):
    """The date of the session the live price belongs to: today until the 4 PM ET close, then the next day, so that
    after the close today's bar counts as finished and the triggers shown are the next session's."""
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    return now.date() + timedelta(days=now.time() >= session_close(now.date()))


@lru_cache(maxsize=64)
def _bars_on(symbol, day):
    path = CACHE / f"bars_{symbol}_{day}.csv"
    if path.exists():
        return pd.read_csv(path, index_col=0, parse_dates=True)
    import yfinance as yf
    b = yf.Ticker(symbol).history(period="max", auto_adjust=True)[["Open", "High", "Low", "Close"]].dropna()
    b.index = b.index.tz_localize(None).normalize()
    b = b[b.index.date < day]
    if len(b) < 2:
        raise ValueError(f"{symbol}: no price history")              # raised, so an empty answer is cached nowhere
    CACHE.mkdir(exist_ok=True)
    for old in CACHE.glob(f"bars_{symbol}_*.csv"):
        old.unlink()
    tmp = path.with_suffix(".tmp")
    b.to_csv(tmp)
    tmp.replace(path)
    return b


def fetch_bars(symbol="SPY"):
    """Daily bars (adjusted open, high, low, close) of finished sessions, oldest first. Downloaded once per ticker
    per session day and kept in a file in the data folder, so the terminal, the Edge Lab and every run of the alert
    job share one download: finished sessions do not change."""
    return _bars_on(symbol, session_day())


def fetch_closes(symbol="SPY"):
    return fetch_bars(symbol)["Close"]


@lru_cache(maxsize=64)
def _earnings_on(symbol, day):
    """The next earnings date on or after `day`. Raises when Yahoo fails or lists none, so a failure is never cached."""
    import yfinance as yf
    return min(d for d in yf.Ticker(symbol).calendar.get("Earnings Date") or [] if d >= day)


def fetch_blackout(symbol, today=None):
    """Why a new 5-day position in `symbol` is blocked today (earnings inside the hold), or None."""
    today = today or date.today()
    if not _extras().get(symbol, {}).get("earnings"):
        return None                                 # ETFs: no earnings to step around
    try:
        nxt = _earnings_on(symbol, today)
    except Exception:
        return "its earnings date is unavailable right now"       # unknown counts as blocked, never as clear
    return f"earnings {nxt:%b %-d} fall inside the {HOLD_DAYS}-day hold" if nxt <= exit_date(today) else None


def refresh():
    """Drop the day's caches (memory and files) so the next read goes back to Yahoo."""
    _bars_on.cache_clear()
    _earnings_on.cache_clear()
    for old in CACHE.glob("bars_*.csv"):
        old.unlink()


def _extras():
    try:
        return json.loads(WATCHLIST.read_text())
    except (OSError, ValueError):
        return dict(DEFAULTS)


def _save(extras):
    tmp = WATCHLIST.with_suffix(".tmp")
    tmp.write_text(json.dumps(extras, indent=1))
    tmp.replace(WATCHLIST)


def watchlist():
    return ["SPY"] + [s for s in _extras() if s != "SPY"]


def add_symbol(symbol):
    """Check the ticker against Yahoo and save it. Returns an error message, or None when it was added."""
    import yfinance as yf
    sym, extras = str(symbol or "").strip().upper(), _extras()
    if not _SYMBOL.fullmatch(sym):
        return "not a valid ticker"
    if sym == "SPY" or sym in extras:
        return f"{sym} is already on the list"
    if len(extras) >= MAX_SYMBOLS:
        return f"the list is full ({MAX_SYMBOLS} symbols); remove one first"
    try:
        if len(fetch_closes(sym)) < 260:
            return f"{sym}: less than a year of price history"
        equity = yf.Ticker(sym).fast_info["quoteType"] == "EQUITY"
    except Exception:
        return f"{sym}: no price history found"
    _save({**extras, sym: {"earnings": equity}})
    return None


def remove_symbol(symbol):
    extras = _extras()
    if extras.pop(str(symbol).upper(), None) is not None:
        _save(extras)


def in_session(now=None):
    """Inside the regular NYSE session (which ends at 13:00 ET on an early close)."""
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    return is_trading_day(now.date()) and OPEN_ET <= now.time() <= session_close(now.date())


def in_entry_window(now=None):
    """The last ENTRY_MINUTES of the session, where the close-based rule is judged: 15:30-16:00 ET, or 12:30-13:00
    on an early close."""
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    if not is_trading_day(now.date()):
        return False
    close = datetime.combine(now.date(), session_close(now.date()), NEW_YORK)
    return close - timedelta(minutes=ENTRY_MINUTES) <= now <= close


def exit_date(opened, hold=HOLD_DAYS):
    d = opened
    while hold:
        d += timedelta(days=1)
        hold -= is_trading_day(d)
    return d


def _history(closes):
    """Daily series the rules share: closes, Wilder RSI(2) averages, 200-day average, forward 5-day return."""
    c = closes.dropna().astype(float)
    d = c.diff()
    gain, loss = d.clip(lower=0).ewm(alpha=0.5, adjust=False).mean(), (-d.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    return c, gain, loss, 100 * gain / (gain + loss), c.rolling(200).mean(), c.shift(-HOLD_DAYS) / c - 1


def grade(sig, base, min_up=0.05):
    """The one evidence gate for the scanner and the Edge Lab: signal forward returns against baseline ones (both
    date-indexed Series). Returns the stats and a grade: 'tested', 'thin' (under 30 cases) or 'none'."""
    sig, base = sig.dropna(), base.dropna()
    out = {"n": len(sig), "p": None, "base": None, "avg": None, "bavg": None, "t": None, "edge": "none"}
    if len(sig) < 8 or len(base) < 8 or not sig.std():
        return out
    half = base.index[len(base) // 2]
    steady = all(sig[k(sig)].mean() > base[k(base)].mean() for k in (lambda x: x.index < half, lambda x: x.index >= half) if k(sig).any())
    se = math.sqrt(sig.var() / len(sig) + base.var() / len(base))
    out.update(p=float((sig > 0).mean()), base=float((base > 0).mean()), avg=float(sig.mean() * 100), bavg=float(base.mean() * 100),
               t=float((sig.mean() - base.mean()) / se) if se else 0.0)
    # ponytail: fixed cut-offs (t >= 2, better than baseline in both halves of the history, min_up of up-rate). A screen,
    # not a significance proof: many rules on many tickers produce some false passes.
    if out["t"] >= 2 and steady and out["p"] - out["base"] >= min_up:
        out["edge"] = "tested" if len(sig) >= 30 else "thin"
    return out


def _stats(mask, c, sma, fwd):
    """grade() of the 5-day forward return after non-overlapping past signals, against every uptrend day."""
    up = (c > sma) & fwd.notna()
    picks, last = [], -HOLD_DAYS
    for i in np.flatnonzero((mask & up).values):
        if i - last >= HOLD_DAYS:
            picks.append(i)
            last = i
    return {**grade(fwd.iloc[picks], fwd[up]), "since": c.index[0].year}


def _evidence(symbol, st):
    return f"too few past cases to test (n={st['n']})" if st["p"] is None else (
        f"{symbol} up {st['p']:.0%} vs {st['base']:.0%} normally {HOLD_DAYS} days later, avg {st['avg']:+.2f}% vs {st['bavg']:+.2f}%, "
        f"t {st['t']:.1f}, n={st['n']} since {st['since']}")


def _rsi_level(prev, g0, l0, target):
    """Today's price at which Wilder RSI(2) equals `target`, given yesterday's close and averages."""
    k = (100 - target) / target                                           # RSI < target  <=>  loss > k x gain
    return prev + (l0 / k - g0 if l0 >= k * g0 else l0 - k * g0)


def dip_signal(closes, live, in_window=True, has_open=False, symbol="SPY", blocked=None):
    """Scanner row for the dip rule, or None without enough history. closes: finished daily sessions (oldest first);
    live: the current price, treated as today's provisional close; blocked: a reason not to open (earnings)."""
    c, gain, loss, rsi, sma, fwd = _history(closes)
    if len(c) < 260 or not live:
        return None
    if symbol in SPECS:
        entry, trade = SPECS[symbol]["rsi"], SPECS[symbol]["trade"]
        st = _stats(rsi < entry, c, sma, fwd)
    else:                                                                # added from the card: 10 or 5, whichever tested better
        trade = "shares"
        cands = [(_stats(rsi < th, c, sma, fwd), th) for th in (10, 5)]
        st, entry = next((x for x in cands if x[0]["edge"] == "tested"), max(cands, key=lambda x: x[0]["t"] or -math.inf))   # 10 if it passes
    # today's bar with the live price: Wilder RSI(2) is one more step of the same averages
    g0, l0, prev = float(gain.iloc[-1]), float(loss.iloc[-1]), float(c.iloc[-1])
    g1, l1 = 0.5 * g0 + 0.5 * max(live - prev, 0), 0.5 * l0 + 0.5 * max(prev - live, 0)
    rsi_now = 100 * g1 / (g1 + l1) if g1 + l1 else 50.0
    sma_now = (float(c.iloc[-199:].sum()) + live) / 200
    level = _rsi_level(prev, g0, l0, entry)
    setup = rsi_now < entry and live > sma_now
    lo, hi = round(live * (1 - HALF_WIDTH)), round(live * (1 + HALF_WIDTH))
    action = (f"Buy a {symbol} call spread ~{DTE_TARGET} DTE: long the {lo} call, short the {hi} call; risk only the debit "
              f"(at most ${MAX_DEBIT * 100:.0f}); sell after {HOLD_DAYS} trading days") if trade == "spread" else \
             (f"Buy {symbol} shares (fractional is fine); sell after {HOLD_DAYS} trading days. Single-stock option bid/ask eats "
              f"most of a narrow call spread's edge, so shares are the tested way in")
    row = _signal_row(
        symbol, "Dip in an uptrend", "Day scanner · daily closes + live quote (core/watch.py)",
        f"2-day RSI under {entry} while {symbol} is above its 200-day average: a short-term washout inside a long-term "
        "uptrend. It is judged in the last 30 minutes of the session because the rule is tested on closing prices.",
        f"RSI(2) {rsi_now:.0f} · {symbol} {live:.2f}, {'above' if live > sma_now else 'below'} the 200-day {sma_now:.2f}"
        + (" · position open" if has_open else f" · blocked: {blocked}" if blocked and setup else
           " · setup forming, confirms 2:30–3:00 PM CT" if setup and not in_window else ""),
        f"{symbol} ≤ {level:.2f} ({(level / live - 1) * 100:+.1f}%) between 2:30 and 3:00 PM CT, above the 200-day average",
        setup and in_window and not has_open and not blocked, "bullish", action,
        _evidence(symbol, st), st["edge"])
    row["rsi2"], row["trigger_price"], row["rsi_entry"] = rsi_now, level, entry
    return row


def pick_expiry(expirations, today=None):
    """The listed expiry closest to DTE_TARGET inside [DTE_MIN, DTE_MAX], or None."""
    today = today or date.today()
    ok = [(abs((date.fromisoformat(e) - today).days - DTE_TARGET), e) for e in expirations
          if DTE_MIN <= (date.fromisoformat(e) - today).days <= DTE_MAX]
    return min(ok)[1] if ok else None


def _leg_mid(calls, contract):
    hit = calls[calls["contractSymbol"] == contract]
    if not len(hit):
        return None
    h = hit.iloc[0]
    return _mid(*(float(h[k]) if h[k] == h[k] else None for k in ("bid", "ask", "lastPrice")))


def pick_spread(calls, spot, max_debit=MAX_DEBIT):
    """The widest pair of strikes around spot inside +-HALF_WIDTH whose debit fits the budget (else the narrowest pair):
    long the lower strike, short the higher one."""
    strikes = sorted(calls["strike"])
    lo = max((s for s in strikes if s <= spot * (1 - HALF_WIDTH)), default=None)
    hi = min((s for s in strikes if s >= spot * (1 + HALF_WIDTH)), default=None)
    if lo is None or hi is None:
        return None
    pick = None
    for a, b in zip([s for s in strikes if lo <= s < spot], reversed([s for s in strikes if spot < s <= hi])):    # widest first
        la, sb = (calls[calls["strike"] == s].iloc[0]["contractSymbol"] for s in (a, b))
        debit = spread_value(calls, la, sb)
        if debit and debit > 0:
            pick = {"long_strike": float(a), "short_strike": float(b), "long_contract": la, "short_contract": sb, "entry_debit": debit}
            if debit <= max_debit:
                break
    return pick


def spread_value(calls, long_contract, short_contract):
    a, b = _leg_mid(calls, long_contract), _leg_mid(calls, short_contract)
    return None if a is None or b is None else a - b


def open_position(conn, spread, expiration, spot, today=None):
    today = today or date.today()
    row = {"opened_on": today.isoformat(), "opened_at": datetime.now().isoformat(timespec="seconds"), "rule": RULE,
           "underlying": "SPY", "underlying_price": spot, "expiration": expiration, "exit_due": exit_date(today).isoformat(), **spread}
    with lake._WRITE_LOCK, conn:
        conn.execute(f"INSERT OR IGNORE INTO v2_watch_positions ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))
    return row


def open_positions(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM v2_watch_positions WHERE closed_at IS NULL ORDER BY opened_at")]


def close_position(conn, position_id, value, spot):
    with lake._WRITE_LOCK, conn:
        conn.execute("UPDATE v2_watch_positions SET closed_at = ?, exit_value = ?, exit_underlying = ? WHERE position_id = ?",
                     (datetime.now().isoformat(timespec="seconds"), value, spot, position_id))


def list_positions(conn, chain_fn):
    """All watch positions, newest first; open ones get the live spread value. chain_fn(sym, exp) -> {"calls": df}."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM v2_watch_positions ORDER BY opened_at DESC")]
    for r in rows:
        value = r["exit_value"]
        if not r["closed_at"]:
            calls = (chain_fn(r["underlying"], r["expiration"]) or {}).get("calls")
            value = spread_value(calls, r["long_contract"], r["short_contract"]) if calls is not None and len(calls) else None
        r["value"] = value
        r["pnl_pct"] = (value / r["entry_debit"] - 1) * 100 if value is not None and r["entry_debit"] else None
    return rows
