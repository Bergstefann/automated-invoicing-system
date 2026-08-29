# Lesson schedule schema contract

This is the contract every schedule source is validated against: a template
workbook (`templates/lesson_schedule.xlsx`) *and* the source code
(`invoicing.schedule_contract`, `invoicing.workbook`). They're generated and
tested against each other (`scripts/build_schedule_template.py`,
`tests/unit/test_workbook.py::test_the_committed_template_loads_and_validates`),
so this document, the workbook, and the code cannot silently drift apart.
If one changes, the other's tests catch it.

It exists because of
[the double-billing incident](POSTMORTEM-double-billing.md): a parent's
email address changing between two syncs forked a duplicate student record,
because student identity was never anything but a name and a lookup key
built transitively through a parent's contact details. This contract makes
identity a first-class, immutable value instead. See
["Identity semantics"](#identity-semantics) below.

## Versioning

The Legend sheet's `B2` cell holds an integer `schema_version`. The parser
checks it against `invoicing.schedule_contract.SUPPORTED_SCHEMA_VERSIONS`
(currently `{1}`) before reading anything else.

- Missing, blank, or non-integer → rejected: `"schema_version must be an
  integer, found <value> (supported versions: [1])"`.
- An integer outside the supported set → rejected: `"unsupported
  schema_version <n> (supported versions: [1])"`.

There is no fallback or best-effort parse for an unsupported version. A
workbook that fails this check is refused before either sheet below it is
even read. It is not a case where students validate but lessons don't; the
whole file fails at once.

## Identity semantics

`student_id` is the **only** identifier a student ever has. It is:

- **Text**, formatted `S-0001`, `S-0002`, … , stored and validated as a
  string, never a number. (In the workbook specifically, the column is also
  given an explicit text number format, `@`, so Excel can't strip a leading
  zero or reinterpret `S-0001`-shaped input as a date or scientific
  notation. That's a real failure mode for numeric-looking IDs in a
  spreadsheet.)
- **Issued once**, the first time a student appears in the Students
  register, and never reassigned or reused, not even if the student later
  leaves.
- **Never derived from name or email.** In the database
  (`invoicing.db.Database.insert_student`), it's minted from the new row's
  own primary key at insert time. In the workbook, it's whatever the person
  editing the sheet typed into a new row, by convention the next number in
  sequence.

Everything else about a student, `name`, `rate_cents`, `parent_name`,
`parent_email`, is an **attribute**, not identity. Attributes can change
freely across edits or syncs without ever creating a second student record.
This is the direct structural fix for the incident. The old pipeline kept
matching a *parent* by email and a *student* by `(name, parent_id)`, so an
email change (a normal, legitimate edit) forked a new parent row, which
forked a new student row underneath it. Under this contract there is
nothing to fork: `student_id` doesn't move when an attribute does.

A schedule row that references a `student_id` not present in the register
is a contract violation, not a skipped row (see
["Failure behaviour"](#failure-behaviour-by-violation-type) below). There
is no silent "couldn't match, so drop the lesson" path left anywhere in
this contract.

## Sheets

### Legend

One cell matters to the parser: `B2`, the schema version (see
["Versioning"](#versioning)). The rest of the sheet is prose for a human
editing the workbook, covering what's safe to edit, a column glossary, and
one example row. `invoicing.workbook` doesn't read any of it.

### Students — the identity register

One row per student. Column order in the sheet doesn't matter; the loader
reads by header name.

| Column | Type | Required | Notes |
|---|---|---|---|
| `student_id` | text, `S-0001` format | yes | Identity. Must be unique across the register. |
| `name` | text | yes | Attribute. Display name only. |
| `rate_cents` | non-negative integer | yes | Attribute. Whole cents: `4000` means $40.00, not $4000. |
| `parent_name` | text | yes | Attribute. |
| `parent_email` | text | yes | Attribute. Not validated as a well-formed email address by the contract itself, only that it's non-blank. |

A blank/missing value in any required column, a `rate_cents` that isn't a
non-negative integer, or a `student_id` repeated across two rows are all
contract violations (see below).

### Schedule — one row per lesson

| Column | Type | Required | Allowed values |
|---|---|---|---|
| `student_id` | text | yes | Must already exist in the Students register. |
| `lesson_date` | date | yes | A real date, not text that merely looks like one. |
| `start_time` | time | yes | A real time. |
| `duration_minutes` | positive integer | yes | Whole minutes, `> 0`. |
| `status` | text | yes | One of `Y` (billable, not yet invoiced), `YI` (already invoiced), `N` (not billable, e.g. cancelled). Case-insensitive on input, normalized to uppercase. |

There is no student *name* column on this sheet at all, by design. The
old Sheets-based schedule joined lessons to students by first name alone.
This sheet can't express that join even by accident, because the only
identity column it has is `student_id`.

## Failure behaviour by violation type

Every violation raises `invoicing.schedule_contract.ScheduleContractError`
(a `ValueError` subclass) with a message specific enough to act on without
re-opening the source file. Nothing in this path silently skips a row,
guesses a value, or produces a partial result. A workbook either loads
completely valid, or it doesn't load at all.

| Violation | Example message |
|---|---|
| Missing/non-integer schema version | `schema_version must be an integer, found None (supported versions: [1])` |
| Unsupported schema version | `unsupported schema_version 2 (supported versions: [1])` |
| Missing required sheet | `missing required sheet 'Students'` |
| Missing required column | `'Schedule' sheet is missing required column(s): ['duration_minutes']` |
| Missing/blank required value | `Students row 4: missing required value for 'parent_email'` |
| Bad `rate_cents` | `Students row 3: rate_cents must be a non-negative integer, found 'forty dollars'` |
| Duplicate `student_id` in the register | `Students row 5: duplicate student_id 'S-0001' (already registered at an earlier row)` |
| Unresolvable `student_id` on a schedule row | `Row 47: student_id 'S-0031' not found in register` |
| Bad `lesson_date` / `start_time` | `Schedule row 12: lesson_date must be a date, found '2/3/2026'` |
| Bad `duration_minutes` | `Schedule row 12: duration_minutes must be a positive integer, found 0` |
| Unknown `status` | `Schedule row 12: status 'Maybe' is not one of ['N', 'Y', 'YI']` |

Row numbers count the actual spreadsheet row (header is row 1, so the
first data row is row 2), so a message points straight at the cell to fix.

## What this replaces

Phase 1 of this work inventoried every heuristic in the old Sheets-only
parser (`invoicing.providers.google`). Under this contract:

- **Header-row and date-block detection** (`_is_header_row`,
  `_parse_date_cell` guessing at a row's shape). Still exists, but only
  inside `GoogleSheetProvider`, which is explicitly the *translation* layer
  for the live Sheet's own wrapped grid layout, not part of the contract
  itself (see the next section).
- **Year inferred from `date.today()` rather than read from the sheet.**
  Unchanged; still a `GoogleSheetProvider`-only heuristic. Not touched by
  this work.
- **Student identity = first name, lowercased.** Replaced entirely, for
  any workbook-sourced schedule: `student_id` is the only identity a
  Schedule row carries. `GoogleSheetProvider` still resolves the live
  Sheet's names to the database's `student_id` internally (see below), but
  that resolution is no longer the schedule's identity model. It's one
  provider's adapter to it.
- **Silent skip on an unmatched name.** Replaced by a required, specific
  `ScheduleContractError` for the workbook path. `GoogleSheetProvider`'s
  sync-time behavior (drop a lesson whose name matches no roster row) is
  unchanged, a live-Sheet-only heuristic, out of scope for this pass
  (see "Known limitations" below).
- **Free-text, unvalidated status.** Replaced by the fixed `{Y, YI, N}`
  vocabulary on the Schedule sheet, enforced by `validate_schedule_rows`.
- **Rate parsed by stripping non-digit characters, defaulting to 40 on a
  blank cell.** Replaced by a strict integer check with no default.
- **Sheet write-back keyed by `(first_name, date)`, reconstructed by
  splitting a display name.** Fixed directly, not just superseded by the
  workbook path: `GoogleSheetProvider.mark_lessons_billed` now resolves a
  cell via the database's `student_id`, using an identity map
  (`student_id -> first_name`) built once at sync time from the same join
  that creates the identity, instead of re-deriving a first name from
  `student.name.split()[0]` on every write-back call. See the postmortem's
  dated follow-up section for the incident-facing version of this.

## How the live Google Sheet relates to this contract

The live schedule is still read via the Sheets API by `GoogleSheetProvider`,
in its original nested weekly-block layout: dates wrapping downward,
`[name, status]` cell pairs under each date header. That layout is a
property of the live Sheet, not of this contract, and this work does not
change it (per this session's ground rules, the live Sheet and its
credentials are untouched).

`GoogleSheetProvider` is the translation boundary. It parses its native
grid into the pipeline's shared `ScheduleSnapshot`/`SheetLessonRecord`
DTOs internally, the same way it always has, then `sync_schedule_into_db`
resolves each Sheet-native first name to the database's `student_id`. That's
the one place in the whole pipeline where a name is still an identity lookup
key, because the live Sheet's own data model has no other option. From that
point on (write-back, billing, everything downstream), `student_id` is what
moves, not a name.

A workbook built to this contract, by contrast, needs no translation step
and no name-based join at all. `invoicing.workbook.load_schedule_workbook`
reads it directly into the same canonical shape
(`invoicing.workbook.to_schedule_snapshot`), because every row in it already
carries its real `student_id`.

## Known limitations

- **The workbook loader isn't wired into the CLI as a live source.**
  `invoicing.workbook.load_schedule_workbook` and `to_schedule_snapshot`
  are fully implemented, unit-tested (`tests/unit/test_workbook.py`), and
  produce the exact `ScheduleSnapshot` shape `sync_schedule_into_db`
  already consumes. But `invoicing/cli.py` has no `--xlsx-path` flag or
  equivalent to actually point `sync`/`run` at a workbook file yet. Right
  now the only way to exercise the loader is from Python directly. Wiring
  it into the CLI is a natural next step, not a design gap. The contract
  and the loader were built to make that wiring trivial once it's wanted.
- **`GoogleSheetProvider`'s remaining heuristics are unchanged.**
  Header/date-block detection, year-from-`date.today()`, silent-skip on an
  unmatched roster name, the `billing_type` hardcode, and duration/
  instrument/school defaults are all still exactly as inventoried in Phase
  1 of this work. Only the write-back identity resolution was fixed (see
  above). The rest are named here as known, disclosed limitations of the
  live-Sheet path, not silently carried forward.
- **The parent-matching-on-email pattern is untouched.**
  `Database.get_or_create_parent` still looks up a parent by email, the
  same kind of mutable-field lookup that caused the original incident, one
  level up the chain from students. It hasn't forked a duplicate student
  since the original fix (student lookup no longer depends on which parent
  row resolved), but the email-keyed parent lookup itself is still there,
  unaddressed by this work.
