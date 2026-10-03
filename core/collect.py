"""One coordinated collection + calculation pass that produces the report context.

Collectors return data only; nothing in here writes ledgers, renders reports or sends notifications.
"""
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import config
from core import cme, history, importer, lake, metrics
from core import sources as src
from core.sources import SourceUnavailable

CHICAGO = ZoneInfo("America/Chicago")
NEW_YORK = ZoneInfo("America/New_York")
EBAY_ITEMS = [
    "116932983600", "145163361842", "389246127742", "336328938777", "205844257920",
    "317033453863", "303115011716", "135152154013", "406597959293",
]
QUOTES = {
    "slv": "SLV", "spy": "SPY", "silver_futures": "SI=F", "gold": "GC=F", "btc": "BTC-USD", "usdcny": "CNY=X",
    "wti": "CL=F", "brent": "BZ=F", "vix": "^VIX", "vix3m": "^VIX3M", "dxy": "DX-Y.NYB", "tnx": "^TNX",
    "zn": "ZN=F", "rsp": "RSP", "nvda": "NVDA", "aapl": "AAPL", "msft": "MSFT",
}
WINDOW_SESSIONS = 30


def _today_local(ctx_now):
    return ctx_now.astimezone(CHICAGO).date()


def _age_status(observed, today, fresh_days=4):
    """fresh when the observation is recent enough for its calendar, stale otherwise."""
    if observed is None:
        return "missing"
    d = observed if isinstance(observed, date) else date.fromisoformat(str(observed)[:10])
    return "fresh" if (today - d).days <= fresh_days else "stale"


# ============================================================ CME acquisition
def _atomic_write(path, data):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=path.suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _archive_name(data_dir, stem, date_str, suffix, data):
    target = data_dir / f"{stem}_{date_str}{suffix}"
    if target.exists():
        if target.read_bytes() == data:
            return target, False
        # a legacy file with this name holds a different report (download-dated); never overwrite it
        target = data_dir / f"{stem}_{date_str}_r{lake.sha256_bytes(data)[:8]}{suffix}"
        if target.exists():
            return target, False
    return target, True


def acquire_cme(conn, session, run_id, data_dir, *, offline, use_browser=True, max_volume_files=10,
                login_wait_seconds=900, notify=None):
    """Validated existing files -> one bounded request per official endpoint -> manual files in CME_Data.
    Returns a per-source outcome dict. History lives in the lake either way."""
    out = {}
    # 1. pick up manually downloaded / not yet imported workbooks (no network)
    rep = {"files": {"volume": 0, "inventory": 0}, "skipped_unchanged": 0, "invalid": [], "conflicts": []}
    seen = {r["sha256"]: Path(r["path"]).name for r in
            conn.execute("SELECT sha256, path FROM v2_imports WHERE kind IN ('volume','inventory')")}
    for p in sorted(data_dir.iterdir()):
        if p.name.startswith("daily_volume") and p.suffix in (".xlsx", ".xls"):
            importer.import_workbook(conn, p, "volume", rep, seen)
        elif p.name.startswith("silver_stocks") and p.suffix in (".xls", ".xlsx"):
            importer.import_workbook(conn, p, "inventory", rep, seen)
    out["local_files"] = {"new_imported": rep["files"], "invalid": rep["invalid"]}

    def accept(kind, data, url, parser, stem, suffix):
        parsed = parser(data)
        report_date = parsed["trade_date"] if kind == "volume" else parsed["report_date"]
        fmt = cme.detect_format(data)
        pid = lake.store_payload(conn, source=f"cme_{kind}", kind=f"{kind}_workbook", content=data, fmt=fmt,
                                 request={"url": url}, source_date=report_date, parsed=parsed, run_id=run_id)
        date_str = report_date.replace("-", "") if kind == "volume" else report_date
        target, is_new = _archive_name(data_dir, stem, date_str, ".xlsx" if fmt == "xlsx" else suffix, data)
        if is_new:
            _atomic_write(target, data)
        src_name = f"cme:{'daily_volume' if kind == 'volume' else 'silver_stocks'}"
        recs = importer.volume_observations(parsed, src_name, pid) if kind == "volume" else \
            importer.inventory_observations(parsed, src_name, pid)
        lake.record_observations(conn, recs, run_id=run_id)
        if is_new:
            # record the archived file so the next run's local scan skips it instead of re-parsing it
            importer._record_import(conn, target, lake.sha256_bytes(data), len(data), kind,
                                    cme.PARSER_VERSION_VOLUME if kind == "volume" else cme.PARSER_VERSION_INVENTORY,
                                    "imported", len(recs), pid, {"embedded_date": report_date, "format": fmt})
        info = {"report_date": report_date, "file": target.name, "new_file": is_new, "payload_id": pid,
                "report_version": parsed.get("report_version")}
        if kind == "inventory" and parsed.get("warnings"):
            info["warnings"] = parsed["warnings"]
        return info, pid

    def reject(kind, data, url):
        evidence_dir = data_dir / "_rejected_downloads"
        evidence_dir.mkdir(exist_ok=True)
        ev = evidence_dir / f"{kind}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.bin"
        ev.write_bytes(data)
        return lake.store_payload(conn, source=f"cme_{kind}", kind=f"{kind}_rejected", content=data,
                                  fmt=cme.detect_format(data), request={"url": url}, status="rejected",
                                  run_id=run_id), str(ev)

    state_file = str(data_dir / "state.json")
    # ---- volume: logged-in FTP listing, only trade dates the lake does not have (bounded)
    if offline or max_volume_files <= 0:
        out["volume"] = {"outcome": "skipped",
                         "detail": "offline run: no CME request" if offline else "volume download not requested"}
    else:
        have = {r[0][:10].replace("-", "") for r in conn.execute(
            "SELECT DISTINCT observed_at FROM v2_observations WHERE metric_id = 'cme.ES_F.volume'")}
        latest_have = max(have) if have else None
        started = lake.utc_now_iso()
        t0 = time.monotonic()
        session.calls["cme"] = session.calls.get("cme", 0) + 1
        results, listing = cme.browser_fetch_volume_listing(
            state_file, have, max_files=max_volume_files, earliest=latest_have,
            profile_dir=str(data_dir / ".cme_browser_profile"), login_wait_seconds=login_wait_seconds,
            username=config.CME_LOGIN_USERNAME, password=config.CME_LOGIN_PASSWORD, notify=notify)
        got, info = [], {"outcome": listing.get("outcome"), "detail": listing.get("detail"),
                         "listing_latest": listing.get("listing_latest"), "requested": listing.get("requested"),
                         "url": cme.VOLUME_DIR_URL, "files": []}
        for d, res in results:
            session.calls["cme"] += 1
            entry = {"file_date": d, "outcome": res.outcome, "http_status": res.http_status, "detail": res.detail}
            pid = None
            if res.outcome == "ok":
                acc, pid = accept("volume", res.data, res.url, cme.parse_volume, "daily_volume", ".xlsx")
                entry.update(acc)
                got.append(acc["report_date"])
            elif res.outcome == "rejected" and res.data:
                pid, entry["evidence"] = reject("volume", res.data, res.url)
            lake.log_fetch(conn, run_id=run_id, source="cme_volume", request={"url": res.url}, started_at=started,
                           elapsed_ms=int((time.monotonic() - t0) * 1000), outcome=res.outcome,
                           http_status=res.http_status, payload_id=pid, detail=res.detail)
            info["files"].append(entry)
        if got:
            info["outcome"] = "ok"
            info["report_date"] = max(got)
        elif any(e["outcome"] == "login_required" for e in info["files"]) or listing.get("outcome") == "login_required":
            info["outcome"] = "login_required"
            info["detail"] = "CME session missing/expired: run `python main_pipeline.py cme-login`"
        elif listing.get("outcome") == "ok" and not listing.get("requested"):
            info["outcome"] = "up_to_date"
        elif info["files"]:
            info["outcome"] = info["files"][-1]["outcome"]
        if not results:
            lake.log_fetch(conn, run_id=run_id, source="cme_volume", request={"url": cme.VOLUME_DIR_URL},
                           started_at=started, elapsed_ms=int((time.monotonic() - t0) * 1000),
                           outcome=info["outcome"] or "error", detail=info.get("detail"))
        out["volume"] = info

    # ---- inventory: public workbook (plain request, then the browser path the legacy script used)
    url = cme.SILVER_STOCKS_URL
    if offline:
        out["inventory"] = {"outcome": "skipped", "detail": "offline run: no CME request"}
    else:
        started = lake.utc_now_iso()
        t0 = time.monotonic()
        session.calls["cme"] = session.calls.get("cme", 0) + 1
        res = cme.fetch_bounded(url, validate=cme.parse_inventory, max_attempts=config.CME_MAX_ATTEMPTS_PER_URL,
                                cooldown=config.CME_429_COOLDOWN_SECONDS)
        via = "requests"
        if res.outcome in ("access_denied", "error") and use_browser:
            session.calls["cme"] += 1
            res = cme.browser_fetch(url, state_file, profile_dir=str(data_dir / ".cme_browser_profile"))
            via = "browser"
            if res.outcome == "ok":
                try:
                    cme.parse_inventory(res.data)
                except cme.CMEValidationError as e:
                    res = cme.AcquisitionResult("rejected", data=res.data, detail=str(e), url=url, attempts=1)
        info = {"outcome": res.outcome, "http_status": res.http_status, "detail": res.detail, "url": url,
                "attempts": res.attempts, "via": via}
        pid = None
        if res.outcome == "ok":
            acc, pid = accept("inventory", res.data, url, cme.parse_inventory, "silver_stocks", ".xls")
            info.update(acc)
        elif res.outcome == "rejected" and res.data:
            pid, info["evidence"] = reject("inventory", res.data, url)
        lake.log_fetch(conn, run_id=run_id, source="cme_inventory", request={"url": url, "via": via},
                       started_at=started, elapsed_ms=int((time.monotonic() - t0) * 1000), outcome=res.outcome,
                       http_status=res.http_status, payload_id=pid, detail=res.detail)
        out["inventory"] = info
    return out


