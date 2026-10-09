"""CME volume acquisition (bounded). Replaces the old open-ended Playwright loop.

- Reads the logged-in CME FTP listing and downloads every listed trade date the lake does not hold, newest first
  (max N per call, 1-40), so old gaps fill over several calls.
- Files are named by the trade date inside the workbook; existing archives are never overwritten.
- If CME refuses a download it opens the login window and waits up to 15 minutes, like a run (no phone push).
  To log in ahead of time:
      python main_pipeline.py cme-login
- Takes the run lock, like a run: while a run (or cme-login, import-history) holds it, this exits 75 and does nothing.
"""
import sys

from config import DATA_DIR, DB_PATH, CME_BACKFILL_MAX_ATTEMPTS, ensure_data_dir
from core import lake
from core.collect import acquire_cme
from core.runlock import EXIT_BUSY, RunBusy, run_lock
from core.sources import SourceSession


def download_latest_cme_files(num_files_to_get=10):
    """Raises core.runlock.RunBusy when another job holds the run lock."""
    n = max(1, min(int(num_files_to_get), CME_BACKFILL_MAX_ATTEMPTS))
    ensure_data_dir(allow_create=True)
    with run_lock():
        conn = lake.connect(DB_PATH)
        try:
            lake.migrate(conn)
            session = SourceSession(conn, "manual-download")
            out = acquire_cme(conn, session, "manual-download", DATA_DIR, offline=False, max_volume_files=n)
        finally:
            conn.close()
    v = out.get("volume", {})
    print(f"CME volume: {v.get('outcome')} — {v.get('detail') or ''} (fetched {len(v.get('files', []))} file(s))")
    for f in v.get("files", []):
        print(f"  {f['file_date']}: {f['outcome']} {f.get('file', '')}")
    return out


if __name__ == "__main__":
    try:
        download_latest_cme_files(int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 10)
    except RunBusy as busy:
        print(busy)
        sys.exit(EXIT_BUSY)
