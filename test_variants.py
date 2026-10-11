#!/usr/bin/env python3
"""Tests for the template hierarchy, verified facts, send pacing and the similarity warning.
Run with: python3 -m unittest test_variants"""
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import bridge
import dnc
import drafts
import find
import sender_profile
import variants
from ledger import Ledger

KW = dict(recipient_first_name="Jane", org=None, firm="Bain & Company", sender_first_name="Alex",
          sender_school="USC", sender_year="sophomore", sender_industry="consulting",
          sender_signature="Alex Example, President, Example Investing Club")
SCHOOL = "University of Southern California"
SIG = KW["sender_signature"]
NO_DNC = []

# Tests run against the placeholder profile, never a real profile.json.
variants.PROFILE = sender_profile.load_profile(sender_profile.EXAMPLE_PATH)


def job(title, company, frm, to=None, cid="123"):
    c = {"name": company}
    if cid:
        c["id"] = cid
    return {"title": title, "company": c, "dates": {"from": frm, "to": to}}


def render(facts):
    choice = variants.select_template(facts)
    subject, body = drafts.render_draft(tier=3, target_school=SCHOOL, facts=facts, **KW)
    return choice, subject, body


class HierarchyTests(unittest.TestCase):
    FULL = {"shared_org": "Sample Consulting Group", "shared_org_source": "s1", "school_degree": "BA Economics",
            "school_degree_source": "s2", "prior_employer": "Deloitte", "prior_employer_source": "s3",
            "firm_start_year": "2026", "firm_start_source": "s4"}

    def pick(self, **drop):
        f = {k: v for k, v in self.FULL.items() if k not in drop and not any(k.startswith(d) for d in drop)}
        return variants.select_template(f)["id"]

    FULL2 = dict(HierarchyTests.FULL if False else {}, high_school="yes", high_school_source="s0", home_city="yes", home_city_source="s2b")

    def pick2(self, facts, **drop):
        f = {k: v for k, v in facts.items() if k not in drop and not any(k.startswith(d) for d in drop)}
        return variants.select_template(f)["id"]

    def test_high_school_and_home_city_slot_into_the_ranking(self):
        full = dict(self.FULL, high_school="yes", high_school_source="s0", home_city="yes", home_city_source="s2b")
        self.assertEqual(self.pick2(full), "shared_high_school")
        self.assertEqual(self.pick2(full, high_school=1), "shared_org")
        self.assertEqual(self.pick2(full, high_school=1, shared_org=1), "shared_major")
        self.assertEqual(self.pick2(full, high_school=1, shared_org=1, school_degree=1), "home_city")
        self.assertEqual(self.pick2(full, high_school=1, shared_org=1, school_degree=1, home_city=1), "prior_employer")

    def test_first_match_wins_down_the_whole_order(self):
        self.assertEqual(self.pick(), "shared_org")
        self.assertEqual(self.pick(shared_org=1), "shared_major")
        self.assertEqual(self.pick(shared_org=1, school_degree=1), "prior_employer")
        self.assertEqual(self.pick(shared_org=1, school_degree=1, prior_employer=1), "recently_joined")

    def test_start_year_boundaries(self):
        base = {"firm_start_year": ""}
        for year, want in (("2026", "recently_joined"), ("2025", "recently_joined"), ("2024", "school_fallback"),
                           ("2023", "longer_tenure"), ("2019", "longer_tenure"), ("", "school_fallback")):
            self.assertEqual(variants.select_template({"firm_start_year": year})["id"], want, year)

    def test_a_non_ir_degree_does_not_claim_the_major(self):
        self.assertEqual(variants.select_template({"school_degree": "BS Business Administration"})["id"], "school_fallback")
        self.assertEqual(variants.select_template({"school_degree": "MA economics"})["id"], "shared_major")

    def test_the_choice_records_its_fact_and_source(self):
        c = variants.select_template(self.FULL)
        self.assertEqual((c["id"], c["source"]), ("shared_org", "s1"))
        self.assertIn("Sample Consulting Group", c["fact"])

    def test_only_the_profile_orgs(self):
        names = {o["name"] for o in variants.load_sender_orgs()}
        self.assertEqual(names, {"Example Investing Club", "Sample Consulting Group"})
        for o in variants.load_sender_orgs():
            for m in o["match"]:
                self.assertGreater(len(m), 4, f"{m!r}: a bare acronym is never a match variant")

    def test_an_empty_profile_never_offers_the_profile_templates(self):
        saved = variants.PROFILE
        try:
            variants.PROFILE = {}
            facts = {"high_school": "yes", "home_city": "yes", "school_degree": "BA Economics"}
            self.assertEqual(variants.select_template(facts)["id"], "school_fallback")
            self.assertEqual(variants.load_sender_orgs(), [])
        finally:
            variants.PROFILE = saved


