"""Draft templates, chosen by a HIERARCHY of real facts (no model calls, no random rotation).

The templates share one ask on purpose: personalization varies, the ask does not. Structure of each:
verified fact, why it makes them relevant, then the same 15 minute call request.

Selection order, first match wins. The first four rungs come from the sender's profile (profile.json, see
sender_profile.py) and are only offered when that profile field is filled in:
  1 shared high school    (profile high_school, matched in the recipient's education history)
  2 shared organization   (only the orgs in the profile's "orgs" list)
  3 shared major          (profile major, read from the recipient's degree line at the target school)
  4 home city             (profile home_city, matched in the recipient's LinkedIn location)
  5 distinctive prior employer (a VERIFIED professional employer only; unverified falls through)
  6 recently joined       (2025 or 2026 start, so they just went through recruiting)
  7 longer tenure         (2023 or earlier, so hindsight is the angle)
  8 school fallback
A row also records WHICH fact drove the choice and WHICH field it came from, so the choice is auditable.

Never claim an organization or major the sender is not really in. Never use "I recruited for it" as a
fallback.

This module is pure: no network, no file writes, no find.py import.
"""
import difflib
import hashlib
import json
import re
from pathlib import Path

import sender_profile


SIMILARITY_THRESHOLD = 0.80   # a WARNING threshold only; pacing is the real control (see bridge.pace_by_domain)

# The sender's profile. Read at import; tests and callers may replace it (variants.PROFILE = {...}).
PROFILE = sender_profile.load_profile()

ASK = "Would you have 15 minutes for a call in the next two weeks?"

T_HIGH_SCHOOL, T_ORG, T_MAJOR, T_CITY, T_PRIOR, T_RECENT, T_TENURE, T_FALLBACK = (
    "shared_high_school", "shared_org", "shared_major", "home_city", "prior_employer", "recently_joined",
    "longer_tenure", "school_fallback")
# Identity and commonality rungs first, then the firm-tenure signals (prior employer, recently joined,
# longer tenure), then the school-only fallback.
TEMPLATE_ORDER = [T_HIGH_SCHOOL, T_ORG, T_MAJOR, T_CITY, T_PRIOR, T_RECENT, T_TENURE, T_FALLBACK]

RECENT_YEARS = (2025, 2026)
TENURE_MAX_YEAR = 2023


def _p(key):
    return sender_profile.field(PROFILE, key)


def major_regex():
    """Regex for the sender's major (whitespace tolerant), or None when the profile has no major."""
    major = _p("major")
    if not major:
        return None
    return re.compile(r"\s+".join(re.escape(w) for w in major.split()), re.I)


def load_sender_orgs():
    """Orgs the sender is really in, from the profile: [{name (as said), match: [full spelled names to
    accept]}]. A bare acronym is never a match variant: 'GIS' also means geographic information systems."""
    orgs = (PROFILE or {}).get("orgs") or []
    return [o for o in orgs if isinstance(o, dict) and o.get("name") and o.get("match")]


def _s(x):
    return str(x or "").strip()


def select_template(facts):
    """facts: dict with high_school, high_school_source, shared_org, shared_org_source, school_degree,
    school_degree_source, home_city, home_city_source, prior_employer, prior_employer_source,
    firm_start_year, firm_start_source. Returns {"id", "fact", "source", "params"}; never raises, always
    ends at the school fallback."""
    f = facts or {}
    if _s(f.get("high_school")) and _p("high_school"):
        return {"id": T_HIGH_SCHOOL, "fact": f"went to {_p('high_school')}", "source": _s(f.get("high_school_source")),
                "params": {}}
    if _s(f.get("shared_org")):
        return {"id": T_ORG, "fact": f"member of {_s(f['shared_org'])}",
                "source": _s(f.get("shared_org_source")), "params": {"org": _s(f["shared_org"])}}
    major_re = major_regex()
    if major_re and major_re.search(_s(f.get("school_degree"))):
        return {"id": T_MAJOR, "fact": f"studied {_p('major')}",
                "source": _s(f.get("school_degree_source")) or "educationHistory degree", "params": {}}
    if _s(f.get("home_city")) and _p("home_city"):
        return {"id": T_CITY, "fact": f"based in {_p('home_city')}", "source": _s(f.get("home_city_source")),
                "params": {}}
    if _s(f.get("prior_employer")):
        return {"id": T_PRIOR, "fact": f"prior employer {_s(f['prior_employer'])}",
                "source": _s(f.get("prior_employer_source")), "params": {"prior": display_firm(_s(f["prior_employer"]))}}
    y = _s(f.get("firm_start_year"))
    if y.isdigit():
        src = _s(f.get("firm_start_source")) or "workHistory dates (current firm)"
        if int(y) in RECENT_YEARS:
            return {"id": T_RECENT, "fact": f"joined the firm in {y}", "source": src, "params": {"year": y}}
        if int(y) <= TENURE_MAX_YEAR:
            return {"id": T_TENURE, "fact": f"at the firm since {y}", "source": src, "params": {"year": y}}
    return {"id": T_FALLBACK, "fact": "school alum (school match)", "source": "school_match", "params": {"year": y}}


