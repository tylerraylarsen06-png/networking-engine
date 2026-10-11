#!/usr/bin/env python3
"""Find real people to network with via the Exa search API (category="people")."""

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import requests

from drafts import DraftContentError, render_draft
import dnc
import sender_profile
import variants

EXA_SEARCH_URL = "https://api.exa.ai/search"
ORG_VARIANTS_PATH = Path(__file__).resolve().parent / "org_variants.json"
TITLE_FILTERS_PATH = Path(__file__).resolve().parent / "title_filters.json"
EXA_MAX_RESULTS = 100

SENIOR_TITLE_PATTERNS = [
    r"\bmanaging director\b",
    r"\bmd\b",
    r"\bpartner\b",
    r"\bprincipal\b",
    r"\bmanager\b",         # Manager, Senior Manager, Engagement Manager: well above the 2 to 5 year target
    r"\bdirector\b",        # Director, Associate Director
    r"\bproject leader\b",  # BCG's manager-equivalent rung (Associate < Consultant < Project Leader < Principal)
    r"\bsenior project management\b",
    r"\bvice president\b",
    r"\bvp\b",
    r"\bsvp\b",
    r"\bevp\b",
    r"\bfounder\b",
    r"\bco-?founder\b",
    r"\bchief\b",
    r"\bpresident\b",
    r"\bhead of\b",
]

INTERN_TITLE_PATTERNS = [
    r"\bintern\b",
    r"\binternship\b",
    r"\bsummer analyst\b",
    r"\bsummer associate\b",
    r"\bco-?op\b",
]

DEFAULT_MIN_SENIORITY_YEARS = 2.0
DEFAULT_MAX_SENIORITY_YEARS = 5.0

ACTIVITIES_RE = re.compile(r"activities\s*(?:and|&)\s*societies\s*:?\s*([^\n]+)", re.IGNORECASE)
ORGANIZATIONS_SECTION_RE = re.compile(r"##\s*Organizations\b(.*?)(?=\n##\s|\Z)", re.IGNORECASE | re.DOTALL)
ORG_ITEM_DATE_TAIL_RE = re.compile(
    r"\s+(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}|\d{4}|Present)\b.*$",
    re.IGNORECASE | re.DOTALL,
)

MIN_VARIANT_LEN = 3  # normalized length; drops risky short abbreviations like "ΔΩ" -> "κσ" (2 chars)

ORG_SUFFIX_WORDS = ["fraternity", "sorority", "international", "national", "chapter", "inc"]
CORROBORATION_WORDS = ["fraternity", "sorority", "chapter", "brother", "pledge", "alumni", "alumnus", "alumna"]
GREEK_CHAPTER_PATTERN_RE = re.compile(r"\b[A-Z][a-z]+-[A-Z][a-z]+\b")  # e.g. "Alpha-Kappa", "Delta-Xi"

BASE_CSV_FIELDS = [
    "name",
    "firm_queried",
    "linkedin_url",
    "linkedin_search_url",
    "city",
    "school_match",
    "firm_match",
    "affiliation_match",
    "tier",
]
EMAIL_CSV_FIELDS = ["email", "email_status", "email_pattern_confidence", "draft_subject", "draft_body"]
TITLE_CSV_FIELDS = ["title_match"]
TAIL_CSV_FIELDS = ["title", "seniority_flag", "seniority_band", "total_experience_years"]
# Real facts about the person, captured at build time so hooks can be varied later from the CSV alone.
FACT_CSV_FIELDS = ["high_school", "high_school_source", "firm_start_year", "firm_start_source", "school_end_year",
                   "school_degree", "school_degree_source", "home_city", "home_city_source", "prior_employer",
                   "prior_employer_source", "shared_org", "shared_org_source", "facts_checked",
                   "template_id", "template_fact", "template_source"]
FACTS_VERSION = "v4"     # bump when the facts columns change, so --enrich-facts refreshes older rows
EVIDENCE_CSV_FIELD = ["evidence"]


# --------------------------------------------------------------------------
# Setup / config
# --------------------------------------------------------------------------

_ENV_LOADED = False


def apply_profile_defaults(args):
    """Fill any sender flag left unset from profile.json (name, school, year, industry, signature).
    A flag given on the command line always wins."""
    for flag, key in (("sender_name", "name"), ("sender_school", "school"), ("sender_year", "year"),
                      ("sender_industry", "industry"), ("sender_signature", "signature")):
        if not getattr(args, flag, None):
            value = sender_profile.field(variants.PROFILE, key)
            if value:
                setattr(args, flag, value)


