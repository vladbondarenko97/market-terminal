"""Shared frame of the operator's backfill scripts (scripts/backfill_*.py): the run lock, a `mode='backfill'` run row and a SourceSession.

A backfill run never commits a snapshot, so it can never be the "latest" run: every reader of the latest run goes through
`v2_latest_snapshot`. Its row only records when it ran, how long it took, the counts and any error.
"""
import time
from datetime import datetime, timezone

import config
from core import lake
from core.runlock import EXIT_BUSY, RunBusy, run_lock
from core.sources import SourceSession


def run(label, work):
    """work(conn, session, run_id) -> counts dict (a non-zero "failed" count makes the run `completed_with_warnings`).
    Returns the exit code: 0 done, 75 when another job holds the run lock."""
    config.ensure_data_dir(allow_create=True)
    try:
        with run_lock():
            conn = lake.connect(config.DB_PATH)
            try:
                lake.migrate(conn)
                run_id = f"backfill-{label}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}"
                lake.create_run(conn, run_id, mode="backfill", trigger="manual", run_folder="", code_version=f"scripts/backfill_{label}.py")
                session, t0, status, counts, error = SourceSession(conn, run_id), time.monotonic(), "failed", {}, None
                try:
                    counts = work(conn, session, run_id)
                    status = "completed_with_warnings" if counts.get("failed") else "completed"
                except BaseException as e:      # recorded, then re-raised: an interrupted backfill is not left "running"
                    error = lake.redact(f"{type(e).__name__}: {e}")
                    raise
                finally:
                    lake.update_run(conn, run_id, status=status, finished_at=lake.utc_now_iso(), elapsed_s=round(time.monotonic() - t0, 1),
                                    stages_json={label: counts}, request_counts_json=session.calls, error=error)
            finally:
                conn.close()
    except RunBusy as busy:
        print(busy)
        return EXIT_BUSY
    return 0
