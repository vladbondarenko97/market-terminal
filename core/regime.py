"""R · Market Regime / Shock Reader: one regime from stored readings, a forced-selling gauge, the playbook conditions and a short read.

Pure functions over one run's blocks (prices, forecast, chaos, fed_ops, bitcoin) plus the previous run's regime. Nothing here
fetches; `write_read()` is the only function that calls a model, and only the pipeline calls it. A reading that is not in the
inputs is `missing` with a reason, never a default. Conditions, not advice: the playbook rows state Vlad's tranche rules as
true or false and say nothing about what to buy.
"""
import json
import math
import re
import subprocess
from datetime import datetime, timezone

REGIME_VERSION = "regime_v1"

# ---- thresholds (every one is printed next to its reading in the block, so the card never hides a cut-off)
VIX_JUMP_PCT = 20.0          # VIX up this much (%) on the day = "up hard"
VIX_RATIO = 1.0              # VIX / VIX3M above 1 = the curve is inverted (stress at the short end)
VVIX_HIGH = 120.0
OAS_CALM = 4.0               # HY OAS below this (%) = no credit confirmation
OAS_STRESS = 4.5             # above this, or +OAS_RISE over 3 months = credit stress
OAS_RISE = 1.0
SAHM_TRIGGER = 0.5
RECESSION_HIGH = 0.30        # 12m recession probability (NY Fed probit) at or above this is "high"; below it is "no confirmation"
LIQ_FALL_PCT = -2.5          # net liquidity 20-day change, % of its level
SOFR_EFFR_STRESS = 0.10      # SOFR minus EFFR, percentage points
SRF_TAKEUP_BN = 10.0         # Standing Repo Facility take-up on the day, USD bn
CHAOS_FALL = 3.0             # Chaos score at least this far below the previous trading day = "falling"
CHAOS_TRANCHE2 = 50.0
VOLCTL_SELL_BN = 10.0        # vol-control 5-day estimate at or beyond +-this (USD bn) = forced selling or buying
LEVETF_BN = 1.0              # leveraged-ETF rebalance of the last session beyond +-this (USD bn)
CTA_NEAR_FLIP_PCT = 3.0      # CTA 1M flip level within this % of price = a flip is close
RETEST_WINDOW, RETEST_MIN_AGE, RETEST_BOUNCE, RETEST_NEAR = 30, 5, 3.0, 2.0   # sessions; sessions since the low; % bounce; % within the low

LABELS = {"CALM": "CALM",
          "VOL_SHOCK": "VOLATILITY SHOCK, NO CREDIT/RECESSION CONFIRMATION",
          "VOL_SHOCK_WATCH": "VOLATILITY SHOCK, CREDIT/RECESSION WATCH",
          "CREDIT_STRESS": "CREDIT STRESS",
          "RECESSION_BEAR": "RECESSION-TYPE BEAR",
          "LIQUIDITY_SQUEEZE": "LIQUIDITY SQUEEZE",
          "PANIC_UNWINDING": "PANIC UNWINDING"}
SHOCKY = ("VOL_SHOCK", "VOL_SHOCK_WATCH", "PANIC_UNWINDING")      # regimes a PANIC UNWINDING can follow
RULES = [("RECESSION_BEAR", f"credit stress and (Sahm >= {SAHM_TRIGGER} or 12m recession probability >= {RECESSION_HIGH:.0%})"),
         ("CREDIT_STRESS", f"HY OAS > {OAS_STRESS}% or up {OAS_RISE:g} pt or more in 3 months"),
         ("LIQUIDITY_SQUEEZE", f"net liquidity 20-day change <= {LIQ_FALL_PCT}% of its level, or SOFR - EFFR >= {SOFR_EFFR_STRESS} pt, or SRF take-up >= ${SRF_TAKEUP_BN:g}B"),
         ("PANIC_UNWINDING", f"the previous run was a shock or an unwinding, VIX/VIX3M is now < {VIX_RATIO:g} and the Chaos score fell {CHAOS_FALL:g}+ points on the day"),
         ("VOL_SHOCK / VOL_SHOCK_WATCH", f"VIX up {VIX_JUMP_PCT:g}%+ on the day, or VIX/VIX3M > {VIX_RATIO:g}, or VVIX >= {VVIX_HIGH:g}; no-confirmation needs HY OAS < {OAS_CALM}%, "
                                         f"recession probability < {RECESSION_HIGH:.0%} and Sahm < {SAHM_TRIGGER}"),
         ("CALM", "none of the above")]      # first match wins, top to bottom


