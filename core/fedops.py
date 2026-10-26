"""Fed and Treasury operations (card 15): NY Fed repo and reverse repo operations, the NY Fed reference rates, Treasury
buybacks and Treasury auctions.

History lives in v2_observations, one JSON row per market-day, rate-day, buyback operation or auction. A revised row (an
announced buyback that now has results, a revised rate) is a new observation and the latest fetch wins. The pipeline tops up
the last weeks on every run; scripts/backfill_fed_ops.py loads the years before. build() turns the stored rows into the
snapshot's fed_ops section: the latest readings plus precomputed monthly, quarterly, yearly and trailing-12-month summaries.
"""
import json
import re
from collections import defaultdict
from datetime import date, timedelta

from core import lake
from core import sources as src

FEDOPS_VERSION = "fed_ops_v2"        # v2: auctions.upcoming from the newest captured calendar; upcoming_status fresh/stale/missing with as_of
REPO, RATE, BUYBACK, AUCTION = "fedops.repo_day", "fedops.ref_rate", "fedops.buyback", "fedops.auction"
MARKETS = {"rrp": "Overnight reverse repo (ON RRP)", "repo": "Overnight repo (Standing Repo Facility since July 2021)",
           "repo_term": "Term repo", "rrp_term": "Term reverse repo"}
RATES = ("SOFR", "TGCR", "BGCR", "EFFR")
# How far back the backfill goes: the NY Fed repo results start in the 2010s, SOFR / TGCR / BGCR on 2018-04-02, the buyback
# program restarted in 2024 and TreasuryDirect search covers any year (five years is plenty for tenor comparisons).
EARLIEST = {REPO: "2014-01-01", RATE: "2018-04-02", BUYBACK: "2024-01-01", AUCTION: "2021-01-01"}
TOPUP_DAYS = 21           # each pipeline run refetches this many days of repo operations and rates
TAIL_REASON = ("an auction tail is the high yield minus the when-issued yield at the bid deadline; no free official source "
               "publishes the when-issued yield, so tails are not computed")
BILLS = ("Bill", "CMB")


def _bn(v):
    v = src._num(v)
    return round(v / 1e9, 3) if v is not None else None


def _rate(op):
    """The operation's rate: the award rate (reverse repo), the weighted average (competitive term repo) or the minimum bid
    rate (full-allotment overnight repo, the Standing Repo Facility rate)."""
    for d in op.get("details") or []:
        for k in ("percentAwardRate", "percentWeightedAverageRate", "percentOfferingRate"):
            if d.get(k) is not None:
                return d[k]
    return None


# ---------------------------------------------------------------- provider rows -> observations
def repo_records(ops, payload_id=None):
    """NY Fed operations -> one row per market and day: submitted and accepted (USD bn, summed over the day's operations),
    the rate of the day's last operation, the number of operations and accepted counterparties (reverse repo only)."""
    days = defaultdict(lambda: {"submitted_bn": 0.0, "accepted_bn": 0.0, "rate": None, "ops": 0, "counterparties": None, "_close": ""})
    for op in ops:
        if op.get("auctionStatus") != "Results" or not op.get("operationDate"):
            continue
        market = ("rrp" if op.get("operationType") == "Reverse Repo" else "repo") + ("" if op.get("term") == "Overnight" else "_term")
        d = days[(market, op["operationDate"])]
        for key, field in (("submitted_bn", "totalAmtSubmitted"), ("accepted_bn", "totalAmtAccepted")):
            v = src._num(op.get(field))           # one missing amount makes the day's total missing, never a smaller number
            d[key] = None if v is None or d[key] is None else d[key] + v / 1e9
        d["ops"] += 1
        if op.get("acceptedCpty") is not None:
            d["counterparties"] = (d["counterparties"] or 0) + op["acceptedCpty"]
        rate = _rate(op)
        if rate is not None and (op.get("closeTime") or "") >= d["_close"]:
            d["rate"], d["_close"] = rate, op.get("closeTime") or ""
    return [lake.obs(REPO, day, entity=market, value={k: (round(v, 3) if isinstance(v, float) else v) for k, v in d.items() if k != "_close"},
                     source="nyfed", source_field="rp/results", payload_id=payload_id, time_quality="date")
            for (market, day), d in days.items()]


def rate_records(rows, payload_id=None):
    out = []
    for r in rows:
        if r.get("type") in RATES and r.get("effectiveDate") and r.get("percentRate") is not None:
            v = {"rate": r["percentRate"], "p1": r.get("percentPercentile1"), "p99": r.get("percentPercentile99"),
                 "volume_bn": r.get("volumeInBillions")}
            if r.get("targetRateFrom") is not None:
                v.update(target_low=r["targetRateFrom"], target_high=r.get("targetRateTo"))
            out.append(lake.obs(RATE, r["effectiveDate"], entity=r["type"], value=v, source="nyfed", source_field="rates",
                                payload_id=payload_id, time_quality="date"))
    return out


