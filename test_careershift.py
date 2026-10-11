import json
import unittest
from unittest import mock

import careershift


class FakeResp:
    def __init__(self, body, status=200, text=""):
        self._body, self.status_code, self.text = body, status, text

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self):
        self.posts = []

    def get(self, url, params=None, headers=None, timeout=None):
        if url.endswith("/companies/autocomplete"):
            return FakeResp({"results": [
                {"lookupKey": "zi-1", "name": "Twenty-First Century Fox", "employeeCount": 21500},
                {"lookupKey": "zi-2", "name": "FOX", "employeeCount": 10650},
            ]})
        if url.endswith("/contacts/search"):
            if params["page"] > 1:
                return FakeResp({"results": []})
            return FakeResp({"results": [
                {"name": "Jane Doe", "jobTitle": "Analyst", "companyName": "FOX", "email": "jane.doe@example.com",
                 "hasEmail": True, "externalId": "1"},
                {"name": "John Roe", "jobTitle": "Associate", "companyName": "FOX", "email": None,
                 "hasEmail": True, "externalId": "2"},
                {"name": "No Mail", "jobTitle": "Director", "companyName": "FOX", "email": None,
                 "hasEmail": False, "externalId": "3"},
            ]})
        if url.endswith("/Contacts/Search"):
            return FakeResp(None, text='<meta name="csrf-token" content="tok">')
        raise AssertionError(url)

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append(json)
        return FakeResp({"results": [{"email": "john.roe@example.com"}], "credits": {"remaining": 49, "limit": 50}})


class CareerShiftTest(unittest.TestCase):
    def test_exact_company_name_beats_autocomplete_order(self):
        self.assertEqual(careershift.find_company(FakeSession(), "FOX"), "zi-2:FOX")

    def test_reveal_only_people_with_hidden_email(self):
        fake = FakeSession()
        with mock.patch.object(careershift, "_session", return_value=fake):
            rows, credits = careershift.lookup("Example University", "FOX", n=10, reveal_n=5)
        self.assertEqual(fake.posts, [{"person_id": "2"}])  # never spends a credit on someone with no email
        self.assertEqual([r["email"] for r in rows], ["jane.doe@example.com", "john.roe@example.com", ""])
        self.assertEqual(credits["remaining"], 49)

    def test_search_alone_spends_no_credits(self):
        fake = FakeSession()
        with mock.patch.object(careershift, "_session", return_value=fake):
            careershift.lookup("Example University", "FOX", n=10, reveal_n=0)
        self.assertEqual(fake.posts, [])


class CsvSafeTest(unittest.TestCase):
    def test_formula_prefixes_are_neutralized(self):
        self.assertEqual(careershift._csv_safe("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(careershift._csv_safe("@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(careershift._csv_safe("Analyst"), "Analyst")
        self.assertEqual(careershift._csv_safe(None), "")


if __name__ == "__main__":
    unittest.main()