class TemplateCopyTests(unittest.TestCase):
    CASES = {
        "shared_high_school": {"high_school": "yes"},
        "shared_org": {"shared_org": "Sample Consulting Group"},
        "shared_major": {"school_degree": "BA Economics"},
        "home_city": {"home_city": "yes"},
        "prior_employer": {"prior_employer": "Deloitte Consulting LLP"},
        "recently_joined": {"firm_start_year": "2026"},
        "longer_tenure": {"firm_start_year": "2023"},
        "school_fallback": {},
    }

    def test_all_six_render_valid_and_share_the_ask(self):
        for tid, facts in self.CASES.items():
            choice, subject, body = render(facts)
            self.assertEqual(choice["id"], tid)
            self.assertEqual(drafts.validate_draft(subject, body, allowed_numbers=variants.template_numbers(choice)), [], tid)
            self.assertIn("Would you have 15 minutes for a call in the next two weeks?", body, tid)
            self.assertTrue(body.endswith("\n\n" + SIG), "the signature stands alone")
            self.assertEqual(body.count("Alex Example"), 1)
            self.assertNotIn("Bain & Company", body + subject, "short firm name in prose")

    def test_banned_copy_never_appears(self):
        for tid, facts in self.CASES.items():
            _, subject, body = render(facts)
            low = (subject + " " + body).lower()
            for bad in ("day to day", "your experience", "how people end up", "a few minutes", "quick call",
                        "recruiting for consulting soon", "i recruited for", "not the usual", "bigger jump", "know least"):
                self.assertNotIn(bad, low, f"{tid}: {bad}")
            self.assertIn("preparing for consulting recruiting", low) if tid != "recently_joined" and tid != "shared_major" else None

    def test_the_specified_wording(self):
        _, s, b = render({"shared_org": "Sample Consulting Group"})
        self.assertEqual(s, "Sample Consulting Group to Bain")
        self.assertIn("I saw that you were part of Sample Consulting Group at USC before joining Bain. I'm also in Sample Consulting Group and preparing for consulting recruiting, so I'd love to hear how you used that experience during the process.", b)
        _, s, b = render({"school_degree": "Economics"})
        self.assertEqual(s, "Economics to Bain")
        self.assertIn("I'm studying Economics (Business track), and I'd love to hear how you positioned that background during recruiting.", b)
        _, s, b = render({"prior_employer": "Deloitte Consulting LLP"})
        self.assertEqual(s, "Your path to Bain")
        self.assertIn("worked at Deloitte before joining Bain.", b)
        _, s, b = render({"firm_start_year": "2025"})
        self.assertEqual(s, "Question from a USC sophomore")
        _, s, b = render({"firm_start_year": "2022"})
        self.assertEqual(s, "USC to Bain")
        self.assertIn("have now been there since 2022.", b)
        _, s, b = render({"high_school": "yes"})
        self.assertEqual(s, "Lincoln to Bain")
        self.assertIn("I also went to Lincoln", b)
        _, s, b = render({"home_city": "yes"})
        self.assertEqual(s, "Chicago to Bain")
        self.assertIn("based in Chicago", b)
        _, s, b = render({})
        self.assertEqual(s, "USC sophomore interested in Bain")

    def test_at_most_five_sentences_and_one_question(self):
        for facts in self.CASES.values():
            _, _, body = render(facts)
            self.assertLessEqual(drafts.count_sentences(body), 5)
            self.assertEqual(body.count("?"), 1)

    def test_followup_has_no_signature_and_reuses_the_ask(self):
        _, body = drafts.render_followup("Jane", "Bain & Company", "Alex", "x")
        self.assertNotIn("President", body)
        self.assertIn("15 minutes", body)


