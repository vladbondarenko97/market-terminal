"""Forecast Lab: nine institutional-style models for SPY / SLV at 1 day, 1 week, 1 month and 1 year.

Every model consumes data the run already captured (option chains, daily OHLC, FRED, CFTC, iShares, Fed calendar);
nothing here makes network requests. Outputs are plain dicts stored in the committed snapshot (ctx["forecast"]).

1 implied_distribution   options-implied (risk-neutral) distribution per horizon, Breeden-Litzenberger
2 vol_forecast           HAR-RV (Corsi 2009) realized-volatility forecast vs implied
3 trend_model            time-series momentum / CTA replication with flip levels (Moskowitz-Ooi-Pedersen 2012)
4 positioning            CFTC COT (managed money silver, leveraged funds/asset managers S&P) + SLV trust ounces
5 silver_fair_value      relative-value regression: gold, real yields, dollar, copper, industrial production
6 macro_regime           NY Fed yield-curve recession probit, NFCI, Sahm rule, credit
7 calendar_effects       pre-FOMC / FOMC day, OPEX week, turn of month, CPI days, weekday; upcoming flags
8 mechanical_flows       vol-control deleveraging, leveraged-ETF rebalance, dealer gamma hedging, CTA sensitivity
9 scorecard              logged forecasts graded after the fact + walk-forward backtests
"""
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

VERSION = "forecast_v1"
HORIZONS = [{"key": "1d", "days": 1, "td": 1}, {"key": "1w", "days": 7, "td": 5},
            {"key": "1m", "days": 30, "td": 21}, {"key": "1y", "days": 365, "td": 252}]
R = 0.05


