#!/usr/bin/env python3
"""Tests for the paste-a-list mode: parsing, classification, and the check_firm 'most recent open
entry' fix that the Paul Placeholder bug exposed. Run with: python3 -m unittest test_alumni_paste"""
import unittest

import alumni_paste
import find

FIRM = "Boston Consulting Group"
CFG = {}
TITLE_FILTERS = find.load_title_filters()


def classify(headline, name="Jane Doe", url="linkedin.com/in/jane-doe"):
    e = {"name": name, "linkedin_url": url, "headline": headline}
    return alumni_paste.classify(e, FIRM, CFG, TITLE_FILTERS, find.company_matches_firm,
                                 find.is_non_target_role, find.seniority_flag, find.is_different_track)


BLOCK_TEXT = """Mia Sample
Consultant at Boston Consulting Group (BCG)
Los Angeles, California, United States
linkedin.com/in/mia-sample-123
3rd+
1 mutual connection

Someone Student
Industrial and Systems Engineering & Finance @ USC
linkedin.com/in/someone-student

Paul Placeholder
Principal at Boston Consulting Group (BCG)
linkedin.com/in/paul-placeholder
"""

LINE_TEXT = (
    "Mia Sample\tlinkedin.com/in/mia-sample-123\tConsultant at Boston Consulting Group (BCG)\n"
    "linkedin.com/in/paul-placeholder | Principal at Boston Consulting Group (BCG) | Paul Placeholder\n"
)


class ParseTests(unittest.TestCase):
    def test_block_form(self):
        entries, unparsed = alumni_paste.parse(BLOCK_TEXT)
        self.assertEqual(unparsed, 0)
        self.assertEqual(len(entries), 3)
        by_name = {e["name"]: e for e in entries}
        self.assertEqual(by_name["Mia Sample"]["linkedin_url"], "linkedin.com/in/mia-sample-123")
        self.assertIn("Consultant at Boston Consulting Group (BCG)", by_name["Mia Sample"]["headline"])
        self.assertNotIn("3rd+", by_name["Mia Sample"]["headline"])
        self.assertNotIn("mutual connection", by_name["Mia Sample"]["headline"])

    def test_block_form_url_sharing_a_line_with_other_text(self):
        text = "Jane Doe\nConsultant at Bain\nhttps://www.linkedin.com/in/jane-doe/ · 2nd\n"
        entries, _ = alumni_paste.parse(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "Jane Doe")
        self.assertEqual(entries[0]["headline"], "Consultant at Bain")

    def test_line_form_field_order_independent(self):
        entries, unparsed = alumni_paste.parse(LINE_TEXT)
        self.assertEqual(unparsed, 0)
        self.assertEqual(len(entries), 2)
        by_name = {e["name"]: e for e in entries}
        self.assertEqual(by_name["Paul Placeholder"]["linkedin_url"], "linkedin.com/in/paul-placeholder")
        self.assertIn("Principal", by_name["Paul Placeholder"]["headline"])

    def test_a_line_with_no_url_is_unparsed_not_dropped_silently(self):
        entries, unparsed = alumni_paste.parse("Jane Doe\tConsultant at Bain\n")
        self.assertEqual((len(entries), unparsed), (0, 1))

    def test_a_block_with_no_url_is_skipped(self):
        entries, _ = alumni_paste.parse("Jane Doe\nConsultant at Bain\n\nJohn Roe\nConsultant at Bain\nlinkedin.com/in/john-roe\n")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "John Roe")


class ClassifyTests(unittest.TestCase):
    def test_current_student_has_no_role_at_the_firm(self):
        for headline in ("Industrial and Systems Engineering & Finance @ USC", "Public Policy @ USC",
                         "Student at University of Southern California"):
            self.assertEqual(classify(headline)["reason"], "no_role", headline)

    def test_an_employed_consultant_survives(self):
        c = classify("Consultant at Boston Consulting Group (BCG)")
        self.assertEqual((c["reason"], c["title"]), ("ok", "Consultant"))

    def test_above_band_titles_are_excluded(self):
        for title in ("Principal", "Project Leader", "Managing Director", "Partner", "Associate Director",
                     "Senior Project Management"):
            self.assertEqual(classify(f"{title} at Boston Consulting Group (BCG)")["reason"], "above_band", title)

    def test_different_track_titles_are_flagged_not_dropped(self):
        for title in ("BCG X", "Forward Deployed AI Engineer", "Data Scientist", "Lead Data Scientist"):
            self.assertEqual(classify(f"{title} at Boston Consulting Group (BCG)")["reason"], "different_track", title)

    def test_support_titles_are_excluded(self):
        for title in ("Coordinator", "Parttime Assistant", "Administrative Assistant", "Receptionist"):
            self.assertEqual(classify(f"{title} at Boston Consulting Group (BCG)")["reason"], "support", title)

    def test_a_mention_of_a_different_firm_is_not_a_role_here(self):
        self.assertEqual(classify("Consultant at Bain & Company")["reason"], "no_role")

    def test_a_past_founder_mention_does_not_read_as_a_current_senior_title(self):
        c = classify("BCG | Ex-founder")
        self.assertEqual((c["reason"], c["title"]), ("ok", "BCG"))

    def test_former_and_previously_are_also_dropped(self):
        for headline in ("Consultant at BCG | Former Director", "Consultant at BCG | Previously Data Scientist"):
            self.assertEqual(classify(headline)["reason"], "ok", headline)

    def test_excluded_firm_names_are_respected(self):
        cfg = {"boston consulting group": {"exclude": ["BCG Digital Ventures"]}}
        e = {"name": "Jane Doe", "linkedin_url": "linkedin.com/in/jane-doe", "headline": "Product Manager at BCG Digital Ventures"}
        c = alumni_paste.classify(e, FIRM, cfg, TITLE_FILTERS, find.company_matches_firm,
                                  find.is_non_target_role, find.seniority_flag, find.is_different_track)
        self.assertEqual(c["reason"], "no_role")


