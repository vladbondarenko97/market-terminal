"""CME volume parsing for the dashboard products, from the lake (trade dates read from inside each workbook).

Works offline with no credentials. Returns the legacy frame
(Product, Type, Volume, Open_Interest, Date, OI_Change) and, when out_path is given, writes it as CSV.
"""
import os
import sys

import pandas as pd

from config import DB_PATH
from core import cme, history, lake


def parse_cme_files(num_days=30, out_path=None):
    conn = lake.connect(DB_PATH)
    lake.migrate(conn)
    rows = []
    for key, name in cme.LEGACY_NAME.items():
        for r in history.cme_series(conn, key)[-num_days:]:
            rows.append({"Product": name, "Type": "O" if key.endswith(("_C", "_P")) else "F", "Volume": r["volume"],
                         "Open_Interest": r["open_interest"], "Date": r["date"], "OI_Change": r["oi_change"]})
    conn.close()
    df = pd.DataFrame(rows)
    if df.empty:
        print("No CME volume history in the lake. Run: python main_pipeline.py import-history")
        return None
    df = df.sort_values(["Product", "Date"])
    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        df.to_csv(out_path, index=False)
    return df


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 30
    print(parse_cme_files(days).tail(10))
