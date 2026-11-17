"""Retire the legacy ledger CSVs: their rows go into the portfolio.db tables, the files go into one zip. Run once by hand.

  python scripts/retire_ledger_csvs.py           # do it
  python scripts/retire_ledger_csvs.py --check   # say what it would do, change nothing

Since the terminal and the pipeline read and write only the tables, the CSV copies are not needed. In order:
  1. refuse while a pipeline run holds the run lock
  2. back the database up to <data>/backups/portfolio-<UTC time>.db (lake.backup_database, integrity_check)
  3. add every CSV row whose key (time column, plus Ticker where there is one) is not in its table yet (rows with no
     time, blank lines in the old files, are in the tables already and cannot be matched, so they are skipped)
  4. stop unless every CSV row is now in its table
  5. zip all the CSVs into <data>/archive/ledger_csvs_<date>.zip, read the zip back and compare every file byte for byte
  6. only then delete the CSVs
Nothing in a table is changed or deleted. Your hand copy ("backup macro_master_ledger copy.csv") is archived, not merged.
"""
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from core import lake, runlock  # noqa: E402
from core.importer import LEDGER_CSVS  # noqa: E402

TABLES = {"macro_master_ledger": "Datetime", "equities_darkpool_gex_ledger": "Date", "institutional_ledger": "Date",
          "physical_arbitrage_ledger": "Datetime", "comex_inventory_history": "Date"}


def _key(frame, time_col):
    cols = [time_col] + (["Ticker"] if "Ticker" in frame.columns else [])
    key = frame[cols[0]].map(str)
    for c in cols[1:]:
        key = key + "|" + frame[c].map(str)
    return key


def missing_rows(conn, table, path):
    """The CSV rows of `path` whose key is not in `table`, as dicts (NaN -> None)."""
    csv = pd.read_csv(path, dtype=str, keep_default_na=False)
    have = set(_key(pd.read_sql(f'SELECT * FROM "{table}"', conn), TABLES[table])) if lake.table_columns(conn, table) else set()
    timed = csv[csv[TABLES[table]].str.strip() != ""]        # rows with no time (blank lines) cannot be matched
    out = timed[~_key(timed, TABLES[table]).isin(have)]
    num = pd.read_csv(path).loc[out.index]                     # the values as pandas parsed the file
    return [{k: (None if pd.isna(v) else v.item() if hasattr(v, "item") else v) for k, v in r.items()}
            for r in num.to_dict("records")]


def main(data_dir, db_path, check=False):
    files = [data_dir / n for n in LEDGER_CSVS if (data_dir / n).is_file()]
    if not files:
        print("no ledger CSVs here: nothing to do")
        return 0
    if runlock.lock_is_held():
        print("a pipeline run is in progress: try again when it has finished")
        return 75
    conn = lake.connect(db_path)
    try:
        plan = {p: missing_rows(conn, p.stem, p) for p in files if p.stem in TABLES}
        for p in files:
            print(f"{p.name}: " + (f"{len(plan[p])} rows to add to {p.stem}" if p in plan else "archived only"))
        if check:
            return 0
        if any(plan.values()):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            (data_dir / "backups").mkdir(exist_ok=True)
            print("backup:", lake.backup_database(db_path, data_dir / "backups" / f"portfolio-{stamp}.db"))
            for p, rows in plan.items():
                for r in rows:
                    lake.append_legacy_row(conn, p.stem, r)
        left = {p.name: len(missing_rows(conn, p.stem, p)) for p in plan}
        if any(left.values()):
            print(f"STOPPED: rows still missing from the tables {left}; no file was zipped or deleted")
            return 1
    finally:
        conn.close()
    archive = data_dir / "archive"
    archive.mkdir(exist_ok=True)
    zpath = archive / f"ledger_csvs_{datetime.now().strftime('%Y-%m-%d')}.zip"
    if zpath.exists():
        print(f"STOPPED: {zpath} already exists; nothing deleted")
        return 1
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, p.name)
    with zipfile.ZipFile(zpath) as z:
        if z.testzip() is not None or any(z.read(p.name) != p.read_bytes() for p in files):
            print(f"STOPPED: {zpath} does not read back identically; nothing deleted")
            return 1
    for p in files:
        p.unlink()
    print(f"archived {len(files)} CSVs to {zpath} ({zpath.stat().st_size // 1024} KB) and deleted them")
    return 0


if __name__ == "__main__":
    sys.exit(main(config.DATA_DIR, config.DB_PATH, "--check" in sys.argv[1:]))
