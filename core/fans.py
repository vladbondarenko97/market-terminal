"""Scenario fans: 100 simulated price/level paths per series, from Kronos and from a plain baseline.

Runs in the pipeline (a guarded stage in collect.build_context) and lands in the snapshot as ctx["fans"]; the
terminal only reads it. Contract: docs/scenario-fans-contract.md. Nothing here touches the network: inputs are
frames and series the pipeline already fetched through SourceSession, and Kronos loads from a local folder.

A missing input gives {"status": "missing", "reason": ...}. Never a placeholder path.
"""
import math
import time
import zlib
from datetime import date, timedelta

import numpy as np
import pandas as pd

import config
from core import market_calendar

FANS_VERSION = "fans_v1"
N_PATHS = 100
DAILY_STEPS = 20
WEEKLY_STEPS = 8
BOOT_DAYS = 504                  # trailing daily log returns the bootstrap resamples
SEASON_YEARS = 10
LOOKBACK = 400
KRONOS_T, KRONOS_TOP_P = 1.0, 0.9
QUANTILES = {"p10": 10, "p25": 25, "p50": 50, "p75": 75, "p90": 90}
REASON_JOINT = "Phase 3: needs jointly sampled legs"
WEEKLY_NOTE = "experimental: weekly single values, not candles"
BBL = 42.0

# id -> (label, group, unit, Yahoo symbol, key in the inputs). Daily series with a Yahoo symbol are gradable.
DAILY = {
    "SPY": ("SPY", "markets", "$", "SPY"), "SLV": ("SLV", "markets", "$", "SLV"),
    "VIX": ("VIX", "markets", "index", "^VIX"), "DXY": ("DXY", "markets", "index", "DX-Y.NYB"),
    "TNX": ("10Y yield", "markets", "%", "^TNX"), "SI": ("Silver futures", "markets", "$", "SI=F"),
    "GC": ("Gold futures", "markets", "$", "GC=F"), "HG": ("Copper futures", "markets", "$", "HG=F"),
    "HO": ("ULSD (diesel)", "fuels", "$/gal", "HO=F"), "RB": ("RBOB (gasoline)", "fuels", "$/gal", "RB=F"),
    "CL": ("WTI crude", "fuels", "$/bbl", "CL=F"), "NG": ("Natural gas", "fuels", "$", "NG=F"),
}
WEEKLY = {   # id -> (label, unit, EIA series id)
    "eia.WDISTUS1": ("Distillate stocks", "kb", "WDISTUS1"), "eia.WGTSTUS1": ("Gasoline stocks", "kb", "WGTSTUS1"),
    "eia.WKJSTUS1": ("Jet fuel stocks", "kb", "WKJSTUS1"), "eia.WCESTUS1": ("Crude stocks (commercial)", "kb", "WCESTUS1"),
    "eia.WCSSTUS1": ("Crude stocks (SPR)", "kb", "WCSSTUS1"), "eia.WPULEUS3": ("Refinery utilization", "%", "WPULEUS3"),
}
DAYS = {   # id -> (label, stocks EIA id, product supplied EIA id)
    "days.distillate": ("Distillate days of supply", "WDISTUS1", "WDIUPUS2"),
    "days.gasoline": ("Gasoline days of supply", "WGTSTUS1", "WGFUPUS2"),
    "days.jet": ("Jet fuel days of supply", "WKJSTUS1", "WKJUPUS2"),
}
YAHOO_FRAME_KEYS = {"SPY": "SPY", "SLV": "SLV", "VIX": "VIX", "DXY": "DXY", "TNX": "TNX", "SI": "SI_F", "GC": "GC_F",
                    "HG": "HG_F"}      # HISTORY_SYMBOLS keys in collect.py; the fuels are fetched next to refining
EIA_IDS = sorted({v[2] for v in WEEKLY.values()} | {v[1] for v in DAYS.values()} | {v[2] for v in DAYS.values()})
FUEL_SYMBOLS = {"HO": "HO=F", "RB": "RB=F", "CL": "CL=F", "NG": "NG=F"}


