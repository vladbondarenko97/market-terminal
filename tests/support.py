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
os.environ["MT_NO_KEYCHAIN"] = "1"   # config must not fill blanks from the developer's Keychain
# Blank every credential and delivery setting so a developer's real .env (loaded by config, which never overrides
# variables that are already set) cannot reach a provider, a mailbox, a phone or a web server during tests.
for k in ("DATABENTO_API_KEY", "DB_API_KEY", "FRED_API_KEY", "EIA_API_KEY", "GOLD_API_KEY",
          "EBAY_APP_ID", "EBAY_CERT_ID", "CME_LOGIN_USERNAME", "CME_LOGIN_PASSWORD", "EMAIL_SENDER",
          "EMAIL_PASSWORD", "SMTP_SERVER", "RECIPIENT_EMAIL", "REPORT_SENDER", "NTFY_URL", "DASHBOARD_URL",
          "UPLOAD_URL", "UPLOAD_TOKEN", "REPORT_UPLOAD", "SCHEDULED_RUNS",
          "ASSISTANT_LOCAL_URL", "ASSISTANT_LOCAL_MODEL", "ASSISTANT_LOCAL_API_KEY",
          "SNAPTRADE_CLIENT_ID", "SNAPTRADE_CONSUMER_KEY"):
    os.environ[k] = ""
sys.path.insert(0, str(ROOT))


class NetworkBlocked(BaseException):
    """Raised on any attempt to reach another machine during the tests. It is a BaseException on purpose: the source
    wrappers catch Exception and would turn an ordinary error into a quiet `missing`, so a leaking test would still pass."""


def _block_network():
    import socket

    connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost") and not host.startswith("/"):
            raise NetworkBlocked(f"tests must not reach the network (tried {host}); patch the fetcher or use an offline session")
        return connect(self, address, *args, **kwargs)
    socket.socket.connect = guarded
    try:                                    # yfinance talks through curl_cffi, which does not use Python sockets
        from curl_cffi import requests as curl

        def refused(*args, **kwargs):
            raise NetworkBlocked("tests must not reach the network (curl_cffi request); patch the fetcher or use an offline session")
        curl.Session.request = refused
    except ImportError:
        pass


_block_network()

# yfinance keeps cookies and time zones in an on-disk cache shared with the live terminal. A blocked fetch caches an
# empty cookie, and under tests/shifted_clock.py it is dated a year ahead, so the live process kept reusing it and every
# Yahoo call came back empty (2026-10-16). Tests get a cache of their own.
import yfinance  # set_tz_cache_location moves the cookie cache too
yfinance.set_tz_cache_location(str(TMP / "yfinance"))


def build_installation():
    DATA.mkdir(parents=True, exist_ok=True)
    for f in FIX.iterdir():
        if f.is_file():           # sub-folders (fixtures/rss, fixtures/chaos) hold parser fixtures, not data files
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
