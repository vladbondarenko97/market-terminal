"""GET /api/version: git fields, env fallback without a checkout, the background peer fetch and the run times. Offline."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401,E402  (temporary CME_Data, no credentials; must come before importing config)

import config  # noqa: E402
import requests  # noqa: E402

sys.path.insert(0, str(support.ROOT / "options_whale"))
import api_router  # noqa: E402


class Version(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.client = api_router.app.test_client()
        api_router._GIT_INFO = None
        self.addCleanup(setattr, api_router, "_GIT_INFO", None)
        for obj, name, value in ((api_router, "_V2_DB_PATH", self.tmp / "none.db"), (api_router, "_PROJECT_ROOT", self.tmp),
                                 (config, "CHART_REMOTE", ""), (config, "MARKET_PEER", ""), (config, "MARKET_SERVER", "Test box"),
                                 (config, "GIT_SHA", ""), (config, "GIT_SUBJECT", ""), (config, "GIT_COMMITTED", "")):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        # no real refresher thread: the tests call the loop body through a fake sleep that stops it
        p = mock.patch.dict(api_router._PEER, {"value": None, "thread": None})
        p.start()
        self.addCleanup(p.stop)

    def get(self, url="/api/version"):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def run_peer_loop(self, get):
        """One pass of the background fetch with a fake requests.get."""
        class Stop(Exception):
            pass
        with mock.patch.object(api_router.requests, "get", get), mock.patch.object(api_router.time, "sleep", side_effect=Stop):
            with self.assertRaises(Stop):
                api_router._peer_loop()

    def test_checkout_answers_from_git(self):
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
               "GIT_AUTHOR_DATE": "1700000000 +0000", "GIT_COMMITTER_DATE": "1700000000 +0000", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "first thing"]):
            subprocess.run(["git", "-C", str(self.tmp), *args], check=True, env=env, capture_output=True)
        sha = subprocess.run(["git", "-C", str(self.tmp), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        d = self.get()
        self.assertEqual((d["server"], d["commit"], d["subject"], d["committed"]), ("Test box", sha, "first thing", 1700000000))
        self.assertIsInstance(d["live_since"], int)
        self.assertEqual(d["chart_store"], "local")
        self.assertIsNone(d["peer"])
        self.assertIsNone(d["last_run"]["snapshot_at"])

    def test_failed_git_read_is_not_cached(self):
        (self.tmp / ".git").mkdir()
        self.assertIsNone(self.get()["commit"])
        self.assertIsNone(api_router._GIT_INFO)

    def test_env_fallback_without_git(self):
        with mock.patch.object(config, "GIT_SHA", "a" * 40), mock.patch.object(config, "GIT_SUBJECT", "built"), \
                mock.patch.object(config, "GIT_COMMITTED", "1700000001"):
            d = self.get()
        self.assertEqual((d["commit"], d["subject"], d["committed"]), ("a" * 40, "built", 1700000001))
        self.assertEqual(d["live_since"], d["started"])

    def test_missing_is_null(self):
        d = self.get()
        self.assertEqual((d["commit"], d["subject"], d["committed"]), (None, None, None))

    def test_peer_unreachable(self):
        self.run_peer_loop(mock.Mock(side_effect=requests.ConnectionError("down")))
        with mock.patch.object(config, "MARKET_PEER", "http://peer.invalid"), \
                mock.patch.object(api_router.threading, "Thread"):
            self.assertEqual(self.get()["peer"], {"status": "unreachable"})

    def test_peer_is_null_before_the_first_answer_and_the_request_does_not_fetch(self):
        with mock.patch.object(config, "MARKET_PEER", "http://peer.invalid"), mock.patch.object(api_router.threading, "Thread") as t, \
                mock.patch.object(api_router.requests, "get") as get:
            self.assertIsNone(self.get()["peer"])
            self.get()
        get.assert_not_called()
        self.assertEqual(t.call_count, 1)                    # started once

    def test_peer_answer_precedence_and_remote_chart_store(self):
        reply = mock.Mock(**{"json.return_value": {"server": "Oracle", "commit": "b" * 40, "committed": 7, "live_since": 5, "x": 1}})
        get = mock.Mock(return_value=reply)
        with mock.patch.object(config, "MARKET_PEER", "http://peer.invalid"), \
                mock.patch.object(config, "CHART_REMOTE", "http://user:pw@oracle:8080/x"):
            self.run_peer_loop(get)
            with mock.patch.object(api_router.threading, "Thread"):
                d = self.get()
        self.assertEqual(d["peer"], {"server": "Oracle", "commit": "b" * 40, "committed": 7, "live_since": 5})
        self.assertEqual(d["chart_store"], "remote oracle")
        self.assertEqual(get.call_args[0][0], "http://peer.invalid/api/version?peer=0")   # MARKET_PEER wins over CHART_REMOTE

    def test_chart_remote_without_scheme(self):
        with mock.patch.object(config, "CHART_REMOTE", "oracle:8080"), mock.patch.object(api_router.threading, "Thread"):
            self.assertEqual(self.get()["chart_store"], "remote oracle")

    def test_peer_zero_does_not_start_the_peer_fetch(self):
        with mock.patch.object(config, "MARKET_PEER", "http://peer.invalid"), mock.patch.object(api_router.threading, "Thread") as t:
            self.assertIsNone(self.get("/api/version?peer=0")["peer"])
        t.assert_not_called()

    def test_run_times_are_unix_and_a_running_run_hides_old_times(self):
        status = {"run_id": "r1", "state": "completed", "stage": "done", "started_at": "2026-06-01T10:00:00Z",
                  "finished_at": "2026-06-01T10:05:00+00:00"}
        with mock.patch.object(api_router.runlock, "read_status", return_value=status), \
                mock.patch.object(api_router, "_run_lock_held", return_value=False):
            lr = self.get()["last_run"]
            self.assertEqual((lr["started_at"], lr["finished_at"]), (1780308000, 1780308300))
            status["state"] = "running"
            self.assertEqual(self.get()["last_run"]["state"], "interrupted")
        with mock.patch.object(api_router.runlock, "read_status", return_value=dict(status, state="running", finished_at=None)), \
                mock.patch.object(api_router, "_run_lock_held", return_value=True):
            lr = self.get()["last_run"]
        self.assertEqual((lr["state"], lr["started_at"], lr["finished_at"], lr["snapshot_at"]), ("running", 1780308000, None, None))


if __name__ == "__main__":
    unittest.main()
