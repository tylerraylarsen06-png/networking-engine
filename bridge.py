#!/usr/bin/env python3
"""Bridge between the results CSV and the Google Sheet the Apps Script sender reads.

    python3 bridge.py queue  --csv outreach_x.csv --sender-name "Alex Example" [--dry-run] [--export queue.csv | --push]
    python3 bridge.py pull                     # sheet -> ledger (sent / replied / bounced / suppressed)
    python3 bridge.py suppress someone@x.com   # never contact again
    python3 bridge.py status                   # ledger counts

Like the rest of this repo it prints COUNTS only. It never prints rows, and it
never prints the web app URL or token.

Selection rules for `queue` (a row must pass all of them):
  seniority_band == in_band, a draft exists, an email exists, email_status is
  in the allowed set (default: valid only), the draft passes validate_draft
  AGAIN at queue time, and the person is not already in the ledger.
"""
import argparse
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

import dnc
import drafts
import sender_profile
import variants
from ledger import Ledger, SUPPRESSED_STATUSES, normalize_email, row_keys

# Keep in sync with COLUMNS in sender/Logic.gs (test_bridge.py checks this).
SHEET_COLUMNS = [
    "id", "email", "name", "firm", "subject", "body", "followup_body", "status",
    "not_before", "scheduled_at", "sent_at", "thread_id", "message_id",
    "followup_due", "followup_status", "followup_sent_at", "reply_flag",
    "bounce_flag", "error", "queued_at", "checked_at",
]
DEFAULT_ALLOWED_STATUSES = ("valid",)


def load_env():
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def read_rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def similarity_warnings(queue_rows, ledger, threshold, days=14):
    """WARNING only, never a rejection. Counts drafts that are `threshold` or more alike to an earlier draft
    to the SAME email domain (earlier in this batch, or queued or sent in the last `days` days). The six
    templates share one ask on purpose, so some overlap is expected and fine; the real risk to a cold
    domain is volume, which pace_by_domain() handles. Returns (similar_count, max_similarity)."""
    seen = {}
    for domain, subj, body, first in ledger.recent_drafts(days):
        seen.setdefault(domain, []).append((subj, body, first))
    similar, top = 0, 0.0
    for q in queue_rows:
        domain = variants.email_domain(q["email"])
        first = (q.get("name") or "").split()[0] if (q.get("name") or "").strip() else ""
        worst = max((variants.similarity(q["subject"], q["body"], first, s2, b2, f2)
                     for s2, b2, f2 in seen.get(domain, [])), default=0.0)
        top = max(top, worst)
        if worst >= threshold:
            similar += 1
        seen.setdefault(domain, []).append((q["subject"], q["body"], first))
    return similar, round(top, 3)


DEFAULT_DOMAIN_WEEKLY_MAX = 4


def next_weekday(d):
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def pace_by_domain(queue_rows, ledger, weekly_max=DEFAULT_DOMAIN_WEEKLY_MAX, start=None):
    """Give every row a not_before date so no email domain gets more than `weekly_max` SENDS in a Monday to
    Sunday week, counting both the first email and its one follow-up (~7 days later, the middle of the 6 to
    8 day window — an estimate, not the sheet's actual followup_due), at most one send per day, spread across
    weekdays. A row that does not fit this week is scheduled into a later week. It stays queued in the Sheet
    (the sender honors not_before); it is never dropped. Counts the ledger too (see Ledger.scheduled_dates),
    so a second run, and pending follow-ups from earlier runs, cannot stack on top of this one.
    Returns the number of rows pushed past the first week."""
    start = next_weekday(start or date.today() + timedelta(days=1))
    taken = {}
    for domain, dates in ledger.scheduled_dates().items():
        taken.setdefault(domain, []).extend(dates)

    def week_of(d):
        return d - timedelta(days=d.weekday())

    deferred = 0
    for q in queue_rows:
        domain = variants.email_domain(q["email"])
        dates = taken.setdefault(domain, [])
        d = start
        while True:
            in_week = sum(1 for x in dates if week_of(x) == week_of(d))
            if d.weekday() < 5 and d not in dates and in_week < weekly_max:
                break
            d += timedelta(days=1)
        dates.append(d)
        dates.append(d + timedelta(days=Ledger.FOLLOWUP_OFFSET_DAYS))   # reserve this row's own follow-up slot
        q["not_before"] = d.isoformat()
        if week_of(d) != week_of(start):
            deferred += 1
    return deferred


