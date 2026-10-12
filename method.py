"""The networking message method (METHOD.md) as code: hook selection, the template, and the checker.

Pure: no network, no file writes. outreach.py does the I/O.

Every fact about the sender comes from the profile. The only text that may come from anywhere else
is `recipient_clause`, the part that says what the recipient does, and check_message() still runs
over the whole message afterwards.
"""
import re

MAX_BODY_WORDS = 90
MIN_SENTENCES, MAX_SENTENCES = 3, 4

CLOSE = "Would you have 15 minutes in the next couple weeks?"
INTERESTING = "which is really interesting to me"

# U+2010 to U+2015 and U+2212: every dash-like character except the ASCII hyphen inside a word.
DASH_CHARS = "‐‑‒–—―−"
SPACED_HYPHEN_RE = re.compile(r"\s-+\s|--")
BANNED = [
    (re.compile(r"\brather\b", re.I), '"rather"'),
    (re.compile(r"\bI saw\b", re.I), '"I saw"'),
    (re.compile(r"\bI found your profile\b", re.I), '"I found your profile"'),
    (re.compile(r"\bday[\s-]to[\s-]day\b", re.I), '"day to day"'),
]
# The career-path recital: "after X and Y", "you went from X to Y", "before joining X".
CAREER_PATH_RES = [
    re.compile(r"\bafter\s+(?:your\s+)?(?:time|years?|stints?|roles?|working)\b", re.I),
    re.compile(r"\bafter\s+[A-Z][\w&.]*(?:\s+[A-Z][\w&.]*)*,?\s+and\s+[A-Z]", re.UNICODE),
    re.compile(r"\b(?:you\s+)?went\s+from\b", re.I),
    re.compile(r"\bmoved\s+from\b.+\bto\b", re.I),
    re.compile(r"\bbefore\s+joining\b", re.I),
    re.compile(r"\byour\s+(?:path|journey)\s+from\b", re.I),
]
DIGIT_RE = re.compile(r"\d")
SENTENCE_END_RE = re.compile(r"[.!?]+(?=\s|$)")

TECH_ROLE_RE = re.compile(
    r"\b(engineer|engineering|developer|software|swe|sde|data scien\w*|machine learning|ml|devops|"
    r"security|hardware|firmware|programmer|technical)\b", re.I)
# Never a networking target: they hire, they advise the company, or they support someone else.
SKIP_ALWAYS_RE = re.compile(
    r"\b(recruit\w*|talent acquisition|sourcer|sourcing|legal|counsel|attorney|lawyer|paralegal|"
    r"administrative|admin|executive assistant|assistant to|office manager|receptionist|intern|student)\b", re.I)
# Skipped for a business posting, kept for a technical one.
SKIP_UNLESS_TECH_RE = re.compile(
    r"\b(engineer|engineering|developer|software|swe|sde|programmer|devops|qa|sre|architect|"
    r"data scien\w*|scientist|technician)\b", re.I)

# Words a posting title shares with a related job title. Extend freely.
ROLE_FAMILIES = {
    "product": ["product", "pm", "program"],
    "marketing": ["marketing", "brand", "growth", "communications", "content", "social", "advertising"],
    "finance": ["finance", "financial", "investment", "investing", "banking", "treasury", "fp&a", "capital"],
    "strategy": ["strategy", "strategic", "business development", "partnerships", "operations", "consulting"],
    "sales": ["sales", "account", "partnerships", "business development"],
    "data": ["data", "analytics", "insights", "analyst"],
    "design": ["design", "designer", "ux", "creative"],
    "engineering": ["engineer", "engineering", "software", "developer"],
}

SAY = {"ai": "AI", "ml": "ML", "ux": "UX", "pr": "PR", "fp&a": "FP&A"}


def say(phrase):
    """Lowercase topic -> the way it reads mid-sentence ("ai marketing" -> "AI marketing")."""
    return " ".join(SAY.get(w.lower(), w) for w in str(phrase).split())


# --------------------------------------------------------------------------- recipient fit

def is_technical_role(role):
    return bool(TECH_ROLE_RE.search(role or ""))


def skip_reason(title, role):
    """Why a person with this title is not worth messaging for this role, or None."""
    title = title or ""
    if not title.strip():
        return "no title"
    m = SKIP_ALWAYS_RE.search(title)
    if m:
        return f"title is {m.group(0).lower()}"
    if not is_technical_role(role):
        m = SKIP_UNLESS_TECH_RE.search(title)
        if m:
            return f"title is {m.group(0).lower()} and the role is not technical"
    return None


