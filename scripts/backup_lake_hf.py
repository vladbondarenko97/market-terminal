"""Back up the lake (portfolio.db) to a private Hugging Face dataset, then remove the copies the timeline no longer needs.

  python scripts/backup_lake_hf.py           # what every live pipeline run starts when it ends (main_pipeline.run)
  python scripts/backup_lake_hf.py --check   # list the copies there and which ones the timeline keeps; changes nothing

A copy is lake.backup_database(): SQLite's online backup of DB_PATH, consistent while the terminal reads it, checked
with integrity_check. It is uploaded uncompressed as db/<UTC time>.db. Uncompressed on purpose: the stored payloads are compressed already (gzip saved
only 20%), and Hugging Face de-duplicates unchanged chunks, so an upload sends little more than what the run added. It
still counts every copy at full size, so old copies are deleted on the timeline in keep() and the history is squashed so
the deleted files stop counting. Needs HF_TOKEN (write access to HF_LAKE_REPO) and HF_LAKE_REPO, a dataset that exists and
holds nothing else; without them it says so and exits 0. Days, weeks and months are Chicago time, like the run folders.
"""
import fcntl
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from core.lake import backup_database  # noqa: E402

CHICAGO = ZoneInfo(config.TZ_NAME)

# The timeline: the newest copy in each of the newest N buckets, per tier. About 17 copies at most once it has run for
# six months (the tiers overlap), each the size of portfolio.db.
TIERS = (("run", 3, lambda t: t),                          # the last 3 runs (about one trading day)
         ("day", 7, lambda t: t.date()),                   # one a day for the last 7 days that had a run
         ("week", 4, lambda t: t.isocalendar()[:2]),       # one a week for 4 weeks
         ("month", 6, lambda t: (t.year, t.month)))        # one a month for 6 months
FMT = "db/%Y%m%dT%H%M%SZ.db"


def copies(names):
    """[(Chicago time, name)] of the repo paths named like FMT: the only files this script ever deletes."""
    out = []
    for n in names:
        try:
            out.append((datetime.strptime(n, FMT).replace(tzinfo=timezone.utc).astimezone(CHICAGO), n))
        except ValueError:
            continue
    return out


def keep(names):
    """{name: [tiers]} for the copies to keep. `names` are repo paths; anything not named like FMT is left alone."""
    stamped = copies(names)
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
        for _, n in sorted(copies(names)):
            print(f"{n}  {'keep: ' + ', '.join(kept[n]) if n in kept else 'delete at the next backup'}")
        return 0
    lock = open(config.DATA_DIR / ".lake_backup.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another lake backup is running")
        return 0
    if not config.DB_PATH.is_file():
        print(f"lake backup: no database at {config.DB_PATH}; nothing uploaded")
        return 1
    name = datetime.now(timezone.utc).strftime(FMT)
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "portfolio.db"
        try:
            backup_database(config.DB_PATH, copy)
        except Exception as exc:                           # never upload, and so never prune for, a damaged copy
            print(f"lake backup NOT uploaded: {exc}")
            return 1
        api.upload_file(path_or_fileobj=copy, path_in_repo=name, repo_id=repo, repo_type="dataset",
                        commit_message=f"portfolio.db {name[3:-3]}")
    names = api.list_repo_files(repo, repo_type="dataset")
    if name not in names:                                  # the upload must be there before anything is deleted
        print(f"lake backup: {name} is not listed after the upload; nothing deleted")
        return 1
    kept = keep(names)
    old = [n for _, n in copies(names) if n not in kept]
    if old:
        api.create_commit(repo, repo_type="dataset", operations=[CommitOperationDelete(n) for n in old],
                          commit_message=f"timeline: remove {len(old)} old copies")
        api.super_squash_history(repo, repo_type="dataset")   # deleted files stop counting toward the storage
    print(f"lake backup done: {repo}/{name}; {len(kept)} copies kept, {len(old)} removed")
    return 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv[1:]))
