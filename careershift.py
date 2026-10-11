"""CareerShift people + email lookup, using your own logged-in CareerShift session.

CareerShift is free through many universities' career centers. Its web app runs on a JSON API
(app.careershift.com/api/v2). This module calls that API with the session cookies that
api-anything imports from your everyday browser, so there is no password and no scraping:

  api-anything login app.careershift.com      # once, imports your browser session

Then:

  python3 careershift.py --school "University of Southern California" --firm "FOX" --n 10
  python3 careershift.py --school "University of Southern California" --firm "FOX" --n 10 --reveal 3

Search is free and returns every alum it knows at the firm, with the email when CareerShift has
already unlocked it for you. --reveal N unlocks emails for up to N more people. Each reveal uses
one of your account's daily credits (50 a day on a typical university license); the remaining
count is printed after every reveal.
"""
import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

import requests

BASE = "https://app.careershift.com"
SESSION_FILE = Path(os.environ.get("API_ANYTHING_HOME", Path.home() / ".api-anything")) / "sessions" / "app.careershift.com.json"


class CareerShiftError(RuntimeError):
    pass


def _session():
    if not SESSION_FILE.exists():
        raise CareerShiftError(
            "No CareerShift session. Sign in to CareerShift in your browser, then run: "
            "api-anything login app.careershift.com")
    data = json.loads(SESSION_FILE.read_text())
    s = requests.Session()
    for c in data.get("cookies", []):
        s.cookies.set(c["name"], c["value"], domain=c.get("domain", "app.careershift.com"), path=c.get("path", "/"))
    s.headers.update({"accept": "application/json", "user-agent": "Mozilla/5.0 networking-engine"})
    return s


def _csrf(s):
    html = s.get(f"{BASE}/Contacts/Search", headers={"accept": "text/html"}, timeout=30).text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    if not m:
        raise CareerShiftError("Could not read a CSRF token. The session probably expired: run api-anything login app.careershift.com again.")
    return m.group(1)


def find_company(s, name):
    """Best CareerShift company id for a firm name, e.g. 'FOX' -> 'zi-24054796:FOX'."""
    r = s.get(f"{BASE}/api/v2/companies/autocomplete", params={"q": name, "per_page": 7, "source": "zoominfo"}, timeout=30)
    if r.status_code == 401:
        raise CareerShiftError("CareerShift session expired: run api-anything login app.careershift.com")
    r.raise_for_status()
    body = r.json()
    items = body if isinstance(body, list) else body.get("results") or body.get("companies") or body.get("data") or []
    if not items:
        return None
    # Autocomplete ranks loosely ("FOX" puts Twenty-First Century Fox first), so prefer an exact
    # name match, then the biggest company whose name contains the query.
    q = name.lower().strip()
    exact = [i for i in items if (i.get("name") or "").lower().strip() == q]
    contains = [i for i in items if q in (i.get("name") or "").lower()]
    pool = exact or contains or items
    top = max(pool, key=lambda i: i.get("employeeCount") or 0) if not exact else exact[0]
    cid = top.get("lookupKey") or top.get("id")
    label = top.get("name") or name
    return f"{cid}:{label}" if cid and ":" not in str(cid) else cid


def search(s, school, company_id, n=20):
    """Alumni of `school` at the company. Free. Returns CareerShift's contact dicts."""
    out, page = [], 1
    while len(out) < n:
        params = {"schoolAttended": school, "headquarters": "all", "requireEmail": "false",
                  "requirePhone": "false", "page": page, "per_page": min(20, n), "companyId[]": company_id}
        r = s.get(f"{BASE}/api/v2/contacts/search", params=params, timeout=30)
        r.raise_for_status()
        rows = r.json().get("results", [])
        if not rows:
            break
        out.extend(rows)
        page += 1
    return out[:n]


def reveal(s, csrf, contact):
    """Unlock one contact's email. Uses one CareerShift credit. Returns (email or None, credits dict)."""
    r = s.post(f"{BASE}/api/v2/contacts/enrich", json={"person_id": contact["externalId"]},
               headers={"x-csrf-token": csrf, "content-type": "application/json"}, timeout=30)
    r.raise_for_status()
    body = r.json()
    rows = body.get("results") or [{}]
    return rows[0].get("email"), body.get("credits") or {}


def lookup(school, firm, n=20, reveal_n=0):
    """Search, then reveal emails for up to reveal_n people who don't have one yet."""
    s = _session()
    company_id = find_company(s, firm)
    if not company_id:
        raise CareerShiftError(f"No CareerShift company matches {firm!r}")
    people = search(s, school, company_id, n)
    csrf = _csrf(s) if reveal_n else None
    credits = None
    for p in people:
        if reveal_n <= 0:
            break
        if p.get("email") or not p.get("hasEmail"):
            continue
        p["email"], credits = reveal(s, csrf, p)
        reveal_n -= 1
    rows = [{"name": p.get("name"), "title": p.get("jobTitle"), "company": p.get("companyName"),
             "city": p.get("city"), "state": p.get("state"), "email": p.get("email") or "",
             "email_source": "careershift" if p.get("email") else "", "has_email": p.get("hasEmail"),
             "careershift_id": p.get("externalId")} for p in people]
    return rows, credits


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--school", required=True)
    ap.add_argument("--firm", required=True)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--reveal", type=int, default=0, help="unlock up to N more emails (uses daily credits)")
    ap.add_argument("--out", help="write a CSV here (default: print JSON)")
    a = ap.parse_args(argv)
    try:
        rows, credits = lookup(a.school, a.firm, a.n, a.reveal)
    except CareerShiftError as e:
        print(f"careershift: {e}", file=sys.stderr)
        return 2
    if a.out:
        with open(a.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["name"])
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {len(rows)} rows to {a.out}")
    else:
        print(json.dumps(rows, indent=1))
    with_email = sum(1 for r in rows if r["email"])
    print(f"{len(rows)} people, {with_email} with email", file=sys.stderr)
    if credits:
        print(f"CareerShift credits remaining today: {credits.get('remaining')} of {credits.get('limit')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