# ------------------------------------------------------------------ small helpers
def sig4(x):
    """Round to 4 significant digits."""
    x = float(x)
    return x if x == 0 or not math.isfinite(x) else float(f"{x:.4g}")


def _round(a):
    return np.vectorize(sig4, otypes=[float])(a).tolist()


def _seed(*parts):
    return zlib.crc32("|".join(str(p) for p in parts).encode())


def trading_dates(last, n):
    """The next n NYSE trading days after `last` (holidays skipped, not plain business days)."""
    out, d = [], last
    while len(out) < n:
        d += timedelta(days=1)
        if market_calendar.is_trading_day(d):
            out.append(d)
    return out


def quantiles(paths):
    a = np.asarray(paths, dtype=float)
    return {k: _round(np.percentile(a, q, axis=0)) for k, q in QUANTILES.items()}


def _model(paths, **extra):
    a = np.asarray(paths, dtype=float)
    return {"status": "fresh", "reason": None, **extra, "paths": _round(a), "q": quantiles(a)}


def _missing(reason, **extra):
    return {"status": "missing", "reason": reason, **extra}


def _naive_index(s):
    s = s.copy()
    idx = pd.DatetimeIndex(s.index)
    s.index = (idx.tz_localize(None) if idx.tz is not None else idx).normalize()
    return s[~s.index.duplicated(keep="last")]


def _clean_ohlcv(df):
    """Provider daily bars -> lower-case open/high/low/close/volume, naive dates, no incomplete rows."""
    if df is None or len(df) == 0:
        return None
    d = df.rename(columns=str.lower)
    if "volume" not in d:
        d["volume"] = 0.0
    d = d[["open", "high", "low", "close", "volume"]]
    d = _naive_index(d).dropna(subset=["open", "high", "low", "close"])
    d["volume"] = d["volume"].fillna(0.0)
    return d[(d[["open", "high", "low", "close"]] > 0).all(axis=1)]


# ------------------------------------------------------------------ baselines
def bootstrap_paths(close, steps, seed, n=N_PATHS):
    """n paths from resampling the trailing daily log returns of `close` (positive series)."""
    c = np.asarray(close, dtype=float)
    r = np.diff(np.log(c))[-BOOT_DAYS:]
    if len(r) < 60:
        return None
    pick = np.random.default_rng(seed).integers(0, len(r), size=(n, steps))
    return c[-1] * np.exp(np.cumsum(r[pick], axis=1))


def joint_bootstrap_levels(legs, steps, seed, n=N_PATHS):
    """legs: DataFrame of aligned daily closes. Each path step resamples ONE historical day for all legs together,
    so the legs keep their correlation. Returns an array (n, steps, legs) of simulated levels."""
    r = np.diff(np.log(legs.values.astype(float)), axis=0)[-BOOT_DAYS:]
    if len(r) < 60:
        return None
    pick = np.random.default_rng(seed).integers(0, len(r), size=(n, steps))
    return legs.values[-1].astype(float) * np.exp(np.cumsum(r[pick], axis=1))


def _season_pool(diffs, dates, week, years=SEASON_YEARS):
    """Indices of historical week-over-week changes whose week of year is within one week of `week`, past `years`."""
    woy = pd.DatetimeIndex(dates).isocalendar().week.values.astype(int)
    gap = np.minimum(np.abs(woy - week), 52 - np.abs(woy - week))
    ok = (gap <= 1) & (pd.DatetimeIndex(dates) >= pd.DatetimeIndex(dates)[-1] - pd.DateOffset(years=years))
    return np.flatnonzero(ok)


