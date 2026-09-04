# `src/invoicing/workbook.py`

Module purpose (from its own docstring, lines 1-22): reads the schema-contract
`.xlsx` workbook (e.g. `templates/lesson_schedule.xlsx`) and validates it
against `invoicing.schedule_contract`, producing the same `ScheduleSnapshot`
shape any live-Sheet `SheetProvider` produces. Unlike `GoogleSheetProvider`,
this module never invents identity — every row it reads already carries an
explicit `student_id`, and all identity/type/vocabulary rules are delegated
to `schedule_contract`. This module's own job is narrower: sheet lookup,
header-based column resolution, blank-row truncation, and one Excel-specific
datetime-shape normalization.

---

## `WorkbookSchedule` (dataclass, frozen) — lines 52-56

Plain immutable container: `schema_version: int`, `register: dict[str,
StudentRegisterEntry]`, `lessons: list[ScheduleRow]`. No behavior. Its
invariant ("every row in it is known-good", stated in
`load_schedule_workbook`'s docstring at lines 115-116) is established
entirely by its one constructor call at line 138 — nothing on the dataclass
itself enforces it. Any other code path that constructs a `WorkbookSchedule`
directly (bypassing `load_schedule_workbook`) would silently break that
invariant; `frozen=True` only prevents *mutation* after construction, not
construction with unvalidated data.

---

## `_require_sheet(workbook: Any, name: str) -> Worksheet` — lines 59-63

Looks up a sheet by exact name via `workbook[name]` (line 61) and converts
`KeyError` into `ScheduleContractError`.

- **Callee**: `openpyxl.workbook.workbook.Workbook.__getitem__`
  (`.venv/Lib/site-packages/openpyxl/workbook/workbook.py:277-287`). Confirmed
  by reading its source: it does an exact, case-sensitive, unstripped
  string-equality scan over `self.worksheets + self.chartsheets` (line 285),
  and raises `KeyError` — exactly the exception this function catches — on no
  match (line 287). So the `except KeyError` here is a precise match to what
  the callee can raise for "sheet not found"; no other exception type is
  possible from that call for that failure mode.
- **Invariant established for callers**: `_require_sheet(wb, name)` either
  returns a real `Worksheet`/`ReadOnlyWorksheet` for that exact `name`, or
  raises `ScheduleContractError`. There is no code path that returns `None`
  or a sentinel.
- **Assumption, unenforced here**: sheet-name matching is case-sensitive and
  whitespace-sensitive (no `.strip()` / `.lower()`, unlike the header-column
  matching in `_header_columns` at line 71). A workbook with a sheet named
  `"legend "` or `"LEGEND"` fails with "missing required sheet" even though a
  human would consider it present. Nothing in this module normalizes sheet
  titles before comparison — contrast with column headers, which are
  explicitly normalized.
- `workbook: Any` — the parameter is typed `Any`, not `Workbook`, so this
  function makes no static assumption about the object beyond supporting
  `__getitem__` that raises `KeyError` on a miss. In practice it is always
  called with the return value of `openpyxl.load_workbook` (line 118), so in
  this module the only concrete objects passed are `Workbook` and, for
  `read_only=True` (which is what's actually used, line 118), an
  `openpyxl.workbook._read_only.ReadOnlyWorkbook`-style wrapper. Not
  independently verified here that the read-only wrapper's `__getitem__`
  behaves identically to `Workbook.__getitem__` — see Open Questions.

---

## `_header_columns(sheet: Worksheet, required: tuple[str, ...]) -> dict[str, int]` — lines 66-78

Reads row 1 of `sheet` (`sheet[1]`, line 68) and builds a
`{normalized_header_name: 1-based_column_index}` map, then requires every
name in `required` to be present.

- Normalization (line 71): `str(cell.value).strip().lower()`. This is the
  place, and the only place, where header-name case/whitespace tolerance is
  established. `REQUIRED_STUDENT_COLUMNS` / `REQUIRED_SCHEDULE_COLUMNS` in
  `schedule_contract.py` (lines 30-37) are already lower-case, so this
  normalization is what makes header matching case-insensitive end to end.
- Line 69: `if cell.value is None: continue` — a blank header cell is simply
  skipped, not registered under any key. So a required column with a blank
  header cell is indistinguishable from a required column whose header
  column doesn't exist at all; both surface as "missing required column(s)"
  from the `missing` check (lines 73-77).
- **Unenforced case**: duplicate header names (after normalization) are not
  detected. If two columns both normalize to `"student_id"` (e.g.
  `"Student_ID"` and `" student_id "`), line 71 silently overwrites the
  earlier mapping with the later column's index — the earlier physical
  column is dropped with no warning. Nothing in this function or its caller
  checks for that; `missing = [...]` only checks *absence*, not duplication.
- **Callee**: `sheet[1]` — `Worksheet.__getitem__`
  (`openpyxl/worksheet/worksheet.py:275-...`), which for a single-row key
  resolves to row iteration via `iter_rows`/`_cells_by_row`. See the
  `_read_rows` section below for the load-bearing detail of how row width is
  determined; it applies identically here to row 1.
- **Invariant established for callers**: on success, every name in
  `required` is a key in the returned dict, and each value is a valid
  1-based openpyxl column index that was actually observed in row 1 of this
  sheet. On failure, `ScheduleContractError` is raised before any partial
  dict is returned to the caller (the dict is built but never handed back on
  the error path).

---

## `_read_rows(sheet: Worksheet, required: tuple[str, ...]) -> list[dict[str, object]]` — lines 81-89

Resolves headers via `_header_columns` (line 82), then iterates data rows
(`sheet.iter_rows(min_row=2)`, line 84) and extracts one dict per non-blank
row: `{header_name: cell_value}` for every header column seen (not just
`required` ones — line 85 uses `columns.items()`, which is the full header
map, so extra/optional columns present in the sheet are also carried into
each row dict, unused by `required`-driven validation later but not
stripped).

- **Line 85 — the load-bearing indexing assumption**:
  `row_cells[col_index - 1].value`. This assumes every row tuple yielded by
  `iter_rows` has at least `max(col_index for col_index in columns.values())`
  cells, indexed the same way for every row as row 1 was indexed by
  `_header_columns`. Traced into the callee to check this:
  - `load_workbook(..., read_only=True)` (line 118) makes `sheet` a
    `ReadOnlyWorksheet`
    (`.venv/Lib/site-packages/openpyxl/worksheet/_read_only.py:19-33`), whose
    `iter_rows` is `Worksheet.iter_rows`, which for a read-only sheet bottoms
    out in `ReadOnlyWorksheet._cells_by_row` (lines 60-102) and
    `_get_row` (lines 105-127).
  - `_get_row` pads every row to a **uniform width**
    (`row_width = max_col + 1 - min_col`, line 113) *provided* `max_col` is
    truthy when passed in. `max_col` is threaded down from
    `_cells_by_row`'s own `max_col = max_col or self.max_column` (line 69),
    i.e. the **sheet-wide** `self.max_column`.
  - `self.max_column` (`ReadOnlyWorksheet.max_column` property, line
    188-190) comes from `self._max_column`, which is populated in
    `_get_size()` (lines 46-52) from `parser.parse_dimensions()`.
  - `WorkSheetParser.parse_dimensions()`
    (`.venv/Lib/site-packages/openpyxl/worksheet/_reader.py:172-186`) reads
    the `<dimension>` XML element that (in a well-formed xlsx) precedes
    `<sheetData>`, and returns its boundaries. **If that element is absent**
    (parser hits the `<sheetData>` tag first, line 183-185, and `break`s),
    `parse_dimensions()` returns `None`, so `_get_size` leaves
    `_min_column, _min_row, _max_column, _max_row` at their class defaults —
    `_max_column = None` (line 23).
  - When `self.max_column` is `None`, `_cells_by_row`'s `max_col` stays
    falsy, so **`_get_row` falls back to `max_col = row[-1]['column']`**
    (line 112) — i.e. *each row's own last populated cell's column*, not the
    sheet's. Row width then varies row-to-row, based only on that row's own
    trailing content.
  - **Consequence for `_read_rows`**: if a workbook's `<dimension>` tag is
    missing or understates the sheet's real extent (possible from
    hand-edited files, some third-party xlsx writers, or a file whose
    dimension was stripped/corrupted), a data row whose rightmost populated
    cell falls *before* one of the required columns' index will be padded
    only to that shorter width. `row_cells[col_index - 1]` for a
    higher-indexed required column then indexes past the end of that
    particular row's tuple.
  - **This is an assumption this function makes and nothing in this module
    enforces**: `_read_rows` never checks `len(row_cells)` against
    `col_index` before indexing at line 85. The guarantee of uniform,
    header-aligned row width is established *only* if the workbook's
    `<dimension>` XML is present and accurate — a property of the input
    file, not verified or defended against here. `load_workbook` is called
    with no `read_only=False` fallback and no post-hoc
    `calculate_dimension(force=True)` call (worksheet.py:138-143, unused by
    this module) that would recompute true dimensions when the declared ones
    are missing/wrong.
- Blank-row truncation is **not** actually a stop condition — despite the
  module docstring's claim ("A sheet is read until the first fully-blank
  row", lines 20-21), the code does not break out of the loop on a blank
  row; it **skips** any row where every extracted value is `None` or
  whitespace-only (lines 86-87: `if all(...): continue`) and keeps iterating
  through the rest of `sheet.iter_rows(min_row=2)`. So a blank row followed
  by more data rows further down would have the blank row silently dropped
  and the later rows still read — the docstring describes a different
  (stronger, boundary-stopping) behavior than what's implemented. This is a
  documentation/implementation mismatch worth flagging, not just a comment
  detail: it means trailing "notes" content below the real data is only
  safely ignored if every one of its rows also happens to have blank values
  in exactly the required-column positions; a notes row that happens to have
  non-blank text under a header-matching column would be parsed as a data
  row and handed to `validate_students_register` /
  `validate_schedule_rows`, which would then likely raise a contract error
  (wrong types) rather than the row being excluded as "past the data".
- `str(v).strip() == ""` (line 86) stringifies every value before checking
  blankness — a `0` (int) or `False` (bool) or `datetime.time(0,0)` cell
  value is not blank under this check (`str(0) == "0"`, not `""`), so a row
  with an all-zero/all-midnight-but-present set of values is correctly
  treated as non-blank. Only `None` or actual empty/whitespace strings count
  as blank.

---

## `_split_datetime_fields(rows: list[dict[str, object]]) -> None` — lines 92-108

Mutates `rows` in place (no return value; docstring explicitly frames this
as "narrowing", lines 92-100). For each row:
- If `row["lesson_date"]` is a `datetime.datetime`, replaces it with `.date()`
  (line 104).
- If `row["start_time"]` is a `datetime.datetime`, replaces it with `.time()`
  (line 107).

Both checks use `isinstance(x, datetime)` — a value that is already a plain
`datetime.date` or `datetime.time` (openpyxl can return either depending on
the cell's number format) is left untouched, and a `start_time` cell that
comes back as a bare `date` (no time component) is *deliberately* left as a
`date` (module docstring lines 98-100) so that `schedule_contract`'s
`isinstance(value, time)` check (schedule_contract.py:177) reports it as a
contract violation rather than this function coercing it to midnight and
masking the input error. This is a documented, intentional non-handling of
one case — the caller (`schedule_contract.validate_schedule_rows`) is
depended on to reject it, and it does (line 176-179 there).

- **Assumption this function relies on `_read_rows` to establish**: that
  `row.get("lesson_date")` / `row.get("start_time")` keys exist at all (they
  do — `.get()` is used defensively, so a missing key just yields `None`,
  which fails both `isinstance` checks and is left as `None`, later caught
  by `schedule_contract`'s "missing required value" check).
- Only mutates the two named keys; every other key in each row dict (e.g.
  `duration_minutes`, `status`, `student_id`) passes through this function
  completely untouched — it's not a generic type-coercion pass.

---

## `load_schedule_workbook(source: str | Path | IO[bytes]) -> WorkbookSchedule` — lines 110-138

The module's single public parse/validate entrypoint. Orchestrates, in
strict order:

1. **Line 118**: `load_workbook(source, data_only=True, read_only=True)`.
   - `data_only=True`: openpyxl returns a formula cell's last
     *cached/saved* computed value rather than the formula string. This is
     an external, silent assumption on the input file: if a formula cell was
     never recalculated and saved with a cached value (e.g. programmatically
     written, or opened by a tool that strips cached values), openpyxl
     returns `None` for that cell regardless of what the formula would
     evaluate to. Downstream this surfaces only as "missing required value"
     (schedule_contract.py:111-114/158-161) or a schema-version type error
     (schedule_contract.py:78-82) — never as a distinguishable "stale
     formula" error. Nothing in this module or `schedule_contract` detects
     or reports that distinction.
   - `read_only=True`: selects the `ReadOnlyWorksheet` code path analyzed
     above under `_read_rows`, whose row-width guarantee depends on the
     `<dimension>` XML tag being present/accurate in `source`.
   - No `try/except` wraps this call. Any exception `load_workbook` itself
     raises for a malformed/corrupt/non-xlsx `source` (e.g.
     `zipfile.BadZipFile`, `openpyxl.utils.exceptions.InvalidFileException`,
     or others) propagates **unwrapped** — i.e. **not** as
     `ScheduleContractError`. Every caller of `load_schedule_workbook` that
     only catches `ScheduleContractError` (per this function's own
     docstring, lines 111-113, which promises `ScheduleContractError` "on
     the first violation found") would not catch a malformed-file error
     here. This is a real gap between the docstring's promise and what the
     code guarantees — the docstring's list of violation kinds ("unsupported
     schema version, a missing sheet or column, or any row-level violation")
     does not include "not a valid xlsx file at all", and indeed that case
     is not translated to `ScheduleContractError`.
2. **Lines 120-121**: `legend = _require_sheet(workbook, LEGEND_SHEET)` then
   `version = validate_schema_version(legend[SCHEMA_VERSION_CELL].value)`.
   - `legend[SCHEMA_VERSION_CELL]` (`"B2"`) — single-cell access via
     `Worksheet.__getitem__`, which per its own docstring
     (`openpyxl/worksheet/worksheet.py:282`) "will always be created if
     [it does] not exist" — so a workbook whose Legend sheet has no B2 cell
     at all yields a cell with `.value is None`, not an exception, and
     `validate_schema_version(None)` raises `ScheduleContractError`
     (schedule_contract.py:78-82: `not isinstance(None, int)` is true) —
     covered, not a gap.
   - **Callee**: `validate_schema_version`
     (`schedule_contract.py:72-88`). Every path either raises
     `ScheduleContractError` or returns an `int` known to be in
     `SUPPORTED_SCHEDULE_VERSIONS`. Explicitly rejects `bool` (line 78:
     `isinstance(version, bool)` — Python `bool` is a subclass of `int`,
     openpyxl could plausibly return a `bool`-typed cell value for a
     TRUE/FALSE-formatted cell, and this guards against `True`/`False` being
     silently treated as schema version `1`/`0`). No path in this callee
     returns without validating both "is an int" and "is a supported
     version" — confirmed by reading all lines 72-88; there is no early
     return before the version check.
3. **Lines 123-128**: reads and validates the Students sheet.
   - `_read_rows(students_sheet, REQUIRED_STUDENT_COLUMNS)` — as analyzed
     above, subject to the row-width caveat.
   - Lines 125-127: for every row where `student_id` is not `None`,
     overwrite it with `str(...).strip()`. This pre-normalization exists
     because openpyxl can return a `student_id` cell as a non-string type
     (e.g. an `int` if the cell was numeric-formatted) — stringifying here
     means `validate_students_register`'s `str(row["student_id"]).strip()`
     (schedule_contract.py:116) is redundant-but-consistent with what was
     already done. Rows where `student_id` **is** `None` are left as `None`
     going into `validate_students_register`, which independently requires
     it to be non-`None`/non-blank (schedule_contract.py:109-114) before
     ever reading it as a string (line 116) — so the `is not None` guard
     here doesn't skip validation, it only skips a redundant `str(None)` →
     `"None"` stringification that would otherwise corrupt a legitimately-
     missing id into the literal text `"None"` before the "missing required
     value" check ever saw it. This ordering (normalize-if-present here,
     detect-if-missing in the callee) is load-bearing: swapping it (always
     stringifying, even `None`) would turn a missing `student_id` into the
     string `"None"`, defeating the callee's blank-check.
   - **Callee**: `validate_students_register`
     (`schedule_contract.py:96-136`). Walked all paths:
     - Every required column checked for presence/non-blank before any
       further per-row processing (lines 109-114) — raises on first miss,
       `line_number` computed as `enumerate(rows, start=2)` (row 1 assumed
       to be the header, consistent with `_read_rows`'s `min_row=2`).
     - `rate_cents` type/sign checked explicitly (lines 117-122),
       rejecting `bool` and negative values.
     - Duplicate `student_id` checked (lines 123-127) **after** the
       blank/type checks for that row, so a duplicate row that is *also*
       missing a required column fails on the missing-column message first
       (order matters only for which error message a caller sees, not for
       whether an error is raised at all — no path allows a duplicate id
       through).
     - Returns `register: dict[str, StudentRegisterEntry]` — every value
       constructed from validated fields (line 129-135); there is no code
       path that inserts a partially-validated entry.
   - **Invariant load_schedule_workbook depends on**: every key in
     `register` is a `student_id` that appeared, well-formed and unique, in
     the Students sheet. This is what makes the later
     `to_schedule_snapshot`'s unguarded `workbook_schedule.register[row.student_id]`
     lookups (lines 167) safe — see that function's analysis.
4. **Lines 130-136**: reads and validates the Schedule sheet, same
   `student_id`-stringify-if-present pattern (lines 132-134), then
   `_split_datetime_fields` (line 135), then:
   - **Callee**: `validate_schedule_rows(schedule_rows, register)`
     (`schedule_contract.py:139-210`). Walked all paths:
     - Required-column blank check first (lines 156-161), same pattern as
       the register validator.
     - `student_id in register` check (lines 163-167) — this is the
       **only** place `student_id` resolvability against the just-built
       `register` is enforced; it depends on `register` being the complete,
       correct dict `validate_students_register` returned. There is no
       fallback or best-effort match (module docstring of
       `schedule_contract.py`, lines 14-16, states this is deliberate —
       "There is no fallback: an unresolvable id is a contract violation").
     - `lesson_date` / `start_time` type checks (lines 169-179) via
       `isinstance(..., date)` / `isinstance(..., time)` — this is exactly
       the shape `_split_datetime_fields` was responsible for narrowing
       upstream; if that narrowing were skipped or buggy, a `datetime`
       value (not `date`/`time`) would fail these `isinstance` checks (a
       `datetime` is not an instance of plain `date`... actually it *is*,
       since `datetime` subclasses `date` — worth noting precisely: `lines
       171` `isinstance(lesson_date, date)` would pass even for an
       un-narrowed `datetime.datetime`, since `datetime` **is-a** `date`.
       So `_split_datetime_fields`'s narrowing of `lesson_date` is not
       load-bearing for correctness of this particular `isinstance` check —
       it would pass either way — but it **is** load-bearing for
       `ScheduleRow.lesson_date`'s claimed type (`date`, not `datetime`) and
       for any equality/comparison logic downstream that assumes a bare
       `date` (e.g. dict/set keying by `(student_id, lesson_date)`
       elsewhere in the pipeline, not analyzed here). For `start_time`,
       narrowing **is** load-bearing for the check itself:
       `datetime.time` is **not** a subclass of `datetime.datetime`, so an
       un-narrowed `datetime` value in `start_time` would fail
       `isinstance(start_time, time)` (line 177) and correctly raise —
       matching the module docstring's stated design (workbook.py
       lines 98-100) that a wrong-shaped `start_time` should surface as a
       contract error, not be silently coerced.
     - `duration_minutes` positive-int check (lines 181-190), rejecting
       `bool` and non-positive values.
     - `status` normalized (`str(...).strip().upper()`, line 192) then
       checked against `VALID_STATUSES = {"Y", "YI", "N"}` (line 194,
       schedule_contract.py:28) — note this normalizes case/whitespace
       *for the check and for the stored value*, so `"y "` or `"yi"` are
       accepted and stored upper-cased; `workbook.py` does no
       pre-normalization of `status` itself (only `student_id` is
       pre-stripped at line 134), so all status normalization is this
       callee's responsibility alone.
     - `assert isinstance(lesson_date, date)` / `assert isinstance(start_time, time)`
       (lines 199-200) — redundant with the `_require` checks just above
       them (which already raised if false); these asserts are
       unreachable-if-false given the preceding `_require` calls, present
       only to narrow the static type for the `ScheduleRow(...)`
       constructor call, not a live runtime guard. (Would be bypassed
       entirely if run with `python -O`, but nothing above them would have
       let a failing case reach this point anyway, so that's not a gap in
       this callee's validation.)
   - Returns `list[ScheduleRow]` where every element's `student_id` is a
     confirmed key of `register`.
5. **Line 138**: constructs and returns `WorkbookSchedule(schema_version=version, register=register, lessons=lessons)`. This is the **only** construction site for `WorkbookSchedule` in this module (see that dataclass's own section above) — reached only after every one of the above validations has succeeded without raising.

**Net invariant this function establishes for all its callers**: a returned
`WorkbookSchedule` has `schema_version` in `SUPPORTED_SCHEMA_VERSIONS`, a
`register` with unique, well-typed entries, and `lessons` where every
`student_id` is present as a key in `register` — *provided* the input
`source` file has correct `<dimension>` XML (or no sheet whose data extends
past a mis-declared/absent dimension) and up-to-date cached formula values.
Those two provided-that conditions are properties of the input file this
module does not itself verify.

---

## `to_schedule_snapshot(workbook_schedule: WorkbookSchedule) -> ScheduleSnapshot` — lines 141-173

Pure transform, no I/O, no mutation of its argument. Converts an
already-validated `WorkbookSchedule` into the `ScheduleSnapshot` DTO shape
(`invoicing/providers/base.py:53-55`) shared with `GoogleSheetProvider`'s
output, so `sync_schedule_into_db` (not in this file) can treat both sources
uniformly (module docstring, lines 143-151).

- **Students list comprehension (lines 152-163)**:
  - `first_name=entry.name.split()[0] if entry.name.split() else entry.name`
    (line 155) — defends against `entry.name.split()` being empty (an
    all-whitespace name), falling back to the raw (whitespace) `entry.name`
    string as `first_name` in that case. But `entry.name` is a
    `StudentRegisterEntry.name`, which by the time it reaches here has
    already passed `schedule_contract.validate_students_register`'s
    non-blank check (schedule_contract.py:109-114) *and* been
    `.strip()`-ed (schedule_contract.py:131). A stripped, non-blank string
    can still be all-whitespace-*internally* only if... actually a
    `.strip()`-ed non-blank string cannot be empty or whitespace-only by
    definition (strip removes only leading/trailing whitespace; if
    non-blank survived the `_require` check pre-strip, post-strip it's
    still non-empty and has no leading/trailing whitespace, though it could
    still be something like a single non-whitespace, non-splittable
    character — `.split()` on any non-empty, non-whitespace-only string
    always yields a non-empty list). So the `if entry.name.split() else
    entry.name` fallback branch is **dead code** given the invariant
    `validate_students_register` establishes on `name` — a defensive check
    for a case its own callee already precludes. Confirmed by reading
    schedule_contract.py:109-114 (blank check) and :131
    (`name=str(row["name"]).strip()`) together: `entry.name` here is always
    a stripped, non-empty string, so `.split()` on it is always non-empty.
  - `last_name=" ".join(entry.name.split()[1:])` (line 156) — for a
    single-word name, this is `""` (empty string), not an error; no
    validation anywhere requires `last_name` to be non-empty. Silent,
    accepted behavior, not a defended-against edge case.
  - `billing_type="Private"` (line 157) — hardcoded literal for every
    workbook-sourced student, regardless of any workbook content; there is
    no column or field in the workbook schema for billing type at all
    (`REQUIRED_STUDENT_COLUMNS`, schedule_contract.py:30, has none). This
    is an unconditional, source-level assumption baked into this converter,
    not derived from validated input.
- **Lessons list comprehension (lines 164-172)**:
  - `student_first_name=workbook_schedule.register[row.student_id].name.split()[0]`
    (line 167) — an **unguarded** dict lookup (`register[row.student_id]`,
    not `.get(...)`) followed by an **unguarded** `.split()[0]` index.
    - The dict lookup's safety depends entirely on the invariant
      `load_schedule_workbook` established: every `ScheduleRow.student_id`
      is a validated key of `register` (schedule_contract.py:163-167, as
      analyzed above). This function receives only `WorkbookSchedule`
      instances, and the *only* place one is constructed is
      `load_schedule_workbook` line 138 (see that section) — so as long as
      every caller obtains its `WorkbookSchedule` from
      `load_schedule_workbook`, this lookup cannot `KeyError`. If any other
      code path ever constructs a `WorkbookSchedule` directly with an
      inconsistent `register`/`lessons` pair (nothing in the type system
      prevents that — the dataclass has no `__post_init__` validation, see
      the `WorkbookSchedule` section above), this lookup would raise
      `KeyError` uncaught.
    - The `.split()[0]` index's safety rests on the same
      already-established `name` invariant discussed above (non-empty,
      stripped) — not dead code in quite the same way as line 155 (there's
      no defensive `if` guard here at all; this one would raise
      `IndexError` if `name.split()` were ever empty, whereas line 155
      defends against exactly that for the `first_name` case). This is an
      **asymmetry**: the students-list comprehension defends against an
      empty-split name (redundantly, per above), but the lessons-list
      comprehension performs the identical `.split()[0]` operation
      *without* that defense. Both rest on the same upstream invariant, so
      neither is currently reachable with a failing input *given that
      invariant holds* — but the inconsistency itself is structural: two
      call sites deriving `first_name` from the same `entry.name` use
      different defensiveness.
  - `lesson_date=row.lesson_date`, `status=row.status` — passthrough of
    already-validated `ScheduleRow` fields, no transformation.
- **Return**: `ScheduleSnapshot(students=students, lessons=lessons)` — a
  pydantic `BaseModel` (`invoicing/providers/base.py:53-55`). Constructing a
  pydantic model re-validates field types against the model's own
  annotations (pydantic's standard behavior) — so even though this function
  trusts `workbook_schedule`'s contents, the DTO construction itself is a
  second, independent type-check boundary via pydantic, for both
  `SheetStudentRecord` (base.py:21-38) and `SheetLessonRecord` (base.py:41-50).
  Not traced further here (pydantic internals out of scope for this file's
  analysis) but worth noting as a structural fact: this function's outputs
  are pydantic-validated on construction, not merely asserted correct by
  this module's own logic.

---

## Open questions

- Does `ReadOnlyWorkbook.__getitem__` (the wrapper actually produced by
  `load_workbook(..., read_only=True)`) behave identically to
  `Workbook.__getitem__` (`openpyxl/workbook/workbook.py:277-287`) for a
  missing-sheet lookup, i.e. does it also raise plain `KeyError`? Not
  independently traced; `_require_sheet`'s `except KeyError` (line 62)
  assumes so.
- Is there any caller of `load_schedule_workbook` (outside this file) that
  wraps the call in a bare `except ScheduleContractError` and would
  therefore let a malformed-file exception from `load_workbook` (e.g.
  `zipfile.BadZipFile`) propagate uncaught? Not checked here — would need
  the call sites (e.g. an ingestion/CLI entrypoint) to answer.
- How commonly, in this system's actual input path (however
  `templates/lesson_schedule.xlsx`-shaped files reach `source`), would a
  workbook lack a correct `<dimension>` XML element? If the only files ever
  fed to this module are produced/re-saved by openpyxl or Excel itself, the
  `_read_rows` row-width assumption (see that section) always holds in
  practice; if arbitrary user-uploaded `.xlsx` files reach this path, it
  does not. Not determined from this file alone — depends on the caller
  supplying `source`.
- Does anything downstream of `to_schedule_snapshot` (e.g.
  `sync_schedule_into_db`, not in this file) rely on `last_name` being
  non-empty for single-word names (line 156), given that this function
  produces `""` in that case without error?