# ============================================================ sections
def _quote(session, key):
    q = src.yf_quote(session, QUOTES[key])
    return q


def collect_quotes(session, today):
    out = {}
    for key in QUOTES:
        q = _quote(session, key)
        if q.get("value") is not None:
            q["status"] = _age_status(q.get("observed_at"), today)
            if q["status"] == "stale":
                q["reason"] = f"last close {str(q['observed_at'])[:10]}"
        out[key] = q
    return out


def collect_options(session, symbol, spot, today):
    try:
        oc = src.yf_option_chains(session, symbol)
    except SourceUnavailable as e:
        return {"status": "missing", "reason": str(e), "symbol": symbol}
    chains, exps = oc["chains"], [e for e in oc["expirations"] if e in oc["chains"]]
    gex = metrics.gex_profile(spot, chains, exps, today)
    mp = metrics.max_pain(chains.get(exps[0])) if exps else {"strike": None, "status": "missing",
                                                              "reason": "no expirations"}
    top, coverage = metrics.top_contracts(chains)
    summary = metrics.options_summary(chains, spot, today)
    n_contracts = sum(len(c["calls"]) + len(c["puts"]) for c in chains.values())
    status = "fresh" if not oc["failed"] else "partial"
    return {"status": status, "reason": f"{len(oc['failed'])} expirations failed" if oc["failed"] else None,
            "symbol": symbol, "spot": spot, "expirations": oc["expirations"], "expirations_captured": len(chains),
            "contracts_captured": n_contracts, "failed": oc["failed"], "payloads": oc["payloads"], "gex": gex,
            "max_pain": mp, "front_expiration": exps[0] if exps else None, "top": top, "coverage": coverage,
            "summary": summary,
            "source": "yahoo", "_chains": chains}


def collect_flow(session, symbol, end, days, limit):
    start = end - timedelta(days=days)
    try:
        df, pid = src.databento_trades(session, config.DATABENTO_API_KEY, symbol, start, end, limit)
    except SourceUnavailable as e:
        r = metrics.block_flow(None)
        r.update({"reason": str(e), "symbol": symbol, "dataset": "DBEQ.BASIC", "session": start.date().isoformat(),
                  "window": [start.isoformat(), end.isoformat()]})
        return r, None
    r = metrics.block_flow(df, limit=limit)
    r.update({"symbol": symbol, "dataset": "DBEQ.BASIC", "payload_id": pid, "source": "databento",
              "session": start.date().isoformat() if days == 1 else f"{start.date()}..{end.date()}",
              "window": [start.isoformat(), end.isoformat()]})
    return r, df


