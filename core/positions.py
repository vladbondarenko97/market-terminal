"""Tracked execution-engine outputs (every live run's ticket, including CASH) and their live value."""
from datetime import date, datetime, timezone

from core import lake

DDL = lake.SIGNALS_DDL

# Version of the horizon (+1D/+1W/+2W) values. Positions are priced when the terminal asks, not stored in a
# snapshot, so the version travels in the horizons dict (`_version`). Bump it when a value's meaning changes and add
# a line to docs/value-changes.md.
#   horizons_v1  the day's FIRST recorded mark
#   horizons_v2  the day's LAST recorded mark (closest to the close)
HORIZON_VERSION = "horizons_v2"


def _mid(bid, ask, last):
    if bid and ask and bid > 0 and ask > 0:
        return (bid + ask) / 2
    return last


def signal_from_ctx(ctx):
    """The position the engine output for this run, or None when the engine produced no verdict."""
    e = ctx.get("execution") or {}
    if e.get("total_score") is None:
        return None
    t = e.get("ticket")
    base = {"run_id": ctx["run"]["run_id"], "created_at": ctx["run"]["generated_at"], "underlying": "SPY",
            "underlying_price": (ctx.get("technicals") or {}).get("current_price")
            or ((ctx.get("options") or {}).get("SPY") or {}).get("spot"),
            "score": e["total_score"], "bias": e.get("directional_bias"),
            "rationale": " | ".join(x for x in (e.get("strike_rationale"), e.get("expiration_rationale")) if x)}
    if isinstance(t, dict) and t.get("contract"):
        return {**base, "position_type": t.get("type"), "contract": t["contract"], "strike": t.get("strike"),
                "expiration": t.get("expiration"), "entry_bid": t.get("bid"), "entry_ask": t.get("ask"),
                "entry_last": t.get("last"), "entry_mid": _mid(t.get("bid"), t.get("ask"), t.get("last")),
                "entry_iv": t.get("iv"), "entry_volume": t.get("volume"), "entry_oi": t.get("open_interest"),
                "allocation": t.get("allocation")}
    if abs(e["total_score"]) < 3:
        return {**base, "position_type": "CASH", "allocation": "Cash / no position"}
    return None   # directional verdict but no contract could be selected: nothing to track


def record_signal(conn, ctx):
    """Idempotent: one row per run_id."""
    s = signal_from_ctx(ctx)
    if not s:
        return None
    lake.migrate(conn)
    insert_signal(conn, s)
    return s


HORIZONS = (("1d", 1), ("1w", 7), ("2w", 14))
RISK_FREE = 0.05


