"""Paste-a-list mode: LinkedIn's own alumni search is the authoritative school-to-firm graph. Exa is guessing
from indexed profile text and misses most of it (one alumni search for a school at a firm returned about 70 people;
an Exa people search for the same pair returned 4). This module skips Exa's find stage entirely: the user runs
the alumni search himself, filtered to the school, and pastes the results. School match is therefore
CONFIRMED by the search itself and is never re-verified or used to drop anyone.

What is pasted is name, LinkedIn URL, and headline per person. The exact copy-paste shape varies, so parse()
tries three forms, in order:
  Markdown-link form   LinkedIn's own alumni-search page renders each name as a link, and copy-pasting it
                       (through a browser or a chat client) commonly comes through as literal Markdown —
                       "[Jane Doe](https://www.linkedin.com/in/jane-doe/)" — with no blank lines between
                       people at all. This is the primary, expected shape: every `[Name](url)` where the url
                       is a personal profile link starts a new person; everything up to the next one is that
                       person's headline and boilerplate (connection degree, mutual connections, the shared
                       school line LinkedIn shows in place of mutual connections for a 3rd-degree match, a
                       stray "[Message](...)"/"Connect"/"Follow" action) — all stripped, never counted as
                       headline content. A `[Name](url)` whose url is NOT a personal profile link (LinkedIn
                       renders a private/blocked profile as "[LinkedIn Member](.../people/?facetSchool=...)",
                       a company search link, not a per-person one) still starts a record, but with no usable
                       identity — classify() drops it as no_profile_url before anything else, since there is
                       no stable way to look the person up or avoid contacting the same blocked profile twice.
  Block form           one person per paragraph (blank-line separated): a name line, a line with the
                       LinkedIn URL (anywhere in the line), and a headline line, in any order.
  Line form            one person per line, fields separated by a tab, " | ", or " || ", in any order
                       (detected by which field is a URL and which looks like a person's name).

Classification (classify()) runs BEFORE any Exa or Hunter spend, since it is free (headline text only):
  no_profile_url   the entry has no personal LinkedIn profile URL (an anonymized "LinkedIn Member" result) —
                    excluded, unconditionally, before any other check
  no_role          the target firm is not mentioned in the headline at all (a current student, or an alum
                    at a different employer) — excluded
  temporary        a short-term secondment or exchange placement (title_filters.json temporary_roles, e.g.
                    Visiting Associate) — excluded
  manual_exclude    the operator named this person via --paste-exclude — excluded
  unclear_role      the operator named this person via --paste-flag, or a rule flags it — kept, not drafted
  support          a support/administrative title (title_filters.json non_target_roles) — excluded
  above_band       a senior title (find.SENIOR_TITLE_PATTERNS, e.g. Principal, Director, Project Leader)
                    — excluded
  different_track  a real role, but not the track this campaign targets (title_filters.json
                    different_track_roles, e.g. Data Scientist, BCG X) — FLAGGED, not dropped: kept in the
                    output for a possible later, different-purpose run
  ok               survives to enrichment

This module is pure: no network calls, no file writes.
"""
import re

BOILERPLATE_RE = re.compile(
    r"^\s*(?:·\s*)?(?:1st|2nd|3rd\+?)\s*(?:degree connection)?\s*$|"
    r"^\s*\d+\s+mutual connection|^\s*(?:message|connect|view profile|follow|linkedin member)\s*$|"
    r".*\bmutual connections?\b.*",
    re.IGNORECASE,
)
URL_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/\S+", re.IGNORECASE)
NAME_RE = re.compile(r"^[A-Z][A-Za-z'.\-]*(?:\s+[A-Z][A-Za-z'.\-]*){0,3}\.?$")

# A markdown link that starts a person record: [Name](url). Excludes the empty-name case ("[]()").
MD_LINK_RE = re.compile(r"\[([^\[\]]+)\]\((https?://[^\s\)]+)\)")
# These link shapes appear BETWEEN people, pointing at an action on the person just described, not at a new
# person: never treated as a record start.
MD_LINK_NOISE_RE = re.compile(r"/(?:messaging|overlay)/", re.IGNORECASE)
PROFILE_URL_RE = re.compile(r"linkedin\.com/(?:in|pub)/", re.IGNORECASE)
SCHOOL_ONLY_LINE_RE = re.compile(
    r"^\s*(?:university of southern california|usc)\s*$", re.IGNORECASE)