def collect_calendar(session, now):
    today = _today_local(now)
    week_start = today - timedelta(days=(today.weekday() + 1) % 7)       # FF weeks run Sunday..Saturday
    feeds, errors, events, payloads = {}, [], [], {}
    covered_until = None
    for week, start in (("thisweek", week_start), ("nextweek", week_start + timedelta(days=7))):
        try:
            evs, pid = src.ff_calendar(session, week)
            feeds[week] = len(evs)
            payloads[week] = pid
            events.extend(dict(e, feed=week) for e in evs)
            if covered_until is None or (week == "nextweek" and "thisweek" in feeds):
                covered_until = start + timedelta(days=6)
        except SourceUnavailable as e:
            errors.append({"feed": week, "error": str(e)})
            if week == "thisweek":
                break
    if "thisweek" not in feeds:
        return {"status": "missing", "reason": errors[0]['error'] if errors else 'no feed',
                "events": [], "upcoming": [], "today_tier1": [], "coverage": None, "errors": errors,
                "catalyst_status": "unavailable"}
    seen, norm = set(), []
    for e in events:
        key = (e["date"], e["time"], e["title"], e["country"])
        if key in seen:
            continue
        seen.add(key)
        rec = dict(e)
        try:
            d = datetime.strptime(e["date"], "%m-%d-%Y").date()
            rec["date_iso"] = d.isoformat()
            if re.fullmatch(r"\d{1,2}:\d{2}[ap]m", e["time"] or ""):
                utc = datetime.strptime(f"{e['date']} {e['time']}", "%m-%d-%Y %I:%M%p").replace(tzinfo=timezone.utc)
                rec["utc"] = utc.isoformat()
                rec["time_ct"] = utc.astimezone(CHICAGO).strftime("%-I:%M%p").lower()
                rec["time_et"] = utc.astimezone(NEW_YORK).strftime("%-I:%M%p").lower()
                rec["date_ct"] = utc.astimezone(CHICAGO).date().isoformat()
            else:
                rec["date_ct"] = d.isoformat()
        except ValueError:
            rec["date_ct"] = None
        norm.append(rec)
    horizon = today + timedelta(days=7)
    upcoming = [e for e in norm if e["country"] == "USD" and e["impact"] in ("High", "Medium") and e.get("date_ct")
                and today.isoformat() <= e["date_ct"] <= horizon.isoformat()]
    upcoming.sort(key=lambda e: (e["date_ct"], e.get("utc") or ""))
    tier1 = [e for e in upcoming if e["impact"] == "High" and e["date_ct"] == today.isoformat()]
    status, reason = "fresh", None
    if covered_until < horizon:
        status, reason = "partial", (f"feed coverage ends {covered_until}; 7-day window runs to {horizon}"
                                     + (f" ({errors[0]['error']})" if errors else ""))
    return {"status": status, "reason": reason, "events": norm, "upcoming": upcoming, "today_tier1": tier1,
            "coverage": {"from": week_start.isoformat(), "to": covered_until.isoformat(), "feeds": feeds},
            "errors": errors, "source": "forexfactory", "payloads": payloads,
            "time_zone_note": "feed times are UTC; displayed in Chicago (CT) and New York (ET)",
            "catalyst_status": "ARMED (Tier-1 Data Today)" if tier1 else "CLEAR (No Tier-1 Data Today)"}


def collect_macro(session):
    out = {}
    for key, sid, div in (("oas", "BAMLH0A0HYM2", 1.0), ("rrp", "RRPONTSYD", 1.0), ("walcl", "WALCL", 1000.0)):
        try:
            obs, pid = src.fred_series(session, sid, config.FRED_API_KEY)
            d = src.fred_deltas(obs, div)
            out[key] = {**d, "series_id": sid, "status": "fresh", "reason": None, "source": "fred",
                        "payload_id": pid, "observed_at": d["date"], "unit": "percent" if key == "oas" else "USD bn"} \
                if d else {"status": "missing", "reason": "no observations", "series_id": sid, "latest": None}
        except SourceUnavailable as e:
            out[key] = {"status": "missing", "reason": str(e), "series_id": sid, "latest": None}
    return out


def collect_shanghai(session, prices):
    try:
        sge = src.sge_silver_benchmark(session)
    except SourceUnavailable as e:
        return {"status": "missing", "reason": str(e), "tax_assumption": 0.13}
    cny = sge["value"]
    fx = prices["usdcny"].get("value")
    comex = prices["silver_futures"].get("value")
    base = {"cny_per_kg": cny, "sge_date": sge["date"], "sge_session": sge["session"], "usdcny": fx,
            "comex_ref": comex, "comex_ref_symbol": "SI=F", "tax_assumption": 0.13,
            "tax_note": "13% VAT stripped from SGE price; retained legacy assumption, benchmark treatment unverified",
            "source": "akshare:SGE", "payload_id": sge["payload_id"], "observed_at": sge["date"]}
    if cny is None or fx is None:
        return {**base, "status": "missing", "reason": "SGE price or USD/CNY unavailable (no hardcoded FX fallback)"}
    usd_kg = cny / fx
    usd_oz = usd_kg / 32.1507
    prem = ((cny / 1.13) / fx) / 32.1507 - comex if comex is not None else None
    return {**base, "usd_per_kg": usd_kg, "usd_per_oz": usd_oz, "raw_spread": usd_oz - comex if comex else None,
            "premium_tax_adj": prem, "status": "fresh" if comex is not None else "partial",
            "reason": None if comex is not None else "COMEX reference unavailable"}


def collect_ebay(session, benchmark):
    try:
        listings, errors = src.ebay_listings(session, config.EBAY_APP_ID, config.EBAY_CERT_ID, EBAY_ITEMS)
    except SourceUnavailable as e:
        return {"status": "missing", "reason": str(e), "listings": [], "benchmark": benchmark}
    rows = []
    for l in listings:
        j = l["raw"]
        price = metrics.safe_float((j.get("price") or {}).get("value"))
        ship_opts = j.get("shippingOptions") or []
        ship = metrics.safe_float(((ship_opts[0] if ship_opts else {}).get("shippingCost") or {}).get("value")) \
            if ship_opts else None
        avail = (j.get("estimatedAvailabilities") or [{}])[0]
        title = j.get("title") or f"Silver Eagle (ID: {l['item_id']})"
        oz = 1.0 if re.search(r"\b1[\s-]?oz\b|\(1 oz\)", title, re.I) else None
        total = price + (ship or 0.0) if price is not None else None
        bench = benchmark.get("value")
        rows.append({
            "item_id": l["item_id"], "title": title, "url": j.get("itemWebUrl"),
            "currency": (j.get("price") or {}).get("currency"), "base_price": price, "shipping": ship,
            "shipping_known": bool(ship_opts), "total_cost": total, "oz_per_unit": oz,
            "cost_per_oz": total / oz if (total is not None and oz) else None,
            "status": avail.get("estimatedAvailabilityStatus"),
            "available_qty": (f"{avail['availabilityThreshold']}+" if "availabilityThreshold" in avail
                              else avail.get("estimatedAvailableQuantity")),
            "sold_qty": avail.get("estimatedSoldQuantity"),
            "premium_dollars": total - bench if (total is not None and bench) else None,
            "premium_percent": (total - bench) / bench * 100 if (total is not None and bench) else None,
            "payload_id": l["payload_id"],
        })
    rows.sort(key=lambda r: (r["total_cost"] is None, r["total_cost"] or 0))
    costs = [r["total_cost"] for r in rows if r["total_cost"] is not None]
    status = "fresh" if rows and not errors else ("partial" if rows else "missing")
    reason = "; ".join(e["error"] for e in errors)[:300] or None
    if benchmark.get("value") is None:
        status, reason = "partial", "benchmark unavailable: premiums not computed"
    return {"status": status, "reason": reason, "benchmark": benchmark, "listings": rows, "errors": errors,
            "cheapest": min(costs) if costs else None, "average": sum(costs) / len(costs) if costs else None,
            "source": "ebay_browse_api"}