def _f(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _closes(df):
    return df["Close"].dropna() if df is not None and len(df) else pd.Series(dtype=float)


def _naive_index(s):
    s = s.copy()
    idx = pd.to_datetime(s.index)
    s.index = (idx.tz_localize(None) if idx.tz is not None else idx).normalize()
    return s[~s.index.duplicated(keep="last")]


# ============================================================ 1. options-implied distribution
def _bs(S, K, T, iv, call):
    if T <= 0 or iv <= 0:
        return max(0.0, S - K) if call else max(0.0, K - S)
    d1 = (math.log(S / K) + (R + iv * iv / 2) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    if call:
        return S * norm.cdf(d1) - K * math.exp(-R * T) * norm.cdf(d2)
    return K * math.exp(-R * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def _iv_from_price(price, S, K, T, call):
    intrinsic = max(0.0, S - K * math.exp(-R * T)) if call else max(0.0, K * math.exp(-R * T) - S)
    if price is None or price <= intrinsic + 1e-4:
        return None
    try:
        return brentq(lambda v: _bs(S, K, T, v, call) - price, 1e-3, 5.0, xtol=1e-6)
    except ValueError:
        return None


def _otm_smile(chain, spot, T, use_last=False):
    """OTM implied vols from bid/ask mids; with use_last=True (quotes empty, e.g. right after the open) from the
    prior session's last trade prices instead. Pass the prior close as spot then: last trades were struck against it,
    and inverting them at today's spot mis-states every IV after an overnight gap."""
    pts = []
    for side, call in (("calls", True), ("puts", False)):
        df = chain.get(side)
        if df is None or not len(df):
            continue
        for _, r in df.iterrows():
            K = _f(r.get("strike"))
            if K is None or (call and K < spot) or (not call and K >= spot):
                continue
            bid, ask, last = _f(r.get("bid")), _f(r.get("ask")), _f(r.get("lastPrice"))
            mid = (bid + ask) / 2 if bid and ask and bid > 0 and ask > 0 else None
            if mid is None and use_last and last and last > 0:
                mid = last
            if mid is None:
                continue                      # no live quote
            iv = _iv_from_price(mid, spot, K, T, call)
            if iv and 0.02 < iv < 3:
                pts.append((K, iv))
    return sorted(pts)


def implied_distribution(chains, spot, today, levels=None, prior_spot=None):
    """Risk-neutral distribution per horizon. Smile fitted in total variance vs log-moneyness (quadratic), call
    prices on a fine strike grid, density = e^{rT} d2C/dK2 (Breeden-Litzenberger)."""
    if not chains or not spot:
        return {"status": "missing", "reason": "no option chains or spot"}
    exps = sorted(chains)
    out = {"status": "fresh", "method": "Breeden-Litzenberger on quadratic total-variance smile (OTM mids)",
           "note": "risk-neutral probabilities (include risk premia); tails beyond listed strikes are extrapolated",
           "horizons": {}}
    for h in HORIZONS:
        dte_of = {e: (date.fromisoformat(e) - today).days for e in exps}
        cand = [e for e in exps if dte_of[e] >= max(h["days"], 1)]
        exp = min(cand, key=lambda e: dte_of[e]) if cand else exps[-1]
        dte = max(dte_of[exp], 1)
        T = dte / 365
        pts = _otm_smile(chains[exp], spot, T)
        hz = {"expiration": exp, "dte": dte, "extrapolated": not cand}
        price_basis = "live bid/ask mids"
        if len(pts) < 5:
            last_pts = _otm_smile(chains[exp], prior_spot, T + 1 / 365, use_last=True) if prior_spot else []
            if len(last_pts) >= 5:
                pts, price_basis = last_pts, "prior-session last trades (live quotes empty)"
        hz["price_basis"] = price_basis
        if len(pts) >= 5:
            K = np.array([p[0] for p in pts])
            iv = np.array([p[1] for p in pts])
            F = spot * math.exp(R * T)
            x = np.log(K / F)
            w = iv ** 2 * T
            wt = np.exp(-(x / (iv.mean() * math.sqrt(T) * 2)) ** 2)      # emphasize near-the-money points
            coef = np.polyfit(x, w, 2, w=wt)
            atm_iv = math.sqrt(max(np.polyval(coef, 0.0), 1e-6) / T)
            basis = "smile"
        else:
            # fallback: flat vol at the nearest-strike provider IV
            ivs = []
            for side in ("calls", "puts"):
                df = chains[exp].get(side)
                if df is not None and len(df):
                    row = df.loc[(pd.to_numeric(df["strike"]) - spot).abs().idxmin()]
                    v = _f(row.get("impliedVolatility"))
                    if v and v > 0.02:
                        ivs.append(v)
            if not ivs:
                hz.update(status="missing", reason="no usable quotes")
                out["horizons"][h["key"]] = hz
                continue
            atm_iv = sum(ivs) / len(ivs)
            coef = np.array([0.0, 0.0, atm_iv ** 2 * T])
            basis = "flat_atm_iv"
            hz["price_basis"] = "provider IV at nearest strike (flat; too few quotes for a smile)"
        if not cand or abs(dte - h["days"]) / h["days"] > 0.10:
            # expiry differs from the horizon (or none that long): rescale total variance to the exact horizon
            T_target = max(h["days"], 1) / 365
            coef = coef * (T_target / T)
            hz["scaled_from_dte"] = dte
            T = T_target
        sd = atm_iv * math.sqrt(T)
        grid = np.linspace(spot * math.exp(-5 * sd), spot * math.exp(5 * sd), 600)
        F = spot * math.exp(R * T)
        wg = np.maximum(np.polyval(coef, np.log(grid / F)), 1e-8)
        ivg = np.sqrt(wg / T)
        calls = np.array([_bs(spot, k, T, v, True) for k, v in zip(grid, ivg)])
        dens = np.gradient(np.gradient(calls, grid), grid) * math.exp(R * T)
        dens = np.clip(dens, 0, None)
        area = np.trapezoid(dens, grid)
        if not area or not math.isfinite(area):
            hz.update(status="missing", reason="degenerate density")
            out["horizons"][h["key"]] = hz
            continue
        dens /= area
        cdf = np.concatenate([[0], np.cumsum((dens[1:] + dens[:-1]) / 2 * np.diff(grid))])
        cdf /= cdf[-1]

        def q(p):
            return float(np.interp(p, cdf, grid))

        def p_above(level):
            return float(1 - np.interp(level, grid, cdf))
        pct = {f"p{int(p * 100):02d}": q(p) for p in (0.05, 0.16, 0.25, 0.5, 0.75, 0.84, 0.95)}
        step = max(1, len(grid) // 80)
        hz.update({
            "status": "fresh", "basis": basis, "atm_iv": atm_iv, "T_years": T,
            "expected_move_1sd": spot * sd, "expected_move_1sd_pct": sd * 100,
            "percentiles": pct, "range68": [pct["p16"], pct["p84"]], "range90": [pct["p05"], pct["p95"]],
            "median": pct["p50"], "mean": float(np.trapezoid(grid * dens, grid)),
            "p_up": p_above(spot), "p_up_2pct": p_above(spot * 1.02), "p_down_2pct": 1 - p_above(spot * 0.98),
            "p_up_5pct": p_above(spot * 1.05), "p_down_5pct": 1 - p_above(spot * 0.95),
            "p_up_10pct": p_above(spot * 1.10), "p_down_10pct": 1 - p_above(spot * 0.90),
            "skew": ((pct["p84"] - pct["p50"]) - (pct["p50"] - pct["p16"])) / (pct["p84"] - pct["p16"]),
            "smile_points": len(pts),
            "curve": [[float(grid[i]), float(dens[i])] for i in range(0, len(grid), step)],
            "levels": {k: p_above(v) for k, v in (levels or {}).items() if v},
        })
        out["horizons"][h["key"]] = hz
    return out


# ============================================================ 2. HAR-RV volatility forecast
def daily_rv(df):
    """Daily variance proxy: overnight gap^2 + Parkinson range estimator (captures gaps and intraday range)."""
    d = df.dropna(subset=["Open", "High", "Low", "Close"])
    prev = d["Close"].shift(1)
    park = (np.log(d["High"] / d["Low"]) ** 2) / (4 * math.log(2))
    gap = np.log(d["Open"] / prev) ** 2
    return (park + gap.fillna(0)).dropna()


def _har_design(rv):
    X = pd.DataFrame({"d": rv, "w": rv.rolling(5).mean(), "m": rv.rolling(22).mean()})
    return X


def vol_forecast(df, implied=None, spot=None):
    """Direct HAR regressions per horizon: mean RV over the next h days on daily/weekly/monthly RV."""
    if df is None or len(df) < 300:
        return {"status": "missing", "reason": "need >= 300 daily bars"}
    rv = daily_rv(df)
    X = _har_design(rv)
    out = {"status": "fresh", "model": "HAR-RV (Corsi 2009), direct multi-horizon OLS; level or log spec chosen on a validation slice; Parkinson+gap daily variance",
           "horizons": {}, "current": {"rv_1d": math.sqrt(252 * rv.iloc[-1]), "rv_5d": math.sqrt(252 * X['w'].iloc[-1]),
                                       "rv_22d": math.sqrt(252 * X['m'].iloc[-1])},
           "long_run_vol": math.sqrt(252 * rv.mean())}
    bt = {}
    for h in HORIZONS:
        hd = h["td"]
        if hd >= 252:
            # 1y: monthly forecast decays toward the long-run mean (22-day persistence from the fit below)
            m = out["horizons"].get("1m", {}).get("forecast_var")
            lr = rv.mean()
            if m is None:
                continue
            phi = out.get("_monthly_persistence", 0.8)
            avg = lr + (m - lr) * (1 - phi ** (252 / 21)) / ((1 - phi) * (252 / 21)) if phi < 1 else m
            fv = math.sqrt(252 * max(avg, 1e-10))
            out["horizons"]["1y"] = {"forecast_vol": fv, "forecast_var": avg, "basis": "HAR 1m forecast mean-reverting to 5y average"}
        else:
            y = rv[::-1].rolling(hd).mean()[::-1].shift(-1)          # mean RV over t+1..t+h
            data = pd.concat([X, y.rename("y")], axis=1).dropna()
            if len(data) < 150:
                continue
            Xv = data[["d", "w", "m"]].clip(lower=1e-10).values
            Yv = data["y"].clip(lower=1e-10).values
            xl_raw = np.clip(X.iloc[-1][["d", "w", "m"]].values.astype(float), 1e-10, None)
            specs = {"level": (lambda a: a, lambda a, s2: a), "log": (np.log, lambda a, s2: np.exp(a + 0.5 * s2))}

            def fit(spec, lo, hi):
                tx, inv = specs[spec]
                A = np.column_stack([np.ones(hi - lo), tx(Xv[lo:hi])])
                b, *_ = np.linalg.lstsq(A, tx(Yv[lo:hi]), rcond=None)
                s2 = float(np.var(tx(Yv[lo:hi]) - A @ b))
                return b, s2

            def predict(spec, b, s2, xs):
                tx, inv = specs[spec]
                return inv(np.column_stack([np.ones(len(xs)), tx(xs)]) @ b, s2)

            n = len(data)
            v0, t0 = int(n * 0.6), int(n * 0.8)
            def rmse(p, a):
                return float(np.sqrt(np.mean((p - a) ** 2)))
            val = {sp: rmse(predict(sp, *fit(sp, 0, v0), Xv[v0:t0]), Yv[v0:t0]) for sp in specs}
            spec = min(val, key=val.get)                               # chosen on validation, scored on test
            b_tr, s2_tr = fit(spec, 0, t0)
            test_pred = predict(spec, b_tr, s2_tr, Xv[t0:])
            naive = Xv[t0:, 2]
            bt[h["key"]] = {"spec": spec, "oos_days": int(n - t0),
                            "rmse_improvement_vs_naive_pct": (1 - rmse(test_pred, Yv[t0:]) / rmse(naive, Yv[t0:])) * 100,
                            "validation_rmse": val}
            beta, s2 = fit(spec, 0, n)
            fvar = max(float(predict(spec, beta, s2, xl_raw[None, :])[0]), 1e-10)
            if hd == 21:
                lb, *_ = np.linalg.lstsq(np.column_stack([np.ones(n), Xv]), Yv, rcond=None)
                out["_monthly_persistence"] = float(min(max(lb[1:].sum(), 0.0), 0.99))
            out["horizons"][h["key"]] = {"forecast_vol": math.sqrt(252 * fvar), "forecast_var": fvar, "spec": spec,
                                         "coefficients": {"const": beta[0], "daily": beta[1], "weekly": beta[2], "monthly": beta[3]}}
    for k, hz in out["horizons"].items():
        td = next(h["td"] for h in HORIZONS if h["key"] == k)
        if spot:
            hz["expected_move_1sd"] = spot * hz["forecast_vol"] * math.sqrt(td / 252)
            hz["expected_move_1sd_pct"] = hz["forecast_vol"] * math.sqrt(td / 252) * 100
        iv = ((implied or {}).get("horizons", {}).get(k) or {}).get("atm_iv")
        if iv:
            hz["implied_vol"] = iv
            hz["vrp"] = iv - hz["forecast_vol"]
            hz["implied_to_forecast"] = iv / hz["forecast_vol"]
            hz["read"] = ("options rich vs forecast (sell-vol bias)" if iv / hz["forecast_vol"] > 1.15 else
                          "options cheap vs forecast (buy-vol bias)" if iv / hz["forecast_vol"] < 0.9 else "fairly priced")
        hz.pop("forecast_var", None)
    out.pop("_monthly_persistence", None)
    out["backtest"] = bt
    return out


# ============================================================ 3. trend / CTA replication
LOOKBACKS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}


def _trend_exposure(c, idx=-1, rv=None):
    zs = {}
    for k, n in LOOKBACKS.items():
        if len(c) + idx < n:
            continue
        ret = c.iloc[idx] / c.iloc[idx - n] - 1
        sd = (rv if rv else c.pct_change().iloc[:idx if idx != -1 else None].tail(63).std() * math.sqrt(252)) * math.sqrt(n / 252)
        zs[k] = ret / sd if sd else 0.0
    if not zs:
        return None, zs
    return float(np.mean([math.tanh(z) for z in zs.values()])), zs


def trend_model(df, target_vol=0.15):
    c = _closes(df)
    if len(c) < 300:
        return {"status": "missing", "reason": "need >= 300 daily closes"}
    rv60 = c.pct_change().tail(60).std() * math.sqrt(252)
    expo, zs = _trend_exposure(c)
    sig = {}
    for k, n in LOOKBACKS.items():
        ret = c.iloc[-1] / c.iloc[-1 - n] - 1
        flip_next = float(c.iloc[-n])              # tomorrow the lookback anchor rolls forward one day
        sig[k] = {"return": ret, "z": zs.get(k), "signal": "LONG" if ret > 0 else "SHORT", "flip_level": flip_next,
                  "flip_distance_pct": (flip_next / c.iloc[-1] - 1) * 100}
    smas = {n: float(c.rolling(n).mean().iloc[-1]) for n in (20, 50, 100, 200)}
    # sensitivity: exposure after a +/-2% and +/-5% move tomorrow
    sens = {}
    for mv in (-0.05, -0.02, 0.02, 0.05):
        c2 = pd.concat([c, pd.Series([c.iloc[-1] * (1 + mv)], index=[c.index[-1] + pd.Timedelta(days=1)])])
        e2, _ = _trend_exposure(c2, rv=rv60)
        sens[f"{mv * 100:+.0f}%"] = e2
    # walk-forward: signal today vs forward return, over the full history (overlapping windows)
    bt = {}
    fwd_sets = {h["key"]: h["td"] for h in HORIZONS}
    rv63 = c.pct_change().rolling(63).std() * math.sqrt(252)
    parts = [np.tanh((c / c.shift(n) - 1) / (rv63 * math.sqrt(n / 252))) for n in LOOKBACKS.values()]
    ser = pd.concat(parts, axis=1).mean(axis=1, skipna=False)
    exps = [(i, float(ser.iloc[i])) for i in range(260, len(c) - 1) if math.isfinite(ser.iloc[i])]
    for key, td in fwd_sets.items():
        rows = [(e, c.iloc[i + td] / c.iloc[i] - 1) for i, e in exps if i + td < len(c) and e is not None]
        if len(rows) < 30:
            continue
        longs = [r for e, r in rows if e > 0]
        shorts = [r for e, r in rows if e < 0]
        hits = sum((e > 0) == (r > 0) for e, r in rows)
        eff_n = len(rows) / td                     # overlapping windows: ~independent observations
        bt[key] = {"n": len(rows), "effective_n": eff_n, "reliable": eff_n >= 10, "hit_rate": hits / len(rows),
                   "avg_fwd_when_long_pct": float(np.mean(longs) * 100) if longs else None,
                   "avg_fwd_when_short_pct": float(np.mean(shorts) * 100) if shorts else None,
                   "up_rate_when_long": float(np.mean([r > 0 for r in longs])) if longs else None,
                   "up_rate_when_short": float(np.mean([r > 0 for r in shorts])) if shorts else None}
    cond = {}
    direction = "LONG" if expo > 0.1 else "SHORT" if expo < -0.1 else "FLAT"
    for key, b in bt.items():
        if direction == "FLAT":
            cond[key] = {"p_up_historical": None, "avg_fwd_pct_historical": None, "reason": "no trend signal (flat)"}
        elif not b["reliable"]:
            cond[key] = {"p_up_historical": None, "avg_fwd_pct_historical": None,
                         "reason": f"only ~{b['effective_n']:.0f} independent windows in history"}
        else:
            long_ = direction == "LONG"
            cond[key] = {"p_up_historical": b["up_rate_when_long"] if long_ else b["up_rate_when_short"],
                         "avg_fwd_pct_historical": b["avg_fwd_when_long_pct"] if long_ else b["avg_fwd_when_short_pct"]}
    return {"status": "fresh", "model": "time-series momentum (1/3/6/12m), tanh(z) average; CTA replication, not reported positions",
            "price": float(c.iloc[-1]), "exposure": expo, "position_vol_scaled": max(-2, min(2, expo * target_vol / rv60)) if rv60 else None,
            "direction": direction, "signals": sig,
            "sma": smas, "above_sma": {n: bool(c.iloc[-1] > v) for n, v in smas.items()}, "rv_60d": rv60,
            "sensitivity": sens, "conditional": cond, "backtest": bt, "history_days": int(len(c))}


# ============================================================ 4. positioning (CFTC COT + SLV trust)
def _cot_stats(dates, net, oi, price=None):
    s = pd.Series(net, index=pd.to_datetime(dates)).astype(float)
    pct_oi = s / pd.Series(oi, index=s.index).astype(float) * 100
    lo, hi = s.min(), s.max()
    out = {"latest_date": s.index[-1].date().isoformat(), "net": float(s.iloc[-1]), "net_pct_oi": float(pct_oi.iloc[-1]),
           "change_1w": float(s.iloc[-1] - s.iloc[-2]) if len(s) > 1 else None,
           "change_4w": float(s.iloc[-1] - s.iloc[-5]) if len(s) > 4 else None,
           "cot_index": float((s.iloc[-1] - lo) / (hi - lo) * 100) if hi > lo else None,
           "percentile": float((s < s.iloc[-1]).mean() * 100), "zscore": float((s.iloc[-1] - s.mean()) / s.std()) if s.std() else None,
           "weeks": int(len(s)), "series": [[d.date().isoformat(), float(v), float(p)] for d, v, p in zip(s.index, s, pct_oi)]}
    ci = out["cot_index"]
    out["read"] = ("crowded long (contrarian bearish)" if ci is not None and ci >= 90 else
                   "crowded short (contrarian bullish)" if ci is not None and ci <= 10 else "not extreme")
    if price is not None and len(price):
        px = _naive_index(price)
        rows = []
        idx = (s - lo) / (hi - lo) * 100 if hi > lo else s * 0
        for d, v in idx.items():
            p0 = px[px.index >= d]
            if len(p0) > 21:
                rows.append((v, p0.iloc[20] / p0.iloc[0] - 1))
        hi_r = [r for v, r in rows if v >= 80]
        lo_r = [r for v, r in rows if v <= 20]
        out["forward_4w"] = {"when_index_ge_80": {"n": len(hi_r), "avg_pct": float(np.mean(hi_r) * 100) if hi_r else None,
                                                  "up_rate": float(np.mean([r > 0 for r in hi_r])) if hi_r else None},
                             "when_index_le_20": {"n": len(lo_r), "avg_pct": float(np.mean(lo_r) * 100) if lo_r else None,
                                                  "up_rate": float(np.mean([r > 0 for r in lo_r])) if lo_r else None}}
    return out


def _complete_cot_rows(rows, fields):
    """CFTC's API omits a field when it is empty: keep only the rows that carry a number for every field used."""
    def ok(r):
        try:
            return all(r.get(f) not in (None, "") and np.isfinite(float(r[f])) for f in fields) \
                and bool(r.get("report_date_as_yyyy_mm_dd"))
        except (TypeError, ValueError):
            return False
    return [r for r in rows or [] if ok(r)]


def _cot_net(rows, long_f, short_f, close=None):
    """Net (long - short) positioning stats over the rows that carry both legs and open interest."""
    rows = _complete_cot_rows(rows, ("open_interest_all", long_f, short_f))
    if not rows:
        return None
    d = [r["report_date_as_yyyy_mm_dd"][:10] for r in rows]
    oi = [float(r["open_interest_all"]) for r in rows]
    net = [float(r[long_f]) - float(r[short_f]) for r in rows]
    return _cot_stats(d, net, oi, close)


def positioning(silver_rows, sp_rows, slv_trust, slv_history, si_close, spy_close):
    out = {"status": "fresh"}
    mm = _cot_net(silver_rows, "m_money_positions_long_all", "m_money_positions_short_all", si_close)
    pm = _cot_net(silver_rows, "prod_merc_positions_long", "prod_merc_positions_short")
    if mm and pm:
        out["silver"] = {"contract": "COMEX silver (084691)", "managed_money": mm, "producer_merchant": pm}
        sw = _cot_net(silver_rows, "swap_positions_long_all", "swap__positions_short_all")
        if sw:
            out["silver"]["swap_dealers"] = sw
    else:
        out["silver"] = {"status": "missing", "reason": "CFTC silver rows unavailable"}
    lev = _cot_net(sp_rows, "lev_money_positions_long", "lev_money_positions_short", spy_close)
    am = _cot_net(sp_rows, "asset_mgr_positions_long", "asset_mgr_positions_short", spy_close)
    if lev and am:
        out["sp500"] = {"contract": "E-mini S&P 500 (13874A)", "leveraged_funds": lev, "asset_managers": am}
        dl = _cot_net(sp_rows, "dealer_positions_long_all", "dealer_positions_short_all")
        if dl:
            out["sp500"]["dealers"] = dl
    else:
        out["sp500"] = {"status": "missing", "reason": "CFTC S&P rows unavailable"}
    t = dict(slv_trust or {})
    hist = sorted(slv_history or [], key=lambda x: x[0])
    if t.get("ounces_in_trust") and hist:
        prev = [h for h in hist if h[0] < t.get("as_of", "9999")]
        if prev:
            t["change_vs_prev_oz"] = t["ounces_in_trust"] - prev[-1][1]
            t["prev_date"] = prev[-1][0]
        m30 = [h for h in hist if h[0] <= (date.fromisoformat(t["as_of"]) - timedelta(days=30)).isoformat()] if t.get("as_of") else []
        if m30:
            t["change_30d_oz"] = t["ounces_in_trust"] - m30[-1][1]
        t["history"] = hist[-120:]
    out["slv_trust"] = t
    return out


# ============================================================ 5. silver fair value
def silver_fair_value(frames, dfii10, indpro):
    need = {k: _naive_index(_closes(frames.get(k))) for k in ("SI_F", "GC_F", "DXY", "HG_F")}
    short = [k for k, v in need.items() if len(v) < 200]
    if short or not dfii10:
        why = (f"insufficient price history: {', '.join(short)}" if short else "") + \
              ("; " if short and not dfii10 else "") + ("10y real yield (FRED DFII10) unavailable this run" if not dfii10 else "")
        return {"status": "missing", "reason": why}
    ry = pd.Series({pd.Timestamp(o["date"]): o["value"] for o in dfii10}).sort_index()
    ip = pd.Series({pd.Timestamp(o["date"]): o["value"] for o in (indpro or [])}).sort_index()
    df = pd.DataFrame({"si": need["SI_F"], "gc": need["GC_F"], "dxy": need["DXY"], "hg": need["HG_F"]}).dropna()
    df["ry"] = ry.reindex(df.index, method="ffill")
    if len(ip):
        df["ip"] = ip.reindex(df.index, method="ffill")
    df = df.dropna()
    wk = df.resample("W-FRI").last().dropna().tail(156)            # 3 years, weekly
    if len(wk) < 60:
        return {"status": "missing", "reason": "fewer than 60 aligned weeks"}
    X = pd.DataFrame({"log_gold": np.log(wk["gc"]), "real_yield_10y": wk["ry"], "log_dxy": np.log(wk["dxy"]),
                      "log_copper": np.log(wk["hg"])})
    if "ip" in wk:
        X["log_indpro"] = np.log(wk["ip"])
    y = np.log(wk["si"])
    A = np.column_stack([np.ones(len(X)), X.values])
    beta, *_ = np.linalg.lstsq(A, y.values, rcond=None)
    fit = A @ beta
    resid = y.values - fit
    s2 = resid.var(ddof=A.shape[1])
    cov = s2 * np.linalg.pinv(A.T @ A)
    tstat = beta / np.sqrt(np.diag(cov))
    r2 = 1 - resid.var() / y.values.var()
    # today's fair value with today's (daily) inputs
    last = df.iloc[-1]
    xl = [1, math.log(last["gc"]), last["ry"], math.log(last["dxy"]), math.log(last["hg"])] + ([math.log(last["ip"])] if "ip" in wk else [])
    fair = math.exp(float(np.array(xl) @ beta))
    res_now = math.log(last["si"]) - math.log(fair)
    sd = float(np.std(resid))
    phi = float(np.corrcoef(resid[:-1], resid[1:])[0, 1])
    hl = -math.log(2) / math.log(phi) if 0 < phi < 1 else None
    path = {}
    for key, weeks in (("1w", 1), ("1m", 4.3), ("1y", 52)):
        exp_res = res_now * (phi ** weeks) if 0 < phi < 1 else res_now
        path[key] = {"implied_price_if_drivers_unchanged": float(last["si"] * math.exp(exp_res - res_now)),
                     "expected_gap_close_pct": (1 - (phi ** weeks if 0 < phi < 1 else 1)) * 100}
    # mean-reversion backtest: sign(-residual) vs next 4-week silver return
    hits, rows = 0, 0
    for i in range(len(resid) - 4):
        r4 = wk["si"].iloc[i + 4] / wk["si"].iloc[i] - 1
        if abs(resid[i]) > sd:
            rows += 1
            hits += (resid[i] < 0) == (r4 > 0)
    gsr = (need["GC_F"] / need["SI_F"]).dropna().tail(1260)
    names = ["const"] + list(X.columns)
    return {"status": "fresh", "model": "weekly OLS of log(silver) on log(gold), 10y TIPS real yield, log(DXY), log(copper), log(industrial production); 3y window",
            "caveat": "levels regression on trending series: a relative-value gauge, not a causal model",
            "price": float(last["si"]), "fair_value": fair, "gap_pct": (float(last["si"]) / fair - 1) * 100,
            "residual_z": res_now / sd if sd else None, "r2": float(r2), "half_life_weeks": hl,
            "coefficients": {n: {"beta": float(b), "t": float(t)} for n, b, t in zip(names, beta, tstat)},
            "inputs": {"gold": float(last["gc"]), "real_yield_10y": float(last["ry"]), "dxy": float(last["dxy"]),
                       "copper": float(last["hg"]), "industrial_production": float(last["ip"]) if "ip" in wk else None},
            "path": path, "series": [[d.date().isoformat(), float(a), float(math.exp(f))] for d, a, f in zip(wk.index, wk["si"], fit)],
            "gold_silver_ratio": {"latest": float(gsr.iloc[-1]), "mean_5y": float(gsr.mean()),
                                  "zscore_5y": float((gsr.iloc[-1] - gsr.mean()) / gsr.std()),
                                  "percentile_5y": float((gsr < gsr.iloc[-1]).mean() * 100)},
            "backtest": {"signal": "|residual| > 1 sd: expect reversion over next 4 weeks", "n": rows,
                         "hit_rate": hits / rows if rows else None}}


# ============================================================ 6. macro regime
def macro_regime(fred):
    def last(series, n=1):
        return [o["value"] for o in (fred.get(series) or [])[:n]]
    t10y3m = fred.get("T10Y3M") or []
    if not t10y3m:
        return {"status": "missing", "reason": "T10Y3M unavailable"}
    spread_m = float(np.mean([o["value"] for o in t10y3m[:21]]))        # NY Fed uses the monthly average
    prob = float(norm.cdf(-0.5333 - 0.6330 * spread_m))
    yr = [o for o in t10y3m if o["date"] <= (date.fromisoformat(t10y3m[0]["date"]) - timedelta(days=365)).isoformat()][:21]
    prob_1y = float(norm.cdf(-0.5333 - 0.6330 * np.mean([o["value"] for o in yr]))) if yr else None
    nfci = fred.get("NFCI") or []
    sahm = fred.get("SAHMREALTIME") or []
    t10y2y = fred.get("T10Y2Y") or []
    oas = fred.get("BAMLH0A0HYM2") or []
    ry = fred.get("DFII10") or []

    def chg(obs, n):
        return obs[0]["value"] - obs[n]["value"] if len(obs) > n else None
    nf = nfci[0]["value"] if nfci else None
    sm = sahm[0]["value"] if sahm else None
    o = oas[0]["value"] if oas else None
    flags = []
    if nf is not None and nf > 0:
        flags.append("financial conditions tighter than average (NFCI > 0)")
    if sm is not None and sm >= 0.5:
        flags.append("Sahm rule triggered (>= 0.5)")
    if o is not None and o > 5:
        flags.append("high-yield spreads above 5%")
    if spread_m < 0:
        flags.append("yield curve (10y-3m) inverted")
    if any("Sahm" in f or "spreads" in f for f in flags) or (nf is not None and nf > 0.5):
        regime = "STRESS / CONTRACTION RISK"
    elif prob > 0.3 or spread_m < 0:
        regime = "LATE CYCLE"
    else:
        regime = "EXPANSION"
    trend = None
    if len(nfci) > 13:
        trend = "tightening" if nfci[0]["value"] > nfci[13]["value"] else "easing"
    return {"status": "fresh", "regime": regime, "conditions_trend_3m": trend, "flags": flags,
            "recession_prob_12m": prob, "recession_prob_12m_year_ago": prob_1y,
            "model": "NY Fed probit (Estrella-Mishkin): P = Phi(-0.5333 - 0.6330 x monthly-avg 10y-3m spread)",
            "spread_10y3m_monthly_avg": spread_m, "spread_10y3m": t10y3m[0]["value"],
            "spread_10y2y": t10y2y[0]["value"] if t10y2y else None, "nfci": nf, "nfci_change_13w": chg(nfci, 13),
            "sahm": sm, "hy_oas": o, "hy_oas_change_3m": chg(oas, 63), "real_yield_10y": ry[0]["value"] if ry else None,
            "real_yield_change_3m": chg(ry, 63), "as_of": t10y3m[0]["date"],
            "history": {"t10y3m": [[x["date"], x["value"]] for x in t10y3m[:260][::-1]],
                        "nfci": [[x["date"], x["value"]] for x in nfci[:156][::-1]]}}


# ============================================================ 7. calendar effects
def _third_friday(y, m):
    d = date(y, m, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def calendar_effects(df, fomc_dates, cpi_dates, today):
    c = _naive_index(_closes(df))
    if len(c) < 500:
        return {"status": "missing", "reason": "need >= 500 daily closes"}
    r = c.pct_change().dropna()
    days = list(r.index.date)
    pos = {d: i for i, d in enumerate(days)}
    base_mean = float(r.mean() * 100)
    base_abs = float(r.abs().mean() * 100)

    def stats(vals, baseline):
        v = np.array(vals) * 100
        if len(v) < 5:
            return {"n": int(len(v))}
        t = (v.mean() - baseline) / (v.std(ddof=1) / math.sqrt(len(v))) if v.std(ddof=1) else None
        return {"n": int(len(v)), "mean_pct": float(v.mean()), "median_pct": float(np.median(v)),
                "hit_rate": float((v > 0).mean()), "avg_abs_pct": float(np.abs(v).mean()), "t_vs_baseline": t}
    eff = {}
    fd = [date.fromisoformat(x) for x in fomc_dates or []]
    fomc_day = [r.iloc[pos[d]] for d in fd if d in pos]
    pre_fomc = [r.iloc[pos[d] - 1] for d in fd if d in pos and pos[d] > 0]
    eff["fomc_day"] = {**stats(fomc_day, base_mean), "definition": "close before to close of decision day"}
    eff["day_before_fomc"] = {**stats(pre_fomc, base_mean), "definition": "close D-2 to close D-1 (daily proxy for the pre-FOMC drift)"}
    cd = [date.fromisoformat(x) for x in cpi_dates or []]
    cpi = [r.iloc[pos[d]] for d in cd if d in pos]
    eff["cpi_day"] = {**stats(cpi, base_mean), "avg_abs_vs_normal": (np.mean(np.abs(cpi)) * 100 / base_abs) if cpi else None,
                      "definition": "CPI release day close-to-close"}
    # OPEX week (Mon-Fri week containing the 3rd Friday) vs all weeks
    wk = c.resample("W-FRI").last().pct_change().dropna()
    opex = [v for d, v in wk.items() if d.date() == _third_friday(d.year, d.month)]
    eff["opex_week"] = {**stats(opex, float(wk.mean() * 100)), "baseline_week_mean_pct": float(wk.mean() * 100),
                        "definition": "weekly return of the monthly-expiration week"}
    # turn of month: last trading day + first 3
    tom = []
    months = pd.Series(r.index, index=r.index).groupby([r.index.year, r.index.month])
    keys = list(months.groups.keys())
    for a, b in zip(keys[:-1], keys[1:]):
        last = months.get_group(a).iloc[-1]
        first3 = months.get_group(b).iloc[:3]
        sel = [last] + list(first3)
        tom.append(float(np.prod([1 + r.loc[x] for x in sel]) - 1))
    four = (1 + r).rolling(4).apply(np.prod, raw=True).dropna() - 1
    eff["turn_of_month"] = {**stats(tom, float(four.mean() * 100)), "baseline_4d_mean_pct": float(four.mean() * 100),
                            "definition": "last trading day + first 3 trading days of the next month"}
    dow = {name: stats([v for d, v in r.items() if d.weekday() == i], base_mean)
           for i, name in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri"])}
    # upcoming flags within 7 days
    horizon = today + timedelta(days=7)
    nxt_fomc = next((d for d in fd if d >= today), None)
    nxt_cpi = next((d for d in cd if d >= today), None)
    nxt_opex = _third_friday(today.year, today.month)
    if nxt_opex < today:
        m = today.month % 12 + 1
        nxt_opex = _third_friday(today.year + (today.month == 12), m)
    end_month = (date(today.year + (today.month == 12), today.month % 12 + 1, 1) - timedelta(days=1))
    upcoming = []
    exp_drift = 0.0
    if nxt_fomc and nxt_fomc <= horizon:
        upcoming.append({"event": "FOMC decision", "date": nxt_fomc.isoformat(), "effect": "day_before_fomc + fomc_day"})
        exp_drift += (eff["day_before_fomc"].get("mean_pct") or 0) + (eff["fomc_day"].get("mean_pct") or 0)
    if nxt_cpi and nxt_cpi <= horizon:
        upcoming.append({"event": "CPI release", "date": nxt_cpi.isoformat(), "effect": "cpi_day (volatility)"})
    if today <= nxt_opex <= horizon + timedelta(days=4):
        upcoming.append({"event": "Monthly OPEX", "date": nxt_opex.isoformat(), "effect": "opex_week"})
        exp_drift += (eff["opex_week"].get("mean_pct") or 0) - eff["opex_week"].get("baseline_week_mean_pct", 0)
    if end_month - today <= timedelta(days=7):
        upcoming.append({"event": "Turn of month", "date": end_month.isoformat(), "effect": "turn_of_month"})
        exp_drift += (eff["turn_of_month"].get("mean_pct") or 0) - eff["turn_of_month"].get("baseline_4d_mean_pct", 0)
    return {"status": "fresh", "history_days": int(len(r)), "baseline_daily_mean_pct": base_mean,
            "effects": eff, "day_of_week": dow, "upcoming_7d": upcoming,
            "next": {"fomc": nxt_fomc.isoformat() if nxt_fomc else None, "cpi": nxt_cpi.isoformat() if nxt_cpi else None,
                     "opex": nxt_opex.isoformat()},
            "calendar_drift_next_7d_pct": exp_drift if upcoming else 0.0,
            "note": "historical averages over the available history; small samples are noisy (see n and t)"}


# ============================================================ 8. mechanical flows
LEVERED = {"SPY": {"SSO": 2, "UPRO": 3, "SPXL": 3, "SH": -1, "SDS": -2, "SPXU": -3, "SPXS": -3},
           "SLV": {"AGQ": 2, "ZSL": -2}}
VOL_CONTROL_AUM = 400e9          # midpoint of the commonly cited $300-500B equity notional in vol-control strategies


def mechanical_flows(symbol, df, aum, gex, trend, spot):
    c = _closes(df)
    if len(c) < 80:
        return {"status": "missing", "reason": "insufficient history"}
    r = c.pct_change().dropna()
    adv = float((df["Close"] * df["Volume"]).tail(20).mean()) if "Volume" in df else None
    out = {"status": "fresh", "avg_daily_dollar_volume_20d": adv}
    lev = {}
    tot = 0.0
    for t, L in LEVERED.get(symbol, {}).items():
        a = aum.get(t)
        if a:
            lev[t] = {"leverage": L, "aum": a, "flow_per_1pct": a * L * (L - 1) * 0.01}
            tot += a * L * (L - 1)
    last_r = float(r.iloc[-1])
    out["leveraged_etfs"] = {"funds": lev, "flow_per_1pct_move": tot * 0.01, "last_session_return_pct": last_r * 100,
                             "last_session_rebalance": tot * last_r,
                             "pct_of_adv_per_1pct": (tot * 0.01 / adv * 100) if adv else None,
                             "note": "end-of-day rebalance = sum AUM x L(L-1) x return; buys after up days, sells after down days"}
    if symbol == "SPY":
        def expo(rets):
            rv = max(rets.tail(21).std(), rets.tail(63).std()) * math.sqrt(252)
            return min(0.10 / rv, 1.5) if rv else 1.5, rv
        e0, rv0 = expo(r)
        e5, _ = expo(r.iloc[:-5])
        e21, _ = expo(r.iloc[:-21])
        scen = {}
        for mv in (-0.03, -0.02, -0.01, 0.01, 0.02, 0.03):
            e1, _ = expo(pd.concat([r, pd.Series([mv])], ignore_index=True))
            scen[f"{mv * 100:+.0f}%"] = (e1 - e0) * VOL_CONTROL_AUM
        out["vol_control"] = {"target_vol": 0.10, "leverage_cap": 1.5, "realized_vol_used": rv0, "exposure": e0,
                              "exposure_5d_ago": e5, "exposure_21d_ago": e21,
                              "flow_last_5d": (e0 - e5) * VOL_CONTROL_AUM, "flow_last_21d": (e0 - e21) * VOL_CONTROL_AUM,
                              "flow_if_tomorrow": scen, "aum_assumption": VOL_CONTROL_AUM,
                              "note": "exposure = min(10% / max(1m,3m realized vol), 1.5); AUM is an industry estimate"}
    ng = (gex or {}).get("net_gex")
    if ng is not None and spot:
        out["dealer_gamma"] = {"net_gex_usd_per_1pt": ng, "hedge_flow_per_1pct": -ng * spot * 0.01,
                               "regime": "long gamma: dealers sell rallies / buy dips (dampening)" if ng > 0 else
                               "short gamma: dealers buy rallies / sell dips (amplifying)",
                               "zero_gamma": (gex or {}).get("zero_gamma")}
    if trend and trend.get("status") == "fresh":
        out["cta"] = {"exposure": trend["exposure"], "exposure_after_move": trend["sensitivity"],
                      "note": "replicated trend exposure (-1..+1); change = direction of systematic trend flow"}
    return out


# ============================================================ assembly
def build(inputs):
    """inputs: dict with today, spot{SPY,SLV}, chains{SPY,SLV}, frames, gex{SPY,SLV}, fred{...}, cot{silver,sp},
    slv_trust, slv_trust_history, fomc, cpi, aum{ticker: total_assets}."""
    today = inputs["today"]
    fr = inputs["frames"]
    out = {"version": VERSION, "horizons": HORIZONS, "generated_for": today.isoformat()}
    for sym, key in (("SPY", "SPY"), ("SLV", "SLV")):
        spot = inputs["spot"].get(sym)
        gx = inputs["gex"].get(sym) or {}
        levels = {"call_wall": gx.get("call_wall"), "put_wall": gx.get("put_wall"), "zero_gamma": gx.get("zero_gamma")}
        closes = _closes(fr.get(key))
        prior = closes[closes.index.date < today]
        imp = implied_distribution(inputs["chains"].get(sym) or {}, spot, today, levels,
                                   prior_spot=float(prior.iloc[-1]) if len(prior) else None)
        vol = vol_forecast(fr.get(key), imp, spot)
        tr = trend_model(fr.get(key))
        cal = calendar_effects(fr.get(key), inputs.get("fomc"), inputs.get("cpi"), today)
        flows = mechanical_flows(sym, fr.get(key), inputs.get("aum", {}), gx, tr, spot)
        out[sym] = {"spot": spot, "implied": imp, "vol_forecast": vol, "trend": tr, "calendar": cal, "flows": flows}
    out["positioning"] = positioning(inputs.get("cot", {}).get("silver"), inputs.get("cot", {}).get("sp"),
                                     inputs.get("slv_trust"), inputs.get("slv_trust_history"),
                                     _closes(fr.get("SI_F")), _closes(fr.get("SPY")))
    out["silver_fair_value"] = silver_fair_value(fr, inputs["fred"].get("DFII10"), inputs["fred"].get("INDPRO"))
    out["macro_regime"] = macro_regime(inputs["fred"])
    out["consensus"] = {sym: consensus(out, sym) for sym in ("SPY", "SLV")}
    return out


def consensus(fc, sym):
    """Per-horizon summary used by the scorecard: market-implied odds, model direction and ranges."""
    s = fc[sym]
    res = {}
    for h in HORIZONS:
        k = h["key"]
        imp = (s["implied"].get("horizons") or {}).get(k) or {}
        vol = (s["vol_forecast"].get("horizons") or {}).get(k) or {}
        tr = (s["trend"].get("conditional") or {}).get(k) or {}
        res[k] = {"implied_p_up": imp.get("p_up"), "implied_range68": imp.get("range68"), "implied_median": imp.get("median"),
                  "har_vol": vol.get("forecast_vol"), "har_move_1sd": vol.get("expected_move_1sd"),
                  "trend_direction": s["trend"].get("direction"), "trend_p_up_hist": tr.get("p_up_historical")}
    return res


# ============================================================ 9. scorecard
from core.lake import FORECASTS_DDL as SCORE_DDL  # noqa: E402


def forecast_rows(ctx):
    fc = ctx.get("forecast") or {}
    if not fc:
        return []
    run = ctx["run"]
    created = run["generated_at"]
    day0 = date.fromisoformat(created[:10])
    rows = []
    for sym in ("SPY", "SLV"):
        s = fc.get(sym) or {}
        spot = s.get("spot")
        if not spot:
            continue
        for h in HORIZONS:
            k = h["key"]
            tgt = (day0 + timedelta(days=h["days"])).isoformat()
            imp = (s.get("implied", {}).get("horizons") or {}).get(k) or {}
            if imp.get("p_up") is not None:
                rows.append((run["run_id"], created, sym, "implied", k, tgt, spot, imp["p_up"], imp["range68"][0],
                             imp["range68"][1], "UP" if imp["p_up"] > 0.5 else "DOWN", imp.get("atm_iv")))
            vol = (s.get("vol_forecast", {}).get("horizons") or {}).get(k) or {}
            if vol.get("expected_move_1sd"):
                m = vol["expected_move_1sd"]
                rows.append((run["run_id"], created, sym, "har_vol", k, tgt, spot, None, spot - m, spot + m, None,
                             vol["forecast_vol"]))
            tr = s.get("trend") or {}
            cond = (tr.get("conditional") or {}).get(k) or {}
            if tr.get("direction") in ("LONG", "SHORT"):
                rows.append((run["run_id"], created, sym, "trend", k, tgt, spot, cond.get("p_up_historical"), None, None,
                             "UP" if tr["direction"] == "LONG" else "DOWN", None))
        cal = s.get("calendar") or {}
        if cal.get("upcoming_7d") and cal.get("calendar_drift_next_7d_pct"):
            rows.append((run["run_id"], created, sym, "calendar", "1w", (day0 + timedelta(days=7)).isoformat(), spot, None,
                         None, None, "UP" if cal["calendar_drift_next_7d_pct"] > 0 else "DOWN", None))
    pos = fc.get("positioning") or {}
    for sym, grp, key in (("SLV", "silver", "managed_money"), ("SPY", "sp500", "leveraged_funds")):
        st = (pos.get(grp) or {}).get(key) or {}
        spot = (fc.get(sym) or {}).get("spot")
        if spot and st.get("cot_index") is not None and (st["cot_index"] >= 90 or st["cot_index"] <= 10):
            rows.append((run["run_id"], created, sym, "positioning", "1m", (day0 + timedelta(days=30)).isoformat(), spot,
                         None, None, None, "DOWN" if st["cot_index"] >= 90 else "UP", None))
    fv = fc.get("silver_fair_value") or {}
    spot = (fc.get("SLV") or {}).get("spot")
    if spot and fv.get("residual_z") is not None and abs(fv["residual_z"]) >= 1:
        for k, days in (("1m", 30), ("1y", 365)):
            rows.append((run["run_id"], created, "SLV", "fair_value", k, (day0 + timedelta(days=days)).isoformat(), spot,
                         None, None, None, "DOWN" if fv["residual_z"] > 0 else "UP", None))
    return rows


def record_forecasts(conn, ctx):
    from core import lake
    rows = forecast_rows(ctx)
    with lake._WRITE_LOCK, conn:
        conn.execute(SCORE_DDL)
        conn.executemany("""INSERT OR IGNORE INTO v2_forecasts (run_id, created_at, symbol, model, horizon, target_date,
                            spot, p_up, lower68, upper68, direction, vol) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    return len(rows)


def scorecard(conn, closes_fn, symbol):
    """Grade logged forecasts whose target date has passed. closes_fn(sym) -> daily close Series (naive index)."""
    conn.execute(SCORE_DDL)
    rows = [dict(r) for r in conn.execute("SELECT * FROM v2_forecasts WHERE symbol = ? ORDER BY created_at", (symbol,))]
    rows = list({(r["created_at"][:10], r["model"], r["horizon"]): r for r in rows}.values())    # one forecast a day: its last run
    c = closes_fn(symbol)
    last_day = c.index[-1].date() if c is not None and len(c) else None
    agg, pending = {}, 0
    for r in rows:
        tgt = date.fromisoformat(r["target_date"])
        if last_day is None or tgt > last_day:
            pending += 1
            continue
        after = c[c.index >= pd.Timestamp(tgt)]
        if not len(after):
            pending += 1
            continue
        px = float(after.iloc[0])
        up = px > r["spot"]
        a = agg.setdefault((r["model"], r["horizon"]), {"n": 0, "dir_hits": 0, "dir_n": 0, "in68": 0, "rng_n": 0, "brier": []})
        a["n"] += 1
        if r["direction"]:
            a["dir_n"] += 1
            a["dir_hits"] += (r["direction"] == "UP") == up
        if r["lower68"] is not None and r["upper68"] is not None:
            a["rng_n"] += 1
            a["in68"] += r["lower68"] <= px <= r["upper68"]
        if r["p_up"] is not None:
            a["brier"].append((r["p_up"] - (1.0 if up else 0.0)) ** 2)
    table = []
    for (m, h), a in sorted(agg.items()):
        table.append({"model": m, "horizon": h, "n": a["n"],
                      "hit_rate": a["dir_hits"] / a["dir_n"] if a["dir_n"] else None,
                      "coverage68": a["in68"] / a["rng_n"] if a["rng_n"] else None,
                      "brier": float(np.mean(a["brier"])) if a["brier"] else None})
    return {"graded": table, "pending": pending, "logged": len(rows), "last_price_date": last_day.isoformat() if last_day else None,
            "note": "one forecast per day (the day's last run); hit_rate: direction correct; coverage68: share inside the 68% range "
                    "(ideal 0.68); brier: lower is better (0.25 = coin flip)"}


# ============================================================ 0. signal watch
def _edge(p, n, base=0.5):
    """Evidence gate for a rule: 'tested' | 'thin' | 'none' from its historical win rate p over n cases."""
    # ponytail: fixed cut-offs (5 points over base, 30 cases), no significance test. Swap in the live scorecard
    # hit rates once each rule has a few dozen graded forecasts.
    if p is None or not n or p - base < 0.05:
        return "none"
    return "tested" if n >= 30 else "thin"


_SIZE = {"tested": "standard size", "thin": "half size (small sample)"}


def _fv_sd(fv):
    """1 sd of the fair-value regression residual, recovered from the stored z-score (None when z is ~0)."""
    z = fv.get("residual_z")
    return math.log(fv["price"] / fv["fair_value"]) / z if z and abs(z) > 1e-6 and fv.get("fair_value") else None


def live_overlay(fc, quotes):
    """Copy of a run's forecast with the price-driven inputs of signal_watch moved to live quotes
    ({"SPY": px, "SLV": px, "SI_F": px}; a missing quote leaves that input as of the run). Levels computed by the run
    (fair value, flip levels, zero gamma) and the slow inputs (positioning, volatility, macro) are not recomputed."""
    import copy
    fc = copy.deepcopy(fc)
    fv, sd = fc.get("silver_fair_value") or {}, None
    if quotes.get("SI_F") and fv.get("price"):
        sd = _fv_sd(fv)
    if sd:
        fv["price"], fv["residual_z"] = quotes["SI_F"], math.log(quotes["SI_F"] / fv["fair_value"]) / sd
    for sym in ("SPY", "SLV"):
        s, px = fc.get(sym) or {}, quotes.get(sym)
        if not px or not s.get("spot"):
            continue
        s["spot"] = px
        m1 = ((s.get("trend") or {}).get("signals") or {}).get("1m") or {}
        if m1.get("flip_level"):
            m1["signal"], m1["flip_distance_pct"] = "LONG" if px > m1["flip_level"] else "SHORT", (m1["flip_level"] / px - 1) * 100
        dg = (s.get("flows") or {}).get("dealer_gamma") or {}
        if dg.get("zero_gamma"):
            # ponytail: live price vs the run's zero-gamma level stands in for the sign of net gamma between runs
            dg["regime"] = ("long" if px > dg["zero_gamma"] else "short") + " gamma (live price vs the run's zero-gamma level)"
    return fc


def _signal_row(asset, name, where, what, now, trigger, fired, bias, action, evidence, edge):
    tradeable = fired and bias in ("bullish", "bearish") and edge in _SIZE
    return {"asset": asset, "name": name, "where": where, "what": what, "now": now, "trigger": trigger,
            "fired": bool(fired), "bias": bias if fired else None, "edge": edge, "evidence": evidence, "plan": action,
            "action": "Wait" if not fired else f"{action} · {_SIZE[edge]}" if tradeable else
                      action if bias == "structure" else f"No trade on this alone ({evidence}); use as confirmation"}


def signal_watch(fc, ticket=None):
    """Rule-based triggers read off the other cards: current reading, firing level, state and the action when fired.
    Pure function of one run's forecast dict (no model, no network). ticket = the engine's latest SPY row (optional)."""
    rows = []

    def add(*a):
        rows.append(_signal_row(*a))

    def structure(vol, bullish):          # spreads when options are rich, outright when cheap or fair
        read = ((vol.get("horizons") or {}).get("1m") or {}).get("read") or ""
        return ("call" if bullish else "put") + (" spreads" if "rich" in read else "s")

    if ticket:
        cash = ticket["position_type"] == "CASH"
        add("SPY", "Execution engine", "Engine Positions card (terminal) · table v2_trade_signals",
            "The engine's SPY ticket from the latest pipeline run: score, bias and the contract it selected.",
            ticket.get("bias") or "—", "score reaches the call or put trigger", not cash,
            "bullish" if ticket["position_type"] == "CALL" else "bearish",
            "Stay in cash" if cash else f"Buy SPY {ticket['expiration']} {ticket['strike']:g} {ticket['position_type'].lower()}"
            + (f" near {ticket['entry_mid']:.2f}" if ticket.get("entry_mid") else ""),
            "live track record is on the Engine Positions card", "thin")

    fv = fc.get("silver_fair_value") or {}
    z, slv = fv.get("residual_z"), fc.get("SLV") or {}
    if z is not None and fv.get("fair_value") and slv.get("spot"):
        sd = _fv_sd(fv)
        lo, hi = (fv["fair_value"] * math.exp(-sd), fv["fair_value"] * math.exp(sd)) if sd else (None, None)
        bt, bull = fv.get("backtest") or {}, z < 0
        add("SLV", "Fair-value reversion", "Card 5 · Silver fair value (z-score, gap, backtest)",
            "Silver vs the value implied by gold, real yields, the dollar, copper and industrial production. "
            "At 1 standard deviation away it has tended to move back toward fair value within 4 weeks.",
            f"z {z:+.2f} · silver {fv['price']:.2f} vs fair {fv['fair_value']:.2f}",
            f"z ≤ −1 (silver ≤ {lo:.2f}) or z ≥ +1 (≥ {hi:.2f})" if sd else "z ≤ −1 or z ≥ +1", abs(z) >= 1,
            "bullish" if bull else "bearish",
            (f"Buy SLV {structure(slv.get('vol_forecast') or {}, True)} 45–60 DTE near {slv['spot']:.0f}, or physical metal"
             if bull else f"Buy SLV {structure(slv.get('vol_forecast') or {}, False)} 45–60 DTE near {slv['spot']:.0f}; pause physical buys")
            + "; exit at fair value or after 4 weeks",
            f"{bt['hit_rate']:.0%} reverted within 4 weeks, n={bt['n']}" if bt.get("hit_rate") is not None else "no backtest",
            _edge(bt.get("hit_rate"), bt.get("n")))

    for sym, grp, who in (("SLV", "silver", "managed_money"), ("SPY", "sp500", "leveraged_funds")):
        s = fc.get(sym) or {}
        spot, vol, tr = s.get("spot"), s.get("vol_forecast") or {}, s.get("trend") or {}
        if not spot:
            continue
        m1, bt = (tr.get("signals") or {}).get("1m") or {}, (tr.get("backtest") or {}).get("1m") or {}
        if m1.get("flip_level"):
            up = m1["signal"] == "LONG"
            add(sym, "1-month momentum", "Card 3 · Trend / CTA model (flip levels, 1M row)",
                "Sign of the 1-month return. The flip level is the close at which it changes sign on the next session.",
                f"{m1['signal']} · {spot:.2f} is {abs(m1['flip_distance_pct']):.1f}% {'above' if up else 'below'} the flip",
                f"close above {m1['flip_level']:.2f}", up, "bullish",
                f"Buy {sym} {structure(vol, True)} 30–45 DTE near {spot:.0f}",
                f"1M trend test: up {bt['up_rate_when_long']:.0%} of the time when long vs {bt['up_rate_when_short']:.0%} when short"
                if bt.get("up_rate_when_long") is not None else "no backtest",
                _edge(bt.get("up_rate_when_long"), bt.get("effective_n"), bt.get("up_rate_when_short") or 0.5))
        st = ((fc.get("positioning") or {}).get(grp) or {}).get(who) or {}
        if st.get("cot_index") is not None:
            ci = st["cot_index"]
            b = (st.get("forward_4w") or {}).get("when_index_le_20" if ci < 50 else "when_index_ge_80") or {}
            p, avg = b.get("up_rate"), b.get("avg_pct") or 0
            bull = p is not None and avg > 0 and p >= 0.5
            add(sym, f"Positioning ({who.replace('_', ' ')})", "Card 4 · Positioning (COT index, forward-return note)",
                "CFTC net position on a 0–100 scale over 3 years: 0 = most short, 100 = most long. "
                "The action follows what the price actually did in the 4 weeks after past extremes.",
                f"index {ci:.0f}", "index ≤ 20 or ≥ 80", ci <= 20 or ci >= 80, "bullish" if bull else "bearish",
                f"Buy {sym} {structure(vol, bull)} 30–45 DTE near {spot:.0f}" + ("; physical metal also fits" if bull and sym == "SLV" else "")
                + "; 4-week hold",
                f"4 weeks after index {'≤ 20' if ci < 50 else '≥ 80'}: up {p:.0%}, avg {avg:+.1f}%, n={b['n']}" if p is not None else "no history",
                _edge(p if bull else None if p is None else 1 - p, b.get("n")))
        h, hb = (vol.get("horizons") or {}).get("1m") or {}, (vol.get("backtest") or {}).get("1m") or {}
        if h.get("implied_to_forecast"):
            rich, beats = "rich" in h["read"], (hb.get("rmse_improvement_vs_naive_pct") or 0) > 0
            add(sym, "Option pricing (1M)", "Card 2 · Volatility forecast (implied ÷ forecast)",
                "Implied volatility divided by the model's forecast of realised volatility. It picks the structure, not the direction.",
                f"implied {h['implied_vol']:.0%} ÷ forecast {h['forecast_vol']:.0%} = {h['implied_to_forecast']:.2f}",
                "ratio < 0.90 (cheap) or > 1.15 (rich)", h["read"] != "fairly priced", "structure" if beats else "none",
                "Options are rich: use spreads, avoid outright long options" if rich else
                "Options are cheap: buy calls or puts outright instead of spreads",
                f"model {'beats' if beats else 'does not beat'} a naive forecast ({hb.get('rmse_improvement_vs_naive_pct', 0):+.1f}% RMSE)", "none")
        dg = (s.get("flows") or {}).get("dealer_gamma") or {}
        if dg.get("zero_gamma"):
            add(sym, "Dealer gamma", "Card 8 · Mechanical flows (dealer gamma, zero-gamma level)",
                "Above zero gamma dealers trade against moves (quiet, pinned). Below it they trade with moves (fast, trending).",
                f"{spot:.2f} vs zero gamma {dg['zero_gamma']:.2f} ({(spot / dg['zero_gamma'] - 1) * 100:+.1f}%)",
                f"net gamma turns negative (near {dg['zero_gamma']:.2f})", dg.get("regime", "").startswith("short"), "structure",
                "Moves amplify: cut size, take profits sooner, do not sell premium", "convention-based estimate, not backtested", "none")

    mr = fc.get("macro_regime") or {}
    if mr.get("regime"):
        add("ALL", "Macro regime", "Card 6 · Macro regime (flags)",
            "Recession probability, financial conditions, Sahm rule, credit spreads and the yield curve. Any flag is a risk-off warning.",
            f"{mr['regime']} · recession 12m {mr.get('recession_prob_12m') or 0:.0%}", "any flag raised", bool(mr.get("flags")), "structure",
            "Risk-off: halve call size and wait for trend confirmation (" + "; ".join(mr.get("flags") or []) + ")",
            "regime filter, not backtested", "none")
    rows.sort(key=lambda r: (not r["fired"], r["asset"]))      # fired first, then grouped by asset
    return rows
