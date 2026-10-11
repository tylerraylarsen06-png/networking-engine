"""Do Not Contact list. Checked BEFORE any Hunter or ZeroBounce spend, and again in bridge.py before queueing.

The list lives in dnc.json next to this file (gitignored: it holds real names). Start from
dnc.example.json, or let `python3 find.py --add-dnc "Full Name" --dnc-firm "Firm"` create it.

Rules:
  * A person is excluded on ANY single hit: email, LinkedIn URL, or name plus firm.
  * An AMBIGUOUS match is excluded too and flagged for review. A false exclusion costs one email;
    a false inclusion costs a relationship.
  * Every exclusion is logged with its reason (dnc-exclusions.log, JSON lines).
  * Entries are NEVER removed by code. There is no remove function on purpose. Delete a line from
    dnc.json by hand if a removal is really intended.
  * Fail closed: if dnc.json is missing or unreadable, callers must stop, not carry on without it.

Tiers:
  never_relationship  live relationships, contact only personally by hand
  never_colleague     current colleagues
  gated               an employer's clients and prospects, ask before contacting
  never_partner       organizations you already work with (anyone there)
"""
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

DNC_PATH = Path(__file__).resolve().parent / "dnc.json"
LOG_PATH = Path(__file__).resolve().parent / "dnc-exclusions.log"

TIERS = ("never_relationship", "never_colleague", "gated", "never_partner")

# Common short forms, so "Ed Sample" or "Matt Example" is recognized as the person on the list.
NICKNAMES = {
    "ed": "edward", "eddie": "edward", "eddy": "edward", "ted": "edward",
    "matt": "matthew", "matty": "matthew",
    "kev": "kevin", "mike": "michael", "mikey": "michael",
    "dan": "daniel", "danny": "daniel",
    "chris": "christopher", "nick": "nicholas", "alex": "alexander",
    "sam": "samuel", "ben": "benjamin", "joe": "joseph", "joey": "joseph",
    "bob": "robert", "rob": "robert", "bobby": "robert",
    "will": "william", "bill": "william", "liz": "elizabeth", "beth": "elizabeth",
    "kate": "katherine", "katie": "katherine", "kathy": "katherine", "kaitlyn": "kaitlyn",
    "cathy": "catherine", "kat": "catherine", "fran": "frances",
    "mo": "maureen", "shirl": "shirley", "andy": "andrew", "drew": "andrew", "tony": "anthony",
    "steve": "steven", "jim": "james", "jimmy": "james", "jon": "jonathan", "zach": "zachary",
}


class DNCError(RuntimeError):
    """The list could not be read. Callers stop rather than run without it."""


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _name_tokens(name):
    name = re.sub(r"\(.*?\)", " ", name or "")
    name = re.sub(r"\b(dr|prof|professor|mr|mrs|ms|jr|sr|ii|iii|phd|mba|cfa|cpa)\b\.?", " ", name, flags=re.I)
    toks = re.findall(r"[a-z]+", name.lower())
    return toks


def _canon_first(tok):
    return NICKNAMES.get(tok, tok)


def _firm_hit(a, b):
    """Loose company match: one normalized name contains the other, both at least 4 characters."""
    a, b = _norm(a), _norm(b)
    if len(a) < 4 or len(b) < 4:
        return a == b and bool(a)
    return a in b or b in a


def normalize_linkedin(url):
    m = re.search(r"linkedin\.com/(in|pub)/([^/?#\s]+)", url or "", re.IGNORECASE)
    return f"{m.group(1).lower()}/{m.group(2).lower()}" if m else ""


def normalize_email(email):
    return (email or "").strip().lower()


# ------------------------------------------------------------------ storage

def load(path=None):
    path = Path(path) if path else DNC_PATH
    if not path.exists():
        raise DNCError(f"{path.name} not found. Refusing to run without a Do Not Contact list. "
                       f"Restore it, or create it with: python3 find.py --add-dnc \"Full Name\" --dnc-firm \"Firm\"")
    try:
        data = json.loads(path.read_text() or "{}")
        entries = data["entries"]
        if not isinstance(entries, list):
            raise ValueError("entries is not a list")
    except (ValueError, KeyError) as e:
        raise DNCError(f"{path.name} is unreadable ({e}). Refusing to run without a Do Not Contact list.")
    return entries


