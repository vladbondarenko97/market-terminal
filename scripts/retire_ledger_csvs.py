"""Retire the legacy ledger CSVs: their rows go into the portfolio.db tables, the files go into one zip. Run once by hand.

  python scripts/retire_ledger_csvs.py           # do it
  python scripts/retire_ledger_csvs.py --check   # say what it would do, change nothing

Since the terminal and the pipeline read and write only the tables, the CSV copies are not needed. In order:
  1. take the run lock for the whole job (refuse while a run holds it); refuse when there is no database
  2. back the database up to <data>/backups/portfolio-<UTC time>.db (lake.backup_database, integrity_check)
  3. add every CSV row whose key (the parsed time, plus Ticker where there is one) is not in its table at all (rows
     with no time, blank lines in the old files, are in the tables already and cannot be matched, so they are
     skipped); a key the CSV holds more often than the table stops the script before anything changes
  4. stop unless every CSV row is now in its table (readers sort by time, so the added older rows are not "latest")
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


def _keys(frame, time_col):
    """Row keys: the time parsed (so '10/1/26 10:00' and '2026-10-01 10:00' match), plus Ticker where there is one.
    Rows with no time (blank lines in the old files) get None: they are in the tables already and cannot be matched."""
    raw = frame[time_col].map(lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip())
    t = pd.to_datetime(raw.where(raw != ""), format="mixed", errors="coerce")
    key = t.dt.strftime("%Y-%m-%dT%H:%M:%S").where(t.notna(), raw)
    if "Ticker" in frame.columns:
        key = key + "|" + frame["Ticker"].map(lambda v: str(v).strip().upper())
    return key.where(raw != "", None)


def compare(conn, table, path):
    """(rows to add, keys that are ambiguous). A row is added when its key is not in the table at all. A key the CSV
    holds more often than the table, while the table has it too, is ambiguous: nothing is guessed, the script stops."""
    csv = pd.read_csv(path, dtype=str, keep_default_na=False)
    tab = pd.read_sql(f'SELECT * FROM "{table}"', conn) if lake.table_columns(conn, table) else pd.DataFrame(columns=csv.columns)
    ck, tk = _keys(csv, TABLES[table]), _keys(tab, TABLES[table]).value_counts()
    counts = ck.value_counts()
    absent = ck.notna() & ~ck.isin(tk.index)
    ambiguous = sorted(k for k, n in counts.items() if k in tk.index and n > tk[k])
    num = pd.read_csv(path).loc[csv.index[absent]]          # the values as pandas parsed the file
    rows = [{k: (None if pd.isna(v) else v.item() if hasattr(v, "item") else v) for k, v in r.items()}
            for r in num.to_dict("records")]
    return rows, ambiguous


def main(data_dir, db_path, check=False):
    files = [data_dir / n for n in LEDGER_CSVS if (data_dir / n).is_file()]
    if not files:
        print("no ledger CSVs here: nothing to do")
        return 0
    if not Path(db_path).is_file():
        print(f"STOPPED: no database at {db_path}")
        return 1
    lock = runlock.RunLock()
    if not check and not lock.acquire():                   # held for the whole job: no run starts meanwhile
        print("a pipeline run is in progress: try again when it has finished")
        return 75
    try:
        return _retire(data_dir, db_path, files, check)
    finally:
        if not check:
            lock.release()


def _retire(data_dir, db_path, files, check):
    conn = lake.connect_readonly(db_path) if check else lake.connect(db_path)
    try:
        plan = {p: compare(conn, p.stem, p) for p in files if p.stem in TABLES}
        for p in files:
            if p in plan:
                rows, amb = plan[p]
                print(f"{p.name}: {len(rows)} rows to add to {p.stem}" + (f"; AMBIGUOUS keys {amb[:3]}" if amb else ""))
            else:
                print(f"{p.name}: archived only")
        if any(amb for _, amb in plan.values()):
            print("STOPPED: some times appear more often in a CSV than in its table; nothing was changed")
            return 1
        if check:
            return 0
        if any(rows for rows, _ in plan.values()):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            (data_dir / "backups").mkdir(exist_ok=True)
            print("backup:", lake.backup_database(db_path, data_dir / "backups" / f"portfolio-{stamp}.db"))
            for p, (rows, _) in plan.items():
                for r in rows:
                    lake.append_legacy_row(conn, p.stem, r)
        left = {p.name: compare(conn, p.stem, p) for p in plan}
        left = {n: (len(r), len(a)) for n, (r, a) in left.items() if r or a}
        if left:
            print(f"STOPPED: CSV rows still not in the tables {left}; no file was zipped or deleted")
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