class VerifiedEmployerTests(unittest.TestCase):
    def prior(self, prior_job, name="Jane Doe"):
        props = {"name": name, "workHistory": [job("Consultant", "Bain & Company", "2023-06"), prior_job]}
        stint = find.firm_stint_start(props, "Bain & Company", {})
        return find.verified_prior_employer(props, "Bain & Company", {}, stint)[0]

    def test_a_real_employer_is_verified(self):
        self.assertEqual(self.prior(job("Analyst", "Deloitte", "2021-06", "2023-05")), "Deloitte")

    def test_unverified_employers_fall_through(self):
        cases = {
            "own venture (named after them)": job("Owner", "Doe Virtual Tours", "2021-06", "2023-05"),
            "founder title": job("Founder", "Northwind Labs", "2021-06", "2023-05"),
            "president title": job("President", "Northwind Labs", "2021-06", "2023-05"),
            "student club": job("Analyst", "USC Example Investing Club", "2021-06", "2023-05"),
            "no company page id": job("Analyst", "Deloitte", "2021-06", "2023-05", cid=None),
            "under a year": job("Analyst", "Deloitte", "2023-01", "2023-05"),
            "internship": job("Summer Analyst", "Deloitte", "2021-06", "2021-09"),
            "a school": job("RA", "University of Southern California", "2020-06", "2022-06"),
            "still there": job("Analyst", "Deloitte", "2021-06"),
        }
        for label, j in cases.items():
            self.assertEqual(self.prior(j), "", label)

    def test_vice_president_is_a_real_job(self):
        self.assertEqual(self.prior(job("Vice President", "Goldman Sachs", "2020-06", "2023-05")), "Goldman Sachs")

    def test_a_digit_bearing_company_name_falls_through(self):
        self.assertEqual(self.prior(job("Analyst", "180 Degrees Consulting", "2021-06", "2023-05")), "",
                         "a real digit in the name would always trip the no-digits content rule")

    def test_source_names_the_field(self):
        props = {"name": "Jane Doe", "workHistory": [job("Consultant", "Bain & Company", "2023-06"), job("Analyst", "Deloitte", "2021-06", "2023-05", cid="99")]}
        stint = find.firm_stint_start(props, "Bain & Company", {})
        src = find.verified_prior_employer(props, "Bain & Company", {}, stint)[1]
        self.assertIn("workHistory", src)
        self.assertIn("company id 99", src)


