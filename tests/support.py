"""Shared offline test environment: a temporary CME_Data built from tests/fixtures, and no credentials.

Every test module imports this first (``import support``), before anything imports ``config``: config resolves
the data folder and reads .env once per process, so the environment must be set before that first import.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
TMP = Path(tempfile.mkdtemp(prefix="v2test_"))
DATA = TMP / "CME_Data"
os.environ["PORTFOLIO_DATA_DIR"] = str(DATA)
# Blank every credential and delivery setting so a developer's real .env (loaded by config, which never overrides
# variables that are already set) cannot reach a provider, a mailbox, a phone or a web server during tests.
for k in ("DATABENTO_API_KEY", "DB_API_KEY", "FRED_API_KEY", "EIA_API_KEY", "GOLD_API_KEY", "ALPHA_VANTAGE_KEY",
          "EBAY_APP_ID", "EBAY_CERT_ID", "CME_LOGIN_USERNAME", "CME_LOGIN_PASSWORD", "EMAIL_SENDER",
          "EMAIL_PASSWORD", "SMTP_SERVER", "RECIPIENT_EMAIL", "REPORT_SENDER", "NTFY_URL", "DASHBOARD_URL",
          "UPLOAD_URL", "UPLOAD_TOKEN", "REPORT_UPLOAD", "SCHEDULED_RUNS"):
    os.environ[k] = ""
sys.path.insert(0, str(ROOT))


def build_installation():
    DATA.mkdir(parents=True, exist_ok=True)
    for f in FIX.iterdir():
        shutil.copy(f, DATA / f.name)
    # a download-dated duplicate like the real archive has (same bytes, different name)
    shutil.copy(FIX / "daily_volume_20260819.xlsx", DATA / "daily_volume_20260820.xlsx")
    (DATA / "state.json").write_text('{"cookies": [{"name": "secret"}]}')
    conn = sqlite3.connect(DATA / "portfolio.db")
    conn.execute('CREATE TABLE "comex_inventory_history" ("Date" TEXT, "Registered" REAL, "Eligible" REAL, '
                 '"Total" REAL, "Reg_Change" REAL, "Elig_Change" REAL, "Total_Change" REAL, "Legacy_Extra" TEXT)')
    conn.execute("INSERT INTO comex_inventory_history VALUES ('2026-09-24', 95494503.432, 235879562.0304, "
                 "331374065.4624, 9661.2, 597730.65, 607391.85, 'keep-me')")
    conn.execute('CREATE TABLE "crypto_metrics_history" ("Date" TEXT, "BTC_Price" REAL, "Silver_Price" REAL, '
                 '"Gold_Price" REAL, "Silver_BTC_Ratio" REAL, "Gold_BTC_Ratio" REAL)')
    import datetime as dt
    for i in range(40):
        d = (dt.date(2026, 9, 29) - dt.timedelta(days=i)).isoformat()
        conn.execute("INSERT INTO crypto_metrics_history VALUES (?,?,?,?,?,?)",
                     (d, 80000 + i * 100, 60 + i * 0.1, 4200 + i, (80000 + i * 100) / (60 + i * 0.1),
                      (80000 + i * 100) / (4200 + i)))
    conn.execute('CREATE TABLE "macro_master_ledger" ("Datetime" TEXT, "VMRI_Score" REAL, "SHFE_Premium" REAL, '
                 '"High_Yield_OAS" REAL, "10Y_Yield" REAL, "Reverse_Repo_BN" REAL)')
    conn.execute("INSERT INTO macro_master_ledger VALUES ('9/24/26 13:59', 172.7, 1.8, 2.73, 5.16, 0.63)")
    conn.commit()
    conn.close()


build_installation()
