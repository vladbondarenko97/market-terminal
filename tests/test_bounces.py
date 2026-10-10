"""Bounce notices: an email the SMTP server accepted can still be rejected by the recipient's server a minute later.
Offline: imaplib is replaced by a fake mailbox."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401  (temporary CME_Data, no credentials; must come before importing config)

import json
import unittest
from unittest import mock

import config
import main_pipeline as mp
import send_email
from core import lake

NOTICE = (b"From: MAILER-DAEMON@mail.example.test\r\nSubject: Undelivered Mail Returned to Sender\r\n\r\n"
          b"<someone@example.test>: host mx.example.test said: 550-5.7.1 blocked\r\n\r\n"
          b"Action: failed\r\nStatus: 5.7.1\r\nDiagnostic-Code: smtp; 550-5.7.1 [203.0.113.5 19] this message is likely\r\n"
          b"    suspicious due to the very low reputation of the sending domain\r\n\r\n"
          b"Subject: Daily Market Report\r\nX-Run-ID: RUN-BOUNCED\r\n")
OTHER = b"From: MAILER-DAEMON@mail.example.test\r\nSubject: Undelivered Mail\r\n\r\nsome other message, no run id\r\n"


class FakeMailbox:
    def __init__(self, *a, **k):
        self.selected = None

    def login(self, user, password):
        return "OK", []

    def select(self, box, readonly=False):
        self.selected = (box, readonly)
        return "OK", []

    def search(self, charset, query):
        assert 'FROM "MAILER-DAEMON" SINCE' in query
        return "OK", [b"1 2"]

    def fetch(self, i, what):
        assert what == "(BODY.PEEK[])"                     # never marks a message as read
        return "OK", [(b"x", NOTICE if i == b"1" else OTHER)]

    def logout(self):
        return "BYE", []


class Bounces(unittest.TestCase):
    def setUp(self):
        support.DATA.mkdir(parents=True, exist_ok=True)
        self.conn = lake.connect(config.DB_PATH)
        lake.migrate(self.conn)
        for rid, status in (("RUN-BOUNCED", "smtp_accepted"), ("RUN-FINE", "smtp_accepted")):
            if not lake.get_run(self.conn, rid):
                lake.create_run(self.conn, rid, mode="live", trigger="manual", run_folder="x", code_version="t")
            lake.update_run(self.conn, rid, delivery_status=status, delivery_detail=json.dumps({"smtp": "accepted"}))
        self.cfg = mock.patch.multiple(send_email, EMAIL_SENDER="reports@example.test", EMAIL_PASSWORD="x", IMAP_SERVER="mail.example.test")
        self.cfg.start()

    def tearDown(self):
        self.cfg.stop()
        self.conn.close()

    def test_notice_is_matched_to_its_run_and_the_reason_is_kept(self):
        with mock.patch("imaplib.IMAP4_SSL", FakeMailbox):
            found = send_email.bounced_runs()
        self.assertEqual(list(found), ["RUN-BOUNCED"])
        self.assertIn("very low reputation of the sending domain", found["RUN-BOUNCED"])
        self.assertNotIn("\n", found["RUN-BOUNCED"])

    def test_run_is_marked_bounced_once_and_others_are_untouched(self):
        with mock.patch("imaplib.IMAP4_SSL", FakeMailbox):
            first, second = mp.mark_bounces(self.conn), mp.mark_bounces(self.conn)
        self.assertEqual(len(first), 1)
        self.assertIn("RUN-BOUNCED bounced", first[0])
        self.assertEqual(second, [])                                              # one warning per bounce, not one per run
        row = lake.get_run(self.conn, "RUN-BOUNCED")
        self.assertEqual(row["delivery_status"], "bounced")
        self.assertEqual(json.loads(row["delivery_detail"])["smtp"], "accepted")  # what was recorded at send time is kept
        self.assertIn("5.7.1", json.loads(row["delivery_detail"])["bounce"])
        self.assertEqual(lake.get_run(self.conn, "RUN-FINE")["delivery_status"], "smtp_accepted")

    def test_unreadable_mailbox_or_no_email_setup_changes_nothing(self):
        def down(*a, **k):
            raise OSError("no IMAP here")
        with mock.patch("imaplib.IMAP4_SSL", down):
            self.assertEqual(mp.mark_bounces(self.conn), [])
        with mock.patch.object(send_email, "EMAIL_SENDER", ""), mock.patch("imaplib.IMAP4_SSL", FakeMailbox):
            self.assertEqual(mp.mark_bounces(self.conn), [])
        self.assertEqual(lake.get_run(self.conn, "RUN-BOUNCED")["delivery_status"], "smtp_accepted")


if __name__ == "__main__":
    unittest.main()