class SharedOrgTests(unittest.TestCase):
    def org(self, work=(), text="profile text"):
        return find.sender_org_match(text, {"workHistory": list(work)})[0]

    def test_matches_only_the_profile_orgs(self):
        self.assertEqual(self.org(work=[job("Member", "Sample Consulting Group at USC", "2022")]), "Sample Consulting Group")
        self.assertEqual(self.org(text="## Organizations\n### Member at Example Investing Club Aug 2023 - Present\n"), "Example Investing Club")
        self.assertEqual(self.org(work=[job("Member", "Other Consulting Group", "2022")]), "")
        self.assertEqual(self.org(work=[job("Member", "180 Degrees Consulting", "2022")]), "")
        self.assertEqual(self.org(work=[job("Member", "EIC", "2022")]), "", "a bare acronym never counts")


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "l.json")

    def tearDown(self):
        self.tmp.cleanup()

    def queue(self, n, domain="bain.com"):
        return [{"email": f"p{i}@{domain}", "name": f"Person{i}"} for i in range(n)]

    MON = date(2026, 9, 21)

    def test_thirteen_to_one_domain_go_four_a_week_one_a_day_and_none_are_dropped(self):
        q = self.queue(13)
        deferred = bridge.pace_by_domain(q, self.ledger, 4, self.MON)
        dates = [date.fromisoformat(r["not_before"]) for r in q]
        self.assertEqual(len(q), 13)
        self.assertEqual(len(set(dates)), 13, "at most one a day")
        self.assertTrue(all(d.weekday() < 5 for d in dates))
        weeks = {}
        for d in dates:
            weeks[d - timedelta(days=d.weekday())] = weeks.get(d - timedelta(days=d.weekday()), 0) + 1
        self.assertEqual(sorted(weeks.values(), reverse=True), [4, 4, 4, 1])
        self.assertEqual(deferred, 9)

    def test_the_limit_is_configurable(self):
        q = self.queue(6)
        bridge.pace_by_domain(q, self.ledger, 2, self.MON)
        first_week = [r for r in q if date.fromisoformat(r["not_before"]) < self.MON + timedelta(days=7)]
        self.assertEqual(len(first_week), 2)

    def test_domains_are_paced_independently(self):
        q = self.queue(3, "bain.com") + self.queue(3, "bcg.com")
        bridge.pace_by_domain(q, self.ledger, 4, self.MON)
        self.assertEqual(sorted(r["not_before"] for r in q)[:2], [self.MON.isoformat()] * 2)

    def test_earlier_runs_count(self):
        for i in range(4):
            self.ledger.add({"email": f"old{i}@bain.com", "name": f"Old{i} Person", "firm_queried": "Bain"}, "queued",
                            not_before=(self.MON + timedelta(days=i)).isoformat())
        q = self.queue(1)
        bridge.pace_by_domain(q, self.ledger, 4, self.MON)
        self.assertGreaterEqual(date.fromisoformat(q[0]["not_before"]), self.MON + timedelta(days=7))

    def test_a_weekend_start_moves_to_monday(self):
        q = self.queue(1)
        bridge.pace_by_domain(q, self.ledger, 4, date(2026, 9, 20))
        self.assertEqual(q[0]["not_before"], "2026-09-21")


class SimilarityWarningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "l.json")

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, n):
        out = []
        for i in range(n):
            _, s, b = render({})
            out.append({"name": f"Person{chr(65 + i)} Test", "email": f"p{i}@bain.com", "linkedin_url": f"https://linkedin.com/in/p{i}",
                        "firm_queried": "Bain & Company", "seniority_band": "in_band", "email_status": "valid",
                        "draft_subject": s, "draft_body": b.replace("Hi Jane", f"Hi Person{chr(65 + i)}")})
        return out

    def test_identical_drafts_warn_but_are_not_rejected(self):
        queue, stats, _ = bridge.select_sendable(self.rows(6), self.ledger, "Alex", dnc_entries=NO_DNC)
        self.assertEqual(len(queue), 6, "the guard warns, it does not reject")
        self.assertEqual(stats["similar_drafts_warning"], 5)
        self.assertGreaterEqual(stats["max_similarity"], 0.99)

    def test_pacing_is_what_limits_a_batch(self):
        queue, stats, _ = bridge.select_sendable(self.rows(6), self.ledger, "Alex", dnc_entries=NO_DNC, start=date(2026, 9, 21))
        self.assertEqual(stats["paced_to_later_weeks"], 2)
        self.assertEqual(len({r["not_before"] for r in queue}), 6)

    def test_the_recipients_start_year_is_allowed_but_no_other_number(self):
        _, s, b = render({"firm_start_year": "2022"})
        row = {"name": "Jane Doe", "email": "j@bain.com", "linkedin_url": "https://linkedin.com/in/j", "firm_queried": "Bain & Company",
               "seniority_band": "in_band", "email_status": "valid", "draft_subject": s, "draft_body": b, "firm_start_year": "2022"}
        queue, stats, _ = bridge.select_sendable([row], self.ledger, "Alex", dnc_entries=NO_DNC)
        self.assertEqual(len(queue), 1)
        row2 = dict(row, linkedin_url="https://linkedin.com/in/k", email="k@bain.com", name="Kim Doe",
                    draft_body=b.replace("2022", "2021"))
        queue, stats, _ = bridge.select_sendable([row2], self.ledger, "Alex", dnc_entries=NO_DNC)
        self.assertEqual((len(queue), stats["failed_validation"]), (0, 1))