def render_template(choice, school, firm, sender_school, sender_year, sender_industry):
    """(subject, paragraph) for a selected template. `school` and `firm` are already the spoken forms.
    Every paragraph is at most two sentences; the ask is a third, identical everywhere."""
    t, p = choice["id"], choice["params"]
    prep = f"preparing for {sender_industry} recruiting"
    if t == T_HIGH_SCHOOL:
        hs, hs_short = _p("high_school"), _p("high_school_short") or _p("high_school")
        return (f"{hs_short} to {firm}",
                f"I saw that you went to {hs} before joining {firm}. I also went to {hs_short}, and "
                f"I'm {prep}, so I'd love to hear how that path shaped your road to {firm}.")
    if t == T_ORG:
        org = p["org"]
        return (f"{org} to {firm}",
                f"I saw that you were part of {org} at {school} before joining {firm}. I'm also in {org} and {prep}, "
                f"so I'd love to hear how you used that experience during the process.")
    if t == T_MAJOR:
        major, major_long = _p("major"), _p("major_long") or _p("major")
        return (f"{major} to {firm}",
                f"I saw that you studied {major} at {school} before joining {firm}. I'm studying "
                f"{major_long}, and I'd love to hear how you positioned that background during recruiting.")
    if t == T_PRIOR:
        return (f"Your path to {firm}",
                f"I saw that you went to {school} and worked at {p['prior']} before joining {firm}. I'm {prep}, "
                f"and I'd love to hear how that experience shaped your path to {firm}.")
    if t == T_CITY:
        city, area = _p("home_city"), _p("home_area") or _p("home_city")
        return (f"{city} to {firm}",
                f"I saw that you're based in {city} and went to {school} before joining {firm}. I'm from "
                f"the {area} area too and {prep}, so I'd love to hear about your path to {firm}.")
    if t == T_RECENT:
        return (f"Question from a {sender_school} {sender_year}",
                f"I saw that you recently joined {firm} after {school}. Since you went through recruiting recently, "
                f"I'd love to hear what made the biggest difference in your process.")
    if t == T_TENURE:
        return (f"{school} to {firm}",
                f"I saw that you went from {school} to {firm} and have now been there since {p['year']}. I'm {prep}, "
                f"and I'd love to hear what you know now that you wish you had known while recruiting.")
    return (f"{school} {sender_year} interested in {firm}",
            f"I saw that you went to {school} before joining {firm}. I'm a {sender_school} {sender_year} {prep}, "
            f"and I'd love to hear how you approached {firm} recruiting from {school}.")


def template_numbers(choice):
    """Digit strings a template inserts about the RECIPIENT (their firm start year), for validate_draft."""
    y = (choice.get("params") or {}).get("year")
    return [y] if choice["id"] == T_TENURE and y else []


FIRM_DISPLAY_PATH = Path(__file__).resolve().parent / "firm_display.json"
_FIRM_CACHE = None
_LEGAL_SUFFIX_RE = re.compile(r"[,\s]+(inc\.?|llc|llp|l\.l\.p\.|ltd\.?|limited|corp\.?|corporation|gmbh|plc)\s*$", re.I)
_NOT_A_COMPANY_RE = re.compile(
    r"\b(university|college|school|institute|self[- ]?employed|freelance|independent|stealth|confidential|various|"
    r"consultant \(|contractor|student|fellowship|society|club|association|fraternity|sorority|chapter|council|honor|"
    r"organization|usc|ucla|mit)\b", re.I)


def _norm(s):
    return re.sub(r"\W", "", (s or "").lower())


def load_firm_display():
    global _FIRM_CACHE
    if _FIRM_CACHE is None:
        table = {}
        try:
            raw = json.loads(FIRM_DISPLAY_PATH.read_text())
            table = {_norm(k): v for k, v in raw.items() if not k.startswith("_") and isinstance(v, str) and v.strip()}
        except (OSError, ValueError):
            pass
        _FIRM_CACHE = table
    return _FIRM_CACHE


def display_firm(name):
    """A firm the way a person says it ("Bain & Company" -> "Bain"). Unlisted firms are written out with
    any trailing legal suffix removed, so a missing entry is safe."""
    if not name:
        return name
    table = load_firm_display()
    if _norm(name) in table:
        return table[_norm(name)]
    cleaned = _LEGAL_SUFFIX_RE.sub("", name.strip())
    return table.get(_norm(cleaned), cleaned)


def usable_company_name(name, firm):
    """True if a prior employer name is safe to say out loud in a hook. A name carrying a digit (e.g. "180
    Degrees Consulting") is never usable: validate_draft bans any digit that isn't the recipient's own firm
    start year, so a real digit-bearing company name would always trip the content rule (seen in a real
    paste run, 2026-09-24) — it falls through to the next template in the hierarchy instead of getting stripped or
    guessed at."""
    if not name or len(name) > 40 or _NOT_A_COMPANY_RE.search(name) or any(c.isdigit() for c in name):
        return False
    a, b = _norm(display_firm(name)), _norm(display_firm(firm))
    return bool(a) and a != b and a not in b and b not in a


# ------------------------------------------------------------------ similarity

def _words(subject, body, first_name):
    text = f"{subject}\n{body}".lower()
    if first_name:
        text = re.sub(r"\b" + re.escape(first_name.lower()) + r"\b", "name", text)
    return re.findall(r"[a-z0-9']+", text)


def similarity(subject_a, body_a, first_a, subject_b, body_b, first_b):
    """Word-level similarity 0 to 1. The recipient's first name is masked so it never counts as a difference."""
    wa, wb = _words(subject_a, body_a, first_a), _words(subject_b, body_b, first_b)
    if not wa or not wb:
        return 0.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def email_domain(email):
    return (email or "").rsplit("@", 1)[-1].strip().lower() if "@" in (email or "") else ""


