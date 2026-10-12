"""Networking drafts: find alumni at a company, write one message each by METHOD.md, save as drafts.

  python3 drafts.py --company "Amazon" --role "Product Management Intern" \
      --school "University of Southern California" --n 3

Steps:
  1. People: Exa people search plus CareerShift alumni search, merged by name. CareerShift reveal
     credits are only spent with --reveal N, and only on people who already passed every check.
  2. Filter: titles that fit the role (no recruiters, legal or admin; no engineers unless the role is
     technical), then the Do Not Contact list and the already contacted log.
  3. Write: the message method in method.py, filled from profile.yaml. With ANTHROPIC_API_KEY set,
     Claude writes only the clause about the recipient's work; the checker validates the result and
     the template takes over when it fails or when there is no key.
  4. Save: Gmail drafts when gmail_token.json exists (drafts.create only), and always a Markdown
     file and a CSV under out/. Nothing here sends mail.
  5. Print a review table.
"""
import argparse
import csv
import os
import re
import sys
from datetime import date
from pathlib import Path

import method

HERE = Path(__file__).resolve().parent
PROFILE_PATH = HERE / "profile.yaml"
DNC_CSV_PATH = HERE / "data" / "do_not_contact.csv"
CONTACTED_PATH = HERE / "data" / "contacted.csv"
OUT_DIR = HERE / "out"
CLAUDE_MODEL = "claude-sonnet-5-5"

CONTACTED_FIELDS = ["date", "name", "company", "email", "linkedin_url", "status"]
OUTPUT_FIELDS = ["name", "title", "company", "email", "linkedin_url", "source", "hook", "writer",
                 "subject", "body", "gmail_draft_id"]


class SetupError(RuntimeError):
    pass


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def csv_safe(v):
    from careershift import _csv_safe
    return _csv_safe(v)


# --------------------------------------------------------------------------- profile

REQUIRED_PROFILE = ("name", "school", "year", "major")


def load_profile(path=None):
    import yaml
    path = Path(path or os.environ.get("NETWORKING_PROFILE_YAML") or PROFILE_PATH)
    if not path.exists():
        raise SetupError(f"{path.name} not found. Copy profile.example.yaml to profile.yaml and fill in your own true facts.")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise SetupError(f"{path.name} must be a YAML mapping")
    missing = [k for k in REQUIRED_PROFILE if not str(data.get(k) or "").strip()]
    if missing:
        raise SetupError(f"{path.name} is missing: {', '.join(missing)}")
    return data


# --------------------------------------------------------------------------- do not contact

def load_dnc(path=None, include_json=True):
    """Do Not Contact entries in dnc.py's shape. Fails closed: no CSV means no run.

    CSV columns: name, company, email, linkedin, org, reason. org=yes blocks everyone at the company.
    The older dnc.json list, when present, is merged in so either list blocks a person."""
    import dnc
    path = Path(path or DNC_CSV_PATH)
    if not path.exists():
        raise SetupError(f"{path} not found. Refusing to draft without a Do Not Contact list. "
                         f"Copy data/do_not_contact.example.csv to data/do_not_contact.csv (it can be empty).")
    entries = []
    with path.open(newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f)):
            name = (row.get("name") or "").strip()
            company = (row.get("company") or "").strip()
            is_org = (row.get("org") or "").strip().lower() in ("yes", "y", "true", "1")
            if not (name or company or row.get("email") or row.get("linkedin")):
                continue
            entries.append({
                "id": f"csv{i + 1}", "name": company if is_org else name, "firm": "" if is_org else company,
                "org": is_org, "tier": "never_partner" if is_org else "never_relationship",
                "reason": (row.get("reason") or "").strip(),
                "emails": [e for e in [(row.get("email") or "").strip()] if e],
                "linkedin": [li for li in [dnc.normalize_linkedin(row.get("linkedin"))] if li],
            })
    if include_json and dnc.DNC_PATH.exists():
        entries.extend(dnc.load())
    return entries


def dnc_hit(person, entries):
    import dnc
    row = dict(person, firm_queried=person.get("company"))
    hits = dnc.check(row, entries)
    return dnc.reason_text(hits[0]) if hits else None


# --------------------------------------------------------------------------- already contacted