def _f(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _missing(why):
    return {"status": "missing", "reason": why}


def reading(key, label, value, threshold, met, unit="", as_of=None, reason=None):
    """One input with its cut-off. met is True / False, or None when the value is missing."""
    return {"key": key, "label": label, "value": value, "unit": unit, "threshold": threshold, "met": met, "as_of": as_of,
            **({"reason": reason} if reason else {})}


def _gt(v, t):
    return None if v is None else v > t


def _ge(v, t):
    return None if v is None else v >= t


def _le(v, t):
    return None if v is None else v <= t


# ============================================================ readings
def _readings(inp):
    """Every number the rules use, each with its threshold. Missing ones carry a reason."""
    px, fc, ch, fo = inp.get("prices") or {}, inp.get("forecast") or {}, inp.get("chaos") or {}, inp.get("fed_ops") or {}
    vix, v3 = px.get("vix") or {}, px.get("vix3m") or {}
    vv, v3v = _f(vix.get("value")), _f(v3.get("value"))
    prev_close = _f(vix.get("prev_close"))
    jump = (vv / prev_close - 1) * 100 if vv is not None and prev_close else None
    ratio = vv / v3v if vv is not None and v3v else None
    vvix = _f((((ch.get("components") or {}).get("vvix")) or {}).get("value"))
    mr = fc.get("macro_regime") or {}
    ok = mr.get("status") == "fresh"
    oas, d3m, sahm, rec = (_f(mr.get(k)) if ok else None for k in ("hy_oas", "hy_oas_change_3m", "sahm", "recession_prob_12m"))
    why_mr = None if ok else f"macro regime {mr.get('status', 'missing')}: {mr.get('reason', 'not in this run')}"
    levers = {r.get("key"): r for r in ((ch.get("levers") or {}).get("rows") or [])}
    nl = levers.get("net_liquidity") or {}
    nl_pct = (nl["change_20d"] / (nl["level"] - nl["change_20d"]) * 100) if nl.get("status") == "fresh" and _f(nl.get("change_20d")) is not None and _f(nl.get("level")) else None
    repo = [r for r in ((fo.get("repo") or {}).get("last_60d") or [])]
    both = next((r for r in reversed(repo) if _f(r.get("sofr")) is not None and _f(r.get("effr")) is not None), None)
    spread = round(both["sofr"] - both["effr"], 3) if both else None
    srf = next((r for r in reversed(repo) if _f(r.get("repo_bn")) is not None), None)
    score, prev = _f(ch.get("score")), _f((ch.get("prev") or {}).get("score"))
    fall = score - prev if score is not None and prev is not None else None
    r = {}
    r["vix"] = reading("vix", "VIX", vv, None, None, "", vix.get("observed_at"), None if vv is not None else "VIX not in this run")
    r["vix_jump"] = reading("vix_jump", "VIX change on the day", None if jump is None else round(jump, 1), f">= +{VIX_JUMP_PCT:g}%", _ge(jump, VIX_JUMP_PCT), "%", None,
                            None if jump is not None else "needs VIX and its previous close")
    r["vix_ratio"] = reading("vix_ratio", "VIX / VIX3M", None if ratio is None else round(ratio, 3), f"> {VIX_RATIO:g}", _gt(ratio, VIX_RATIO), "", None,
                             None if ratio is not None else "needs VIX and VIX3M")
    r["vvix"] = reading("vvix", "VVIX", vvix, f">= {VVIX_HIGH:g}", _ge(vvix, VVIX_HIGH), "", (((ch.get("components") or {}).get("vvix")) or {}).get("as_of"),
                        None if vvix is not None else "VVIX is not in the Chaos components")
    r["hy_oas"] = reading("hy_oas", "HY OAS", oas, f"calm < {OAS_CALM}%, stress > {OAS_STRESS}%", None, "%", mr.get("as_of"), None if oas is not None else why_mr)
    r["hy_oas_3m"] = reading("hy_oas_3m", "HY OAS change, 3 months", None if d3m is None else round(d3m, 2), f">= +{OAS_RISE:g} pt", _ge(d3m, OAS_RISE), "pt", mr.get("as_of"),
                             None if d3m is not None else why_mr)
    r["sahm"] = reading("sahm", "Sahm rule", sahm, f">= {SAHM_TRIGGER}", _ge(sahm, SAHM_TRIGGER), "", mr.get("as_of"), None if sahm is not None else why_mr)
    r["recession"] = reading("recession", "Recession probability, 12m", rec, f">= {RECESSION_HIGH:.0%} is high", _ge(rec, RECESSION_HIGH), "fraction", mr.get("as_of"),
                             None if rec is not None else why_mr)
    r["net_liquidity"] = reading("net_liquidity", "Net liquidity, 20-day change", None if nl_pct is None else round(nl_pct, 2), f"<= {LIQ_FALL_PCT}% of level", _le(nl_pct, LIQ_FALL_PCT), "%",
                                 nl.get("as_of"), None if nl_pct is not None else (nl.get("reason") or "the Chaos block has no net-liquidity lever"))
    r["sofr_effr"] = reading("sofr_effr", "SOFR minus EFFR", spread, f">= {SOFR_EFFR_STRESS} pt", _ge(spread, SOFR_EFFR_STRESS), "pt", both and both.get("date"),
                             None if spread is not None else "no day with both SOFR and EFFR in the stored repo rows")
    r["srf"] = reading("srf", "Standing Repo Facility take-up", _f(srf and srf.get("repo_bn")), f">= ${SRF_TAKEUP_BN:g}B", _ge(_f(srf and srf.get("repo_bn")), SRF_TAKEUP_BN), "USD bn",
                       srf and srf.get("date"), None if srf else "no stored repo operation rows")
    r["chaos"] = reading("chaos", "Chaos / Peace score", score, f"tranche 2 below {CHAOS_TRANCHE2:g}", None, "", ch.get("as_of"), None if score is not None else (ch.get("reason") or "no Chaos score in this run"))
    r["chaos_fall"] = reading("chaos_fall", "Chaos change vs previous trading day", None if fall is None else round(fall, 1), f"<= -{CHAOS_FALL:g}", _le(fall, -CHAOS_FALL), "pts",
                              (ch.get("prev") or {}).get("date"), None if fall is not None else "needs today's and the previous day's Chaos score")
    return r


# ============================================================ classification
def classify(r, prev=None):
    """-> {status, key, regime, why[], confirmation, ...} from the readings of _readings(). First matching rule wins (see RULES)."""
    m = lambda k: r[k]["met"]
    vol_have = [k for k in ("vix_jump", "vix_ratio", "vvix") if r[k]["value"] is not None]
    if r["hy_oas"]["value"] is None:
        return _missing(f"HY OAS is missing ({r['hy_oas'].get('reason')}): a volatility shock cannot be told from a credit event without it")
    if not vol_have:
        return _missing("no volatility reading (VIX change, VIX/VIX3M, VVIX) in this run")
    oas, d3m = r["hy_oas"]["value"], r["hy_oas_3m"]["value"]
    credit = oas > OAS_STRESS or bool(d3m is not None and d3m >= OAS_RISE)
    recession = bool(m("sahm")) or bool(m("recession"))
    liq = [k for k in ("net_liquidity", "sofr_effr", "srf") if m(k)]
    ratio, prev_key = r["vix_ratio"]["value"], (prev or {}).get("key")
    shock_by = [k for k in vol_have if m(k)]
    out = {"status": "fresh", "version": REGIME_VERSION}
    if credit:
        key, why = ("RECESSION_BEAR" if recession else "CREDIT_STRESS"), ["hy_oas", "hy_oas_3m"] + (["sahm", "recession"] if recession else [])
    elif liq:
        key, why = "LIQUIDITY_SQUEEZE", liq
    elif prev_key in SHOCKY and ratio is not None and ratio < VIX_RATIO and m("chaos_fall"):
        key, why = "PANIC_UNWINDING", ["vix_ratio", "chaos_fall"]
    elif shock_by:
        # no confirmation = credit calm and no recession reading; an unknown recession reading is not a confirmation either way
        calm = oas < OAS_CALM and not recession and r["sahm"]["value"] is not None and r["recession"]["value"] is not None
        key, why = ("VOL_SHOCK" if calm else "VOL_SHOCK_WATCH"), shock_by + ["hy_oas", "recession", "sahm"]
    else:
        key, why = "CALM", vol_have + ["hy_oas"]
    out.update(key=key, regime=LABELS[key], why=[_why_line(r[k]) for k in why if r[k]["value"] is not None or r[k].get("reason")],
               readings=list(r.values()),
               confirmation={"credit": credit, "recession": recession, "liquidity": bool(liq)})
    return out


def _fmt(x, unit):
    if x is None:
        return "missing"
    return f"{x:.0%}" if unit == "fraction" else f"{x:g}%" if unit == "%" else f"{x:g}" if not unit else f"{x:g} {unit}"


def _why_line(rd):
    if rd["value"] is None:
        return f"{rd['label']}: missing ({rd.get('reason')})"
    state = "" if rd["met"] is None else (" - MET" if rd["met"] else " - not met")
    return f"{rd['label']} {_fmt(rd['value'], rd['unit'])} vs {rd['threshold']}{state}" if rd["threshold"] else f"{rd['label']} {_fmt(rd['value'], rd['unit'])}"


# ============================================================ forced-selling gauge
def _row(key, label, number, unit, direction, status, detail, reason=None):
    return {"key": key, "label": label, "value": number, "unit": unit, "direction": direction, "status": status, "detail": detail,
            **({"reason": reason} if reason else {})}


def _gone(key, label, why):
    return _row(key, label, None, "", None, "missing", "", why)


def forced_selling(fc):
    """Rows from the Forecast Lab flows (SPY: vol-control, CTA, leveraged ETFs, dealer gamma; SLV: leveraged ETFs, dealer gamma).
    value is USD for flows, exposure for the CTA. direction SELL / BUY / NEUTRAL; status ACTIVE (forced flow now), quiet or missing."""
    rows, spy, slv = [], (fc or {}).get("SPY") or {}, (fc or {}).get("SLV") or {}
    fl = spy.get("flows") or {}
    if fl.get("status") != "fresh":
        why = f"SPY flows {fl.get('status', 'missing')}: {fl.get('reason', 'not in this run')}"
        return {"status": "missing", "reason": why, "rows": [_gone(k, n, why) for k, n in (("vol_control", "Vol-control funds"), ("cta", "CTA / trend followers"),
                                                                                                ("leveraged_etf", "Leveraged ETFs (SPY)"), ("dealer_gamma", "Dealer gamma (SPY)"))]}
    vc, cta, lev, dg = fl.get("vol_control"), fl.get("cta"), fl.get("leveraged_etfs"), fl.get("dealer_gamma")
    if vc and _f(vc.get("flow_last_5d")) is not None:
        v5, v21, t2 = vc["flow_last_5d"], _f(vc.get("flow_last_21d")), (vc.get("flow_if_tomorrow") or {}).get("-2%")
        d = "SELL" if v5 <= -VOLCTL_SELL_BN * 1e9 else "BUY" if v5 >= VOLCTL_SELL_BN * 1e9 else "NEUTRAL"
        rows.append(_row("vol_control", "Vol-control funds (est.)", v5, "USD", d, "ACTIVE" if d != "NEUTRAL" else "quiet",
                         f"last 5 sessions {v5 / 1e9:+.1f}B; last 21 {0 if v21 is None else v21 / 1e9:+.1f}B; exposure {vc['exposure']:.2f} of target "
                         f"(5d ago {vc.get('exposure_5d_ago', float('nan')):.2f}); a further -2% day adds {0 if t2 is None else t2 / 1e9:+.1f}B. "
                         f"Cut-off +-{VOLCTL_SELL_BN:g}B over 5 sessions; AUM {vc['aum_assumption'] / 1e9:.0f}B is an industry estimate"))
    else:
        rows.append(_gone("vol_control", "Vol-control funds (est.)", "no vol_control block in the SPY flows"))
    sig, tr = ((spy.get("trend") or {}).get("signals") or {}), spy.get("trend") or {}
    if cta and _f(cta.get("exposure")) is not None:
        e, m1, px = cta["exposure"], sig.get("1m") or {}, _f(spy.get("spot"))
        after = _f((cta.get("exposure_after_move") or {}).get("-2%"))
        d = "SELL" if e < -0.05 else "BUY" if e > 0.05 else "NEUTRAL"
        near = m1.get("flip_distance_pct") is not None and abs(m1["flip_distance_pct"]) <= CTA_NEAR_FLIP_PCT
        flips = ", ".join(f"{k} {v['signal']} (flips {v['flip_level']:.0f}, {v['flip_distance_pct']:+.1f}%)" for k, v in sig.items() if v.get("flip_level"))
        rows.append(_row("cta", "CTA / trend position (replicated)", e, "exposure -1..+1", d, "ACTIVE" if near else "quiet",
                         f"exposure {e:+.2f}" + ("" if after is None else f", {after:+.2f} after a -2% day") + (f"; {flips}" if flips else "") + (f". 1M flip level {m1['flip_level']:.0f} is within {CTA_NEAR_FLIP_PCT:g}% of price" if near else "")
                         + (f" (price {px:.0f})" if px else "")))
    else:
        rows.append(_gone("cta", "CTA / trend position (replicated)", (tr.get("reason") or "no CTA block in the SPY flows")))
    if lev and _f(lev.get("last_session_rebalance")) is not None:
        rb = lev["last_session_rebalance"]
        d = "SELL" if rb <= -LEVETF_BN * 1e9 else "BUY" if rb >= LEVETF_BN * 1e9 else "NEUTRAL"
        rows.append(_row("leveraged_etf", "Leveraged ETFs, SPY (est.)", rb, "USD", d, "ACTIVE" if d != "NEUTRAL" else "quiet",
                         f"last session's end-of-day rebalance {rb / 1e9:+.2f}B after a {lev['last_session_return_pct']:+.2f}% day; every further 1% move is {lev['flow_per_1pct_move'] / 1e9:.2f}B. Cut-off +-{LEVETF_BN:g}B"))
    else:
        rows.append(_gone("leveraged_etf", "Leveraged ETFs, SPY (est.)", "no leveraged-ETF AUM in this run"))
    rows.append(_gamma_row("dealer_gamma", "Dealer gamma, SPY", dg, _f(spy.get("spot"))))
    sfl = slv.get("flows") or {}
    if sfl.get("status") == "fresh":
        sl = sfl.get("leveraged_etfs") or {}
        if _f(sl.get("last_session_rebalance")) is not None and sl.get("funds"):
            rb = sl["last_session_rebalance"]
            d = "SELL" if rb <= -0.05e9 else "BUY" if rb >= 0.05e9 else "NEUTRAL"
            rows.append(_row("leveraged_etf_slv", "Leveraged ETFs, SLV (est.)", rb, "USD", d, "ACTIVE" if d != "NEUTRAL" else "quiet",
                             f"last session's rebalance {rb / 1e6:+.0f}M after a {sl['last_session_return_pct']:+.2f}% day (cut-off +-50M)"))
        rows.append(_gamma_row("dealer_gamma_slv", "Dealer gamma, SLV", sfl.get("dealer_gamma"), _f(slv.get("spot"))))
    sell = [r for r in rows if r["direction"] == "SELL" and r["status"] == "ACTIVE" and r["unit"] == "USD" and r["key"] in ("vol_control", "leveraged_etf")]
    t2v = (vc or {}).get("flow_if_tomorrow", {}).get("-2%")
    nxt = (t2v or 0) - (lev or {}).get("flow_per_1pct_move", 0) * 2 if vc and lev and t2v is not None else None
    return {"status": "fresh", "rows": rows, "active_sellers": [r["label"] for r in rows if r["direction"] == "SELL" and r["status"] == "ACTIVE"],
            "sold_recent_usd": sum(r["value"] for r in sell) if sell else None,
            "if_spy_down_2pct_usd": nxt, "note": "Estimates from public AUM and replicated models, not reported positions (Forecast Lab card 8)."}


def _gamma_row(key, label, dg, spot):
    if not dg or _f(dg.get("net_gex_usd_per_1pt")) is None:
        return _gone(key, label, "no dealer-gamma estimate in this run (the option chain was missing)")
    ng, zg = dg["net_gex_usd_per_1pt"], _f(dg.get("zero_gamma"))
    neg = ng < 0
    where = f"; spot {spot:.2f} vs zero gamma {zg:.2f} ({(spot / zg - 1) * 100:+.1f}%)" if zg and spot else ""
    return _row(key, label, ng, "USD per 1pt", "SELL" if neg else "NEUTRAL", "ACTIVE" if neg else "quiet",
                f"net gamma {ng / 1e6:+.1f}M per 1 point: " + ("negative, dealers hedge WITH the move (amplifying)" if neg else "positive, dealers hedge against the move (dampening)") + where)


# ============================================================ playbook
def retest(closes):
    """(met, text) for 'retest without a new low' on daily closes, oldest first: the lowest close of the last RETEST_WINDOW sessions
    was at least RETEST_MIN_AGE sessions ago, price rebounded RETEST_BOUNCE% or more from it since, and is now back within RETEST_NEAR%
    of that low without closing below it."""
    c = [x for x in (_f(v) for v in (closes or [])) if x is not None][-RETEST_WINDOW:]
    if len(c) < RETEST_WINDOW:
        return None, f"needs {RETEST_WINDOW} SPY daily closes, has {len(c)}"
    low = min(c)
    i = c.index(low)
    age = len(c) - 1 - i
    if age < RETEST_MIN_AGE:
        return False, f"the {RETEST_WINDOW}-session low {low:.2f} is only {age} session(s) old (needs {RETEST_MIN_AGE}+): no retest yet"
    peak = max(c[i:])
    bounce = (peak / low - 1) * 100
    near = (c[-1] / low - 1) * 100
    met = bounce >= RETEST_BOUNCE and 0 < near <= RETEST_NEAR
    return met, f"low {low:.2f} {age} sessions ago, rebound {bounce:+.1f}%, last close {c[-1]:.2f} is {near:+.1f}% from the low (needs a {RETEST_BOUNCE:g}%+ rebound and 0-{RETEST_NEAR:g}% above the low)"


def _etf_turning(btc):
    ib = (((btc or {}).get("flows") or {}).get("ibit")) or {}
    if (btc or {}).get("status") in (None, "missing", "error") or ib.get("status") in (None, "missing", "error"):
        return None, "no IBIT flow data in this run"
    if ib.get("sum_5d_usd") is not None:
        return ib["sum_5d_usd"] > 0, f"IBIT 5-day net flow {ib['sum_5d_usd'] / 1e6:+.0f}M"
    if (ib.get("stored_days") or 0) >= 2:
        return (ib.get("positive_days_running") or 0) >= 1, f"IBIT {ib.get('positive_days_running') or 0} positive flow day(s) running"
    return None, f"IBIT flows need 2+ stored days, has {ib.get('stored_days') or 0}"


def playbook(r, spy_closes, btc):
    """Vlad's tranche plan as conditions. status FIRED / waiting / missing (an input is not there)."""
    def row(key, name, rule, fired, detail):
        return {"key": key, "name": name, "rule": rule, "fired": fired, "status": "missing" if fired is None else "FIRED" if fired else "waiting", "detail": detail}
    ratio, chaos, oas = r["vix_ratio"], r["chaos"], r["hy_oas"]
    t1 = None if ratio["value"] is None else ratio["value"] < VIX_RATIO
    t2c = None if chaos["value"] is None else chaos["value"] < CHAOS_TRANCHE2
    t2o = None if oas["value"] is None else oas["value"] < OAS_CALM
    t2 = False if (t2c is False or t2o is False) else None if (t2c is None or t2o is None) else True
    rt, rt_txt = retest(spy_closes)
    et, et_txt = _etf_turning(btc)
    t3 = True if (rt or et) else None if rt is None and et is None else False      # FIRED needs one true; waiting when none is (the detail names an unknown side)
    stop = None if oas["value"] is None else oas["value"] > OAS_STRESS
    return [row("tranche1", "1st tranche", f"VIX/VIX3M < {VIX_RATIO:g}", t1, "VIX/VIX3M " + ("missing" if ratio["value"] is None else f"{ratio['value']:.3f}")),
            row("tranche2", "2nd tranche", f"Chaos < {CHAOS_TRANCHE2:g} and HY OAS < {OAS_CALM:g}%", t2,
                f"Chaos {_fmt(chaos['value'], '')}, HY OAS {_fmt(oas['value'], '%')}"),
            row("tranche3", "3rd tranche", "a retest without a new low, or BTC ETF flows turning positive", t3,
                f"retest: {rt_txt}; ETF flows: {et_txt}"),
            row("stop", "STOP · hold cash", f"HY OAS > {OAS_STRESS:g}%", stop, "HY OAS " + _fmt(oas["value"], "%"))]


# ============================================================ assembly
def build(inp):
    """inp: {today, prices, forecast, chaos, fed_ops, bitcoin, spy_closes, prev} -> the snapshot's `regime` block.
    `prev` is the previous run's regime block (or None). Never raises on missing parts; a block it cannot classify is missing."""
    r = _readings(inp)
    cls = classify(r, inp.get("prev"))
    prev = inp.get("prev") or {}
    out = {"version": REGIME_VERSION, "as_of": str(inp.get("today")), "rules": [{"regime": k, "when": w} for k, w in RULES],
           "readings": list(r.values()), "forced_selling": forced_selling(inp.get("forecast")), "playbook": playbook(r, inp.get("spy_closes"), inp.get("bitcoin")),
           "previous": {"key": prev.get("key"), "regime": prev.get("regime"), "run_id": prev.get("run_id")} if prev.get("key") else None,
           "read": {"status": "missing", "reason": "not written yet"}}
    out.update(cls)
    if cls.get("status") == "fresh":
        out["changed"] = bool(prev.get("key")) and prev["key"] != cls["key"]
    return out


# ============================================================ THE READ
READ_SYSTEM = """You write "THE READ" for a private market terminal: three plain sentences, under 80 words, no markdown.
Sentence 1: what is happening, in the regime's own terms. Sentence 2: who is being forced to sell or buy (only the rows marked ACTIVE in the FACTS, with their sizes). Sentence 3: what would change the picture (name a reading and its level from the FACTS).
Use only the FACTS; copy every number as printed. No advice, no price targets, no causes or motives, never the words buy, sell short, should."""
MAX_READ_CHARS = 600


def facts(block):
    """The FACTS text the model sees: regime, why, forced rows, playbook. One line each."""
    fs = block.get("forced_selling") or {}
    lines = [f"Regime: {block.get('regime')}" + (f" (changed from {block['previous']['regime']})" if block.get("changed") and block.get("previous") else "")]
    lines += ["Why: " + w for w in block.get("why") or []]
    lines += [f"Forced flow, {x['label']}: {x['direction'] or 'n/a'}, {x['status']}. {x['detail'] or x.get('reason') or ''}" for x in fs.get("rows") or []]
    lines += [f"Playbook, {p['name']}: {p['status']} ({p['rule']}; {p['detail']})" for p in block.get("playbook") or []]
    return "\n".join(lines)


def _nums(s):
    return [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.?\d*", s)]


def valid_read(text, brief):
    """None when the text is usable, else why not: the right size and every number in it appears in the FACTS (within rounding)."""
    if not text or len(text) > MAX_READ_CHARS or len(text) < 40:
        return f"not usable length ({len(text or '')} characters)"
    have = _nums(brief)
    for n in _nums(text):
        if n not in have and not any(abs(n - h) <= max(0.051, abs(h) * 0.006) or abs(n - round(h)) < 1e-9 for h in have):
            return f"the number {n:g} is not in the FACTS"
    return None


def chat_claude(system, user, max_tokens, timeout, budget_usd=0.10):
    """One no-tools Sonnet call through the Claude Code login (the same call shape as the console's Sonnet engine), capped by
    --max-budget-usd. Returns (text, cost). Raises RuntimeError when the CLI is missing or answers nothing."""
    import shutil
    from pathlib import Path
    cli = shutil.which("claude") or next((str(p) for p in [Path.home() / ".local/bin/claude"] if p.exists()), "")
    if not cli:
        raise RuntimeError("the claude CLI is not installed")
    import os
    flags = ["--tools", "", "--no-session-persistence", "--output-format", "json", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
             "--disable-slash-commands", "--setting-sources", "", "--max-budget-usd", str(budget_usd)]
    try:
        p = subprocess.run([cli, "-p", "--model", "sonnet", "--system-prompt", system, *flags], input=user, capture_output=True, text=True, timeout=timeout,
                           cwd="/tmp", env={k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")})
        d = json.loads(p.stdout)
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        raise RuntimeError(f"claude: {type(e).__name__}")
    if p.returncode or d.get("is_error") or not d.get("result"):
        raise RuntimeError("claude returned no answer")
    return d["result"].strip(), d.get("total_cost_usd")


def write_read(block, local, local_model, claude, now=None):
    """Fill block["read"]. `local(system, user, max_tokens, timeout)` is the picked oMLX model (narrative.chat_local) and `claude(...)`
    the Sonnet fallback; either may be None. Returns the read dict; both failing leaves it missing with both reasons."""
    brief = facts(block)
    user = f"FACTS\n{brief}\n\nWrite THE READ."
    why, now = [], (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    for engine, model, fn, wait in (("local", local_model, local, 60), ("claude", "sonnet", claude, 90)):
        if fn is None:
            why.append(f"{engine}: not configured")
            continue
        try:
            res = fn(READ_SYSTEM, user, 260, wait)
        except Exception as e:                       # a driver error is a missing read, never a failed run
            why.append(f"{engine}: {str(e)[:120]}")
            continue
        text, cost = res if isinstance(res, tuple) else (res, None)
        text = re.sub(r"\s+", " ", text or "").strip()
        bad = valid_read(text, brief)
        if bad:
            why.append(f"{engine}: {bad}")
            continue
        return {"status": "fresh", "text": text, "engine": engine, "model": model, "generated_at": now, **({"cost_usd": cost} if cost else {})}
    return {"status": "missing", "reason": "; ".join(why)}
