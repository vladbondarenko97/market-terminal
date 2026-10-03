import os
import sys
import pandas as pd
import yfinance as yf

# Ensure config and core can be imported
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from core.sqlite_layer import get_connection
from core import lake


def backfill():
    """Adds missing daily BTC/metals rows to crypto_metrics_history. Existing rows are never replaced."""
    print("Fetching 1 year of historical data for BTC, Silver, and Gold...")

    btc = yf.Ticker("BTC-USD").history(period="1y")
    sil = yf.Ticker("SI=F").history(period="1y")
    gld = yf.Ticker("GC=F").history(period="1y")

    btc.index = btc.index.tz_localize(None).normalize()
    sil.index = sil.index.tz_localize(None).normalize()
    gld.index = gld.index.tz_localize(None).normalize()

    df = pd.DataFrame(index=btc.index)
    df['BTC_Price'] = btc['Close']
    df['Silver_Price'] = sil['Close']
    df['Gold_Price'] = gld['Close']
    # Metals don't trade on weekends; the weekend rows carry the prior close (operand dates differ).
    df.ffill(inplace=True)
    df.dropna(inplace=True)

    df['Silver_BTC_Ratio'] = df['BTC_Price'] / df['Silver_Price']
    df['Gold_BTC_Ratio'] = df['BTC_Price'] / df['Gold_Price']

    df.reset_index(inplace=True)
    df.rename(columns={'index': 'Date'}, inplace=True)
    df['Date'] = df['Date'].dt.strftime('%Y-%m-%d')

    conn = get_connection()
    try:
        existing = {r[0] for r in conn.execute("SELECT Date FROM crypto_metrics_history")} \
            if lake.table_columns(conn, "crypto_metrics_history") else set()
        added = 0
        for rec in df.to_dict(orient="records"):
            if rec["Date"] in existing:
                continue
            lake.append_legacy_row(conn, "crypto_metrics_history", rec)
            added += 1
    finally:
        conn.close()
    print(f"✅ Added {added} missing daily rows (existing rows untouched).")


if __name__ == "__main__":
    backfill()
