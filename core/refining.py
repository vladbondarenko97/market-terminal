"""Diesel & Refining layer: margins, EIA fundamentals, maintenance seasonality, live outage tracking.

Public sources only (no keys): Yahoo futures (HO=F, RB=F, CL=F), EIA weekly history spreadsheets, EIA Refinery
Capacity Report, TCEQ emission-event feed + detail pages, CFTC NY Harbor ULSD positioning. Pure functions here;
fetching happens in core/collect.py through the shared SourceSession.
"""
import math
import re
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

VERSION = "refining_v1"
BBL = 42.0
EIA_SERIES = {
    "util_us": "WPULEUS3", "util_p1": "W_NA_YUP_R10_PER", "util_p2": "W_NA_YUP_R20_PER", "util_p3": "W_NA_YUP_R30_PER",
    "util_p4": "W_NA_YUP_R40_PER", "util_p5": "W_NA_YUP_R50_PER",
    "dist_stocks": "WDISTUS1", "dist_prod": "WDIRPUS2", "dist_supplied": "WDIUPUS2", "dist_exports": "WDIEXUS2",
    "crude_runs": "WCRRIUS2", "gas_stocks": "WGTSTUS1", "dist_stocks_p1": "WDISTP11", "dist_stocks_p3": "WDISTP31",
    # inventories card
    "crude_stocks": "WCESTUS1", "spr_stocks": "WCSSTUS1", "cushing_stocks": "W_EPC0_SAX_YCUOK_MBBL",
    "jet_stocks": "WKJSTUS1", "resid_stocks": "WRESTUS1", "propane_stocks": "WPRSTUS1", "total_stocks": "WTTSTUS1",
    "crude_prod": "WCRFPUS2", "crude_imports": "WCEIMUS2", "crude_exports": "WCREXUS2",
    "gas_supplied": "WGFUPUS2", "jet_supplied": "WKJUPUS2", "total_supplied": "WRPUPUS2",
}
# stocks shown on the inventories chart (thousand barrels), in fixed legend/colour order
INVENTORY_LINES = [("crude_stocks", "Crude (commercial)"), ("dist_stocks", "Distillate / diesel"), ("gas_stocks", "Gasoline"),
                   ("jet_stocks", "Jet fuel"), ("spr_stocks", "Strategic Petroleum Reserve"), ("cushing_stocks", "Cushing, OK (WTI hub)"),
                   ("propane_stocks", "Propane / propylene"), ("resid_stocks", "Residual fuel oil")]
# days of supply = stocks / 4-week average of this flow (products: demand; crude: refinery crude runs, as EIA defines it)
DEMAND_FOR = {"crude_stocks": "crude_runs", "dist_stocks": "dist_supplied", "gas_stocks": "gas_supplied", "jet_stocks": "jet_supplied"}


