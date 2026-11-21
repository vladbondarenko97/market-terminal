"""Email transport choice (EMAIL_TRANSPORT smtp|resend), the Resend HTTPS send on a built email.eml, and the ntfy footer for a failed or bounced email."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401  (temporary CME_Data, no credentials; must come before importing config)

import base64
import json
import tempfile
from types import SimpleNamespace
from unittest import mock

import requests

import main_pipeline as mp
import send_email

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
SET = dict(RESEND_API_KEY="re_test", RESEND_FROM="report@example.test", RECIPIENT_EMAIL="to@example.test")


def make_eml():
    d = Path(tempfile.mkdtemp(prefix="resend_"))
    (d / "c.png").write_bytes(PNG)
    manifest = [{"family": 1, "email_title": "T", "window_label": "1D", "status": "generated", "path": str(d / "c.png"), "file": "c.png"}]
    msg = send_email.build_email({"run": {"run_id": "R1", "run_folder": "Nov-20-26"}}, "REPORT BODY", manifest, d)
    send_email.save_eml(msg, d / "email.eml")
    return d / "email.eml"


def reply(status=200, body=None):
    return SimpleNamespace(status_code=status, json=lambda: body)


class Transport(unittest.TestCase):
    def setUp(self):
        self.eml = make_eml()

    def send(self, post, **cfg):
        with mock.patch.multiple(send_email, **{"EMAIL_TRANSPORT": "resend", **SET, **cfg}), mock.patch("requests.post", **post) as p:
            return send_email.deliver(self.eml), p

    def test_resend_posts_text_html_and_the_inline_chart(self):
        (status, detail), p = self.send({"return_value": reply(200, {"id": "abc-1"})})
        self.assertEqual(status, "smtp_accepted")
        self.assertIn("abc-1", detail)
        body = p.call_args.kwargs["json"]
        self.assertEqual(p.call_args.args[0], "https://api.resend.com/emails")
        self.assertEqual(p.call_args.kwargs["headers"]["Authorization"], "Bearer re_test")
        self.assertEqual((body["from"], body["to"], body["headers"]["X-Run-ID"]), ("report@example.test", ["to@example.test"], "R1"))
        self.assertIn("REPORT BODY", body["text"])
        cid = body["attachments"][0]["content_id"]
        self.assertIn(f"cid:{cid}", body["html"])
        self.assertEqual(base64.b64decode(body["attachments"][0]["content"]), PNG)

    def test_resend_rejection_is_failed_with_its_message_and_a_timeout_is_unknown(self):
        (status, detail), _ = self.send({"return_value": reply(422, {"name": "validation_error", "message": "domain is not verified"})})
        self.assertEqual(status, "failed")
        self.assertIn("domain is not verified", detail)
        (status, _), _ = self.send({"side_effect": requests.ReadTimeout("slow")})
        self.assertEqual(status, "outcome_unknown")
        (status, _), _ = self.send({"side_effect": requests.ConnectionError("down")})
        self.assertEqual(status, "failed")

    def test_resend_settings_all_empty_is_skipped_partly_set_is_failed_and_names_it(self):
        (status, _), p = self.send({"return_value": reply()}, RESEND_API_KEY="", RESEND_FROM="", RECIPIENT_EMAIL="")
        self.assertEqual(status, "skipped")
        (status, detail), p = self.send({"return_value": reply()}, RESEND_API_KEY="")
        self.assertEqual((status, p.called), ("failed", False))
        self.assertIn("RESEND_API_KEY", detail)

    def test_transport_choice_smtp_is_the_default_and_never_calls_resend(self):
        with mock.patch.multiple(send_email, EMAIL_TRANSPORT="smtp", **SET, EMAIL_SENDER="a@b", EMAIL_PASSWORD="x", SMTP_SERVER="smtp.invalid"), \
                mock.patch("smtplib.SMTP", side_effect=OSError("refused")), mock.patch("requests.post") as post:
            status, detail = send_email.deliver(self.eml)
        self.assertEqual((status, post.called), ("failed", False))
        self.assertIn("connect/login", detail)

    def test_config_rejects_an_unknown_transport(self):
        import subprocess
        code = "import os; os.environ['EMAIL_TRANSPORT']='carrier-pigeon'; os.environ['MT_NO_KEYCHAIN']='1'; os.environ['PORTFOLIO_DATA_DIR']=%r; import config" % str(support.DATA)
        r = subprocess.run([sys.executable, "-c", code], cwd=str(support.ROOT), capture_output=True, text=True)
        self.assertIn("EMAIL_TRANSPORT must be smtp or resend", r.stderr)


class NtfyFooter(unittest.TestCase):
    def test_email_notice_wording(self):
        self.assertIsNone(mp.email_notice("smtp_accepted", "ok", []))
        self.assertIsNone(mp.email_notice("skipped", "not configured", []))
        self.assertEqual(mp.email_notice("failed", "rejected: (550, b'5.7.1 low domain\n reputation')", []), "email: failed (rejected: (550, b'5.7.1 low domain reputation'))")
        self.assertEqual(mp.email_notice("smtp_accepted", "ok", ["email of run R bounced: 550 5.7.1 low domain reputation"]),
                         "email: bounced (550 5.7.1 low domain reputation)")
        self.assertTrue(mp.email_notice("failed", "x" * 500, []).startswith("email: failed (xxx"))
        self.assertLess(len(mp.email_notice("failed", "x" * 500, [])), 130)

    def test_a_failed_email_puts_one_line_at_the_bottom_of_the_runs_ntfy_and_no_other_push(self):
        from core import lake
        import upload_data
        conn = lake.connect(":memory:")
        lake.migrate(conn)
        lake.create_run(conn, "r1", mode="live", trigger="manual", run_folder="Nov-20-26", code_version="t")
        with mock.patch.object(send_email, "deliver", return_value=("failed", "rejected: 550 5.7.1 low domain reputation")), \
                mock.patch.object(send_email, "ntfy_brief", return_value={"status": "sent"}) as brief, \
                mock.patch.object(send_email, "ntfy_push", return_value=[]) as push, \
                mock.patch.object(upload_data, "upload_files", return_value=("skipped", "")), \
                mock.patch.object(upload_data, "upload_report_result", return_value={"status": "skipped"}), \
                mock.patch.object(mp, "mark_bounces", return_value=[]):
            mp._deliver(conn, {"run": {}}, "r1", Path(tempfile.mkdtemp()), "report", upload=False)
        self.assertEqual(brief.call_args.kwargs["notice"], "email: failed (rejected: 550 5.7.1 low domain reputation)")
        push.assert_called_once()                     # the existing refinery-alerts call only: no separate email alert


if __name__ == "__main__":
    unittest.main()
