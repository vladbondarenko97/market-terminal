"""Console engine model switch: the same switch as the iMessage assistant's /load (pick file, then the dash's placement POST), mocked (no oMLX, no dash)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401

sys.path.insert(0, str(support.ROOT / "options_whale"))
import assistant as A  # noqa: E402
from flask import Flask  # noqa: E402

M = A.SWITCH_MODELS


def _app():
    app = Flask(__name__)
    A.register(app)
    return app


def _resp(code=200, body=None):
    return mock.Mock(status_code=code, json=lambda: body if body is not None else {"ok": True})


class Switch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pick = Path(self.tmp.name) / "pick"
        self.pick.write_text(M["12b"])
        for p in (mock.patch.object(A, "PICK_FILE", self.pick), mock.patch.object(A, "can_switch", return_value=True)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.client = _app().test_client()

    def test_switch_writes_the_pick_then_posts_the_placement_with_its_header(self):
        with mock.patch.object(A.requests, "post", return_value=_resp()) as post:
            j = self.client.post("/api/assistant/switch", json={"model": "27b"}).get_json()
        self.assertEqual((j["status"], j["model"]), ("success", M["27b"]))
        self.assertEqual(self.pick.read_text(), M["27b"])
        self.assertEqual(post.call_args[0][0], "http://127.0.0.1:8803/placement/omlx?on=1")
        self.assertEqual(post.call_args[1]["headers"], {"X-Placement": "1"})

    def test_a_model_id_works_and_sonnet_turns_omlx_off_without_touching_the_pick(self):
        with mock.patch.object(A.requests, "post", return_value=_resp()) as post:
            self.assertEqual(self.client.post("/api/assistant/switch", json={"model": M["9b"]}).get_json()["status"], "success")
            self.assertEqual(self.pick.read_text(), M["9b"])
            self.assertEqual(self.client.post("/api/assistant/switch", json={"model": "sonnet"}).get_json()["status"], "success")
        self.assertEqual(self.pick.read_text(), M["9b"])
        self.assertTrue(post.call_args[0][0].endswith("on=0"))

    def test_a_failed_placement_restores_the_pick(self):
        with mock.patch.object(A.requests, "post", side_effect=A.requests.ConnectionError("down")):
            r = self.client.post("/api/assistant/switch", json={"model": "9b"})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(self.pick.read_text(), M["12b"])
        with mock.patch.object(A.requests, "post", return_value=_resp(200, {"ok": False})):
            self.assertEqual(self.client.post("/api/assistant/switch", json={"model": "27b"}).status_code, 502)
        self.assertEqual(self.pick.read_text(), M["12b"])

    def test_unknown_model_is_refused_without_a_call(self):
        with mock.patch.object(A.requests, "post") as post:
            r = self.client.post("/api/assistant/switch", json={"model": "gpt-9"})
        self.assertEqual(r.status_code, 502)
        post.assert_not_called()
        self.assertEqual(self.pick.read_text(), M["12b"])

    def test_oracle_has_no_switch(self):
        with mock.patch.object(A, "can_switch", return_value=False), mock.patch.object(A.requests, "post") as post:
            r = self.client.post("/api/assistant/switch", json={"model": "27b"})
            st = self.client.get("/api/assistant/status").get_json()
        self.assertEqual(r.status_code, 403)
        post.assert_not_called()
        self.assertFalse(st["local"]["can_switch"])

    def test_status_lists_what_omlx_reports_and_which_can_be_switched_to(self):
        listing = mock.Mock(status_code=200, json=lambda: {"data": [{"id": M["12b"]}, {"id": M["27b"]}, {"id": "other-model"}, {"id": "qwen3-embedding"}]})
        with mock.patch.object(A.requests, "get", return_value=listing):
            loc = self.client.get("/api/assistant/status").get_json()["local"]
        self.assertEqual((loc["model"], loc["can_switch"]), (M["12b"], True))
        self.assertEqual(loc["models"], sorted([M["12b"], M["27b"], "other-model"]))
        self.assertEqual(sorted(loc["switchable"]), sorted([M["12b"], M["27b"]]))

    def test_ask_only_ever_names_the_loaded_model(self):
        seen = []
        with mock.patch.object(A, "run_local", side_effect=lambda app, conv, q, model, r: seen.append(model) or iter([])), \
             mock.patch.object(A, "NINEB_OFF", Path("/nonexistent/off")):
            self.client.post("/api/assistant/ask", json={"question": "hi", "model": M["27b"], "engine": "local"}).get_data()
            self.pick.write_text(M["9b"])                         # after a switch the new pick is used
            self.client.post("/api/assistant/ask", json={"question": "hi", "model": M["27b"], "engine": "local"}).get_data()
        self.assertEqual(seen, [M["12b"], M["9b"]])


if __name__ == "__main__":
    unittest.main()
