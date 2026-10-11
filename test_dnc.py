#!/usr/bin/env python3
"""Tests for the Do Not Contact list. Run with: python3 -m unittest test_dnc"""
import json
import tempfile
import unittest
from pathlib import Path

import bridge
import dnc
from ledger import Ledger

R, C, G, P = "never_relationship", "never_colleague", "gated", "never_partner"


def entries(tmp):
    path = Path(tmp) / "dnc.json"
    dnc.add("Edward Sample", "Bain & Company", R, "referral", emails=["Edward.Sample@example.com"],
            linkedin=["https://www.linkedin.com/in/edward-sample-1/"], path=path)
    dnc.add("Matthew Example", "McKinsey & Company", R, path=path)
    dnc.add("Emily", "Northwind Advisory", C, path=path)
    dnc.add("Northwind Advisory", "", C, org=True, path=path)
    dnc.add("Scott Placeholder", "", G, path=path)
    return path, dnc.load(path)


def row(name, firm="Acme", email="", li=""):
    return {"name": name, "firm_queried": firm, "email": email, "linkedin_url": li}


class DNCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path, self.entries = entries(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def hits(self, r):
        return dnc.check(r, self.entries)

    def test_any_single_key_excludes(self):
        self.assertEqual(self.hits(row("Someone Else", email="edward.sample@EXAMPLE.com"))[0]["kind"], "email")
        self.assertEqual(self.hits(row("Someone Else", li="http://linkedin.com/in/Edward-Sample-1?x=1"))[0]["kind"], "LinkedIn URL")
        self.assertEqual(self.hits(row("Edward Sample", "Bain & Company"))[0]["kind"], "name + firm")

    def test_ambiguous_matches_exclude_and_flag_for_review(self):
        h = self.hits(row("Edward Sample", "Some Other Firm"))
        self.assertTrue(h and h[0]["review"], "same name, different firm: excluded and flagged")
        h = self.hits(row("Ed Sample", "Bain"))
        self.assertTrue(h, "a nickname is still the same person")
        h = self.hits(row("E. Sample", "Bain"))
        self.assertTrue(h and h[0]["review"])
        h = self.hits(row("Matt Example", "McKinsey"))
        self.assertTrue(h)
        self.assertTrue(self.hits(row("Scott Placeholder", "Anywhere"))[0]["review"], "name-only entry, any firm")

    def test_first_name_entries_need_the_firm(self):
        self.assertTrue(self.hits(row("Emily Doe", "Northwind Advisory")))
        self.assertFalse(self.hits(row("Emily Doe", "Bain & Company")), "another Emily at another firm is someone else")

    def test_organization_entries_match_anyone_there(self):
        self.assertEqual(self.hits(row("Nobody Known", "Northwind Advisory LLC"))[0]["kind"], "organization")
        self.assertFalse(self.hits(row("Nobody Known", "Bain & Company")))

    def test_an_unrelated_person_is_clear(self):
        self.assertFalse(self.hits(row("Jane Doe", "Bain & Company", "jane.doe@bain.com", "https://linkedin.com/in/jane-doe")))
        self.assertFalse(self.hits(row("Edward Example", "Bain & Company")))

    def test_filter_rows_logs_and_reports(self):
        log = Path(self.tmp.name) / "log"
        rows = [row("Edward Sample", "Bain & Company"), row("Jane Doe", "Bain & Company")]
        clear, excluded = [], []
        for r in rows:
            h = dnc.check(r, self.entries)
            (excluded if h else clear).append((r, h) if h else r)
            if h:
                dnc.log_exclusion(r, h, "test", path=log)
        self.assertEqual((len(clear), len(excluded)), (1, 1))
        rec = json.loads(log.read_text().splitlines()[0])
        self.assertEqual((rec["name"], rec["tier"]), ("Edward Sample", R))
        self.assertIn("never_relationship", rec["reason"])
        self.assertIn("Edward Sample", "\n".join(dnc.summarize(excluded)))

    def test_add_never_removes_or_downgrades(self):
        n = len(self.entries)
        entry, created = dnc.add("Edward Sample", "Bain & Company", G, "x", emails=["new@bain.com"], path=self.path)
        self.assertFalse(created)
        self.assertEqual(entry["tier"], R, "tier is not changed by a re-add")
        self.assertIn("new@bain.com", entry["emails"])
        self.assertEqual(len(dnc.load(self.path)), n)
        self.assertFalse(hasattr(dnc, "remove"), "removal is manual only")

    def test_a_missing_or_broken_list_fails_closed(self):
        with self.assertRaises(dnc.DNCError):
            dnc.load(Path(self.tmp.name) / "nope.json")
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text("{not json")
        with self.assertRaises(dnc.DNCError):
            dnc.load(bad)

    def test_the_example_list_loads_and_catches_its_entry(self):
        example = dnc.load(Path(__file__).resolve().parent / "dnc.example.json")
        self.assertTrue(dnc.check(row("Jane Doe", "Example Corp"), example))
        self.assertTrue(dnc.check(row("Someone", "Elsewhere", email="jane.doe@example.com"), example))
        self.assertFalse(dnc.check(row("John Roe", "Example Corp"), example))


class BridgeDNCTests(unittest.TestCase):
    def setUp(self):
        self.log_tmp = tempfile.TemporaryDirectory()
        self.original_log = dnc.LOG_PATH
        dnc.LOG_PATH = Path(self.log_tmp.name) / "dnc-exclusions.log"

    def tearDown(self):
        dnc.LOG_PATH = self.original_log
        self.log_tmp.cleanup()

    def test_the_bridge_excludes_a_dnc_person_even_from_an_old_results_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, ents = entries(tmp)
            good = {"name": "Jane Doe", "firm_queried": "Bain & Company", "email": "jane.doe@bain.com", "linkedin_url": "",
                    "seniority_band": "in_band", "email_status": "valid",
                    "draft_subject": "USC to Bain", "draft_body": "Hi Jane,\n\nI saw you.\n\nWould you have 15 minutes for a call in the next two weeks?\n\nAlex"}
            bad = dict(good, name="Edward Sample", email="edward.sample@example.com")
            queue, stats, _ = bridge.select_sendable([bad, good], Ledger(Path(tmp) / "l.json"), "Alex", dnc_entries=ents)
            self.assertEqual([q["name"] for q in queue], ["Jane Doe"])
            self.assertEqual(stats["dnc_excluded"], 1)


if __name__ == "__main__":
    unittest.main()