def _save(entries, path=None):
    path = Path(path) if path else DNC_PATH
    payload = json.dumps({"version": 1, "entries": entries}, indent=1, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".dnc-", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(payload + "\n")
    os.replace(tmp, path)


def add(name, firm="", tier="never_relationship", reason="", emails=(), linkedin=(), org=False,
        source="cli", path=None):
    """Append an entry (or widen an existing one with new emails/LinkedIn). Never removes or downgrades.
    Returns (entry, created)."""
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}")
    if not (name or "").strip():
        raise ValueError("a name is required")
    try:
        entries = load(path)
    except DNCError:
        entries = []          # adding is how a missing list gets created
    key = (_norm(name), _norm(firm), bool(org))
    for e in entries:
        if (_norm(e.get("name")), _norm(e.get("firm")), bool(e.get("org"))) == key:
            for em in emails:
                if normalize_email(em) not in e["emails"]:
                    e["emails"].append(normalize_email(em))
            for li in linkedin:
                if normalize_linkedin(li) and normalize_linkedin(li) not in e["linkedin"]:
                    e["linkedin"].append(normalize_linkedin(li))
            _save(entries, path)
            return e, False
    entry = {
        "id": "d" + str(len(entries) + 1).zfill(3), "name": name.strip(), "firm": (firm or "").strip(),
        "org": bool(org), "tier": tier, "reason": reason, "emails": [normalize_email(e) for e in emails],
        "linkedin": [normalize_linkedin(u) for u in linkedin if normalize_linkedin(u)],
        "added": datetime.now().strftime("%Y-%m-%d"), "source": source,
    }
    entries.append(entry)
    _save(entries, path)
    return entry, True


# ------------------------------------------------------------------ matching

def _row_firm(row):
    return row.get("firm_queried") or row.get("firm") or ""


def _person_hits(entry, row):
    """(kind, review) for a person entry against a row, or None."""
    ename = _name_tokens(entry["name"])
    rname = _name_tokens(row.get("name"))
    if not ename or not rname:
        return None
    efirm = entry.get("firm") or ""
    rfirm = _row_firm(row)
    firm_matches = bool(efirm) and _firm_hit(efirm, rfirm)

    if len(ename) == 1:
        # First name only ("Emily"): meaningless alone, so it needs the entry's firm to agree. A same
        # first name at some other firm is a different person.
        if _canon_first(ename[0]) == _canon_first(rname[0]) and firm_matches:
            return ("first name + firm", False)
        if _canon_first(ename[0]) == _canon_first(rname[0]) and not efirm:
            return ("first name only, entry has no firm", True)
        return None

    e_first, e_last = _canon_first(ename[0]), ename[-1]
    r_first, r_last = _canon_first(rname[0]), rname[-1]
    if e_last != r_last and not (e_last in rname and e_first == r_first):
        return None
    exact = e_first == r_first
    initial = (len(rname[0]) == 1 and rname[0] == ename[0][0]) or (len(ename[0]) == 1 and ename[0] == rname[0][0])
    prefix = ename[0].startswith(rname[0]) or rname[0].startswith(ename[0])
    if exact:
        if firm_matches:
            return ("name + firm", False)
        return ("name matches, firm differs or unknown", True)
    if initial or prefix:
        return ("similar first name and same last name", True)
    return None


def check(row, entries):
    """All hits for one row: list of {entry, kind, review}. Empty list means clear."""
    hits = []
    email = normalize_email(row.get("email"))
    li = normalize_linkedin(row.get("linkedin_url"))
    rfirm = _row_firm(row)
    for e in entries:
        kind, review = None, False
        if email and email in [normalize_email(x) for x in e.get("emails", [])]:
            kind = "email"
        elif li and li in e.get("linkedin", []):
            kind = "LinkedIn URL"
        elif e.get("org"):
            if _firm_hit(e["name"], rfirm):
                kind = "organization"
        else:
            res = _person_hits(e, row)
            if res:
                kind, review = res
        if kind:
            hits.append({"entry": e, "kind": kind, "review": review})
    return hits


def reason_text(hit):
    e = hit["entry"]
    who = e["name"] + (f" ({e['firm']})" if e.get("firm") else "")
    flag = " [REVIEW: ambiguous match, excluded to be safe]" if hit["review"] else ""
    return f"{e['tier']}: {who}, matched on {hit['kind']}{flag}. {e.get('reason', '')}".strip()


def log_exclusion(row, hits, context="", path=None):
    path = Path(path) if path else LOG_PATH
    with path.open("a", encoding="utf-8") as f:
        for h in hits[:1]:
            f.write(json.dumps({
                "at": datetime.now().isoformat(timespec="seconds"), "context": context,
                "name": row.get("name", ""), "firm": _row_firm(row), "entry": h["entry"]["id"],
                "tier": h["entry"]["tier"], "matched_on": h["kind"], "review": h["review"],
                "reason": reason_text(h),
            }, ensure_ascii=False) + "\n")


def filter_rows(rows, entries=None, context="", log=True):
    """Split rows into (clear, excluded[(row, hits)]). Excluded rows never reach Hunter or ZeroBounce."""
    entries = load() if entries is None else entries
    clear, excluded = [], []
    for row in rows:
        hits = check(row, entries)
        if hits:
            excluded.append((row, hits))
            if log:
                log_exclusion(row, hits, context)
        else:
            clear.append(row)
    return clear, excluded


def summarize(excluded):
    """Printable lines. Names appear so the run summary shows who was caught."""
    review = sum(1 for _, hits in excluded if any(h["review"] for h in hits))
    lines = [f"Do Not Contact: {len(excluded)} excluded ({review} flagged for review as ambiguous)"]
    for row, hits in excluded:
        lines.append(f"  - {row.get('name', '?')} ({_row_firm(row)}): {reason_text(hits[0])}")
    return lines
