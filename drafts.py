"""Stage 3: draft_subject / draft_body generation from fixed string templates.

No model calls — everything here is deterministic string interpolation plus
a hard content-rule validator. See test_drafts.py for the enforcement tests.
"""

import json
import re
from pathlib import Path

BANNED_PHRASES = [
    "pick your brain",
    "hope this finds you well",
    "let me know if you'd be open to connecting",
    "i was impressed by",
]

# Dash-family characters banned in prose: U+2010 hyphen, U+2011 non-breaking hyphen, U+2012 figure
# dash, U+2013 en dash, U+2014 em dash, U+2015 horizontal bar, U+2212 minus sign.
# The ASCII hyphen-minus (U+002D) is NOT in this set. Settled rule: it is allowed INSIDE compounds
# ("Simon-Kucher", "semester-long", "third-year", "data-driven").
DASH_CHARS = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"

# An ASCII hyphen that is NOT inside a compound: it must have a word character on both sides.
# Catches a spaced " - ", "--", a leading "- " bullet, and a dangling trailing hyphen.
HYPHEN_AS_PUNCTUATION_RE = re.compile(r"(?<!\w)-|-(?!\w)")

RATHER_RE = re.compile(r"\brather\b", re.IGNORECASE)
DIGIT_RE = re.compile(r"\d")
SENTENCE_END_RE = re.compile(r"[.!?]+(?:\s|$)")
# Catches a missing template value (e.g. target_school=None) leaking into
# the rendered text as the literal word "None" — case-sensitive and word-
# bounded so it never flags the ordinary lowercase word "none".
NONE_LEAK_RE = re.compile(r"\bNone\b")


class DraftContentError(ValueError):
    """Raised when generated draft content violates a hard content rule."""


def count_sentences(body):
    """Counts sentence-terminating punctuation, not text fragments — a
    greeting ("Hi Jane,") or signature line ("Alex") with no terminal
    punctuation isn't a sentence."""
    return len(SENTENCE_END_RE.findall(body))


def validate_draft(subject, body, allowed_numbers=()):
    """Returns a list of violation strings; empty list means the draft is clean.

    allowed_numbers: literal digit strings the template itself inserted about the RECIPIENT (for example the
    year they joined their firm). Those exact tokens are exempt from the digit rule. Any other digit, in
    particular any number about the sender, is still a violation."""
    violations = []
    full_text = f"{subject}\n{body}"

    dashes_found = sorted(set(c for c in full_text if c in DASH_CHARS))
    if dashes_found:
        violations.append(f"contains dash character(s): {dashes_found!r}")
    if HYPHEN_AS_PUNCTUATION_RE.search(full_text):
        violations.append("contains a hyphen used as punctuation (not inside a compound word)")

    if RATHER_RE.search(full_text):
        violations.append('contains the word "rather"')

    n_sentences = count_sentences(body)
    if n_sentences > 5:
        violations.append(f"body has {n_sentences} sentences (max 5)")

    digit_text = full_text.replace("15 minutes", "")     # the fixed ask, the one number the copy rules require
    for token in allowed_numbers:
        digit_text = re.sub(r"(?<!\d)" + re.escape(str(token)) + r"(?!\d)", "", digit_text)
    if DIGIT_RE.search(digit_text):
        violations.append("contains a digit (no numbers about the sender)")

    lower = full_text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lower:
            violations.append(f'contains banned phrase "{phrase}"')

    if NONE_LEAK_RE.search(full_text):
        violations.append('contains the literal word "None" — a template value was missing (e.g. no target_school)')

    return violations


SCHOOL_DISPLAY_PATH = Path(__file__).resolve().parent / "school_display.json"
_SCHOOL_DISPLAY_CACHE = None


def _norm_school(name):
    return re.sub(r"\W", "", (name or "").lower())


