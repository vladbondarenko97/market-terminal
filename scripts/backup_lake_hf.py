"""Back up the lake (portfolio.db) to a private Hugging Face dataset, then remove the copies the timeline no longer needs.

  python scripts/backup_lake_hf.py           # what every live pipeline run starts when it ends (main_pipeline.run)
  python scripts/backup_lake_hf.py --check   # list the copies there and which ones the timeline keeps; changes nothing

A copy is SQLite's online backup of DB_PATH (consistent while the terminal reads it), checked with quick_check, then
uploaded uncompressed as db/<UTC time>.db. Uncompressed on purpose: the stored payloads are compressed already (gzip saved
only 20%), and Hugging Face de-duplicates unchanged chunks, so an upload sends little more than what the run added. It
still counts every copy at full size, so old copies are deleted on the timeline in keep() and the history is squashed so
the deleted files stop counting. Needs HF_TOKEN and HF_LAKE_REPO; without them it says so and exits 0.
"""
import fcntl
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

# The timeline: the newest copy in each of the newest N buckets, per tier. About 17 copies at most once it has run for
# six months (the tiers overlap), each the size of portfolio.db.
TIERS = (("run", 3, lambda t: t),                          # the last 3 runs (about one trading day)
         ("day", 7, lambda t: t.date()),                   # one a day for the last 7 days that had a run
         ("week", 4, lambda t: t.isocalendar()[:2]),       # one a week for 4 weeks
         ("month", 6, lambda t: (t.year, t.month)))        # one a month for 6 months
FMT = "db/%Y%m%dT%H%M%SZ.db"


def keep(names):
    """{name: [tiers]} for the copies to keep. `names` are repo paths; anything not named like FMT is left alone."""
    stamped = []
    for n in names:
        try:
            stamped.append((datetime.strptime(n, FMT), n))
        except ValueError:
            continue
    stamped.sort(reverse=True)
    kept = {}
    for tier, count, bucket in TIERS:
        seen = []
        for t, n in stamped:                               # newest first: the first copy of a bucket is its newest
            b = bucket(t)
            if b not in seen:
                if len(seen) == count:
                    break
                seen.append(b)
                kept.setdefault(n, []).append(tier)
    return kept


def main(check):
    token, repo = config.HF_TOKEN, config.HF_LAKE_REPO
    if not (token and repo):
        print("lake backup not configured (HF_TOKEN and HF_LAKE_REPO): nothing uploaded")
        return 0
    from huggingface_hub import CommitOperationDelete, HfApi
    api = HfApi(token=token)
    if check:
        names = api.list_repo_files(repo, repo_type="dataset")
        kept = keep(names)
        for n in sorted(n for n in names if n.startswith("db/")):
            print(f"{n}  {'keep: ' + ', '.join(kept[n]) if n in kept else 'delete at the next backup'}")
        return 0
    lock = open(config.DATA_DIR / ".lake_backup.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another lake backup is running")
        return 0
    api.create_repo(repo, repo_type="dataset", private=True, exist_ok=True)
    name = datetime.now(timezone.utc).strftime(FMT)
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "portfolio.db"
        src, dst = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True), sqlite3.connect(copy)
        src.backup(dst)
        src.close()
        ok = dst.execute("PRAGMA quick_check").fetchone()[0]
        dst.close()
        if ok != "ok":                                     # never upload, and so never prune for, a damaged copy
            print(f"lake backup NOT uploaded: the copy failed quick_check ({ok})")
            return 1
        api.upload_file(path_or_fileobj=copy, path_in_repo=name, repo_id=repo, repo_type="dataset",
                        commit_message=f"portfolio.db {name[3:-3]}")
    names = api.list_repo_files(repo, repo_type="dataset")
    if name not in names:                                  # the upload must be there before anything is deleted
        print(f"lake backup: {name} is not listed after the upload; nothing deleted")
        return 1
    kept = keep(names)
    old = [n for n in names if n.startswith("db/") and n not in kept]
    if old:
        api.create_commit(repo, repo_type="dataset", operations=[CommitOperationDelete(n) for n in old],
                          commit_message=f"timeline: remove {len(old)} old copies")
        api.super_squash_history(repo, repo_type="dataset")   # deleted files stop counting toward the storage
    print(f"lake backup done: {repo}/{name}; {len(kept)} copies kept, {len(old)} removed")
    return 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv[1:]))