def load_dotenv():
    """Populate os.environ from a .env file next to this script (idempotent).
    Values already set in the real environment are never overwritten."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if value:
                os.environ.setdefault(key.strip(), value)
    _ENV_LOADED = True


def require_env(var_name, setup_hint):
    load_dotenv()
    value = os.environ.get(var_name)
    if not value:
        sys.exit(f"{var_name} not set. {setup_hint}")
    return value


def load_api_key():
    return require_env(
        "EXA_API_KEY",
        "Copy .env.example to .env and fill in your key, or export EXA_API_KEY in your shell.",
    )


def load_hunter_key():
    return require_env("HUNTER_API_KEY", "Add HUNTER_API_KEY=... to .env.")


def load_zerobounce_key():
    return require_env("ZEROBOUNCE_API_KEY", "Add ZEROBOUNCE_API_KEY=... to .env.")


def load_org_variants():
    """Config-driven spelling variants per org, keyed by lowercased org name.
    Add new orgs here (fraternities, sports teams, honor societies, clubs...)
    without touching any code."""
    if ORG_VARIANTS_PATH.exists():
        try:
            return json.loads(ORG_VARIANTS_PATH.read_text())
        except json.JSONDecodeError:
            print(f"WARNING: could not parse {ORG_VARIANTS_PATH}, ignoring it.", file=sys.stderr)
    return {}


def load_title_filters():
    """Global (not per-firm) title-based lists: non_target_roles (support/
    non-client functions to flag out of seniority banding entirely) and
    leave_indicators (a title that means "not actively working right now"
    even though workHistory shows the role as ongoing)."""
    if TITLE_FILTERS_PATH.exists():
        try:
            return json.loads(TITLE_FILTERS_PATH.read_text())
        except json.JSONDecodeError:
            print(f"WARNING: could not parse {TITLE_FILTERS_PATH}, ignoring it.", file=sys.stderr)
    return {"non_target_roles": [], "leave_indicators": []}


GREEK_UNICODE_RANGE = (0x0370, 0x03FF)  # "Greek and Coptic" block


def is_greek_letters_only(s):
    """True only if every letter in s is a literal Greek Unicode codepoint —
    never a Latin lookalike. Greek "Κ" (U+039A) and Latin "K" (U+004B) look
    identical but are different codepoints; this checks the real codepoint,
    not the rendered glyph, so a config typo (typing a Latin K by mistake)
    gets rejected rather than silently accepted as "Greek"."""
    letters = [c for c in (s or "") if c.isalpha()]
    if not letters:
        return False
    return all(GREEK_UNICODE_RANGE[0] <= ord(c) <= GREEK_UNICODE_RANGE[1] for c in letters)


def org_entry(org, org_config):
    return org_config.get(org.strip().lower()) or {}


def variants_for_org(org, org_config):
    """Drops variants that are too short to be safe (short abbreviations
    collide with unrelated things — state codes, initials — far more often
    than they're worth keeping), EXCEPT "greek_variants" entries, which are
    a distinct, separately-validated variant type: literal Greek Unicode
    monograms only (e.g. "ΔΩ" for Delta Omega). Because normalize() never
    folds Greek to Latin (they're different codepoints, and nothing here
    does Unicode confusable-folding), a Greek monogram can never match its
    ASCII lookalike ("ΔΩ" cannot match "KS") — so it's safe to keep even at
    2 characters. Any greek_variants entry that isn't actually pure Greek
    (e.g. a Latin "K" typo'd in) is rejected, not silently downgraded."""
    entry = org_entry(org, org_config)
    variants = entry.get("variants") or [org]
    variants = [v for v in variants if len(normalize(v)) >= MIN_VARIANT_LEN]

    for gv in entry.get("greek_variants") or []:
        if is_greek_letters_only(gv):
            variants.append(gv)
        else:
            print(
                f"WARNING: org_variants.json greek_variants entry {gv!r} for {org!r} "
                f"contains non-Greek characters (a Latin lookalike?); ignoring it.",
                file=sys.stderr,
            )
    return variants


def excludes_for_org(org, org_config):
    return org_entry(org, org_config).get("exclude") or []


# --------------------------------------------------------------------------
# Fuzzy text helpers
# --------------------------------------------------------------------------

def normalize(s):
    # \W (not \w) so this stays Unicode-aware and keeps non-ASCII letters,
    # e.g. Greek "ΔΩ" — the old [^a-z0-9] silently stripped those to "".
    return re.sub(r"\W", "", (s or "").lower())


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def acronym_of(name):
    return "".join(w[0] for w in re.findall(r"[A-Za-z']+", name or "") if w[0].isupper())


def fuzzy_contains(candidate_name, target_name):
    """True if candidate_name plausibly refers to target_name (substring or acronym match).

    Covers cases like a structured institution name of "USC Marshall School of
    Business" (a sub-school), which contains the university's acronym as a
    standalone word but not its full name as a substring."""
    if not candidate_name or not target_name:
        return False
    c, t = normalize(candidate_name), normalize(target_name)
    if not c or not t:
        return False
    if t in c or c in t:
        return True
    acr = acronym_of(target_name)
    if acr and len(acr) >= 2:
        if normalize(acr) == c:
            return True
        if re.search(r"\b" + re.escape(acr.lower()) + r"\b", candidate_name.lower()):
            return True
    return False


def find_snippet(text, term, window=90):
    """Return a short window of raw text around the first mention of term (or its acronym)."""
    if not text or not term:
        return ""
    m = re.search(re.escape(term), text, re.IGNORECASE)
    if not m:
        acr = acronym_of(term)
        if len(acr) >= 2:
            m = re.search(r"\b" + re.escape(acr) + r"\b", text)
    if not m:
        return ""
    start = max(0, m.start() - window)
    end = min(len(text), m.end() + window)
    snippet = re.sub(r"\s+", " ", text[start:end]).strip()
    return f"...{snippet}..."


# --------------------------------------------------------------------------
# Exa response parsing
# --------------------------------------------------------------------------

def get_person_props(result):
    for ent in result.get("entities") or []:
        if ent.get("type") == "person":
            return ent.get("properties") or {}
    return {}


def guess_name(result, props):
    name = props.get("name")
    if name:
        return name
    title = result.get("title") or ""
    name = re.split(r"\s+[-|]\s+LinkedIn\b", title, maxsplit=1)[0]
    return name.strip() or title.strip()


def guess_title_from_text(text):
    if not text:
        return ""
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    m = re.match(r"(.{3,80}?)\s+at\s+.{2,80}", first_line, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def seniority_flag(title):
    t = (title or "").lower()
    for pat in SENIOR_TITLE_PATTERNS:
        if re.search(pat, t):
            return "SENIOR"
    return ""


def is_intern_title(title):
    t = (title or "").lower()
    return any(re.search(pat, t) for pat in INTERN_TITLE_PATTERNS)


def parse_partial_date(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def years_experience(props, now=None):
    """TOTAL career experience: years since the earliest NON-internship
    workHistory start date, across every employer. Exposed as its own
    column (not used for banding) — someone with a prior career before
    joining as, say, an Associate Consultant would otherwise look
    "senior" by total years even on day one of an entry-level role."""
    starts = []
    for job in props.get("workHistory") or []:
        if is_intern_title(job.get("title") or ""):
            continue
        frm = (job.get("dates") or {}).get("from")
        d = parse_partial_date(frm)
        if d:
            starts.append(d)
    if not starts:
        return None
    now = now or datetime.now()
    return (now - min(starts)).days / 365.25


def role_tenure_years(start_date_str, now=None):
    """Years since the start of the CURRENT role at the queried firm — this
    is what seniority banding is actually based on, not total career
    experience (see years_experience)."""
    d = parse_partial_date(start_date_str)
    if not d:
        return None
    now = now or datetime.now()
    return (now - d).days / 365.25


def is_incoming_title(title):
    return bool(re.search(r"\bincoming\b", title or "", re.IGNORECASE))


def is_non_target_role(title, title_filters):
    t = (title or "").lower()
    return any(role.lower() in t for role in (title_filters or {}).get("non_target_roles") or [])


def is_leave_title(title, title_filters):
    t = (title or "").lower()
    return any(ind.lower() in t for ind in (title_filters or {}).get("leave_indicators") or [])


def is_different_track(title, title_filters):
    """A real role at the firm, but not the consulting track this campaign targets (BCG X, Forward Deployed
    AI Engineer, Data Scientist, ...). Flagged, never dropped: kept in the CSV for a future, different-purpose
    run, just excluded from seniority_band == in_band so this campaign's drafting stage skips it."""
    t = (title or "").lower()
    return any(role.lower() in t for role in (title_filters or {}).get("different_track_roles") or [])


def is_temporary_role(title, title_filters):
    """A short-term secondment or exchange placement (Visiting Associate, Visiting Scholar, ...) — a real
    role, but not the standing headcount this campaign is reading tenure/hierarchy facts against."""
    t = (title or "").lower()
    return any(role.lower() in t for role in (title_filters or {}).get("temporary_roles") or [])


def entry_titles_for_firm(firm, firm_config):
    return firm_entry(firm, firm_config).get("entry_titles") or []


def is_known_entry_title(title, entry_titles):
    t = (title or "").lower()
    return any(et.lower() in t for et in entry_titles)


def compute_seniority_band(title, props, min_years, max_years, current_role_start, entry_titles, title_filters):
    if is_non_target_role(title, title_filters):
        return "excluded_role", f"non-target/support role, not a recruiting-relevant function ({title!r})"
    if is_temporary_role(title, title_filters):
        return "excluded_role", f"short-term secondment/exchange title, not a standing role ({title!r})"
    if is_different_track(title, title_filters):
        return "different_track", f"a real role, but a different track from this campaign ({title!r}) — flagged, not dropped"
    if is_incoming_title(title):
        return "incoming", f"not yet started ({title!r}) — can't refer internally, still worth contacting"
    if is_intern_title(title):
        return "below", f"intern-type title ({title!r})"
    if seniority_flag(title) == "SENIOR":
        return "above", f"senior title keyword ({title!r})"
    if is_known_entry_title(title, entry_titles):
        return "in_band", f"known entry-level title for this firm ({title!r}), authoritative regardless of prior experience"

    tenure = role_tenure_years(current_role_start)
    if tenure is not None:
        years, basis = tenure, f"~{tenure:.1f}y tenure in current role at this firm"
    else:
        years = years_experience(props)
        basis = (
            f"~{years:.1f}y total career experience (no confirmed current-role start date to base tenure on)"
            if years is not None else None
        )

    if years is None:
        return "below", "no workHistory dates available to estimate tenure"
    if years < min_years:
        return "below", f"{basis} (< {min_years})"
    if years > max_years:
        return "above", f"{basis} (> {max_years})"
    return "in_band", basis


def check_school(props, text, school):
    for edu in props.get("educationHistory") or []:
        inst = (edu.get("institution") or {}).get("name")
        if inst and fuzzy_contains(inst, school):
            degree = edu.get("degree") or ""
            evidence = f"[structured] educationHistory: {degree} — {inst}".strip(" —")
            return "yes", evidence
    snippet = find_snippet(text, school)
    if snippet:
        return "unverified", snippet
    return "no", ""


def job_is_current(job):
    """True/False if workHistory[].dates tells us; None if we don't have enough info."""
    dates = job.get("dates")
    if not dates:
        return None
    if "to" in dates:
        return not dates.get("to")
    return None


def firm_entry(firm, firm_config):
    return firm_config.get(firm.strip().lower()) or {}


def excludes_for_firm(firm, firm_config):
    return firm_entry(firm, firm_config).get("exclude") or []


def company_matches_firm(candidate_name, firm, firm_config):
    """Fuzzy match, but an excluded name (e.g. "Bain Capital" excluded from
    "Bain & Company") is checked FIRST and blocks the match even though it
    would otherwise pass fuzzy_contains — generalizes to any confusable
    pair (Morgan Stanley/JPMorgan, Wells Fargo/Wells Fargo Advisors, etc.)
    via patterns.json's per-firm "exclude" list, same shape as org excludes."""
    if not candidate_name:
        return False
    for excl in excludes_for_firm(firm, firm_config):
        if fuzzy_contains(candidate_name, excl):
            return False
    return fuzzy_contains(candidate_name, firm)


def check_firm(props, text, firm, firm_config, title_filters):
    """Returns (firm_match, title, evidence, current_role_start_date).
    firm_match is "yes" (confirmed ongoing), "on_leave" (ongoing per dates,
    but the title itself says otherwise — "MBA Educational Leave" and
    similar are not "currently in role" even though LinkedIn shows no end
    date), "past", "mentioned_only", or "no"."""
    work = props.get("workHistory") or []
    matches = [(idx, job) for idx, job in enumerate(work)
               if company_matches_firm((job.get("company") or {}).get("name"), firm, firm_config)]
    if matches:
        # Prefer a match we can confirm is ongoing via dates.to == null. A promotion is often two workHistory
        # entries at the same firm, both left open-ended by a stale/incomplete profile, so more than one can
        # be "current" — pick the one with the LATEST start date, not just the first hit in list order (that
        # bug let a stale earlier title like "Consultant" outrank the person's actual current "Principal").
        current = [(idx, job) for idx, job in matches if job_is_current(job) is True]
        if current:
            idx, job = max(current, key=lambda ij: parse_partial_date((ij[1].get("dates") or {}).get("from")) or datetime.min)
            title = job.get("title") or ""
            company = (job.get("company") or {}).get("name")
            start = (job.get("dates") or {}).get("from")
            if is_leave_title(title, title_filters):
                evidence = f"[structured] workHistory[{idx}] title indicates leave/non-active status: {title} at {company}"
                return "on_leave", title, evidence, start
            evidence = f"[structured] workHistory[{idx}] (dates.to is null, i.e. ongoing, latest start of {len(current)} open entries): {title} at {company}"
            return "yes", title, evidence, start
        # No confirmed end date anywhere: fall back to "most recent entry = current"
        # (LinkedIn-style profiles list experience newest-first).
        idx0, job0 = matches[0]
        title = job0.get("title") or ""
        company = (job0.get("company") or {}).get("name")
        start = (job0.get("dates") or {}).get("from")
        if idx0 == 0 and job_is_current(job0) is None:
            if is_leave_title(title, title_filters):
                evidence = f"[structured] workHistory[0] title indicates leave/non-active status: {title} at {company}"
                return "on_leave", title, evidence, start
            evidence = f"[structured] workHistory[0] (most recent entry, no date data): {title} at {company}"
            return "yes", title, evidence, start
        evidence = f"[structured] workHistory[{idx0}]: {title} at {company}"
        return "past", title, evidence, start
    snippet = find_snippet(text, firm)
    if snippet:
        return "mentioned_only", "", snippet, None
    return "no", "", "", None


def firm_stint_start(props, firm, firm_config):
    """Start date of the person's CURRENT continuous, non-intern stint at `firm`, or None.
    A promotion is often a second workHistory entry at the same firm, so the start of the newest entry
    would understate how long they have been there. Chain back through same-firm entries that ended
    within about three months of the next one starting. Internships never count."""
    spans = []
    for job in props.get("workHistory") or []:
        name = (job.get("company") or {}).get("name")
        if not company_matches_firm(name, firm, firm_config) or is_intern_title(job.get("title") or ""):
            continue
        dates = job.get("dates") or {}
        start, end = parse_partial_date(dates.get("from")), parse_partial_date(dates.get("to"))
        if start:
            spans.append((start, end))
    current = [s for s in spans if s[1] is None]
    if not current:
        return None
    earliest = min(s[0] for s in current)
    for start, end in sorted((s for s in spans if s[1] is not None), key=lambda s: s[0], reverse=True):
        if end >= earliest - timedelta(days=92):
            earliest = min(earliest, start)
    return earliest


# A title that means the person built or led something of their own, or a student-org role, rather than being
# employed there. Such a job is never offered as a "prior employer" hook. "Vice President" is a real job title
# at a bank, so only a bare "President" is caught.
NOT_EMPLOYEE_TITLE_RE = re.compile(
    r"\b(founder|co-?founder|owner|self[- ]employed|freelance|volunteer|ambassador|member|captain|chief)\b|"
    r"(?<!vice )(?<!senior vice )(?<!executive vice )\bpresident\b", re.I)


def verified_prior_employer(props, firm, firm_config, stint, org_names=None):
    """(employer name, source string) for a prior employer that is a VERIFIED professional role, else ("", "").
    Verified means all of: the company has a LinkedIn company id in the structured work history, the title is
    an employee title (not founder/owner/president/member/volunteer), the stint lasted a year or more and
    ended by the time they joined this firm, the name is not a school, club, society or the person's own
    name, and it is not the firm itself. Unverified means fall through to the next template, never guess."""
    if not stint:
        return "", ""
    person_tokens = [t for t in re.findall(r"[A-Za-z]+", props.get("name") or "") if len(t) > 2]
    best = None
    for job in props.get("workHistory") or []:
        company = job.get("company") or {}
        name, title = company.get("name"), job.get("title") or ""
        if not name or not company.get("id"):
            continue
        if company_matches_firm(name, firm, firm_config) or is_intern_title(title) or NOT_EMPLOYEE_TITLE_RE.search(title):
            continue
        if org_names and any(fuzzy_contains(name, o) for o in org_names):
            continue
        if any(t.lower() in name.lower() for t in person_tokens) or not variants.usable_company_name(name, firm):
            continue
        dates = job.get("dates") or {}
        start, end = parse_partial_date(dates.get("from")), parse_partial_date(dates.get("to"))
        if not start or not end or end > stint + timedelta(days=92) or (end - start).days < 330:
            continue
        if best is None or end > best[0]:
            best = (end, name, f"workHistory: {title} at {name}, {dates.get('from')} to {dates.get('to')}, company id {company['id']}")
    return (best[1], best[2]) if best else ("", "")


def sender_org_match(text, props):
    """(spoken org name, evidence) if the person is in one of the orgs the sender is really in (profile "orgs"),
    matched as a whole item in workHistory, Activities and Societies, or the Organizations section. Only a
    "yes" counts; the free-text "unverified" fallback is never enough to claim a shared org."""
    orgs = variants.load_sender_orgs()
    for o in orgs:
        cfg = {o["name"].strip().lower(): {"variants": o["match"]}}
        verdict, evidence, _ = check_affiliation(text or "", props, [o["name"]], cfg)
        if verdict == "yes":
            return o["name"], evidence
    return "", ""


def extract_facts(props, school, firm, firm_config, org_names=None, text=""):
    """Real, structured facts that pick a draft template. Every fact carries the field it came from (a *_source
    column) so the choice can be audited. Anything not clearly supported by the data is left blank, and a blank
    fact simply means that template is not offered for this person."""
    facts = {"high_school": "", "high_school_source": "", "firm_start_year": "", "firm_start_source": "",
             "school_end_year": "", "school_degree": "", "school_degree_source": "", "home_city": "",
             "home_city_source": "",
             "prior_employer": "", "prior_employer_source": "", "shared_org": "", "shared_org_source": ""}
    sender_hs = sender_profile.field(variants.PROFILE, "high_school")
    for edu in props.get("educationHistory") or []:
        inst = (edu.get("institution") or {}).get("name")
        if sender_hs and inst and fuzzy_contains(inst, sender_hs):
            facts["high_school"] = "yes"
            facts["high_school_source"] = f"educationHistory: {inst!r}"
            break
    location = props.get("location") or ""
    home_city = sender_profile.field(variants.PROFILE, "home_city")
    if home_city and re.search(r"\b" + re.escape(home_city) + r"\b", location, re.I):
        facts["home_city"] = "yes"
        facts["home_city_source"] = f"location: {location!r}"
    stint = firm_stint_start(props, firm, firm_config)
    if stint:
        facts["firm_start_year"] = stint.year
        facts["firm_start_source"] = f"workHistory: current stint at {firm} began {stint.isoformat()[:7]}"
    ends, degrees = [], []
    for edu in props.get("educationHistory") or []:
        inst = (edu.get("institution") or {}).get("name")
        if inst and school and fuzzy_contains(inst, school):
            d = parse_partial_date((edu.get("dates") or {}).get("to"))
            if d:
                ends.append(d.year)
            if edu.get("degree"):
                degrees.append((edu["degree"], inst))
    if ends:
        facts["school_end_year"] = min(ends)
    if degrees:
        major_re = variants.major_regex()
        same_major = [d for d in degrees if major_re and major_re.search(d[0])]
        deg, inst = (same_major or degrees)[0]
        facts["school_degree"] = deg
        facts["school_degree_source"] = f"educationHistory: {inst}, degree {deg!r}"
    facts["prior_employer"], facts["prior_employer_source"] = verified_prior_employer(
        props, firm, firm_config, stint, org_names)
    facts["shared_org"], facts["shared_org_source"] = sender_org_match(text, props)
    return facts


def reapply_title_band(row):
    """Old result files carry a band computed under older title rules. Re-apply the title-based senior
    rule so a stale CSV cannot keep someone in band that the current rules would exclude. Only ever
    downgrades in_band; never promotes."""
    if row.get("seniority_band") == "in_band" and seniority_flag(row.get("title")) == "SENIOR":
        row["seniority_band"] = "above"
        row["seniority_flag"] = "SENIOR"
        row["evidence"] = (row.get("evidence") or "") + f" || Seniority (re-applied): senior title keyword ({row.get('title')!r})"
        return True
    return False


def strip_org_suffixes(item):
    """Strip a trailing "- <Chapter Name> Chapter" segment and trailing
    "Fraternity"/"Sorority"/"Inc."/etc words, so "Delta Omega Fraternity",
    "Delta Omega - Delta Phi Chapter", and "Delta Omega" all compare equal
    as whole strings."""
    s = (item or "").strip().rstrip(".")
    s = re.sub(r"\s*[-–—]\s*[\w\s]+\bchapter\b\.?$", "", s, flags=re.IGNORECASE).strip()
    changed = True
    while changed:
        changed = False
        for suffix in ORG_SUFFIX_WORDS:
            new_s = re.sub(r"\s+" + re.escape(suffix) + r"\.?$", "", s, flags=re.IGNORECASE).strip()
            if new_s != s:
                s = new_s
                changed = True
    return s


def whole_item_matches_org(item, org, org_config):
    """Whole-string match only — "Phi Delta Omega" must NOT match "Kappa
    Sigma" just because it contains it as a substring."""
    norm_item = normalize(strip_org_suffixes(item))
    if not norm_item:
        return False
    for excl in excludes_for_org(org, org_config):
        if norm_item == normalize(strip_org_suffixes(excl)):
            return False
    for variant in variants_for_org(org, org_config):
        if norm_item == normalize(strip_org_suffixes(variant)):
            return True
    return False


def split_activity_items(section):
    """Activities and Societies lines are sometimes comma-separated
    ("Delta Omega, Club Rowing") and sometimes dash-bulleted
    ("- Delta Omega Fraternity - Beta Gamma Sigma"). Split on both, but not
    on a hyphen with no surrounding whitespace (keeps compound words like
    "Alpha-Kappa" or "Co-Chair" intact)."""
    padded = " " + (section or "")
    parts = re.split(r",|\s[-–—]+\s*", padded)
    return [p.strip() for p in parts if p.strip()]


def extract_organizations_items(text):
    """Pulls org names out of a LinkedIn "## Organizations" section, e.g.
    "### Grand Master (President) at Delta Omega May 2021 - May 2024" ->
    "Delta Omega". This is a real, separate LinkedIn profile section from
    Activities and Societies (self-reported org membership, not tied to a
    school's education entry)."""
    m = ORGANIZATIONS_SECTION_RE.search(text or "")
    if not m:
        return []
    items = []
    for line in re.findall(r"###\s*([^\n]+)", m.group(1)):
        line = line.strip()
        if line.lower().startswith("at "):
            org_part = line[3:]
        elif " at " in line:
            org_part = line.rsplit(" at ", 1)[1]
        else:
            org_part = line
        org_part = ORG_ITEM_DATE_TAIL_RE.sub("", org_part).strip()
        if org_part:
            items.append(org_part)
    return items


def has_corroboration(text, start, end, window=40):
    """A raw text mention only counts (even as "unverified") if a word like
    "fraternity"/"chapter"/"brother"/"pledge", or a Greek chapter-name
    pattern like "Alpha-Kappa", sits within ~40 chars. Without this, "D Omega"
    or similar short variants surface unrelated things (e.g. "KS" hitting a
    Kansas location) with no actual connection to the org."""
    ctx = text[max(0, start - window):min(len(text), end + window)]
    ctx_lower = ctx.lower()
    if any(w in ctx_lower for w in CORROBORATION_WORDS):
        return True
    return bool(GREEK_CHAPTER_PATTERN_RE.search(ctx))


def find_corroborated_phrase(text, variant, excludes):
    """Whole-phrase match (never a substring of a longer excluded org name)
    that also has a corroborating word/pattern nearby."""
    for m in re.finditer(r"\b" + re.escape(variant) + r"\b", text, re.IGNORECASE):
        start, end = m.start(), m.end()
        if any(
            em.start() <= start and em.end() >= end
            for excl in excludes
            for em in re.finditer(re.escape(excl), text, re.IGNORECASE)
        ):
            continue
        if not has_corroboration(text, start, end):
            continue
        return m
    return None


def check_affiliation(text, props, orgs, org_config):
    """Structured-first, across three real LinkedIn sources, in order of
    reliability:
      1. workHistory company names — Exa's fully-structured entity data;
         people often list a fraternity role as work "experience".
      2. The Activities and Societies line under Education.
      3. The separate "## Organizations" profile section.
    All three are whole-item matched (never substring — "Phi Delta Omega"
    must not match "Delta Omega"). Only if none of those hit do we fall back
    to a free-text search, which requires a corroborating word nearby and is
    marked "unverified" rather than "yes"."""
    if not text or not orgs:
        return "no", "", None

    structured_items = []
    for job in props.get("workHistory") or []:
        company = (job.get("company") or {}).get("name")
        if company:
            structured_items.append((company, "workHistory company"))

    m = ACTIVITIES_RE.search(text)
    if m:
        for item in split_activity_items(m.group(1)):
            structured_items.append((item, "Activities and Societies"))

    for item in extract_organizations_items(text):
        structured_items.append((item, "Organizations section"))

    for org in orgs:
        for item, source in structured_items:
            if whole_item_matches_org(item, org, org_config):
                return "yes", f"[{source}] {item.strip()!r}", org

    for org in orgs:
        excludes = excludes_for_org(org, org_config)
        for variant in variants_for_org(org, org_config):
            hit = find_corroborated_phrase(text, variant, excludes)
            if hit:
                start, end = max(0, hit.start() - 90), min(len(text), hit.end() + 90)
                snippet = re.sub(r"\s+", " ", text[start:end]).strip()
                return "unverified", f"...{snippet}...", org

    return "no", "", None


def compute_tier(school_match, affiliation_match):
    school_ok = school_match == "yes"
    org_ok = affiliation_match == "yes"
    if school_ok and org_ok:
        return 1
    if org_ok and not school_ok:
        return 2
    if school_ok and not org_ok:
        return 3
    return 4


def effective_tier(row):
    """tier if present and parseable, else derived from school_match /
    affiliation_match. Needed for rows loaded via --from-csv from a CSV
    that predates universal tier assignment (tier used to only get
    computed when --org was passed, so an old school-only CSV has no tier
    column at all)."""
    tier = row.get("tier")
    if tier not in (None, ""):
        try:
            return int(tier)
        except (TypeError, ValueError):
            pass
    return compute_tier(row.get("school_match"), row.get("affiliation_match") or "no")


# --------------------------------------------------------------------------
# Exa API
# --------------------------------------------------------------------------

def build_query_plain(school, firm, city):
    q = f"{school} alumni now working at {firm}"
    if city:
        q += f" based in {city}"
    return q


def build_query_school_org(school, org, firm, city):
    q = f"{school} alumni who are members of {org} now working at {firm}"
    if city:
        q += f" based in {city}"
    return q


def build_query_org_only(org, firm, city):
    q = f"Members of {org} now working at {firm}"
    if city:
        q += f" based in {city}"
    return q


def dedupe_key(result):
    """Same profile can come back as https://www.linkedin.com/... in one
    search and https://linkedin.com/... (no www) in another; normalize so
    they collapse into one row instead of a duplicate."""
    url = result.get("url") or result.get("id") or ""
    return re.sub(r"^https?://(www\.)?", "", url).rstrip("/").lower()


def name_firm_key(name, firm):
    """Secondary dedup key for after URL-based dedup: the same person can
    come back under two different LinkedIn URL formats that don't collapse
    via dedupe_key (e.g. a vanity slug vs. a numeric profile ID)."""
    return (normalize(name), normalize(firm))


def linkedin_search_url(name, firm):
    """A constructed LinkedIn people-search URL as a fallback — the direct
    profile URL Exa returns can be stale even when the rest of the row's
    data is correct. Never HTTP-validated: LinkedIn blocks unauthenticated
    requests, so a validation attempt would be meaningless noise, not signal."""
    query = f"{name} {firm}".strip()
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(query)}"


def exa_search(api_key, query, n, max_retries=3):
    body = {
        "query": query,
        "type": "auto",
        "category": "people",
        "numResults": n,
        "contents": {"text": True},
    }
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(EXA_SEARCH_URL, json=body, headers=headers, timeout=30)
        except requests.RequestException as e:
            last_err = e
            time.sleep(2 * attempt)
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504):
            last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            time.sleep(2 * attempt)
            continue
        raise RuntimeError(f"Exa API error {resp.status_code}: {resp.text[:500]}")

    raise RuntimeError(f"Exa API request failed after {max_retries} attempts: {last_err}")


# --------------------------------------------------------------------------
# Stage 2: email pattern discovery + address construction + verification
# --------------------------------------------------------------------------

PATTERNS_PATH = Path(__file__).resolve().parent / "patterns.json"
HUNTER_DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"
HUNTER_ACCOUNT_URL = "https://api.hunter.io/v2/account"
ZEROBOUNCE_VALIDATE_URL = "https://api.zerobounce.net/v2/validate"
ZEROBOUNCE_CREDITS_URL = "https://api.zerobounce.net/v2/getcredits"

PATTERN_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def load_patterns_cache():
    """firm -> {firm, web_domain, mail_domain, pattern, confidence,
    how_confirmed, date}. This is the compounding asset: every firm is
    looked up via Hunter at most once, ever.

    web_domain vs. mail_domain: a firm's website domain and its actual mail
    domain are not always the same (Goldman Sachs: web goldmansachs.com,
    mail gs.com — Hunter's company-name search resolves the website domain,
    which is not necessarily where mailboxes live). Addresses are ALWAYS
    constructed from mail_domain, never web_domain.

    A "manual" entry (how_confirmed starting with "manual:") is a
    human-confirmed mail_domain and is never overwritten by a Hunter
    lookup — Hunter is only used to fill in whatever is still missing
    (pattern, confidence, web_domain)."""
    if PATTERNS_PATH.exists():
        try:
            return json.loads(PATTERNS_PATH.read_text())
        except json.JSONDecodeError:
            print(f"WARNING: could not parse {PATTERNS_PATH}, starting a fresh cache.", file=sys.stderr)
    return {}


def save_patterns_cache(cache):
    PATTERNS_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")


def is_manually_confirmed(entry):
    return (entry.get("how_confirmed") or "").startswith("manual:")


def get_or_fetch_pattern(firm, cache, hunter_key, usage, email_domain_override=None):
    """Cache-first. Only ever calls Hunter's domain-search once per firm,
    ever — never per person. A manually-confirmed mail_domain is never
    overwritten by Hunter; Hunter only fills in gaps (pattern, web_domain,
    confidence)."""
    key = firm.strip().lower()
    entry = cache.get(key)

    if email_domain_override:
        if entry is None:
            entry = {
                "firm": firm, "web_domain": None, "mail_domain": None,
                "pattern": None, "confidence": None, "how_confirmed": "", "date": "",
            }
        entry["mail_domain"] = email_domain_override
        entry["how_confirmed"] = f"manual: --email-domain override ({datetime.now().strftime('%Y-%m-%d')})"
        entry["date"] = datetime.now().strftime("%Y-%m-%d")
        cache[key] = entry
        save_patterns_cache(cache)

    if entry and entry.get("pattern") and entry.get("mail_domain"):
        return entry  # fully resolved already, no Hunter call needed

    resp = requests.get(
        HUNTER_DOMAIN_SEARCH_URL, params={"company": firm, "api_key": hunter_key}, timeout=30
    )
    usage["hunter_searches"] += 1

    if entry is None:
        entry = {
            "firm": firm, "web_domain": None, "mail_domain": None,
            "pattern": None, "confidence": None, "how_confirmed": "", "date": "",
        }

    if resp.status_code != 200:
        if not entry.get("how_confirmed"):
            entry["how_confirmed"] = f"Hunter domain-search failed: HTTP {resp.status_code}: {resp.text[:200]}"
        cache[key] = entry
        save_patterns_cache(cache)
        return entry

    payload = resp.json()
    data = payload.get("data") or {}
    emails = data.get("emails") or []
    confidences = [e.get("confidence") for e in emails if e.get("confidence") is not None]
    avg_confidence = round(sum(confidences) / len(confidences), 1) if confidences else None
    total_found = (payload.get("meta") or {}).get("results", 0)
    hunter_note = (
        f"Hunter domain-search (company={firm!r}): {len(emails)} sample emails "
        f"(of {total_found} on file), avg confidence {avg_confidence}, pattern={data.get('pattern')!r}"
    )

    entry["web_domain"] = data.get("domain")
    if not entry.get("mail_domain"):
        # No manually-confirmed mail domain on file — fall back to Hunter's
        # website domain as the best available guess, clearly labeled as
        # unverified so the quota guard can catch a wrong guess quickly
        # rather than assume it's right the way the original bug did.
        entry["mail_domain"] = data.get("domain")
        entry["how_confirmed"] = f"{hunter_note} (mail_domain UNVERIFIED, same as web_domain guess)"
    else:
        entry["how_confirmed"] = (entry.get("how_confirmed") or "") + f" | {hunter_note}"
    if not entry.get("pattern"):
        entry["pattern"] = data.get("pattern")
        entry["confidence"] = avg_confidence
    entry["date"] = datetime.now().strftime("%Y-%m-%d")

    cache[key] = entry
    save_patterns_cache(cache)
    return entry


def fold_to_ascii(s):
    """Accented characters (José -> Jose) — corporate email systems almost
    always strip diacritics."""
    nfkd = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def clean_local_part_piece(s):
    """Lowercase, ASCII-folded, letters and hyphens only (hyphenated names
    like "Mary-Kate" or "Garcia-Lopez" keep their hyphen, as confirmed
    against a real Hunter-returned address of that shape).
    Apostrophes and spaces are dropped, matching typical corporate mail
    system behavior."""
    return re.sub(r"[^a-z\-]", "", fold_to_ascii(s).lower())


def split_person_name(name):
    """First token = first name, LAST token = surname. Middle tokens (if
    any) are dropped — we have no reliable middle-initial data, and Hunter
    doesn't document a middle-initial placeholder, so a pattern that needs
    one fails cleanly in construct_email rather than guessing."""
    parts = (name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def construct_email(name, pattern, domain):
    """Builds exactly ONE address from the cached pattern — never
    permutations. Returns (email_or_None, reason_if_none)."""
    if not pattern or not domain:
        return None, "no pattern/domain available for this firm"

    first_raw, last_raw = split_person_name(name)
    if not first_raw or not last_raw:
        return None, f"could not split {name!r} into first/last name"

    first, last = clean_local_part_piece(first_raw), clean_local_part_piece(last_raw)
    if not first or not last:
        return None, f"name {name!r} had no usable characters after normalization"

    values = {"first": first, "last": last, "f": first[0], "l": last[0]}

    missing = [tok for tok in PATTERN_PLACEHOLDER_RE.findall(pattern) if tok not in values]
    if missing:
        return None, (
            f"pattern {pattern!r} uses unsupported placeholder(s) {missing} "
            f"(e.g. a middle initial we have no data for) — not guessing"
        )

    local = PATTERN_PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], pattern)
    if not local:
        return None, f"pattern {pattern!r} produced an empty address"
    return f"{local}@{domain}", None


def zerobounce_validate(email, zb_key, usage, max_retries=3):
    """Verifies ONE constructed address. Records ZeroBounce's status
    verbatim (valid / invalid / catch-all / unknown / spamtrap / abuse /
    do_not_mail) — catch-all is never collapsed into valid. Also returns
    whether ZeroBounce considers the domain catch-all, needed both for the
    quota guard (an "invalid" verdict is only trustworthy signal on a
    non-catch-all domain) and the collision-vs-wrong-domain distinction."""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(
                ZEROBOUNCE_VALIDATE_URL, params={"api_key": zb_key, "email": email}, timeout=30
            )
        except requests.RequestException as e:
            last_err = e
            time.sleep(2 * attempt)
            continue
        usage["zerobounce_verifications"] += 1
        if resp.status_code == 200:
            data = resp.json()
            return data.get("status") or "unknown", bool(data.get("catchall_domain")), None
        if resp.status_code in (429, 500, 502, 503, 504):
            last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            time.sleep(2 * attempt)
            continue
        return None, None, f"ZeroBounce error {resp.status_code}: {resp.text[:200]}"
    return None, None, f"ZeroBounce request failed after {max_retries} attempts: {last_err}"


def fetch_hunter_account_usage(hunter_key):
    try:
        resp = requests.get(HUNTER_ACCOUNT_URL, params={"api_key": hunter_key}, timeout=15)
        resp.raise_for_status()
        reqs = (resp.json().get("data") or {}).get("requests") or {}
        return reqs.get("searches") or {}, reqs.get("verifications") or {}
    except requests.RequestException as e:
        print(f"  WARNING: could not fetch Hunter account usage: {e}", file=sys.stderr)
        return {}, {}


def fetch_zerobounce_credits(zb_key):
    try:
        resp = requests.get(ZEROBOUNCE_CREDITS_URL, params={"api_key": zb_key}, timeout=15)
        resp.raise_for_status()
        return resp.json().get("Credits")
    except requests.RequestException as e:
        print(f"  WARNING: could not fetch ZeroBounce credits: {e}", file=sys.stderr)
        return None


def enrich_row_with_email(row, pattern_entry, zb_key, usage):
    """Mutates row in place: adds email, email_status, email_pattern_confidence.
    Returns (attempted_verification: bool, status_or_None, is_catchall_or_None)
    so the caller can run the per-firm quota guard."""
    row["email_pattern_confidence"] = pattern_entry.get("confidence")

    email, reason = construct_email(row["name"], pattern_entry.get("pattern"), pattern_entry.get("mail_domain"))
    if not email:
        row["email"] = ""
        row["email_status"] = ""
        row["evidence"] += f" || Email: not constructed ({reason})"
        return False, None, None

    row["email"] = email
    status, is_catchall, err = zerobounce_validate(email, zb_key, usage)
    if err:
        row["email_status"] = ""
        row["evidence"] += f" || Email: {email} (verification failed: {err})"
        return False, None, None

    if status == "invalid" and not is_catchall and is_manually_confirmed(pattern_entry):
        # Domain is human-confirmed correct and non-catch-all, so a
        # well-formed invalid guess is more likely a name collision (Goldman
        # adds a middle initial when first.last is already taken) than a
        # wrong pattern or domain.
        status = "invalid_possible_collision"

    row["email_status"] = status
    row["evidence"] += f" || Email: {email} (ZeroBounce status: {status}, catch-all domain: {is_catchall})"
    return True, status, is_catchall


# --------------------------------------------------------------------------
# Row building
# --------------------------------------------------------------------------

def best_fallback_title(props, orgs, org_config):
    """workHistory[0] isn't always a real job — Exa's entity extraction
    treats a self-reported fraternity/club officer role (e.g. "Head of
    Recruitment" at "Delta Omega Fraternity") as work experience too, and if
    it's the most recent entry it would otherwise get used as the person's
    professional title and skew their seniority classification. Skip
    entries whose "company" is one of the orgs being searched for."""
    for job in props.get("workHistory") or []:
        company = (job.get("company") or {}).get("name")
        if orgs and company and any(whole_item_matches_org(company, org, org_config) for org in orgs):
            continue
        title = job.get("title")
        if title:
            return title
    return ""


def check_title_match(title, title_terms):
    """Loose substring, case-insensitive — "Consultant" catches "Senior
    Consultant". Returns None (not "no") when no --title filter is active,
    so build_row can tell "not applicable" from "checked and didn't match"."""
    if not title_terms:
        return None
    t = (title or "").lower()
    return "yes" if any(tf.lower() in t for tf in title_terms) else "no"


def build_row(result, school, firm, orgs, org_config, firm_config, min_seniority, max_seniority,
              title_config, title_terms=None):
    props = get_person_props(result)
    text = result.get("text") or ""

    name = guess_name(result, props)
    url = result.get("url") or ""

    school_match, school_evidence = check_school(props, text, school)
    firm_match, firm_title, firm_evidence, firm_start = check_firm(props, text, firm, firm_config, title_config)
    title = firm_title or best_fallback_title(props, orgs, org_config) or guess_title_from_text(text)
    flag = seniority_flag(title)
    entry_titles = entry_titles_for_firm(firm, firm_config)
    band, band_evidence = compute_seniority_band(
        title, props, min_seniority, max_seniority, firm_start, entry_titles, title_config
    )
    total_years = years_experience(props)

    evidence_parts = [
        f"School: {school_evidence or 'no match found'}",
        f"Firm: {firm_evidence or 'no match found'}",
        f"Seniority: {band_evidence}",
    ]

    row = {
        "name": name,
        "firm_queried": firm,
        "linkedin_url": url,
        "linkedin_search_url": linkedin_search_url(name, firm),
        "city": props.get("location") or "",
        "school_match": school_match,
        "firm_match": firm_match,
        "title": title,
        "seniority_flag": flag,
        "seniority_band": band,
        "total_experience_years": round(total_years, 1) if total_years is not None else "",
    }

    # Tier is a universal concept (school-only = tier 3, org-only = tier 2,
    # both = tier 1, neither = tier 4) independent of whether --org was
    # passed — a run with no --org is the canonical tier-3 case, not tier 4.
    # It used to only get computed "if orgs", which meant every school-only
    # row silently had no tier at all and drafts could never find a hook.
    if orgs:
        affiliation_match, aff_evidence, matched_org = check_affiliation(text, props, orgs, org_config)
        org_label = matched_org or "/".join(orgs)
        evidence_parts.append(f"Affiliation ({org_label}): {aff_evidence or 'no match found'}")
    else:
        affiliation_match, matched_org = "no", None
    facts = extract_facts(props, school, firm, firm_config, orgs, text)
    row.update(facts)
    row["facts_checked"] = FACTS_VERSION
    row["affiliation_match"] = affiliation_match
    row["tier"] = compute_tier(school_match, affiliation_match)
    row["_matched_org"] = matched_org  # internal only, not a CSV column — needed for draft hook lines

    if title_terms:
        row["title_match"] = check_title_match(title, title_terms)

    row["evidence"] = " || ".join(evidence_parts)
    return row


# --------------------------------------------------------------------------
# Stage 2 + 3 orchestration
# --------------------------------------------------------------------------

QUOTA_GUARD_MIN_SAMPLE = 5
QUOTA_GUARD_INVALID_RATE = 0.70


TEMPLATE_FACT_KEYS = ("high_school", "high_school_source", "firm_start_year", "firm_start_source", "school_end_year",
                      "school_degree", "school_degree_source", "home_city", "home_city_source", "prior_employer",
                      "prior_employer_source", "shared_org", "shared_org_source")


def row_facts(row):
    return {k: row.get(k) or "" for k in TEMPLATE_FACT_KEYS}


def row_template(row):
    """The hierarchy's pick for one row (variants.select_template), from the facts on the row."""
    return variants.select_template(row_facts(row))


def render_row_draft(row, orgs, target_school, sender_first, args, choice=None):
    """(subject, body) for one row, or (None, None) when the tier has no hook. Raises DraftContentError on a
    content-rule violation, or when the templates' "before joining" order cannot be verified."""
    tier = effective_tier(row)
    if tier == 3:
        fy, sy = str(row.get("firm_start_year") or ""), str(row.get("school_end_year") or "")
        if fy.isdigit() and sy.isdigit() and int(sy) > int(fy):
            raise DraftContentError(
                f"school ended ({sy}) after they joined the firm ({fy}), so 'before joining' is unverified")
    recipient_first = split_person_name(row["name"])[0]
    org_for_hook = row.get("_matched_org") or (orgs[0] if orgs else None)
    return render_draft(
        recipient_first_name=recipient_first,
        tier=tier,
        target_school=target_school,
        org=org_for_hook,
        firm=row["firm_queried"],
        sender_first_name=sender_first,
        sender_school=args.sender_school,
        sender_year=args.sender_year,
        sender_industry=args.sender_industry,
        sender_signature=getattr(args, "sender_signature", None),
        school_short=getattr(args, "school_short", None),
        facts=row_facts(row),
        choice=choice or (row_template(row) if tier == 3 else None),
    )


def render_and_store_draft(row, orgs, target_school, sender_first, args):
    """Renders + stores draft_subject/draft_body on row in place, plus which template was chosen, on what fact
    and from which field. Returns True if a draft was generated, False if skipped (logged in evidence)."""
    row["template_id"] = row["template_fact"] = row["template_source"] = ""
    try:
        subject, body = render_row_draft(row, orgs, target_school, sender_first, args)
    except DraftContentError as e:
        row["evidence"] += f" || Draft: skipped ({e})"
        return False

    if subject is None:
        row["evidence"] += " || Draft: skipped (no valid hook for this tier)"
        return False

    if effective_tier(row) == 3:
        t = row_template(row)
        row["template_id"], row["template_fact"], row["template_source"] = t["id"], t["fact"], t["source"]
    row["draft_subject"] = subject
    row["draft_body"] = body
    return True


def similarity_report(all_rows):
    """Warning only. Reports the highest pairwise similarity among drafts to the same email domain."""
    by_domain, top, over = {}, 0.0, 0
    for r in all_rows:
        if r.get("draft_body") and r.get("email"):
            by_domain.setdefault(variants.email_domain(r["email"]), []).append(r)
    rows = 0
    for group in by_domain.values():
        rows += len(group)
        for i, a in enumerate(group):
            for b in group[:i]:
                sim = variants.similarity(a["draft_subject"], a["draft_body"], (a["name"] or "").split()[0],
                                          b["draft_subject"], b["draft_body"], (b["name"] or "").split()[0])
                top = max(top, sim)
                over += sim >= variants.SIMILARITY_THRESHOLD
    return {"rows": rows, "domains": len(by_domain), "max_similarity": round(top, 3), "pairs_over": over}


def run_email_and_draft_stages(all_rows, orgs, target_school, args, cache, drafts_only=False):
    """Scoped to seniority_band == "in_band" only: that's the sole population
    that ever gets a draft, and Hunter/ZeroBounce free-tier quota is too
    scarce to spend verifying emails for people who will never receive one.
    Mutates rows in place; every row gets blank draft columns as a default
    (and blank email columns too, unless drafts_only) so the CSV writer
    never KeyErrors on out-of-band rows.

    drafts_only=True (--drafts-only) skips Hunter/ZeroBounce entirely and
    only (re-)renders drafts, preserving whatever email/email_status data
    is already on the rows — for re-running the draft stage on a result set
    that already has verified emails without spending any more quota.

    Otherwise processes in_band rows grouped by firm so the quota guard can
    operate per firm: once a firm's mail_domain looks wrong (or a lot of
    collisions are piling up), stop spending ZeroBounce calls on the rest
    of that firm's rows rather than burning through the whole batch to
    find out."""
    for row in all_rows:
        if not drafts_only:
            row["email"] = ""
            row["email_status"] = ""
            row["email_pattern_confidence"] = ""
        row["draft_subject"] = ""
        row["draft_body"] = ""

    from collections import Counter, defaultdict
    status_counts = Counter()
    constructed = 0
    drafts_generated = 0
    drafts_skipped = 0
    firms_guard_tripped = []
    in_band_rows = [r for r in all_rows if r["seniority_band"] == "in_band"]
    sender_first = split_person_name(args.sender_name)[0]

    if drafts_only:
        for row in in_band_rows:
            if row.get("email"):
                constructed += 1
            if row.get("email_status"):
                status_counts[row["email_status"]] += 1
            if render_and_store_draft(row, orgs, target_school, sender_first, args):
                drafts_generated += 1
            else:
                drafts_skipped += 1
        variation = similarity_report(all_rows)
        for row in all_rows:
            row.pop("_matched_org", None)
        return {
            "variation": variation,
            "attempted": len(in_band_rows),
            "constructed": constructed,
            "status_counts": dict(status_counts),
            "drafts_generated": drafts_generated,
            "drafts_skipped": drafts_skipped,
            "firms_guard_tripped": [],
            "hunter_searches_this_run": 0,
            "zerobounce_verifications_this_run": 0,
            "hunter_searches_remaining": None,
            "hunter_verifications_remaining": None,
            "zerobounce_credits_remaining": None,
        }

    hunter_key = load_hunter_key()
    zb_key = load_zerobounce_key()
    usage = {"hunter_searches": 0, "zerobounce_verifications": 0}

    rows_by_firm = defaultdict(list)
    for row in in_band_rows:
        rows_by_firm[row["firm_queried"]].append(row)

    for firm, firm_rows in rows_by_firm.items():
        pattern_entry = get_or_fetch_pattern(firm, cache, hunter_key, usage, args.email_domain)
        guard_tripped = False
        checked = 0
        invalid_like = 0

        for row in firm_rows:
            if guard_tripped:
                # Still construct locally (free) so the row shows what the
                # address would be, but never spend another ZeroBounce call.
                email, reason = construct_email(
                    row["name"], pattern_entry.get("pattern"), pattern_entry.get("mail_domain")
                )
                row["email"] = email or ""
                row["email_pattern_confidence"] = pattern_entry.get("confidence")
                if email:
                    constructed += 1
                    row["evidence"] += f" || Email: {email} (verification skipped — quota guard tripped)"
                else:
                    row["evidence"] += f" || Email: not constructed ({reason})"
            else:
                attempted, status, is_catchall = enrich_row_with_email(row, pattern_entry, zb_key, usage)
                if row["email"]:
                    constructed += 1
                if status:
                    status_counts[status] += 1
                if attempted:
                    checked += 1
                    if status in ("invalid", "invalid_possible_collision"):
                        invalid_like += 1
                    if (
                        checked >= QUOTA_GUARD_MIN_SAMPLE
                        and is_catchall is False
                        and (invalid_like / checked) > QUOTA_GUARD_INVALID_RATE
                    ):
                        guard_tripped = True
                        firms_guard_tripped.append(firm)
                        rate_pct = round(100 * invalid_like / checked)
                        if is_manually_confirmed(pattern_entry):
                            reason = (
                                f"mail_domain ({pattern_entry.get('mail_domain')}) is human-confirmed, so "
                                f"this looks like a wrong PATTERN or a lot of collisions/turnover, not a wrong domain"
                            )
                        else:
                            reason = (
                                f"mail_domain ({pattern_entry.get('mail_domain')}) was never independently "
                                f"confirmed (it's Hunter's website-domain guess) and is PROBABLY WRONG"
                            )
                        print(
                            f"\n*** QUOTA GUARD TRIPPED for {firm!r}: {invalid_like}/{checked} ({rate_pct}%) "
                            f"came back invalid on a non-catch-all domain. Stopped verifying the remaining "
                            f"{len(firm_rows) - checked} row(s) for this firm. {reason}. ***\n",
                            file=sys.stderr,
                        )

            if render_and_store_draft(row, orgs, target_school, sender_first, args):
                drafts_generated += 1
            else:
                drafts_skipped += 1

    variation = similarity_report(all_rows)
    for row in all_rows:
        row.pop("_matched_org", None)

    hunter_searches, hunter_verifications = fetch_hunter_account_usage(hunter_key)
    zb_credits = fetch_zerobounce_credits(zb_key)

    return {
        "variation": variation,
        "attempted": len(in_band_rows),
        "constructed": constructed,
        "status_counts": dict(status_counts),
        "drafts_generated": drafts_generated,
        "drafts_skipped": drafts_skipped,
        "firms_guard_tripped": firms_guard_tripped,
        "hunter_searches_this_run": usage["hunter_searches"],
        "zerobounce_verifications_this_run": usage["zerobounce_verifications"],
        "hunter_searches_remaining": hunter_searches.get("remaining"),
        "hunter_verifications_remaining": hunter_verifications.get("remaining"),
        "zerobounce_credits_remaining": zb_credits,
    }


def enrich_facts(rows, school, firm_config, orgs, api_key):
    """Backfill the facts columns on rows from an OLDER results file, which never stored work history.
    One small Exa people lookup per person (about a cent each). Rows are matched by LinkedIn profile,
    falling back to name plus confirmed current employer. Returns (cost, found, missed)."""
    from ledger import normalize_linkedin
    cost, found, missed = 0.0, 0, 0
    for row in rows:
        if row.get("facts_checked") == FACTS_VERSION or row.get("firm_match") != "yes":
            continue
        query = f"{row['name']} {row.get('title', '')} at {row['firm_queried']}".strip()
        data = exa_search(api_key, query, 5)
        cost += (data.get("costDollars") or {}).get("total") or 0.0
        results = data.get("results") or []
        match = None
        want = normalize_linkedin(row.get("linkedin_url"))
        for res in results:
            if want and normalize_linkedin(res.get("url")) == want:
                match = res
                break
        if match is None:
            for res in results:
                props = get_person_props(res)
                if normalize(props.get("name")) == normalize(row["name"]) and \
                        check_firm(props, res.get("text") or "", row["firm_queried"], firm_config, {})[0] == "yes":
                    match = res
                    break
        if match is None:
            row["facts_checked"] = "no_match"
            missed += 1
            continue
        row.update(extract_facts(get_person_props(match), school, row["firm_queried"], firm_config, orgs,
                                 match.get("text") or ""))
        row["facts_checked"] = FACTS_VERSION
        found += 1
    return cost, found, missed


def passes_requirements(row, require):
    """--require is opt-in and DROPS rows; default behavior everywhere else
    stays tag-not-drop."""
    if "school" in require and row.get("school_match") != "yes":
        return False
    if "firm" in require and row.get("firm_match") != "yes":
        return False
    if "title" in require and row.get("title_match") != "yes":
        return False
    if "org" in require and row.get("affiliation_match") != "yes":
        return False
    return True


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Find real people to network with via the Exa search API."
    )
    school_group = parser.add_mutually_exclusive_group()
    school_group.add_argument("--school", default=None, help="University to match against.")
    school_group.add_argument(
        "--highschool", default=None,
        help="High school to match against instead of a university — a shared high school "
             "is often a rarer, stronger connection than a shared college.",
    )
    parser.add_argument(
        "--firm", action="append",
        help="Firm to match against. Repeatable for multiple firms in one run.",
    )
    parser.add_argument(
        "--paste-exclude", action="append", default=[], metavar="NAME",
        help="With --paste-list: exclude this person (case-insensitive substring match on the parsed name) "
             "regardless of what the headline says. Repeatable. For a one-off judgment call, e.g. a "
             "short-term secondment the automated rules didn't catch.",
    )
    parser.add_argument(
        "--paste-flag", action="append", default=[], metavar="NAME",
        help="With --paste-list: flag this person as unclear_role instead of drafting them — kept in the "
             "output, not excluded, not enriched. Repeatable. For a headline that doesn't clearly state a "
             "current role (e.g. a bare list of past employers with no 'at'/'@').",
    )
    parser.add_argument(
        "--paste-list", default=None, metavar="PATH",
        help="Skip Exa's find stage entirely: build rows from a pasted LinkedIn alumni-search export "
             "(name, LinkedIn URL, headline per person) instead of an Exa people search. LinkedIn's own "
             "school-to-company graph is authoritative; Exa is guessing from indexed text and misses most "
             "of it. School match is CONFIRMED by the alumni search itself (you ran it filtered to your "
             "school) and is never re-verified or used to drop anyone. Requires exactly one --firm and "
             "--school/--highschool. Costs $0 Exa search; survivors are enriched at ~$0.007 each (the "
             "existing --enrich-facts path). See alumni_paste.py for the accepted paste formats.",
    )
    parser.add_argument(
        "--from-csv", default=None,
        help="Skip Stage 1 (Exa search) entirely and load rows from an existing output CSV "
             "instead — e.g. to run --emails on a result set you already have without "
             "re-spending Exa quota. --school/--firm are not required with this. All of the "
             "input CSV's columns are preserved in the output; --require/--title etc. only "
             "apply if you pass them again this run.",
    )
    parser.add_argument(
        "--city", action="append", default=None,
        help="City to narrow the search (optional). Repeatable — one Exa query is run per "
             "firm-city combination rather than combining cities into one query.",
    )
    parser.add_argument("--n", type=int, default=25, help="Number of results to request per firm (default 25).")
    parser.add_argument(
        "--title", action="append", default=None,
        help="Filter on the person's current title (loose substring, case-insensitive — "
             "\"Consultant\" catches \"Senior Consultant\"). Repeatable. Doesn't drop "
             "non-matching rows, just flags them in title_match, same as seniority_band.",
    )
    parser.add_argument(
        "--org", action="append", default=None,
        help="Org/affiliation to check for (fraternity, sports team, honor society, club...). "
             "Repeatable. Spelling variants are configured in org_variants.json.",
    )
    parser.add_argument(
        "--require", action="append", default=None, choices=["school", "firm", "title", "org"],
        help="Hard filter that DROPS rows instead of tagging them (default behavior stays "
             "tag-not-drop). Repeatable. 'school'/'firm' require school_match/firm_match == "
             "\"yes\"; 'title' requires title_match == \"yes\" (needs --title too); 'org' "
             "requires affiliation_match == \"yes\" (needs --org too).",
    )
    parser.add_argument(
        "--min-seniority", type=float, default=DEFAULT_MIN_SENIORITY_YEARS,
        help=f"Minimum years of experience to count as in-band (default {DEFAULT_MIN_SENIORITY_YEARS}).",
    )
    parser.add_argument(
        "--max-seniority", type=float, default=DEFAULT_MAX_SENIORITY_YEARS,
        help=f"Maximum years of experience to count as in-band (default {DEFAULT_MAX_SENIORITY_YEARS}).",
    )
    parser.add_argument(
        "--emails", action="store_true",
        help="Find + verify email addresses and generate outreach drafts for in_band rows. "
             "Requires --sender-name/--sender-school/--sender-year/--sender-industry.",
    )
    parser.add_argument(
        "--drafts-only", action="store_true",
        help="Re-render drafts without touching Hunter/ZeroBounce at all — for re-running the "
             "draft stage on a result set (usually via --from-csv) that already has verified "
             "emails, without spending any more quota. Preserves existing email/email_status. "
             "Same sender flags required as --emails; doesn't need HUNTER_API_KEY/ZEROBOUNCE_API_KEY.",
    )
    parser.add_argument(
        "--sender-signature", default=None,
        help='Optional signature line added under the sign-off, e.g. "Alex Example, President, Example '
             'Investing Club". Defaults to profile.json "signature". Checked by validate_draft like the rest of the draft.',
    )
    parser.add_argument(
        "--school-short", default=None,
        help="How to say the target school in the hook for this run (e.g. USC). Overrides "
             "school_display.json. Matching always uses the full --school name.",
    )
    parser.add_argument("--sender-name", default=None, help="Your full name (required with --emails/--drafts-only).")
    parser.add_argument("--sender-school", default=None, help="Your school (required with --emails/--drafts-only).")
    parser.add_argument(
        "--sender-year", default=None,
        help='Your class year, e.g. "senior" (required with --emails/--drafts-only).',
    )
    parser.add_argument(
        "--sender-industry", default=None,
        help="Your target industry (required with --emails/--drafts-only).",
    )
    parser.add_argument(
        "--email-domain", default=None,
        help="Override the mail domain used to construct addresses this run (e.g. gs.com for Goldman "
             "Sachs, whose mail domain differs from its web domain). Persists to patterns.json as a "
             "manually-confirmed mail_domain for every firm in this run.",
    )
    parser.add_argument("--out", default=None, help="Output CSV path (default: auto-generated).")
    parser.add_argument(
        "--enrich-facts", action="store_true",
        help="With --from-csv: backfill firm_start_year, school_end_year and prior_employer for rows from an "
             "older results file (one small Exa lookup per person, about a cent each). Needed once per old "
             "file so hooks can be varied from real facts.",
    )
    parser.add_argument(
        "--no-dedupe", action="store_true",
        help="Do not drop people already in ledger.json (queued, sent, replied, bounced, suppressed). "
             "By default a fresh search drops them BEFORE any Hunter/ZeroBounce spend, so the same "
             "person never appears in two runs. Ignored with --from-csv.",
    )
    parser.add_argument("--add-dnc", metavar="NAME", default=None,
        help="Add a person (or, with --dnc-org, an organization) to the Do Not Contact list and exit. "
             "Use after a call, a referral, or any hand-written email. Entries are never removed by code.")
    parser.add_argument("--dnc-firm", default="", help="With --add-dnc: the person's firm (improves matching).")
    parser.add_argument("--dnc-email", action="append", default=[], help="With --add-dnc: an email address. Repeatable.")
    parser.add_argument("--dnc-linkedin", action="append", default=[], help="With --add-dnc: a LinkedIn URL. Repeatable.")
    parser.add_argument("--dnc-tier", default="never_relationship", choices=dnc.TIERS,
        help="With --add-dnc: tier (default never_relationship).")
    parser.add_argument("--dnc-reason", default="", help="With --add-dnc: why, in a few words.")
    parser.add_argument("--dnc-org", action="store_true", help="With --add-dnc: the name is an organization (anyone there).")
    args = parser.parse_args()

    if args.add_dnc:
        entry, created = dnc.add(args.add_dnc, args.dnc_firm, args.dnc_tier, args.dnc_reason,
                                 emails=args.dnc_email, linkedin=args.dnc_linkedin, org=args.dnc_org)
        print(f"{'Added to' if created else 'Already on'} the Do Not Contact list: {entry['name']}"
              + (f" ({entry['firm']})" if entry['firm'] else "") + f" [{entry['tier']}]")
        print("Removal is manual: edit dnc.json by hand.")
        return

    if args.emails and args.drafts_only:
        parser.error("--emails and --drafts-only are mutually exclusive — --drafts-only never touches email data")
    apply_profile_defaults(args)
    if (args.emails or args.drafts_only) and not all(
        [args.sender_name, args.sender_school, args.sender_year, args.sender_industry]
    ):
        parser.error("--emails/--drafts-only require --sender-name, --sender-school, --sender-year, and "
                     "--sender-industry (pass the flags, or set name/school/year/industry in profile.json)")

    require = set(args.require or [])
    if "title" in require and not args.title:
        parser.error("--require title needs --title too — there's no title_match to require without it")
    if "org" in require and not args.org:
        parser.error("--require org needs --org too — there's no affiliation_match to require without it")

    if args.paste_list and args.from_csv:
        parser.error("--paste-list and --from-csv are mutually exclusive")
    if args.paste_list:
        if not args.school and not args.highschool:
            parser.error("--school or --highschool is required with --paste-list")
        if not args.firm or len(args.firm) != 1:
            parser.error("--paste-list needs exactly one --firm (the alumni search was run against one firm)")
    elif not args.from_csv:
        if not args.school and not args.highschool:
            parser.error("--school or --highschool is required (unless --from-csv/--paste-list is used)")
        if not args.firm:
            parser.error("--firm is required (unless --from-csv/--paste-list is used)")

    target_school = args.highschool or args.school
    if args.from_csv and (args.emails or args.drafts_only) and not target_school:
        print(
            "\n*** WARNING: --school/--highschool not given with --from-csv. Tier 1/3 drafts need "
            "a target school for the hook line (\"I saw you went to ... too.\") — without it, "
            "those rows will fail draft validation (a missing value can't silently render as "
            "literal \"None\") and get skipped rather than shipped broken. Pass --school if any "
            "rows are tier 1 or 3. ***\n",
            file=sys.stderr,
        )
    orgs = args.org or []
    org_config = load_org_variants() if orgs else {}
    # patterns.json doubles as the per-firm exclusion config (e.g. "Bain Capital"
    # excluded from "Bain & Company") and, when --emails is used, the email
    # pattern cache — loaded unconditionally since firm matching needs the
    # exclusions regardless of whether --emails is set.
    firm_config = load_patterns_cache()
    title_config = load_title_filters()

    all_rows = []
    total_cost = 0.0
    firm_stats = []
    csv_fields = None  # set below, either from a fresh search or preserved from --from-csv

    if args.from_csv:
        in_path = Path(args.from_csv)
        if not in_path.exists():
            sys.exit(f"--from-csv file not found: {in_path}")
        with in_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            input_fields = list(reader.fieldnames or [])
            reband_count = 0
            for row in reader:
                row["tier"] = effective_tier(row)  # backfills a missing/old tier column
                if reapply_title_band(row):
                    reband_count += 1
                all_rows.append(row)
        if reband_count:
            print(f"Re-applied the senior-title rule: {reband_count} row(s) moved out of in_band "
                  f"(title is manager level or above).", file=sys.stderr)
        backfilled_tier = "tier" not in input_fields
        print(
            f"Loaded {len(all_rows)} rows from {in_path} (Stage 1 skipped, $0.00 Exa cost)"
            + (" — tier column was missing, derived from school_match/affiliation_match" if backfilled_tier else ""),
            file=sys.stderr,
        )
        csv_fields = input_fields + [f for f in ("affiliation_match", "tier") if f not in input_fields] \
            + [f for f in EMAIL_CSV_FIELDS if (args.emails or args.drafts_only) and f not in input_fields] \
            + [f for f in FACT_CSV_FIELDS if f not in input_fields]
        if args.enrich_facts:
            facts_cost, facts_found, facts_missed = enrich_facts(all_rows, target_school, firm_config, orgs, load_api_key())
            total_cost += facts_cost
            print(f"Backfilled facts: {facts_found} found, {facts_missed} not matched, "
                  f"Exa cost ${facts_cost:.4f}", file=sys.stderr)

    if not args.from_csv and not args.paste_list:
        api_key = load_api_key()
        csv_fields = (
            BASE_CSV_FIELDS
            + (EMAIL_CSV_FIELDS if (args.emails or args.drafts_only) else []) + TAIL_CSV_FIELDS
            + FACT_CSV_FIELDS
            + (TITLE_CSV_FIELDS if args.title else []) + EVIDENCE_CSV_FIELD
        )

    if args.paste_list:
        import alumni_paste
        paste_firm = args.firm[0]
        try:
            paste_text = Path(args.paste_list).read_text(encoding="utf-8")
        except OSError as e:
            sys.exit(f"--paste-list file not found or unreadable: {e}")
        entries, unparsed = alumni_paste.parse(paste_text)
        csv_fields = (
            BASE_CSV_FIELDS + ["title_source"]
            + (EMAIL_CSV_FIELDS if (args.emails or args.drafts_only) else []) + TAIL_CSV_FIELDS
            + FACT_CSV_FIELDS + EVIDENCE_CSV_FIELD
        )
        paste_exclude = [n.lower() for n in (args.paste_exclude or [])]
        paste_flag = [n.lower() for n in (args.paste_flag or [])]
        counts = {"no_role": 0, "support": 0, "above_band": 0, "different_track": 0, "temporary": 0,
                  "manual_exclude": 0, "unclear_role": 0, "no_profile_url": 0, "ok": 0, "no_headline_or_url": 0}
        BAND_OF = {"no_role": "not_employed", "support": "excluded_role", "above_band": "above",
                  "different_track": "different_track", "temporary": "excluded_role",
                  "manual_exclude": "excluded_role", "unclear_role": "unclear_role",
                  "no_profile_url": "not_employed", "ok": "in_band"}
        EVIDENCE_OF = {
            "no_role": lambda t: f"headline does not mention {paste_firm} — likely a current student or an alum elsewhere",
            "support": lambda t: f"support/administrative title in the headline ({t!r})",
            "above_band": lambda t: f"senior title in the headline ({t!r})",
            "different_track": lambda t: f"a real role, but a different track from this campaign ({t!r}) — flagged, not dropped",
            "temporary": lambda t: f"a short-term secondment/exchange title ({t!r}), not a standing role",
            "manual_exclude": lambda t: "excluded by --paste-exclude (operator judgment call)",
            "unclear_role": lambda t: f"flagged by --paste-flag ({t!r}) — no clear current-role statement, kept for manual review",
            "no_profile_url": lambda t: "no personal LinkedIn profile URL (an anonymized/blocked result) — cannot look up or dedupe",
            "ok": lambda t: f"confirmed by the alumni search's own headline ({t!r})",
        }
        for e in entries:
            if not e.get("linkedin_url") or not e.get("headline"):
                counts["no_headline_or_url"] += 1
                row = {
                    "name": e.get("name") or "(unparsed)", "firm_queried": paste_firm,
                    "linkedin_url": e.get("linkedin_url", ""),
                    "linkedin_search_url": linkedin_search_url(e.get("name") or "", paste_firm),
                    "city": "", "school_match": "yes", "firm_match": "unknown", "affiliation_match": "no",
                    "tier": compute_tier("yes", "no"), "title": "", "title_source": "linkedin_headline",
                    "seniority_flag": "", "seniority_band": "not_employed", "total_experience_years": "",
                    "facts_checked": "", "evidence": "Pasted entry had no LinkedIn URL or no headline text "
                                                      "to classify — could not confirm a role at the firm.",
                }
                all_rows.append(row)
                continue
            name_lower = (e.get("name") or "").lower()
            force = ("exclude" if any(n in name_lower for n in paste_exclude) else
                    "flag" if any(n in name_lower for n in paste_flag) else None)
            c = alumni_paste.classify(e, paste_firm, firm_config, title_config, company_matches_firm,
                                      is_non_target_role, seniority_flag, is_different_track,
                                      is_temporary_role, force=force)
            counts[c["reason"]] += 1
            band = BAND_OF[c["reason"]]
            band_evidence = EVIDENCE_OF[c["reason"]](c["title"])
            row = {
                "name": e["name"] or "(name not parsed)", "firm_queried": paste_firm,
                "linkedin_url": e["linkedin_url"], "linkedin_search_url": linkedin_search_url(e["name"], paste_firm),
                "city": "", "school_match": "yes",
                "firm_match": "no" if c["reason"] in ("no_role", "no_profile_url") else "yes",
                "affiliation_match": "no", "tier": compute_tier("yes", "no"),
                "title": c["title"], "title_source": "linkedin_headline", "seniority_flag": seniority_flag(c["title"]),
                "seniority_band": band, "total_experience_years": "", "facts_checked": "",
            }
            for k in FACT_CSV_FIELDS:
                row.setdefault(k, "")
            row["evidence"] = (f"School: confirmed by the alumni search itself (filtered to {target_school}), "
                               f"not re-verified || Firm: [linkedin_headline] {e['headline']!r} || Seniority: {band_evidence}")
            all_rows.append(row)
        print(
            f"Parsed {len(entries)} of {len(entries) + unparsed} pasted entries"
            + (f" ({unparsed} line(s) could not be parsed)" if unparsed else "") + ".\n"
            f"  no LinkedIn URL or headline: {counts['no_headline_or_url']}, no personal profile URL "
            f"(anonymized): {counts['no_profile_url']}, current student / no role at {paste_firm}: "
            f"{counts['no_role']}, support title: {counts['support']}, temporary/secondment: "
            f"{counts['temporary']}, above band: {counts['above_band']}, different track (flagged, kept): "
            f"{counts['different_track']}, manually excluded: {counts['manual_exclude']}, flagged unclear "
            f"(kept, not drafted): {counts['unclear_role']}, survive to enrichment: {counts['ok']}",
            file=sys.stderr,
        )
        firm_stats.append({"firm": paste_firm, "raw": len(entries), "unique": len(all_rows), "confirmed_aff": None})

        # DNC and cross-run dedupe BEFORE any enrichment spend — only the survivors are candidates for it.
        survivors = [r for r in all_rows if r["seniority_band"] == "in_band"]
        others = [r for r in all_rows if r["seniority_band"] != "in_band"]
        try:
            survivors, paste_dnc_excluded = dnc.filter_rows(survivors, context="paste-list")
        except dnc.DNCError as e:
            sys.exit(f"\n*** {e} ***")
        for line in dnc.summarize(paste_dnc_excluded):
            print(("\n" if line.startswith("Do Not") else "") + line, file=sys.stderr)
        if not args.no_dedupe:
            from ledger import Ledger
            _ledger = Ledger()
            before = len(survivors)
            survivors, ledger_dropped = _ledger.filter_new(survivors)
            if before != len(survivors):
                print(f"\nCross-run dedupe: dropped {before - len(survivors)} of {before} pasted survivor(s) "
                      f"already in the ledger, before any enrichment spend. {len(survivors)} remain.", file=sys.stderr)

        if survivors:
            facts_cost, facts_found, facts_missed = enrich_facts(survivors, target_school, firm_config, orgs, load_api_key())
            total_cost += facts_cost
            print(f"Enriched {facts_found} of {len(survivors)} survivor(s) (about $0.007 each), "
                  f"{facts_missed} not matched, Exa cost ${facts_cost:.4f}", file=sys.stderr)
        all_rows = others + survivors

    cities = args.city or [None]  # None = no city qualifier, one search instead of one per city

    for firm in (args.firm or []) if not (args.from_csv or args.paste_list) else []:
        pooled = {}
        raw_count = 0
        firm_cost = 0.0
        expected_raw = 0

        if orgs:
            request_n = args.n * 3
            if request_n > EXA_MAX_RESULTS:
                print(
                    f"  Note: capping numResults at {EXA_MAX_RESULTS} (Exa's max) "
                    f"instead of {request_n} (n={args.n} x 3).",
                    file=sys.stderr,
                )
                request_n = EXA_MAX_RESULTS

            # One query per (city, org, tier) combination — never stuffing
            # multiple cities into one query, since that dilutes semantic
            # matching. Merged into the same per-firm pool regardless of
            # which city's query surfaced a given result.
            searches = []
            for city in cities:
                for org in orgs:
                    searches.append((f"tier1 (school+org), org={org!r} city={city!r}",
                                      build_query_school_org(target_school, org, firm, city)))
                    searches.append((f"tier2 (org only), org={org!r} city={city!r}",
                                      build_query_org_only(org, firm, city)))
            expected_raw = request_n * len(searches)

            for label, query in searches:
                print(f"Searching [{label}]: firm={firm!r} n={request_n}", file=sys.stderr)
                try:
                    data = exa_search(api_key, query, request_n)
                except RuntimeError as e:
                    print(f"  WARNING: search failed ({label}, firm={firm!r}): {e}", file=sys.stderr)
                    continue
                results = data.get("results") or []
                raw_count += len(results)
                firm_cost += (data.get("costDollars") or {}).get("total") or 0.0
                for r in results:
                    key = dedupe_key(r)
                    if key and key not in pooled:
                        pooled[key] = r
        else:
            expected_raw = args.n * len(cities)
            for city in cities:
                print(f"Searching: school={target_school!r} firm={firm!r} city={city!r} n={args.n}", file=sys.stderr)
                try:
                    data = exa_search(api_key, build_query_plain(target_school, firm, city), args.n)
                except RuntimeError as e:
                    print(f"  WARNING: skipping firm={firm!r} city={city!r} due to error: {e}", file=sys.stderr)
                    continue
                results = data.get("results") or []
                raw_count += len(results)
                firm_cost += (data.get("costDollars") or {}).get("total") or 0.0
                for r in results:
                    key = dedupe_key(r)
                    if key and key not in pooled:
                        pooled[key] = r

        if raw_count < expected_raw:
            print(f"  Note: requested {expected_raw} total results for {firm!r} across "
                  f"{len(cities)} cit{'y' if len(cities) == 1 else 'ies'}, Exa returned {raw_count}.",
                  file=sys.stderr)

        rows = [
            build_row(r, target_school, firm, orgs, org_config, firm_config,
                      args.min_seniority, args.max_seniority, title_config, args.title)
            for r in pooled.values()
        ]

        deduped_rows = []
        seen_name_firm = set()
        for row in rows:
            key = name_firm_key(row["name"], row["firm_queried"])
            if key in seen_name_firm:
                continue
            seen_name_firm.add(key)
            deduped_rows.append(row)
        rows = deduped_rows

        band_rank = lambda r: 0 if r["seniority_band"] == "in_band" else 1
        rows.sort(key=lambda r: (r["tier"], band_rank(r)))
        all_rows.extend(rows)
        total_cost += firm_cost

        confirmed_aff = sum(1 for r in rows if r.get("affiliation_match") == "yes") if orgs else None
        firm_stats.append({
            "firm": firm, "raw": raw_count, "unique": len(rows), "confirmed_aff": confirmed_aff,
        })

    if not all_rows:
        print("No results returned for any firm. Nothing written.", file=sys.stderr)
        sys.exit(1)

    if require:
        total_before_require = len(all_rows)
        all_rows = [r for r in all_rows if passes_requirements(r, require)]
        dropped = total_before_require - len(all_rows)
        print(
            f"\n--require {sorted(require)}: {len(all_rows)} of {total_before_require} rows survive "
            f"({dropped} dropped).",
            file=sys.stderr,
        )
        if not all_rows:
            print("No rows survive --require. Nothing written.", file=sys.stderr)
            sys.exit(1)

    if not args.from_csv and not args.no_dedupe:
        from ledger import Ledger
        ledger = Ledger()
        before = len(all_rows)
        all_rows, ledger_dropped = ledger.filter_new(all_rows)
        if before != len(all_rows):
            print(
                f"\nCross-run dedupe: dropped {before - len(all_rows)} of {before} rows already in the "
                f"ledger (queued, sent, replied, bounced or suppressed). {len(all_rows)} remain.",
                file=sys.stderr,
            )
        if not all_rows:
            print("Every row is already in the ledger. Nothing written.", file=sys.stderr)
            sys.exit(1)

    # Do Not Contact: before any Hunter or ZeroBounce spend, same place as the ledger. Applies to fresh searches
    # AND --from-csv, and there is no flag to skip it. Fails closed if dnc.json is unreadable.
    try:
        all_rows, dnc_excluded = dnc.filter_rows(all_rows, context="from-csv" if args.from_csv else "search")
    except dnc.DNCError as e:
        sys.exit(f"\n*** {e} ***")
    for line in dnc.summarize(dnc_excluded):
        print(("\n" if line.startswith("Do Not") else "") + line, file=sys.stderr)
    if not all_rows:
        print("Every row is on the Do Not Contact list. Nothing written.", file=sys.stderr)
        sys.exit(1)

    email_summary = None
    if args.emails or args.drafts_only:
        email_summary = run_email_and_draft_stages(
            all_rows, orgs, target_school, args, firm_config, drafts_only=args.drafts_only
        )

    if args.out:
        out_path = Path(args.out)
    else:
        # Include firm(s) and org(s) so Goldman/McKinsey/Bain runs are
        # distinguishable by filename alone, without opening any of them.
        if args.from_csv:
            school_for_name = target_school or "from-csv"
            firms_for_name = sorted(set(r.get("firm_queried", "") for r in all_rows if r.get("firm_queried")))
        else:
            school_for_name = target_school
            firms_for_name = args.firm
        parts = [slugify(school_for_name), "-".join(slugify(f) for f in firms_for_name)]
        if orgs:
            parts.append("-".join(slugify(o) for o in orgs))
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = Path(f"outreach_{'_'.join(p for p in parts if p)}_{timestamp}.csv")

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    confirmed_both = sum(
        1 for r in all_rows if r["school_match"] == "yes" and r["firm_match"] == "yes"
    )

    print("\n--- Summary ---")
    for stat in firm_stats:
        if orgs:
            rate = f"{100 * stat['confirmed_aff'] / stat['unique']:.0f}%" if stat["unique"] else "n/a"
            print(f"  {stat['firm']}: raw={stat['raw']} unique={stat['unique']} "
                  f"confirmed-affiliation={stat['confirmed_aff']} ({rate})")
        else:
            print(f"  {stat['firm']}: {stat['unique']} results (raw={stat['raw']})")
    print(f"Total results: {len(all_rows)}")
    print(f"Confirmed on both school and firm: {confirmed_both}")
    from collections import Counter
    tiers = Counter(r["tier"] for r in all_rows)
    print(f"Tier distribution: " + ", ".join(f"Tier {t}={tiers.get(t, 0)}" for t in (1, 2, 3, 4)))
    bands = Counter(r["seniority_band"] for r in all_rows)
    print(f"Seniority band ({args.min_seniority}-{args.max_seniority}y): " + ", ".join(
        f"{b}={bands.get(b, 0)}" for b in ("below", "in_band", "above", "incoming", "excluded_role", "different_track", "unclear_role", "not_employed")
    ))
    print(f"Total cost: ${total_cost:.4f}")
    print(f"Do Not Contact: {len(dnc_excluded)} excluded before any email spend"
          + (" (" + ", ".join(f"{r.get('name')}" for r, _ in dnc_excluded) + "), see dnc-exclusions.log" if dnc_excluded else ""))
    if email_summary:
        label = "Drafts only" if args.drafts_only else "Emails / Drafts"
        print(f"\n--- {label} (in_band rows only) ---")
        print(f"  In-band rows processed: {email_summary['attempted']}")
        print(f"  Constructed: {email_summary['constructed']}, "
              f"not constructed: {email_summary['attempted'] - email_summary['constructed']}")
        status_str = ", ".join(f"{k}={v}" for k, v in email_summary["status_counts"].items()) or "none"
        print(f"  Verification status: {status_str}" + ("" if not args.drafts_only else " (preserved, not re-checked)"))
        if email_summary["firms_guard_tripped"]:
            print(f"  QUOTA GUARD TRIPPED for: {', '.join(email_summary['firms_guard_tripped'])} "
                  f"(see warning above — mail domain is likely wrong)")
        print(f"  Drafts generated: {email_summary['drafts_generated']}, "
              f"skipped (no valid hook or content-rule violation): {email_summary['drafts_skipped']}")
        v = email_summary.get("variation") or {}
        if v.get("rows"):
            print(f"  Draft similarity (warning only): {v['rows']} drafts across {v['domains']} domain(s), highest "
                  f"pair {v['max_similarity']:.0%}, {v['pairs_over']} pair(s) at {variants.SIMILARITY_THRESHOLD:.0%} or more. "
                  f"The ask is shared on purpose; per-domain send pacing in bridge.py is the control.")
        if args.drafts_only:
            print("  Hunter/ZeroBounce not touched — quota unchanged.")
        else:
            print(f"  This run used: {email_summary['hunter_searches_this_run']} Hunter domain-search(es) "
                  f"[cache misses only], {email_summary['zerobounce_verifications_this_run']} ZeroBounce verification(s)")
            hs, hv = email_summary["hunter_searches_remaining"], email_summary["hunter_verifications_remaining"]
            print(f"  Hunter remaining this cycle: searches={hs if hs is not None else '?'}, "
                  f"verifications={hv if hv is not None else '?'}")
            zb = email_summary["zerobounce_credits_remaining"]
            print(f"  ZeroBounce remaining this cycle: {zb if zb is not None else '?'}")
    print(f"Output written to: {out_path}")


if __name__ == "__main__":
    main()
