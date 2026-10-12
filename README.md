# networking-engine

A CLI that takes a target (school, firm or firms, city), finds real people to
network with through the [Exa](https://exa.ai) people search API
(`category="people"`), builds and optionally verifies their work email
(Hunter for the pattern, ZeroBounce for verification), checks every person
against your Do Not Contact list, and writes a personalized cold email draft
for each one from fixed copywriting templates. You read the drafts, edit
what you want, and send.

## CareerShift: verified emails from your school's free license

Many universities give students CareerShift for free. `careershift.py` uses your own logged-in
CareerShift session to search alumni at a firm and pull their emails, with no API key and no
guessing. CareerShift emails count as verified.

```
api-anything login app.careershift.com     # once: imports your browser session
python3 careershift.py --school "University of Southern California" --firm "FOX" --n 20
python3 careershift.py --school "University of Southern California" --firm "FOX" --n 20 --reveal 5 --out fox.csv
```

Search is free and returns every alum CareerShift knows at that firm, with the email when it
is already unlocked for you. `--reveal N` unlocks up to N more, skipping anyone CareerShift has
no email for. Each reveal uses one daily credit (50 a day on a typical university license), and
the remaining count is printed after the run. Tested 2026-10-11: a USC-at-FOX search returned
the firm's alumni, and one reveal returned a verified @fox.com address.

Requires [api-anything](https://github.com/goodnight000/api-anything) for the session import.

## Networking drafts (`drafts.py`)

One command goes from a company and a posting to reviewed drafts in your Gmail:

```
python3 drafts.py --company "Amazon" --role "Product Management Intern" \
    --school "University of Southern California" --n 3
```

What it does, in order:

1. **Finds people.** Exa people search and CareerShift alumni search run for the company, and
   the results merge by name, so a CareerShift email lands on the matching Exa profile. Either
   source can be missing: no `EXA_API_KEY` or no CareerShift session just skips that source.
2. **Keeps the right titles.** A business posting skips engineers. Every posting skips
   recruiters, lawyers and administrative staff. A technical posting (engineer, developer, data science)
   keeps engineers. People whose titles share words with the posting rank first.
3. **Checks two lists before writing anything.** `data/do_not_contact.csv`, which must exist before anything runs, and `data/contacted.csv`, the log of everyone this command already drafted for. The
   older `dnc.json` and `ledger.json` are honored too when they exist.
4. **Spends CareerShift credits only when asked.** `--reveal N` unlocks at most N emails, and only
   for people who already passed every check. A revealed address is checked against Do Not
   Contact again.
5. **Writes one message each** by the method in [METHOD.md](METHOD.md), filled from
   `profile.yaml`. With `ANTHROPIC_API_KEY` set, Claude (`claude-sonnet-5-5`) writes only the
   clause about the recipient's work. The full message then runs through the checker in
   `method.py`, and the template takes over when the clause fails or when no key is set. A person
   with no true hook in your profile is skipped, not stretched.
6. **Saves drafts, never sends.** With Gmail set up, each message becomes a Gmail draft. Every run
   also writes `out/drafts_<company>_<date>.md` and a CSV next to it.
7. **Prints a review table:** name, title, email, hook, then each full message.

Setup:

```
pip3 install -r requirements.txt
cp profile.example.yaml profile.yaml          # your true facts: clubs, work, aspirations
cp data/do_not_contact.example.csv data/do_not_contact.csv   # people never to message
```

Flags: `--industry media` matches the `fits` lists in your profile, `--city` narrows the Exa
search, `--no-gmail` and `--no-claude` force the local file and the template, `--no-log` leaves
`data/contacted.csv` alone.

### The profile is the only source of facts about you

Each club and job in `profile.yaml` says what you actually do there (`what_i_do`), what you want
tips on (`advice_on`), and which words in a recipient's title make it the right link (`topics`).
`aspirations` covers people no club or job matches. `notable_facts` only appear for recipients
whose industry, company or city is on the fact's `fits` list. `targets` lists other companies you
are going after, so a message never names one of them. `profile.yaml` is gitignored.

### Gmail drafts

Gmail is optional. Without it, the Markdown file and the CSV are the output. To turn it on:

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project, enable the
   Gmail API, and set up the OAuth consent screen as "External" in testing mode with your own
   address as a test user.
2. Create an OAuth client ID of type "Desktop app" and download its JSON as
   `gmail_credentials.json` in this folder.
3. Run `python3 gmail_drafts.py --setup`, sign in, and approve. That writes `gmail_token.json`.

The only scope requested is `gmail.compose`, and the only call made is `users.drafts.create`.
There is no code path that sends mail. Both JSON files are gitignored and never printed.

## Setup

```
pip3 install -r requirements.txt
cp .env.example .env
# edit .env: EXA_API_KEY is required. HUNTER_API_KEY and ZEROBOUNCE_API_KEY
# are only needed with --emails.
cp profile.example.json profile.json
# edit profile.json: your name, school, year, target industry, and the
# shared facts the templates may claim (see "Draft templates" below)
cp dnc.example.json dnc.json
# edit dnc.json: people and organizations you must never cold email.
# The tool refuses to run without it.
```

`.env`, `profile.json`, `dnc.json`, the exclusion log, the send ledger and
every output CSV are gitignored. They hold keys or real people's data.

## Usage

```
python3 find.py --school "University of Southern California" \
    --firm "Goldman Sachs" --city "New York" --n 25
```

`--firm` is repeatable, pass it multiple times to cover several firms in one
run. All firms are combined into a single output CSV.

```
python3 find.py --school "University of Southern California" \
    --firm "Goldman Sachs" --firm "Morgan Stanley" --city "New York" --n 25
```

`--city` is repeatable too, for a multi-city run in one command:

```
python3 find.py --school "University of Southern California" \
    --firm "Goldman Sachs" --city "New York" --city "Boston" --city "Chicago" --n 100
```

One Exa query runs per firm-city combination, cities are never combined
into a single query, since that dilutes semantic matching. Results are
merged and deduped across all of them (URL first, then normalized name +
firm, same two-pass dedup as everything else). A `city` column records
where each person actually *is* (Exa's structured `location` field on
their profile), not which city's query happened to surface them, those
can differ, since the search itself is semantic/fuzzy.

`--title` filters loosely on the person's current title (substring,
case-insensitive, `"Consultant"` catches `"Senior Consultant"`) and is
also repeatable. It never drops rows, only flags them in a `title_match`
column, same convention as `seniority_band`, so you can still see and
sort on everyone even if their title didn't match.

## High school (`--highschool`)

`--highschool` searches on a high school instead of a university, a shared
high school is often a rarer, stronger connection than a shared college.
It's mutually exclusive with `--school` and works standalone:

```
python3 find.py --highschool "Lincoln High School" \
    --firm "Deloitte" --firm "McKinsey"
```

Everything else (matching, `--org`, seniority band) works identically, the
"school" being matched is just pointed at a different institution name.
Expect low result counts: a single high school among people at a specific
firm is a narrow target, and zero confirmed hits is a real, valid outcome,
not a bug.

## Affiliation matching (`--org`)

`--org` checks for membership in an org, a fraternity, sports team, honor
society, club, anything, and is repeatable to check for several at once:

```
python3 find.py --school "University of Southern California" \
    --org "Delta Omega" --firm "Goldman Sachs" --city "New York" --n 25
```

This adds two columns, `affiliation_match` and `tier`, and sorts the output
by tier:

- **Tier 1**: school match AND org match (the best of both worlds)
- **Tier 2**: org match only, different school (still a genuine warm
  connection, national orgs share founders/ritual/culture across chapters,
  so this is *not* weaker than Tier 3, just a different kind of connection)
- **Tier 3**: school match only, no org match
- **Tier 4**: neither confirmed (still included, sorted last)

Spelling variants for each org are config, not code, see
`org_variants.json`. An org not listed there just searches on its literal
`--org` value; add an entry to catch more variants without touching
`find.py`. Two variant types:

- `variants`: plain nicknames/abbreviations (e.g. "Delta O", "D Omega").
  Anything under 3 normalized characters is dropped automatically.
- `greek_variants`: literal Greek Unicode monograms (e.g. "ΔΩ" for Delta
  Omega), exempt from the length floor. These are validated at load time to
  contain *only* real Greek Unicode codepoints, Greek "Κ" (U+039A) and
  Latin "K" (U+004B) render identically but are different characters, and
  nothing in this codebase folds one to the other, so a Greek monogram can
  never match its ASCII lookalike ("ΔΩ" cannot match "KS") even though both
  are 2 characters. A `greek_variants` entry that isn't pure Greek (e.g. a
  Latin "K" typo'd in) is rejected with a warning, not silently downgraded
  to a regular variant.

**Why two Exa calls per org per firm:** a single query mixing school + org
tends to under-serve the highest-value Tier 1 combination, so Tier 1/Tier 2
candidates are fetched with two separate queries (school+org+firm, and
org+firm alone) and merged, deduped by LinkedIn URL. Tier 3/4 show up
naturally in that same pool. Two Exa calls run per org per firm, cheap
(~$0.02 each) relative to the value of not missing Tier 1 people.

**Why 3x `--n`:** roughly 32% of LinkedIn profiles fill in "Activities and
Societies" at all, so `--org` runs request `3 * n` results per search (capped
at Exa's max of 100) to land closer to `n` *confirmed* affiliation hits
rather than `n` raw results. The terminal summary prints both the raw count
and the confirmed-affiliation count per firm so you can see the real yield.

## Seniority band (`--min-seniority` / `--max-seniority`)

Adds a `seniority_band` column (`below` / `in_band` / `above` / `incoming` /
`excluded_role`) and a separate `total_experience_years` column, and sorts
`in_band` rows first (within each tier, if `--org` is used). Defaults to a
2 to 5 year band, overridable:

```
python3 find.py --school "..." --firm "..." --min-seniority 1 --max-seniority 3
```

Nobody is dropped from the CSV for being out of band, this is a sort/label,
not a filter, so you can still run a separate pass on senior people later
(or use `--require` to actually drop, see below). Classification order:

1. Title matches this firm's `entry_titles` list in `patterns.json` (e.g.
   Bain: Associate Consultant, Senior Associate Consultant, Consultant;
   Goldman: Analyst, Associate) → **always `in_band`, regardless of prior
   experience.** This is what fixes a real bug: someone with a prior career
   before joining in an entry-level role was landing `above` on total years,
   even on day one of that role. Per-firm and config-driven, not hardcoded , 
   add an `entry_titles` list to any firm's `patterns.json` entry.