def is_profile_url(url):
    """A per-person LinkedIn profile URL, as opposed to a company/search/messaging link."""
    return bool(PROFILE_URL_RE.search(url or ""))


def _parse_markdown(text):
    """[Name](url) anchors one person per record; returns None (not a partial result) if the text has no
    such link at all, so the caller falls back to the block/line parsers instead of returning nothing."""
    # A paste that starts mid-link ("Jane Doe](url)...", missing only its opening bracket — this happens
    # when whatever the text was copied through swallows a single leading "[") is recovered by treating the
    # very start of the text as an implicit "[".
    fixed = text
    if not text.lstrip().startswith("["):
        m = re.match(r"^\s*[A-Za-zÀ-ÿ][^\[\]\n]{0,80}\]\(https?://", text)
        if m:
            fixed = "[" + text.lstrip()
    starts = [m for m in MD_LINK_RE.finditer(fixed) if not MD_LINK_NOISE_RE.search(m.group(2))]
    if not starts:
        return None
    entries = []
    for i, m in enumerate(starts):
        name = m.group(1).strip()
        url = m.group(2).strip()
        chunk_end = starts[i + 1].start() if i + 1 < len(starts) else len(fixed)
        lines = [ln.strip() for ln in fixed[m.end():chunk_end].splitlines() if ln.strip()]

        def is_noise(ln):
            if BOILERPLATE_RE.match(ln) or SCHOOL_ONLY_LINE_RE.match(ln):
                return True
            lm = MD_LINK_RE.fullmatch(ln)      # a whole line that is JUST "[Message](url)" or similar
            return bool(lm and MD_LINK_NOISE_RE.search(lm.group(2)))

        headline = " ".join(ln for ln in lines if not is_noise(ln)).strip()
        entries.append({"name": name, "linkedin_url": url, "headline": headline})
    return entries


def _looks_like_name(s):
    s = s.strip()
    return bool(s) and len(s) <= 60 and not URL_RE.search(s) and bool(NAME_RE.match(s))


TRAILING_JUNK_RE = re.compile(r"\s*(?:·\s*)?(?:1st|2nd|3rd\+?)\s*(?:degree connection)?\s*$", re.IGNORECASE)


def _clean_block_lines(block):
    return [ln.strip() for ln in block.splitlines() if ln.strip() and not BOILERPLATE_RE.match(ln.strip())]


def _parse_block(lines):
    url_idx = next((i for i, ln in enumerate(lines) if URL_RE.search(ln)), None)
    if url_idx is None:
        return None
    m = URL_RE.search(lines[url_idx])
    url = m.group(0)
    # If the URL line also carries other text (e.g. "Jane Doe linkedin.com/in/jane-doe · 2nd"), pull the
    # non-URL text out as its own line — cleaned of the same boilerplate a standalone line would be — so
    # it is not lost, and a trailing connection-degree marker glued onto it doesn't leak into the headline.
    leftover = TRAILING_JUNK_RE.sub("", lines[url_idx][:m.start()] + " " + lines[url_idx][m.end():]).strip()
    others = [ln for i, ln in enumerate(lines) if i != url_idx]
    if leftover:
        others.insert(min(url_idx, len(others)), leftover)
    if not others:
        return {"name": "", "linkedin_url": url, "headline": ""}
    name_idx = next((i for i, ln in enumerate(others) if _looks_like_name(ln)), 0)
    name = others.pop(name_idx)
    headline = " ".join(others).strip()
    return {"name": name, "linkedin_url": url, "headline": headline}


def _parse_line(line):
    for sep in ("\t", " || ", " | "):
        if sep in line:
            fields = [f.strip() for f in line.split(sep) if f.strip()]
            break
    else:
        return None
    url_field = next((f for f in fields if URL_RE.search(f)), None)
    if not url_field:
        return None
    m = URL_RE.search(url_field)
    rest = [f for f in fields if f != url_field]
    name_field = next((f for f in rest if _looks_like_name(f)), None)
    if name_field is None and rest:
        name_field = rest[0]
    headline_fields = [f for f in rest if f != name_field]
    return {"name": name_field or "", "linkedin_url": m.group(0), "headline": " ".join(headline_fields).strip()}


