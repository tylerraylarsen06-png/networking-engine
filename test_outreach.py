"""outreach.py: lists, selection, writing fallbacks, Gmail drafts, and the full run with fakes.
All names, schools, companies and addresses here are made up. No network calls."""
import csv
import os
import re
import shutil
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import method
import outreach
from test_method import PROFILE

HERE = Path(__file__).resolve().parent


def person(name, title, company="Examplecorp", **kw):
    return dict({"name": name, "title": title, "company": company, "email": "", "linkedin_url": "", "source": "exa"}, **kw)


class FakeContacted(outreach.ContactedLog):
    def __init__(self, rows=()):
        self.path = Path(tempfile.mkdtemp()) / "contacted.csv"
        self.rows = list(rows)
        self.ledger = None


class FakeCareerShift:
    def __init__(self, rows):
        self.rows, self.revealed = rows, []

    def people(self, company, school, n):
        return [dict(r) for r in self.rows]

    def reveal(self, p):
        self.revealed.append(p["name"])
        return p["name"].split()[0].lower() + "@examplecorp.test"


class FakeClaude:
    def __init__(self, text):
        self.text, self.calls = text, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        block = mock.Mock(type="text", text=self.text)
        return mock.Mock(content=[block])


def args(**kw):
    base = dict(company="Examplecorp", role="Product Management Intern", school="Example State University", n=3,
                industry="", city="", reveal=0, no_gmail=False, no_claude=False, no_log=False, profile=None)
    base.update(kw)
    return Namespace(**base)