def _has(text, word):
    return re.search(r"(?<![\w&])" + re.escape(word.lower()) + r"(?![\w&])", (text or "").lower()) is not None


def role_families(role):
    return [fam for fam, words in ROLE_FAMILIES.items() if any(_has(role, w) for w in [fam] + words)]


def relevance(title, role, topics=()):
    """How well a recipient's title fits the posting and the sender's own topics. Higher is better."""
    score = 0
    for fam in role_families(role):
        if any(_has(title, w) for w in [fam] + ROLE_FAMILIES[fam]):
            score += 2
    score += sum(1 for t in topics if _has(title, t))
    return score


# --------------------------------------------------------------------------- hooks

def recipient_tags(recipient, industry=""):
    """Lowercased strings a `fits` entry may match: industry, company, city, state."""
    vals = [industry, recipient.get("company"), recipient.get("city"), recipient.get("state")]
    return [str(v).strip().lower() for v in vals if v and str(v).strip()]


def fits(item, tags):
    """True when a profile item may be used for this recipient. No `fits` list means any recipient,
    except for notable facts, which always need an explicit match (see pick_fact)."""
    want = [str(f).strip().lower() for f in (item.get("fits") or [])]
    if not want or "any" in want:
        return True
    return any(w == t or w in t for w in want for t in tags)


def pick_fact(profile, tags):
    """The first notable fact whose `fits` list matches this recipient, or None. A fact without a
    `fits` list never appears: notable facts are only for the recipients they were written for."""
    for fact in profile.get("notable_facts") or []:
        if fact.get("fits") and fits(fact, tags) and str(fact.get("text") or "").strip():
            return fact
    return None


def pick_hook(profile, recipient, industry="", exclude=()):
    """The sender's best true peer link to this recipient.

    Returns {"kind": "work", "item": club_or_job, "topic": matched_topic} when a club or job the
    sender really does matches a word in the recipient's title, else {"kind": "aspiration", ...} when
    the profile has a true aspiration for this industry, else None (skip the person).
    `exclude` holds hook names already used at this company, so two people there never get the same pitch."""
    tags = recipient_tags(recipient, industry)
    title = recipient.get("title") or ""
    best, best_score, best_topic = None, 0, None
    for item in list(profile.get("clubs") or []) + list(profile.get("past_work") or []):
        if not (item.get("what_i_do") and item.get("advice_on")) or not fits(item, tags):
            continue
        if hook_name(item) in exclude:
            continue
        matched = [t for t in item.get("topics") or [] if _has(title, t)]
        if len(matched) > best_score:
            best, best_score = item, len(matched)
            # Every matched word, in title order: "Product Marketing Manager" -> "product marketing".
            best_topic = " ".join(sorted(matched, key=lambda t: title.lower().find(t.lower())))
    if best:
        return {"kind": "work", "item": best, "topic": best_topic}
    for asp in profile.get("aspirations") or []:
        if asp.get("industry") and fits(asp, tags) and hook_name(asp) not in exclude:
            return {"kind": "aspiration", "item": asp, "topic": None}
    return None


def hook_name(item):
    return str(item.get("name") or item.get("industry") or item.get("what_i_do") or "").strip().lower()


def hook_label(hook, fact=None):
    if not hook:
        return ""
    item = hook["item"]
    label = item.get("name") or item.get("industry") or hook["kind"]
    label = f"{hook['kind']}: {label}"
    if fact:
        label += f" + fact: {fact.get('name') or fact.get('text')}"
    return label


# --------------------------------------------------------------------------- template

def first_name(full_name):
    parts = re.sub(r"\(.*?\)", " ", full_name or "").split()
    parts = [p for p in parts if p.lower().rstrip(".") not in ("dr", "mr", "mrs", "ms", "prof")]
    return parts[0].strip(",") if parts else ""


def template_recipient_clause(recipient, hook):
    """What the recipient does, from facts we have: the matched topic and their company."""
    company = recipient.get("company") or "your company"
    return f"you work on {say(hook['topic'])} at {company}"


def article(word):
    """"a" or "an" by sound: "an NYU", "a USC", "an Example State", "a University"."""
    w = (word or "").strip()
    if not w:
        return "a"
    first = w.split()[0]
    if first.isupper() and len(first) > 1:
        return "an" if first[0] in "AEFHILMNORSX" else "a"
    return "an" if first[0].lower() in "aeio" else "a"