def buyback_records(rows, payload_id=None):
    """Fiscal Data buyback rows (sources.BUYBACK_KEYS) -> one row per operation, keyed by date, operation type, security
    type and maturity bucket; amounts in USD bn, None until results are out."""
    out = []
    for r in rows:
        if not r.get("operation_date"):
            continue
        v = {"settlement_date": r.get("settlement_date"), "operation_type": r.get("operation_type"), "security_type": r.get("security_type"),
             "maturity_bucket": r.get("maturity_bucket"), "max_bn": _bn(r.get("max_par_amt_redeemed")),
             "offered_bn": _bn(r.get("total_par_amt_offered")), "accepted_bn": _bn(r.get("total_par_amt_accepted")),
             "issues_accepted": src._num(r.get("nbr_issues_accepted")), "issues_eligible": src._num(r.get("nbr_issues_eligible"))}
        out.append(lake.obs(BUYBACK, r["operation_date"], entity=f"{r.get('operation_type')}|{r.get('security_type')} {r.get('maturity_bucket')}", value=v,
                            source="treasury", source_field="buybacks_operations", payload_id=payload_id, time_quality="date"))
    return out


def auction_records(rows, payload_id=None):
    """sources.auction_row()s -> one row per held auction (keyed by CUSIP and date). Announced auctions without results are skipped."""
    keep = lambda r: {k: v for k, v in r.items() if k not in ("auctionDate", "securityType", "offeringAmount", "date", "cusip")}
    return [lake.obs(AUCTION, r["date"], entity=r["cusip"], value=keep(r), source="treasury", source_field="TA_WS securities",
                     payload_id=payload_id, time_quality="date")
            for r in rows if r.get("date") and r.get("cusip") and r.get("bid_to_cover") is not None]


def stored_upcoming(conn, today, auctions=()):
    """(announced auctions on or after today without a stored result, soonest first; when the calendar was fetched) from the
    newest captured TreasuryDirect calendar. Only the newest capture counts, so a postponed or cancelled auction drops out."""
    with lake._WRITE_LOCK:
        row = conn.execute("SELECT payload_id, fetched_at FROM v2_payloads WHERE kind = 'treasury_upcoming' "
                           "ORDER BY fetched_at DESC LIMIT 1").fetchone()
        content = lake.load_payload(conn, row["payload_id"])[1] if row else None
    if not content:
        return [], None
    held = {(r["entity"], r["date"]) for r in auctions}
    rows = [src.auction_row(x) for x in json.loads(content)]
    keep = ("date", "term", "type", "size_bn", "reopening", "issue_date", "cusip")
    return (sorted(({k: r.get(k) for k in keep} for r in rows if r.get("date") and r["date"] >= today.isoformat()
                    and (r.get("cusip"), r["date"]) not in held), key=lambda r: r["date"]), row["fetched_at"][:10])


_UNITS = {"y": "year", "yr": "year", "yrs": "year", "year": "year", "years": "year", "w": "week", "wk": "week", "wks": "week",
          "week": "week", "weeks": "week", "m": "month", "mo": "month", "month": "month", "months": "month", "d": "day", "day": "day", "days": "day"}
_KINDS = {"bill": "Bill", "bills": "Bill", "note": "Note", "notes": "Note", "bond": "Bond", "bonds": "Bond", "tips": "TIPS",
          "frn": "FRN", "frns": "FRN", "cmb": "CMB", "cmbs": "CMB"}


def _tenor_parts(text):
    """'30 years', '30-Year Bond', '13wk bills' -> ((30, 'year') or None, {'Bond'})."""
    s = str(text or "").lower()
    m = re.search(r"(\d+)\s*-?\s*(years?|yrs?|y|weeks?|wks?|w|months?|mo|m|days?|d)\b", s)
    return ((int(m.group(1)), _UNITS[m.group(2)]) if m else None), {_KINDS[w] for w in re.findall(r"[a-z]+", s) if w in _KINDS}


def tenor_matches(query, tenor_name):
    """Whether a question's tenor ('30 year', '30yrs bond', '6 week', 'bills') names this tenor ('30-Year Bond'). The number
    must match whole ('6 week' is not the 26-week); other words ('treasury', 'auctions') are ignored."""
    q_len, q_kinds = _tenor_parts(query)
    t_len, t_kinds = _tenor_parts(tenor_name)
    return (q_len is None or q_len == t_len) and (not q_kinds or bool(q_kinds & t_kinds))


