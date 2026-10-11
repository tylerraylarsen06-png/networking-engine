#!/usr/bin/env python3
"""Tests for the ledger and the CSV-to-Sheet bridge. Run with: python3 -m unittest test_bridge"""
import contextlib
import csv
import io
import json
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import bridge
import dnc
import drafts
from ledger import Ledger, normalize_linkedin, row_keys

_DNC_TMP = None
_DNC_ORIGINAL = (dnc.DNC_PATH, dnc.LOG_PATH)


def setUpModule():
    """The bridge refuses to run without a Do Not Contact list. Point it at an empty one for these tests,
    so they never depend on (or read) a real dnc.json."""
    global _DNC_TMP
    _DNC_TMP = tempfile.TemporaryDirectory()
    dnc.DNC_PATH = Path(_DNC_TMP.name) / "dnc.json"
    dnc.DNC_PATH.write_text(json.dumps({"version": 1, "entries": []}))
    dnc.LOG_PATH = Path(_DNC_TMP.name) / "dnc-exclusions.log"


def tearDownModule():
    dnc.DNC_PATH, dnc.LOG_PATH = _DNC_ORIGINAL
    _DNC_TMP.cleanup()


def make_row(**over):
    row = {
        "name": "Jane Doe", "firm_queried": "Acme Foundation", "linkedin_url": "https://www.linkedin.com/in/jane-doe-123/",
        "seniority_band": "in_band", "email": "jane.doe@acme.org", "email_status": "valid", "tier": "3",
        "draft_subject": "USC sophomore, question about Acme Foundation",
        "draft_body": "Hi Jane,\n\nI'm a USC sophomore recruiting for consulting soon. I saw you went to USC too.\n\nAcme Foundation is high on my list and I'd love to hear about your experience there. Do you have any time over the next couple weeks?\n\nAlex",
    }
    row.update(over)
    return row


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.json"
        self.ledger = Ledger(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_linkedin_normalization(self):
        self.assertEqual(normalize_linkedin("https://www.linkedin.com/in/Jane-Doe-123/?trk=x"), "in/jane-doe-123")
        self.assertEqual(normalize_linkedin("http://linkedin.com/in/jane-doe-123"), "in/jane-doe-123")
        self.assertEqual(normalize_linkedin("https://example.com/profile"), "")

    def test_same_person_is_caught_by_any_key(self):
        self.ledger.add(make_row(), "queued")
        # different URL and email, same name+firm
        self.assertIsNotNone(self.ledger.blocked_reason(make_row(linkedin_url="https://linkedin.com/in/other", email="")))
        # different name spelling, same LinkedIn profile
        self.assertIsNotNone(self.ledger.blocked_reason(make_row(name="J. Doe", email="")))
        # different profile and name, same email
        self.assertIsNotNone(self.ledger.blocked_reason(make_row(name="Someone Else", linkedin_url="", email="JANE.DOE@acme.org")))
        # a genuinely different person is free
        self.assertIsNone(self.ledger.blocked_reason(make_row(name="John Roe", linkedin_url="https://linkedin.com/in/john-roe", email="john.roe@acme.org")))

    def test_same_name_at_a_different_firm_is_a_different_person(self):
        self.ledger.add(make_row(), "queued")
        self.assertIsNone(self.ledger.blocked_reason(make_row(firm_queried="Other Org", linkedin_url="", email="")))

    def test_filter_new_drops_ledger_hits_and_in_batch_duplicates(self):
        self.ledger.add(make_row(name="Old Person", linkedin_url="https://linkedin.com/in/old", email="old@acme.org"), "sent")
        rows = [make_row(name="Old Person", linkedin_url="https://linkedin.com/in/old", email="old@acme.org"),
                make_row(name="New A", linkedin_url="https://linkedin.com/in/a", email="a@acme.org"),
                make_row(name="New A", linkedin_url="https://linkedin.com/in/a", email="a@acme.org")]
        fresh, dropped = self.ledger.filter_new(rows)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(sorted(r for _, r in dropped)[0].startswith("already"), True)
        self.assertTrue(any("duplicate within this batch" in r for _, r in dropped))

    def test_bounced_replied_and_suppressed_are_never_free_again(self):
        for i, status in enumerate(["bounced", "replied", "suppressed", "opt_out"]):
            row = make_row(name=f"P{i}", linkedin_url=f"https://linkedin.com/in/p{i}", email=f"p{i}@x.org")
            self.ledger.add(row, status)
            self.assertIsNotNone(self.ledger.blocked_reason(row), status)

    def test_suppress_unknown_email_and_persistence(self):
        self.ledger.suppress_email("Never@Contact.com", "asked to stop")
        self.ledger.save()
        again = Ledger(self.path)
        self.assertIsNotNone(again.blocked_reason({"email": "never@contact.com"}))
        self.assertEqual(again.counts(), {"suppressed": 1})


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_sendable_rows_are_selected(self):
        rows = [
            make_row(name="Good One", linkedin_url="https://linkedin.com/in/g1", email="g1@a.org"),
            make_row(name="Below", seniority_band="below", linkedin_url="https://linkedin.com/in/b", email="b@a.org"),
            make_row(name="NoEmail", email="", linkedin_url="https://linkedin.com/in/n"),
            make_row(name="NoDraft", draft_body="", linkedin_url="https://linkedin.com/in/d", email="d@a.org"),
            make_row(name="Unknown", email_status="unknown", linkedin_url="https://linkedin.com/in/u", email="u@a.org"),
            make_row(name="Collision", email_status="invalid_possible_collision", linkedin_url="https://linkedin.com/in/c", email="c@a.org"),
        ]
        queue, stats, _ = bridge.select_sendable(rows, self.ledger, "Alex")
        self.assertEqual([q["name"] for q in queue], ["Good One"])
        self.assertEqual((stats["not_in_band"], stats["no_email"], stats["no_draft"], stats["status_not_allowed"]), (1, 1, 1, 2))

    def test_allow_status_widens_the_set_only_when_asked(self):
        rows = [make_row(name="U", email_status="unknown", email="u@a.org", linkedin_url="https://linkedin.com/in/u")]
        self.assertEqual(len(bridge.select_sendable(rows, self.ledger, "Alex")[0]), 0)
        self.assertEqual(len(bridge.select_sendable(rows, self.ledger, "Alex", allowed_statuses=("valid", "unknown"))[0]), 1)

    def test_drafts_are_revalidated_at_queue_time(self):
        bad = make_row(name="Bad", draft_body="Hi,\n\nThis has a dash – in it.", email="bad@a.org", linkedin_url="https://linkedin.com/in/bad")
        queue, stats, _ = bridge.select_sendable([bad], self.ledger, "Alex")
        self.assertEqual((len(queue), stats["failed_validation"]), (0, 1))

    def test_ledger_people_are_not_queued_again(self):
        row = make_row()
        self.ledger.add(row, "sent")
        queue, stats, _ = bridge.select_sendable([row], self.ledger, "Alex")
        self.assertEqual((len(queue), stats["already_in_ledger"]), (0, 1))

    def test_followup_body_is_rendered_and_valid(self):
        queue, _, _ = bridge.select_sendable([make_row()], self.ledger, "Alex")
        fb = queue[0]["followup_body"]
        self.assertIn("Hi Jane", fb)
        self.assertEqual(drafts.validate_draft("Re: x", fb), [])

    def test_queue_row_has_only_sheet_columns(self):
        queue, _, _ = bridge.select_sendable([make_row()], self.ledger, "Alex")
        self.assertTrue(set(queue[0]) <= set(bridge.SHEET_COLUMNS))
        self.assertEqual(queue[0]["status"], "queued")

    def test_columns_match_apps_script(self):
        src = (Path(__file__).parent / "sender" / "Logic.gs").read_text()
        block = re.search(r"const COLUMNS = \[(.*?)\];", src, re.S).group(1)
        self.assertEqual(re.findall(r"'(\w+)'", block), bridge.SHEET_COLUMNS)


class PullTests(unittest.TestCase):
    def test_pull_advances_and_never_unsuppresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = Ledger(Path(tmp) / "l.json")
            led.add(make_row(name="A", email="a@x.org", linkedin_url="https://linkedin.com/in/a"), "queued")
            led.add(make_row(name="B", email="b@x.org", linkedin_url="https://linkedin.com/in/b"), "bounced")
            sheet = [
                {"email": "a@x.org", "status": "sent", "reply_flag": "", "bounce_flag": ""},
                {"email": "b@x.org", "status": "sent", "reply_flag": "", "bounce_flag": ""},
                {"email": "c@x.org", "status": "replied", "reply_flag": "TRUE", "bounce_flag": "", "name": "C", "firm": "F"},
            ]
            changed = bridge.apply_pull(led, sheet)
            self.assertEqual(led.find({"email": "a@x.org"})["status"], "sent")
            self.assertEqual(led.find({"email": "b@x.org"})["status"], "bounced")
            self.assertEqual(led.find({"email": "c@x.org"})["status"], "replied")
            self.assertEqual(changed, {"sent": 1, "replied": 1})

    def test_followup_sent_maps_to_ledger(self):
        self.assertEqual(bridge.sheet_status_to_ledger_status({"status": "sent", "followup_status": "sent"}), "followup_sent")
        self.assertEqual(bridge.sheet_status_to_ledger_status({"status": "sent", "followup_status": "pending"}), "sent")


class _Handler(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.received.append(body)
        if body.get("token") != "tok":
            out = {"ok": False, "error": "unauthorized"}
        elif body["action"] == "append":
            rows = body["rows"]
            out = {"ok": True, "added": len(rows) - 1, "skipped": [{"email": rows[0]["email"], "reason": "already in sheet"}]}
        else:
            out = {"ok": True, "rows": []}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class EndToEndTests(unittest.TestCase):
    def test_push_marks_only_rows_the_sheet_accepted_and_prints_no_pii(self):
        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                csv_path = Path(tmp) / "in.csv"
                rows = [make_row(name="Skipped Person", email="skip@acme.org", linkedin_url="https://linkedin.com/in/s"),
                        make_row(name="Kept Person", email="keep@acme.org", linkedin_url="https://linkedin.com/in/k",
                                 draft_subject="Quick question about Acme Foundation",
                                 draft_body="Hi Kept,\n\nI'm trying to learn what the work at Acme is really like day to day. "
                                            "Could we find a few minutes to talk this month?\n\nAlex Example, President, Example Investing Club")]
                with open(csv_path, "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(rows[0]))
                    w.writeheader()
                    w.writerows(rows)
                import ledger as ledger_mod
                original = ledger_mod.LEDGER_PATH
                ledger_mod.LEDGER_PATH = Path(tmp) / "ledger.json"
                import os
                os.environ["SHEET_WEBAPP_URL"] = f"http://127.0.0.1:{srv.server_port}/exec"
                os.environ["SHEET_WEBAPP_TOKEN"] = "tok"
                buf = io.StringIO()
                try:
                    with contextlib.redirect_stdout(buf):
                        bridge.main(["queue", "--csv", str(csv_path), "--sender-name", "Alex Example", "--push"])
                    out = buf.getvalue()
                    led = ledger_mod.Ledger(Path(tmp) / "ledger.json")
                finally:
                    ledger_mod.LEDGER_PATH = original
                self.assertIn("added 1", out)
                for secret in ("Kept Person", "keep@acme.org", "Skipped Person", "tok", "127.0.0.1"):
                    self.assertNotIn(secret, out, "the bridge must print counts only")
                self.assertIsNotNone(led.find({"email": "keep@acme.org"}))
                self.assertIsNone(led.find({"email": "skip@acme.org"}), "a row the sheet refused must not be marked queued")
                self.assertIn("append", [r["action"] for r in _Handler.received])
        finally:
            srv.shutdown()

    def test_a_bad_token_fails_loudly_and_marks_nothing(self):
        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(SystemExit) as cm:
                bridge.webapp_call("status", url=f"http://127.0.0.1:{srv.server_port}/exec", token="wrong")
            self.assertIn("unauthorized", str(cm.exception))
        finally:
            srv.shutdown()

    def test_nothing_is_queued_without_an_explicit_transport(self):
        with self.assertRaises(SystemExit):
            bridge.main(["queue", "--csv", "x.csv", "--sender-name", "Alex"])


if __name__ == "__main__":
    unittest.main()