class DoNotContact(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_missing_list_fails_closed(self):
        with self.assertRaises(outreach.SetupError):
            outreach.load_dnc(self.tmp / "nope.csv", include_json=False)

    def test_example_csv_loads(self):
        entries = outreach.load_dnc(HERE / "data" / "do_not_contact.example.csv", include_json=False)
        self.assertEqual(len(entries), 2)

    def test_dnc_person_is_skipped(self):
        path = self.tmp / "dnc.csv"
        path.write_text("name,company,email,linkedin,org,reason\nJamie Doe,Examplecorp,,,,family friend\n")
        entries = outreach.load_dnc(path, include_json=False)
        people = [person("Jamie Doe", "Product Marketing Manager"), person("Riley Roe", "Product Marketing Lead")]
        chosen, skipped = outreach.select_people(people, PROFILE, "Product Management Intern", 3, "", entries, FakeContacted())
        self.assertEqual([p["name"] for p in chosen], ["Riley Roe"])
        self.assertIn("Do Not Contact", skipped[0][1])

    def test_dnc_by_email_and_org(self):
        path = self.tmp / "dnc.csv"
        path.write_text("name,company,email,linkedin,org,reason\n,,riley@examplecorp.test,,,\n,Partnerco,,,yes,partner\n")
        entries = outreach.load_dnc(path, include_json=False)
        self.assertTrue(outreach.dnc_hit(person("Riley Roe", "PM", email="riley@examplecorp.test"), entries))
        self.assertTrue(outreach.dnc_hit(person("Anyone Else", "PM", company="Partnerco"), entries))
        self.assertIsNone(outreach.dnc_hit(person("Anyone Else", "PM"), entries))

    def test_revealed_email_rechecked(self):
        path = self.tmp / "dnc.csv"
        path.write_text("name,company,email,linkedin,org,reason\n,,jamie@examplecorp.test,,,\n")
        entries = outreach.load_dnc(path, include_json=False)
        cs = FakeCareerShift([])
        p = person("Jamie Doe", "PM", careershift_id="x1", has_email=True)
        self.assertEqual(outreach.reveal_emails([p], cs, 1, entries), [])


class AlreadyContacted(unittest.TestCase):
    def test_contacted_person_is_skipped(self):
        log = FakeContacted([{"date": "2026-01-01", "name": "Jamie Doe", "company": "Examplecorp", "email": "",
                              "linkedin_url": "", "status": "drafted"}])
        people = [person("Jamie Doe", "Product Marketing Manager"), person("Riley Roe", "Product Marketing Lead")]
        chosen, skipped = outreach.select_people(people, PROFILE, "Product Management Intern", 3, "", [], log)
        self.assertEqual([p["name"] for p in chosen], ["Riley Roe"])
        self.assertIn("already drafted", skipped[0][1])

    def test_log_round_trip(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        log = outreach.ContactedLog(tmp / "c.csv", ledger=False)
        log.ledger = None
        log.append([person("Riley Roe", "PM", email="riley@examplecorp.test")], when="2026-02-02")
        again = outreach.ContactedLog(tmp / "c.csv", ledger=False)
        again.ledger = None
        self.assertIn("email", again.reason(person("R. Roe", "PM", email="RILEY@examplecorp.test")))


class Selection(unittest.TestCase):
    def test_skips_wrong_titles_and_ranks_relevant_first(self):
        people = [person("Avery Smith", "Software Development Engineer"), person("Casey Jones", "Technical Recruiter"),
                  person("Jamie Doe", "Brand Manager"), person("Riley Roe", "Senior Product Marketing Manager")]
        chosen, skipped = outreach.select_people(people, PROFILE, "Product Management Intern", 3, "", [], FakeContacted())
        # Jamie's only true hook is the one Riley already got at the same company, so Jamie is skipped.
        self.assertEqual([p["name"] for p in chosen], ["Riley Roe"])
        self.assertEqual(len(skipped), 3)

    def test_two_people_at_one_company_never_get_the_same_hook(self):
        people = [person("Riley Roe", "Senior Product Marketing Manager"),
                  person("Jamie Doe", "Brand Manager"),
                  person("Sam Lee", "Brand Manager", company="Othercorp")]
        chosen, skipped = outreach.select_people(people, PROFILE, "Product Management Intern", 3, "", [], FakeContacted())
        by_co = {}
        for p in chosen:
            by_co.setdefault(p["company"], []).append(method.hook_name(p["_hook"]["item"]))
        for hooks in by_co.values():
            self.assertEqual(len(hooks), len(set(hooks)))
        self.assertIn("Sam Lee", [p["name"] for p in chosen])  # same hook is fine at a different company
        self.assertIn("Jamie Doe", [p["name"] for p, _ in skipped])

    def test_merge_by_name_fills_email(self):
        merged = outreach.merge_people([person("Jamie Doe", "Brand Manager", linkedin_url="https://linkedin.com/in/x")],
                                       [person("Jamie Doe", "Brand Mgr", email="j@examplecorp.test", source="careershift")])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["email"], "j@examplecorp.test")
        self.assertEqual(merged[0]["source"], "exa+careershift")


class Writing(unittest.TestCase):
    R = person("Jamie Doe", "Product Marketing Manager")

    def test_template_fallback_without_api_key(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("find.load_dotenv"):
            self.assertIsNone(outreach.make_claude())
        msg = outreach.write_message(PROFILE, self.R, "", None)
        self.assertEqual(msg["writer"], "template")
        self.assertEqual(method.check_message(msg["subject"], msg["body"], "Jamie", "Alex"), [])

    def test_claude_clause_used_when_valid(self):
        claude = FakeClaude("you're using AI to market products at a huge scale")
        msg = outreach.write_message(PROFILE, self.R, "", claude)
        self.assertEqual(msg["writer"], "claude")
        self.assertIn("you're using AI to market products at a huge scale", msg["body"])
        self.assertEqual(claude.calls[0]["model"], "claude-sonnet-5-5")

    def test_bad_claude_clause_falls_back(self):
        for bad in ("you're a marketer — a great one", "I love that you market things",
                    "you went from sales to marketing", "you lead a team of 40 marketers"):
            msg = outreach.write_message(PROFILE, self.R, "", FakeClaude(bad))
            self.assertEqual(msg["writer"], "template", bad)

    def test_claude_error_falls_back(self):
        claude = mock.Mock()
        claude.messages.create.side_effect = RuntimeError("boom")
        self.assertEqual(outreach.write_message(PROFILE, self.R, "", claude)["writer"], "template")

    def test_sender_facts_only_from_profile(self):
        msg = outreach.write_message(PROFILE, self.R, "", None)
        self.assertIn(PROFILE["clubs"][0]["what_i_do"], msg["body"])
        self.assertIn(PROFILE["clubs"][0]["advice_on"], msg["body"])


class Gmail(unittest.TestCase):
    def test_creates_drafts_and_never_sends(self):
        import gmail_drafts
        service = mock.MagicMock()
        service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {"id": "d1"}
        drafts = [{"email": "jamie@examplecorp.test", "subject": "Hello", "body": "Hi Jamie,\n\nBody.\n\nThanks,\nAlex"}]
        outreach.save_gmail(drafts, service)
        create = service.users.return_value.drafts.return_value.create
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["userId"], "me")
        self.assertIn("raw", create.call_args.kwargs["body"]["message"])
        self.assertEqual(drafts[0]["gmail_draft_id"], "d1")
        service.users.return_value.messages.return_value.send.assert_not_called()
        service.users.return_value.drafts.return_value.send.assert_not_called()
        self.assertEqual(gmail_drafts.SCOPES, ["https://www.googleapis.com/auth/gmail.compose"])

    def test_no_send_code_path_exists(self):
        for name in ("gmail_drafts.py", "outreach.py", "method.py"):
            src = (HERE / name).read_text()
            self.assertIsNone(re.search(r"\.send\s*\(", src), name)
            self.assertNotIn("gmail.send", src, name)


class FullRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.profile = self.tmp / "profile.yaml"
        import yaml
        self.profile.write_text(yaml.safe_dump(PROFILE))

    def _run(self, **kw):
        exa = lambda company, school, n, city: [person("Riley Roe", "Senior Product Marketing Manager",
                                                       context="Leads product marketing.", linkedin_url="https://www.linkedin.com/in/riley-roe-example")]
        # Jamie's title matches a different true hook than Riley's, since one company never gets one pitch twice.
        cs = FakeCareerShift([person("Jamie Doe", "Equity Research Analyst", source="careershift", careershift_id="c1", has_email=True),
                              person("Casey Jones", "Recruiter", source="careershift", careershift_id="c2", has_email=True)])
        log = FakeContacted()
        service = kw.pop("service", None)
        kw.setdefault("industry", "finance")
        a = args(profile=str(self.profile), **kw)
        drafts, skipped = outreach.run(a, exa_fn=exa, careershift=cs, claude=None, gmail_service=service,
                                       dnc_entries=[], contacted=log, out_dir=self.tmp / "out")
        return drafts, skipped, cs, log

    def test_no_reveal_without_flag(self):
        with mock.patch.object(outreach, "make_claude", return_value=None), \
                mock.patch("gmail_drafts.is_configured", return_value=False):
            drafts, skipped, cs, log = self._run()
        self.assertEqual(cs.revealed, [])
        self.assertEqual({d["name"] for d in drafts}, {"Riley Roe", "Jamie Doe"})
        self.assertEqual([p["name"] for p, _ in skipped], ["Casey Jones"])
        self.assertEqual(len(log.rows), 2)

    def test_reveal_budget_respected(self):
        with mock.patch.object(outreach, "make_claude", return_value=None), \
                mock.patch("gmail_drafts.is_configured", return_value=False):
            drafts, _, cs, _ = self._run(reveal=1)
        self.assertEqual(cs.revealed, ["Jamie Doe"])
        self.assertEqual([d["email"] for d in drafts if d["name"] == "Jamie Doe"], ["jamie@examplecorp.test"])

    def test_local_files_written_and_csv_safe(self):
        with mock.patch.object(outreach, "make_claude", return_value=None), \
                mock.patch("gmail_drafts.is_configured", return_value=False):
            drafts, _, _, _ = self._run()
        files = sorted(p.name for p in (self.tmp / "out").iterdir())
        self.assertTrue(any(f.startswith("drafts_examplecorp_") and f.endswith(".md") for f in files))
        csv_path = next((self.tmp / "out").glob("*.csv"))
        with csv_path.open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)
        self.assertEqual(outreach.csv_safe("=HYPERLINK(1)"), "'=HYPERLINK(1)")

    def test_gmail_service_gets_drafts(self):
        service = mock.MagicMock()
        service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {"id": "d9"}
        with mock.patch.object(outreach, "make_claude", return_value=None):
            drafts, _, _, _ = self._run(service=service)
        self.assertEqual(service.users.return_value.drafts.return_value.create.call_count, 2)
        self.assertTrue(all(d["gmail_draft_id"] == "d9" for d in drafts))

    def test_review_table(self):
        with mock.patch.object(outreach, "make_claude", return_value=None), \
                mock.patch("gmail_drafts.is_configured", return_value=False):
            drafts, _, _, _ = self._run()
        table = outreach.review_table(drafts)
        for col in ("Name", "Title", "Email", "Hook"):
            self.assertIn(col, table)
        self.assertIn("Thanks,\nAlex", table)


class Privacy(unittest.TestCase):
    def test_personal_files_are_gitignored(self):
        ignore = (HERE / ".gitignore").read_text()
        for entry in ("profile.yaml", "data/do_not_contact.csv", "data/contacted.csv", "out/", "gmail_token.json",
                      "gmail_credentials.json"):
            self.assertIn(entry, ignore)


if __name__ == "__main__":
    unittest.main()
