# `src/invoicing/schedule_contract.py`

Module purpose (per docstring, lines 1-19): the single source of truth for
what a valid lesson schedule *is*, independent of the source format (Excel,
Sheets API, etc). It is pure validation logic over already-extracted
`dict[str, object]` rows — it does no I/O and knows nothing about where the
rows came from. Its only caller in this codebase is
`src/invoicing/workbook.py::load_schedule_workbook` (lines 110-138), which
supplies rows read from an `.xlsx` file via `openpyxl`.

Two register-level rules the module docstring calls out (lines 8-16) are the
organizing intent for the whole file:

- `student_id` is the *only* identity a schedule row may reference; name,
  rate, and contact fields are mutable attributes, not identity.
- Every schedule row's `student_id` must already exist in the register —
  no silent-skip fallback for an unresolvable id.

---

## Module-level constants (lines 26-37)

- `SUPPORTED_SCHEMA_VERSIONS = frozenset({1})` — single supported version.
  Consumed only by `validate_schema_version`.
- `VALID_STATUSES = frozenset({"Y", "YI", "N"})` — closed vocabulary for
  `ScheduleRow.status`. Consumed only by `validate_schedule_rows`.
- `REQUIRED_STUDENT_COLUMNS` / `REQUIRED_SCHEDULE_COLUMNS` — tuples defining
  both what `validate_students_register`/`validate_schedule_rows` require
  present-and-non-blank per row, and (via `workbook.py`) what column headers
  must exist in the source sheet. These two uses (contract-level required
  fields vs. sheet-level required headers) are coupled only by both files
  importing the same tuple; nothing in this module enforces that a caller
  supplying rows from elsewhere also enforces header presence — a caller
  that hands rows missing a key entirely (not just blank) is handled by
  `row.get(column)` returning `None`, which the blank check below treats
  identically to an empty string (line 110, line 157).

---

## `ScheduleContractError` (lines 40-43)

A `ValueError` subclass used for every contract violation in this module.
Docstring asserts messages are always specific enough to act on (row number
+ what was found). This is a documentation-level property, not something
structurally enforced — each raise site individually embeds `line_number`
and the offending value; there is no mechanism preventing a future edit from
adding a raise without that detail.

---

## `StudentRegisterEntry` (lines 46-57) and `ScheduleRow` (lines 59-70)

Both are frozen dataclasses — immutable once constructed. `ScheduleRow`'s
docstring states an invariant that this module itself is fully responsible
for establishing: "`student_id` is guaranteed resolvable by the time this is
constructed" (lines 61-63). That guarantee is established entirely by the
membership check at line 164-167 inside `validate_schedule_rows`, which runs
before the `ScheduleRow(...)` construction at line 201. There is no
enforcement inside the dataclass itself (frozen dataclasses do not validate
field values) — the guarantee is a caller-order invariant, not a
type-level one. Nothing prevents a future caller from constructing
`ScheduleRow` directly (bypassing `validate_schedule_rows`) with an
unresolvable `student_id`; the invariant lives only in the validation
function's discipline, not in the type.

---

## `validate_schema_version(version: object) -> int` (lines 72-88)

**Purpose:** validate an untyped value (from a spreadsheet cell) is a
supported schema version int.

**Logic:**
- Line 78: rejects anything that is not `int`, or that *is* `bool` — this
  explicitly guards against Python's `bool` being an `int` subclass (so
  `True`/`False` from a checkbox-like cell can't silently pass as version
  `1`/`0`).
- Line 83: rejects an int not in `SUPPORTED_SCHEMA_VERSIONS` (currently only
  `{1}`).
- Returns the value narrowed to `int` (line 88) — the return type lets
  callers skip a second isinstance check, per docstring lines 74-77.

**Invariants on return:** returned value is an `int` and a member of
`SUPPORTED_SCHEMA_VERSIONS`.