def seasonal_paths(frame, future_dates, seed, n=N_PATHS):
    """frame: DataFrame of aligned weekly levels (columns = legs). Each step resamples ONE historical week-over-week
    change per path (the same week for every column, so legs stay joint) from the same week of year +-1, last 10
    years. Returns (n, steps, columns) levels, or None when the history is too short."""
    d = frame.diff().dropna()
    dates = d.index
    if len(d) < 104:
        return None
    rng = np.random.default_rng(seed)
    out = np.empty((n, len(future_dates), frame.shape[1]))
    level = np.tile(frame.values[-1].astype(float), (n, 1))
    for k, fd in enumerate(future_dates):
        pool = _season_pool(d.values, dates, int(pd.Timestamp(fd).isocalendar()[1]))
        if len(pool) < 3:
            return None
        level = level + d.values[rng.choice(pool, size=n)]
        out[:, k, :] = level
    return out


# ------------------------------------------------------------------ Kronos
def load_kronos():
    """(predictor_fn, info) or (None, info) with info["reason"]. Loads ONLY from config.KRONOS_DIR; no network."""
    info = {"model": "Kronos-small", "params": "24.7M", "T": KRONOS_T, "top_p": KRONOS_TOP_P, "lookback": LOOKBACK,
            "device": None, "seconds": 0.0}
    if not config.KRONOS_ENABLED:
        return None, {**info, "status": "missing", "reason": "disabled: KRONOS_ENABLED=0"}
    try:
        import torch
        import einops  # noqa: F401
        import huggingface_hub  # noqa: F401
    except ImportError as e:
        return None, {**info, "status": "missing",
                      "reason": f"torch not installed: pip install -r requirements-kronos.txt ({e.name})"}
    mdir, tdir = config.KRONOS_DIR / "model", config.KRONOS_DIR / "tokenizer"
    if not ((mdir / "config.json").exists() and (tdir / "config.json").exists()):
        return None, {**info, "status": "missing", "reason": "weights not found: run scripts/fetch_kronos.py"}
    from third_party.kronos import Kronos, KronosPredictor, KronosTokenizer
    device = config.KRONOS_DEVICE or ("mps" if torch.backends.mps.is_available() else "cpu")
    tok = KronosTokenizer.from_pretrained(str(tdir))
    model = Kronos.from_pretrained(str(mdir))
    pred = KronosPredictor(model, tok, device=device, max_context=512)

    def predict(df, x_ts, y_ts, n, steps, seed):
        """n independent paths: the same window n times with sample_count=1 (predict(sample_count=n) averages)."""
        torch.manual_seed(seed)
        out = pred.predict_batch([df] * n, [x_ts] * n, [y_ts] * n, pred_len=steps, T=KRONOS_T, top_p=KRONOS_TOP_P,
                                 sample_count=1, verbose=False)
        return np.stack([o["close"].values for o in out])
    return predict, {**info, "status": "fresh", "reason": None, "device": device}


def _kronos_paths(predictor, window, future_dates, seed, n=N_PATHS):
    """window: DataFrame open/high/low/close/volume with a DatetimeIndex."""
    w = window.tail(LOOKBACK)
    if len(w) < 60:
        return None
    x_ts = pd.Series(w.index)
    y_ts = pd.Series(pd.to_datetime(list(future_dates)))
    a = np.asarray(predictor(w.reset_index(drop=True), x_ts, y_ts, n, len(future_dates), seed), dtype=float)
    if a.shape != (n, len(future_dates)) or not np.isfinite(a).all():
        raise ValueError(f"Kronos returned an unusable array {a.shape}")
    return a


# ------------------------------------------------------------------ series assembly
def _hist(dates, values, k):
    return {"dates": [pd.Timestamp(d).date().isoformat() for d in dates[-k:]], "values": _round(np.asarray(values)[-k:])}


def _series(id_, label, group, unit, freq, symbol, last_date, last, hist, dates, models, levels=None, note=None):
    return {"label": label, "group": group, "unit": unit, "freq": freq, "cards": [], "symbol": symbol,
            "asof": pd.Timestamp(last_date).date().isoformat(), "last": sig4(last), "hist": hist,
            "dates": [d.isoformat() for d in dates], "models": models, "levels": levels or [], "note": note}


def _try_kronos(predictor, kinfo, window, dates, seed):
    if predictor is None:
        return _missing(kinfo.get("reason") or "Kronos unavailable")
    try:
        a = _kronos_paths(predictor, window, dates, seed)
        return _missing("history too short for Kronos") if a is None else _model(a)
    except Exception as e:
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"}


