"""Offline tests for configuration parsing, the run lock, stage isolation, exit codes and delivery results.

Three layers, none of which touches the network, a credential or a real data folder:
  * in-process checks of parsing, upload-reply judging and the lock (they never write to the database);
  * `_deliver()` with every channel stubbed;
  * one subprocess (`_child_main`) with its own temporary CME_Data that runs the pipeline for real from a stored
    snapshot, so stage isolation, exit codes, the offline folder, `interrupted` runs, backups and `ntfy-test` are
    checked end to end without disturbing the shared test installation.
"""
import contextlib
import copy
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401  (temporary CME_Data, no credentials; must come before importing config)

import config  # noqa: E402
import main_pipeline as mp  # noqa: E402
import send_email  # noqa: E402
import upload_data  # noqa: E402
from core import lake, runlock  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent


class Resp:
    """Stand-in for a requests.Response."""
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")
        self.ok = status < 400

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


# ============================================================ configuration
class ConfigParsing(unittest.TestCase):
    def test_blank_numbers_use_the_default(self):
        with mock.patch.dict(os.environ, {"T_PORT": "", "T_PORT2": "   "}):
            self.assertEqual(config.int_env("T_PORT", 587), 587)
            self.assertEqual(config.int_env("T_PORT2", 8080), 8080)
            self.assertEqual(config.int_env("T_PORT_UNSET", 9), 9)

    def test_numbers_are_parsed_and_bad_text_names_the_variable(self):
        with mock.patch.dict(os.environ, {"T_PORT": " 2525 "}):
            self.assertEqual(config.int_env("T_PORT", 587, minimum=1, maximum=65535), 2525)
        with mock.patch.dict(os.environ, {"T_PORT": "abc"}):
            with self.assertRaisesRegex(config.ConfigError, "T_PORT must be a whole number"):
                config.int_env("T_PORT", 587)
        with mock.patch.dict(os.environ, {"T_PORT": "70000"}):
            with self.assertRaises(config.ConfigError):
                config.int_env("T_PORT", 587, minimum=1, maximum=65535)

    def test_alias_is_used_when_the_primary_is_unset_or_blank(self):
        with mock.patch.dict(os.environ, {"DATABENTO_API_KEY": "", "DB_API_KEY": "alias-key"}):
            self.assertEqual(config.optional_env("DATABENTO_API_KEY", default="", alt_name="DB_API_KEY"), "alias-key")
            self.assertEqual(config.required_env("DATABENTO_API_KEY", alt_name="DB_API_KEY"), "alias-key")
        with mock.patch.dict(os.environ, {"DATABENTO_API_KEY": "primary", "DB_API_KEY": "alias-key"}):
            self.assertEqual(config.optional_env("DATABENTO_API_KEY", default="", alt_name="DB_API_KEY"), "primary")
        env = {k: v for k, v in os.environ.items() if k not in ("DATABENTO_API_KEY", "DB_API_KEY")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.optional_env("DATABENTO_API_KEY", default="", alt_name="DB_API_KEY"), "")
            with self.assertRaises(EnvironmentError):
                config.required_env("DATABENTO_API_KEY", alt_name="DB_API_KEY")

    def test_require_databento_key(self):
        with mock.patch.object(config, "DATABENTO_API_KEY", ""):
            with self.assertRaises(EnvironmentError):
                config.require_databento_key()
        with mock.patch.object(config, "DATABENTO_API_KEY", "k"):
            self.assertEqual(config.require_databento_key(), "k")

    def test_flags_need_exactly_one(self):
        with mock.patch.dict(os.environ, {"T_FLAG": "1"}):
            self.assertTrue(config.flag_env("T_FLAG"))
        for value in ("", "true", "yes", "2", "0"):
            with mock.patch.dict(os.environ, {"T_FLAG": value}):
                self.assertFalse(config.flag_env("T_FLAG"), value)

    def test_unused_alpha_vantage_setting_is_gone(self):
        self.assertFalse(hasattr(config, "ALPHA_VANTAGE_KEY"))

    def test_explicit_data_folder_is_never_created_but_the_default_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "CME_Data"
            with mock.patch.object(config, "DATA_DIR", target), mock.patch.object(config, "DATA_DIR_EXPLICIT", True):
                with self.assertRaisesRegex(config.ConfigError, "does not exist"):
                    config.ensure_data_dir(allow_create=True)
                self.assertFalse(target.exists())
            with mock.patch.object(config, "DATA_DIR", target), mock.patch.object(config, "DATA_DIR_EXPLICIT", False):
                with self.assertRaises(config.ConfigError):
                    config.ensure_data_dir()
                self.assertFalse(target.exists())
                self.assertEqual(config.ensure_data_dir(allow_create=True), target)
                self.assertTrue(target.is_dir())

    def test_run_refuses_a_missing_explicit_folder_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "typo" / "CME_Data"
            with mock.patch.object(config, "DATA_DIR", target), mock.patch.object(config, "DATA_DIR_EXPLICIT", True), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(mp.main(["run", "--offline"]), 1)
            self.assertIn("does not exist", err.getvalue())
            self.assertFalse(target.parent.exists())

    def test_import_in_a_fresh_clone_creates_no_folder_and_survives_blank_settings(self):
        """config.py copied next to nothing: a blank SMTP_PORT must not crash and the sibling CME_Data must not appear."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            proj, home = tmp / "proj", tmp / "home"
            proj.mkdir()
            home.mkdir()
            shutil.copy(ROOT / "config.py", proj / "config.py")
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "SMTP_PORT": "", "OPTIONS_WHALE_PORT": "",
                   "DATABENTO_API_KEY": "", "DB_API_KEY": "alias-key", "REPORT_UPLOAD": "", "ALPHAFLOW_DEBUG": "1",
                   "DASHBOARD_URL": "https://dash.invalid", "UPLOAD_URL": "https://up.invalid/r.php",
                   "UPLOAD_TOKEN": "tok"}
            code = ("import sys, json; sys.path.insert(0, sys.argv[1]); import config as c; "
                    "print(json.dumps({'smtp': c.SMTP_PORT, 'web': c.OPTIONS_WHALE_PORT, 'key': c.DATABENTO_API_KEY, "
                    "'debug': c.ALPHAFLOW_DEBUG, 'report': c.REPORT_UPLOAD, 'dash': c.DASHBOARD_URL, "
                    "'url': c.UPLOAD_URL, 'token': c.UPLOAD_TOKEN, 'explicit': c.DATA_DIR_EXPLICIT, "
                    "'data': str(c.DATA_DIR)}))")
            out = subprocess.run([sys.executable, "-c", code, str(proj)], env=env, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr[-1500:])
            got = json.loads(out.stdout.strip().splitlines()[-1])
            self.assertEqual((got["smtp"], got["web"]), (587, 8080))
            self.assertEqual(got["key"], "alias-key")
            self.assertTrue(got["debug"])
            self.assertFalse(got["report"])
            self.assertEqual((got["dash"], got["url"], got["token"]),
                             ("https://dash.invalid", "https://up.invalid/r.php", "tok"))
            self.assertFalse(got["explicit"])
            self.assertEqual(Path(got["data"]), (tmp / "CME_Data").resolve())
            self.assertEqual(sorted(p.name for p in tmp.iterdir()), ["home", "proj"], "import created a folder")


# ============================================================ delivery channels
class EmailAndPushStatus(unittest.TestCase):
    def _deliver(self, **settings):
        values = {"EMAIL_SENDER": "", "EMAIL_PASSWORD": "", "SMTP_SERVER": "", "RECIPIENT_EMAIL": ""}
        values.update(settings)
        with contextlib.ExitStack() as stack:
            for name, value in values.items():
                stack.enter_context(mock.patch.object(send_email, name, value))
            return send_email.deliver(TESTS_DIR / "does-not-exist.eml")

    def test_email_not_configured_at_all_is_skipped(self):
        status, detail = self._deliver()
        self.assertEqual(status, "skipped")
        self.assertIn("not configured", detail)

    def test_email_partly_configured_fails_and_names_what_is_missing(self):
        status, detail = self._deliver(EMAIL_SENDER="a@b", SMTP_SERVER="smtp.invalid")
        self.assertEqual(status, "failed")
        self.assertIn("EMAIL_PASSWORD", detail)
        self.assertIn("RECIPIENT_EMAIL", detail)

    def test_empty_ntfy_url_is_skipped_not_failed(self):
        with mock.patch.object(send_email, "NTFY_URL", ""), \
                mock.patch("requests.put", side_effect=AssertionError("no request without NTFY_URL")), \
                mock.patch("requests.post", side_effect=AssertionError("no request without NTFY_URL")):
            brief = send_email.ntfy_brief({"run": {"generated_at": "2026-10-09T14:31:00+00:00"}}, "report")
            alerts = send_email.ntfy_push([("t", "body", "urgent", "x")])
        self.assertEqual(brief["status"], "skipped")
        self.assertEqual([a["status"] for a in alerts], ["skipped"])


class UploadReplies(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="upload_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.day = self.tmp / "day"
        self.day.mkdir()
        (self.day / "volume_dashboard.txt").write_text("<dashboard/>")
        self.db = self.tmp / "portfolio.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE legacy (x)")
        con.execute("CREATE TABLE v2_runs (x)")
        con.commit()
        con.close()
        for name, value in (("UPLOAD_URL", "https://up.invalid/upload_receiver.php"), ("UPLOAD_TOKEN", "tok"),
                            ("DB_PATH", self.db)):
            patcher = mock.patch.object(upload_data, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _upload(self, response):
        with mock.patch.object(upload_data.requests, "post", return_value=response) as post:
            result = upload_data.upload_files(str(self.day))
        return result, post

    def test_every_file_ok_is_uploaded(self):
        (status, detail), post = self._upload(Resp(200, {"portfolio": {"status": "ok"}, "dashboard": {"status": "ok"}}))
        self.assertEqual(status, "uploaded")
        self.assertIn("portfolio: ok", detail)
        sent = post.call_args.kwargs["files"]
        self.assertEqual(sorted(sent), ["dashboard", "portfolio"])

    def test_http_200_with_a_rejected_file_is_a_failure(self):
        (status, detail), _ = self._upload(Resp(200, {"portfolio": {"status": "rejected", "name": "x.db"},
                                                      "dashboard": {"status": "ok"}}))
        self.assertEqual(status, "failed")
        self.assertIn("portfolio: rejected", detail)
        self.assertIn("dashboard: ok", detail)

    def test_a_file_missing_from_the_reply_or_an_error_status_is_a_failure(self):
        (status, detail), _ = self._upload(Resp(200, {"portfolio": {"status": "error", "code": 3}}))
        self.assertEqual(status, "failed")
        self.assertIn("portfolio: error", detail)
        self.assertIn("dashboard: missing from the reply", detail)

    def test_wrong_http_status_and_non_json_replies_fail(self):
        (status, detail), _ = self._upload(Resp(403, text="Unauthorized"))
        self.assertEqual(status, "failed")
        self.assertIn("HTTP 403", detail)
        (status, detail), _ = self._upload(Resp(200, text="<html>maintenance</html>"))
        self.assertEqual(status, "failed")
        self.assertIn("not a JSON object", detail)

    def test_a_network_error_fails(self):
        with mock.patch.object(upload_data.requests, "post", side_effect=ConnectionError("down")):
            status, detail = upload_data.upload_files(str(self.day))
        self.assertEqual(status, "failed")
        self.assertIn("down", detail)

    def test_unconfigured_upload_is_skipped_before_a_database_copy_is_built(self):
        with mock.patch.object(upload_data, "UPLOAD_URL", ""), mock.patch.object(upload_data, "UPLOAD_TOKEN", ""), \
                mock.patch.object(upload_data, "slim_db_copy", side_effect=AssertionError("copy built")), \
                mock.patch.object(upload_data.requests, "post", side_effect=AssertionError("request sent")):
            status, detail = upload_data.upload_files(str(self.day))
        self.assertEqual(status, "skipped")
        self.assertIn("not configured", detail)

    def test_only_one_of_url_and_token_is_a_failure_not_a_skip(self):
        with mock.patch.object(upload_data, "UPLOAD_TOKEN", ""), \
                mock.patch.object(upload_data, "slim_db_copy", side_effect=AssertionError("copy built")):
            status, detail = upload_data.upload_files(str(self.day))
        self.assertEqual(status, "failed")
        self.assertIn("both", detail)

    def test_the_database_copy_sent_has_no_v2_tables(self):
        seen = {}

        def post(url, data=None, files=None, timeout=None):
            handle = files["portfolio"][1]
            copy_path = self.tmp / "received.db"
            copy_path.write_bytes(handle.read())
            con = sqlite3.connect(copy_path)
            seen["tables"] = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            con.close()
            return Resp(200, {"portfolio": {"status": "ok"}, "dashboard": {"status": "ok"}})
        with mock.patch.object(upload_data.requests, "post", side_effect=post):
            self.assertEqual(upload_data.upload_files(str(self.day))[0], "uploaded")
        self.assertEqual(seen["tables"], ["legacy"])

    def test_report_upload_results(self):
        report = self.tmp / "report.txt"
        report.write_text("report")
        name = "market-report-2026-10-09-1431Z.txt"
        with mock.patch.object(upload_data, "REPORT_UPLOAD", False), \
                mock.patch.object(upload_data.requests, "post", side_effect=AssertionError("request sent")):
            self.assertEqual(upload_data.upload_report_result(report, name)["status"], "skipped")
            self.assertIsNone(upload_data.upload_report(report, name))
        with mock.patch.object(upload_data, "REPORT_UPLOAD", True):
            ok = Resp(200, {"report": {"status": "ok", "url": "https://up.invalid/reports/x.txt"}})
            with mock.patch.object(upload_data.requests, "post", return_value=ok):
                result = upload_data.upload_report_result(report, name)
            self.assertEqual((result["status"], result["url"]), ("uploaded", "https://up.invalid/reports/x.txt"))
            with mock.patch.object(upload_data.requests, "post", return_value=Resp(200, {"report": {"status": "rejected"}})):
                result = upload_data.upload_report_result(report, name)
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("url", result)
            with mock.patch.object(upload_data.requests, "post", return_value=Resp(200, {"report": {"status": "ok"}})):
                self.assertEqual(upload_data.upload_report_result(report, name)["status"], "failed")  # no URL returned
            with mock.patch.object(upload_data, "UPLOAD_URL", ""), mock.patch.object(upload_data, "UPLOAD_TOKEN", ""):
                result = upload_data.upload_report_result(report, name)
            self.assertEqual(result["status"], "failed")           # asked for, but no receiver configured


class DeliveryOutcomes(unittest.TestCase):
    """_deliver(): which results are warnings. Every channel is stubbed."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="deliver_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "email.eml").write_bytes(b"Subject: x\r\n\r\nbody\r\n")
        (self.tmp / "daily_market_report.txt").write_text("report")
        self.conn = lake.connect(":memory:")
        self.addCleanup(self.conn.close)
        lake.migrate(self.conn)
        lake.create_run(self.conn, "r1", mode="live", trigger="manual", run_folder="Oct-09-26", code_version="t")
        self.ctx = {"run": {"generated_at": "2026-10-09T14:31:00+00:00"}}
        self.defaults = {
            "email": ("skipped", "email not configured"),
            "brief": {"status": "skipped"},
            "push": [],
            "report": {"status": "skipped"},
            "files": ("skipped", "not configured"),
        }
        self.m = {
            "email": self._patch(send_email, "deliver"),
            "brief": self._patch(send_email, "ntfy_brief"),
            "push": self._patch(send_email, "ntfy_push"),
            "report": self._patch(upload_data, "upload_report_result"),
            "files": self._patch(upload_data, "upload_files"),
        }
        self.reset_stubs()

    def _patch(self, module, name):
        patcher = mock.patch.object(module, name)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def reset_stubs(self):
        for key, stub in self.m.items():
            stub.reset_mock(side_effect=True)
            stub.return_value = self.defaults[key]

    def deliver(self, upload=True):
        warnings = mp._deliver(self.conn, self.ctx, "r1", self.tmp, "report", upload=upload)
        return warnings, lake.get_run(self.conn, "r1")

    def test_channels_that_are_not_configured_are_skipped_without_a_warning(self):
        warnings, row = self.deliver()
        self.assertEqual(warnings, [])
        self.assertEqual(row["delivery_status"], "skipped")
        detail = json.loads(row["delivery_detail"])
        self.assertEqual(detail["upload"], ["skipped", "not configured"])
        self.assertEqual(detail["ntfy"][0]["status"], "skipped")

    def test_everything_delivered_has_no_warning(self):
        self.m["email"].return_value = ("smtp_accepted", "accepted")
        self.m["brief"].return_value = {"status": "sent"}
        self.m["report"].return_value = {"status": "uploaded", "url": "https://x/r.txt"}
        self.m["files"].return_value = ("uploaded", "portfolio: ok; dashboard: ok")
        warnings, row = self.deliver()
        self.assertEqual(warnings, [])
        self.assertEqual(row["delivery_status"], "smtp_accepted")
        self.assertEqual(self.m["brief"].call_args.args[3], "https://x/r.txt")   # the push links the uploaded report

    def test_a_configured_channel_that_fails_is_a_warning(self):
        cases = {
            "email": (("failed", "rejected: 550"), "email failed"),
            "email_unknown": (("outcome_unknown", "connection lost"), "email outcome_unknown"),
            "push": ({"status": "failed", "error": "HTTP 500"}, "phone push failed"),
            "report": ({"status": "failed", "detail": "report: rejected"}, "report upload failed"),
            "files": (("failed", "portfolio: rejected"), "upload failed"),
        }
        for name, (value, expected) in cases.items():
            with self.subTest(name):
                self.reset_stubs()
                key = {"email_unknown": "email", "push": "brief"}.get(name, name)
                self.m[key].return_value = value
                warnings, _ = self.deliver()
                self.assertEqual(len(warnings), 1, warnings)
                self.assertIn(expected, warnings[0])

    def test_one_channel_raising_does_not_stop_the_others(self):
        self.m["email"].side_effect = RuntimeError("smtp exploded")
        warnings, row = self.deliver()
        self.assertEqual(len(warnings), 1)
        self.assertIn("email failed", warnings[0])
        self.assertEqual(row["delivery_status"], "failed")
        for name in ("brief", "report", "files"):
            self.assertTrue(self.m[name].called, name)

    def test_no_upload_flag_makes_no_upload_call(self):
        warnings, row = self.deliver(upload=False)
        self.assertEqual(warnings, [])
        self.assertFalse(self.m["report"].called)
        self.assertFalse(self.m["files"].called)
        self.assertEqual(json.loads(row["delivery_detail"])["upload"], ["not_requested", None])