class ContactedLog:
    """data/contacted.csv: everyone this tool has drafted for, plus the older ledger.json if present."""

    def __init__(self, path=None, ledger=None):
        self.path = Path(path or CONTACTED_PATH)
        self.rows = []
        if self.path.exists():
            with self.path.open(newline="", encoding="utf-8") as f:
                self.rows = list(csv.DictReader(f))
        self.ledger = ledger
        if ledger is None:
            try:
                import ledger as ledger_mod
                if ledger_mod.LEDGER_PATH.exists():
                    self.ledger = ledger_mod.Ledger()
            except (ImportError, ValueError):
                self.ledger = None

    def reason(self, person):
        email = (person.get("email") or "").strip().lower()
        li = _norm(person.get("linkedin_url"))
        nf = (_norm(person.get("name")), _norm(person.get("company")))
        for r in self.rows:
            if email and email == (r.get("email") or "").strip().lower():
                return f"already {r.get('status') or 'contacted'} on {r.get('date')} (email)"
            if li and li == _norm(r.get("linkedin_url")):
                return f"already {r.get('status') or 'contacted'} on {r.get('date')} (LinkedIn)"
            if nf[0] and nf == (_norm(r.get("name")), _norm(r.get("company"))):
                return f"already {r.get('status') or 'contacted'} on {r.get('date')} (name and company)"
        if self.ledger:
            return self.ledger.blocked_reason(dict(person, firm_queried=person.get("company")))
        return None

    def append(self, people, status="drafted", when=None):
        when = when or date.today().isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CONTACTED_FIELDS)
            if new:
                w.writeheader()
            for p in people:
                row = {"date": when, "name": p.get("name"), "company": p.get("company"), "email": p.get("email"),
                       "linkedin_url": p.get("linkedin_url"), "status": status}
                w.writerow({k: csv_safe(v) for k, v in row.items()})
                self.rows.append(row)


# --------------------------------------------------------------------------- people search

def exa_people(company, school, n, city=""):
    """People at `company` from Exa, already matched on school and current firm by find.py."""
    import find
    find.load_dotenv()
    key = os.environ.get("EXA_API_KEY")
    if not key:
        raise SetupError("EXA_API_KEY not set")
    data = find.exa_search(key, find.build_query_plain(school, company, city), n)
    firm_config, title_config = find.load_patterns_cache(), find.load_title_filters()
    out = []
    for r in data.get("results") or []:
        row = find.build_row(r, school, company, [], {}, firm_config, 0, 60, title_config)
        if row.get("firm_match") != "yes" or row.get("school_match") == "no":
            continue
        out.append({"name": row.get("name"), "title": row.get("title"), "company": company,
                    "city": row.get("city"), "email": "", "linkedin_url": row.get("linkedin_url"),
                    "source": "exa", "context": (r.get("text") or "")[:1500]})
    return out


class CareerShiftSource:
    """Search is free. reveal() spends one credit and is only called for people already selected."""

    def __init__(self):
        import careershift
        self.cs = careershift
        self.session = careershift._session()
        self.csrf = None
        self.credits = None

    def people(self, company, school, n):
        cid = self.cs.find_company(self.session, company)
        if not cid:
            return []
        rows = self.cs.search(self.session, school, cid, n)
        return [{"name": p.get("name"), "title": p.get("jobTitle"), "company": company, "city": p.get("city"),
                 "state": p.get("state"), "email": p.get("email") or "", "linkedin_url": "",
                 "source": "careershift", "careershift_id": p.get("externalId"), "has_email": p.get("hasEmail")}
                for p in rows]

    def reveal(self, person):
        if self.csrf is None:
            self.csrf = self.cs._csrf(self.session)
        email, self.credits = self.cs.reveal(self.session, self.csrf, {"externalId": person["careershift_id"]})
        return email


def merge_people(*lists):
    """One record per person, by normalized name. Later lists fill blanks in earlier ones."""
    merged = {}
    for lst in lists:
        for p in lst:
            k = _norm(p.get("name"))
            if not k:
                continue
            if k not in merged:
                merged[k] = dict(p)
                continue
            cur = merged[k]
            for field, val in p.items():
                if val and not cur.get(field):
                    cur[field] = val
            if p.get("source") and p["source"] not in cur.get("source", ""):
                cur["source"] = f"{cur.get('source')}+{p['source']}"
    return list(merged.values())


# --------------------------------------------------------------------------- selection

def select_people(candidates, profile, role, n, industry, dnc_entries, contacted):
    """(chosen, skipped). skipped is [(person, reason)]. Never drafts anyone on either list."""
    chosen, skipped = [], []
    for p in candidates:
        reason = method.skip_reason(p.get("title"), role)
        if not reason:
            hit = dnc_hit(p, dnc_entries)
            reason = f"Do Not Contact: {hit}" if hit else None
        if not reason:
            reason = contacted.reason(p)
        hook = None
        if not reason:
            hook = method.pick_hook(profile, p, industry)
            if not hook:
                reason = "no true hook in profile.yaml fits this person"
        if reason:
            skipped.append((p, reason))
            continue
        topics = hook["item"].get("topics") or []
        p = dict(p, _hook=hook, _score=method.relevance(p.get("title"), role, topics))
        chosen.append(p)
    chosen.sort(key=lambda p: (-p["_score"], 0 if p.get("email") else 1, p.get("name") or ""))
    return chosen[:n], skipped


