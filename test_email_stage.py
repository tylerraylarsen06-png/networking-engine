#!/usr/bin/env python3
"""Tests for the Stage 2 quota guard — entirely mocked, zero real API spend.
Run with: python3 -m unittest test_email_stage"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import find


def make_response(json_data):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_data
    resp.text = str(json_data)
    return resp


def make_rows(n, firm="Test Firm"):
    # Alphabetic surnames only -- construct_email strips digits from the
    # local part, so a numbered fixture name like "Person 0" silently fails
    # to construct and never reaches ZeroBounce at all.
    letters = "abcdefghijklmnopqrstuvwxyz"
    return [
        {
            "name": f"Person Surname{letters[i % 26]}",
            "firm_queried": firm,
            "seniority_band": "in_band",
            "tier": None,  # no --org in these tests -> render_draft returns (None, None), never called
            "evidence": "",
        }
        for i in range(n)
    ]


def make_args():
    return SimpleNamespace(
        sender_name="Alex Example", sender_school="USC", sender_year="sophomore",
        sender_industry="investment banking", email_domain=None,
    )


def resolved_cache(firm="Test Firm", manual=False):
    """A firm_config cache entry that is already fully resolved (pattern +
    mail_domain present), so get_or_fetch_pattern never needs to call
    Hunter — isolates these tests to ZeroBounce/quota-guard behavior."""
    how_confirmed = "manual: test fixture" if manual else "Hunter domain-search: test fixture"
    return {
        firm.lower(): {
            "firm": firm, "web_domain": "testfirm.com", "mail_domain": "testfirm.com",
            "pattern": "{first}.{last}", "confidence": 90.0,
            "how_confirmed": how_confirmed, "date": "2026-08-21",
        }
    }


def mock_get_factory(zb_statuses, catchall):
    """requests.get replacement: Hunter domain-search asserts it's never
    called (proves zero quota spend when the pattern is pre-cached); Hunter
    account / ZeroBounce credits return static fixtures; ZeroBounce
    validate returns statuses from zb_statuses in call order."""
    state = {"zb_calls": 0}

    def _get(url, params=None, timeout=None, **kwargs):
        if url == find.HUNTER_DOMAIN_SEARCH_URL:
            raise AssertionError(
                "Hunter domain-search was called — pattern should already be fully cached, "
                "this test must spend zero Hunter quota"
            )
        if url == find.HUNTER_ACCOUNT_URL:
            return make_response({"data": {"requests": {
                "searches": {"remaining": 49}, "verifications": {"remaining": 98},
            }}})
        if url == find.ZEROBOUNCE_CREDITS_URL:
            return make_response({"Credits": "50"})
        if url == find.ZEROBOUNCE_VALIDATE_URL:
            idx = state["zb_calls"]
            state["zb_calls"] += 1
            status = zb_statuses[idx] if idx < len(zb_statuses) else "valid"
            return make_response({"status": status, "catchall_domain": catchall})
        raise AssertionError(f"unexpected URL in test: {url}")

    return _get, state


class QuotaGuardTests(unittest.TestCase):
    def setUp(self):
        patch("find.load_hunter_key", return_value="fake-hunter-key").start()
        patch("find.load_zerobounce_key", return_value="fake-zb-key").start()
        patch("find.save_patterns_cache", return_value=None).start()
        self.addCleanup(patch.stopall)

    def run_stage(self, rows, zb_statuses, catchall, cache=None):
        get_fn, state = mock_get_factory(zb_statuses, catchall)
        with patch("find.requests.get", side_effect=get_fn):
            summary = find.run_email_and_draft_stages(
                rows, orgs=[], target_school="Test School",
                args=make_args(), cache=cache or resolved_cache(),
            )
        return summary, state

    def test_stops_after_5_when_over_70_percent_invalid_noncatchall(self):
        rows = make_rows(10)
        zb_statuses = ["invalid"] * 10
        summary, state = self.run_stage(rows, zb_statuses, catchall=False)

        self.assertEqual(state["zb_calls"], 5, "should stop verifying after the 5th call")
        self.assertEqual(summary["firms_guard_tripped"], ["Test Firm"])
        verified = [r for r in rows if r["email_status"]]
        self.assertEqual(len(verified), 5)

    def test_does_not_stop_when_domain_is_catchall(self):
        rows = make_rows(10)
        zb_statuses = ["invalid"] * 10  # same bad-looking rate...
        summary, state = self.run_stage(rows, zb_statuses, catchall=True)  # ...but catch-all

        self.assertEqual(state["zb_calls"], 10, "catch-all domain must never trip the guard")
        self.assertEqual(summary["firms_guard_tripped"], [])
        verified = [r for r in rows if r["email_status"]]
        self.assertEqual(len(verified), 10)

    def test_does_not_stop_when_invalid_rate_under_threshold(self):
        rows = make_rows(10)
        # 1 invalid out of 10 = 10%, well under the 70% threshold.
        zb_statuses = ["valid", "valid", "valid", "valid", "invalid",
                        "valid", "valid", "valid", "valid", "valid"]
        summary, state = self.run_stage(rows, zb_statuses, catchall=False)

        self.assertEqual(state["zb_calls"], 10)
        self.assertEqual(summary["firms_guard_tripped"], [])
        verified = [r for r in rows if r["email_status"]]
        self.assertEqual(len(verified), 10)

    def test_remaining_rows_still_get_addresses_constructed_not_verified(self):
        rows = make_rows(10)
        zb_statuses = ["invalid"] * 10
        self.run_stage(rows, zb_statuses, catchall=False)

        tripped_rows = rows[5:]  # rows 6-10, past the trip point
        for row in tripped_rows:
            self.assertTrue(row["email"], f"{row['name']} should still have a constructed address")
            self.assertEqual(row["email_status"], "", f"{row['name']} should NOT have been verified")
            self.assertIn("quota guard tripped", row["evidence"])

        checked_rows = rows[:5]
        for row in checked_rows:
            self.assertTrue(row["email"])
            self.assertEqual(row["email_status"], "invalid")

    def test_exactly_70_percent_does_not_trip(self):
        # Rule is "MORE than 70%", so exactly 70% must never trip. Sequence
        # is arranged so no incremental checkpoint (n=5..10) exceeds 70%
        # either, not just the final tally: 3 valid, then 7 invalid ->
        # 40%, 50%, 57%, 62.5%, 66.7%, 70% at n=5..10 respectively.
        rows = make_rows(10, firm="Boundary Firm")
        cache = resolved_cache(firm="Boundary Firm")
        zb_statuses = ["valid"] * 3 + ["invalid"] * 7
        summary, state = self.run_stage(rows, zb_statuses, catchall=False, cache=cache)

        self.assertEqual(state["zb_calls"], 10, "exactly 70% must not trip the guard")
        self.assertEqual(summary["firms_guard_tripped"], [])

    def test_collision_labeled_status_counts_toward_guard_rate(self):
        # invalid_possible_collision only appears when the mail_domain is
        # manually confirmed -- verify it still counts as "invalid-like"
        # for the guard's rate calculation, not just plain "invalid".
        rows = make_rows(10, firm="Manual Firm")
        cache = resolved_cache(firm="Manual Firm", manual=True)
        zb_statuses = ["invalid"] * 10  # each becomes invalid_possible_collision (manual + non-catchall)
        summary, state = self.run_stage(rows, zb_statuses, catchall=False, cache=cache)

        self.assertEqual(state["zb_calls"], 5, "invalid_possible_collision must still trip the guard")
        self.assertEqual(summary["status_counts"].get("invalid_possible_collision"), 5)


if __name__ == "__main__":
    unittest.main()