def _position_marks(conn, spy_opts):
    """Real marks for every open tracked SPY contract, read from the chains this run already captured."""
    from core import positions
    chains = spy_opts.get("_chains") or {}
    marks = []
    for p in positions.open_contracts(conn):
        ch = chains.get(p["expiration"])
        df = (ch or {}).get("calls" if p["position_type"] == "CALL" else "puts")
        if df is None or not len(df):
            continue
        hit = df[df["contractSymbol"] == p["contract"]]
        if len(hit):
            h = hit.iloc[0]
            bid, ask, last = (metrics.safe_float(h.get(k)) for k in ("bid", "ask", "lastPrice"))
            mid = (bid + ask) / 2 if bid and ask and bid > 0 and ask > 0 else last
            marks.append({"contract": p["contract"], "bid": bid, "ask": ask, "last": last, "mid": mid,
                          "iv": metrics.safe_float(h.get("impliedVolatility"))})
    return marks


def _execution_ticket(engine, options, today):
    tgt = engine.get("target_strike")
    if engine.get("total_score") is None or abs(engine["total_score"]) < 3 or tgt is None or tgt != tgt:
        return "CASH POSITION - No trade ticket generated." if engine.get("total_score") is not None else None
    chains = options.get("_chains") or {}
    lo, hi = engine["dte_window"]
    target = None
    for i in range(lo, hi + 7):
        d = today + timedelta(days=i)
        if d.weekday() == 4:
            target = d
            break
    exps = [e for e in chains]
    if not exps or target is None:
        return {"status": "missing", "reason": "no captured expirations for the target window"}
    actual = min(exps, key=lambda e: abs(date.fromisoformat(e) - target))
    side = "calls" if engine["total_score"] >= 3 else "puts"
    df = chains[actual].get(side)
    if df is None or not len(df):
        return {"status": "missing", "reason": f"no {side} captured for {actual}"}
    strike = min(df["strike"].tolist(), key=lambda k: abs(k - engine["target_strike"]))
    row = df[df["strike"] == strike].iloc[0]
    return {"contract": row.get("contractSymbol"), "type": "CALL" if side == "calls" else "PUT", "strike": strike,
            "expiration": actual, "target_friday": target.isoformat(), "allocation": engine.get("allocation"),
            "bid": metrics.safe_float(row.get("bid")), "ask": metrics.safe_float(row.get("ask")),
            "last": metrics.safe_float(row.get("lastPrice")), "iv": metrics.safe_float(row.get("impliedVolatility")),
            "volume": metrics.safe_float(row.get("volume")), "open_interest": metrics.safe_float(row.get("openInterest")),
            "source": "captured chain (no extra request)"}


def cme_section(conn, as_of, today, acquisition):
    products = {}
    for key in cme.TARGET_PRODUCTS:
        s = history.cme_series(conn, key, as_of)
        products[key] = s[-WINDOW_SESSIONS:]
    dates = [r["date"] for r in products.get("ES_F", []) or products.get("SI_F", [])]
    latest = max((r["date"] for p in products.values() for r in p), default=None)
    status = _age_status(latest, today, fresh_days=5) if latest else "missing"
    acq = acquisition.get("volume", {})
    if status == "fresh" and acq.get("outcome") not in ("ok", "up_to_date"):
        status = "cached"
    reason = None
    if status == "stale":
        reason = f"latest CME volume report is trade date {latest}; acquisition: {acq.get('outcome')}"
    elif status == "missing":
        reason = "no CME volume history"
    latest_vals = {k: (v[-1] if v else None) for k, v in products.items()}
    return {"status": status, "reason": reason, "latest_trade_date": latest, "sessions": len(dates),
            "products": products, "latest": latest_vals, "legacy_names": cme.LEGACY_NAME,
            "acquisition": acq, "source": "cme:daily_volume", "observed_at": latest}


def inventory_section(conn, as_of, today, acquisition):
    series = history.inventory_series(conn, as_of)
    wb = [r for r in series if r.get("source") == "workbook"]
    if not wb:
        return {"status": "missing", "reason": "no parsed inventory reports", "series": []}
    last = wb[-1]
    acq = acquisition.get("inventory", {})
    status = _age_status(last["date"], today, fresh_days=4)
    if status == "fresh" and acq.get("outcome") != "ok":
        status = "cached"
    reason = None if status != "stale" else f"latest report {last['date']}; acquisition: {acq.get('outcome')}"
    deps = conn.execute("""SELECT entity, value_json FROM v2_observations WHERE metric_id='comex.silver.depository'
                           AND observed_at = ? ORDER BY entity""", (last["date"],)).fetchall()
    import json as _json
    return {"status": status, "reason": reason, "report_date": last["date"], "unit": "troy_oz",
            "registered": last.get("registered"), "eligible": last.get("eligible"), "total": last.get("total"),
            "reg_change": last.get("registered_net_change"), "elig_change": last.get("eligible_net_change"),
            "total_change": last.get("total_net_change"), "reg_adjustment": last.get("registered_adjustment"),
            "elig_adjustment": last.get("eligible_adjustment"), "total_adjustment": last.get("total_adjustment"),
            "depositories": [{"depository": d["entity"], "rows": _json.loads(d["value_json"])} for d in deps],
            "series": series[-60:], "acquisition": acq, "source": "cme:silver_stocks", "payload_id": last.get("payload_id"),
            "observed_at": last["date"]}


