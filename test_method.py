"""The message method: one failing example per checker rule, hook selection, and title fit.
All names, schools and companies here are made up."""
import unittest

import method

PROFILE = {
    "name": "Alex Example", "school": "Example State University", "school_short": "Example State",
    "year": "sophomore", "major": "economics",
    "clubs": [
        {"name": "Example AI Club", "what_i_do": "run an AI club that does AI marketing for companies",
         "advice_on": "doing AI marketing", "topics": ["marketing", "brand", "product"], "fits": ["any"]},
        {"name": "Example Investing Club", "what_i_do": "pitch stocks in a student investing club",
         "advice_on": "learning to pitch stocks", "topics": ["equity", "research"], "fits": ["finance"]},
    ],
    "aspirations": [{"industry": "media", "fits": ["media"]}],
    "notable_facts": [{"name": "stadium crew", "text": "worked on the field crew at a college bowl game",
                       "fits": ["sports", "media", "los angeles"]}],
}

GOOD = ("Hi Jamie,\n\nI'm an Example State sophomore studying economics. I run an AI club that does AI "
        "marketing for companies, and you work on marketing at Examplecorp, which is really interesting to me. "
        "I'd love any tips you have for someone doing AI marketing like me. Would you have 15 minutes in the "
        "next couple weeks?\n\nThanks,\nAlex")
SUBJECT = "Example State sophomore with a question"


def check(body, subject=SUBJECT, others=()):
    return method.check_message(subject, body, "Jamie", "Alex", others)


def swap(old, new):
    assert old in GOOD, old
    return GOOD.replace(old, new)


class CheckerRules(unittest.TestCase):
    def test_good_message_passes(self):
        self.assertEqual(check(GOOD), [])

    def test_greeting_required(self):
        self.assertTrue(any("does not open" in v for v in check(swap("Hi Jamie,", "Hello Jamie,"))))

    def test_greeting_uses_first_name(self):
        self.assertTrue(any("does not open" in v for v in check(swap("Hi Jamie,", "Hi Jamie Doe,"))))

    def test_signoff_required(self):
        self.assertTrue(any("sign off" in v for v in check(swap("Thanks,\nAlex", "Best,\nAlex"))))

    def test_signoff_name_required(self):
        self.assertTrue(any("sign off" in v for v in check(swap("Thanks,\nAlex", "Thanks,\nAlex Example"))))

    def test_em_dash(self):
        self.assertTrue(any("dash character" in v for v in check(swap(", which is", " — which is"))))

    def test_en_dash(self):
        self.assertTrue(any("dash character" in v for v in check(swap(", which is", " – which is"))))

    def test_spaced_hyphen(self):
        self.assertTrue(any('" - "' in v for v in check(swap(", which is", " - which is"))))

    def test_hyphen_inside_word_is_fine(self):
        self.assertEqual(check(swap("AI club", "AI-first club")), [])

    def test_rather(self):
        self.assertTrue(any('"rather"' in v for v in check(swap("really interesting", "rather interesting"))))

    def test_i_saw(self):
        self.assertTrue(any('"I saw"' in v for v in check(swap("I run an AI club", "I saw you work there and I run an AI club"))))

    def test_i_found_your_profile(self):
        body = swap("I run an AI club", "I found your profile and I run an AI club")
        self.assertTrue(any('"I found your profile"' in v for v in check(body)))

    def test_day_to_day(self):
        body = swap("you work on marketing", "your day to day is marketing")
        self.assertTrue(any('"day to day"' in v for v in check(body)))

    def test_number_about_sender(self):
        self.assertTrue(any("number" in v for v in check(swap("an AI club that", "a 40 person AI club that"))))

    def test_number_in_subject(self):
        self.assertTrue(any("number" in v for v in check(GOOD, subject="Example State class of 2028")))

    def test_fifteen_minutes_is_allowed(self):
        self.assertFalse(any("number" in v for v in check(GOOD)))

    def test_exactly_one_question_mark_not_two(self):
        body = swap("like me.", "like me, could you help?")
        self.assertTrue(any("question marks" in v for v in check(body)))

    def test_exactly_one_question_mark_not_zero(self):
        body = swap("next couple weeks?", "next couple weeks.")
        self.assertTrue(any("question marks" in v for v in check(body)))

    def test_career_path_after_x_and_y(self):
        body = swap("you work on marketing at Examplecorp", "after Acme and Initech you work on marketing at Examplecorp")
        self.assertTrue(any("career path" in v for v in check(body)))

    def test_career_path_went_from(self):
        body = swap("you work on marketing at Examplecorp", "you went from sales to marketing at Examplecorp")
        self.assertTrue(any("career path" in v for v in check(body)))

    def test_word_cap(self):
        long = " ".join(["really"] * 60)
        body = swap("really interesting to me", f"{long} interesting to me")
        self.assertTrue(any("words" in v for v in check(body)))

    def test_too_many_sentences(self):
        body = swap("like me.", "like me. Marketing is fun. I like it.")
        self.assertTrue(any("sentences" in v for v in check(body)))

    def test_too_few_sentences(self):
        body = ("Hi Jamie,\n\nI'm an Example State sophomore studying economics. Would you have 15 minutes in the "
                "next couple weeks?\n\nThanks,\nAlex")
        self.assertTrue(any("sentences" in v for v in check(body)))

    def test_one_ask_only(self):
        body = swap("like me.", "like me, and I'd love an intro too.")
        self.assertTrue(any("more than one ask" in v for v in check(body)))

    def test_other_target_company(self):
        body = swap("which is really", "like Example Media does, which is really")
        self.assertTrue(any("another company" in v for v in check(body, others=["Example Media"])))

    def test_clause_checks(self):
        self.assertEqual(method.check_clause("you're using AI to market audiobooks at a huge scale"), [])
        self.assertTrue(method.check_clause("I think you market audiobooks"))
        self.assertTrue(method.check_clause("Marketing lead at Examplecorp"))