# ============================================================ the run lock
class LockedCommands(unittest.TestCase):
    def setUp(self):
        self.lock = runlock.RunLock()
        self.assertTrue(self.lock.acquire())
        self.addCleanup(self.lock.release)

    def test_lock_probe_sees_the_holder_and_never_keeps_the_lock(self):
        self.assertTrue(runlock.lock_is_held())
        self.lock.release()
        self.assertFalse(runlock.lock_is_held())
        again = runlock.RunLock()
        self.assertTrue(again.acquire())            # the probe left the lock free
        again.release()
        self.assertTrue(self.lock.acquire())        # let the cleanup release it

    def test_run_package_exports_are_unchanged(self):
        self.assertIs(mp.RunLock, runlock.RunLock)
        self.assertEqual(mp.EXIT_BUSY, 75)

    def _busy(self, argv, *forbidden):
        with contextlib.ExitStack() as stack:
            for target in forbidden:
                stack.enter_context(mock.patch(target, side_effect=AssertionError(f"{target} ran while the lock was held")))
            out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            code = mp.main(argv)
        self.assertIn("BUSY", out.getvalue())
        return code

    def test_import_history_waits_for_the_lock_and_makes_no_backup(self):
        self.assertEqual(self._busy(["import-history"], "core.importer.import_history",
                                    "main_pipeline.backup_database_file"), 75)

    def test_cme_login_takes_the_lock(self):
        self.assertEqual(self._busy(["cme-login"], "core.cme.interactive_login"), 75)

    def test_backfill_positions_takes_the_lock(self):
        self.assertEqual(self._busy(["backfill-positions"], "core.positions.backfill"), 75)

    def test_run_takes_the_lock(self):
        self.assertEqual(self._busy(["run", "--offline"], "core.sources.SourceSession.fetch"), 75)

    def test_download_scripts_take_the_lock(self):
        import download_volume
        import update_inventory
        with mock.patch.object(download_volume, "acquire_cme", side_effect=AssertionError("ran")), \
                mock.patch.object(update_inventory, "acquire_cme", side_effect=AssertionError("ran")):
            with self.assertRaises(runlock.RunBusy):
                download_volume.download_latest_cme_files(1)
            with self.assertRaises(runlock.RunBusy):
                update_inventory.update_silver_inventory(download=True)

    def test_busy_message_names_the_running_run(self):
        mp.set_status(run_id="r-busy", stage="collecting", state="running", pid=os.getpid(), mode="live")
        try:
            self.assertIn("run r-busy is in progress (stage collecting)", runlock.busy_message())
        finally:
            mp.STATUS_PATH.unlink()


