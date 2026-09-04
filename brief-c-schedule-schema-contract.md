# Brief: Lesson schedule schema contract + Invoicer parser hardening

## What this is and why

Two deliverables that are really one idea:

1. An **Excel workbook** that defines the canonical lesson-schedule format: typed
   columns, validation, stable identifiers, a version marker.
2. A change to **Invoicer** (`automated-invoicing-system`) so its schedule parser
   **validates against that contract** instead of heuristically guessing at an undefined
   sheet layout.

### The reason this matters more than it looks

Invoicer's strongest artefact is `docs/POSTMORTEM-double-billing.md`: a parent's email
change forked a duplicate student record, and a real run billed 44 invoices instead of 22.
That was root-caused, fixed, and regression-tested.

But the underlying shape of that bug was **identity keyed on something mutable**. A schema
contract with a stable, immutable `student_id`, issued once and never derived from an email
address or a name, is the *structural* remediation for that entire class of bug, not just
the one instance.

That's the story this work tells: incident → root cause → point fix → structural fix. Very
few junior portfolios show the fourth step.

## Honesty constraint — read this before touching the postmortem

The postmortem describes what actually happened and what was actually done at the time.

- **Do not rewrite it to imply the schema contract existed then, or that it was part of the
  original fix.** It wasn't.
- If it should reference this work, that goes in a clearly dated **follow-up section** at
  the end, written as a later structural change.
- Do not upgrade or dramatise any claim in the existing text.

## Phase 1 — Inventory. Change nothing.

Report:

- How the schedule is currently parsed: which module, what it assumes about columns,
  header position, date formats, student naming.
- **Every heuristic in the parser**, meaning each place it guesses rather than knows. Name
  them explicitly; these are what the contract has to eliminate.
- How a student is currently identified, end to end, from schedule row to invoice. This is
  the crux. Say precisely what the identity key is today.
- What the parser does now with a malformed or unexpected sheet: does it fail loudly, or
  silently produce something plausible?
- Current test coverage of the parsing path, and what fixtures exist.

Note that the live schedule is a Google Sheet and the pipeline uses the Sheets API. The
deliverable here is an `.xlsx` because the portfolio targets a Microsoft-stack market.
That's decided. So be explicit in your report about how you propose the two relate.
My expectation is that the workbook is the **canonical schema definition and test
fixture**, and the parser validates any source (Sheets or xlsx) against it. Flag it if you
see a problem with that.

**CHECKPOINT: report before building.**

## Phase 2 — The workbook

An `.xlsx` that is simultaneously a spec, a usable template, and a test fixture.

- **Schema version cell**, so the parser can refuse a version it doesn't understand.
- **Students sheet**, the identity register. Stable `student_id` as primary key, issued
  once. Name, rate, and contact are *attributes*, explicitly not identity. A comment in
  the sheet saying so, since that's the whole point.
- **Schedule sheet**, one row per lesson: `student_id` (validated against the register,
  not free text), date, start time, duration, status. Dropdown validation on status.
  Typed, formatted columns. No free-text student names anywhere in this sheet.
- **Legend sheet**, covering which cells are editable, what each column means, and one
  example row of realistic values showing expected format.
- Pseudonymous example data only. No real students, parents, or addresses. This is going
  in a public repo.

## Phase 3 — Parser changes

- Parse **against the contract**: required columns present, types correct, `student_id`
  resolvable in the register, schema version supported.
- **Fail loudly and specifically.** `Row 47: student_id 'S-0031' not found in register`,
  not a silent skip, not a best guess. Every heuristic identified in Phase 1 either
  becomes a validated rule or is deleted.
- Tests, including deliberately malformed fixture workbooks: missing column, unknown
  `student_id`, duplicate `student_id` in the register, bad date format, unsupported
  schema version, and, directly echoing the incident, **an attribute change (email or
  name) on an existing `student_id`, which must resolve to one student, not two.**
- Keep the existing regression test for the original double-billing bug passing. If the
  refactor changes its shape, say so rather than quietly rewriting it.
- Maintain the existing bar: strict mypy, CI green, and report the real test count and
  coverage after the change rather than carrying the old README numbers forward.

## Phase 4 — Documentation

- `docs/SCHEDULE-SCHEMA.md`, the contract in prose: every column, type, required or
  optional, allowed values, identity semantics, versioning policy, and what the parser
  does on each violation.
- README updated: test count and coverage re-verified against an actual run, not assumed.
- Screenshots of the workbook for the README. Nobody opens an `.xlsx` from GitHub.
- Dated follow-up section on the postmortem, per the honesty constraint above.

## Standing rules

- **No `Co-Authored-By` trailers on any commit.** This repo needed a `git filter-repo`
  rewrite of 23 commits to strip these once already.
- Conventional commits.
- Verify by running things, not by inferring.
- Anything irreversible, or needing my login, hand back to me.