class CheckFirmLatestEntryTests(unittest.TestCase):
    """The Paul Placeholder bug: two workHistory entries at the firm both left open (dates.to is null) — a stale
    earlier title must never outrank the person's actual current one."""

    def job(self, title, frm, to=None):
        return {"title": title, "company": {"name": "Boston Consulting Group"}, "dates": {"from": frm, "to": to}}

    def test_the_latest_started_open_entry_wins(self):
        props = {"workHistory": [self.job("Consultant", "2020-01"), self.job("Principal", "2024-01")]}
        firm_match, title, evidence, start = find.check_firm(props, "", FIRM, {}, {})
        self.assertEqual((firm_match, title, start), ("yes", "Principal", "2024-01"))
        self.assertIn("latest start", evidence)

    def test_order_in_the_list_does_not_matter(self):
        props = {"workHistory": [self.job("Principal", "2024-01"), self.job("Consultant", "2020-01")]}
        _, title, _, _ = find.check_firm(props, "", FIRM, {}, {})
        self.assertEqual(title, "Principal")

    def test_a_single_open_entry_is_unaffected(self):
        props = {"workHistory": [self.job("Consultant", "2023-06")]}
        firm_match, title, _, start = find.check_firm(props, "", FIRM, {}, {})
        self.assertEqual((firm_match, title, start), ("yes", "Consultant", "2023-06"))


class NoProfileUrlTests(unittest.TestCase):
    def test_an_anonymized_linkedin_member_is_excluded_first(self):
        e = {"name": "LinkedIn Member", "linkedin_url": "https://www.linkedin.com/company/bcg/people/?facetSchool=3084",
             "headline": "Consultant at Boston Consulting Group (BCG)"}
        c = alumni_paste.classify(e, FIRM, CFG, TITLE_FILTERS, find.company_matches_firm,
                                  find.is_non_target_role, find.seniority_flag, find.is_different_track)
        self.assertEqual(c["reason"], "no_profile_url")


class RealPasteFixtureTests(unittest.TestCase):
    """A regression fixture from the actual BCG alumni-search paste (2026-09-24): markdown links with no
    blank lines between people, mutual-connection lines, a missing leading bracket on the very first entry,
    and anonymized "LinkedIn Member" results at the end."""

    TEXT = (
        "Jane Doe](https://www.linkedin.com/in/jane-doe/)\n"
        "· 2nd\n"
        "Consultant at Boston Consulting Group (BCG)\n"
        "someone and 14 other mutual connections\n"
        "Connect\n"
        "[John Roe](https://www.linkedin.com/in/john-roe/)\n"
        "· 3rd\n"
        "Principal at Boston Consulting Group (BCG)\n"
        "University of Southern California\n"
        "[Message](https://www.linkedin.com/messaging/compose/?profileUrn=x)\n"
        "[LinkedIn Member](https://www.linkedin.com/company/bcg/people/?facetSchool=3084)\n"
        "Lead Generation Specialist at Boston Consulting Group (BCG)\n"
    )

    def test_missing_leading_bracket_is_recovered(self):
        entries, unparsed = alumni_paste.parse(self.TEXT)
        self.assertEqual(unparsed, 0)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["name"], "Jane Doe")
        self.assertEqual(entries[0]["linkedin_url"], "https://www.linkedin.com/in/jane-doe/")

    def test_boilerplate_is_fully_stripped_from_every_headline(self):
        entries, _ = alumni_paste.parse(self.TEXT)
        for e in entries:
            self.assertEqual(e["headline"], e["headline"].strip())
            for junk in ("mutual connection", "Connect", "[Message]", "University of Southern California",
                        "· 2nd", "· 3rd"):
                self.assertNotIn(junk, e["headline"], (e["name"], junk))

    def test_the_message_link_does_not_start_a_new_record(self):
        entries, _ = alumni_paste.parse(self.TEXT)
        self.assertEqual(entries[1]["name"], "John Roe")
        self.assertEqual(entries[1]["headline"], "Principal at Boston Consulting Group (BCG)")

    def test_the_anonymized_member_is_its_own_record_with_no_profile_url(self):
        entries, _ = alumni_paste.parse(self.TEXT)
        self.assertEqual(entries[2]["name"], "LinkedIn Member")
        self.assertFalse(alumni_paste.is_profile_url(entries[2]["linkedin_url"]))


if __name__ == "__main__":
    unittest.main()