2. Title contains "incoming" (e.g. "Incoming Associate Consultant") →
   `incoming`. Someone who hasn't started yet can't make an internal
   referral, but is still worth contacting for recruiting insight, kept as
   its own group rather than mixed into `in_band`.
3. Title matches `title_filters.json`'s `non_target_roles` (support/
   non-client functions, Executive Assistant and similar) → `excluded_role`.
4. Title contains an intern/summer-analyst/co-op pattern → `below`.
5. Title contains a senior keyword (VP, Managing Director, Partner,
   Principal, Head of, Chief, Founder, President) → `above`.
6. Otherwise, **tenure in the current role at the queried firm** , 
   `workHistory[].dates.from` on the matched job, not years since their
   career started, is compared against the band, because "Analyst" alone
   doesn't distinguish a first-year from a third-year, but a start date
   does. If there's no confirmed current-role start date (e.g. `firm_match`
   isn't `"yes"`), this falls back to total career experience as the
   closest available proxy, noted as such in `evidence`. No usable date
   data at all defaults to `below` (conservative: don't claim someone's in
   the target band without evidence).

`total_experience_years` (career-total, not role-tenure) is kept as its own
column precisely so you can still see it even though banding no longer uses
it directly.

## Non-target roles and leave status

`title_filters.json`: global, not per-firm, holds two lists:

- `non_target_roles`: titles that make someone `excluded_role` in
  `seniority_band` even though they're a genuine, currently-employed match
  (an Executive Assistant at the target firm is real, just not a
  recruiting-relevant function).
- `leave_indicators`: a title like "MBA Educational Leave" or "Leave of
  Absence" makes `firm_match` come back `on_leave` instead of `yes`, even
  though `workHistory` shows the role as ongoing (`dates.to == null`) , 
  they're not actually available for a referral conversation right now.

Both lists are seeded only with roles actually observed in a real audited
run, not guessed ahead of time, extend them as you find more.

## Hard filtering (`--require`)

Everything above tags rows; nothing gets dropped by default. `--require`
opts into dropping instead, and is repeatable:

```
python3 find.py --school "..." --firm "..." --require school --require firm
```

`school` / `firm` require `school_match` / `firm_match == "yes"` (so
`on_leave` doesn't pass `--require firm`); `title` requires
`title_match == "yes"` and needs `--title` to also be passed; `org`
requires `affiliation_match == "yes"` and needs `--org` to also be passed.
The terminal summary prints how many rows survived out of how many.

## Output

A CSV (`outreach_<school>_<firms>[_<orgs>]_<timestamp>.csv` by default, or
`--out path.csv`) with one row per person, firm(s) and org(s) are in the
filename so a Goldman run and a Bain run sitting in the same directory are
distinguishable without opening either one:

| column | meaning |
|---|---|
| `name` | person's name |
| `firm_queried` | which `--firm` this row was searched for (added so multi-firm runs stay interpretable in one file) |
| `linkedin_url` | URL Exa returned for the profile (usually LinkedIn, not guaranteed, can go stale) |
| `linkedin_search_url` | a constructed `linkedin.com/search/results/people/?keywords=...` URL from the person's name + firm, a fallback that always works even when `linkedin_url` is dead. Never HTTP-validated (LinkedIn blocks unauthenticated requests, so a validation attempt would be noise, not signal) |
| `city` | the person's actual location (Exa's structured `location` field), not necessarily the `--city` value that surfaced them in a multi-city run, since matching is semantic |
| `school_match` | `yes` (found in structured education history), `unverified` (school mentioned in profile text but not in structured data), or `no` |
| `firm_match` | `yes` (confirmed ongoing via `workHistory.dates.to == null`, or most-recent entry as a fallback), `on_leave` (ongoing per dates, but the title says otherwise, see `title_filters.json`), `past` (appears earlier in work history, no longer current), `mentioned_only` (firm name appears in text but not as a job), or `no` |
| `affiliation_match` *(only when `--org` is used)* | `yes` (found via a structured source, see below), `unverified` (org mentioned in free text with a corroborating word nearby), or `no` |
| `tier` *(only when `--org` is used)* | see [Affiliation matching](#affiliation-matching---org) below |
| `title` | current or best-guess title |
| `seniority_flag` | `SENIOR` if title looks like Managing Director / Partner / Principal / VP / Founder / Chief / President / Head of, else blank |
| `seniority_band` | `below` / `in_band` / `above` / `incoming` / `excluded_role`: see [Seniority band](#seniority-band---min-seniority---max-seniority) |
| `total_experience_years` | career-total years (not role-tenure, that's what `seniority_band` is based on) |
| `title_match` *(only when `--title` is used)* | `yes` / `no`: loose substring match against `--title`, never drops non-matching rows |
| `evidence` | snippet quoting/citing the profile data that justified the school, firm, seniority, and affiliation calls |

Terminal output prints per-firm result counts, how many rows are confirmed
`yes`/`yes` on both school and firm, tier distribution (if `--org`), seniority
band distribution, and the total `costDollars` spent.

## How matching works

Exa's `category="people"` results can include a structured `entities` block
(`educationHistory`, `workHistory`, each with `dates: {from, to}`) alongside
the raw profile `text`. This script prefers the structured data:

- A school only counts as a match if it appears in `educationHistory`
  (matched against the full name or its acronym as a standalone word, so
  e.g. "USC Marshall School of Business" still matches "University of
  Southern California").
- A firm only counts as *current* employment if a matching `workHistory`
  entry has `dates.to == null` (confirmed ongoing); if no entry has date
  info at all, the most recent (`index 0`) entry is used as a fallback.
  Older entries or a firm just named in the text are marked `past` or
  `mentioned_only`, not `yes`: that's the "passing mention isn't
  employment" rule from the brief.
- Firm matching checks an **exclusion list first**, before fuzzy matching , 
  same mechanism and same `patterns.json` file as the org exclusions.
  "Bain Capital" (private equity) and "Bain & Company" (consulting) are
  different firms that share a word; without this, `--firm "Bain"` would
  fuzzy-match Bain Capital employees onto a Bain & Company list. Give the
  `"bain & company"` entry `"exclude": ["Bain Capital"]` to prevent it. This generalizes to
  any confusable pair (Morgan Stanley/JPMorgan, Wells Fargo/Wells Fargo
  Advisors), add an `"exclude"` list to that firm's `patterns.json` entry,
  no code change needed. The `--firm` value must match the `patterns.json`
  key exactly (case-insensitive) for its exclusions to apply, same
  requirement as `--org` and `org_variants.json`.
- An org/affiliation match checks three real LinkedIn sources, in order of
  reliability, before ever falling back to free text:
  1. **`workHistory` company names**: people often list a fraternity/club
     officer role as work "experience" (e.g. company = "Delta Omega
     Fraternity"), and this is Exa's fully-structured entity data.
  2. **The "Activities and Societies" line** under Education, split into
     individual items (it's sometimes comma-separated, sometimes
     dash-bulleted) and compared whole-string, never substring.
  3. **The separate "## Organizations" profile section** (its own LinkedIn
     section, distinct from Activities and Societies), parsed the same way.

  All three compare each item as a **whole string** (after stripping
  "Fraternity"/"Sorority"/"Inc."/chapter suffixes/etc), never a substring.
  Greek-letter org names collide constantly (Phi Delta Omega vs. Delta
  Omega, Alpha Phi vs. Alpha Phi Alpha, Sigma Chi vs. Sigma Chi Omega), and
  substring matching would wrongly credit someone with membership in an org
  they aren't in. An optional `exclude` list per org in `org_variants.json`
  is a second layer of protection against known confusables. Variants
  shorter than 3 normalized characters are dropped automatically, short
  abbreviations (a 2-letter Greek monogram, a state code) collide with
  unrelated things far more than they're worth keeping.

  Only if none of the three structured sources hit does matching fall back
  to a free-text search, and even then a match only counts (as `unverified`,
  never `yes`) if a corroborating word, "fraternity", "sorority", "chapter",
  "brother", "pledge", or a Greek chapter-name pattern like "Alpha-Kappa" or
  "Delta-Xi" sits within ~40 characters of it. Without that check, a short
  variant like "D Omega" can surface unrelated things (this genuinely happened
  once during testing: "KS", expanded from "D Omega" by an acronym heuristic
  that has since been removed, matched "Manhattan, KS").

If Exa doesn't return structured entities for a given result, matching falls
back to a text search and is marked `unverified` rather than `yes`, so you
know to double check it.

## Emails + drafts (`--emails`)

Requires `HUNTER_API_KEY` and `ZEROBOUNCE_API_KEY` in `.env`. Also requires
`--sender-name`, `--sender-school`, `--sender-year` (e.g. `"sophomore"`),
and `--sender-industry`. Any of these left off the command line is read from
`profile.json` (`name`, `school`, `year`, `industry`).

Optional: `--sender-signature "Alex Example, President, Example Investing Club"`
(or `signature` in `profile.json`) adds a signature line under the sign-off (it is not a sentence, and it is still
checked by `validate_draft`).

**How the school is said in the hook.** Matching always uses the full `--school`
name. The draft says the short spoken form, looked up in `school_display.json`
("University of Southern California" becomes "USC"; case and punctuation are
ignored). A school that is not listed is written out in full, so a missing entry
is safe. Add a line to the JSON to support a new school, or pass `--school-short`
to override for one run. To re-render existing results with a changed template
without spending any quota, use `--from-csv <file> --drafts-only`. To read the
drafts that would go out: `python3 show_drafts.py <file>`.

```
python3 find.py --school "University of Southern California" \
    --org "Delta Omega" --firm "Goldman Sachs" --city "New York" --emails \
    --sender-name "Alex Example" --sender-school "USC" \
    --sender-year "sophomore" --sender-industry "investment banking"
```

### Paste-a-list mode: LinkedIn's own alumni search, not Exa

LinkedIn's alumni search filtered to a school and a company is the authoritative graph; Exa's people search is
guessing from indexed profile text and misses most of it (in one real comparison for a single school and firm,
the alumni search returned about 70 people and Exa found 4). `--paste-list <file>` skips Exa's find stage entirely:

```
python3 find.py --paste-list list.txt --firm "Boston Consulting Group" \
    --school "University of Southern California" --drafts-only \
    --sender-name "Alex Example" --sender-school "USC" \
    --sender-year "sophomore" --sender-industry "consulting"
```

The file holds name, LinkedIn URL, and headline per person, in whatever shape you copy-pasted (see
`alumni_paste.py` for the accepted forms, one person per paragraph, or one per line with tab/`|` fields, in
any field order). School match is CONFIRMED by the search itself (you ran it filtered to your school) and is
never re-verified or used to drop anyone. Classification runs on the headline alone, for free, before any Exa
spend:

- **no role at the firm**: a current student or an alum elsewhere, excluded
- **support title** (title_filters.json `non_target_roles`), excluded
- **above band** (a senior title, including firm-specific rungs like BCG's "Project Leader"), excluded
- **different track** (title_filters.json `different_track_roles`, e.g. Data Scientist), FLAGGED, kept in the
  output for a possible later, different-purpose run, never drafted this campaign
- everyone else survives to enrichment (the `--enrich-facts` path, about $0.007 per person) and the template hierarchy

Do Not Contact and cross-run dedupe are checked on the survivors before that enrichment spend, same as
everywhere else.

### Draft templates: a hierarchy of real facts

Drafts are not rotated or randomized. Eight templates share one ask ("Would you have 15 minutes for a call in the
next two weeks?") and the available data picks the draft, first match wins. The first four come from your
`profile.json` and are only offered when that field is filled in:

1. shared high school (`high_school`, matched in their education history)
2. shared organization (only the orgs in your profile's `orgs` list, matched as a whole item, never a bare acronym)
3. shared major (`major`, read from their degree line at the target school)
4. home city (`home_city`, matched in their LinkedIn location)
5. distinctive prior employer, VERIFIED only (a LinkedIn company page id, an employee title, a year or more,
   not a club, school, founder or owner role, not named after the person). Unverified falls through.
6. recently joined (2025 or 2026 start)
7. longer tenure (2023 or earlier; the start year is the one number a draft may carry)
8. school fallback

Only list facts that are true about you. A template claims the shared fact out loud ("I also went to ...").

Each row records `template_id`, `template_fact` and `template_source` (the field the fact came from), and
`python3 show_templates.py <file>` prints them per person. A draft is skipped, not guessed, when "before joining"
cannot be verified (their school end year is after their start at the firm). The follow-up reuses the same ask.

`--enrich-facts` (with `--from-csv`) backfills the facts these templates need (start year, degree, verified prior
employer, shared org) for results files made before they were stored, at about a cent per person. Rows already
at the current facts version are skipped.

### Similarity is a warning, pacing is the control

Drafts to one domain will look alike, because the ask is shared on purpose. `find.py` and `bridge.py queue`
report the highest similarity to the same domain as a WARNING and never reject a draft for it. The real risk to a
cold domain is volume, so `bridge.py queue` paces first emails per email domain: `--domain-weekly-max` (default 4)
per Monday to Sunday week, at most one per day, on weekdays, from `--start-date` (default next weekday). Rows over
the limit stay queued with a later `not_before` date (the Sheet sender honors it) and are never dropped.

### Do Not Contact list (`dnc.json`)

`dnc.json` (gitignored, real names; start from `dnc.example.json`) is checked in
`find.py` before any Hunter or ZeroBounce spend (fresh searches and `--from-csv`, no flag skips it) and again in
`bridge.py queue`. A hit on email, LinkedIn URL, or name plus firm excludes the person. An ambiguous match (same
name at another firm, a nickname, a similar first name with the same last name) is excluded and flagged for review.
Exclusions are logged with the reason in `dnc-exclusions.log` and counted in the run summary. If `dnc.json` is
missing or unreadable the run stops. Add someone after a call:

```
python3 find.py --add-dnc "Jane Doe" --dnc-firm "Bain & Company" --dnc-email jane.doe@bain.com --dnc-reason "took a call"
```

Nothing removes an entry automatically. Remove one by editing `dnc.json` by hand.

### Running `--emails` on a result set you already have (`--from-csv`)

`--from-csv path.csv` skips Stage 1 (Exa search) entirely and loads rows
from an existing output CSV instead, for running `--emails` on people
you've already found without re-spending Exa quota:

```
python3 find.py --from-csv "outreach_..._bain-company_....csv" --emails \
    --email-domain bain.com --sender-name "Alex Example" \
    --sender-school "USC" --sender-year "sophomore" --sender-industry "consulting"
```

`--school`/`--firm` aren't required with `--from-csv`. Every column from
the input CSV is preserved in the output, with the email/draft columns
added. If the input CSV predates tier being computed unconditionally (no
`tier` column at all, see the tier note above), tier is backfilled from
`school_match`/`affiliation_match` on load, so tier-3 drafts (the school
hook) still generate correctly even from an old CSV.

### Re-running just the draft stage (`--drafts-only`)

Combine with `--from-csv` to re-render drafts on a result set that
*already has verified emails*, without touching Hunter or ZeroBounce at
all, for exactly the situation where you fixed something about how
drafts are written (a hook bug, a sender detail) and don't want to
re-spend scarce ZeroBounce quota re-verifying addresses that are already
confirmed:

```
python3 find.py --from-csv "outreach_..._with-emails_....csv" --drafts-only \
    --sender-name "Alex Example" --sender-school "USC" \
    --sender-year "sophomore" --sender-industry "consulting"
```

Existing `email`/`email_status` columns are preserved untouched;
`draft_subject`/`draft_body` are re-rendered from scratch. Mutually
exclusive with `--emails`: one verifies emails and drafts, the other only
drafts.

**Scoped to `seniority_band == "in_band"` only**: that's the sole
population that can ever get a draft, and Hunter/ZeroBounce free-tier quota
(this Hunter account: 50 domain-searches / 100 verifications per month;
ZeroBounce: 100 verifications/month, check your own account, don't assume
the numbers on your plan match) is too scarce to spend verifying emails for
people who will never be emailed. Adds 5 columns, blank for everyone else:

| column | meaning |
|---|---|
| `email` | the one address constructed from the firm's cached **mail_domain**: never a list of guesses |
| `email_status` | ZeroBounce's status **verbatim**: `valid`, `invalid`, `catch-all`, `unknown`, `spamtrap`, `abuse`, `do_not_mail`: or `invalid_possible_collision` (see below). Never collapsed into a binary valid/invalid |
| `email_pattern_confidence` | the firm's cached pattern confidence (Hunter's avg per-email confidence across its sample) |
| `draft_subject` / `draft_body` | string-templated (no model calls), only for tiers 1 to 3 (tier 4 has no valid hook, so no draft) |

**Pattern cache (`patterns.json`)** is the compounding asset: `firm ->
{web_domain, mail_domain, pattern, confidence, how_confirmed, date}`. Every
firm is looked up via Hunter's domain-search **once, ever**: always
checked first. It is created on the first `--emails` run and grows across
runs; it holds only firm-level data, so committing it in your own fork is
safe. Hunter is never called per person, only
once per uncached firm.

**`web_domain` vs. `mail_domain`: this distinction exists because of a
real incident.** A firm's website domain and its actual mail domain are not
always the same: Goldman Sachs's website domain is `goldmansachs.com`, but
its mail domain is `gs.com`. Hunter's company-name search resolves the
*website* domain, on 2026-08-16 every constructed address used that
website domain and all 17 came back verified invalid, burning real
ZeroBounce quota, before this was caught and fixed. **Addresses are always
constructed from `mail_domain`, never `web_domain`.** When no mail domain
has been independently confirmed, `mail_domain` falls back to Hunter's
website-domain guess, clearly labeled `(mail_domain UNVERIFIED...)` in
`how_confirmed`: the quota guard below exists specifically to catch that
guess being wrong, fast, instead of burning a whole batch to find out.

- `--email-domain DOMAIN` overrides the mail domain for every firm in this
  run and persists it to `patterns.json`, marked `how_confirmed: "manual:
  ..."`: a manually-confirmed mail domain is never overwritten by a later
  Hunter lookup (Hunter is only used to fill in whatever's still missing , 
  pattern, confidence, web_domain).
- Only set a mail domain you have a real source for. This class of bug
  generalizes badly across finance, so guessing a "few more majors" from
  general knowledge just plants the next version of the same bug. Leave a
  pattern `null` rather than guess it.

**Quota guard:** per firm, once at least 5 addresses have been checked, if
more than 70% have come back `invalid`/`invalid_possible_collision` *and*
the domain is confirmed non-catch-all (a catch-all domain would make
"invalid" meaningless noise, so the guard only fires on a trustworthy
signal), verification stops for the rest of that firm's rows immediately , 
loud warning to stderr, remaining rows still get their address constructed
(free) but no more ZeroBounce calls. This is what would have caught the
`goldmansachs.com` bug after ~5 calls instead of 17. Covered by
`test_email_stage.py` (`python3 -m unittest test_email_stage`) against a
mocked ZeroBounce, zero real API spend, proving: it trips after 5 calls
on a >70%-invalid non-catch-all domain, it never trips on a catch-all
domain even at the same invalid rate, it never trips under the 70%
threshold (including exactly 70%, since the rule is *more than* 70%), and
`invalid_possible_collision` counts toward the rate the same as plain
`invalid`.

**`invalid_possible_collision`** vs. plain `invalid`: on a domain whose
mail_domain is human-confirmed (not Hunter's guess) and that isn't
catch-all, a well-formed address that still comes back invalid is more
likely a name collision than a wrong pattern, Goldman Sachs adds a middle
initial when `first.last` is already taken by someone else. Plain
`invalid` stays reserved for domains without that confirmation, where a
wrong pattern or wrong domain is still the more likely explanation.

**Address construction** substitutes into whatever pattern Hunter actually
returns (`{first}`, `{last}`, `{f}`, `{l}`: e.g. `{first}.{last}`), it
does not assume `first.last`. Accented characters are ASCII-folded
(`José` -> `jose`), hyphenated names keep their hyphen (Hunter has confirmed
real addresses of the `mary-kate.doe@` shape), and a pattern needing data we don't
have (e.g. a middle initial) fails cleanly with a reason in `evidence`
rather than guessing. Exactly one address per person, no permutations are
ever generated for sending.

**Tier is computed unconditionally, whether or not `--org` was used** , 
`affiliation_match` defaults to `"no"` when there's no org to check, so
`compute_tier` still runs and lands correctly on tier 3. A run with no
`--org` is the canonical tier-3 case (school match, no org queried at
all), not tier 4, tier 4 means neither matched, which is a different,
weaker case. (This was a real bug: tier used to only get computed `if
orgs`, so every school-only row silently had no tier at all and could
never get a draft, even with `--emails` and a valid hook available.)

**Draft hook lines branch on tier:**

| Tier | Hook |
|---|---|
| 1 (school AND org) | "I saw you went to `[school]` and you're a `[org]` too.", the two-hook maximum, never a third |
| 2 (org only, different school) | "I saw you're a `[org]` too.", never mentions school |
| 3 (school only, incl. any run without `--org`) | "I saw you went to `[school]` too." |
| 4 (neither) | no draft, there's no real hook to write |

Content rules are enforced in code (`drafts.py`) with a `validate_draft()`
check on every rendered draft, not just followed by convention, and
covered by `test_drafts.py` (`python3 -m unittest test_drafts`): no dash
character in prose (7 codepoints checked: U+2010 through U+2015 and the
minus sign U+2212, i.e. em dash, en dash, figure dash, horizontal bar,
and the Unicode hyphens), never the word "rather", max 5 sentences in the
body, no digits anywhere, and 4 banned phrases ("pick your brain", "hope
this finds you well", "let me know if you'd be open to connecting", "I was
impressed by"). **The ASCII hyphen is allowed inside compounds** ("Simon-Kucher",
"semester-long", "third-year"), but not as punctuation: a spaced " - ", "--",
a leading "- " bullet, or a dangling trailing hyphen is still rejected.
If a render would violate a rule (e.g. an en dash in a firm name), the row's
draft columns stay blank with the reason logged in `evidence` rather than
shipping a rule-violating draft.

## Design: the intended pipeline

This section is the spec the code is growing toward. Each item is marked
**Exists today** or **Roadmap**.

**Onboarding.** The user sets `EXA_API_KEY` (required) and, optionally,
CareerShift, Hunter and ZeroBounce keys, then fills in `profile.json`
(who they are, what they can truthfully claim in common) and `dnc.json`.
- Exists today: `.env` with `EXA_API_KEY` (required), `HUNTER_API_KEY` and
  `ZEROBOUNCE_API_KEY` (optional, for `--emails`); `profile.json`;
  `dnc.json` with a fail-closed check.
- Exists today: CareerShift through your own browser session
  (`careershift.py`, no key needed).
- Roadmap: a guided `onboard` command that asks for each key and writes
  `.env` and `profile.json`.

**Targeted mode: "find 20 people at Bain".** One firm, a count, and the
user's school and city as defaults.
- Exists today: `python3 find.py --school "..." --firm "Bain & Company" --n 20`
  plus `--city`, `--title`, `--org`, seniority bands, and `--emails` for
  addresses and drafts.
- Roadmap: a short natural-language front end that turns "find 20 people at
  Bain" into that command using the profile's school and city.

**Auto mode, fed by the internship scanner.** When the user plans to apply
somewhere later ("applying to FOX in three months"), the engine finds a few
people there now ("find 3 people at FOX"), so the outreach happens weeks
before the application.
- Roadmap: not built. The scanner integration, the scheduling and the
  per-application people count are all future work. Today the same result
  takes a manual `find.py --firm "Fox Corporation" --n 3` run.

**Email discovery and verification.** Verification is optional.
- Exists today: Hunter finds each firm's address pattern once and caches it
  in `patterns.json`; ZeroBounce verifies each constructed address, with a
  quota guard that stops spending on a domain that is clearly wrong.
- Exists today: CareerShift as an email source (`careershift.py`).
  CareerShift addresses are treated as verified.
- Exists today: `drafts.py` feeds CareerShift and Exa results straight into
  drafts for one company.
- Roadmap: a "safe" mode that adds ZeroBounce verification on top of any
  source, CareerShift included.

**Drafts land in the user's email drafts folder.** The user edits and hits
send.
- Exists today: drafts are written to the output CSV
  (`draft_subject`, `draft_body`); `show_drafts.py` prints them for review;
  the optional Google Sheet sender (`bridge.py` plus the Apps Script in
  `sender/`) queues and paces sends from Gmail with a kill switch on bounces.
- Exists today: `drafts.py` creates each draft directly in the user's Gmail
  drafts folder. It asks for the `gmail.compose` scope and only creates drafts.
- Roadmap: Outlook drafts.

**Send confirmation mode.** Every send needs an explicit approval, and the
mode stays ON until the user has approved the copy a few times. Only then
can it be turned off.
- Exists today: nothing is sent implicitly. `bridge.py queue` requires one
  of `--dry-run`, `--export` or `--push`, and the sheet sender starts paused.
- Roadmap: a per-user approval counter that keeps confirmation mode on
  until a set number of drafts have been approved unchanged.

## Notes

- Results are deduped twice: first by normalized LinkedIn URL (handles
  `www.` vs. non-`www.`), then by normalized (name, firm), the same person
  can come back under two different URL formats that don't collapse via the
  first pass.
- Never scrapes LinkedIn directly and uses no browser automation. People come
  from Exa's API and, for `careershift.py` and `drafts.py`, CareerShift's API
  through your own session.
- `.env` is gitignored; the API key is never printed.
- Output CSVs are gitignored too, since they contain real people's data , 
  commit them yourself if you actually want a CSV under version control.

Built with Chewbacca