# ---------------------------------------------------------------- reading the stored history
def read(conn, metric, start=None, end=None, entity=None):
    """Stored rows of one metric as [{date, entity, **value}], oldest first; the latest fetch of each (entity, date) wins."""
    sql, args = "SELECT entity, substr(observed_at, 1, 10), value_json FROM v2_observations WHERE metric_id = ?", [metric]
    for cond, val in (("observed_at >= ?", start), ("observed_at <= ?", f"{end}T99" if end else None), ("entity = ?", entity)):
        if val:
            sql, args = f"{sql} AND {cond}", args + [str(val)]
    with lake._WRITE_LOCK:
        rows = conn.execute(sql + " ORDER BY fetched_at, obs_id", args).fetchall()
    latest = {(e, d): v for e, d, v in rows if v}
    return [{"date": d, "entity": e, **json.loads(v)} for (e, d), v in sorted(latest.items(), key=lambda kv: (kv[0][1], kv[0][0]))]


def earliest(conn, metric):
    with lake._WRITE_LOCK:
        row = conn.execute("SELECT MIN(observed_at) FROM v2_observations WHERE metric_id = ?", (metric,)).fetchone()
    return row[0][:10] if row and row[0] else None


def repo_daily(conn, start=None, end=None):
    """One compact row per day: accepted USD bn per market and the reference rates (percent), SOFR volume (USD bn)."""
    return daily_rows(read(conn, REPO, start, end), read(conn, RATE, start, end))


def daily_rows(repo_rows, rate_rows):
    days = defaultdict(dict)
    for r in repo_rows:
        days[r["date"]][f"{r['entity']}_bn"] = r["accepted_bn"]
    for r in rate_rows:
        days[r["date"]][r["entity"].lower()] = r["rate"]
        if r["entity"] == "SOFR":
            days[r["date"]]["sofr_volume_bn"] = r.get("volume_bn")
    return [{"date": d, **days[d]} for d in sorted(days)]


# ---------------------------------------------------------------- summaries
def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _sum(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs), 3) if xs else None


def _quarter(d):
    return f"{d[:4]}-Q{(int(d[5:7]) - 1) // 3 + 1}"


def repo_monthly(daily):
    """Monthly means of the daily rows (reverse repo and repo take-up in USD bn, rates in percent)."""
    months = defaultdict(list)
    for r in daily:
        months[r["date"][:7]].append(r)
    keys = ("rrp_bn", "repo_bn", "sofr", "tgcr", "bgcr", "effr", "sofr_volume_bn")
    return [{"month": m, **{k: _mean([r.get(k) for r in rs]) for k in keys}} for m, rs in sorted(months.items())]


def _buyback_totals(rows):
    return {"ops": len(rows), "max_bn": _sum(r["max_bn"] for r in rows), "offered_bn": _sum(r["offered_bn"] for r in rows),
            "accepted_bn": _sum(r["accepted_bn"] for r in rows)}


def buyback_summary(rows, today):
    """Held operations only (accepted known): totals by month, quarter, year (with year-to-date against the same dates a year
    earlier), the trailing 12 months against the 12 before, and the trailing 12 months by security type."""
    held = [r for r in rows if r.get("accepted_bn") is not None]
    group = lambda key: [{"period": p, **_buyback_totals([r for r in held if key(r["date"]) == p])}
                         for p in sorted({key(r["date"]) for r in held})]
    t = today.isoformat()
    y1, y2 = (today - timedelta(days=365)).isoformat(), (today - timedelta(days=730)).isoformat()
    cut = t[5:]
    years = []
    for y in sorted({r["date"][:4] for r in held}):
        ytd = [r for r in held if r["date"][:4] == y and r["date"][5:] <= cut]
        in_y = [r for r in held if r["date"][:4] == y]
        types = defaultdict(list)
        for r in in_y:
            types[str(r.get("operation_type"))].append(r)
        years.append({"year": y, **_buyback_totals(in_y), "ytd_to": cut, "ytd": _buyback_totals(ytd),
                      "by_operation_type": {k: _buyback_totals(v) for k, v in sorted(types.items())}})
    last12 = [r for r in held if r["date"] > y1]
    by_type = {}
    for r in last12:
        by_type.setdefault(r["security_type"], []).append(r)
    top = sorted(held, key=lambda r: r["accepted_bn"], reverse=True)[:5]
    return {"monthly": group(lambda d: d[:7]), "quarterly": group(_quarter), "yearly": years,
            "largest_5": [{k: r.get(k) for k in ("date", "operation_type", "security_type", "maturity_bucket", "max_bn", "offered_bn", "accepted_bn")} for r in top],
            "t12m": {"from": y1, **_buyback_totals(last12)},
            "prior_t12m": {"from": y2, **_buyback_totals([r for r in held if y2 < r["date"] <= y1])},
            "t12m_by_security_type": {str(k): _buyback_totals(v) for k, v in sorted(by_type.items(), key=lambda kv: str(kv[0]))}}