def parse(text):
    """Returns (entries, unparsed_line_count). entries: [{"name", "linkedin_url", "headline"}], one per
    person that had at least a LinkedIn URL. A person with no name or no headline is still returned (with
    that field blank) rather than dropped silently — downstream classification will flag a blank headline
    as no_role, since there is nothing to confirm the firm with.

    Which form it is comes from where the URLs sit, not from whether blank lines are present: if EVERY
    non-blank line already carries its own LinkedIn URL, it is one person per line (delimited fields,
    whether or not blank lines separate them); otherwise it is the multi-line-per-person block form,
    grouped on blank lines."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    md_entries = _parse_markdown(text)
    if md_entries is not None:
        return md_entries, 0
    raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not BOILERPLATE_RE.match(ln.strip())]
    if raw_lines and all(URL_RE.search(ln) for ln in raw_lines):
        entries, unparsed = [], 0
        for line in raw_lines:
            e = _parse_line(line) or _parse_block([line])
            if e:
                entries.append(e)
            else:
                unparsed += 1
        return entries, unparsed
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    entries, unparsed = [], 0
    for b in blocks:
        lines = _clean_block_lines(b)
        if not lines:
            continue
        e = _parse_block(lines)
        if e:
            entries.append(e)
        else:
            unparsed += 1     # no LinkedIn URL anywhere in the block — can't be a person entry
    return entries, unparsed


def firm_mentioned(headline, firm, firm_config, company_matches_firm):
    """True if the headline claims a role at `firm` (word-boundary/acronym aware, exclude-list aware — the
    same fuzzy match used everywhere else in this repo, applied to free text instead of a company name)."""
    return company_matches_firm(headline or "", firm, firm_config)


PAST_SEGMENT_RE = re.compile(r"^\s*(?:ex[- ]|former\b|formerly\b|prev\.?\b|previously\b)", re.IGNORECASE)


def _drop_past_segments(text):
    """Drops any '|' or ','-delimited segment marked as past ("Ex-founder", "Former BCG", "prev. Google")
    before title classification sees it — otherwise a senior/track keyword describing what someone WAS
    (a headline like "BCG | Ex-founder") gets misread as their current role."""
    segs = re.split(r"[|,]", text or "")
    kept = [s for s in segs if not PAST_SEGMENT_RE.match(s.strip())]
    return "|".join(kept).strip() or (text or "").strip()


def extract_title(headline, firm, firm_config, company_matches_firm):
    """Best-effort 'title' portion of a headline like 'Consultant at Boston Consulting Group (BCG)'. Falls
    back to the whole headline (past-tense segments dropped first) when the split can't be confirmed
    against the firm — band/track/support matching below works on substrings either way, so a failed split
    never loses information."""
    m = re.match(r"^(.*?)\s+(?:at|@|,)\s+(.+)$", headline or "", re.IGNORECASE)
    if m and company_matches_firm(m.group(2), firm, firm_config):
        return _drop_past_segments(m.group(1))
    return _drop_past_segments(headline)


def classify(entry, firm, firm_config, title_filters, company_matches_firm, is_non_target_role,
            seniority_flag, is_different_track, is_temporary_role=None, force=None):
    """Returns {"reason": one of the module docstring's values, "title": extracted title}.

    force: an optional manual override — {"exclude"} or {"flag"} — for a name-matched entry the operator
    has already looked at by eye (see find.py's --paste-exclude/--paste-flag). Checked first, before any
    automated rule, since a human's specific judgment about a specific person outranks a general pattern."""
    if force == "exclude":
        return {"reason": "manual_exclude", "title": entry.get("headline") or ""}
    if force == "flag":
        return {"reason": "unclear_role", "title": entry.get("headline") or ""}
    if not is_profile_url(entry.get("linkedin_url")):
        return {"reason": "no_profile_url", "title": ""}
    headline = entry.get("headline") or ""
    if not firm_mentioned(headline, firm, firm_config, company_matches_firm):
        return {"reason": "no_role", "title": ""}
    title = extract_title(headline, firm, firm_config, company_matches_firm)
    if is_non_target_role(title, title_filters):
        return {"reason": "support", "title": title}
    if is_temporary_role and is_temporary_role(title, title_filters):
        return {"reason": "temporary", "title": title}
    if seniority_flag(title) == "SENIOR":
        return {"reason": "above_band", "title": title}
    if is_different_track(title, title_filters):
        return {"reason": "different_track", "title": title}
    return {"reason": "ok", "title": title}