def load_school_display():
    """normalized full name -> short spoken form. Missing file or bad JSON means no mapping (safe)."""
    global _SCHOOL_DISPLAY_CACHE
    if _SCHOOL_DISPLAY_CACHE is None:
        table = {}
        try:
            raw = json.loads(SCHOOL_DISPLAY_PATH.read_text())
            table = {_norm_school(k): v for k, v in raw.items() if not k.startswith("_") and isinstance(v, str) and v.strip()}
        except (OSError, ValueError):
            pass
        _SCHOOL_DISPLAY_CACHE = table
    return _SCHOOL_DISPLAY_CACHE


def display_school(name, override=None):
    """The form of a school name a person would say out loud. MATCHING always uses the full name;
    only the draft text uses this. Precedence: explicit override, then school_display.json, then the
    full name unchanged."""
    if override and override.strip():
        return override.strip()
    if not name:
        return name
    return load_school_display().get(_norm_school(name), name)


def hook_line(tier, target_school, org):
    """Tier 1 gets both hooks (school, then org) as a single sentence — the
    two-hook maximum, never a third. Tier 4 (neither confirmed) has no valid
    hook, so it returns None rather than fabricating a personalization."""
    if tier == 1:
        return f"I saw you went to {target_school} and you're a {org} too."
    if tier == 2:
        return f"I saw you're a {org} too."
    if tier == 3:
        return f"I saw you went to {target_school} too."
    return None


def recipient_numbers(row):
    """Digit strings a draft may carry about the RECIPIENT: their firm start year (longer tenure template)."""
    y = str((row or {}).get("firm_start_year") or "")
    return (y,) if y.isdigit() else ()


def render_draft(
    recipient_first_name, tier, target_school, org, firm,
    sender_first_name, sender_school, sender_year, sender_industry,
    sender_signature=None, school_short=None, facts=None, choice=None,
):
    """Returns (subject, body), or (None, None) if tier has no valid hook. Raises DraftContentError if the
    rendered draft would violate a content rule.

    Tier 3 (school confirmed) uses the six-template HIERARCHY in variants.py: the available facts pick the
    draft, first match wins. `choice` may be a precomputed variants.select_template() result; None computes
    it from `facts`. Tiers 1 and 2 (--org runs) keep their fixed org hook with the same shared ask.

    target_school is the FULL name (matching used it); the text says display_school(target_school). The firm
    is said the way a person says it ("Bain", not "Bain & Company"). The signature, if given, stands alone
    as the sign-off; otherwise the first name."""
    import variants

    school_say = display_school(target_school, school_short)
    firm_say = variants.display_firm(firm)
    numbers = []
    if tier == 3:
        choice = choice or variants.select_template(facts)
        subject, para = variants.render_template(choice, school_say, firm_say, sender_school, sender_year, sender_industry)
        numbers = variants.template_numbers(choice)
    else:
        hook = hook_line(tier, school_say, org)
        if hook is None:
            return None, None
        subject = f"{sender_school} {sender_year} interested in {firm_say}"
        para = (f"{hook} I'm preparing for {sender_industry} recruiting, and I'd love to hear about your path to {firm_say}.")

    signoff = sender_signature.strip() if sender_signature and sender_signature.strip() else sender_first_name
    body = f"Hi {recipient_first_name},\n\n{para}\n\n{variants.ASK}\n\n{signoff}"

    violations = validate_draft(subject, body, allowed_numbers=numbers)
    if violations:
        raise DraftContentError(f"generated draft violated content rules: {violations}")
    return subject, body


def render_followup(recipient_first_name, firm, sender_first_name, original_subject):
    """The ONE follow-up. Sent 6 to 8 days after the first email, in the same
    thread, only if there has been no reply. There is no second follow-up.

    Same content rules as the first email (validate_draft): short, one ask, no
    dashes, no digits, no easy outs, no groveling. Returns (subject, body).
    Raises DraftContentError if the rendered text would break a rule."""
    import variants
    firm = variants.display_firm(firm)
    subject = f"Re: {original_subject}"
    body = (
        f"Hi {recipient_first_name},\n\n"
        f"Following up on my earlier note about {firm} in case it got buried. {variants.ASK}\n\n"
        f"{sender_first_name}"
    )
    violations = validate_draft(subject, body)
    if violations:
        raise DraftContentError(f"generated follow-up violated content rules: {violations}")
    return subject, body
