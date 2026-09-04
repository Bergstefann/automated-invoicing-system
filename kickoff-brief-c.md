# Kickoff prompt — schema contract + parser hardening

Run from `C:\Users\thoma\automated-invoicing-system`.

Copy `brief-c-schedule-schema-contract.md` into this directory before starting.

---

Read `brief-c-schedule-schema-contract.md` in this directory first. It's the full spec
for this work. This message covers only the first step.

**This is a finished, audited, working repo.** It already has a clean close-out audit
behind it, a passing CI pipeline, and a postmortem doc that is the strongest single
artefact in the whole portfolio. Nothing here gets touched carelessly.

## Ground rules for this session

- Work on a **new branch**, not `main`. Something like `feat/schema-contract`.
- Do not modify, rewrite, or "improve" `docs/POSTMORTEM-double-billing.md` beyond what
  the brief explicitly allows: a clearly dated, clearly separate follow-up section at the
  end. The existing text describing what actually happened stays exactly as it is.
- Do not touch the live Google Sheet or its API credentials. This work is about the
  parsing contract and code, not the production data source.
- No `Co-Authored-By` trailers. This repo already needed a `git filter-repo` rewrite
  once for exactly that.

## Step 1 — Inventory. Change nothing.

Per Phase 1 of the brief, report:

- How the schedule is currently parsed: module, assumptions about columns, header
  position, date formats, student naming.
- **Every heuristic in the parser**, named explicitly. These are what the schema
  contract has to eliminate.
- How a student is identified today, end to end, from schedule row to invoice. State
  precisely what the identity key is right now. This is the crux of the whole exercise,
  since the double-billing incident happened because identity wasn't stable.
- What happens today on a malformed or unexpected sheet: fail loudly, or silently
  produce something plausible?
- Current test coverage of the parsing path and what fixtures already exist.
- Confirm whether the live schedule is still a Google Sheet via the Sheets API. The
  brief assumes so. If anything's changed, say so.

Also do a quick sanity pass relevant to this specific session: read
`docs/POSTMORTEM-double-billing.md` in full and summarise, in your own words, exactly
what the root cause was and what was fixed at the time. I want to confirm we agree on
what actually happened before any follow-up section gets written.

**CHECKPOINT: report all of this before proposing or building anything.**

I'll respond with the schema design decisions (student_id format, versioning approach,
how the workbook and the Sheets-based production source should relate) once I've seen
what's actually there.