def intro_sentence(profile, fact=None):
    school = profile.get("school_short") or profile["school"]
    s = f"I'm {profile.get('intro_article') or article(school)} {school} {profile['year']} studying {profile['major']}"
    if fact:
        s += f", and I {str(fact['text']).strip().rstrip('.')}"
    return s + "."


def compose(profile, recipient, hook, recipient_clause=None, fact=None):
    """(subject, body) for one recipient. recipient_clause overrides the template clause (Claude's
    wording); everything about the sender comes from the profile."""
    item = hook["item"]
    sentences = [intro_sentence(profile, fact)]
    if hook["kind"] == "work":
        clause = (recipient_clause or template_recipient_clause(recipient, hook)).strip().rstrip(".,")
        sentences.append(f"I {str(item['what_i_do']).strip().rstrip('.')}, and {clause}, {INTERESTING}.")
        sentences.append(f"I'd love any tips you have for someone {str(item['advice_on']).strip().rstrip('.')} like me.")
    else:
        ind = str(item["industry"]).strip()
        sentences.append(f"I'm hoping to start working with {ind} clients, and you're at one of the top {ind} companies.")
        sentences.append("I'd love any advice for someone trying to break in.")
    sentences.append(CLOSE)
    sender_first = profile.get("first_name") or first_name(profile["name"])
    body = f"Hi {first_name(recipient['name'])},\n\n{' '.join(sentences)}\n\nThanks,\n{sender_first}"
    school = profile.get("school_short") or profile["school"]
    subject = f"{school} {profile['year']} with a question"
    return subject, body


# --------------------------------------------------------------------------- checker

def _core(body):
    """The body between the greeting line and the sign off."""
    lines = body.strip().split("\n")
    if lines and lines[0].startswith("Hi "):
        lines = lines[1:]
    text = "\n".join(lines)
    text = re.sub(r"\n\s*Thanks,\s*\n.*$", "", text, flags=re.S)
    return text.strip()


def count_sentences(text):
    return len(SENTENCE_END_RE.findall(text))


def check_message(subject, body, recipient_first, sender_first, other_companies=()):
    """List of rule violations; empty means the message follows the method."""
    v = []
    full = f"{subject}\n{body}"
    if not body.startswith(f"Hi {recipient_first},\n"):
        v.append(f'does not open "Hi {recipient_first},"')
    if not re.search(r"\nThanks,\n" + re.escape(sender_first) + r"\s*$", body):
        v.append(f'does not sign off "Thanks," then "{sender_first}"')
    found = sorted({c for c in full if c in DASH_CHARS})
    if found:
        v.append(f"contains dash character(s) {found!r}")
    if SPACED_HYPHEN_RE.search(full):
        v.append('contains " - " or "--" used as a dash')
    for rx, label in BANNED:
        if rx.search(full):
            v.append(f"contains {label}")
    if DIGIT_RE.search(full.replace("15 minutes", "")):
        v.append("contains a number (only the 15 minute ask may have one)")
    q = full.count("?")
    if q != 1:
        v.append(f"has {q} question marks (exactly one)")
    for rx in CAREER_PATH_RES:
        m = rx.search(full)
        if m:
            v.append(f'recites their career path ("{m.group(0)}")')
            break
    core = _core(body)
    words = len(core.split())
    if words > MAX_BODY_WORDS:
        v.append(f"body is {words} words (max {MAX_BODY_WORDS})")
    n = count_sentences(core)
    if not MIN_SENTENCES <= n <= MAX_SENTENCES:
        v.append(f"body has {n} sentences ({MIN_SENTENCES} to {MAX_SENTENCES})")
    if len(re.findall(r"\bI'd love\b", core)) > 1:
        v.append("more than one ask")
    for other in other_companies or ():
        if other and _has(full, other):
            v.append(f"mentions another company ({other})")
    return v


def check_clause(clause):
    """Checks on a recipient clause written by Claude, before it goes into the template."""
    v = []
    c = (clause or "").strip()
    if not c.lower().startswith(("you're", "you are", "you ")):
        v.append('clause must start with "you"')
    if re.search(r"\b(I|I'm|I've|my|me)\b", c):
        v.append("clause talks about the sender")
    if len(c.split()) > 25:
        v.append("clause is too long")
    if "\n" in c or count_sentences(c + " ") > 1:
        v.append("clause is more than one sentence")
    return v
