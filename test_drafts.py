#!/usr/bin/env python3
"""Tests for the Stage 3 draft content rules. Run with: python3 -m unittest test_drafts"""

import unittest

from drafts import (
    BANNED_PHRASES,
    DASH_CHARS,
    DraftContentError,
    count_sentences,
    hook_line,
    render_draft,
    validate_draft,
)


class TestHookLines(unittest.TestCase):
    def test_tier1_has_both_hooks_school_first(self):
        line = hook_line(1, "University of Southern California", "Delta Omega")
        self.assertEqual(
            line,
            "I saw you went to University of Southern California and you're a Delta Omega too.",
        )

    def test_tier2_org_only_never_mentions_school(self):
        line = hook_line(2, "University of Southern California", "Delta Omega")
        self.assertEqual(line, "I saw you're a Delta Omega too.")
        self.assertNotIn("University of Southern California", line)

    def test_tier3_school_only(self):
        line = hook_line(3, "University of Southern California", "Delta Omega")
        self.assertEqual(line, "I saw you went to University of Southern California too.")

    def test_tier4_has_no_hook(self):
        self.assertIsNone(hook_line(4, "USC", "Delta Omega"))

    def test_unknown_tier_has_no_hook(self):
        self.assertIsNone(hook_line(None, "USC", "Delta Omega"))


class TestValidateDraft(unittest.TestCase):
    def setUp(self):
        self.clean_subject = "USC senior, question about Goldman Sachs"
        self.clean_body = (
            "Hi Jane,\n\n"
            "I'm a USC senior recruiting for investment banking soon. "
            "I saw you went to USC too.\n\n"
            "Goldman Sachs is high on my list and I'd love to hear about your experience "
            "there. Do you have any time over the next couple weeks?\n\n"
            "Alex"
        )

    def test_clean_draft_has_no_violations(self):
        self.assertEqual(validate_draft(self.clean_subject, self.clean_body), [])

    def test_every_dash_character_is_caught(self):
        for dash in DASH_CHARS:
            body = self.clean_body.replace("Goldman Sachs", f"Goldman{dash}Sachs")
            violations = validate_draft(self.clean_subject, body)
            self.assertTrue(
                any("dash" in v for v in violations),
                f"dash char {dash!r} (U+{ord(dash):04X}) was not caught",
            )

    def test_compound_hyphens_are_allowed(self):
        for word in ("long-term", "semester-long", "third-year", "data-driven", "Simon-Kucher", "Mary-Kate"):
            violations = validate_draft(self.clean_subject, self.clean_body + f" {word}")
            self.assertFalse(any("dash" in v or "hyphen" in v for v in violations), word)

    def test_ascii_hyphen_used_as_punctuation_is_caught(self):
        for bad in (" and - truly", " and -- truly", "\n- a bullet", " trailing-", " pre- and post-"):
            violations = validate_draft(self.clean_subject, self.clean_body + bad)
            self.assertTrue(any("hyphen used as punctuation" in v for v in violations), repr(bad))

    def test_every_non_ascii_dash_is_still_banned_inside_a_compound(self):
        for dash in DASH_CHARS:
            violations = validate_draft(self.clean_subject, self.clean_body + f" long{dash}term")
            self.assertTrue(any("dash character" in v for v in violations), f"U+{ord(dash):04X}")

    def test_ascii_hyphen_is_not_in_the_banned_set(self):
        self.assertNotIn("-", DASH_CHARS)
        self.assertEqual(len(DASH_CHARS), 7)

    def test_em_dash_is_caught(self):
        violations = validate_draft(self.clean_subject, self.clean_body + " and—truly")
        self.assertTrue(any("dash" in v for v in violations))

    def test_en_dash_is_caught(self):
        violations = validate_draft(self.clean_subject, self.clean_body + " 2024–2025")
        self.assertTrue(any("dash" in v for v in violations))

    def test_word_rather_is_banned(self):
        violations = validate_draft(self.clean_subject, self.clean_body + " I would rather meet in person.")
        self.assertTrue(any("rather" in v for v in violations))

    def test_rather_is_case_insensitive(self):
        violations = validate_draft(self.clean_subject, self.clean_body + " Rather soon.")
        self.assertTrue(any("rather" in v for v in violations))

    def test_word_gather_does_not_false_positive_on_rather(self):
        violations = validate_draft(self.clean_subject, self.clean_body + " Let's gather info.")
        self.assertFalse(any("rather" in v for v in violations))

    def test_more_than_five_sentences_is_rejected(self):
        six_sentences = "One. Two. Three. Four. Five. Six."
        violations = validate_draft(self.clean_subject, six_sentences)
        self.assertTrue(any("sentences" in v for v in violations))

    def test_exactly_five_sentences_is_allowed(self):
        five_sentences = "One. Two. Three. Four. Five."
        violations = validate_draft(self.clean_subject, five_sentences)
        self.assertFalse(any("sentences" in v for v in violations))

    def test_digit_anywhere_is_rejected(self):
        violations = validate_draft(self.clean_subject, self.clean_body + " I have 3 years of experience.")
        self.assertTrue(any("digit" in v for v in violations))

    def test_digit_in_subject_is_rejected(self):
        violations = validate_draft(self.clean_subject + " (2027)", self.clean_body)
        self.assertTrue(any("digit" in v for v in violations))

    def test_all_four_banned_phrases_are_caught(self):
        for phrase in BANNED_PHRASES:
            violations = validate_draft(self.clean_subject, self.clean_body + " " + phrase.capitalize() + ".")
            self.assertTrue(
                any(phrase in v for v in violations),
                f"banned phrase {phrase!r} was not caught",
            )

    def test_banned_phrase_is_case_insensitive(self):
        violations = validate_draft(self.clean_subject, self.clean_body + " Hope This Finds You Well.")
        self.assertTrue(any("hope this finds you well" in v for v in violations))

    def test_leaked_none_from_missing_template_value_is_caught(self):
        # Regression: target_school=None once rendered as the literal text
        # "I saw you went to None too." and shipped in real output.
        violations = validate_draft(self.clean_subject, self.clean_body + " I saw you went to None too.")
        self.assertTrue(any("None" in v for v in violations))

    def test_lowercase_none_is_not_a_false_positive(self):
        violations = validate_draft(self.clean_subject, self.clean_body + " none of this matters here.")
        self.assertFalse(any("None" in v and "literal" in v for v in violations))