class SeniorityBandTests(unittest.TestCase):
    TF = {}

    def band(self, title, start="2024-01"):
        return find.compute_seniority_band(title, {"workHistory": [job(title, "Bain", start)]}, 2.0, 5.0, start, [], self.TF)[0]

    def test_manager_level_titles_are_above(self):
        for title in ("Manager", "Senior Manager", "Engagement Manager", "Associate Director", "Director",
                      "Principal", "Partner", "Managing Director"):
            self.assertEqual(self.band(title), "above", title)

    def test_entry_and_early_titles_are_not_caught(self):
        for title in ("Associate Consultant", "Senior Associate Consultant", "Consultant", "Analyst", "Associate"):
            self.assertNotEqual(self.band(title, "2022-06"), "above", title)

    def test_stale_csv_rows_are_reapplied(self):
        old = {"title": "Senior Manager", "seniority_band": "in_band", "evidence": "x"}
        self.assertTrue(find.reapply_title_band(old))
        self.assertEqual((old["seniority_band"], old["seniority_flag"]), ("above", "SENIOR"))
        keep = {"title": "Associate Consultant", "seniority_band": "in_band", "evidence": "x"}
        self.assertFalse(find.reapply_title_band(keep))
        self.assertEqual(keep["seniority_band"], "in_band")
        below = {"title": "Manager", "seniority_band": "below", "evidence": "x"}
        self.assertFalse(find.reapply_title_band(below), "never promotes or rewrites other bands")


class ExtractFactsTests(unittest.TestCase):
    CFG = {}

    def facts(self, work, edu=None, orgs=None, text=""):
        props = {"name": "Jane Doe", "workHistory": work, "educationHistory": edu or [
            {"institution": {"name": SCHOOL}, "degree": "BA Economics", "dates": {"from": "2018", "to": "2022"}}]}
        return find.extract_facts(props, SCHOOL, "Bain & Company", self.CFG, orgs, text)

    def test_basic(self):
        f = self.facts([job("Consultant", "Bain & Company", "2023-06"), job("Analyst", "Deloitte Consulting LLP", "2021-06", "2023-05")])
        self.assertEqual((f["firm_start_year"], f["school_end_year"], f["prior_employer"]), (2023, 2022, "Deloitte Consulting LLP"))
        self.assertIn("Economics", f["school_degree"])
        self.assertIn("educationHistory", f["school_degree_source"])

    def test_a_promotion_does_not_reset_the_start_year(self):
        f = self.facts([job("Consultant", "Bain & Company", "2025-01"), job("Associate Consultant", "Bain & Company", "2023-06", "2025-01")])
        self.assertEqual(f["firm_start_year"], 2023)

    def test_an_internship_at_the_firm_does_not_count_as_joining(self):
        f = self.facts([job("Associate Consultant", "Bain & Company", "2023-06"), job("Summer Intern", "Bain & Company", "2021-06", "2021-08")])
        self.assertEqual(f["firm_start_year"], 2023)

    def test_picks_the_most_recent_prior_employer(self):
        f = self.facts([job("Consultant", "Bain & Company", "2023-06"), job("Analyst", "Deloitte", "2021-06", "2023-05"),
                        job("Associate", "McKinsey & Company", "2019-06", "2021-05")])
        self.assertEqual(f["prior_employer"], "Deloitte")

    def test_no_current_stint_means_no_facts(self):
        f = self.facts([job("Consultant", "Bain & Company", "2019-06", "2022-06")])
        self.assertEqual((f["firm_start_year"], f["prior_employer"]), ("", ""))

    def test_missing_school_dates_leave_the_year_blank(self):
        f = self.facts([job("Consultant", "Bain & Company", "2023-06")],
                       edu=[{"institution": {"name": SCHOOL}, "degree": "BA"}])
        self.assertEqual(f["school_end_year"], "")

    def test_shared_high_school_is_detected_from_any_education_entry(self):
        edu = [{"institution": {"name": SCHOOL}, "degree": "BA Economics", "dates": {"to": "2022"}},
               {"institution": {"name": "Lincoln High School"}, "dates": {"to": "2018"}}]
        f = self.facts([job("Consultant", "Bain & Company", "2023-06")], edu=edu)
        self.assertEqual(f["high_school"], "yes")
        self.assertIn("educationHistory", f["high_school_source"])

    def test_no_high_school_entry_leaves_it_blank(self):
        f = self.facts([job("Consultant", "Bain & Company", "2023-06")])
        self.assertEqual(f["high_school"], "")

    def test_home_city_location_is_detected(self):
        props = {"name": "Jane Doe", "workHistory": [job("Consultant", "Bain & Company", "2023-06")],
                 "educationHistory": [{"institution": {"name": SCHOOL}, "dates": {"to": "2022"}}],
                 "location": "Chicago, Illinois, United States"}
        f = find.extract_facts(props, SCHOOL, "Bain & Company", self.CFG)
        self.assertEqual(f["home_city"], "yes")
        self.assertIn("Chicago", f["home_city_source"])

    def test_a_different_city_does_not_claim_home_city(self):
        props = {"name": "Jane Doe", "workHistory": [job("Consultant", "Bain & Company", "2023-06")],
                 "educationHistory": [{"institution": {"name": SCHOOL}, "dates": {"to": "2022"}}],
                 "location": "New York, New York, United States"}
        f = find.extract_facts(props, SCHOOL, "Bain & Company", self.CFG)
        self.assertEqual(f["home_city"], "")

    def test_the_before_joining_order_is_checked(self):
        row = {"name": "Jane Doe", "tier": "3", "firm_queried": "Bain & Company", "firm_start_year": "2022",
               "school_end_year": "2024", "evidence": ""}
        class A: sender_school, sender_year, sender_industry = "USC", "sophomore", "consulting"
        with self.assertRaises(drafts.DraftContentError):
            find.render_row_draft(row, [], SCHOOL, "Alex", A)


