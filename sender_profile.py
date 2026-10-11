"""The sender's own profile: who is writing the drafts, and which shared facts the templates may claim.

Read from profile.json next to this file (gitignored), or from the path in NETWORKING_PROFILE. Copy
profile.example.json to profile.json and fill it in. A missing file means an empty profile: the templates
that depend on a profile field (shared high school, shared major, home city, shared organization) are
simply never offered, and the CLI asks for the sender flags instead.

Every field is optional:
  name, school, year, industry, signature   defaults for --sender-name/--sender-school/--sender-year/
                                            --sender-industry/--sender-signature
  high_school, high_school_short            a shared high school (full name for matching, short to say)
  major, major_long                         a shared major (matched in the recipient's degree line)
  home_city, home_area                      the city matched in a recipient's LinkedIn location, and how
                                            the draft says it ("I'm from the <home_area> area too")
  orgs                                      [{"name": said in the email, "match": [full spellings]}]
                                            Only orgs the sender is really in. Never a bare acronym.
"""
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILE_PATH = HERE / "profile.json"
EXAMPLE_PATH = HERE / "profile.example.json"


def load_profile(path=None):
    """dict of the profile, or {} if there is no profile file. Keys starting with "_" are comments."""
    path = Path(path or os.environ.get("NETWORKING_PROFILE") or PROFILE_PATH)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_")} if isinstance(raw, dict) else {}


def field(profile, key):
    """A trimmed string field, '' when missing."""
    return str((profile or {}).get(key) or "").strip()
