"""Compatibility renderer: the Morning Market Brief from the latest committed v2 snapshot.

No Databento/Yahoo requests happen here any more (they were duplicated with institutional_scanner.py and the
API). Block-flow sides use Databento semantics (A = sell aggressor, B = buy aggressor) in core/metrics.py.
"""
from core import lake, render
from config import DB_PATH


def analyze_market():
    conn = lake.connect(DB_PATH)
    lake.migrate(conn)
    ctx = lake.load_snapshot(conn)
    conn.close()
    if ctx is None:
        return "No committed v2 snapshot yet. Run: python main_pipeline.py run"
    return render.market_brief_text(ctx)


if __name__ == "__main__":
    print(analyze_market())