# ============================================================ orchestration
def build_context(conn, session, run, *, use_browser=True, skip_cme_download=False, max_volume_files=10,
                  login_wait_seconds=900, notify=None):
    now = datetime.fromisoformat(run["generated_at"])
    today = _today_local(now)
    as_of = run["generated_at"]
    offline = session.offline
    data_dir = Path(config.DATA_DIR)
    stage_times = {}

    def timed(name, fn):
        t0 = time.monotonic()
        try:
            return fn()
        finally:
            stage_times[name] = round(time.monotonic() - t0, 2)

    db_end = src.databento_window(now)
    with ThreadPoolExecutor(max_workers=config.MAX_PROVIDER_CONCURRENCY) as pool:
        f_cme = pool.submit(timed, "cme", lambda: acquire_cme(conn, session, run["run_id"], data_dir,
                                                              offline=offline or skip_cme_download,
                                                              use_browser=use_browser,
                                                              max_volume_files=max_volume_files,
                                                              login_wait_seconds=login_wait_seconds, notify=notify))
        f_quotes = pool.submit(timed, "quotes", lambda: collect_quotes(session, today))
        f_misc = pool.submit(timed, "macro_calendar", lambda: (collect_macro(session), collect_calendar(session, now)))
        f_flow = pool.submit(timed, "block_flow", lambda: {
            "SPY": collect_flow(session, "SPY", db_end, 1, 50000),
            "SLV": collect_flow(session, "SLV", db_end, 1, 50000),
            "SPY_5d": collect_flow(session, "SPY", db_end, 7, 100000)})
        prices = f_quotes.result()
        macro, calendar = f_misc.result()
        f_opts = pool.submit(timed, "options", lambda: {
            "SPY": collect_options(session, "SPY", prices["spy"].get("value"), today),
            "SLV": collect_options(session, "SLV", prices["slv"].get("value"), today)})
        f_side = pool.submit(timed, "sge_ebay_spot", lambda: (
            collect_shanghai(session, prices),
            collect_ebay(session, {"value": prices["silver_futures"].get("value"), "symbol": "SI=F",
                                   "source": "yahoo", "observed_at": prices["silver_futures"].get("observed_at")}),
            _spot(session)))
        flows = f_flow.result()
        cme_acq = f_cme.result()   # last: a CME login wait must not hold up quotes, options and flow
        options = f_opts.result()
        shanghai, ebay, silver_spot = f_side.result()

    def tech():
        try:
            h1, _ = src.yf_history(session, "SPY", "1mo", "1h")
            d1, _ = src.yf_history(session, "SPY", "2y", "1d")
            w1, _ = src.yf_history(session, "SPY", "5y", "1wk")
        except SourceUnavailable as e:
            return {"status": "missing", "reason": str(e)}
        return metrics.technicals(h1, d1, w1)
    technicals = timed("technicals", tech)
    prices["silver_spot"] = silver_spot

    # ---- CME-derived sections (from the lake, never from yesterday's XML)
    cme_vol = cme_section(conn, as_of, today, cme_acq)
    inventory = inventory_section(conn, as_of, today, cme_acq)
    si_latest = cme_vol["latest"].get("SI_F") or {}
    pp = metrics.paper_physical(si_latest.get("open_interest"), inventory.get("registered"))
    pp.update({"oi_trade_date": si_latest.get("date"), "inventory_report_date": inventory.get("report_date")})
    div_silver = metrics.oi_divergence(cme_vol["products"]["SI_F"], cme_vol["products"]["SIL_F"])
    div_es = metrics.oi_divergence(cme_vol["products"]["ES_F"], cme_vol["products"]["MES_F"])
    pc = []
    calls = {r["date"]: r["volume"] for r in cme_vol["products"]["ES_C"]}
    for r in cme_vol["products"]["ES_P"]:
        c = calls.get(r["date"])
        if c is not None:
            pc.append({"date": r["date"], "calls": c, "puts": r["volume"],
                       "ratio": (r["volume"] / c) if c else None})

    # ---- ratios / vmri
    g, s, b = (prices[k].get("value") for k in ("gold", "silver_futures", "btc"))
    ratios = {"gold_silver": g / s if g and s else None, "btc_silver_oz": b / s if b and s else None,
              "btc_gold_oz": b / g if b and g else None,
              "operands": {k: {"value": prices[k].get("value"), "observed_at": prices[k].get("observed_at")}
                           for k in ("gold", "silver_futures", "btc")},
              "status": "fresh" if g and s and b else "partial", "source": "derived"}
    vm = metrics.vmri(prices["dxy"].get("value"), prices["tnx"].get("value"), macro["oas"].get("latest"),
                      prices["vix"].get("value"))
    weather = metrics.weather(prices["vix"].get("value"), prices["vix3m"].get("value"))
    pct = {}
    for k, sym in (("spy", "SPY"), ("rsp", "RSP"), ("nvda", "NVDA"), ("aapl", "AAPL"), ("msft", "MSFT")):
        q = prices[k]
        pct[sym] = (q["change"] / q["prev_close"] * 100) if q.get("change") is not None and q.get("prev_close") else None
    breadth = metrics.breadth(pct)
    spy_flow, _ = flows["SPY"]
    nodes_5d = flows["SPY_5d"][1]
    nodes = metrics.price_nodes(nodes_5d)
    spy_opts = options["SPY"]
    engine = metrics.execution_engine(
        vix=prices["vix"].get("value"), breadth_cond=breadth.get("condition"), flow_bias=spy_flow.get("bias"),
        alignment=technicals.get("alignment"), price=technicals.get("current_price"),
        call_wall=(spy_opts.get("gex") or {}).get("call_wall"), put_wall=(spy_opts.get("gex") or {}).get("put_wall"),
        max_pain_strike=(spy_opts.get("max_pain") or {}).get("strike"), nodes=[n["price"] for n in nodes],
        flow_method=spy_flow.get("bias_method"))
    engine["ticket"] = _execution_ticket(engine, spy_opts, today)
    position_marks = _position_marks(conn, spy_opts)

    extended = timed("extended_features", lambda: extended_features(conn, session, as_of, prices, options, cme_vol,
                                                                     inventory, macro, technicals, flows,
                                                                     current={"VMRI_Score": vm.get("score"),
                                                                              "Gold_Silver_Ratio": ratios.get("gold_silver"),
                                                                              "SHFE_Premium": shanghai.get("premium_tax_adj"),
                                                                              "Paper_Physical_Ratio": pp.get("ratio")}))

    frames_hist = extended.pop("_frames", {})
    forecast = timed("forecast_lab", lambda: build_forecast(conn, session, now, today, frames_hist, options, prices, macro))
    refining_ctx = timed("refining", lambda: _guarded_refining(conn, session, run["run_id"], today))

    ctx = {
        "run": {k: run[k] for k in ("run_id", "generated_at", "run_folder", "mode", "code_version", "trigger")}
               | {"generated_local": now.astimezone(CHICAGO).strftime("%Y-%m-%d %I:%M %p %Z"),
                  "data_dir": str(data_dir)},
        "calendar": calendar, "inventory": inventory, "cme_volume": cme_vol, "prices": prices,
        "shanghai": shanghai, "ratios": ratios, "macro": macro, "vmri": vm, "paper_physical": pp,
        "oi_divergence": {"silver": div_silver, "es": div_es}, "es_put_call": pc,
        "options": {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in options.items()},
        "flow": {"SPY": spy_flow, "SLV": flows["SLV"][0], "SPY_5d": {**flows["SPY_5d"][0], "nodes": nodes}},
        "technicals": technicals, "weather": weather, "breadth": breadth, "execution": engine, "ebay": ebay,
        "sources": {"cme": cme_acq}, "stage_seconds": stage_times, **extended, "position_marks": position_marks,
        "forecast": forecast, "refining": refining_ctx,
    }
    frames = {"trades": {k: v[1] for k, v in flows.items()}}
    return lake.clean_json(ctx), frames