**Assumptions on input:** none — `version: object` is treated as fully
untrusted, consistent with docstring line 76 ("untyped user-editable
input").

**Callee:** none (self-contained, only frozenset membership and isinstance
checks).

**Caller dependency:** `workbook.py` line 121 calls this with
`legend[SCHEMA_VERSION_CELL].value`, i.e. whatever `openpyxl` returns for
cell B2 (could be `None`, a string, a float, a formula-result, etc. per
`data_only=True` load at workbook.py line 118). The caller depends on this
function to reject anything not a clean supported int; it does not use the
returned `version` for anything beyond storing it in `WorkbookSchedule`
(workbook.py line 138) — it is not used to branch schema-version-dependent
parsing logic anywhere in this module or its caller.

---

## `_require(condition: bool, message: str) -> None` (lines 91-93)

Trivial assertion helper: raises `ScheduleContractError(message)` if
`condition` is falsy, otherwise no-op. All row-level required-field and
membership/status checks in `validate_students_register` and
`validate_schedule_rows` route through this. It performs no argument
validation of its own (e.g., `message` could be anything) — it's a pure
control-flow helper, not a boundary.

---

## `validate_students_register(rows) -> dict[str, StudentRegisterEntry]` (lines 96-136)

**Purpose:** validate the Students sheet rows and build the identity
register keyed by `student_id`.

**Per-row logic** (`line_number` starts at 2, treating input row 0 as
spreadsheet header row 1 — line 108):

1. Lines 109-114: for each of `REQUIRED_STUDENT_COLUMNS`, require
   `row.get(column)` is not `None` and, stringified and stripped, is
   non-empty. This is the *only* place blank/missing values are rejected;
   it runs before any type-specific check below.
2. Line 116: `student_id = str(row["student_id"]).strip()` — direct
   dict access (not `.get`), safe only because step 1 already guaranteed
   `student_id` is present in `REQUIRED_STUDENT_COLUMNS` and passed the
   blank check for every row (student_id is one of the five required
   columns, line 30).
3. Line 117: `rate_cents = row["rate_cents"]` — raw value, no stringify yet.
4. Lines 118-122: `rate_cents` must be `int` (excluding `bool`) and `>= 0`,
   else raise. Note: this is a stricter check than the generic blank check
   — a `rate_cents` of `0` passes the blank check (str(0).strip() != ""
   → "0" != "" → True) and is explicitly allowed by `>= 0`, i.e. free
   lessons/rate are a valid register state.
5. Lines 123-127: `student_id not in register` — duplicate-id check. This
   is the module's stated "one identity rule the register itself must
   never break" (docstring lines 104-106). It runs *after* the row has
   already had its `rate_cents` validated but *before* the row is added to
   `register`, so a duplicate is caught deterministically at the second
   occurrence regardless of which row's other fields are "better."
6. Lines 129-135: construct `StudentRegisterEntry`, storing `name`,
   `parent_name`, `parent_email` as stripped strings; `rate_cents` as the
   validated int; keyed into `register` by `student_id`.

**Invariants on return:** every key in the returned dict is a stripped,
non-empty string appearing exactly once across `rows`; every value's
`rate_cents` is a non-negative int; all string fields are stripped.

**Order dependency (raises on *first* violation in row order):** established
by the single top-to-bottom `for` loop with immediate raises — no
accumulation of multiple errors, no continue-on-error path exists anywhere
in this function.

**Unenforced assumptions:**
- Nothing normalizes `student_id` case or Unicode form — two ids differing
  only by case or whitespace-adjacent characters not caught by `.strip()`
  (e.g. `"A1"` vs `"a1"`) are treated as distinct register entries. Nothing
  in this function (or the caller) enforces a canonical id format.
- `str(row[column])` at lines 116/131/133/134 will call `str()` on whatever
  type the raw cell value is (could be a float, a `datetime`, etc. if the
  upstream reader is other than `workbook.py`) — no type restriction on
  `name`, `parent_name`, `parent_email`, or `student_id` beyond "not blank
  after stringifying." A numeric-looking student_id (e.g. an Excel cell
  auto-typed as float `1.0`) would stringify to `"1.0"`, not `"1"` —
  nothing in this module normalizes numeric-vs-string id representations
  between the Students and Schedule sheets; that consistency depends on
  whatever populates `row["student_id"]` upstream (in `workbook.py`, both
  sheets independently `str(...).strip()` their raw id at lines 127/134 of
  workbook.py, so as long as both cells hold the same underlying openpyxl
  type per id, the stringification is at least consistent between the two
  read paths — but this module itself does not guarantee or check that).

**Callees:** none beyond `_require`, `str`, `isinstance` — no I/O, no calls
into other modules.

---

## `validate_schedule_rows(rows, register) -> list[ScheduleRow]` (lines 139-210)

**Purpose:** validate Schedule sheet rows against an **already-validated**
register (the function takes `register: dict[str, StudentRegisterEntry]` as
a parameter — it does not build or re-validate it).

**Precondition (unenforced by this function):** `register` must already be
the output of `validate_students_register` (or an equivalent structure) —
this function performs no validation of `register`'s internal consistency
(e.g., it does not check keys equal values' `.student_id`, does not check
for `None` register, does not check register is non-empty). If called with
`register={}`, every row with a non-blank `student_id` fails the membership
check at line 164-167. If called with a `register` dict whose keys don't
match the format used by rows' `student_id` after stripping (e.g. built by
some other caller with unstripped or differently-cased keys), rows will
spuriously fail the membership check — this function trusts the shape of
`register` completely; only `workbook.py` line 128 establishes that
`register` came from `validate_students_register`.

**Per-row logic** (`line_number` starts at 2, same header-row convention as
above, line 155):

1. Lines 156-161: blank/missing check for all of
   `REQUIRED_SCHEDULE_COLUMNS`, identical pattern to the students function.