def spy_levels(options):
    """Put wall, call wall and zero gamma from this run's SPY GEX block, when it is fresh."""
    g = ((options or {}).get("SPY") or {}).get("gex") or {}
    if g.get("status") not in ("fresh", "partial"):
        return []
    out = []
    for key, name in (("put_wall", "Put wall"), ("zero_gamma", "Zero gamma"), ("call_wall", "Call wall")):
        v = g.get(key)
        if isinstance(v, (int, float)) and math.isfinite(v):
            out.append({"v": sig4(v), "name": f"{name} (this run)"})
    return out


def build_fans(inputs, predictor=None, kinfo=None):
    """inputs: {"run_id", "today": date, "daily": {id: OHLCV DataFrame}, "weekly": {eia id: Series}, "options": ctx options}.
    predictor/kinfo: injected for tests; by default Kronos is loaded once from config.KRONOS_DIR."""
    today, run_key = inputs["today"], str(inputs["today"])
    if predictor is None and kinfo is None:
        predictor, kinfo = load_kronos()
    kinfo = dict(kinfo or {"status": "missing", "reason": "Kronos unavailable"})
    kinfo.setdefault("seconds", 0.0)
    series, kron_s, errors = {}, 0.0, []

    def run_kronos(window, dates, seed):
        nonlocal kron_s
        t0 = time.monotonic()
        m = _try_kronos(predictor, kinfo, window, dates, seed)
        kron_s += time.monotonic() - t0
        if m["status"] == "error":
            errors.append(m["reason"])
        return m

    daily = {k: _clean_ohlcv(v) for k, v in (inputs.get("daily") or {}).items()}
    for sid, (label, group, unit, sym) in DAILY.items():
        df = daily.get(sid)
        if df is None or len(df) < 60:
            series[sid] = {"label": label, "group": group, "unit": unit, "freq": "D", "cards": [], "symbol": sym,
                           "models": {"kronos": _missing(f"no daily history for {sym}"),
                                      "baseline": _missing(f"no daily history for {sym}", kind="bootstrap")}}
            continue
        dates = trading_dates(df.index[-1].date(), DAILY_STEPS)
        seed = _seed(sid, run_key)
        base = bootstrap_paths(df["close"].values, DAILY_STEPS, seed)
        models = {"kronos": run_kronos(df, dates, seed),
                  "baseline": _missing("history too short", kind="bootstrap") if base is None
                  else _model(base, kind="bootstrap")}
        series[sid] = _series(sid, label, group, unit, "D", sym, df.index[-1], df["close"].iloc[-1],
                              _hist(df.index, df["close"].values, 90), dates, models,
                              levels=spy_levels(inputs.get("options")) if sid == "SPY" else None)

    # derived daily: the same historical days for every leg
    legs = pd.DataFrame({k: daily[k]["close"] for k in ("HO", "RB", "CL") if daily.get(k) is not None}).dropna()
    for sid, label, fn in (("crack.diesel", "Diesel crack (HO x 42 - CL)", lambda ho, rb, cl: ho * BBL - cl),
                           ("crack.321", "3-2-1 crack", lambda ho, rb, cl: (2 * rb * BBL + ho * BBL - 3 * cl) / 3)):
        head = {"label": label, "group": "derived", "unit": "$/bbl", "freq": "D", "cards": [], "symbol": None}
        if legs.shape[1] < 3 or len(legs) < 120:
            series[sid] = {**head, "models": {"kronos": _missing(REASON_JOINT),
                                              "baseline": _missing("no daily history for HO=F / RB=F / CL=F", kind="joint_bootstrap")}}
            continue
        lv = joint_bootstrap_levels(legs, DAILY_STEPS, _seed(sid, run_key))
        dates = trading_dates(legs.index[-1].date(), DAILY_STEPS)
        hist = fn(legs["HO"], legs["RB"], legs["CL"])
        paths = fn(lv[..., 0], lv[..., 1], lv[..., 2])
        series[sid] = _series(sid, label, "derived", "$/bbl", "D", None, legs.index[-1], hist.iloc[-1],
                              _hist(legs.index, hist.values, 90), dates,
                              {"kronos": _missing(REASON_JOINT),
                               "baseline": _model(paths, kind="joint_bootstrap")})

    # weekly EIA
    weekly = {k: _naive_index(v.dropna()) for k, v in (inputs.get("weekly") or {}).items() if v is not None}
    for sid, (label, unit, eid) in WEEKLY.items():
        s = weekly.get(eid)
        if s is None or len(s) < 156:
            r = f"no EIA history for {eid}"
            series[sid] = {"label": label, "group": "inventories", "unit": unit, "freq": "W", "cards": [], "symbol": None,
                           "models": {"kronos": _missing(r), "baseline": _missing(r, kind="seasonal")}}
            continue
        dates = [s.index[-1].date() + timedelta(days=7 * (k + 1)) for k in range(WEEKLY_STEPS)]
        seed = _seed(sid, run_key)
        sp = seasonal_paths(s.to_frame("v"), dates, seed)
        w = pd.DataFrame({"open": s, "high": s, "low": s, "close": s, "volume": 0.0})
        models = {"kronos": run_kronos(w, dates, seed),
                  "baseline": _missing("history too short", kind="seasonal") if sp is None
                  else _model(sp[..., 0], kind="seasonal")}
        series[sid] = _series(sid, label, "inventories", unit, "W", None, s.index[-1], s.iloc[-1],
                              _hist(s.index, s.values, 52), dates, models, note=WEEKLY_NOTE)

    # derived weekly: days of supply = stocks / 4-week average of product supplied (as core/refining.py DEMAND_FOR)
    for sid, (label, stock_id, supp_id) in DAYS.items():
        st, su = weekly.get(stock_id), weekly.get(supp_id)
        head = {"label": label, "group": "derived", "unit": "days", "freq": "W", "cards": [], "symbol": None}
        if st is None or su is None:
            series[sid] = {**head, "models": {"kronos": _missing(REASON_JOINT), "baseline": _missing(
                f"no EIA history for {stock_id if st is None else supp_id}", kind="joint_bootstrap")}}
            continue
        f = pd.DataFrame({"stocks": st, "supplied": su.rolling(4).mean()}).dropna()
        f = f[f["supplied"] > 0]
        sim = seasonal_paths(f, [f.index[-1].date() + timedelta(days=7 * (k + 1)) for k in range(WEEKLY_STEPS)],
                             _seed(sid, run_key)) if len(f) >= 156 else None
        if sim is None:
            series[sid] = {**head, "models": {"kronos": _missing(REASON_JOINT), "baseline": _missing(
                "history too short", kind="joint_bootstrap")}}
            continue
        dates = [f.index[-1].date() + timedelta(days=7 * (k + 1)) for k in range(WEEKLY_STEPS)]
        days = f["stocks"] / f["supplied"]
        sim_days = sim[..., 0] / np.maximum(sim[..., 1], 1e-9)
        series[sid] = _series(sid, label, "derived", "days", "W", None, f.index[-1], days.iloc[-1],
                              _hist(f.index, days.values, 52), dates,
                              {"kronos": _missing(REASON_JOINT),
                               "baseline": _model(sim_days, kind="joint_bootstrap")},
                              note="stocks / 4-week average product supplied")

    return _finish(series, kinfo, kron_s, errors)