def tenor(r):
    """'10-Year Note', '13-Week Bill', '5-Year TIPS': the original term with the instrument type."""
    return f"{r.get('term')} {r.get('type')}"


def auction_summary(rows, today):
    """Per tenor: the latest result and the mean bid-to-cover over the last 12 months against the 12 before. Monthly gross
    offering by bills and coupons, and the trailing-12-month totals."""
    y1, y2 = (today - timedelta(days=365)).isoformat(), (today - timedelta(days=730)).isoformat()
    by = defaultdict(list)
    for r in rows:
        by[tenor(r)].append(r)
    tenors = {}
    for k, rs in sorted(by.items()):
        last = rs[-1]
        if last["date"] <= y2:            # a tenor not auctioned for two years (the 1-day CMB of 2023)
            continue
        recent, prior = [r for r in rs if r["date"] > y1], [r for r in rs if y2 < r["date"] <= y1]
        tenors[k] = {"last_date": last["date"], "last_high": last.get("high_yield") if last.get("high_yield") is not None else last.get("high_rate"),
                     "last_bid_to_cover": last.get("bid_to_cover"), "last_size_bn": last.get("size_bn"), "last_dealer_pct": last.get("dealer_pct"),
                     "n_12m": len(recent), "bid_to_cover_12m": _mean(r["bid_to_cover"] for r in recent),
                     "bid_to_cover_prior_12m": _mean(r["bid_to_cover"] for r in prior), "dealer_pct_12m": _mean(r.get("dealer_pct") for r in recent),
                     "by_year": {y: {"auctions": len(ys), "size_bn": _sum(r.get("size_bn") for r in ys),
                                     "bid_to_cover_avg": _mean(r["bid_to_cover"] for r in ys),
                                     "high_avg": _mean((r.get("high_yield") if r.get("high_yield") is not None else r.get("high_rate")) for r in ys),
                                     "dealer_pct_avg": _mean(r.get("dealer_pct") for r in ys)}
                                 for y in sorted({r["date"][:4] for r in rs}) for ys in [[r for r in rs if r["date"][:4] == y]]}}
    months = defaultdict(list)
    for r in rows:
        months[r["date"][:7]].append(r)
    split = lambda rs: {"bills_bn": _sum(r["size_bn"] for r in rs if r.get("type") in BILLS),
                        "coupons_bn": _sum(r["size_bn"] for r in rs if r.get("type") not in BILLS), "auctions": len(rs)}
    return {"by_tenor": tenors, "monthly": [{"month": m, **split(rs)} for m, rs in sorted(months.items())],
            "t12m": {"from": y1, **split([r for r in rows if r["date"] > y1])},
            "prior_t12m": {"from": y2, **split([r for r in rows if y2 < r["date"] <= y1])}}


def _part(rows, errors, keys):
    """Status of one part: stale (with the failed fetch named) when this run could not refresh it, missing when nothing is stored."""
    why = "; ".join(errors[k] for k in keys if errors.get(k))
    if not rows:
        return {"status": "missing", "reason": why or "nothing stored yet: run scripts/backfill_fed_ops.py or wait for a pipeline run"}
    return {"status": "stale" if why else "fresh", "as_of": rows[-1]["date"], **({"reason": f"not refreshed this run ({why}); showing stored data as of {rows[-1]['date']}"} if why else {})}


