"""Terminal API (options_whale/api_router.py): stored routes, run control, request validation and the pure helpers.

Everything runs offline against a scratch data folder with Flask's test client. Yahoo, Databento and the pipeline
subprocess are replaced by fakes; no route here reaches a network or a real data folder.
"""
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401  (temporary CME_Data, no credentials; must come before importing config)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import config  # noqa: E402
from core import lake, metrics, positions, runlock  # noqa: E402

sys.path.insert(0, str(support.ROOT / "options_whale"))
# The server starts without a Databento key (the client is created on the first /api/darkpool request).
import api_router  # noqa: E402
import quant_engine  # noqa: E402

PYTHON = sys.executable
NOT_INITIALISED = "database not initialised: run the pipeline once"

# api_router constants that hold paths under the data folder
PATH_NAMES = {
    "DATA_DIR": "", "LEDGER_CSV": "macro_master_ledger.csv", "LEDGER_FILE": "physical_arbitrage_ledger.csv",
    "INSTITUTIONAL_LEDGER_CSV": "equities_darkpool_gex_ledger.csv", "INVENTORY_CSV": "comex_inventory_history.csv",
    "MANUAL_RUN_LOG": ".v2_manual_run.log", "_V2_DB_PATH": "portfolio.db",
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FakeTicker:
    """Stands in for yfinance.Ticker. `registry` maps a symbol to {"history": DataFrame, "options": [dates],
    "chains": {date: (calls, puts)}, "raises": Exception}."""
    registry = {}

    def __init__(self, symbol):
        self.symbol = symbol
        self.spec = self.registry.get(symbol, {})

    def history(self, period=None, interval=None):
        if self.spec.get("raises"):
            raise self.spec["raises"]
        return self.spec.get("history", pd.DataFrame({"Close": []}))

    @property
    def options(self):
        if self.spec.get("raises"):
            raise self.spec["raises"]
        return tuple(self.spec.get("options", ()))

    def option_chain(self, exp):
        calls, puts = self.spec["chains"][exp]
        return SimpleNamespace(calls=calls.copy(), puts=puts.copy())


def closes(*values):
    return pd.DataFrame({"Close": list(values)})


def walk(n=40, start=100.0, step=0.01):
    """A deterministic daily close series with some movement (for realized volatility)."""
    return closes(*[start * (1 + step * ((-1) ** i) * (1 + (i % 3))) for i in range(n)])


def chain_frame(strikes, iv=0.2, oi=100.0, volume=10.0, last=1.0):
    n = len(strikes)
    return pd.DataFrame({
        "contractSymbol": [f"X{int(k)}" for k in strikes], "strike": [float(k) for k in strikes],
        "lastPrice": [last] * n, "bid": [last - 0.05] * n, "ask": [last + 0.05] * n,
        "volume": [volume] * n, "openInterest": [oi] * n, "impliedVolatility": [iv] * n})


def in_days(n):
    return (datetime.now() + timedelta(days=n)).strftime("%Y-%m-%d")


class ApiCase(unittest.TestCase):
    """A scratch data folder per test, wired into the api_router constants, with the module caches cleared."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="api_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(mock.patch.stopall)
        self.use_data_dir(self.tmp)
        api_router.data_cache.cache.clear()
        api_router.data_cache.last_update.clear()
        api_router._manual_run = None
        FakeTicker.registry = {}
        # keep the expected error logs out of the test output (assertLogs still sees them)
        mock.patch.object(api_router.app.logger, "handlers", [logging.NullHandler()]).start()
        mock.patch.object(api_router.app.logger, "propagate", False).start()
        self.client = api_router.app.test_client()

    def use_data_dir(self, data):
        data = Path(data)
        for name, leaf in PATH_NAMES.items():
            mock.patch.object(api_router, name, str(data / leaf) if leaf else str(data)).start()
        mock.patch.object(runlock, "LOCK_PATH", data / ".v2_run.lock").start()
        mock.patch.object(runlock, "STATUS_PATH", data / ".v2_run_status.json").start()
        mock.patch.object(api_router.quant, "ledger_file", str(data / "macro_master_ledger.csv")).start()

    def fake_yahoo(self, **registry):
        FakeTicker.registry = registry
        mock.patch.object(api_router.yf, "Ticker", FakeTicker).start()

    def write_status(self, **fields):
        runlock.STATUS_PATH.write_text(json.dumps(fields))

    def get_json(self, url, status=200):
        r = self.client.get(url)
        self.assertEqual(r.status_code, status, r.get_data(as_text=True)[:300])
        return r.get_json()


# ============================================================ /help
class HelpPage(ApiCase):
    def test_help_is_well_formed_and_lists_every_route(self):
        r = self.client.get("/help")
        self.assertEqual(r.status_code, 200)
        root = ET.fromstring(r.get_data(as_text=True).split("?>", 1)[1])
        documented = set()
        for ep in root.iter("endpoint"):
            documented.add(ep.get("path"))
            if ep.get("alias"):
                documented.add(ep.get("alias"))
        served = set()
        for rule in api_router.app.url_map.iter_rules():
            path = rule.rule.replace("<int:signal_id>", "{signal_id}").replace("<path:filename>", "{filename}")
            served.add(path)
        self.assertEqual(documented, served)

    def test_help_documents_the_new_run_and_scan_behaviour(self):
        text = self.client.get("/help").get_data(as_text=True)
        self.assertIn("/api/run_status", text)
        self.assertNotIn("macro_direction", text)
        self.assertNotIn("options_scanner", text)
        silver = [ep for ep in ET.fromstring(text.split("?>", 1)[1]).iter("endpoint")
                  if ep.get("path") == "/api/silver_eagle_prices"][0]
        self.assertEqual(silver.get("method"), "POST")


# ============================================================ security
class SecurityModel(ApiCase):
    def test_no_cors_headers_are_sent(self):
        r = self.client.get("/help", headers={"Origin": "http://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)
        r = self.client.open("/api/positions", method="OPTIONS", headers={"Origin": "http://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)

    def test_cross_site_writes_are_refused_before_the_route_runs(self):
        with mock.patch.object(api_router.subprocess, "Popen") as popen, \
                mock.patch.object(api_router.subprocess, "run") as run:
            for path in ("/run", "/api/silver_eagle_prices"):
                r = self.client.post(path, headers={"Origin": "http://evil.example"})
                self.assertEqual(r.status_code, 403, path)
            r = self.client.delete("/api/positions/1", headers={"Referer": "http://evil.example/page"})
            self.assertEqual(r.status_code, 403)
        popen.assert_not_called()
        run.assert_not_called()

    def test_unknown_routes_and_wrong_methods_answer_json_for_the_api(self):
        r = self.client.get("/api/macro_direction")                      # removed: it returned a constant
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["status"], "error")
        r = self.client.get("/api/silver_eagle_prices")                  # writes a ledger row: POST only
        self.assertEqual(r.status_code, 405)
        self.assertIn("POST", r.headers["Allow"])
        self.assertEqual(self.client.get("/run").status_code, 405)

    def test_chart_pages_pin_chartjs(self):
        for path in ("/vmri_chart", "/inventory_chart"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn("chart.js@4.4.1", html, path)
            self.assertNotIn("npm/chart.js\"", html, path)
        self.assertIn("chartjs-plugin-annotation@2.1.0", self.client.get("/vmri_chart").get_data(as_text=True))

    def test_unexpected_errors_do_not_leak_exception_text(self):
        self.fake_yahoo(SPY={"raises": RuntimeError("secret path /Users/someone/.env")})
        for url in ("/api/gex?ticker=SPY", "/api/option_chain?ticker=SPY", "/api/time_arbitrage?ticker=SPY"):
            r = self.client.get(url)
            self.assertGreaterEqual(r.status_code, 500, url)
            self.assertNotIn("secret", r.get_data(as_text=True), url)
            self.assertEqual(r.get_json()["status"], "error")
        r = self.client.get("/api/custom?ticker=SPY")
        self.assertGreaterEqual(r.status_code, 500)
        self.assertNotIn("secret", r.get_data(as_text=True))
        self.assertTrue(r.get_data(as_text=True).startswith("<error>"))


# ============================================================ run control
class RunControl(ApiCase):
    def test_run_status_without_a_status_file_is_all_null(self):
        data = self.get_json("/api/run_status")["data"]
        self.assertFalse(data["lock_held"])
        for key in ("run_id", "stage", "state", "pid", "mode", "started_at", "updated_at", "finished_at",
                    "elapsed_s", "error"):
            self.assertIn(key, data)
            self.assertIsNone(data[key], key)

    def test_run_status_passes_the_file_state_through(self):
        self.write_status(run_id="r1", stage="done", state="completed_with_warnings", pid=7, mode="live",
                          error=None, elapsed_s=12.5, unrelated="dropped")
        data = self.get_json("/api/run_status")["data"]
        self.assertEqual((data["run_id"], data["state"], data["elapsed_s"]), ("r1", "completed_with_warnings", 12.5))
        self.assertNotIn("unrelated", data)
        self.assertFalse(data["lock_held"])

    def test_a_running_state_without_the_lock_is_interrupted(self):
        self.write_status(run_id="r2", stage="collect", state="running")
        data = self.get_json("/api/run_status")["data"]
        self.assertEqual(data["state"], "interrupted")
        self.assertFalse(data["lock_held"])

    def test_a_running_state_with_the_lock_stays_running(self):
        self.write_status(run_id="r3", stage="collect", state="running")
        lock = runlock.RunLock()
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        data = self.get_json("/api/run_status")["data"]
        self.assertEqual(data["state"], "running")
        self.assertTrue(data["lock_held"])

    def test_run_answers_409_busy_and_starts_nothing_when_the_lock_is_held(self):
        self.write_status(run_id="r4", stage="collect", state="running")
        lock = runlock.RunLock()
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        with mock.patch.object(api_router.subprocess, "Popen") as popen:
            r = self.client.post("/run")
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual((body["status"], body["run_id"], body["stage"]), ("busy", "r4", "collect"))
        popen.assert_not_called()

    def test_run_starts_the_script_in_the_background_and_answers_202(self):
        proc = mock.Mock(pid=4242)
        proc.poll.return_value = 0
        with mock.patch.object(api_router.subprocess, "Popen", return_value=proc) as popen:
            r = self.client.post("/run", headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.get_json(), {"status": "started", "pid": 4242})
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["/bin/zsh", api_router.RUN_COMMAND, "manual"])
        self.assertEqual(kwargs["cwd"], str(config.PROJECT_ROOT))
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(Path(kwargs["stdout"].name), self.tmp / ".v2_manual_run.log")
        self.assertTrue((self.tmp / ".v2_manual_run.log").exists())

    def test_a_second_request_while_the_first_run_is_starting_is_busy(self):
        proc = mock.Mock(pid=4243)
        proc.poll.return_value = None            # started, still alive, has not taken the lock yet
        with mock.patch.object(api_router.subprocess, "Popen", return_value=proc) as popen:
            self.assertEqual(self.client.post("/run").status_code, 202)
            r = self.client.post("/run")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["status"], "busy")
        self.assertEqual(popen.call_count, 1)

    def test_run_reports_a_missing_script_as_json_404(self):
        with mock.patch.object(api_router, "RUN_COMMAND", str(self.tmp / "missing.command")):
            r = self.client.post("/run")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["status"], "error")

    def test_run_failing_to_spawn_gives_a_short_message(self):
        with mock.patch.object(api_router.subprocess, "Popen", side_effect=OSError("/bin/zsh: secret detail")):
            r = self.client.post("/run")
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("secret", r.get_data(as_text=True))


# ============================================================ stored routes on a database that may not exist
class DatabaseReads(ApiCase):
    def test_positions_and_forecast_are_503_before_the_first_run(self):
        for url in ("/api/positions", "/api/forecast?ticker=SPY", "/api/eia_history"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 503, url)
            self.assertEqual(r.get_json(), {"status": "error", "message": NOT_INITIALISED}, url)
        self.assertFalse((self.tmp / "portfolio.db").exists(), "a read created the database")

    def test_positions_and_forecast_are_503_on_a_database_without_the_v2_tables(self):
        import sqlite3
        conn = sqlite3.connect(self.tmp / "portfolio.db")
        conn.execute("CREATE TABLE legacy (x)")
        conn.commit()
        conn.close()
        for url in ("/api/positions", "/api/forecast?ticker=SLV"):
            self.assertEqual(self.client.get(url).status_code, 503, url)

    def test_forecast_is_404_on_a_migrated_database_with_no_snapshot(self):
        conn = lake.connect(self.tmp / "portfolio.db")
        lake.migrate(conn)
        conn.close()
        r = self.client.get("/api/forecast?ticker=SPY")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["status"], "error")
        r = self.client.get("/api/positions")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["data"], [])

    def test_forecast_rejects_other_tickers(self):
        r = self.client.get("/api/forecast?ticker=AAPL")
        self.assertEqual(r.status_code, 400)

    def test_star_and_delete_never_create_the_database(self):
        self.assertEqual(self.client.post("/api/positions/1/star").status_code, 503)
        self.assertEqual(self.client.delete("/api/positions/1").status_code, 503)
        self.assertFalse((self.tmp / "portfolio.db").exists())

    def test_star_delete_and_list_on_a_migrated_database(self):
        db = self.tmp / "portfolio.db"
        conn = lake.connect(db)
        lake.migrate(conn)
        positions.insert_signal(conn, {"run_id": "run-1", "created_at": "2026-10-01T14:00:00+00:00",
                                       "underlying": "SPY", "position_type": "CASH"})
        conn.close()
        mock.patch.object(api_router, "_cached_underlying", lambda s: 500.0).start()
        mock.patch.object(api_router, "_cached_daily_closes", lambda s: pd.Series(dtype=float)).start()
        rows = self.get_json("/api/positions")["data"]
        self.assertEqual(len(rows), 1)
        sid = rows[0]["signal_id"]
        self.assertEqual(self.client.post(f"/api/positions/{sid}/star").get_json(), {"status": "success", "starred": True})
        self.assertEqual(self.client.post("/api/positions/999/star").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/positions/{sid}").get_json(), {"status": "success"})
        self.assertEqual(self.client.delete(f"/api/positions/{sid}").status_code, 404)
        self.assertEqual(self.get_json("/api/positions")["data"], [])


class AfterAnOfflineRun(ApiCase):
    """The pipeline runs once, offline, in a scratch folder; the terminal then reads what it stored."""

    @classmethod
    def setUpClass(cls):
        cls.data = Path(tempfile.mkdtemp(prefix="api_offline_"))
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(cls.data), "PORTFOLIO_DATA_DIR": str(cls.data)}
        proc = subprocess.run([PYTHON, str(support.ROOT / "main_pipeline.py"), "run", "--offline"], env=env,
                              cwd=str(support.ROOT), capture_output=True, text=True, timeout=300)
        cls.rc, cls.output = proc.returncode, proc.stdout + proc.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.data, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.assertEqual(self.rc, 0, self.output[-800:])
        self.use_data_dir(self.data)                      # after the base class pointed at an empty folder
        mock.patch.object(api_router, "_cached_daily_closes", lambda s: pd.Series(dtype=float)).start()

    def test_positions_and_forecast_answer_200(self):
        self.assertEqual(self.get_json("/api/positions")["status"], "success")
        body = self.get_json("/api/forecast?ticker=SPY")
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["ticker"], "SPY")
        self.assertIn("scorecard", body)

    def test_the_reads_do_not_touch_the_database_file(self):
        before = sha(self.data / "portfolio.db")
        for url in ("/api/positions", "/api/forecast?ticker=SPY", "/api/forecast?ticker=SLV", "/api/eia_history"):
            self.client.get(url)
        self.assertEqual(sha(self.data / "portfolio.db"), before)

    def test_run_status_reads_the_finished_run(self):
        data = self.get_json("/api/run_status")["data"]
        self.assertEqual(data["state"], "completed")
        self.assertEqual(data["mode"], "offline")
        self.assertFalse(data["lock_held"])


# ============================================================ /dump and the other folder readers
class RunFolders(ApiCase):
    def make_day(self, name, marker):
        folder = self.tmp / name
        folder.mkdir()
        (folder / "tactical_ruling.txt").write_text(f"<tactical_ruling><marker>{marker}</marker></tactical_ruling>")
        return folder

    def test_dump_uses_the_newest_dated_folder_not_the_newest_modification_time(self):
        old = self.make_day("Sep-29-26", "sep")
        new = self.make_day("Oct-02-26", "oct")
        wrapped = self.make_day("Dec-31-25", "dec")        # sorts after "Oct" as text, before it by date
        for name in (".cme_browser_profile", "_rejected_downloads", "scratch-folder", "backups"):
            (self.tmp / name).mkdir()
        # make every non-run folder, and the older run folders, the most recently modified
        now = datetime.now().timestamp()
        os.utime(new, (now - 500, now - 500))
        for folder in (old, wrapped, self.tmp / ".cme_browser_profile", self.tmp / "_rejected_downloads",
                       self.tmp / "backups"):
            os.utime(folder, (now, now))
        for url in ("/dump", "/api/dump"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200)
            root = ET.fromstring(r.get_data(as_text=True).split("?>", 1)[-1])
            self.assertEqual(root.get("source_folder"), "Oct-02-26")
            self.assertEqual(root.find(".//marker").text, "oct")

    def test_dump_without_any_dated_folder_is_404(self):
        (self.tmp / ".cme_browser_profile").mkdir()
        r = self.client.get("/dump")
        self.assertEqual(r.status_code, 404)
        self.assertTrue(r.get_data(as_text=True).startswith("<error>"))

    def test_dump_without_a_data_folder_is_404(self):
        with mock.patch.object(api_router, "DATA_DIR", str(self.tmp / "missing")):
            self.assertEqual(self.client.get("/dump").status_code, 404)

    def test_macro_calendar_skips_folders_that_are_not_run_folders(self):
        self.make_day("Oct-02-26", "oct")
        (self.tmp / "zzz_other").mkdir()
        (self.tmp / "zzz_other" / "tactical_ruling.txt").write_text("<tactical_ruling><bad/></tactical_ruling>")
        self.assertEqual(self.get_json("/api/macro_calendar")["events"], [])

    def test_run_folder_helper_orders_by_the_date_in_the_name(self):
        for name in ("Jan-02-26", "Dec-31-25", "Mar-15-26", "not-a-day"):
            (self.tmp / name).mkdir()
        (self.tmp / "May-01-26").write_text("a file, not a folder")
        names = [os.path.basename(p) for p in api_router._run_folders()]
        self.assertEqual(names, ["Mar-15-26", "Jan-02-26", "Dec-31-25"])


# ============================================================ request parameters
class Parameters(ApiCase):
    def test_a_bad_limit_is_a_400_json_error(self):
        for path in ("/api/institutional_history", "/api/macro_ledger_full", "/api/arbitrage_history"):
            for bad in ("abc", "1.5", "0", "-3"):
                r = self.client.get(f"{path}?limit={bad}")
                self.assertEqual(r.status_code, 400, f"{path} {bad}")
                body = r.get_json()
                self.assertEqual(body["status"], "error")
                self.assertIn("limit", body["message"])

    def test_a_good_limit_reaches_the_route(self):
        self.assertEqual(self.client.get("/api/institutional_history?limit=5").status_code, 404)   # no ledger yet
        self.assertEqual(self.client.get("/api/macro_ledger_full?limit=5").status_code, 404)
        self.assertEqual(self.client.get("/api/arbitrage_history?limit=5").status_code, 404)

    def test_invalid_tickers_are_refused(self):
        for url in ("/api/gex", "/api/darkpool", "/api/option_chain", "/api/time_arbitrage", "/api/option_calc"):
            r = self.client.get(url + "?ticker=bad%20ticker!")
            self.assertEqual(r.status_code, 400, url)
        for url in ("/api/custom", "/api/morning", "/api/evening"):
            r = self.client.get(url + "?ticker=a;b")
            self.assertEqual(r.status_code, 400, url)
            self.assertTrue(r.get_data(as_text=True).startswith("<error>"))

    def test_scan_parameters_are_validated(self):
        for query in ("min_vol_oi=abc", "min_premium=1e999", "max_dte=2.5", "max_dte=-1", "min_premium=-5"):
            r = self.client.get(f"/api/custom?ticker=SPY&{query}")
            self.assertEqual(r.status_code, 400, query)
            self.assertTrue(r.get_data(as_text=True).startswith("<error>"))


# ============================================================ scans
class Scans(ApiCase):
    def setUp(self):
        super().setUp()
        near, far = in_days(3), in_days(40)
        # volume x last x 100: 200,000 / 20,000 / 600,000 / 120,000
        calls_near = chain_frame([100, 101], oi=500.0, volume=1000.0, last=2.0)
        calls_near.loc[1, ["volume", "lastPrice"]] = [100.0, 2.0]                       # small premium
        calls_far = chain_frame([100, 102], oi=1000.0, volume=2000.0, last=3.0)          # 600,000, ratio 2
        calls_far.loc[1, ["volume", "openInterest", "lastPrice"]] = [1200.0, 1000.0, 1.0]   # 120,000, ratio 1.2
        puts_empty = chain_frame([100]).iloc[0:0]
        self.near, self.far = near, far
        self.fake_yahoo(SPY={"history": closes(100.0), "options": [near, far],
                             "chains": {near: (calls_near, puts_empty), far: (calls_far, puts_empty)}},
                        EMPTY={"options": []})

    def scan(self, url, status=200):
        r = self.client.get(url)
        self.assertEqual(r.status_code, status, r.get_data(as_text=True)[:300])
        return ET.fromstring(r.get_data(as_text=True).split("?>", 1)[-1])

    def symbols(self, root):
        return sorted(c.get("symbol") for c in root.findall("contract"))

    def test_morning_is_a_filtered_scan_of_the_requested_ticker(self):
        root = self.scan("/api/morning?ticker=spy")
        self.assertEqual((root.tag, root.get("ticker"), root.get("strategy")), ("whale_hunt", "SPY", "MORNING_HUNT"))
        # expirations within 14 days, Vol/OI >= 1.5, premium >= $100,000: only the first near-dated contract
        self.assertEqual(self.symbols(root), ["X100"])
        self.assertEqual(root.get("whale_count"), "1")
        contract = root.find("contract")
        self.assertEqual(contract.get("expiration"), self.near)
        self.assertEqual(contract.get("premium_spent"), "$200,000.00")

    def test_evening_has_no_dte_limit_and_a_larger_premium_floor(self):
        root = self.scan("/api/evening?ticker=SPY")
        self.assertEqual(root.get("strategy"), "EVENING_HUNT")
        self.assertEqual(self.symbols(root), ["X100"])                                   # the 600,000 far-dated one
        self.assertEqual(root.find("contract").get("expiration"), self.far)
        self.assertEqual(root.find("contract").get("premium_spent"), "$600,000.00")

    def test_custom_honours_min_premium(self):
        default = self.scan("/api/custom?ticker=SPY&max_dte=365&min_vol_oi=1")
        self.assertEqual(default.get("strategy"), "CUSTOM_HUNT")
        self.assertEqual(default.get("whale_count"), "3")                                # 200k, 600k and 120k
        high = self.scan("/api/custom?ticker=SPY&min_premium=500000")
        self.assertEqual(high.get("whale_count"), "1")
        low = self.scan("/api/custom?ticker=SPY&min_premium=10000&min_vol_oi=0")
        self.assertEqual(low.get("whale_count"), "4")                                    # also the 20,000 one
        # sorted by premium, largest first
        premiums = [float(c.get("premium_spent").replace("$", "").replace(",", "")) for c in low.findall("contract")]
        self.assertEqual(premiums, sorted(premiums, reverse=True))

    def test_custom_blank_fields_use_the_defaults_and_blank_dte_means_no_limit(self):
        root = self.scan("/api/custom?ticker=SPY&min_vol_oi=&min_premium=&max_dte=")
        self.assertEqual(root.get("whale_count"), "3")
        limited = self.scan("/api/custom?ticker=SPY&max_dte=14")
        self.assertEqual(limited.get("whale_count"), "1")

    def test_a_ticker_without_options_is_a_404_error_element(self):
        for url in ("/api/custom?ticker=EMPTY", "/api/morning?ticker=EMPTY", "/api/evening?ticker=EMPTY"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 404, url)
            self.assertIn("<error>", r.get_data(as_text=True))

    def test_the_scans_start_no_subprocess_and_send_no_alert(self):
        with mock.patch.object(api_router.subprocess, "run") as run, \
                mock.patch.object(api_router.subprocess, "check_output") as check_output, \
                mock.patch.object(api_router.requests, "post") as post:
            for url in ("/api/morning", "/api/evening", "/api/custom"):
                self.client.get(url)
        run.assert_not_called()
        check_output.assert_not_called()
        post.assert_not_called()
        self.assertFalse(hasattr(api_router, "execute_morning_hunt"))
        self.assertFalse(hasattr(api_router, "send_whale_alert"))
        self.assertFalse((support.ROOT / "options_scanner.py").exists())


# ============================================================ Silver Eagles
class SilverEagles(ApiCase):
    def test_post_runs_ebay_and_returns_its_xml(self):
        xml = '<physical_arbitrage comex_spot="$30.00" benchmark_symbol="SI=F" item_count="0"/>'
        done = subprocess.CompletedProcess([], 0, stdout=xml, stderr="")
        with mock.patch.object(api_router.subprocess, "run", return_value=done) as run:
            r = self.client.post("/api/silver_eagle_prices", headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/xml")
        self.assertEqual(ET.fromstring(r.get_data(as_text=True)).get("benchmark_symbol"), "SI=F")
        self.assertEqual(run.call_args[0][0], [api_router.PYTHON_BIN, api_router.EBAY_SCRIPT_PATH])

    def test_a_failing_script_gives_a_short_error_and_logs_the_output(self):
        failed = subprocess.CompletedProcess([], 1, stdout="Traceback ... SECRET-TOKEN-123", stderr="more detail")
        with mock.patch.object(api_router.subprocess, "run", return_value=failed), \
                self.assertLogs(api_router.app.logger, level="ERROR") as logs:
            r = self.client.post("/api/silver_eagle_prices")
        self.assertEqual(r.status_code, 500)
        self.assertTrue(r.get_data(as_text=True).startswith("<error>"))
        self.assertNotIn("SECRET", r.get_data(as_text=True))
        self.assertIn("ebay.py exited", "\n".join(logs.output))

    def test_a_timeout_is_a_504(self):
        with mock.patch.object(api_router.subprocess, "run", side_effect=subprocess.TimeoutExpired("ebay.py", 120)):
            r = self.client.post("/api/silver_eagle_prices")
        self.assertEqual(r.status_code, 504)
        self.assertTrue(r.get_data(as_text=True).startswith("<error>"))

    def test_the_benchmark_is_named_in_the_script(self):
        text = (support.ROOT / "ebay.py").read_text()
        self.assertIn('benchmark_symbol=BENCHMARK_SYMBOL', text)
        self.assertIn('BENCHMARK_SYMBOL = "SI=F"', text)


# ============================================================ gamma exposure
def gex_chains(call_oi, put_oi, expiration):
    calls = chain_frame([105], iv=0.2, oi=call_oi)
    puts = chain_frame([95], iv=0.2, oi=put_oi)
    return {expiration: {"calls": calls, "puts": puts}}


class GammaExposure(ApiCase):
    TODAY = datetime.now().date()

    def test_zero_gamma_is_the_pipelines_level_not_the_spot(self):
        exp = in_days(30)
        chains = gex_chains(1000.0, 3000.0, exp)
        payload = api_router.build_gex_payload(100.0, chains, [exp], self.TODAY)
        expected = metrics.gex_profile(100.0, chains, [exp], self.TODAY)
        self.assertIsNotNone(payload["zeroGamma"])
        self.assertAlmostEqual(payload["zeroGamma"], expected["zero_gamma"])
        self.assertNotAlmostEqual(payload["zeroGamma"], 100.0, places=3)
        self.assertGreaterEqual(payload["zeroGamma"], 90.0)
        self.assertLessEqual(payload["zeroGamma"], 110.0)
        self.assertIsNone(payload["zeroGammaReason"])
        self.assertEqual(payload["strikes"], sorted(payload["strikes"]))
        self.assertEqual(len(payload["strikes"]), len(payload["gamma"]))
        self.assertEqual((payload["callWall"], payload["putWall"]), (105.0, 95.0))

    def test_no_sign_change_gives_null_with_a_reason(self):
        exp = in_days(30)
        payload = api_router.build_gex_payload(100.0, gex_chains(1000.0, 0.0, exp), [exp], self.TODAY)
        self.assertIsNone(payload["zeroGamma"])
        self.assertTrue(payload["zeroGammaReason"])
        self.assertIsNotNone(payload["callWall"])

    def test_no_usable_contracts_is_an_error_not_a_made_up_profile(self):
        exp = in_days(30)
        with self.assertRaises(api_router.ApiError) as ctx:
            api_router.build_gex_payload(100.0, gex_chains(0.0, 0.0, exp), [exp], self.TODAY)
        self.assertEqual(ctx.exception.status, 422)

    def test_the_route_uses_the_same_profile(self):
        exp = in_days(30)
        chains = gex_chains(1000.0, 3000.0, exp)
        self.fake_yahoo(SPY={"history": closes(99.0, 100.0), "options": [exp, in_days(60)],
                             "chains": {exp: (chains[exp]["calls"], chains[exp]["puts"]),
                                        in_days(60): (chain_frame([105], oi=0.0), chain_frame([95], oi=0.0))}})
        data = self.get_json("/api/gex?ticker=SPY")["data"]
        self.assertEqual(data["spot"], 100.0)
        self.assertNotAlmostEqual(data["zeroGamma"], 100.0, places=3)
        self.assertEqual(set(data), {"spot", "zeroGamma", "zeroGammaReason", "callWall", "putWall", "strikes", "gamma"})

    def test_route_errors_have_real_statuses(self):
        self.fake_yahoo(SPY={"history": closes(100.0), "options": []}, NOPX={"options": ["2030-01-01"]})
        self.assertEqual(self.client.get("/api/gex?ticker=SPY").status_code, 404)
        self.assertEqual(self.client.get("/api/gex?ticker=NOPX").status_code, 502)       # no spot price


# ============================================================ dark pool
def trades_frame(rows):
    """rows: (price, size, side). A DatetimeIndex, as Databento's to_df() returns."""
    index = pd.date_range("2026-10-02 14:00:00", periods=len(rows), freq="min", tz="UTC")
    return pd.DataFrame({"price": [r[0] for r in rows], "size": [r[1] for r in rows], "side": [r[2] for r in rows]},
                        index=index)


class DarkPool(ApiCase):
    def test_databento_sides_a_is_sell_and_b_is_buy(self):
        frame = trades_frame([(100.0, 20000, "B"), (101.0, 12000, "A"), (100.5, 10000, "N"), (50.0, 100, "B")])
        data = api_router.build_darkpool_payload("SPY", frame)
        sentiment = data["sentiment"]
        # B (buy aggressor) volume 20,000 vs A (sell aggressor) 12,000: bullish. The old code had it reversed.
        self.assertEqual((sentiment["bull_volume"], sentiment["bear_volume"]), (20000, 12000))
        self.assertEqual(sentiment["bias"], "BULLISH")
        self.assertEqual(sentiment["method"], "aggressor")
        sides = {p["size"]: p["side"] for p in data["recent_prints"]}
        self.assertEqual(sides, {20000: "BUY", 12000: "SELL", 10000: "UNKNOWN"})
        self.assertEqual(data["total_block_volume"], 42000)
        self.assertEqual(data["largest_single_block"], 20000)
        self.assertEqual(data["recent_prints"][0]["size"], 10000)              # newest first
        self.assertRegex(data["recent_prints"][0]["time"], r"^\d\d:\d\d:\d\d$")

    def test_sell_aggressors_make_it_bearish(self):
        frame = trades_frame([(100.0, 30000, "A"), (100.0, 10000, "B")])
        data = api_router.build_darkpool_payload("SPY", frame)
        self.assertEqual(data["sentiment"]["bias"], "BEARISH")
        self.assertEqual((data["sentiment"]["bull_volume"], data["sentiment"]["bear_volume"]), (10000, 30000))

    def test_mostly_unknown_sides_use_the_labelled_vwap_heuristic(self):
        frame = trades_frame([(102.0, 30000, "N"), (98.0, 10000, "N"), (100.0, 10000, "B")])
        data = api_router.build_darkpool_payload("SPY", frame)
        self.assertEqual(data["sentiment"]["method"], "vwap_heuristic")
        self.assertIn(data["sentiment"]["bias"], ("BULLISH", "BEARISH", "NEUTRAL"))
        self.assertEqual(data["sentiment"]["bull_volume"] + data["sentiment"]["bear_volume"], 50000)
        self.assertTrue(data["note"])

    def test_no_block_sized_prints_gives_none(self):
        self.assertIsNone(api_router.build_darkpool_payload("SPY", trades_frame([(100.0, 500, "B")])))

    def mock_databento(self, frame):
        client = mock.Mock()
        client.timeseries.get_range.return_value.to_df.return_value = frame
        mock.patch.object(api_router, "db_client", client).start()
        return client

    def test_route_success_and_empty_window(self):
        client = self.mock_databento(trades_frame([(100.0, 20000, "B"), (101.0, 12000, "A")]))
        data = self.get_json("/api/darkpool?ticker=SLV")["data"]
        self.assertEqual(data["ticker"], "SLV")
        self.assertEqual(data["sentiment"]["bias"], "BULLISH")
        self.assertEqual(client.timeseries.get_range.call_args.kwargs["symbols"], ["SLV"])

        self.mock_databento(trades_frame([(100.0, 500, "B")]))
        body = self.get_json("/api/darkpool")
        self.assertEqual((body["status"], body["data"]), ("success", None))

        self.mock_databento(trades_frame([]))
        r = self.client.get("/api/darkpool")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["status"], "error")

    def test_without_a_key_the_server_runs_and_darkpool_answers_503(self):
        mock.patch.object(api_router, "db_client", None).start()
        mock.patch.object(config, "DATABENTO_API_KEY", "").start()
        r = self.client.get("/api/darkpool")
        self.assertEqual(r.status_code, 503)
        self.assertIn("DATABENTO_API_KEY", r.get_json()["message"])
        self.assertEqual(self.client.get("/help").status_code, 200)

    def test_a_databento_failure_is_a_502_without_the_provider_text(self):
        client = mock.Mock()
        client.timeseries.get_range.side_effect = RuntimeError("401 key=SECRET-KEY")
        mock.patch.object(api_router, "db_client", client).start()
        r = self.client.get("/api/darkpool")
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("SECRET", r.get_data(as_text=True))