def reveal_emails(people, source, budget, dnc_entries):
    """Spend at most `budget` CareerShift credits on chosen people with no email. A revealed address
    is checked against Do Not Contact again, and a hit drops the person."""
    kept = []
    for p in people:
        if budget > 0 and source and not p.get("email") and p.get("careershift_id") and p.get("has_email"):
            p["email"] = source.reveal(p) or ""
            budget -= 1
            hit = dnc_hit(p, dnc_entries)
            if hit:
                print(f"  dropped {p.get('name')} after reveal: Do Not Contact: {hit}", file=sys.stderr)
                continue
        kept.append(p)
    return kept


# --------------------------------------------------------------------------- writing

CLAUSE_SYSTEM = (
    "You write one clause for a short networking email. The clause says what the recipient does at "
    "their company, aimed at the specialty named. Start with \"you're\" or \"you\". Use only facts in the "
    "title and profile text given. No numbers, no dashes, no career history, no flattery, no other "
    "companies, nothing about the sender. Under 20 words. Reply with the clause only, no period.")


def make_claude():
    """An Anthropic client when ANTHROPIC_API_KEY is set and the SDK is installed, else None."""
    try:
        import find
        find.load_dotenv()
    except ImportError:
        pass
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        print("ANTHROPIC_API_KEY is set but the anthropic package is missing; using the template.", file=sys.stderr)
        return None
    return anthropic.Anthropic()


def claude_clause(client, person, hook):
    prompt = (f"Recipient title: {person.get('title')}\nCompany: {person.get('company')}\n"
              f"Specialty to aim at: {method.say(hook['topic'])}\n"
              f"Profile text:\n{(person.get('context') or '')[:1200]}")
    resp = client.messages.create(model=CLAUDE_MODEL, max_tokens=100, system=CLAUSE_SYSTEM,
                                  messages=[{"role": "user", "content": prompt}])
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    return text.strip().strip('"').rstrip(".")


def write_message(profile, person, industry="", client=None):
    """{"subject", "body", "hook", "writer"} or None when no compliant message can be written."""
    hook = person.get("_hook") or method.pick_hook(profile, person, industry)
    if not hook:
        return None
    fact = method.pick_fact(profile, method.recipient_tags(person, industry))
    sender_first = profile.get("first_name") or method.first_name(profile["name"])
    rfirst = method.first_name(person.get("name"))
    company = _norm(person.get("company"))
    others = [t for t in profile.get("targets") or [] if _norm(t) != company]

    attempts = []
    if client is not None and hook["kind"] == "work":
        try:
            clause = claude_clause(client, person, hook)
            if not method.check_clause(clause):
                attempts.append(("claude", clause))
        except Exception as e:  # any API failure falls back to the template
            print(f"  Claude unavailable for {person.get('name')}: {type(e).__name__}; using the template", file=sys.stderr)
    attempts.append(("template", None))
    for use_fact in ([fact, None] if fact else [None]):
        for writer, clause in attempts:
            subject, body = method.compose(profile, person, hook, clause, use_fact)
            if not method.check_message(subject, body, rfirst, sender_first, others):
                return {"subject": subject, "body": body, "hook": method.hook_label(hook, use_fact), "writer": writer}
    return None


# --------------------------------------------------------------------------- output

def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "company"