def build(conn, today, errors=None):
    """The fed_ops section from the stored history (the snapshot's block, and the routes' fallback before the first run).
    `errors` maps a failed fetch ("nyfed:rp", "nyfed:rates", "treasury:buybacks", "treasury:auctions", "treasury:upcoming") to its reason."""
    errors, t = errors or {}, today.isoformat()
    since = (today - timedelta(days=60)).isoformat()
    repo_rows, rate_rows = read(conn, REPO), read(conn, RATE)
    markets = {}
    for m, label in MARKETS.items():
        rs = [r for r in repo_rows if r["entity"] == m]
        if not rs or rs[-1]["date"] < (today - timedelta(days=365)).isoformat():     # term reverse repo last ran in 2019
            continue
        acc = [r["accepted_bn"] for r in rs]
        markets[m] = {"label": label, "earliest": rs[0]["date"], "latest": rs[-1], "last_10": rs[-10:],
                      "avg_5_bn": _mean(acc[-5:]), "avg_prev_5_bn": _mean(acc[-10:-5]),
                      "max_60d": max(({"date": r["date"], "accepted_bn": r["accepted_bn"]} for r in rs if r["date"] >= since),
                                     key=lambda x: x["accepted_bn"], default=None)}
    rates = {}
    for name in RATES:
        rs = [r for r in rate_rows if r["entity"] == name]
        if rs:
            prior = [r for r in rs if r["date"] <= (date.fromisoformat(rs[-1]["date"]) - timedelta(days=28)).isoformat()]
            rates[name] = {"earliest": rs[0]["date"], "latest": rs[-1],
                           "change_4w_pp": round(rs[-1]["rate"] - prior[-1]["rate"], 4) if prior else None}
    daily = daily_rows(repo_rows, rate_rows)
    buys = [dict(r, weekday=date.fromisoformat(r["date"]).strftime("%A")) for r in read(conn, BUYBACK)]
    held = [r for r in buys if r.get("accepted_bn") is not None]
    auctions = read(conn, AUCTION)
    up, up_at = stored_upcoming(conn, today, [r for r in auctions if r["date"] >= t])
    return {
        "version": FEDOPS_VERSION, "as_of": t,
        "repo": {**_part(repo_rows, errors, ("nyfed:rp",)), "markets": markets, "rates": rates,
                 "rates_status": _part(rate_rows, errors, ("nyfed:rates",)),
                 "monthly": repo_monthly(daily), "last_60d": [r for r in daily if r["date"] >= since],
                 "units": "accepted and submitted in USD bn, rates in percent",
                 "note": "Repo rate: the minimum bid rate of the full-allotment overnight repo (the Standing Repo Facility rate) or the "
                         "weighted average of a competitive term operation. Reverse repo rate: the award rate. Days with no operation of a "
                         "kind have no row."},
        "buybacks": {**_part(held, errors, ("treasury:buybacks",)), "earliest": buys[0]["date"] if buys else None,
                     "last_8": held[-8:], "scheduled": [r for r in buys if r.get("accepted_bn") is None and r["date"] >= t],
                     **buyback_summary(buys, today), "units": "USD bn par; max_bn is the most Treasury said it would buy back. Lists run oldest first"},
        "auctions": {**_part(auctions, errors, ("treasury:auctions",)), "earliest": auctions[0]["date"] if auctions else None,
                     "recent": auctions[-15:],
                     "upcoming": [{k: r.get(k) for k in ("date", "term", "type", "size_bn", "reopening", "issue_date", "cusip")} for r in up],
                     "upcoming_status": {"status": "missing", "reason": errors.get("treasury:upcoming") or "no auction calendar captured yet"} if up_at is None
                                        else {"status": "stale", "as_of": up_at, "reason": f"not refreshed ({errors['treasury:upcoming']}); calendar as fetched on {up_at}"}
                                        if errors.get("treasury:upcoming") else {"status": "fresh", "as_of": up_at},
                     **auction_summary(auctions, today), "tail": {"status": "missing", "reason": TAIL_REASON},
                     "units": "size in USD bn; high_yield (coupons) and high_rate (bills, discount basis) in percent; dealer/direct/"
                              "indirect_pct are shares of the competitive award. Lists run oldest first"},
    }


# ---------------------------------------------------------------- fetching
def topup(conn, session, run_id, today):
    """Refetch the recent window of every source, and the auction calendar, into the lake. Returns the errors. Never raises for a source failure."""
    errors = {}
    start = (today - timedelta(days=TOPUP_DAYS)).isoformat()
    jobs = (("nyfed:rp", lambda: src.nyfed_repo_ops(session, start, today.isoformat()), repo_records),
            ("nyfed:rates", lambda: src.nyfed_ref_rates(session, start, today.isoformat()), rate_records),
            ("treasury:buybacks", lambda: src.treasury_buybacks(session, (today - timedelta(days=56)).isoformat()), buyback_records),
            ("treasury:auctions", lambda: src.treasury_auctions(session, 60), auction_records),
            ("treasury:upcoming", lambda: src.treasury_upcoming(session), lambda rows, pid: []))     # the captured payload is the calendar
    for key, fetch, to_records in jobs:
        try:
            rows, pid = fetch()
            lake.record_observations(conn, to_records(rows, pid), run_id=run_id)
        except src.SourceUnavailable as e:
            errors[key] = lake.redact(str(e))
    return errors