class EnrichFactsTests(unittest.TestCase):
    def test_matches_by_url_falls_back_to_name_records_cost_and_refreshes_old_rows(self):
        rows = [
            {"name": "Jane Doe", "title": "Consultant", "firm_queried": "Bain & Company", "linkedin_url": "https://www.linkedin.com/in/jane-doe/", "firm_match": "yes", "facts_checked": "yes"},
            {"name": "John Roe", "title": "Consultant", "firm_queried": "Bain & Company", "linkedin_url": "https://linkedin.com/in/stale-url", "firm_match": "yes"},
            {"name": "No Match", "title": "Consultant", "firm_queried": "Bain & Company", "linkedin_url": "https://linkedin.com/in/none", "firm_match": "yes"},
            {"name": "Skip Me", "title": "Consultant", "firm_queried": "Bain & Company", "linkedin_url": "", "firm_match": "yes", "facts_checked": find.FACTS_VERSION},
        ]
        def person(name, start):
            return {"entities": [{"type": "person", "properties": {"name": name, "workHistory": [job("Consultant", "Bain & Company", start)],
                    "educationHistory": [{"institution": {"name": SCHOOL}, "degree": "BA Economics", "dates": {"to": "2022"}}]}}]}
        calls = []
        def fake(api_key, query, n):
            calls.append(query)
            if "Jane" in query:
                return {"costDollars": {"total": 0.007}, "results": [dict(person("Jane Doe", "2023-06"), url="https://linkedin.com/in/jane-doe")]}
            if "John" in query:
                return {"costDollars": {"total": 0.007}, "results": [dict(person("John Roe", "2024-02"), url="https://linkedin.com/in/new-url")]}
            return {"costDollars": {"total": 0.007}, "results": []}
        original = find.exa_search
        find.exa_search = fake
        try:
            cost, found, missed = find.enrich_facts(rows, SCHOOL, {}, [], "key")
        finally:
            find.exa_search = original
        self.assertEqual((found, missed, len(calls)), (2, 1, 3))
        self.assertAlmostEqual(cost, 0.021)
        self.assertEqual((rows[0]["firm_start_year"], rows[1]["firm_start_year"]), (2023, 2024))
        self.assertIn("Economics", rows[0]["school_degree"])
        self.assertEqual(rows[2]["facts_checked"], "no_match")


if __name__ == "__main__":
    unittest.main()
