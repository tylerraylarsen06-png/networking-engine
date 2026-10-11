#!/usr/bin/env python3
"""Read the drafts before anything goes out. Prints one block per sendable person:
name, title, email, status, subject, body. Uses the SAME selection as `bridge.py queue`
(in band, valid email, draft present, draft passes validate_draft, not already in the ledger),
so what you read here is exactly what would be queued. Prints nothing else, writes nothing.

    python3 show_drafts.py outreach_x.csv [--allow-status unknown]
"""
import argparse
import csv
import sys

import bridge
from ledger import Ledger


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--allow-status", action="append")
    a = ap.parse_args(argv)
    rows = bridge.read_rows(a.csv)
    allowed = tuple(a.allow_status or bridge.DEFAULT_ALLOWED_STATUSES)
    _, stats, source = bridge.select_sendable(rows, Ledger(), "Sender", allowed)
    print(f"{len(source)} sendable of {stats['considered']} rows (allowed email_status: {', '.join(allowed)})\n")
    for i, r in enumerate(source, 1):
        print(f"[{i}/{len(source)}] {r['name']}")
        print(f"  title:   {r.get('title', '')}")
        print(f"  email:   {r['email']}")
        print(f"  status:  {r['email_status']}")
        print(f"  subject: {r['draft_subject']}")
        print("  body:")
        for line in r["draft_body"].splitlines():
            print(f"    {line}")
        print("-" * 72)


if __name__ == "__main__":
    sys.exit(main())