def select_sendable(rows, ledger, sender_first, allowed_statuses=DEFAULT_ALLOWED_STATUSES,
                    require_band="in_band", limit=None, similarity_threshold=variants.SIMILARITY_THRESHOLD,
                    weekly_max=DEFAULT_DOMAIN_WEEKLY_MAX, start=None, dnc_entries=None):
    """Returns (queue_rows, stats, selected_source_rows). Pure: touches no network and
    does not modify the ledger. Rows are checked against the Do Not Contact list first (a second line of
    defense behind find.py, since older results files predate the list)."""
    if dnc_entries is None:
        try:
            dnc_entries = dnc.load()
        except dnc.DNCError as e:
            raise SystemExit(f"*** {e} ***")
    stats = {
        "considered": len(rows), "dnc_excluded": 0, "dnc_flagged_for_review": 0, "not_in_band": 0, "no_email": 0,
        "no_draft": 0, "status_not_allowed": 0, "failed_validation": 0, "already_in_ledger": 0,
        "duplicate_in_batch": 0, "selected": 0, "similar_drafts_warning": 0, "max_similarity": 0.0,
        "paced_to_later_weeks": 0,
    }
    candidates = []
    for r in rows:
        hits = dnc.check(r, dnc_entries)
        if hits:
            stats["dnc_excluded"] += 1
            stats["dnc_flagged_for_review"] += 1 if any(h["review"] for h in hits) else 0
            dnc.log_exclusion(r, hits, context="bridge queue")
            continue
        if r.get("seniority_band") != require_band:
            stats["not_in_band"] += 1
        elif not (r.get("email") or "").strip():
            stats["no_email"] += 1
        elif not (r.get("draft_subject") and r.get("draft_body")):
            stats["no_draft"] += 1
        elif (r.get("email_status") or "") not in allowed_statuses:
            stats["status_not_allowed"] += 1
        else:
            candidates.append(r)

    fresh, dropped = ledger.filter_new(candidates)
    for _, reason in dropped:
        stats["duplicate_in_batch" if reason.startswith("duplicate") else "already_in_ledger"] += 1

    queue_rows, source = [], []
    for r in fresh:
        subject, body = r["draft_subject"], r["draft_body"]
        if drafts.validate_draft(subject, body, allowed_numbers=drafts.recipient_numbers(r)):
            stats["failed_validation"] += 1
            continue
        first = (r.get("name") or "").split()[0] if (r.get("name") or "").strip() else ""
        try:
            _, followup_body = drafts.render_followup(first, r.get("firm_queried", ""), sender_first, subject)
        except drafts.DraftContentError:
            stats["failed_validation"] += 1
            continue
        keys = row_keys(r)
        rec_id = "r" + __import__("hashlib").sha1("|".join(sorted(keys)).encode()).hexdigest()[:10]
        queue_rows.append({
            "id": rec_id, "email": normalize_email(r["email"]), "name": r.get("name", ""),
            "firm": r.get("firm_queried", ""), "subject": subject, "body": body,
            "followup_body": followup_body, "status": "queued",
            "queued_at": datetime.now().isoformat(timespec="seconds"),
        })
        source.append(r)
    if limit:
        queue_rows, source = queue_rows[:limit], source[:limit]
    stats["similar_drafts_warning"], stats["max_similarity"] = similarity_warnings(queue_rows, ledger, similarity_threshold)
    stats["paced_to_later_weeks"] = pace_by_domain(queue_rows, ledger, weekly_max, start)
    stats["selected"] = len(queue_rows)
    return queue_rows, stats, source