# ============================================================ war room
LEDGER_HEADER = "Datetime,VMRI_Score,DXY,10Y_Yield,High_Yield_OAS,VIX,GEX,DIX\n"


class WarRoom(ApiCase):
    def write_ledger(self, text):
        (self.tmp / "macro_master_ledger.csv").write_text(text)

    def test_returns_the_scenario_from_the_ledger(self):
        self.write_ledger(LEDGER_HEADER + "2026-10-01 10:00,180.0,100,4.0,4.0,20,,\n")
        body = self.get_json("/api/war_room?vix_shift=5&dxy_shift=1")
        self.assertEqual(body["current"]["vix"], 20.0)
        self.assertEqual(body["hypothetical"]["vix"], 25.0)
        self.assertEqual(body["hypothetical"]["dxy"], 101.0)
        self.assertIsNone(body["history"])
        post = self.client.post("/api/war_room", json={"vix_shift_pct": 50}, headers={"Origin": "http://localhost"})
        self.assertEqual(post.get_json()["hypothetical"]["vix"], 30.0)

    def test_a_blank_latest_value_crawls_up_to_the_last_recorded_one(self):
        self.write_ledger(LEDGER_HEADER + "2026-10-01 10:00,180.0,100,4.0,4.0,18,,\n2026-10-02 10:00,181.0,101,4.1,4.2,,,\n")
        body = self.get_json("/api/war_room")
        self.assertEqual((body["current"]["vix"], body["current"]["dxy"]), (18.0, 101.0))

    def test_a_missing_input_is_a_503_naming_it_and_no_constant_is_substituted(self):
        self.write_ledger(LEDGER_HEADER + "2026-10-01 10:00,180.0,100,4.0,,20,,\n")
        r = self.client.get("/api/war_room")
        self.assertEqual(r.status_code, 503)
        self.assertIn("High_Yield_OAS", r.get_json()["message"])
        self.write_ledger("Datetime,VMRI_Score,DXY,10Y_Yield,High_Yield_OAS\n2026-10-01 10:00,180.0,100,4.0,4.0\n")
        r = self.client.get("/api/war_room")
        self.assertEqual(r.status_code, 503)
        self.assertIn("VIX", r.get_json()["message"])
        self.write_ledger(LEDGER_HEADER + "2026-10-01 10:00,,100,4.0,4.0,20,,\n")
        r = self.client.get("/api/war_room")
        self.assertEqual(r.status_code, 503)
        self.assertIn("VMRI_Score", r.get_json()["message"])

    def test_no_ledger_is_a_503_and_bad_parameters_are_400(self):
        self.assertEqual(self.client.get("/api/war_room").status_code, 503)
        self.write_ledger(LEDGER_HEADER + "2026-10-01 10:00,180.0,100,4.0,4.0,20,,\n")
        self.assertEqual(self.client.get("/api/war_room?dxy_shift=abc").status_code, 400)
        self.assertEqual(self.client.get("/api/war_room?vix_shift=nan").status_code, 400)
        r = self.client.post("/api/war_room", data="not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)


# ============================================================ option_calc and time_arbitrage
class OptionAnalytics(ApiCase):
    def setUp(self):
        super().setUp()
        self.exp = in_days(30)
        self.spec = {"history": walk(), "options": [self.exp],
                     "chains": {self.exp: (chain_frame([95, 100, 105], iv=0.25, last=2.0, oi=50.0),
                                           chain_frame([95, 100, 105], iv=0.25, last=2.0, oi=50.0))}}
        self.irx = {"history": closes(4.3)}

    def calc(self, params, status=200):
        r = self.client.get(f"/api/option_calc?ticker=SLV&expiration={self.exp}&type=call&{params}")
        self.assertEqual(r.status_code, status, r.get_data(as_text=True)[:300])
        return r.get_json()

    def test_realized_volatility_is_for_the_requested_ticker(self):
        self.fake_yahoo(SLV=self.spec, **{"^IRX": self.irx})
        seen = []
        mock.patch.object(api_router.quant, "calculate_realized_volatility",
                          lambda ticker=None, window=20: (seen.append(ticker), 0.30)[1]).start()
        data = self.calc("strike=100")["data"]
        self.assertEqual(seen, ["SLV"])
        self.assertEqual(data["hv_pct"], 30.0)
        self.assertEqual(data["iv_pct"], 25.0)
        self.assertEqual(data["missing"], {})

    def test_unknown_history_gives_null_with_a_reason_not_a_made_up_volatility(self):
        spec = dict(self.spec, history=closes(100.0, 101.0))              # too short for 20 returns
        self.fake_yahoo(SLV=spec, **{"^IRX": self.irx})
        data = self.calc("strike=100")["data"]
        self.assertIsNone(data["hv_pct"])
        self.assertIsNone(data["iv_signal"])
        self.assertIn("hv_pct", data["missing"])
        self.assertIn("iv_signal", data["missing"])

    def test_market_price_is_null_when_the_chain_has_no_price_and_breakeven_uses_the_model(self):
        for frame in (chain_frame([95, 100, 105], iv=0.25, last=0.0), chain_frame([95, 100, 105], iv=0.25, last=float("nan"))):
            spec = dict(self.spec, chains={self.exp: (frame, frame)})
            self.fake_yahoo(SLV=spec, **{"^IRX": self.irx})
            data = self.calc("strike=100&market_price=0")["data"]
            self.assertIsNone(data["market_price"])
            self.assertIn("market_price", data["missing"])
            self.assertAlmostEqual(data["breakeven"], 100 + data["bs_price"], places=3)
        self.fake_yahoo(SLV=self.spec, **{"^IRX": self.irx})                 # the chain's last price (2.0) is a quote
        data = self.calc("strike=100&market_price=0")["data"]
        self.assertEqual(data["market_price"], 2.0)
        self.assertNotIn("market_price", data["missing"])
        data = self.calc("strike=100&market_price=3.5")["data"]              # a price the caller gives is kept
        self.assertEqual(data["market_price"], 3.5)
        self.assertEqual(data["breakeven"], 103.5)

    def test_a_contract_without_implied_volatility_is_a_422(self):
        self.fake_yahoo(SLV=self.spec, **{"^IRX": self.irx})
        self.assertEqual(self.calc("strike=101", 422)["status"], "error")           # not in the chain
        nan_chain = chain_frame([95, 100, 105], iv=float("nan"))
        zero_chain = chain_frame([95, 100, 105], iv=0.0)
        for frame in (nan_chain, zero_chain):
            spec = dict(self.spec, chains={self.exp: (frame, frame)})
            self.fake_yahoo(SLV=spec, **{"^IRX": self.irx})
            self.assertEqual(self.calc("strike=100", 422)["status"], "error")

    def test_no_risk_free_rate_is_a_503_not_five_percent(self):
        self.fake_yahoo(SLV=self.spec)                                                # no ^IRX history
        mock.patch.object(api_router.quant, "_rate_cache", None).start()
        self.assertEqual(self.calc("strike=100", 503)["status"], "error")

    def test_bad_input_is_a_400(self):
        self.fake_yahoo(SLV=self.spec, **{"^IRX": self.irx})
        for params in ("strike=0", "strike=abc", "strike=100&market_price=-1", "strike=100&type=straddle"):
            r = self.client.get(f"/api/option_calc?ticker=SLV&expiration={self.exp}&{params}")
            self.assertEqual(r.status_code, 400, params)
        r = self.client.get("/api/option_calc?ticker=SLV&strike=100&expiration=tomorrow")
        self.assertEqual(r.status_code, 400)


class TimeArbitrage(ApiCase):
    SPOT = 100.0

    def setUp(self):
        super().setUp()
        self.exp = in_days(30)
        strikes = list(range(60, 141))
        self.calls = chain_frame(strikes, iv=0.25, oi=10.0)
        self.registry = {
            "SPY": {"history": closes(*walk(start=self.SPOT)["Close"], self.SPOT), "options": [self.exp],
                    "chains": {self.exp: (self.calls, chain_frame(strikes, iv=0.25))}},
            "^VIX": {"history": closes(18.0)}, "^IRX": {"history": closes(4.3)}}
        mock.patch.object(api_router.quant, "_rate_cache", None).start()

    def write_macro(self, with_gex=False):
        rows = ["Datetime,VIX,GEX,DIX"]
        for i in range(10):
            day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d %H:%M")
            rows.append(f"{day},{15 + i % 4},{1000 + i if with_gex else ''},")
        (self.tmp / "macro_master_ledger.csv").write_text("\n".join(rows) + "\n")

    def run_route(self):
        self.fake_yahoo(**self.registry)
        return self.get_json("/api/time_arbitrage?ticker=SPY")["data"]

    def test_strikes_are_the_ones_nearest_spot(self):
        data = self.run_route()
        strikes = [row["strike"] for row in data["iv_bleed"]]
        self.assertEqual(strikes, [float(k) for k in range(93, 108)])        # 15 strikes around 100, not 60..74
        self.assertEqual([p["strike"] for p in data["probabilities"]], [98.0, 99.0, 100.0, 101.0, 102.0])
        profile = [row["strike"] for row in data["dealer_trapdoor"]["vanna_profile"]]
        self.assertEqual(profile, strikes)

    def test_missing_inputs_are_null_with_reasons_and_no_invented_numbers(self):
        data = self.run_route()                                            # no macro ledger, no GEX ledger
        self.assertIsNone(data["z_score"])
        self.assertIsNone(data["gamma_state"])
        self.assertIn("z_score", data["missing"])
        self.assertIn("gamma_state", data["missing"])
        self.assertEqual(data["z_components"]["used"], [])
        self.assertIsNotNone(data["iv_hv_spread"]["realized_volatility_20d"])
        self.assertAlmostEqual(data["iv_hv_spread"]["atm_implied_volatility"], 0.25)

    def test_the_oscillator_ignores_components_with_no_history_and_reports_what_it_used(self):
        self.write_macro(with_gex=False)
        data = self.run_route()
        self.assertEqual(data["z_components"]["used"], ["vix"])
        self.assertEqual(set(data["z_components"]["missing"]), {"gex", "dix"})
        self.assertIsNotNone(data["z_score"])
        self.assertLessEqual(abs(data["z_score"]), 100)
        self.assertNotIn("z_score", data["missing"])

    def test_gex_joins_the_oscillator_when_the_ledger_has_it(self):
        self.write_macro(with_gex=True)
        with open(self.tmp / "macro_master_ledger.csv", "a") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M')},17,1500,\n")
        data = self.run_route()
        self.assertEqual(data["z_components"]["used"], ["vix", "gex"])
        self.assertEqual(set(data["z_components"]["missing"]), {"dix"})

    def test_gamma_state_comes_from_the_stored_zero_gamma_level(self):
        (self.tmp / "equities_darkpool_gex_ledger.csv").write_text(
            "Date,Ticker,Spot_Price,GEX_Zero_Gamma\n2026-10-02,SPY,100.0,98.0\n")
        data = self.run_route()
        self.assertAlmostEqual(data["gamma_state"]["distance"], 2.0)
        self.assertFalse(data["gamma_state"]["short_gamma_active"])
        self.assertNotIn("gamma_state", data["missing"])

    def test_a_missing_risk_free_rate_nulls_what_needs_it(self):
        del self.registry["^IRX"]
        data = self.run_route()
        self.assertEqual(data["probabilities"], [])
        self.assertIsNone(data["dealer_trapdoor"]["vanna_exposure"])
        self.assertIn("probabilities", data["missing"])
        self.assertIn("dealer_trapdoor", data["missing"])
        self.assertEqual(len(data["iv_bleed"]), 15)                                  # IV bleed needs no rate

    def test_no_realized_volatility_nulls_the_bleed(self):
        self.registry["SPY"]["history"] = closes(100.0, 100.5)
        data = self.run_route()
        self.assertIsNone(data["iv_hv_spread"]["realized_volatility_20d"])
        self.assertTrue(all(row["bleed"] is None and row["hist_iv"] is None for row in data["iv_bleed"]))
        self.assertIn("realized_volatility_20d", data["missing"])

    def test_the_response_is_valid_json_even_with_unquoted_nan_in_the_chain(self):
        bad = chain_frame(list(range(60, 141)), iv=float("nan"))
        self.registry["SPY"]["chains"] = {self.exp: (bad, bad)}
        self.fake_yahoo(**self.registry)
        r = self.client.get("/api/time_arbitrage?ticker=SPY")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("NaN", r.get_data(as_text=True))
        data = r.get_json()["data"]
        self.assertIsNone(data["iv_hv_spread"]["atm_implied_volatility"])
        self.assertIn("atm_implied_volatility", data["missing"])


# ============================================================ QuantEngine
class QuantEngineUnits(ApiCase):
    def engine(self, rows=None):
        path = self.tmp / "ledger.csv"
        if rows is not None:
            path.write_text("Datetime,VIX,GEX,DIX\n" + "\n".join(rows) + "\n")
        return quant_engine.QuantEngine(str(path))

    def recent(self, n, vix, gex="", dix=""):
        return [f"{(datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d %H:%M')},{vix(i)},{gex},{dix}"
                for i in range(n)]

    def test_oscillator_without_a_ledger_has_no_score(self):
        out = self.engine().calculate_z_score_oscillator({"vix": 20, "gex": 1, "dix": 0.4})
        self.assertIsNone(out["score"])
        self.assertEqual(out["components_used"], [])
        self.assertEqual(set(out["components_missing"]), {"vix", "gex", "dix"})

    def test_oscillator_skips_empty_components_instead_of_averaging_in_zeros(self):
        engine = self.engine(self.recent(10, lambda i: 15 + i % 3))
        out = engine.calculate_z_score_oscillator({"vix": 25, "gex": None, "dix": float("nan")})
        self.assertEqual(out["components_used"], ["vix"])
        self.assertEqual(set(out["components_missing"]), {"gex", "dix"})
        self.assertLess(out["score"], 0)                       # VIX far above its mean is bearish
        # one component carries the whole composite (not a third of it)
        mean = np.mean([15 + i % 3 for i in range(10)])
        std = np.std([15 + i % 3 for i in range(10)], ddof=1)
        self.assertAlmostEqual(out["score"], float(np.clip(-(25 - mean) / std, -2, 2) * 50))

    def test_oscillator_uses_gex_and_dix_once_they_have_history(self):
        rows = [f"{(datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d %H:%M')},{15 + i % 3},{100 + i},{0.4 + i / 100}"
                for i in range(10)]
        out = self.engine(rows).calculate_z_score_oscillator({"vix": 16, "gex": 104.5, "dix": 0.445})
        self.assertEqual(out["components_used"], ["vix", "gex", "dix"])
        self.assertEqual(out["components_missing"], {})

    def test_oscillator_needs_some_spread(self):
        engine = self.engine(self.recent(5, lambda i: 18))     # all equal: no standard deviation
        out = engine.calculate_z_score_oscillator({"vix": 25})
        self.assertIsNone(out["score"])
        self.assertIn("vix", out["components_missing"])

    def test_probabilities_need_a_rate_and_skip_strikes_without_volatility(self):
        engine = self.engine()
        items = [{"strike": 100, "iv": 0.2}, {"strike": 101, "iv": None}, {"strike": 102, "iv": 0.0},
                 {"strike": 103, "iv": float("nan")}]
        self.assertEqual(engine.calculate_strike_probabilities(100.0, items, r=None), [])
        out = engine.calculate_strike_probabilities(100.0, items, r=0.04)
        self.assertEqual([p["strike"] for p in out], [100.0])
        self.assertEqual(set(out[0]), {"strike", "iv", "prob_3d", "prob_5d", "prob_7d"})
        self.assertTrue(0 < out[0]["prob_3d"] < 1)

    def test_risk_free_rate_is_none_without_data_and_cached_when_found(self):
        engine = self.engine()
        self.fake_yahoo()
        self.assertIsNone(engine.get_risk_free_rate())
        self.fake_yahoo(**{"^IRX": {"history": closes(4.3)}})
        self.assertAlmostEqual(engine.get_risk_free_rate(), 0.043)
        self.fake_yahoo()                                       # Yahoo goes away: the cached rate is still good for a minute
        self.assertAlmostEqual(engine.get_risk_free_rate(), 0.043)

    def test_realized_volatility_is_none_without_enough_history(self):
        engine = self.engine()
        self.fake_yahoo(AAA={"history": closes(*([100.0] * 10))}, BBB={"raises": RuntimeError("down")},
                        CCC={"history": walk()})
        self.assertIsNone(engine.calculate_realized_volatility("AAA"))
        self.assertIsNone(engine.calculate_realized_volatility("BBB"))
        self.assertIsNone(engine.calculate_realized_volatility("ZZZ"))
        hv = engine.calculate_realized_volatility("CCC")
        self.assertGreater(hv, 0)

    def test_option_analytics_uses_the_given_tickers_volatility(self):
        engine = self.engine()
        asked = []
        engine.calculate_realized_volatility = lambda ticker=None, window=20: (asked.append(ticker), 0.2)[1]
        out = engine.calculate_option_analytics(100.0, 100.0, 30, 0.04, 0.35, "call", ticker="SLV")
        self.assertEqual(asked, ["SLV"])
        self.assertEqual(out["hv_pct"], 20.0)
        self.assertEqual(out["iv_signal"], "OVERPRICED — SELL")
        out = engine.calculate_option_analytics(100.0, 100.0, 30, 0.04, 0.35, "call")      # no ticker
        self.assertEqual(asked, ["SLV"])
        self.assertIsNone(out["hv_pct"])
        self.assertIsNone(out["iv_signal"])
        self.assertEqual(set(out["missing"]), {"hv_pct", "iv_signal", "market_price"})   # no price was given either
        self.assertIsNone(out["market_price"])


class JsonSafety(unittest.TestCase):
    def test_json_safe_replaces_nan_infinity_and_numpy_scalars(self):
        out = api_router._json_safe({"a": float("nan"), "b": [float("inf"), 1.5, np.float64(2.5), np.int64(3)],
                                     "c": {"d": None}})
        self.assertEqual(out, {"a": None, "b": [None, 1.5, 2.5, 3], "c": {"d": None}})
        json.dumps(out, allow_nan=False)

    def test_num_accepts_only_finite_numbers(self):
        self.assertEqual(api_router._num("2.5"), 2.5)
        for bad in (None, "x", float("nan"), float("inf"), pd.NA):
            self.assertIsNone(api_router._num(bad))


if __name__ == "__main__":
    unittest.main()
