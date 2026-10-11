"""Cross-run ledger: who has already been queued, sent, replied, bounced or suppressed.

Purpose: the same person must never appear in two runs, and anyone who
bounced, replied, or opted out must never be queued again.

A person is matched on ANY of three keys, so a changed URL or a re-pulled
profile does not slip through (mistake 6 in the handoff was a duplicate that
slipped past a URL-only check):
  email:<lowercased address>
  li:<normalized LinkedIn profile path>
  nf:<normalized name>|<normalized firm>

The ledger file holds real names and addresses. It is gitignored.
"""
import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

LEDGER_PATH = Path(__file__).resolve().parent / "ledger.json"

# A person in any of these states is never selected again.
BLOCKING_STATUSES = {
    "queued", "sent", "followup_sent", "replied", "bounced", "suppressed", "opt_out", "error",
}
# States that also mean "never contact again for any reason".
SUPPRESSED_STATUSES = {"bounced", "suppressed", "opt_out", "replied"}


def _norm(s):
    return re.sub(r"\W", "", (s or "").lower())


def normalize_email(email):
    return (email or "").strip().lower()


def normalize_linkedin(url):
    """linkedin.com/in/jane-doe-123 regardless of scheme, www, query string or
    trailing slash. Empty string when it is not a profile URL."""
    m = re.search(r"linkedin\.com/(in|pub)/([^/?#\s]+)", url or "", re.IGNORECASE)
    return f"{m.group(1).lower()}/{m.group(2).lower()}" if m else ""


def row_keys(row):
    keys = []
    email = normalize_email(row.get("email"))
    if email:
        keys.append(f"email:{email}")
    li = normalize_linkedin(row.get("linkedin_url"))
    if li:
        keys.append(f"li:{li}")
    name, firm = _norm(row.get("name")), _norm(row.get("firm_queried") or row.get("firm"))
    if name and firm:
        keys.append(f"nf:{name}|{firm}")
    return keys


def _record_id(keys):
    return "r" + hashlib.sha1("|".join(sorted(keys)).encode()).hexdigest()[:10]


class Ledger:
    def __init__(self, path=None):
        self.path = Path(path) if path else LEDGER_PATH
        self.records = []
        self._index = {}
        if self.path.exists():
            data = json.loads(self.path.read_text() or "{}")
            self.records = data.get("records", [])
        self._reindex()

    def _reindex(self):
        self._index = {}
        for i, rec in enumerate(self.records):
            for k in rec.get("keys", []):
                self._index[k] = i

    def save(self):
        payload = json.dumps({"version": 1, "records": self.records}, indent=1, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".ledger-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(payload + "\n")
        os.replace(tmp, self.path)

    def find(self, row_or_keys):
        keys = row_or_keys if isinstance(row_or_keys, list) else row_keys(row_or_keys)
        for k in keys:
            if k in self._index:
                return self.records[self._index[k]]
        return None

    def blocked_reason(self, row):
        """None if the person is free to be selected, else a short reason."""
        rec = self.find(row)
        if rec and rec.get("status") in BLOCKING_STATUSES:
            return f"already {rec['status']} (ledger {rec['id']})"
        return None

    def add(self, row, status, run_id="", note="", draft=None, not_before=None):
        keys = row_keys(row)
        if not keys:
            raise ValueError("cannot add a person with no email, LinkedIn URL, or name+firm")
        now = datetime.now().isoformat(timespec="seconds")
        rec = self.find(keys)
        if rec:
            for k in keys:
                if k not in rec["keys"]:
                    rec["keys"].append(k)
            rec["status"], rec["updated_at"] = status, now
            if note:
                rec["note"] = note
            if draft:
                rec["draft"] = draft
            if not_before:
                rec["not_before"] = not_before
        else:
            rec = {
                "id": _record_id(keys), "keys": keys, "name": row.get("name", ""),
                "firm": row.get("firm_queried") or row.get("firm", ""),
                "email": normalize_email(row.get("email")), "status": status,
                "first_run": run_id, "created_at": now, "updated_at": now, "note": note,
            }
            if draft:
                rec["draft"] = draft
            if not_before:
                rec["not_before"] = not_before
            self.records.append(rec)
        self._reindex()
        return rec

    def set_status_by_email(self, email, status, note=""):
        rec = self.find([f"email:{normalize_email(email)}"])
        if not rec:
            return None
        rec["status"] = status
        rec["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if note:
            rec["note"] = note
        return rec

    def suppress_email(self, email, reason="manual"):
        """Suppress an address whether or not it is already in the ledger."""
        email = normalize_email(email)
        rec = self.find([f"email:{email}"])
        if rec:
            rec["status"], rec["note"] = "suppressed", reason
            rec["updated_at"] = datetime.now().isoformat(timespec="seconds")
        else:
            self.add({"email": email, "name": "", "firm_queried": ""}, "suppressed", note=reason)
        return True

    def filter_new(self, rows):
        """Split rows into (fresh, dropped[(row, reason)]). Also drops
        duplicates inside the batch itself, keeping the first occurrence."""
        fresh, dropped, seen = [], [], set()
        for row in rows:
            keys = row_keys(row)
            reason = self.blocked_reason(row)
            if reason is None and any(k in seen for k in keys):
                reason = "duplicate within this batch"
            if reason:
                dropped.append((row, reason))
                continue
            seen.update(keys)
            fresh.append(row)
        return fresh, dropped

    def recent_drafts(self, days=14):
        """Drafts already queued or sent recently: [(email_domain, subject, body, first_name)]. Used so a new
        batch is compared against earlier batches to the same domain, not only against itself."""
        cutoff = datetime.now().timestamp() - days * 86400
        out = []
        for rec in self.records:
            d = rec.get("draft")
            if not d or rec.get("status") not in ("queued", "sent", "followup_sent", "replied"):
                continue
            try:
                if datetime.fromisoformat(rec["created_at"]).timestamp() < cutoff:
                    continue
            except (KeyError, ValueError):
                continue
            email = rec.get("email", "")
            first = (rec.get("name") or "").split()[0] if (rec.get("name") or "").strip() else ""
            out.append((email.rsplit("@", 1)[-1].lower() if "@" in email else "", d.get("subject", ""), d.get("body", ""), first))
        return out

    FOLLOWUP_OFFSET_DAYS = 7   # the middle of the 6 to 8 day follow-up window (sender/Logic.gs); an estimate, since
                               # the ledger does not record the sheet's own followup_due date

    def scheduled_dates(self):
        """{email_domain: [date, ...]} of sends already scheduled or sent, so pacing counts earlier runs too
        and counts against the SAME weekly cap as first emails. Uses the recorded not_before, else the day
        the record was created. A record still eligible for its one follow-up (queued or sent, not yet
        followup_sent, replied, bounced, or suppressed) reserves a SECOND, estimated slot ~7 days later —
        an approximation, since the ledger does not know the sheet's actual followup_due date."""
        out = {}
        for rec in self.records:
            status = rec.get("status")
            if status not in ("queued", "sent", "followup_sent", "replied"):
                continue
            email = rec.get("email", "")
            if "@" not in email:
                continue
            try:
                d = date.fromisoformat(rec.get("not_before") or rec["created_at"][:10])
            except (KeyError, ValueError):
                continue
            domain = email.rsplit("@", 1)[-1].lower()
            out.setdefault(domain, []).append(d)
            if status in ("queued", "sent"):
                out[domain].append(d + timedelta(days=self.FOLLOWUP_OFFSET_DAYS))
        return out

    def counts(self):
        out = {}
        for rec in self.records:
            out[rec["status"]] = out.get(rec["status"], 0) + 1
        return out
