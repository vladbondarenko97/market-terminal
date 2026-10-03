"""Compatibility renderer: tactical_ruling.txt from the latest committed v2 snapshot.

Collection now happens once per run in main_pipeline.py (core/collect.py); this module no longer fetches data.
The legacy implementation is in git history (main branch) for rollback.
"""
import sys

from core import lake, render
from config import DB_PATH


def _latest_ctx():
    conn = lake.connect(DB_PATH)
    lake.migrate(conn)
    ctx = lake.load_snapshot(conn)
    conn.close()
    if ctx is None:
        raise RuntimeError("No committed v2 snapshot yet. Run: python main_pipeline.py run")
    return ctx


def calculate_vmri(dxy, tnx, oas, vix):
    """Legacy signature kept; formula owned by core.metrics (vmri_v1)."""
    from core.metrics import vmri
    r = vmri(dxy, tnx, oas, vix)
    return r["score"], r["tier"]


def print_tactical_ruling(inventory_df=None):
    """Returns the tactical ruling XML (string) for the latest committed run."""
    return render.tactical_xml(_latest_ctx())


if __name__ == "__main__":
    print(print_tactical_ruling())
    sys.exit(0)