def _finish(series, kinfo, kron_s, errors):
    kinfo = {**kinfo, "seconds": round(kron_s, 1)}
    if errors:
        kinfo.update(status="error", reason=errors[0])
    elif kinfo["status"] == "fresh" and not any(
            m.get("status") == "fresh" and k == "kronos" for s in series.values() for k, m in s["models"].items()):
        kinfo.update(status="missing", reason="no series had enough history for Kronos")
    models = [m for s in series.values() for m in s["models"].values() if m.get("reason") != REASON_JOINT]   # by design, not a gap
    fresh = [m for m in models if m["status"] == "fresh"]
    if not fresh:
        status, reason = "missing", "no series had enough history"
    elif len(fresh) < len(models):
        status = "partial"
        reason = "kronos: " + (kinfo.get("reason") or "") if kinfo["status"] != "fresh" else "some series lack history"
    else:
        status, reason = "fresh", None
    return {"status": status, "reason": reason, "version": FANS_VERSION, "kronos": kinfo, "series": series}


# ------------------------------------------------------------------ forecast log
FAN_MODELS = {"kronos": "kronos_fan_v1", "baseline": "baseline_fan_v1"}
FAN_HORIZONS = (("1w", 5), ("1m", 20))      # forecast.HORIZONS keys and their trading-day steps