HISTORY_SYMBOLS = {"SPY": ("SPY", "5y"), "SLV": ("SLV", "5y"), "SI_F": ("SI=F", "5y"), "GC_F": ("GC=F", "5y"),
                   "VIX": ("^VIX", "1y"), "DXY": ("DX-Y.NYB", "5y"), "TNX": ("^TNX", "1y"), "HG_F": ("HG=F", "5y")}


def extended_features(conn, session, as_of, prices, options, cme_vol, inventory, macro, technicals, flows,
                      current=None):
    """Model features for the extended tactical XML: trend/vol stats, vol regime, positioning, regime percentiles."""
    stats, frames = {}, {}
    for key, (sym, period) in HISTORY_SYMBOLS.items():
        try:
            df, pid = src.yf_history(session, sym, period, "1d")
            frames[key] = df
            stats[key] = {**metrics.price_stats(df), "symbol": sym, "source": "yahoo", "payload_id": pid}
        except SourceUnavailable as e:
            stats[key] = {"status": "missing", "reason": str(e), "symbol": sym}

    def pct(key, value):
        df = frames.get(key)
        if df is None or value is None:
            return None
        r = metrics.percentile_context(df["Close"].dropna().tail(252).tolist(), value)
        if r:
            r["window"] = f"{len(df['Close'].dropna().tail(252))} daily closes"
        return r

    # volatility regime: implied vs realized
    spy_rv = stats.get("SPY", {}).get("rv_20d")
    vix = prices["vix"].get("value")
    slv_sum = (options.get("SLV") or {}).get("summary") or {}
    slv_iv = (slv_sum.get("atm_iv_30d") or slv_sum.get("atm_iv_near") or {}).get("atm_iv")
    slv_rv = stats.get("SLV", {}).get("rv_20d")
    vol_regime = {
        "spy_implied_vix": vix / 100 if vix is not None else None, "spy_realized_20d": spy_rv,
        "spy_variance_risk_premium": (vix / 100 - spy_rv) if (vix is not None and spy_rv is not None) else None,
        "slv_atm_iv_30d": slv_iv, "slv_realized_20d": slv_rv,
        "slv_iv_minus_rv": (slv_iv - slv_rv) if (slv_iv is not None and slv_rv is not None) else None,
        "vix_term_ratio": (vix / prices["vix3m"]["value"]) if vix and prices["vix3m"].get("value") else None,
        "status": "fresh" if spy_rv is not None and vix is not None else "partial", "source": "derived",
        "note": "IV and RV are annualized fractions; VRP = implied - realized"}

    # CME positioning over sessions (full report-dated history, not only the 30-session window)
    positioning = {}
    for key in cme.TARGET_PRODUCTS:
        positioning[key] = metrics.cme_positioning(history.cme_series(conn, key, as_of))
    so_c, so_p = positioning.get("SO_C", {}), positioning.get("SO_P", {})
    silver_opts = {"put_call_volume_ratio": so_p.get("volume") / so_c["volume"] if so_c.get("volume") and so_p.get("volume") is not None else None,
                   "put_call_oi_ratio": so_p.get("open_interest") / so_c["open_interest"] if so_c.get("open_interest") and so_p.get("open_interest") is not None else None,
                   "trade_date": so_c.get("trade_date"), "source": "CME SO options (daily volume report)"}

    # inventory trends by report
    series = [r for r in inventory.get("series", []) if r.get("source") == "workbook" and r.get("registered") is not None]

    def inv_chg(n, f):
        return series[-1][f] - series[-1 - n][f] if len(series) > n else None
    deps = []
    for d in inventory.get("depositories", []):
        reg = ((d.get("rows") or {}).get("registered") or {}).get("total_today")
        elig = ((d.get("rows") or {}).get("eligible") or {}).get("total_today")
        deps.append({"depository": d["depository"], "registered": reg, "eligible": elig})
    deps.sort(key=lambda x: -(x["registered"] or 0))
    inv_trend = {"registered_change_5_reports": inv_chg(5, "registered"), "registered_change_20_reports": inv_chg(20, "registered"),
                 "eligible_change_5_reports": inv_chg(5, "eligible"), "eligible_change_20_reports": inv_chg(20, "eligible"),
                 "registered_share_of_total": (inventory["registered"] / inventory["total"]) if inventory.get("total") else None,
                 "registered_coverage_days_at_5r_pace": None, "top_depositories": deps[:6], "reports": len(series)}
    if inv_trend["registered_change_5_reports"] and inv_trend["registered_change_5_reports"] < 0:
        pace = -inv_trend["registered_change_5_reports"] / 5
        inv_trend["registered_coverage_days_at_5r_pace"] = inventory["registered"] / pace if pace else None

    # silver cross-market basis
    sf, sp, slv = (prices[k].get("value") for k in ("silver_futures", "silver_spot", "slv"))
    basis = {"futures_minus_spot": (sf - sp) if sf and sp else None,
             "futures_minus_spot_pct": (sf / sp - 1) if sf and sp else None,
             "slv_per_spot_oz": (slv / sp) if slv and sp else None,
             "note": "SI=F continuous front month vs GoldAPI spot; SLV/spot approximates ounces per share"}

    # regime percentiles over the macro ledger (latest value from this run)
    mdf = history.macro_daily(conn, as_of)
    regime = {"window": None}
    if not mdf.empty:
        regime["window"] = f"{mdf['_ts'].min():%Y-%m-%d} → {mdf['_ts'].max():%Y-%m-%d}, {len(mdf)} ledger days"
        latest_vals = {"High_Yield_OAS": macro["oas"].get("latest"), "Reverse_Repo_BN": macro["rrp"].get("latest"),
                       **(current or {})}
        for col, val in latest_vals.items():
            if col in mdf.columns:
                series_vals = pd.to_numeric(mdf[col], errors="coerce").tolist()
                if val is None:
                    val = next((v for v in reversed(series_vals) if v == v), None)
                regime[col] = metrics.percentile_context(series_vals, val)
    regime["VIX_1y"] = pct("VIX", vix)
    regime["DXY_1y"] = pct("DXY", prices["dxy"].get("value"))
    regime["TNX_1y"] = pct("TNX", prices["tnx"].get("value"))
    regime["SLV_1y"] = pct("SLV", slv)
    regime["GC_F_1y"] = pct("GC_F", prices["gold"].get("value"))
    return {"_frames": frames, "price_stats": stats, "vol_regime": vol_regime, "cme_positioning": positioning,
            "silver_options_cme": silver_opts, "inventory_trend": inv_trend, "silver_basis": basis,
            "regime_context": regime}


