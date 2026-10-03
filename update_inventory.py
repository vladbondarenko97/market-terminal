"""COMEX silver inventory: bounded acquisition + header-based parsing (core/cme.py).

Returns the inventory history as a DataFrame keyed by REPORT date (not download date). Registered/eligible are
troy ounces. A failed download never writes zeros; saved history is returned with its real dates.
"""
import pandas as pd

from config import DATA_DIR, DB_PATH
from core import history, lake
from core.collect import acquire_cme
from core.sources import SourceSession


def update_silver_inventory(download=True):
    conn = lake.connect(DB_PATH)
    lake.migrate(conn)
    if download:
        out = acquire_cme(conn, SourceSession(conn, "manual-inventory"), "manual-inventory", DATA_DIR,
                          offline=False, max_volume_files=0)
        print(f"COMEX inventory: {out['inventory'].get('outcome')} {out['inventory'].get('report_date') or ''}")
    series = history.inventory_series(conn)
    conn.close()
    df = pd.DataFrame(series)
    if df.empty:
        return None
    return df.rename(columns={"date": "Date", "registered": "Registered", "eligible": "Eligible", "total": "Total",
                              "registered_net_change": "Reg_Change", "eligible_net_change": "Elig_Change",
                              "total_net_change": "Total_Change"})


if __name__ == "__main__":
    print(update_silver_inventory().tail())