# ============================================================ end to end, in a subprocess with its own CME_Data
def _child_main():
    """Runs in a subprocess (see EndToEnd). Prints one RESULT line of JSON."""
    from core import collect, positions
    from core.importer import import_history

    results = {}
    data = config.DATA_DIR

    def capture(fn, *args, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(*args, **kw)
        return rc, buf.getvalue()

    def last_run(**where):
        conn = lake.connect(config.DB_PATH)
        sql = "SELECT * FROM v2_runs"
        if where:
            sql += " WHERE " + " AND ".join(f"{k} = ?" for k in where)
        row = conn.execute(sql + " ORDER BY rowid DESC LIMIT 1", tuple(where.values())).fetchone()
        conn.close()
        return dict(row)

    import_history(verbose=False)
    conn = lake.connect(config.DB_PATH)
    lake.migrate(conn)

    # --- an offline run writes into its own folder and never into the day's shared files
    rc, _ = capture(mp.run, offline=True, deliver=False)
    row = last_run(mode="offline")
    out_dir = Path(json.loads(row["artifacts_json"])["out_dir"])
    day_dir = data / row["run_folder"]
    results["offline"] = {
        "rc": rc, "run_id": row["run_id"], "out_dir_name": out_dir.name, "parent_is_day": out_dir.parent == day_dir,
        "out_files": sorted(p.name for p in out_dir.iterdir() if p.is_file()),
        "shared_day_files": sorted(p.name for p in day_dir.iterdir() if p.is_file()) if day_dir.exists() else [],
        "ledger_csvs": sorted(p.name for p in data.glob("*_ledger.csv")),
    }
    base_ctx = lake.load_snapshot(conn, row["run_id"])

    # --- a killed run: status file and run row both say running, nobody holds the lock
    stale_id = "20260101T000000Z-stale1"
    lake.create_run(conn, stale_id, mode="live", trigger="manual", run_folder="Jan-01-26", code_version="t")
    mp.set_status(run_id=stale_id, stage="collecting", state="running", pid=999999, mode="live")
    rc, shown = capture(mp.status)
    holder = mp.RunLock()
    holder.acquire()
    _, shown_locked = capture(mp.status)
    holder.release()
    results["status"] = {
        "rc": rc,
        "row_shown_interrupted": any(stale_id in line and "'status': 'interrupted'" in line for line in shown.splitlines()),
        "file_shown_interrupted": '"state": "interrupted"' in shown,
        "row_in_db_unchanged": lake.get_run(conn, stale_id)["status"] == "running",
        "locked_shows_running": '"state": "running"' in shown_locked and "'status': 'interrupted'" not in shown_locked,
    }

    # --- live runs from a stored snapshot (no provider is called)
    def fake_build(conn_, session, run_meta, **kw):
        ctx = copy.deepcopy(base_ctx)
        ctx["run"] = {**ctx["run"], **run_meta}
        return ctx, {}

    def live_run(**kw):
        with mock.patch.object(collect, "build_context", fake_build), \
                mock.patch.object(collect, "context_observations", lambda ctx, frames: []):
            return capture(mp.run, offline=False, **kw)

    with mock.patch.object(positions, "record_signal", side_effect=RuntimeError("boom")):
        rc, out = live_run(deliver=False, upload=False)
    row = last_run(mode="live", trigger="manual")
    stages = json.loads(row["stages_json"])
    day_dir = data / row["run_folder"]
    results["broken_signal"] = {
        "rc": rc, "status": row["status"], "error": row["error"], "stage_keys": sorted(stages),
        "warnings": stages.get("warnings"),
        "day_files": sorted(p.name for p in day_dir.iterdir() if p.is_file()),
        "status_file_state": json.loads(mp.STATUS_PATH.read_text())["state"],
        "lock_held_after": runlock.lock_is_held(),
        "stale_row_status": lake.get_run(conn, stale_id)["status"],
        "traceback_has_boom": "boom" in stages.get("trade_signal_error", ""),
        "forecasts_logged": stages.get("forecasts_logged"),
        "ledgers": stages.get("ledgers"),
    }

    rc, out = live_run(deliver=True, upload=True)          # nothing is configured: every channel is skipped
    row = last_run(mode="live", trigger="manual")
    results["unconfigured_delivery"] = {
        "rc": rc, "status": row["status"], "error": row["error"], "delivery_status": row["delivery_status"],
        "detail": json.loads(row["delivery_detail"]),
        "trade_signal": json.loads(row["stages_json"]).get("trade_signal"),
    }

    # --- ntfy-test: exit 2 unless the push went out
    rc_skipped, _ = capture(mp.main, ["ntfy-test"])
    with mock.patch.object(send_email, "NTFY_URL", "https://ntfy.invalid/topic"), \
            mock.patch("requests.put", side_effect=requests.ConnectionError("down")), \
            mock.patch("requests.post", side_effect=requests.ConnectionError("down")):
        rc_failed, _ = capture(mp.main, ["ntfy-test"])
    ok = Resp(200, {"attachment": {"name": "r.txt"}})
    with mock.patch.object(send_email, "NTFY_URL", "https://ntfy.invalid/topic"), mock.patch("requests.put", return_value=ok):
        rc_sent, _ = capture(mp.main, ["ntfy-test"])
    results["ntfy_test"] = {"skipped": rc_skipped, "failed": rc_failed, "sent": rc_sent}

    # --- import-history backs the database up first, --no-backup does not
    with mock.patch("core.importer.import_history") as importer:
        rc1, out1 = capture(mp.main, ["import-history"])
        first = sorted((data / "backups").glob("portfolio-*.db"))
        rc2, _ = capture(mp.main, ["import-history", "--no-backup"])
        second = sorted((data / "backups").glob("portfolio-*.db"))
    backup_ok = False
    if first:
        check = sqlite3.connect(first[0])
        backup_ok = check.execute("SELECT COUNT(*) FROM v2_runs").fetchone()[0] > 0
        check.close()
    results["import_history"] = {"rc": (rc1, rc2), "backups_after_first": len(first), "backups_after_second": len(second),
                                 "importer_calls": importer.call_count, "backup_opens": backup_ok,
                                 "backup_named_utc": bool(first and re.fullmatch(r"portfolio-\d{8}T\d{6}Z\.db", first[0].name))}

    # --- backfill-positions: a committed live run without a ticket gets one, once
    rid = "20260102T000000Z-back01"
    lake.create_run(conn, rid, mode="live", trigger="manual", run_folder="Jan-02-26", code_version="t")
    lake.commit_snapshot(conn, rid, {"run": {"run_id": rid, "generated_at": "2026-01-02T15:00:00+00:00"},
                                     "technicals": {"current_price": 500.0},
                                     "execution": {"total_score": 1, "directional_bias": "NEUTRAL"}})
    lake.update_run(conn, rid, status="completed")
    before = conn.execute("SELECT COUNT(*) FROM v2_trade_signals WHERE run_id = ?", (rid,)).fetchone()[0]
    rc1, out1 = capture(mp.main, ["backfill-positions"])
    rc2, out2 = capture(mp.main, ["backfill-positions"])
    after = conn.execute("SELECT COUNT(*) FROM v2_trade_signals WHERE run_id = ?", (rid,)).fetchone()[0]
    results["backfill"] = {"rc": (rc1, rc2), "rows": (before, after), "first": out1.strip(), "second": out2.strip()}
    conn.close()
    print("RESULT:" + json.dumps(results, default=str))


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {k: v for k, v in os.environ.items() if k != "PORTFOLIO_DATA_DIR"}
        proc = subprocess.run([sys.executable, "-c", "import test_pipeline_fixes as t; t._child_main()"],
                              cwd=TESTS_DIR, env=env, capture_output=True, text=True, timeout=900)
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
        if proc.returncode != 0 or not lines:
            raise AssertionError(f"child failed ({proc.returncode}):\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}")
        cls.r = json.loads(lines[-1][len("RESULT:"):])

    def test_offline_run_has_its_own_folder_and_leaves_the_day_files_alone(self):
        r = self.r["offline"]
        self.assertEqual(r["rc"], 0)
        self.assertEqual(r["out_dir_name"], f"offline_{r['run_id']}")
        self.assertTrue(r["parent_is_day"])
        for name in ("email.eml", "daily_market_report.txt", "volume_dashboard.html", "run_manifest.json"):
            self.assertIn(name, r["out_files"])
        self.assertEqual(r["shared_day_files"], [], "an offline run wrote into the shared day folder")
        self.assertEqual(r["ledger_csvs"], [], "an offline run appended to a ledger CSV")

    def test_a_failing_stage_is_a_warning_and_the_other_stages_still_run(self):
        r = self.r["broken_signal"]
        self.assertEqual(r["rc"], 3)
        self.assertEqual(r["status"], "completed_with_warnings")
        self.assertEqual(len(r["warnings"]), 1, r["warnings"])
        self.assertIn("trade_signal error: boom", r["warnings"][0])
        self.assertIn("trade_signal error", r["error"])
        self.assertTrue(r["traceback_has_boom"])
        self.assertIn("trade_signal_error", r["stage_keys"])
        self.assertIn("export", r["stage_keys"])                 # rendering still ran
        self.assertIsInstance(r["forecasts_logged"], int)         # so did the forecast log
        self.assertIsInstance(r["ledgers"], dict)                 # and the ledgers
        for name in ("email.eml", "daily_market_report.txt", "volume_dashboard.html", "tactical_ruling.txt"):
            self.assertIn(name, r["day_files"])
        self.assertEqual(r["status_file_state"], "completed_with_warnings")
        self.assertFalse(r["lock_held_after"])

    def test_unconfigured_channels_are_skipped_and_the_run_is_clean(self):
        r = self.r["unconfigured_delivery"]
        self.assertEqual(r["rc"], 0, r["error"])
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["delivery_status"], "skipped")
        self.assertEqual(r["detail"]["ntfy"][0]["status"], "skipped")
        self.assertEqual(r["detail"]["upload"][0], "skipped")
        self.assertEqual(r["detail"]["report_upload"]["status"], "skipped")
        self.assertIsNotNone(r["trade_signal"])

    def test_killed_run_shows_interrupted_and_the_next_run_records_it(self):
        s = self.r["status"]
        self.assertEqual(s["rc"], 0)
        self.assertTrue(s["row_shown_interrupted"])
        self.assertTrue(s["file_shown_interrupted"])
        self.assertTrue(s["row_in_db_unchanged"], "status must not write to the database")
        self.assertTrue(s["locked_shows_running"], "a run that holds the lock is running, not interrupted")
        self.assertEqual(self.r["broken_signal"]["stale_row_status"], "interrupted")

    def test_ntfy_test_exits_2_unless_the_push_was_sent(self):
        self.assertEqual(self.r["ntfy_test"], {"skipped": 2, "failed": 2, "sent": 0})

    def test_import_history_backs_up_first_unless_told_not_to(self):
        h = self.r["import_history"]
        self.assertEqual(h["rc"], [0, 0])
        self.assertEqual((h["backups_after_first"], h["backups_after_second"]), (1, 1))
        self.assertEqual(h["importer_calls"], 2)
        self.assertTrue(h["backup_opens"])
        self.assertTrue(h["backup_named_utc"])

    def test_backfill_positions_is_idempotent(self):
        b = self.r["backfill"]
        self.assertEqual(b["rc"], [0, 0])
        self.assertEqual(b["rows"], [0, 1])
        self.assertGreaterEqual(int(re.search(r"(\d+) new position row", b["first"]).group(1)), 1)
        self.assertIn("0 new position row(s)", b["second"])


if __name__ == "__main__":
    unittest.main()
