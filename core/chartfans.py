"""CHART tab fans: daily and intraday scenario fans for what the snapshot's fans (core/fans.py) do not cover.

Built by `main_pipeline.py bars --fans` from the stored chart bars (and the non-price series of portfolio.db) and kept in
chart.db v2_chart_fans, one row per (item, tf, asof, model), INSERT OR IGNORE. Nothing here touches the network. The
models are the snapshot's own (fans.bootstrap_paths, fans._kronos_paths); a missing input is recorded as status
missing with the reason, never a placeholder path.
"""
import json
from datetime import timedelta

import numpy as np
import pandas as pd

from core import bars, fans, lake, market_calendar

NY = bars.NY
Q = (5, 10, 25, 50, 75, 90, 95)
# chart item -> snapshot fan id, for the items the snapshot already forecasts (those get no chart-store fan)
SNAPSHOT_ITEMS = {sym: sid for sid, (_, _, _, sym) in fans.DAILY.items()}
SNAPSHOT_ITEMS |= {f"eia.{k}": k for k in fans.DAYS} | {k: k for k in fans.WEEKLY}
INTRADAY_ITEMS = ("SPY", "SLV")
INTRADAY = (("1h", 60, 24), ("5m", 5, 48))              # tf, minutes per bar, steps
INTRADAY_VERSIONS = {"kronos": "kronos_fan_intraday_v1", "baseline": "baseline_fan_intraday_v1"}
INTRADAY_VERDICT = "intraday fans are not backtested yet; the daily Kronos fan loses to the baseline out of sample"


def pack(m):
    """A fans model dict -> {status, reason} plus, when it has paths, paths and p05..p95 per step."""
    out = {"status": m["status"], "reason": m.get("reason")}
    if m.get("paths") is not None:
        a = np.asarray(m["paths"], dtype=float)
        out |= {"paths": m["paths"], "q": {f"p{q:02d}": fans._round(np.percentile(a, q, axis=0)) for q in Q}}
    return out


def future_stamps(last, minutes, n):
    """The next n bar opens (New York time) after `last`, on a 04:00-20:00 trading-day grid like the stored bars."""
    t, out = last.tz_convert(NY), []
    while len(out) < n:
        t += pd.Timedelta(minutes=minutes)
        while not (market_calendar.is_trading_day(t.date()) and 4 <= t.hour < 20):
            t = (t if t.hour < 4 else t + pd.Timedelta(days=1)).normalize() + pd.Timedelta(hours=4)
        out.append(t)
    return out


def _store(conn, item, tf, asof, last, dates, models, versions):
    stamp = lake.utc_now_iso()
    rows = [(item, tf, asof, k, versions[k], stamp,
             json.dumps({"last": float(last), "dates": dates, "model": pack(m)})) for k, m in models.items()]
    with conn:
        before = conn.total_changes
        conn.executemany("INSERT OR IGNORE INTO v2_chart_fans VALUES (?,?,?,?,?,?,?)", rows)
        return conn.total_changes - before


def build_one(conn, item, tf, df, asof, dates, out_dates, predictor, kinfo, versions, widen):
    """Both models for one item. df: OHLCV window with a naive index; dates: the future steps for Kronos;
    out_dates: the same steps as stored strings. Returns the rows added."""
    seed = fans._seed(item, tf, asof)
    base = fans.bootstrap_paths(df["close"].values, len(dates), seed)
    models = {"kronos": fans._try_kronos(predictor, kinfo, df, dates, seed, widen),
              "baseline": fans._missing("history too short", kind="bootstrap") if base is None
              else fans._model(base, kind="bootstrap")}
    return _store(conn, item, tf, asof, df["close"].iloc[-1], out_dates, models, versions or fans.FAN_MODELS)


def build_all(conn, main=None):
    """Daily fans for every tracked symbol and every chart series without a snapshot fan, and the 1h and 5m fans of
    SPY and SLV. `main` is a read-only portfolio.db connection for the non-price series (None: they are skipped).
    Returns {"added": rows, "kronos": reason or None}."""
    predictor, kinfo = fans.load_kronos()
    kinfo = dict(kinfo or {})
    added = 0
    for sym in bars.tracked(conn):
        if sym not in SNAPSHOT_ITEMS:
            df = fans._clean_ohlcv(bars.read_bars(conn, sym, "1d", limit=fans.LOOKBACK + 1))
            added += _daily(conn, sym, df, predictor, kinfo, True)
    for sid in bars.SERIES if main is not None else ():
        if sid in SNAPSHOT_ITEMS:
            continue
        try:
            s = bars.SERIES[sid][4](main)
        except Exception:                      # a missing table or payload: no series, no fan
            continue
        s = s[s > 0]
        df = fans._clean_ohlcv(pd.DataFrame({"open": s, "high": s, "low": s, "close": s, "volume": 0.0}))
        added += _daily(conn, sid, df, predictor, kinfo, False)
    for sym in INTRADAY_ITEMS:
        for tf, minutes, steps in INTRADAY:
            df = bars.read_bars(conn, sym, tf, limit=fans.LOOKBACK + 1).dropna()
            if df.empty:
                continue
            asof, stamps = df.index[-1].isoformat(), future_stamps(df.index[-1], minutes, steps)
            df.index = df.index.tz_convert(NY).tz_localize(None)      # Kronos sees naive New York clock times
            added += build_one(conn, sym, tf, df, asof, [x.tz_localize(None) for x in stamps],
                               [x.tz_convert("UTC").isoformat() for x in stamps], predictor, kinfo, INTRADAY_VERSIONS, False)
    return {"added": added, "kronos": None if kinfo.get("status") == "fresh" else kinfo.get("reason")}


def _daily(conn, item, df, predictor, kinfo, widen):
    if df is None or len(df) < 60:
        return 0
    dates = fans.trading_dates(df.index[-1].date(), fans.DAILY_STEPS)
    return build_one(conn, item, "1d", df, df.index[-1].date().isoformat(), dates, [d.isoformat() for d in dates],
                     predictor, kinfo, None, widen)


def built_today(conn, now=None):
    last = conn.execute("SELECT MAX(built_at) FROM v2_chart_fans").fetchone()[0]
    return bool(last) and pd.Timestamp(last).tz_convert(NY).date() == (now or bars.now_ny()).date()


def due(now=None):
    """After 16:30 New York on a trading day (the scheduled job's cue for building fans)."""
    now = now or bars.now_ny()
    return market_calendar.is_trading_day(now.date()) and (now.hour, now.minute) >= (16, 30)


def read(conn, item, tf):
    """The newest stored fans of (item, tf): (asof, last, dates, {model: packed}) or None."""
    if conn is None:
        return None
    rows = conn.execute("""SELECT asof, model, json FROM v2_chart_fans WHERE item = ? AND tf = ? AND asof =
                           (SELECT MAX(asof) FROM v2_chart_fans WHERE item = ? AND tf = ?)""", (item, tf, item, tf)).fetchall()
    if not rows:
        return None
    parsed = {r["model"]: json.loads(r["json"]) for r in rows}
    first = next(iter(parsed.values()))
    return (rows[0]["asof"], first["last"], first["dates"], {k: v["model"] for k, v in parsed.items()})
