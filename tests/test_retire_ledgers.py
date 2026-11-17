"""scripts/retire_ledger_csvs.py on a scratch data folder: rows land in the tables, the CSVs in a verified zip. Offline."""
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import support  # noqa: F401  (offline environment, before config is imported)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import retire_ledger_csvs as r  # noqa: E402
from core import lake  # noqa: E402

HEADER = "Date,Ticker,Spot_Price,DP_Sentiment\n"


class Retire(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.db = self.dir / "portfolio.db"
        conn = lake.connect(self.db)
        lake.append_legacy_row(conn, "institutional_ledger", {"Date": "2026-10-01 09:31", "Ticker": "SPY",
                                                               "Spot_Price": 500.0, "DP_Sentiment": "BULLISH"})
        conn.close()
        (self.dir / "institutional_ledger.csv").write_text(
            HEADER + "2026-10-01 09:31,SPY,500.0,BULLISH\n2026-10-01 09:31,SLV,30.5,N/A\n2026-10-02 09:31,SPY,501.0,BEARISH\n")
        (self.dir / "backup macro_master_ledger copy.csv").write_text("Datetime,VIX\n2026-01-01 10:00,20\n")

    def table(self):
        conn = lake.connect(self.db)
        rows = [dict(x) for x in conn.execute("SELECT * FROM institutional_ledger ORDER BY rowid")]
        conn.close()
        return rows

    def test_check_changes_nothing(self):
        self.assertEqual(r.main(self.dir, self.db, check=True), 0)
        self.assertEqual(len(self.table()), 1)
        self.assertTrue((self.dir / "institutional_ledger.csv").exists())

    def test_missing_rows_are_added_and_the_csvs_zipped_and_deleted(self):
        before = {p.name: p.read_bytes() for p in self.dir.glob("*.csv")}
        self.assertEqual(r.main(self.dir, self.db), 0)
        rows = self.table()
        self.assertEqual([(x["Ticker"], x["Spot_Price"]) for x in rows], [("SPY", 500.0), ("SLV", 30.5), ("SPY", 501.0)])
        self.assertIsNone(rows[1]["DP_Sentiment"])                          # "N/A" in the file is a missing value
        self.assertEqual(list(self.dir.glob("*.csv")), [])
        (z,) = (self.dir / "archive").glob("ledger_csvs_*.zip")
        with zipfile.ZipFile(z) as f:
            self.assertEqual({n: f.read(n) for n in f.namelist()}, before)
        self.assertEqual(len(list((self.dir / "backups").glob("portfolio-*.db"))), 1)
        self.assertEqual(r.main(self.dir, self.db), 0)                      # a second run finds nothing to do


if __name__ == "__main__":
    unittest.main()
