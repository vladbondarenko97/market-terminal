"""Read-side history queries over the lake and legacy tables, with explicit as-of cutoffs."""
import json
from datetime import datetime, timedelta, timezone

import pandas as pd

from core import lake


def cme_series(conn, key, as_of=None):
    """Daily volume/OI for one dashboard product, one row per trade date (latest fetch wins on revisions).
    OI change is computed across the full ordered history, so a window keeps the preceding session."""
    rows = conn.execute(
        """SELECT metric_id, observed_at, value_num, fetched_at, obs_id FROM v2_observations
           WHERE entity = ? AND metric_id IN (?, ?) AND source LIKE 'cme:%' AND (? IS NULL OR observed_at <= ?)
           ORDER BY observed_at, fetched_at, obs_id""",
        (key, f"cme.{key}.volume", f"cme.{key}.open_interest", as_of, as_of)).fetchall()
    by_date = {}
    for r in rows:
        field = "volume" if r["metric_id"].endswith(".volume") else "open_interest"
        by_date.setdefault(r["observed_at"][:10], {})[field] = r["value_num"]
    out, prev_oi = [], None
    for d in sorted(by_date):
        v = by_date[d]
        oi = v.get("open_interest")
        out.append({"date": d, "volume": v.get("volume"), "open_interest": oi,
                    "oi_change": (oi - prev_oi) if (oi is not None and prev_oi is not None) else None})
        if oi is not None:
            prev_oi = oi
    return out


def inventory_series(conn, as_of=None):
    """COMEX silver inventory by REPORT date from parsed workbooks. Legacy CSV/DB rows are included only for
    dates no workbook covers and whose values don't match any parsed report (they carry download dates)."""
    rows = conn.execute(
        """SELECT metric_id, observed_at, value_num, payload_id, dims_json FROM v2_observations
           WHERE entity = 'SILVER' AND metric_id LIKE 'comex.silver.%' AND source LIKE 'cme:%'
           AND (? IS NULL OR observed_at <= ?) ORDER BY observed_at, fetched_at""", (as_of, as_of)).fetchall()
    reports = {}
    for r in rows:
        name = r["metric_id"].split(".", 2)[2]
        if name in ("depository", "reconciliation"):
            continue
        rec = reports.setdefault(r["observed_at"][:10], {"date": r["observed_at"][:10], "source": "workbook",
                                                         "payload_id": r["payload_id"]})
        rec[name] = r["value_num"]
    known_pairs = {(round(v.get("registered") or 0), round(v.get("eligible") or 0)) for v in reports.values()}
    if lake.table_columns(conn, "comex_inventory_history"):
        for r in conn.execute("SELECT * FROM comex_inventory_history"):
            d = str(r["Date"])[:10]
            if as_of and d > as_of[:10]:
                continue
            pair = (round(r["Registered"] or 0), round(r["Eligible"] or 0))
            if d in reports or pair in known_pairs:
                continue
            reports[d] = {"date": d, "registered": r["Registered"], "eligible": r["Eligible"], "total": r["Total"],
                          "total_net_change": r["Total_Change"], "registered_net_change": r["Reg_Change"],
                          "source": "legacy_table (download-dated)"}
            known_pairs.add(pair)
    return [reports[d] for d in sorted(reports)]


def legacy_frame(conn, table, time_col, as_of=None, where=None, params=()):
    if not lake.table_columns(conn, table):
        return pd.DataFrame()
    q = f'SELECT * FROM "{table}"' + (f" WHERE {where}" if where else "")
    df = pd.read_sql(q, conn, params=params)
    if df.empty:
        return df
    df["_ts"] = pd.to_datetime(df[time_col].astype(str), format="mixed", errors="coerce")
    df = df.dropna(subset=["_ts"]).sort_values("_ts")
    if as_of:
        df = df[df["_ts"] <= as_of_local(as_of)]
    return df


def as_of_local(as_of):
    """Legacy tables hold naive Chicago times; convert an ISO cutoff to that convention."""
    cutoff = pd.Timestamp(as_of)
    if cutoff.tzinfo is not None:
        return cutoff.tz_convert("America/Chicago").tz_localize(None)
    return cutoff + pd.Timedelta(days=1) if len(str(as_of)) <= 10 else cutoff


def daily_last(df):
    """One row per calendar day (the last observation of that day)."""
    if df.empty:
        return df
    df = df.copy()
    df["_day"] = df["_ts"].dt.normalize()
    return df.groupby("_day").tail(1).reset_index(drop=True)


def crypto_daily(conn, as_of=None):
    return daily_last(legacy_frame(conn, "crypto_metrics_history", "Date", as_of))


def macro_daily(conn, as_of=None):
    return daily_last(legacy_frame(conn, "macro_master_ledger", "Datetime", as_of))


def flow_ledger_daily(conn, ticker, as_of=None):
    return daily_last(legacy_frame(conn, "equities_darkpool_gex_ledger", "Date", as_of, "Ticker = ?", (ticker,)))


def eagle_listing_runs(conn, as_of=None):
    """The live runs' eBay listings from the 36 days before `as_of` (the newest run when None), oldest run first:
    [{"run_at": aware datetime, "listings": {item_id: listing dict}}]."""
    rows = conn.execute(
        """SELECT o.run_id, o.entity, o.observed_at, o.value_json FROM v2_observations o
           JOIN v2_runs r ON r.run_id = o.run_id
           WHERE o.metric_id = 'ebay.listing' AND r.mode = 'live'
             AND r.status IN ('committed', 'completed', 'completed_with_warnings')
             AND (? IS NULL OR o.observed_at <= ?)
             AND o.observed_at >= datetime(COALESCE(?, (SELECT MAX(observed_at) FROM v2_observations
                                                        WHERE metric_id = 'ebay.listing')), '-37 days')""",
        (as_of, as_of, as_of)).fetchall()
    runs = {}
    for r in rows:
        try:
            at = datetime.fromisoformat(r["observed_at"])
            listing = json.loads(r["value_json"])
        except (TypeError, ValueError):
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        runs.setdefault(r["run_id"], {"run_at": at, "listings": {}})["listings"][r["entity"]] = listing
    ordered = sorted(runs.values(), key=lambda run: run["run_at"])
    if ordered:
        anchor = ordered[-1]["run_at"]
        if as_of:
            anchor = datetime.fromisoformat(as_of)
            anchor = anchor if anchor.tzinfo else anchor.replace(tzinfo=timezone.utc)
        floor = anchor - timedelta(days=36)
        ordered = [run for run in ordered if run["run_at"] >= floor]
    return ordered
