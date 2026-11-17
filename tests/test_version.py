"""GET /api/version: git fields, env fallback without a checkout, the peer comparison and its recursion guard. Offline."""
import os
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
        api_router._PEER_CACHE = (0.0, None)
        self.addCleanup(setattr, api_router, "_GIT_INFO", None)
        for name, value in (("_V2_DB_PATH", self.tmp / "none.db"), ("_PROJECT_ROOT", self.tmp)):
            p = mock.patch.object(api_router, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(config, "CHART_REMOTE", "")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"MARKET_PEER": "", "MARKET_SERVER": "Test box"})
        p.start()
        self.addCleanup(p.stop)

    def get(self, url="/api/version"):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_checkout_answers_from_git(self):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t", "GIT_AUTHOR_DATE": "1700000000 +0000", "GIT_COMMITTER_DATE": "1700000000 +0000"}
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "first thing"]):
            subprocess.run(["git", "-C", str(self.tmp), *args], check=True, env=env, capture_output=True)
        sha = subprocess.run(["git", "-C", str(self.tmp), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        d = self.get()
        self.assertEqual((d["server"], d["commit"], d["subject"], d["committed"]), ("Test box", sha, "first thing", 1700000000))
        self.assertIsInstance(d["live_since"], int)
        self.assertEqual(d["chart_store"], "local")
        self.assertIsNone(d["peer"])
        self.assertEqual(d["last_run"]["snapshot_at"], None)

    def test_env_fallback_without_git(self):
        with mock.patch.dict(os.environ, {"GIT_SHA": "a" * 40, "GIT_SUBJECT": "built", "GIT_COMMITTED": "1700000001"}):
            d = self.get()
        self.assertEqual((d["commit"], d["subject"], d["committed"]), ("a" * 40, "built", 1700000001))
        self.assertEqual(d["live_since"], d["started"])

    def test_missing_is_null(self):
        with mock.patch.dict(os.environ, {"GIT_SHA": "", "GIT_SUBJECT": "", "GIT_COMMITTED": ""}):
            d = self.get()
        self.assertEqual((d["commit"], d["subject"], d["committed"]), (None, None, None))

    def test_peer_unreachable(self):
        with mock.patch.dict(os.environ, {"MARKET_PEER": "http://peer.invalid"}), \
                mock.patch.object(api_router.requests, "get", side_effect=requests.ConnectionError("down")) as get:
            self.assertEqual(self.get()["peer"], {"status": "unreachable"})
            self.get()                                  # cached for 60 s
        self.assertEqual(get.call_count, 1)

    def test_peer_answer_and_remote_chart_store(self):
        reply = mock.Mock(**{"json.return_value": {"server": "Oracle", "commit": "b" * 40, "live_since": 5, "extra": 1}})
        with mock.patch.dict(os.environ, {"MARKET_PEER": "http://peer.invalid/"}), \
                mock.patch.object(config, "CHART_REMOTE", "http://user:pw@oracle:8080/x"), \
                mock.patch.object(api_router.requests, "get", return_value=reply) as get:
            d = self.get()
        self.assertEqual(d["peer"], {"server": "Oracle", "commit": "b" * 40, "live_since": 5})
        self.assertEqual(d["chart_store"], "remote oracle")
        self.assertEqual(get.call_args[0][0], "http://user:pw@oracle:8080/x/api/version?peer=0")

    def test_peer_zero_does_not_fetch_back(self):
        with mock.patch.dict(os.environ, {"MARKET_PEER": "http://peer.invalid"}), \
                mock.patch.object(api_router.requests, "get") as get:
            self.assertIsNone(self.get("/api/version?peer=0")["peer"])
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