def fan_rows(ctx):
    """v2_forecasts rows (same columns as forecast.forecast_rows) for daily series with a Yahoo symbol."""
    fans = ctx.get("fans") or {}
    run = ctx["run"]
    rows = []
    for sid, s in (fans.get("series") or {}).items():
        if s.get("freq") != "D" or not s.get("symbol") or not s.get("dates"):
            continue
        for mk, name in FAN_MODELS.items():
            m = (s.get("models") or {}).get(mk) or {}
            if m.get("status") != "fresh":
                continue
            for hk, step in FAN_HORIZONS:
                allp = np.asarray(m["paths"], dtype=float)
                p = allp[:, step - 1]
                spot, q = s["last"], m["q"]
                p_up = float((p > spot).mean())
                steps = np.log(np.hstack([np.full((len(p), 1), spot), allp[:, :step]]))
                vol = float(np.diff(steps, axis=1).std(ddof=1) * math.sqrt(252))
                lo, hi = np.percentile(p, [16, 84])
                rows.append((run["run_id"], run["generated_at"], s["symbol"], name, hk, s["dates"][step - 1], spot, p_up,
                             float(lo), float(hi), "UP" if q["p50"][step - 1] > spot else "DOWN", vol))
    return rows


def record_fans(conn, ctx):
    from core import lake
    from core.forecast import SCORE_DDL
    rows = fan_rows(ctx)
    with lake._WRITE_LOCK, conn:
        conn.execute(SCORE_DDL)
        conn.executemany("""INSERT OR IGNORE INTO v2_forecasts (run_id, created_at, symbol, model, horizon, target_date,
                            spot, p_up, lower68, upper68, direction, vol) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    return len(rows)


# ------------------------------------------------------------------ backtest scoring (scripts/backtest_fans.py)
def crps(samples, y):
    """Continuous ranked probability score of an ensemble against the outcome y."""
    x = np.sort(np.asarray(samples, dtype=float))
    n = len(x)
    pair = 2 * np.sum((2 * np.arange(1, n + 1) - n - 1) * x) / (n * n)      # mean |X - X'| from the sorted sample
    return float(np.mean(np.abs(x - y)) - 0.5 * pair)


def score_origin(terminal, spot, actual):
    """One forecast origin and horizon: terminal = simulated end values. Band = p10..p90."""
    p10, p50, p90 = np.percentile(terminal, [10, 50, 90])
    return {"band": bool(p10 <= actual <= p90), "crps_pct": crps(terminal, actual) / spot * 100,
            "dir": bool((p50 > spot) == (actual > spot))}


def summarise_scores(rows, horizons):
    """rows: [{"origin": date, h: {"band", "crps_pct", "dir"}}]. -> {"by_horizon": {...}, "period": [first, last]}."""
    out = {"by_horizon": {}, "period": [min(r["origin"] for r in rows), max(r["origin"] for r in rows)] if rows else None}
    for h in horizons:
        got = [r[str(h)] for r in rows if str(h) in r]
        if got:
            out["by_horizon"][str(h)] = {"n": len(got), "band_hit": float(np.mean([g["band"] for g in got])),
                                         "crps_pct": float(np.mean([g["crps_pct"] for g in got])),
                                         "dir_hit": float(np.mean([g["dir"] for g in got]))}
    return out
