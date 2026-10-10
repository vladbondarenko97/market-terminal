"""Download SPY one-minute candles for the last 5 days and write them to spy_wicks_1m.json in the project root.

Manual, network-using helper (Yahoo Finance via yfinance); nothing in the pipeline runs it. `main_pipeline.py
import-history` captures the file if it is in the repository root (core/importer.py). It is gitignored.

    .venv/bin/python scripts/dump_spy_wicks.py
"""
import json
import sys
from pathlib import Path

import yfinance as yf

OUTPUT = Path(__file__).resolve().parent.parent / "spy_wicks_1m.json"


def main():
    # The Ticker object avoids the MultiIndex column error that yf.download() can raise.
    spy_data = yf.Ticker("SPY").history(period="5d", interval="1m")

    wick_data = []
    for index, row in spy_data.iterrows():
        # Skip empty/NaN rows that sometimes occur outside market hours
        if not row.isna().any():
            wick_data.append({
                "timestamp": str(index),
                "open": round(row["Open"], 2),
                "high": round(row["High"], 2),
                "low": round(row["Low"], 2),
                "close": round(row["Close"], 2),
                "volume": int(row["Volume"]),
            })

    if not wick_data:
        print("No SPY minute data came back from Yahoo Finance; leaving any existing file untouched.", file=sys.stderr)
        return 1

    with open(OUTPUT, "w") as f:
        json.dump(wick_data, f, indent=4)
    print(f"Wrote {len(wick_data)} minute candles to {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