2. Lines 163-167: `student_id = str(row["student_id"]).strip()`, then
   membership check against `register`. This is the module's headline
   contract rule (docstring lines 14-16, 147-149): **no silent skip** on
   an unresolvable id — raises immediately rather than dropping the row.
   Note this check happens *before* `lesson_date`/`start_time`/
   `duration_minutes`/`status` are validated, so a row with both a bad
   student_id and a bad date will report the student_id violation first.
3. Lines 169-173: `lesson_date` must be `isinstance(..., date)`. Note:
   `datetime.datetime` is a subclass of `datetime.date` in Python, so a
   `datetime` value would pass this isinstance check silently — this
   function relies on the caller (`workbook.py::_split_datetime_fields`,
   lines 92-107) to have already narrowed any `datetime` to a plain `date`/
   `time` before calling this function. This module does not itself
   distinguish `date` from `datetime`; if a caller passed a raw `datetime`
   through as `lesson_date`, it would pass this check and be stored as-is
   in `ScheduleRow.lesson_date`, which callers of *this* module's output
   might then assume is a bare `date` (per the field's type annotation,
   line 66) — the annotation is not runtime-enforced here.
4. Lines 175-179: `start_time` must be `isinstance(..., time)`. Same
   `datetime`-is-not-`time` caveat does *not* apply here since
   `datetime.datetime` is not a subclass of `datetime.time` — so a raw
   `datetime` in `start_time` *would* be correctly rejected by this
   isinstance check (consistent with workbook.py's own comment at lines
   98-100 about this exact asymmetry).
5. Lines 181-190: `duration_minutes` must be `int` (excluding `bool`) and
   `> 0` — strictly positive, unlike `rate_cents`'s `>= 0`. Zero-duration
   lessons are rejected here.
6. Lines 192-197: `status = str(row["status"]).strip().upper()` then
   membership check against `VALID_STATUSES`. Case-insensitive by design
   (uppercased before comparison) — this is the only field in either
   function that normalizes case before validating.
7. Lines 199-200: `assert isinstance(lesson_date, date)` /
   `assert isinstance(start_time, time)` — redundant re-assertions of
   checks already performed at lines 171/177. These exist purely to narrow
   the static type for the type checker (no runtime effect beyond
   defense-in-depth, since if the earlier `_require` didn't raise, these
   assertions cannot fail under normal Python execution — unless run with
   `-O` optimization, which strips `assert` statements entirely, removing
   this narrowing but not the actual validation, since the actual
   validation at lines 170-179 uses `_require`/`raise`, not `assert`, so
   `-O` does not weaken the contract itself, only the type-narrowing
   redundancy).
8. Lines 201-209: construct `ScheduleRow` with the validated/normalized
   fields.

**Invariants on return:** every `ScheduleRow` in the returned list has a
`student_id` present in the `register` passed in; `lesson_date` is a `date`
instance; `start_time` is a `time` instance; `duration_minutes` is a
strictly positive int; `status` is one of `{"Y", "YI", "N"}` (uppercased).

**Order dependency:** same as `validate_students_register` — single pass,
first violation raises immediately, no partial/accumulated results ever
returned.

**Callees:** `_require`, `str`, `isinstance` — no I/O, no calls into other
modules. Does not call `validate_students_register` itself (caller's
responsibility to sequence these two calls correctly, per `workbook.py`
lines 128/136).

---

## Cross-function / module-level observations

- Both validation functions use the same `line_number = enumerate(rows,
  start=2)` convention assuming `rows` corresponds 1:1 to spreadsheet rows
  starting at row 2 (row 1 = header). This is an assumption about the
  caller's row-extraction order and completeness — nothing in this module
  checks that `rows` wasn't filtered, reordered, or sourced from something
  other than a sheet read top-to-bottom. In `workbook.py`, `_read_rows`
  (lines 81-89) does preserve top-to-bottom order and only skips fully-blank
  rows (also skipping them from `line_number` counting in this module,
  since blank rows are simply never in the list) — so a fully-blank row
  in the middle of a real sheet would cause this module's reported
  `line_number` for subsequent rows to be *off by however many blank rows
  preceded it*, understating the true spreadsheet row number. This is a
  caller-side behavior this module has no visibility into and cannot
  correct for.
- Neither function validates that `rows` is non-empty; an empty `rows` list
  produces an empty register / empty schedule with no error. Whether an
  empty schedule or empty register is acceptable is not decided in this
  module.
- No function in this module performs any cross-row structural checks
  beyond the `student_id` duplicate check (e.g., no check for duplicate
  lesson rows, no check that lesson_date/start_time combinations are
  sane relative to each other, no overlap detection between lessons for
  the same student).
- All error paths are exceptions (`ScheduleContractError`); there is no
  return-value error signaling anywhere in this module, so any caller that
  does not wrap calls in try/except will propagate the exception directly.