FRED_FORECAST = {"DFII10": 1400, "INDPRO": 80, "T10Y3M": 400, "T10Y2Y": 30, "NFCI": 200, "SAHMREALTIME": 24}
COT_SPECS = {"silver": ("72hh-3qpy", "084691"), "sp": ("gpe5-46if", "13874A")}


def build_forecast(conn, session, now, today, frames, options, prices, macro):
    """Fetch the Forecast Lab's extra inputs (grouped by provider, 3 at a time) and run core.forecast."""
    from core import forecast as F
    since3y = (today - timedelta(days=3 * 365 + 7)).isoformat()
    errors = {}

    def fred_all():
        out = {}
        for sid, lim in FRED_FORECAST.items():
            try:
                out[sid], _ = src.fred_series(session, sid, config.FRED_API_KEY, limit=lim)
            except SourceUnavailable as e:
                errors[f"fred:{sid}"] = str(e)
        try:   # same request the macro section already made (deduplicated)
            out["BAMLH0A0HYM2"], _ = src.fred_series(session, "BAMLH0A0HYM2", config.FRED_API_KEY, limit=300)
        except SourceUnavailable as e:
            errors["fred:BAMLH0A0HYM2"] = str(e)
        try:
            out["_cpi"], _ = src.fred_release_dates(session, 10, config.FRED_API_KEY, (today - timedelta(days=5 * 365)).isoformat())
        except SourceUnavailable as e:
            errors["fred:cpi_dates"] = str(e)
        return out

    def web_all():
        out = {}
        for k, (ds, code) in COT_SPECS.items():
            try:
                out[k], _ = src.cftc_cot(session, ds, code, since3y)
            except SourceUnavailable as e:
                errors[f"cftc:{k}"] = str(e)
        try:
            out["slv_trust"] = src.ishares_slv(session)
        except SourceUnavailable as e:
            errors["ishares"] = str(e)
        try:
            out["fomc"], _ = src.fomc_calendar(session)
        except SourceUnavailable as e:
            errors["federalreserve"] = str(e)
        return out

    def aum_all():
        out = {}
        for grp in F.LEVERED.values():
            for t in grp:
                try:
                    out[t] = src.yf_total_assets(session, t)
                except SourceUnavailable as e:
                    errors[f"aum:{t}"] = str(e)
        return out
    with ThreadPoolExecutor(max_workers=3) as pool:
        f1, f2, f3 = pool.submit(fred_all), pool.submit(web_all), pool.submit(aum_all)
        fred, web, aum = f1.result(), f2.result(), f3.result()
    hist = [(r["observed_at"][:10], r["value_num"]) for r in conn.execute(
        "SELECT observed_at, value_num FROM v2_observations WHERE metric_id = 'slv.ounces_in_trust' ORDER BY observed_at")]
    inputs = {"today": today, "frames": frames, "fred": fred, "aum": aum,
              "spot": {"SPY": prices["spy"].get("value"), "SLV": prices["slv"].get("value")},
              "chains": {k: (options.get(k) or {}).get("_chains") or {} for k in ("SPY", "SLV")},
              "gex": {k: (options.get(k) or {}).get("gex") or {} for k in ("SPY", "SLV")},
              "cot": {"silver": web.get("silver"), "sp": web.get("sp")}, "slv_trust": web.get("slv_trust"),
              "slv_trust_history": hist, "fomc": web.get("fomc"), "cpi": fred.pop("_cpi", None)}
    try:
        fc = F.build(inputs)
    except Exception as e:
        import traceback
        return {"status": "error", "reason": lake.redact(f"{type(e).__name__}: {e}"),
                "trace": lake.redact(traceback.format_exc()[-1500:]), "source_errors": {k: lake.redact(v) for k, v in errors.items()}}
    fc["status"] = "partial" if errors else "fresh"
    fc["source_errors"] = {k: lake.redact(v) for k, v in errors.items()}
    return fc


def _guarded_refining(conn, session, run_id, today):
    """The refining card is optional: a failure there must not abort the run (same contract as build_forecast)."""
    try:
        return build_refining(conn, session, run_id, today)
    except Exception as e:
        import traceback
        return {"status": "error", "reason": lake.redact(f"{type(e).__name__}: {e}"),
                "trace": lake.redact(traceback.format_exc()[-1500:])}


def build_refining(conn, session, run_id, today):
    """Diesel & Refining layer (public sources only). Refinery events are captured into the lake as they are seen,
    so the outage history accumulates even though the TCEQ feed only holds ~5 days."""
    import json as _json
    from core import forecast as F
    from core import refining as RF
    errors = {}
    since3y = (today - timedelta(days=3 * 365 + 7)).isoformat()

    def eia_all():
        out = {}

        def one(item):
            key, sid = item
            try:
                out[key], _ = src.eia_weekly(session, sid)
            except SourceUnavailable as e:
                errors[f"eia:{sid}"] = str(e)
        with ThreadPoolExecutor(max_workers=5) as p2:     # EIA serves static files; modest parallelism
            list(p2.map(one, RF.EIA_SERIES.items()))
        return out

    def futures():
        out = {}
        for key, sym in (("ho", "HO=F"), ("rb", "RB=F"), ("cl", "CL=F")):
            try:
                df, _ = src.yf_history(session, sym, "5y", "1d")
                out[key] = df["Close"].dropna()
            except SourceUnavailable as e:
                errors[f"yahoo:{sym}"] = str(e)
        try:
            out["cot"], _ = src.cftc_cot(session, "72hh-3qpy", "022651", since3y)
        except SourceUnavailable as e:
            errors["cftc:ulsd"] = str(e)
        return out

    def capacity_and_feed():
        out = {}
        try:
            out["capacity"], out["capacity_year"], _ = src.eia_refinery_capacity(session)
        except SourceUnavailable as e:
            errors["eia:refcap"] = str(e)
        try:
            out["feed"], _ = src.tceq_feed(session)
        except SourceUnavailable as e:
            errors["tceq:feed"] = str(e)
        return out
    with ThreadPoolExecutor(max_workers=3) as pool:
        f1, f2, f3 = pool.submit(eia_all), pool.submit(futures), pool.submit(capacity_and_feed)
        eia, fut, cf = f1.result(), f2.result(), f3.result()
    capacity = cf.get("capacity")

    # --- outage capture: fetch details only for refinery-like facilities not yet captured
    seen = {r[0] for r in conn.execute("SELECT entity FROM v2_observations WHERE metric_id = 'refinery.event'")}
    new_events, new_major = [], []
    cands = [i for i in (cf.get("feed") or []) if i.get("event_id") and RF.looks_like_refinery(i.get("name"))]
    for item in [i for i in cands if i["event_id"] not in seen][:15]:
        try:
            ev = RF.enrich_event(src.tceq_event(session, item["event_id"]), capacity)
        except SourceUnavailable as e:
            errors[f"tceq:{item['event_id']}"] = str(e)
            continue
        new_events.append(lake.obs("refinery.event", ev.get("start") or today.isoformat(), entity=ev["event_id"],
                                   value=ev, source="tceq", dims={"kind": ev["kind"], "is_refinery": ev["is_refinery"]}))
        if ev["major"]:
            new_major.append(ev)
    if new_events:
        lake.record_observations(conn, new_events, run_id=run_id)
    history = [_json.loads(r[0]) for r in conn.execute(
        "SELECT value_json FROM v2_observations WHERE metric_id = 'refinery.event' ORDER BY observed_at DESC LIMIT 500")]
    cot = fut.get("cot")
    ulsd_cot = None
    # Socrata omits null fields: keep only complete rows
    cot = [r for r in cot or [] if all(r.get(k) is not None for k in (
        "report_date_as_yyyy_mm_dd", "open_interest_all", "m_money_positions_long_all", "m_money_positions_short_all"))]
    if cot:
        d = [r["report_date_as_yyyy_mm_dd"][:10] for r in cot]
        oi = [float(r["open_interest_all"]) for r in cot]
        mm = [float(r["m_money_positions_long_all"]) - float(r["m_money_positions_short_all"]) for r in cot]
        ulsd_cot = F._cot_stats(d, mm, oi, fut.get("ho"))
    out = {"version": RF.VERSION, "status": "partial" if errors else "fresh",
           "margins": RF.crack_spreads(fut.get("ho"), fut.get("rb"), fut.get("cl")),
           "fundamentals": RF.eia_fundamentals(eia),
           "maintenance": RF.maintenance_outlook(eia.get("util_us")),
           "inventories": RF.inventories(eia),
           "outages": {**RF.outage_summary(history, today), "new_this_run": len(new_events),
                       "new_major_this_run": new_major, "feed_items": len(cf.get("feed") or []),
                       "refinery_items_in_feed": len(cands)},
           "top_refineries": RF.refinery_table(capacity), "capacity_report_year": cf.get("capacity_year"),
           "ulsd_positioning": ulsd_cot, "source_errors": {k: lake.redact(v) for k, v in errors.items()}}
    return out