def _bs(S, K, T, iv, kind):
    """Black-Scholes value; T in years. At/after expiry returns intrinsic."""
    import math
    from scipy.stats import norm
    intrinsic = max(0.0, S - K) if kind == "CALL" else max(0.0, K - S)
    if T <= 0 or not iv or iv <= 0:
        return intrinsic
    d1 = (math.log(S / K) + (RISK_FREE + iv * iv / 2) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    if kind == "CALL":
        return S * norm.cdf(d1) - K * math.exp(-RISK_FREE * T) * norm.cdf(d2)
    return K * math.exp(-RISK_FREE * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def _implied_vol(price, S, K, T, kind):
    """IV that reproduces the entry mid (the provider's IV field is unreliable pre-market)."""
    from scipy.optimize import brentq
    if not price or not S or T <= 0 or price <= _bs(S, K, 0, 0, kind) + 1e-6:
        return None
    try:
        return brentq(lambda v: _bs(S, K, T, v, kind) - price, 1e-4, 5.0, xtol=1e-6)
    except ValueError:
        return None


def _market_mark(conn, contract, day):
    """Real recorded mark for a contract on a given trading day (captured from a run's chain), if any. When several
    runs recorded one that day, the LAST is used: it is the closest to the close."""
    import json
    row = conn.execute("""SELECT value_json FROM v2_observations WHERE metric_id = 'position.mark' AND entity = ?
                          AND substr(observed_at, 1, 10) = ? AND value_json IS NOT NULL
                          ORDER BY observed_at DESC, obs_id DESC LIMIT 1""", (contract, day)).fetchone()
    return json.loads(row[0]) if row else None


def horizon_values(conn, r, closes):
    """Value at +1 day / +1 week / +2 weeks from entry. closes: pandas Series of daily closes (DatetimeIndex, naive).
    basis: market (recorded chain mark) | model (BS at that day's close with the IV implied by the entry mid) |
    expiry_intrinsic | pending (date not reached)."""
    from datetime import timedelta
    import pandas as pd
    out = {}
    entry_day = pd.Timestamp(r["created_at"]).tz_convert("America/New_York").date() \
        if pd.Timestamp(r["created_at"]).tzinfo else pd.Timestamp(r["created_at"]).date()
    dates = [d.date() for d in closes.index] if closes is not None and len(closes) else []
    exp = date.fromisoformat(r["expiration"]) if r.get("expiration") else None
    S0, K, kind = r.get("underlying_price"), r.get("strike"), r["position_type"]
    iv = None
    if S0 and K and exp and kind != "CASH":
        iv = _implied_vol(r.get("entry_mid"), S0, K, (exp - entry_day).days / 365, kind)
        if iv is None and (r.get("entry_iv") or 0) > 0.05:
            iv = r["entry_iv"]
    out["_model_iv"] = iv
    out["_version"] = HORIZON_VERSION
    for label, days in HORIZONS:
        target = entry_day + timedelta(days=days)
        day = next((d for d in dates if d >= target), None)
        if day is None:
            out[label] = {"basis": "pending", "target": target.isoformat()}
            continue
        S = float(closes.iloc[dates.index(day)])
        h = {"date": day.isoformat(), "spy_close": S}
        if kind == "CASH":
            h.update(basis="spy_move", change_pct=(S / S0 - 1) * 100 if S0 else None)
        elif exp and day >= exp:
            settle_day = next((d for d in dates if d >= exp), None)
            Se = float(closes.iloc[dates.index(settle_day)]) if settle_day else S
            v = max(0.0, Se - K) if kind == "CALL" else max(0.0, K - Se)
            h.update(basis="expiry_intrinsic", value=v, date=exp.isoformat(), spy_close=Se)
        else:
            mark = _market_mark(conn, r["contract"], day.isoformat())
            if mark and mark.get("mid") is not None:
                h.update(basis="market", value=mark["mid"])
            elif iv:
                h.update(basis="model", value=_bs(S, K, (exp - day).days / 365, iv, kind), iv=iv)
            else:
                h.update(basis="unavailable")
        if h.get("value") is not None and r.get("entry_mid"):
            h["change_pct"] = (h["value"] / r["entry_mid"] - 1) * 100
        out[label] = h
    return out


def list_live(conn, underlying_quote, option_chain, daily_closes=None):
    """Tracked (not deleted) positions, newest first, with live prices and +1d/+1w/+2w values.
    underlying_quote(sym) -> float|None; option_chain(sym, exp) -> {"calls": df, "puts": df}|None;
    daily_closes(sym) -> pandas Series of daily closes. All cached by the caller.
    Read-only: it runs no DDL or migration, so `conn` may be a `lake.connect_readonly()` connection. A database the
    pipeline has not migrated yet raises sqlite3.OperationalError (no such table)."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM v2_trade_signals WHERE deleted_at IS NULL ORDER BY created_at DESC")]
    today = date.today().isoformat()
    closes = {}
    for r in rows:
        sym = r["underlying"]
        if daily_closes and sym not in closes:
            closes[sym] = daily_closes(sym)
        r["horizons"] = horizon_values(conn, r, closes.get(sym))
        now_u = underlying_quote(sym)
        r["underlying_now"] = now_u
        r["underlying_change_pct"] = (now_u / r["underlying_price"] - 1) * 100 \
            if now_u and r.get("underlying_price") else None
        if r["position_type"] == "CASH":
            continue
        r["expired"] = bool(r.get("expiration") and r["expiration"] < today)
        r["dte"] = (date.fromisoformat(r["expiration"]) - date.today()).days if r.get("expiration") else None
        if r["expired"]:
            continue                      # final value is the expiry_intrinsic horizon; nothing live to price
        chain = option_chain(sym, r["expiration"])
        df = (chain or {}).get("calls" if r["position_type"] == "CALL" else "puts")
        now_mid = None
        if df is not None and len(df):
            hit = df[df["contractSymbol"] == r["contract"]]
            if len(hit):
                h = hit.iloc[0]
                r["now_bid"], r["now_ask"], r["now_last"] = (float(h[k]) if h[k] == h[k] else None
                                                             for k in ("bid", "ask", "lastPrice"))
                now_mid = _mid(r["now_bid"], r["now_ask"], r["now_last"])
        r["now_mid"] = now_mid
        if now_mid is not None and r.get("entry_mid"):
            r["change"] = now_mid - r["entry_mid"]
            r["change_pct"] = (now_mid / r["entry_mid"] - 1) * 100
    return rows


def open_contracts(conn, underlying="SPY"):
    lake.migrate(conn)
    return [dict(r) for r in conn.execute(
        """SELECT DISTINCT contract, expiration, position_type FROM v2_trade_signals WHERE deleted_at IS NULL
           AND position_type IN ('CALL','PUT') AND underlying = ? AND expiration >= ?""",
        (underlying, date.today().isoformat()))]


def toggle_star(conn, signal_id):
    with lake._WRITE_LOCK, conn:
        conn.execute("UPDATE v2_trade_signals SET starred = 1 - starred WHERE signal_id = ?", (signal_id,))
    row = conn.execute("SELECT starred FROM v2_trade_signals WHERE signal_id = ?", (signal_id,)).fetchone()
    return None if row is None else bool(row[0])


def stop_tracking(conn, signal_id):
    """Hidden from the card and never priced again; the record stays in the database (history is kept)."""
    with lake._WRITE_LOCK, conn:
        cur = conn.execute("UPDATE v2_trade_signals SET deleted_at = ? WHERE signal_id = ? AND deleted_at IS NULL",
                           (datetime.now(timezone.utc).isoformat(timespec="seconds"), signal_id))
    return cur.rowcount > 0


def backfill(conn):
    """Record the ticket of every committed live run that has none (runs from before tickets were tracked, or whose
    recording failed). Idempotent: a run that already has a ticket is never touched, so a second call returns 0.
    Offline, replay and failed runs are not eligible. Returns the number of tickets recorded."""
    import json
    lake.migrate(conn)
    n = 0
    for (cj,) in conn.execute(
            """SELECT s.context_json FROM v2_snapshots s JOIN v2_runs r ON r.run_id = s.run_id
               WHERE r.mode = 'live' AND r.status IN ('committed', 'completed', 'completed_with_warnings')
               AND NOT EXISTS (SELECT 1 FROM v2_trade_signals t WHERE t.run_id = s.run_id)
               ORDER BY s.created_at""").fetchall():
        sig = signal_from_ctx(json.loads(cj))
        if sig and insert_signal(conn, sig):
            n += 1
    return n


# ---------------------------------------------------------------- recovery from sent report emails
def _money(text):
    import re
    m = re.search(r"[-\d.,]+", str(text or ""))
    try:
        return float(m.group(0).replace(",", "")) if m else None
    except ValueError:
        return None


def signal_from_email(sent_iso, message_id, ticket, score, bias, spy_price):
    """Map a 'live_trade_ticket' block from a sent report email to a signal row (CASH included)."""
    import re
    base = {"run_id": f"email:{message_id}", "created_at": sent_iso, "underlying": "SPY", "underlying_price": spy_price,
            "score": int(score) if str(score).lstrip("-").isdigit() else None, "bias": bias, "source": "email_archive"}
    if isinstance(ticket, str) and ticket.startswith("CASH"):
        return {**base, "position_type": "CASH", "allocation": "Cash / no position"}
    if not isinstance(ticket, dict) or not ticket.get("contract"):
        return None
    def grab(label, text, pat=r"\$?([\d.]+)"):
        m = re.search(label + r":\s*" + pat, text)
        return _money(m.group(1)) if m else None   # 'N/A' / 'nan' → None
    pr = str(ticket.get("pricing") or "")
    bid, ask, last = grab("Bid", pr), grab("Ask", pr), grab("Last", pr)
    mt = str(ticket.get("metrics") or "")
    iv = grab("IV", mt, r"([\d.]+)")
    iv = iv / 100 if iv is not None else None
    vol, oi = grab("Volume", mt, r"([\d.]+)"), grab("OI", mt, r"([\d.]+)")
    return {**base, "position_type": ticket.get("type"), "contract": ticket["contract"], "strike": _money(ticket.get("strike")),
            "expiration": ticket.get("expiration"), "entry_bid": bid, "entry_ask": ask, "entry_last": last,
            "entry_mid": _mid(bid, ask, last), "entry_iv": iv, "entry_volume": vol, "entry_oi": oi,
            "allocation": ticket.get("allocation")}


def insert_signal(conn, s):
    cols = list(s.keys())
    with lake._WRITE_LOCK, conn:
        cur = conn.execute(f"INSERT OR IGNORE INTO v2_trade_signals ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                           [s[c] for c in cols])
    return cur.rowcount
