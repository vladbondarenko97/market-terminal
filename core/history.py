"""Read-side history queries over the lake and legacy tables, with explicit as-of cutoffs."""
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