def write_export(path, queue_rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SHEET_COLUMNS, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(queue_rows)


def webapp_call(action, payload=None, url=None, token=None, timeout=90):
    load_env()
    url = url or os.environ.get("SHEET_WEBAPP_URL")
    token = token or os.environ.get("SHEET_WEBAPP_TOKEN")
    if not url or not token:
        raise SystemExit("SHEET_WEBAPP_URL and SHEET_WEBAPP_TOKEN must be set in .env (see .env.example).")
    body = {"token": token, "action": action}
    body.update(payload or {})
    resp = requests.post(url, json=body, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        raise SystemExit("Web app did not return JSON. Check the deployment: Execute as Me, access Anyone.")
    if not data.get("ok"):
        raise SystemExit(f"Web app refused the request: {data.get('error', 'unknown error')}")
    return data


def sheet_status_to_ledger_status(r):
    if str(r.get("bounce_flag", "")).upper() == "TRUE" or r.get("status") == "bounced":
        return "bounced"
    if r.get("status") == "suppressed":
        return "suppressed"
    if str(r.get("reply_flag", "")).upper() == "TRUE" or r.get("status") == "replied":
        return "replied"
    if r.get("status") == "sent" and r.get("followup_status") == "sent":
        return "followup_sent"
    if r.get("status") in ("sent", "error", "queued"):
        return r["status"]
    return None


def apply_pull(ledger, sheet_rows):
    """Advance ledger statuses from sheet rows. Never moves a suppressed person back."""
    changed = {}
    for r in sheet_rows:
        new = sheet_status_to_ledger_status(r)
        if not new:
            continue
        rec = ledger.find([f"email:{normalize_email(r.get('email'))}"])
        if rec is None:
            rec = ledger.add({"email": r.get("email"), "name": r.get("name", ""),
                              "firm_queried": r.get("firm", "")}, new, note="from sheet pull")
            changed[new] = changed.get(new, 0) + 1
            continue
        if rec["status"] in SUPPRESSED_STATUSES and new not in SUPPRESSED_STATUSES:
            continue
        if rec["status"] != new:
            ledger.set_status_by_email(r["email"], new)
            changed[new] = changed.get(new, 0) + 1
    return changed


def cmd_queue(args):
    args.sender_name = args.sender_name or sender_profile.field(variants.PROFILE, "name")
    if not args.sender_name:
        raise SystemExit("--sender-name is required (used to sign the follow-up), or set name in profile.json.")
    if not (args.dry_run or args.export or args.push):
        raise SystemExit("Choose one of --dry-run, --export PATH, --push. Nothing is queued silently.")
    sender_first = args.sender_name.split()[0]
    ledger = Ledger()
    rows = read_rows(args.csv)
    allowed = tuple(args.allow_status or DEFAULT_ALLOWED_STATUSES)
    queue_rows, stats, source = select_sendable(
        rows, ledger, sender_first, allowed, limit=args.limit,
        similarity_threshold=args.similarity_threshold, weekly_max=args.domain_weekly_max,
        start=date.fromisoformat(args.start_date) if args.start_date else None)

    print("--- Queue selection (counts only) ---")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(f"  allowed email_status: {', '.join(allowed)}")
    if stats["similar_drafts_warning"]:
        print(f"  WARNING: {stats['similar_drafts_warning']} draft(s) are {args.similarity_threshold:.0%} or more alike to another "
              f"draft to the same domain. Expected (the ask is shared on purpose). Pacing, not rewording, is the control.")
    weeks = {}
    for q in queue_rows:
        wk = date.fromisoformat(q["not_before"])
        wk = (wk - timedelta(days=wk.weekday())).isoformat()
        weeks[(variants.email_domain(q["email"]), wk)] = weeks.get((variants.email_domain(q["email"]), wk), 0) + 1
    for (dom, wk), n in sorted(weeks.items()):
        print(f"  pacing: {dom} week of {wk}: {n} (max {args.domain_weekly_max})")
    if args.dry_run or not queue_rows:
        print("Dry run, nothing written." if args.dry_run else "Nothing to queue.")
        return 0

    if args.export:
        write_export(args.export, queue_rows)
        print(f"Wrote {len(queue_rows)} rows to {args.export} (import into the Queue tab).")
    if args.push:
        data = webapp_call("append", {"rows": queue_rows})
        added = int(data.get("added", 0))
        skipped = data.get("skipped", [])
        print(f"Pushed to sheet: added {added}, skipped by sheet {len(skipped)}.")
        skipped_emails = {normalize_email(s.get("email")) for s in skipped}
        queue_rows = [q for q in queue_rows if q["email"] not in skipped_emails]
        source = [s for s in source if normalize_email(s["email"]) not in skipped_emails]

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    for q, s in zip(queue_rows, source):
        ledger.add({**s, "email": q["email"]}, "queued", run_id=run_id,
                   draft={"subject": q["subject"], "body": q["body"]}, not_before=q["not_before"])
    ledger.save()
    print(f"Ledger updated: {len(queue_rows)} people marked queued.")
    return 0


def cmd_pull(args):
    ledger = Ledger()
    data = webapp_call("export")
    changed = apply_pull(ledger, data.get("rows", []))
    ledger.save()
    print(f"Pulled {len(data.get('rows', []))} rows. Ledger changes: {changed or 'none'}")
    print("Ledger totals:", ledger.counts())
    return 0


def cmd_suppress(args):
    ledger = Ledger()
    ledger.suppress_email(args.email, args.reason)
    ledger.save()
    print("Suppressed in ledger.")
    if args.push:
        webapp_call("suppress", {"emails": [normalize_email(args.email)], "reason": args.reason})
        print("Suppressed in sheet.")
    return 0


def cmd_status(args):
    print("Ledger totals:", Ledger().counts())
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("queue", help="select sendable rows and send them to the sheet")
    q.add_argument("--csv", required=True)
    q.add_argument("--sender-name")
    q.add_argument("--allow-status", action="append",
                   help="email_status values allowed to be queued (repeatable). Default: valid only.")
    q.add_argument("--limit", type=int)
    q.add_argument("--similarity-threshold", type=float, default=variants.SIMILARITY_THRESHOLD,
                   help="WARN when a draft is this similar (0 to 1) to an earlier draft to the same email domain. "
                        "Never blocks. Default 0.80.")
    q.add_argument("--domain-weekly-max", type=int, default=DEFAULT_DOMAIN_WEEKLY_MAX,
                   help="most first emails to one email domain in a Monday to Sunday week, at most one per day "
                        "(default 4). Rows over the limit stay queued and get a later not_before date.")
    q.add_argument("--start-date", metavar="YYYY-MM-DD", help="earliest send date (default: next weekday)")
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--export", metavar="PATH")
    q.add_argument("--push", action="store_true")
    q.set_defaults(fn=cmd_queue)
    sub.add_parser("pull", help="update the ledger from the sheet").set_defaults(fn=cmd_pull)
    s = sub.add_parser("suppress", help="never contact this address again")
    s.add_argument("email")
    s.add_argument("--reason", default="manual")
    s.add_argument("--push", action="store_true", help="also add to the sheet's Suppress tab")
    s.set_defaults(fn=cmd_suppress)
    sub.add_parser("status", help="ledger counts").set_defaults(fn=cmd_status)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