class TestCountSentences(unittest.TestCase):
    def test_counts_periods_and_question_marks(self):
        self.assertEqual(count_sentences("One. Two? Three."), 3)

    def test_signature_line_with_no_terminal_punctuation_not_counted(self):
        self.assertEqual(count_sentences("Hi Jane,\n\nOne sentence.\n\nAlex"), 1)


class TestRenderDraft(unittest.TestCase):
    def test_tier2_body_leads_with_org_never_mentions_school(self):
        subject, body = render_draft(
            recipient_first_name="Adam",
            tier=2,
            target_school="University of Southern California",
            org="Delta Omega",
            firm="Goldman Sachs",
            sender_first_name="Alex",
            sender_school="USC",
            sender_year="senior",
            sender_industry="investment banking",
        )
        self.assertIn("I saw you're a Delta Omega too.", body)
        self.assertNotIn("University of Southern California", body)
        self.assertEqual(validate_draft(subject, body), [])

    def test_tier1_body_has_exactly_two_hooks_school_then_org(self):
        _, body = render_draft(
            recipient_first_name="Jane",
            tier=1,
            target_school="USC",
            org="Delta Omega",
            firm="Goldman Sachs",
            sender_first_name="Alex",
            sender_school="USC",
            sender_year="senior",
            sender_industry="investment banking",
        )
        self.assertIn("I saw you went to USC and you're a Delta Omega too.", body)

    def test_tier4_returns_none_none(self):
        subject, body = render_draft(
            recipient_first_name="Jane",
            tier=4,
            target_school="USC",
            org="Delta Omega",
            firm="Goldman Sachs",
            sender_first_name="Alex",
            sender_school="USC",
            sender_year="senior",
            sender_industry="investment banking",
        )
        self.assertIsNone(subject)
        self.assertIsNone(body)

    def test_hyphenated_recipient_name_renders(self):
        subject, body = render_draft(
            recipient_first_name="Mary-Kate", tier=3, target_school="USC", org="Delta Omega",
            firm="Goldman Sachs", sender_first_name="Alex", sender_school="USC",
            sender_year="senior", sender_industry="investment banking",
        )
        self.assertIn("Hi Mary-Kate,", body)

    def test_hyphenated_firm_renders_a_draft(self):
        """The bug that kept coming back: Simon-Kucher used to produce no draft at all."""
        subject, body = render_draft(
            recipient_first_name="Jane", tier=3, target_school="University of Southern California",
            org=None, firm="Simon-Kucher", sender_first_name="Alex", sender_school="USC",
            sender_year="sophomore", sender_industry="consulting",
        )
        self.assertIn("Simon-Kucher", subject)
        self.assertIn("Simon-Kucher", body)
        self.assertEqual(validate_draft(subject, body), [])

    def test_hyphenated_firm_renders_the_followup_too(self):
        from drafts import render_followup
        fsubject, fbody = render_followup("Jane", "Simon-Kucher", "Alex", "USC sophomore, question about Simon-Kucher")
        self.assertIn("Simon-Kucher", fbody)
        self.assertEqual(validate_draft(fsubject, fbody), [])

    def test_an_en_dash_in_a_firm_name_still_refuses_to_render(self):
        with self.assertRaises(DraftContentError):
            render_draft(
                recipient_first_name="Jane", tier=3, target_school="USC", org=None, firm="Simon\u2013Kucher",
                sender_first_name="Alex", sender_school="USC", sender_year="senior", sender_industry="consulting",
            )

    def test_five_or_fewer_sentences_by_construction(self):
        _, body = render_draft(
            recipient_first_name="Jane",
            tier=1,
            target_school="USC",
            org="Delta Omega",
            firm="Goldman Sachs",
            sender_first_name="Alex",
            sender_school="USC",
            sender_year="senior",
            sender_industry="investment banking",
        )
        self.assertLessEqual(count_sentences(body), 5)


