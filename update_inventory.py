"""COMEX silver inventory: bounded acquisition + header-based parsing (core/cme.py).

Returns the inventory history as a DataFrame keyed by REPORT date (not download date). Registered/eligible are
troy ounces. A failed download never writes zeros; saved history is returned with its real dates.
The download takes the run lock, like a run: while another job holds it, this exits 75 and does nothing.
"""
import sys

import pandas as pd

from config import DATA_DIR, DB_PATH, ensure_data_dir
from core import history, lake
from core.collect import acquire_cme
from core.runlock import EXIT_BUSY, RunBusy, run_lock
from core.sources import SourceSession


def update_silver_inventory(download=True):
    """Raises core.runlock.RunBusy when `download` is true and another job holds the run lock."""
    if download:
        ensure_data_dir(allow_create=True)
        with run_lock():
            conn = lake.connect(DB_PATH)
            try:
                lake.migrate(conn)
                out = acquire_cme(conn, SourceSession(conn, "manual-inventory"), "manual-inventory", DATA_DIR,
                                  offline=False, max_volume_files=0)
            finally:
                conn.close()
        print(f"COMEX inventory: {out['inventory'].get('outcome')} {out['inventory'].get('report_date') or ''}")
    conn = lake.connect(DB_PATH)
    lake.migrate(conn)
    series = history.inventory_series(conn)
    conn.close()
    df = pd.DataFrame(series)
    if df.empty:
        return None
    return df.rename(columns={"date": "Date", "registered": "Registered", "eligible": "Eligible", "total": "Total",
                              "registered_net_change": "Reg_Change", "eligible_net_change": "Elig_Change",
                              "total_net_change": "Total_Change"})


if __name__ == "__main__":
    try:
        result = update_silver_inventory()
    except RunBusy as busy:
        print(busy)
        sys.exit(EXIT_BUSY)
    print(result.tail() if result is not None else "No COMEX inventory history yet.")
