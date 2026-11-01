"""Download the Kronos weights (MIT, NeoQuasar on Hugging Face) into <data folder>/models/kronos/.

Operator-run, uses the network once. The pipeline never downloads: core/fans.py loads only from that folder.

    pip install -r requirements-kronos.txt
    PORTFOLIO_DATA_DIR=... .venv/bin/python scripts/fetch_kronos.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

REPOS = {"model": "NeoQuasar/Kronos-small", "tokenizer": "NeoQuasar/Kronos-Tokenizer-base"}


def main():
    from huggingface_hub import snapshot_download
    config.ensure_data_dir()
    for sub, repo in REPOS.items():
        dest = config.KRONOS_DIR / sub
        dest.mkdir(parents=True, exist_ok=True)
        snapshot_download(repo_id=repo, local_dir=str(dest))
        print(f"{repo} -> {dest}")


if __name__ == "__main__":
    sys.exit(main())