def _spot(session):
    try:
        return src.goldapi_spot(session, config.GOLD_API_KEY)
    except SourceUnavailable as e:
        return {"value": None, "change": None, "status": "missing", "reason": str(e), "source": "goldapi"}


# ============================================================ observations from a context
def context_observations(ctx, frames=None):
    from core.catalog import metric_rows, resolve, _section_meta
    run = ctx["run"]
    recs = []
    for m in metric_rows():
        val = resolve(ctx, m["path"])
        if isinstance(val, (dict, list)):
            continue
        meta = _section_meta(ctx, m["path"])
        observed = meta.get("observed_at") or meta.get("report_date") or run["generated_at"]
        status = meta.get("status") or ("fresh" if val is not None else "missing")
        if val is None and status in ("fresh", "cached", "stale"):
            status = "missing"
        recs.append(lake.obs(m["id"], str(observed), value=val, unit=m["unit"], status=status,
                             reason=meta.get("reason") or (None if val is not None else "not available this run"),
                             source=str(meta.get("source") or "derived"), payload_id=meta.get("payload_id"),
                             derivation=meta.get("version"), dims={"run_id": run["run_id"]}))
    for e in ctx["calendar"].get("events", []):
        recs.append(lake.obs("calendar.event", e.get("utc") or e.get("date_iso") or e["date"], entity=e["country"],
                             dims={"title": e["title"], "impact": e["impact"]}, value=e, source="forexfactory",
                             payload_id=(ctx["calendar"].get("payloads") or {}).get(e.get("feed"))))
    for sym in ("SPY", "SLV"):
        o = ctx["options"].get(sym) or {}
        if o.get("gex"):
            recs.append(lake.obs(f"options.{sym}.gex_profile", run["generated_at"], entity=sym, value=o["gex"],
                                 source="derived", derivation=metrics.GEX_VERSION, status=o["gex"].get("status", "fresh"),
                                 reason=o["gex"].get("reason"), inputs={"payloads": o.get("payloads")}))
        for label, c in (o.get("top") or {}).items():
            recs.append(lake.obs(f"options.{sym}.selection", run["generated_at"], entity=sym, dims={"rank": label},
                                 value=c, source="derived"))
    for sym in ("SPY", "SLV", "SPY_5d"):
        f = ctx["flow"].get(sym) or {}
        recs.append(lake.obs(f"flow.{sym}.summary", f.get("session") or run["generated_at"], entity=sym, value=f,
                             source="derived", derivation=metrics.FLOW_VERSION, status=f.get("status", "missing"),
                             reason=f.get("reason"), payload_id=f.get("payload_id")))
    trades = (frames or {}).get("trades", {})
    for sym, df in trades.items():
        if sym == "SPY_5d" or df is None or len(df) == 0:
            continue
        blocks = df[df["size"] >= metrics.BLOCK_MIN_SIZE]
        pid = (ctx["flow"].get(sym) or {}).get("payload_id")
        for ts, row in blocks.iterrows():
            recs.append(lake.obs("trade.block", pd.Timestamp(ts).isoformat(), entity=sym, source="databento:DBEQ.BASIC",
                                 value={k: (v.item() if hasattr(v, "item") else v) for k, v in row.items()},
                                 dims={"side_raw": str(row.get("side")), "aggressor": metrics.normalize_side(row.get("side"))},
                                 payload_id=pid))
    trust = ((ctx.get("forecast") or {}).get("positioning") or {}).get("slv_trust") or {}
    if trust.get("ounces_in_trust") and trust.get("as_of"):
        recs.append(lake.obs("slv.ounces_in_trust", trust["as_of"], entity="SLV", value=trust["ounces_in_trust"],
                             unit="troy_oz", source="ishares.com", time_quality="as_of_date"))
    for m in ctx.get("position_marks") or []:
        recs.append(lake.obs("position.mark", run["generated_at"], entity=m["contract"], value=m, source="yahoo:option_chain"))
    recs.append(lake.obs("execution.engine", run["generated_at"], value=ctx["execution"], source="derived",
                         derivation=metrics.EXECUTION_VERSION, status=ctx["execution"].get("status", "missing"),
                         reason=ctx["execution"].get("reason")))
    recs.append(lake.obs("vmri.components", run["generated_at"], value=ctx["vmri"], source="derived",
                         derivation=metrics.VMRI_VERSION, status=ctx["vmri"].get("status"), reason=ctx["vmri"].get("reason")))
    for l in ctx["ebay"].get("listings", []):
        recs.append(lake.obs("ebay.listing", run["generated_at"], entity=l["item_id"], value=l, source="ebay_browse_api",
                             payload_id=l.get("payload_id")))
    return recs
