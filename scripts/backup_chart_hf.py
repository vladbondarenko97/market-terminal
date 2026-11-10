"""Back up the chart store (CHART_STORE) to a private Hugging Face dataset. Run on the machine that owns the store.

  python scripts/backup_chart_hf.py            # daily: the bars/ day files (HF stores each file's content once)
  python scripts/backup_chart_hf.py --weekly   # also a consistent, gzipped copy of chart.db as db/chart-YYYY-WW.db.gz

Needs HF_TOKEN (a write token for that one repo) and HF_REPO (e.g. you/market-chart-store). Without them it says so and
exits 0, so the timers can be installed before Hugging Face is set up. Uploads only; never deletes anything there.
"""
import gzip
import shutil
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402


def main(weekly):
    token, repo = config.HF_TOKEN, config.HF_REPO
    if not (token and repo):
        print("HF backup not configured (HF_TOKEN and HF_REPO): nothing uploaded")
        return 0
    from huggingface_hub import HfApi
    api, store = HfApi(token=token), Path(config.CHART_STORE)
    api.create_repo(repo, repo_type="dataset", private=True, exist_ok=True)
    if (store / "bars").is_dir():
        api.upload_folder(repo_id=repo, repo_type="dataset", folder_path=store / "bars", path_in_repo="bars",
                          commit_message=f"bars {date.today()}")
    if weekly and (store / "chart.db").exists():
        with tempfile.TemporaryDirectory() as tmp:
            raw, gz = Path(tmp) / "chart.db", Path(tmp) / "chart.db.gz"
            src, dst = sqlite3.connect(f"file:{store / 'chart.db'}?mode=ro", uri=True), sqlite3.connect(raw)
            src.backup(dst)
            src.close(), dst.close()
            with open(raw, "rb") as f, gzip.open(gz, "wb") as g:
                shutil.copyfileobj(f, g)
            y, w, _ = date.today().isocalendar()
            api.upload_file(path_or_fileobj=gz, path_in_repo=f"db/chart-{y}-{w:02d}.db.gz", repo_id=repo, repo_type="dataset",
                            commit_message=f"chart.db {y}-W{w:02d}")
    print(f"HF backup done: {repo}{' (with chart.db)' if weekly else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main("--weekly" in sys.argv[1:]))