class DisplayNameAndSignatureTests(unittest.TestCase):
    KW = dict(recipient_first_name="Jane", tier=3, org=None, firm="Bain & Company",
              sender_first_name="Alex", sender_school="USC", sender_year="sophomore",
              sender_industry="consulting")
    SIG = "Alex Example, President, Example Investing Club"

    def test_hook_uses_the_short_form_from_the_mapping(self):
        from drafts import display_school
        self.assertEqual(display_school("University of Southern California"), "USC")
        subject, body = render_draft(target_school="University of Southern California", **self.KW)
        self.assertIn("I saw that you went to USC before joining Bain.", body)
        self.assertNotIn("University of Southern California", body)

    def test_mapping_ignores_case_and_punctuation(self):
        from drafts import display_school
        for variant in ("university of southern california", "University of Southern California.",
                        "UNIVERSITY  OF SOUTHERN-CALIFORNIA"):
            self.assertEqual(display_school(variant), "USC", variant)
        self.assertEqual(display_school("University of California, Los Angeles"), "UCLA")

    def test_unknown_school_falls_back_to_the_full_name(self):
        from drafts import display_school
        self.assertEqual(display_school("Reed College"), "Reed College")
        _, body = render_draft(target_school="Reed College", **self.KW)
        self.assertIn("I saw that you went to Reed College before joining Bain.", body)

    def test_school_short_override_wins_over_the_mapping(self):
        _, body = render_draft(target_school="University of Southern California", school_short="Southern Cal", **self.KW)
        self.assertIn("went to Southern Cal before joining Bain.", body)

    def test_tier_1_hook_also_uses_the_short_form(self):
        kw = dict(self.KW, tier=1, org="Delta Omega")
        _, body = render_draft(target_school="University of Southern California", **kw)
        self.assertIn("I saw you went to USC and you're a Delta Omega too.", body)

    def test_mapping_file_is_valid_and_all_values_are_strings(self):
        import json
        from drafts import SCHOOL_DISPLAY_PATH
        data = json.loads(SCHOOL_DISPLAY_PATH.read_text())
        self.assertTrue(all(isinstance(v, str) and v for k, v in data.items() if not k.startswith("_")))

    def test_matching_still_uses_the_full_name(self):
        import find
        self.assertTrue(find.fuzzy_contains("USC Marshall School of Business", "University of Southern California"))
        self.assertTrue(find.fuzzy_contains("University of Southern California", "University of Southern California"))

    def test_signature_replaces_the_first_name_signoff(self):
        _, body = render_draft(target_school="University of Southern California", sender_signature=self.SIG, **self.KW)
        self.assertTrue(body.endswith("\n\n" + self.SIG))
        self.assertNotIn("\n\nAlex\n", body, "the first-name line must be gone")
        self.assertEqual(body.count("Alex"), 1, "the name appears once, inside the signature")

    def test_without_a_signature_the_first_name_still_signs_off(self):
        _, body = render_draft(target_school="University of Southern California", **self.KW)
        self.assertTrue(body.endswith("\n\nAlex"))

    def test_signature_passes_validation_and_adds_no_sentence(self):
        s1, plain = render_draft(target_school="University of Southern California", **self.KW)
        s2, signed = render_draft(target_school="University of Southern California", sender_signature=self.SIG, **self.KW)
        self.assertEqual(validate_draft(s2, signed), [])
        self.assertEqual(count_sentences(signed), count_sentences(plain))
        self.assertLessEqual(count_sentences(signed), 5)

    def test_no_signature_means_body_is_unchanged(self):
        _, a = render_draft(target_school="University of Southern California", **self.KW)
        _, b = render_draft(target_school="University of Southern California", sender_signature="  ", **self.KW)
        self.assertEqual(a, b)

    def test_a_signature_is_still_checked_for_dashes(self):
        with self.assertRaises(DraftContentError):
            render_draft(target_school="USC", sender_signature="Alex Example \u2014 President", **self.KW)

    def test_hyphenated_org_in_the_signature_is_allowed(self):
        _, body = render_draft(target_school="USC", sender_signature="Alex Example, President, Simon-Kucher Club, USC", **self.KW)
        self.assertIn("Simon-Kucher Club", body)


if __name__ == "__main__":
    unittest.main()
