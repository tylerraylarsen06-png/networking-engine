#!/usr/bin/env python3
"""Per-person table: which template the hierarchy chose, the fact that drove it, and the field it came from.
    python3 show_templates.py results.csv [--all]
Prints only in-band rows by default. Reads the CSV here, in code; nothing is sent."""
import csv
import sys

path = sys.argv[1]
show_all = "--all" in sys.argv
rows = [r for r in csv.DictReader(open(path, newline="", encoding="utf-8")) if show_all or r.get("seniority_band") == "in_band"]
print(f"{len(rows)} row(s){'' if show_all else ' in band'}\n")
for i, r in enumerate(rows, 1):
    print(f"[{i}] {r['name']}, {r.get('title', '')}")
    print(f"     template: {r.get('template_id') or '(no draft)'}   subject: {r.get('draft_subject', '')}")
    print(f"     fact:     {r.get('template_fact', '')}")
    print(f"     field:    {r.get('template_source', '')}")