def _f(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _naive(s):
    s = s.copy()
    idx = pd.to_datetime(s.index)
    s.index = (idx.tz_localize(None) if idx.tz is not None else idx).normalize()
    return s[~s.index.duplicated(keep="last")].sort_index()


# ============================================================ margins (crack spreads)
def crack_spreads(ho, rb, cl):
    """$/bbl. Diesel crack = HO x 42 - CL; gasoline crack = RB x 42 - CL; 3-2-1 = (2 RB + 1 HO) x 42 - 3 CL, per bbl."""
    if any(x is None or len(x) < 60 for x in (ho, rb, cl)):
        return {"status": "missing", "reason": "futures history unavailable (HO=F / RB=F / CL=F)"}
    df = pd.DataFrame({"ho": _naive(ho), "rb": _naive(rb), "cl": _naive(cl)}).dropna()
    df = df[(df > 0).all(axis=1)]
    cracks = pd.DataFrame({"diesel": df.ho * BBL - df.cl, "gasoline": df.rb * BBL - df.cl,
                           "three_two_one": (2 * df.rb * BBL + df.ho * BBL - 3 * df.cl) / 3})
    out = {"status": "fresh", "as_of": cracks.index[-1].date().isoformat(), "units": "USD per barrel",
           "prices": {"ulsd_usd_gal": float(df.ho.iloc[-1]), "rbob_usd_gal": float(df.rb.iloc[-1]), "wti_usd_bbl": float(df.cl.iloc[-1])},
           "note": "front-month continuous futures (NY Harbor ULSD, RBOB, WTI)"}
    woy = cracks.index.isocalendar().week
    this_w = int(date.today().isocalendar()[1])
    for k in cracks.columns:
        s = cracks[k]
        last = float(s.iloc[-1])
        hist = s.tail(1260)
        seas = s[(woy >= this_w - 1) & (woy <= this_w + 1) & (s.index < s.index[-1] - pd.Timedelta(days=200))].tail(5 * 15)
        out[k] = {"latest": last, "change_1w": last - float(s.iloc[-6]) if len(s) > 5 else None,
                  "change_1m": last - float(s.iloc[-22]) if len(s) > 21 else None,
                  "percentile_5y": float((hist < last).mean() * 100), "zscore_5y": float((last - hist.mean()) / hist.std()),
                  "mean_5y": float(hist.mean()), "seasonal_avg_this_week": float(seas.mean()) if len(seas) else None,
                  "vs_seasonal": last - float(seas.mean()) if len(seas) else None}
    # forward change of the diesel crack from this week of year (history): does fall maintenance tighten it?
    fwd = []
    for yr in sorted(set(cracks.index.year))[:-1]:
        s = cracks["diesel"]
        start = s[(s.index.year == yr) & (s.index.isocalendar().week == this_w)]
        if len(start):
            t0 = start.index[0]
            after = s[s.index >= t0 + pd.Timedelta(days=28)]
            if len(after):
                fwd.append(float(after.iloc[0] - start.iloc[0]))
    out["diesel_seasonal_4w_change"] = {"n_years": len(fwd), "avg_usd_bbl": float(np.mean(fwd)) if fwd else None,
                                        "up_years": int(sum(x > 0 for x in fwd))}
    tail = cracks.tail(260)
    out["series"] = [[d.date().isoformat(), round(float(a), 2), round(float(b), 2)] for d, a, b in
                     zip(tail.index, tail["diesel"], tail["three_two_one"])]
    return out


# ============================================================ EIA fundamentals
def _same_week_stats(s, years=5):
    """Latest value vs the same ISO week in the previous `years` years (min / avg / max)."""
    last_d, last = s.index[-1], float(s.iloc[-1])
    w = last_d.isocalendar()[1]
    vals = []
    for k in range(1, years + 1):
        yr = s[(s.index.year == last_d.year - k)]
        near = yr[(yr.index.isocalendar().week >= w - 1) & (yr.index.isocalendar().week <= w + 1)]
        if len(near):
            vals.append(float(near.iloc[len(near) // 2]))
    if not vals:
        return {"latest": last, "date": last_d.date().isoformat()}
    return {"latest": last, "date": last_d.date().isoformat(), "avg_5y": float(np.mean(vals)), "min_5y": float(min(vals)),
            "max_5y": float(max(vals)), "vs_avg_5y": last - float(np.mean(vals)),
            "vs_avg_5y_pct": (last / float(np.mean(vals)) - 1) * 100, "change_1w": last - float(s.iloc[-2]) if len(s) > 1 else None}


def _has(s):
    return s is not None and len(s) > 0


def eia_fundamentals(series):
    if not _has(series.get("util_us")) and not _has(series.get("dist_stocks")):
        return {"status": "missing", "reason": "EIA weekly data unavailable"}
    out = {"status": "fresh", "source": "EIA Weekly Petroleum Status Report (public history files)"}
    for k in EIA_SERIES:
        s = series.get(k)
        if s is not None and len(s) > 60:
            out[k] = _same_week_stats(s)
    ds, sup = series.get("dist_stocks"), series.get("dist_supplied")
    if _has(ds) and sup is not None and len(sup) > 4:
        dem4 = float(sup.tail(4).mean())
        out["distillate_days_of_supply"] = float(ds.iloc[-1]) / dem4 if dem4 else None
        out["distillate_demand_4w_avg"] = dem4
    if ds is not None and len(ds) > 260:
        out["history"] = {"dist_stocks": [[d.date().isoformat(), float(v)] for d, v in ds.tail(104).items()]}
    missing = [k for k in EIA_SERIES if k not in out]
    if missing:
        out["status"], out["reason"] = "partial", f"missing series: {', '.join(missing)}"
    return out


# ============================================================ inventories (6 months, combined chart)
def _seasonal_avg(s, when, years=5):
    """Average of the same ISO week (±1) over the previous `years` years, for any date."""
    w = when.isocalendar()[1]
    vals = []
    for k in range(1, years + 1):
        yr = s[s.index.year == when.year - k]
        near = yr[(yr.index.isocalendar().week >= w - 1) & (yr.index.isocalendar().week <= w + 1)]
        if len(near):
            vals.append(float(near.mean()))
    return float(np.mean(vals)) if len(vals) >= 3 else None


def inventories(series, weeks=26):
    """Every major US petroleum stock for the last `weeks` weeks: level, % vs 5-year same-week average, and indexed
    to the window start; plus a table with weekly/4-week changes, days of supply and crude balance flows."""
    lines, table = [], []
    for key, label in INVENTORY_LINES:
        s = series.get(key)
        if s is None or len(s) < 300:
            continue
        win = s.tail(weeks)
        pts = []
        for d, v in win.items():
            avg = _seasonal_avg(s[s.index < d - pd.Timedelta(days=180)], d)
            pts.append({"date": d.date().isoformat(), "mbbl": float(v) / 1000,
                        "vs_5y_pct": (float(v) / avg - 1) * 100 if avg else None,
                        "indexed": float(v) / float(win.iloc[0]) * 100})
        last, avg_now = float(s.iloc[-1]), _seasonal_avg(s[s.index < s.index[-1] - pd.Timedelta(days=180)], s.index[-1])
        dem = series.get(DEMAND_FOR.get(key, ""), None)
        dos = last / float(dem.tail(4).mean()) if dem is not None and len(dem) >= 4 and dem.tail(4).mean() else None
        lines.append({"key": key, "label": label, "points": pts})
        table.append({"key": key, "label": label, "latest_mbbl": last / 1000, "date": s.index[-1].date().isoformat(),
                      "change_1w_mbbl": (last - float(s.iloc[-2])) / 1000, "change_4w_mbbl": (last - float(s.iloc[-5])) / 1000,
                      "change_26w_mbbl": (last - float(win.iloc[0])) / 1000,
                      "vs_5y_pct": (last / avg_now - 1) * 100 if avg_now else None, "days_of_supply": dos})
    flows = {}
    for key, label in (("crude_prod", "Crude production"), ("crude_imports", "Crude imports"), ("crude_exports", "Crude exports"),
                       ("crude_runs", "Refinery crude runs"), ("total_supplied", "Total products supplied (demand)"),
                       ("dist_supplied", "Distillate demand"), ("gas_supplied", "Gasoline demand"), ("jet_supplied", "Jet demand"),
                       ("dist_exports", "Distillate exports")):
        s = series.get(key)
        if s is not None and len(s) > 60:
            st = _same_week_stats(s)
            flows[key] = {"label": label, "latest_kbd": st["latest"], "avg_4w_kbd": float(s.tail(4).mean()),
                          "vs_5y_pct": st.get("vs_avg_5y_pct"), "change_1w_kbd": st.get("change_1w")}
    tot = series.get("total_stocks")
    total = None
    if tot is not None and len(tot) > 5:
        total = {"latest_mbbl": float(tot.iloc[-1]) / 1000, "change_1w_mbbl": float(tot.iloc[-1] - tot.iloc[-2]) / 1000,
                 "change_4w_mbbl": float(tot.iloc[-1] - tot.iloc[-5]) / 1000}
    if not lines:
        return {"status": "missing", "reason": "EIA inventory series unavailable"}
    return {"status": "fresh", "weeks": weeks, "as_of": table[0]["date"], "lines": lines, "table": table, "flows": flows,
            "total_incl_spr": total, "units": "million barrels (stocks), thousand barrels/day (flows)",
            "note": "vs 5y = same ISO week (±1) averaged over the previous five years"}


# ============================================================ inventories, full history (card 11 chart)
def _seasonal_avg_series(s, years=5):
    """`_seasonal_avg` for every date of a weekly series in one pass (same rule: same ISO week ±1, previous `years`
    calendar years, at least 3 of them, nothing from the last 180 days)."""
    by = {}
    weeks = s.index.isocalendar().week.to_numpy()
    for d, w, v in zip(s.index, weeks, s.to_numpy()):
        by.setdefault((d.year, int(w)), []).append((d, float(v)))
    out = []
    for d, w in zip(s.index, weeks):
        cutoff, vals = d - pd.Timedelta(days=180), []
        for k in range(1, years + 1):
            near = [v for ww in (w - 1, w, w + 1) for (dd, v) in by.get((d.year - k, int(ww)), ()) if dd < cutoff]
            if near:
                vals.append(sum(near) / len(near))
        out.append(sum(vals) / len(vals) if len(vals) >= 3 else None)
    return pd.Series(out, index=s.index, dtype="float64")


def inventory_history(series):
    """Every week EIA has published for the card-11 stocks, on one shared date axis: level (million barrels),
    % vs the 5-year same-week average, and days of supply where a matching flow exists. Columns are aligned to
    `dates`; a series that had not started yet holds null."""
    cols = {}
    for key, label in INVENTORY_LINES:
        s = series.get(key)
        if s is None or len(s) < 60:
            continue
        s = s[~s.index.duplicated(keep="last")].sort_index()
        avg = _seasonal_avg_series(s)
        col = {"label": label, "mbbl": s / 1000, "vs_5y_pct": (s / avg - 1) * 100}
        flow = series.get(DEMAND_FOR.get(key, ""))
        if flow is not None and len(flow) >= 4:
            flow = flow[~flow.index.duplicated(keep="last")].sort_index()
            avg4 = flow.rolling(4).mean().reindex(s.index)
            col["days_of_supply"] = s / avg4.where(avg4 > 0)
        cols[key] = col
    if not cols:
        return {"status": "missing", "reason": "EIA inventory series unavailable"}
    idx = sorted(set().union(*(c["mbbl"].index for c in cols.values())))

    def arr(s, nd):
        return [None if pd.isna(v) else round(float(v), nd) for v in s.reindex(idx)]
    out = []
    for key, c in cols.items():
        item = {"key": key, "label": c["label"], "first": c["mbbl"].index[0].date().isoformat(),
                "mbbl": arr(c["mbbl"], 2), "vs_5y_pct": arr(c["vs_5y_pct"], 2)}
        if "days_of_supply" in c:
            item["days_of_supply"] = arr(c["days_of_supply"], 2)
            item["supply_basis"] = "refinery crude runs" if key == "crude_stocks" else "product supplied (demand)"
        out.append(item)
    return {"status": "fresh", "version": VERSION, "dates": [d.date().isoformat() for d in idx],
            "start": idx[0].date().isoformat(), "as_of": idx[-1].date().isoformat(), "series": out,
            "note": "weekly; vs 5y = same ISO week (±1) averaged over the previous five years; "
                    "days of supply = stocks ÷ 4-week average of demand (crude: refinery crude runs)"}


# ============================================================ maintenance seasonality
def maintenance_outlook(util, weeks_ahead=10, years=10):
    """Typical utilization path by ISO week (deviation from each year's mean), applied to today's level."""
    if util is None or len(util) < 300:
        return {"status": "missing", "reason": "utilization history unavailable"}
    u = util[util.index >= util.index[-1] - pd.Timedelta(days=365 * years + 30)]
    dev = u - u.groupby(u.index.year).transform("mean")
    by_week = dev.groupby(u.index.isocalendar().week.values).mean()
    last_d = util.index[-1]
    cur_w = last_d.isocalendar()[1]
    base = float(util.iloc[-1]) - float(by_week.get(cur_w, 0.0))
    path = []
    for k in range(1, weeks_ahead + 1):
        d = last_d + timedelta(weeks=k)
        w = d.isocalendar()[1]
        path.append({"week_ending": d.date().isoformat(), "iso_week": int(w),
                     "expected_utilization": base + float(by_week.get(w, 0.0))})
    trough = min(path, key=lambda x: x["expected_utilization"])
    spring = [w for w in range(6, 23)]
    fall = [w for w in range(36, 48)]
    season = ("fall turnaround season (Sep–Nov)" if cur_w in fall else "spring turnaround season (Feb–May)" if cur_w in spring
              else "between maintenance seasons")
    return {"status": "fresh", "season": season, "current_week": int(cur_w), "current_utilization": float(util.iloc[-1]),
            "path": path, "expected_trough": trough, "years_used": int(len(set(u.index.year))),
            "method": "average weekly deviation from each year's mean utilization over the last 10 years"}


# ============================================================ outage tracking (TCEQ)
REFINERY_BRANDS = {
    "VALERO": ["VALERO", "PREMCOR", "DIAMOND SHAMROCK"], "MOTIVA": ["MOTIVA"], "EXXONMOBIL": ["EXXON"],
    "MARATHON": ["MARATHON", "GALVESTON BAY", "WESTERN REFINING"], "PHILLIPS 66": ["PHILLIPS", "WRB"],
    "CITGO": ["CITGO"], "FLINT HILLS": ["FLINT HILLS"], "TOTALENERGIES": ["TOTAL"], "DEER PARK": ["DEER PARK REFIN", "PEMEX", "SHELL DEER PARK"],
    "PASADENA": ["PASADENA", "CHEVRON"], "DELEK": ["DELEK", "ALON"], "HF SINCLAIR": ["SINCLAIR", "NAVAJO"],
    "KINDER MORGAN": ["KINDER MORGAN"], "CALUMET": ["CALUMET"],
}
UNIT_RULES = [
    (r"\bFCC|FCCU|CAT(ALYTIC)? CRACK|CAT CRACKER", "FCC (gasoline & light cycle oil)", 3),
    (r"HYDROCRACK|\bHCU\b|\bHCK\b", "hydrocracker (diesel & jet)", 3),
    (r"CRUDE|\bCDU\b|ATMOSPHERIC|PIPESTILL|\bPS-?\d", "crude distillation (whole refinery)", 3),
    (r"DIESEL|ULSD|\bDHT\b|DISTILLATE HYDRO", "diesel hydrotreater", 3),
    (r"COKER|COKING|\bDCU\b", "coker", 2),
    (r"HYDROTREAT|HDS|DESULF", "hydrotreater", 2),
    (r"REFORM|PLATFORM", "reformer (gasoline)", 1),
    (r"ALKY", "alkylation (gasoline)", 1),
    (r"SULFUR|\bSRU\b|TAIL GAS", "sulfur recovery", 1),
    (r"FLARE", "flare", 1),
    (r"BOILER|COGEN|POWER|STEAM", "utilities / power", 2),
]


def classify_event(event_type):
    t = (event_type or "").upper()
    if "SCHEDULED MAINTENANCE" in t:
        return "planned_maintenance"
    if "STARTUP" in t or "SHUTDOWN" in t:
        return "startup_shutdown"
    if t:
        return "unplanned"
    return "unknown"


def classify_units(units, cause=""):
    text = f"{units or ''} {cause or ''}".upper()
    hits = []
    for pat, label, weight in UNIT_RULES:
        if re.search(pat, text):
            hits.append((label, weight))
    if not hits:
        return [], 1
    return [h[0] for h in hits], max(h[1] for h in hits)


def _duration_hours(text):
    if not text:
        return None
    d = re.search(r"(\d+)\s*day", text)
    h = re.search(r"(\d+)\s*hour", text)
    m = re.search(r"(\d+)\s*minute", text)
    tot = (int(d.group(1)) * 24 if d else 0) + (int(h.group(1)) if h else 0) + (int(m.group(1)) / 60 if m else 0)
    return tot if (d or h or m) else None


def _city(location):
    m = re.search(r";\s*([A-Z .'-]+),\s*TX", (location or "").upper())
    return m.group(1).strip() if m else None


def match_refinery(name, location, capacity):
    """Match a TCEQ facility to an EIA refinery (Texas) by city and brand. Returns (row dict or None, confidence)."""
    if capacity is None or not len(capacity):
        return None, "no capacity table"
    tx = capacity[capacity["STATE_NAME"] == "Texas"]
    city = _city(location)
    nm = (name or "").upper()
    cands = tx[tx["SITE"].str.upper().str.startswith(city)] if city else tx.iloc[0:0]
    if not len(cands):
        return None, "no Texas refinery in that city"
    if len(cands) == 1 and ("REFIN" in nm or any(a in nm for al in REFINERY_BRANDS.values() for a in al)):
        return cands.iloc[0].to_dict(), "city"
    for brand, aliases in REFINERY_BRANDS.items():
        if any(a in nm for a in aliases):
            m = cands[cands["COMPANY_NAME"].str.upper().str.contains("|".join(re.escape(a) for a in aliases + [brand]))]
            if len(m):
                return m.iloc[0].to_dict(), "city+brand"
    if "REFIN" in nm and len(cands):
        return cands.sort_values("capacity_bpd", ascending=False).iloc[0].to_dict(), "city (largest in city, ambiguous)"
    return None, "not a refinery"


def looks_like_refinery(name):
    nm = (name or "").upper()
    return "REFIN" in nm or any(a in nm for al in REFINERY_BRANDS.values() for a in al)


def enrich_event(ev, capacity):
    ref, conf = match_refinery(ev.get("name"), ev.get("location"), capacity)
    kind = classify_event(ev.get("event_type"))
    units, weight = classify_units(ev.get("units"), ev.get("cause"))
    hrs = _duration_hours(ev.get("duration"))
    cap = float(ref["capacity_bpd"]) if ref else None
    try:
        start = datetime.strptime(ev["start"], "%m/%d/%Y %I:%M %p").isoformat() if ev.get("start") else None
    except ValueError:
        start = None
    major = bool(ref and kind == "unplanned" and ((cap or 0) >= 250000 and weight >= 3 or (hrs or 0) >= 24 and weight >= 2))
    return {**{k: ev.get(k) for k in ("event_id", "name", "rn", "location", "county", "event_type", "duration", "url",
                                     "notified")},
            "start": start, "end_text": ev.get("end"), "duration_hours": hrs, "kind": kind, "units_classified": units,
            "unit_weight": weight, "cause": (ev.get("cause") or "")[:600],
            "refinery": {"company": ref["COMPANY_NAME"], "site": ref["SITE"], "capacity_bpd": cap, "padd": int(ref["PADD"])} if ref else None,
            "match": conf, "is_refinery": ref is not None, "major": major}


def outage_summary(events, today):
    """Summary over captured refinery events (history accumulates run by run)."""
    ev = [e for e in events if e.get("is_refinery")]
    ev.sort(key=lambda e: e.get("start") or "", reverse=True)

    def window(days):
        lo = (today - timedelta(days=days)).isoformat()
        return [e for e in ev if (e.get("start") or "") >= lo]
    w7, w30 = window(7), window(30)

    def cap(lst, kind=None):
        seen, tot = set(), 0.0
        for e in lst:
            if kind and e["kind"] != kind:
                continue
            key = (e["refinery"]["company"], e["refinery"]["site"])
            if key not in seen:
                seen.add(key)
                tot += e["refinery"]["capacity_bpd"] or 0
        return tot, len(seen)
    u7, n7 = cap(w7, "unplanned")
    u30, n30 = cap(w30, "unplanned")
    return {"events_7d": len(w7), "unplanned_7d": sum(e["kind"] == "unplanned" for e in w7),
            "planned_7d": sum(e["kind"] != "unplanned" for e in w7), "major_7d": sum(e["major"] for e in w7),
            "capacity_hit_unplanned_7d_bpd": u7, "refineries_hit_unplanned_7d": n7,
            "capacity_hit_unplanned_30d_bpd": u30, "refineries_hit_unplanned_30d": n30,
            "events": ev[:40], "coverage": "Texas (TCEQ). Louisiana/other states not covered by a public feed yet."}


def refinery_table(capacity, top=15):
    if capacity is None or not len(capacity):
        return []
    t = capacity.sort_values("capacity_bpd", ascending=False).head(top)
    return [{"company": r.COMPANY_NAME, "site": r.SITE, "state": r.STATE_NAME, "padd": int(r.PADD),
             "capacity_bpd": float(r.capacity_bpd)} for r in t.itertuples()]
