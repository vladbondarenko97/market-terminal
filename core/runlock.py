"""The run lock: one process at a time owns collection, the CME browser profile and the exports.

The lock is an exclusive `flock` on `DATA_DIR/.v2_run.lock`, so the operating system frees it when the holder dies.
`main_pipeline.py run`, `cme-login`, `import-history` and `backfill-positions`, `download_volume.py` and
`update_inventory.py` take it. Read-only commands (`replay`, `resend`, `catalog`, `status`) and the terminal do not;
they use `lock_is_held()` to ask without taking it.
"""
import fcntl
import json
import os
from contextlib import contextmanager

from config import DATA_DIR

LOCK_PATH = DATA_DIR / ".v2_run.lock"
STATUS_PATH = DATA_DIR / ".v2_run_status.json"
EXIT_BUSY = 75


class RunBusy(RuntimeError):
    """Another process holds the run lock."""


class RunLock:
    """One run owns collection and exports. A second request gets a clear busy answer."""
    def __init__(self):
        self.fh = None

    def acquire(self):
        self.fh = open(LOCK_PATH, "a+")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.close()
            self.fh = None
            return False
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return True

    def release(self):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None


def lock_is_held():
    """True while any process (this one included, through another RunLock) holds the run lock.

    A non-blocking probe that never keeps the lock. It takes the lock for an instant when it is free, so a run
    starting in that instant can see a false BUSY; callers treat the answer as a status display, not a guard."""
    try:
        fh = open(LOCK_PATH, "r")
    except OSError:
        return False                      # no lock file yet (or no data folder): nobody has ever held it
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    finally:
        fh.close()


def read_status():
    """The contents of `.v2_run_status.json` as a dict ({} when absent or unreadable)."""
    try:
        cur = json.loads(STATUS_PATH.read_text())
        return cur if isinstance(cur, dict) else {}
    except (OSError, ValueError):
        return {}


def effective_state(status=None):
    """`state` from the status file, or `interrupted` when the file says `running` but nobody holds the lock
    (the run was killed before it could record its end)."""
    status = read_status() if status is None else status
    state = status.get("state")
    if state == "running" and not lock_is_held():
        return "interrupted"
    return state


def busy_message():
    cur = read_status()
    if cur.get("state") == "running":
        return f"⏳ BUSY: run {cur.get('run_id')} is in progress (stage {cur.get('stage')}). Not starting another."
    return ("⏳ BUSY: another command holds the run lock (a run, cme-login, import-history, backfill-positions, "
            "download_volume.py or update_inventory.py). Not starting another.")


@contextmanager
def run_lock():
    """`with run_lock():` for scripts that must not overlap a run. Raises RunBusy (message = busy_message())."""
    lock = RunLock()
    if not lock.acquire():
        raise RunBusy(busy_message())
    try:
        yield lock
    finally:
        lock.release()