def write_local(drafts, company, out_dir=None, when=None):
    out_dir = Path(out_dir or OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    when = when or date.today().isoformat()
    base = out_dir / f"drafts_{slug(company)}_{when}"
    md = [f"# Networking drafts: {company} ({when})", "", "Drafts only. Nothing was sent.", ""]
    for d in drafts:
        md += [f"## {d['name']}", "", f"- Title: {d.get('title') or ''}", f"- Email: {d.get('email') or 'unknown'}",
               f"- LinkedIn: {d.get('linkedin_url') or ''}", f"- Hook: {d['hook']}", f"- Writer: {d['writer']}",
               f"- Subject: {d['subject']}", "", "```", d["body"], "```", ""]
    Path(f"{base}.md").write_text("\n".join(md), encoding="utf-8")
    with open(f"{base}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        w.writeheader()
        for d in drafts:
            w.writerow({k: csv_safe(d.get(k)) for k in OUTPUT_FIELDS})
    return Path(f"{base}.md"), Path(f"{base}.csv")


def save_gmail(drafts, service):
    import gmail_drafts
    for d in drafts:
        d["gmail_draft_id"] = gmail_drafts.create_draft(service, d.get("email"), d["subject"], d["body"])


def review_table(drafts):
    lines = []
    widths = (24, 34, 30, 34)
    head = ("Name", "Title", "Email", "Hook")
    fmt = "  ".join("{:<%d}" % w for w in widths)
    lines.append(fmt.format(*head))
    lines.append(fmt.format(*("-" * w for w in widths)))
    for d in drafts:
        cells = [d.get("name") or "", d.get("title") or "", d.get("email") or "unknown", d.get("hook") or ""]
        lines.append(fmt.format(*(c[:w] for c, w in zip(cells, widths))))
    for d in drafts:
        lines += ["", f"== {d['name']} ({d['writer']}) | Subject: {d['subject']}", d["body"]]
    return "\n".join(lines)


# --------------------------------------------------------------------------- main

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Write networking drafts for alumni at one company. Never sends.")
    ap.add_argument("--company", required=True)
    ap.add_argument("--role", required=True, help='the posting, e.g. "Product Management Intern"')
    ap.add_argument("--school", help="full school name for search (default: profile school)")
    ap.add_argument("--n", type=int, default=3, help="max people to draft for (default 3)")
    ap.add_argument("--industry", default="", help='e.g. "media"; matches `fits` lists in profile.yaml')
    ap.add_argument("--city", default="", help="narrow the Exa search to a city")
    ap.add_argument("--reveal", type=int, default=0,
                    help="spend up to N CareerShift credits on emails for chosen people (default 0)")
    ap.add_argument("--no-gmail", action="store_true", help="skip Gmail even when configured")
    ap.add_argument("--no-claude", action="store_true", help="use the template even with ANTHROPIC_API_KEY set")
    ap.add_argument("--no-log", action="store_true", help="do not record these people in data/contacted.csv")
    ap.add_argument("--profile", help="path to profile.yaml")
    return ap.parse_args(argv)


def run(args, exa_fn=None, careershift=None, claude=None, gmail_service=None, dnc_entries=None,
        contacted=None, out_dir=None):
    profile = load_profile(args.profile)
    school = args.school or profile["school"]
    dnc_entries = load_dnc() if dnc_entries is None else dnc_entries
    contacted = contacted or ContactedLog()
    pool = max(args.n * 5, 10)

    lists = []
    exa_fn = exa_fn or exa_people
    try:
        lists.append(exa_fn(args.company, school, min(pool, 25), args.city))
    except (SetupError, RuntimeError) as e:
        print(f"Exa search skipped: {e}", file=sys.stderr)
    if careershift is None:
        try:
            careershift = CareerShiftSource()
        except Exception as e:
            print(f"CareerShift skipped: {e}", file=sys.stderr)
            careershift = False
    if careershift:
        try:
            lists.append(careershift.people(args.company, school, pool))
        except Exception as e:
            print(f"CareerShift search failed: {type(e).__name__}: {e}", file=sys.stderr)
    candidates = merge_people(*lists)
    if not candidates:
        print("No people found. Check EXA_API_KEY and the CareerShift session.", file=sys.stderr)
        return [], []

    chosen, skipped = select_people(candidates, profile, args.role, args.n, args.industry, dnc_entries, contacted)
    if args.reveal:
        chosen = reveal_emails(chosen, careershift or None, args.reveal, dnc_entries)

    if claude is None and not args.no_claude:
        claude = make_claude()
    drafts = []
    for p in chosen:
        msg = write_message(profile, p, args.industry, None if args.no_claude else claude)
        if not msg:
            skipped.append((p, "could not write a message that passes the checker"))
            continue
        drafts.append(dict({k: v for k, v in p.items() if not k.startswith("_")}, **msg))

    if drafts and not args.no_gmail:
        service = gmail_service
        if service is None:
            import gmail_drafts
            if gmail_drafts.is_configured():
                try:
                    service = gmail_drafts.build_service()
                except Exception as e:
                    print(f"Gmail skipped: {type(e).__name__}: {e}", file=sys.stderr)
        if service is not None:
            save_gmail(drafts, service)
            print(f"Created {len(drafts)} Gmail drafts. Nothing was sent.", file=sys.stderr)
    if drafts:
        md, csv_path = write_local(drafts, args.company, out_dir)
        print(f"Wrote {md} and {csv_path}", file=sys.stderr)
        if not args.no_log:
            contacted.append(drafts)
    return drafts, skipped


def main(argv=None):
    args = parse_args(argv)
    try:
        drafts, skipped = run(args)
    except SetupError as e:
        print(f"drafts: {e}", file=sys.stderr)
        return 2
    if skipped:
        print(f"\nSkipped {len(skipped)}:", file=sys.stderr)
        for p, why in skipped:
            print(f"  {p.get('name')} ({p.get('title') or 'no title'}): {why}", file=sys.stderr)
    if drafts:
        print()
        print(review_table(drafts))
    return 0 if drafts else 1


if __name__ == "__main__":
    sys.exit(main())
