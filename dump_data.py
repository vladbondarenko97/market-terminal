"""Compatibility renderer: volume_dashboard.txt (XML) from the latest committed v2 snapshot."""
import os

from config import DATA_DIR, DB_PATH
from core import lake, render


def aggregate_data_to_text(volume_df=None, inventory_df=None, tactical_text=""):
    conn = lake.connect(DB_PATH)
    lake.migrate(conn)
    ctx = lake.load_snapshot(conn)
    conn.close()
    if ctx is None:
        raise RuntimeError("No committed v2 snapshot yet. Run: python main_pipeline.py run")
    tactical = tactical_text or render.tactical_xml(ctx)
    out_dir = os.path.join(DATA_DIR, ctx["run"]["run_folder"])
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "volume_dashboard.txt")
    with open(out_path + ".tmp", "w", encoding="utf-8") as f:
        f.write(render.dashboard_xml(ctx, tactical))
    os.replace(out_path + ".tmp", out_path)
    print(f"✅ Data dump ready at {out_path} (XML format)")
    return out_path


if __name__ == "__main__":
    aggregate_data_to_text()