class Hooks(unittest.TestCase):
    def test_title_topic_picks_matching_club(self):
        hook = method.pick_hook(PROFILE, {"name": "Jamie Doe", "title": "Product Marketing Manager", "company": "Examplecorp"})
        self.assertEqual(hook["item"]["name"], "Example AI Club")
        self.assertEqual(hook["topic"], "product marketing")

    def test_club_fits_restricts_industry(self):
        p = {"name": "Pat Lee", "title": "Equity Research Associate", "company": "Example Bank"}
        self.assertIsNone(method.pick_hook(PROFILE, p, "technology"))
        self.assertEqual(method.pick_hook(PROFILE, p, "finance")["item"]["name"], "Example Investing Club")

    def test_aspiration_fallback_when_no_work_matches(self):
        hook = method.pick_hook(PROFILE, {"name": "Sam Roe", "title": "Director of Programming", "company": "Example Media"}, "media")
        self.assertEqual(hook["kind"], "aspiration")

    def test_no_true_hook_returns_none(self):
        self.assertIsNone(method.pick_hook(PROFILE, {"name": "Sam Roe", "title": "Director of Programming", "company": "Example Bank"}, "finance"))

    def test_fact_only_for_recipients_on_fits_list(self):
        media = method.recipient_tags({"company": "Example Media", "city": "New York"}, "media")
        bank = method.recipient_tags({"company": "Example Bank", "city": "Chicago"}, "finance")
        la = method.recipient_tags({"company": "Example Bank", "city": "Los Angeles"}, "finance")
        self.assertIsNotNone(method.pick_fact(PROFILE, media))
        self.assertIsNone(method.pick_fact(PROFILE, bank))
        self.assertIsNotNone(method.pick_fact(PROFILE, la))

    def test_fact_without_fits_never_used(self):
        prof = dict(PROFILE, notable_facts=[{"text": "ran a marathon"}])
        self.assertIsNone(method.pick_fact(prof, ["media"]))

    def test_composed_message_passes_checker(self):
        r = {"name": "Jamie Doe", "title": "Product Marketing Manager", "company": "Examplecorp"}
        subject, body = method.compose(PROFILE, r, method.pick_hook(PROFILE, r))
        self.assertEqual(method.check_message(subject, body, "Jamie", "Alex"), [])
        self.assertIn("I'm an Example State sophomore studying economics.", body)

    def test_article(self):
        self.assertEqual(method.article("USC"), "a")
        self.assertEqual(method.article("NYU"), "an")
        self.assertEqual(method.article("Example State"), "an")
        self.assertEqual(method.article("Stanford"), "a")


class TitleFit(unittest.TestCase):
    def test_business_role_skips_engineers_recruiters_legal_admin(self):
        role = "Product Management Intern"
        for title in ("Software Development Engineer", "Senior Technical Recruiter", "Corporate Counsel",
                      "Executive Assistant", "Administrative Coordinator"):
            self.assertIsNotNone(method.skip_reason(title, role), title)
        self.assertIsNone(method.skip_reason("Senior Product Manager", role))

    def test_technical_role_keeps_engineers(self):
        self.assertIsNone(method.skip_reason("Software Development Engineer", "Software Engineering Intern"))
        self.assertIsNotNone(method.skip_reason("Technical Recruiter", "Software Engineering Intern"))

    def test_relevance_prefers_related_titles(self):
        role = "Product Management Intern"
        self.assertGreater(method.relevance("Senior Product Manager", role), method.relevance("Finance Manager", role))


if __name__ == "__main__":
    unittest.main()
