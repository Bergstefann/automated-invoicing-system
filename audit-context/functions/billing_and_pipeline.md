# Function analysis: billing.py and pipeline.py

Scope: `src/invoicing/billing.py` (pure domain logic) and `src/invoicing/pipeline.py`
(orchestration: sync, bill, email). Callees followed into `db.py`, `models.py`,
`invoice_numbers.py`, `providers/base.py`, `providers/google.py`, and
`templates/invoice_email.py`.

---

## `period_bounds` in src/invoicing/billing.py (L25-L31)

**Purpose:** Converts a 1-based period number into a concrete `[start_date, end_date]`
window. Every other function that reasons about "a period" (billing, preview, the
CLI) computes its window through this function, so its arithmetic is the sole
definition of period boundaries in the system.

**Inputs & Assumptions:**
- `term_start` (date): trusted — caller-supplied constant, not derived from any
  external record.
- `period_number` (int): trust depends on caller. Precondition `>= 1`, enforced here
  at L27-L28.
- No implicit state, no I/O.

**Outputs & Effects:**
- Returns a frozen `PeriodBounds(period_number, start_date, end_date)`. Pure; no
  writes.
- Postcondition: periods are contiguous and non-overlapping — `end_date` for period
  N is exactly one day before `start_date` for period N+1, since both are computed
  from the same `term_start` and `PERIOD_LENGTH_DAYS` (L29-L30).

**Block-by-Block:**

```python
# L27-L28
if period_number < 1:
    raise ValueError("period_number must be >= 1")
```
- **What:** Rejects non-positive period numbers.
- **Assumes:** nothing upstream already validated this.
- **Establishes:** `period_number >= 1` for the arithmetic below.
- **Depended on by:** L29's offset calculation, which would silently produce a
  window *before* `term_start` for `period_number <= 0` if this guard were absent.

```python
# L29-L30
start = term_start + timedelta(days=(period_number - 1) * PERIOD_LENGTH_DAYS)
end = start + timedelta(days=PERIOD_LENGTH_DAYS - 1)
```
- **What:** Computes a 14-day inclusive window.
- **Establishes:** the "back-to-back, no gaps" invariant described in the docstring,
  contingent on every caller always using the same `term_start` for a given term —
  nothing enforces that two calls agree on `term_start` (see pipeline callers below).

**Cross-Function Dependencies:**
- Callers: `preview_period` (pipeline.py L132), `bill_period` (pipeline.py L198),
  `run_period`'s dry-run branch (pipeline.py L378). All three take `term_start` as a
  parameter passed through from the CLI; nothing in this file or `pipeline.py`
  persists or checks that value against what was used for previously-billed periods.
  If a caller ever supplies a different `term_start` for the same `period_number` on
  a later run, `bounds.start_date`/`end_date` silently shift — `get_or_create_period`
  (db.py L344) then trusts whichever bounds are passed *first* and returns the old
  ones from cache thereafter (see that function's analysis).
- No shared mutable state.

**Open Questions:**
- unclear; need to inspect the CLI (`cli.py`) to see whether `term_start` is derived
  from a stored/config value the same way on every invocation, or is a free
  parameter an operator can vary run-to-run.

---

## `period_number_for_date` in src/invoicing/billing.py (L34-L38)

**Purpose:** Inverse of `period_bounds` — maps a calendar date to the period it
falls in. Not called anywhere in `billing.py` or `pipeline.py` itself (grep below);
kept for symmetry / external callers (e.g. CLI or tests).

**Inputs & Assumptions:**
- `term_start`, `lesson_date` (date): trust depends on caller.
- Precondition: `lesson_date >= term_start`, enforced at L36-L37.

**Outputs & Effects:**
- Returns an int `>= 1`. Pure.

**Block-by-Block:**
```python
# L36-L37
if lesson_date < term_start:
    raise ValueError("date is before the term start")
```
- **What:** Rejects dates before the term.
- **Establishes:** the subtraction at L38 is non-negative, so floor division yields
  a period index `>= 0`, hence a returned period number `>= 1`.

**Cross-Function Dependencies:**
- No internal callers found in `billing.py`/`pipeline.py` (confirmed by inspection
  of both files — the only calls to `period_bounds`-family functions are to
  `period_bounds` itself, at pipeline.py L132/198/378).
- Boundary agreement with `period_bounds`: a `lesson_date` exactly on
  `bounds.end_date + 1` of period N maps to period N+1 here (docstring: "lands in
  exactly one period, the one it opens"), consistent with `period_bounds`'
  back-to-back construction.

**Open Questions:**
- unclear; need to inspect callers outside this file (CLI, tests) to see how this
  function is actually used, since it is unreferenced within the two files audited.

---

## `is_period_completed` in src/invoicing/billing.py (L41-L43)

**Purpose:** Gate that prevents billing a period before it has fully elapsed —
the single check standing between `bill_period` and billing lessons that might
still be added or corrected within an in-progress period.

**Inputs & Assumptions:**
- `period_end` (date), `today` (date). `today` is caller-supplied, not
  `date.today()` internally — see `bill_period` at pipeline.py L197, which derives
  it from the `now: datetime` parameter, itself caller-supplied. Nothing in this
  function or its caller pins `now` to the real wall clock; a caller can pass any
  `datetime`.

**Outputs & Effects:**
- Returns `period_end < today` (strict). A period whose `end_date == today` is
  **not** yet completed under this check — it completes the day after it ends.

**Cross-Function Dependencies:**
- Caller `bill_period` (pipeline.py L199-L200): raises `ValueError` if this returns
  `False`, blocking the entire billing phase. This is the only enforcement point for
  "don't bill an in-progress period" — nothing in `db.py` or `models.py` re-checks
  it, so any other code path calling `db.insert_invoice` directly would bypass it
  entirely (none currently does in these two files).

**Open Questions:** none.

---

## `unbilled_lessons_in_period` in src/invoicing/billing.py (L46-L51)

**Purpose:** Filters a lesson list down to the ones that both fall inside a period
window and are still billable — the core idempotency filter that makes re-running
`bill_period` for an already-billed period a no-op.

**Inputs & Assumptions:**
- `lessons` (list[Lesson]): trusted to already be scoped to a reasonable date range
  by the caller (both callers pre-filter via `db.lessons_in_range`), though this
  function re-checks the date bounds itself (L50) regardless of what the caller
  passed, so it does not actually *depend* on the caller's pre-filtering for
  correctness — only for efficiency.
- `bounds` (PeriodBounds): trusted, typically freshly produced by `period_bounds`.
- Relies on `Lesson.is_unbilled` (models.py L91-93) for the "still billable" half of
  the predicate.

**Outputs & Effects:** Pure filter/list comprehension; no writes.

**Cross-Function Dependencies:**
- Callee `Lesson.is_unbilled` (models.py L91-93): `is_billable and
  billed_invoice_id is None and not pre_billed`. Three conditions, all must hold.
  This is where the two independent "already billed" representations
  (`billed_invoice_id` set by `mark_lessons_billed`, vs. `pre_billed` set once at
  sync time from the legacy Sheet's `YI` status) are unified. A lesson can be
  billable, have no `billed_invoice_id`, yet still be excluded via `pre_billed` —
  this is intentional (see `sync_schedule_into_db` L105-108) but means
  `unbilled_lessons_in_period`'s notion of "unbilled" is not simply "no invoice
  row exists for it" from the `invoices`/`invoice_lines` tables' point of view.
- Callers: `preview_period` (read-only) and `bill_period` (mutates). Both call
  `group_by_student` on the result immediately after.

**Open Questions:** none.

---

## `group_by_student` in src/invoicing/billing.py (L54-L60)

**Purpose:** Buckets a flat lesson list by `student_id` and sorts each bucket
chronologically, so downstream code (invoice line generation, email rendering) sees
lessons for one student in date order.

**Inputs & Assumptions:**
- `lessons` (list[Lesson]): no assumption about pre-sorting; this function sorts
  internally (L59) so callers need not.

**Outputs & Effects:**
- Returns a plain `dict[int, list[Lesson]]` (converted from `defaultdict` at L60,
  so callers cannot trigger accidental key creation via `.get`/indexing misuse
  downstream). Pure.

**Cross-Function Dependencies:**
- Callers `preview_period` (L134) and `bill_period` (L206) both then do
  `sorted(grouped.items())` — sorting by `student_id` (int) — giving a deterministic
  processing order across students. Combined with this function's per-student date
  sort, the two together fix the order invoices are created in and the order lesson
  lines appear within an invoice.

**Open Questions:** none.

---

## `compute_totals` in src/invoicing/billing.py (L70-L83)

**Purpose:** Computes subtotal/GST/total in cents for one student's lesson batch.
GST is hardcoded to zero — explicitly documented as carried-forward behavior from
the original system, not an oversight (L71-76).

**Inputs & Assumptions:**
- `student` (Student): trusted; `rate_cents` assumed non-negative — enforced by
  `Student.rate_cents: int = Field(ge=0)` (models.py L68), so `pydantic` rejects
  construction of a `Student` with a negative rate. This function does not itself
  guard against a negative rate; it relies entirely on the model's field validator.
- `lessons` (list[Lesson]): trust depends on caller; length used directly as a
  multiplier (L77) with no cap.

**Outputs & Effects:**
- Returns `InvoiceTotals(subtotal_cents, gst_cents=0, total_cents)`. Pure; no
  overflow guard, but Python ints are arbitrary precision so this is not a
  correctness concern in-language.

**Cross-Function Dependencies:**
- Callers: `preview_period` (L139), `_build_doc_data` (L168), `bill_period` (L214).
  All three then use `totals.total_cents`/`subtotal_cents` to populate persisted
  `Invoice` rows or rendered documents — `compute_totals` is the single source of
  truth for the money that ends up on an invoice and in the emailed PDF. Note
  `email_pending_invoices` (pipeline.py L300-304) does **not** call
  `compute_totals` again when re-rendering an already-billed invoice for email; it
  reconstructs an `InvoiceTotals` directly from the persisted `invoice.subtotal_cents`
  etc. (see that function's analysis) — so a bug in `compute_totals` at bill time
  becomes baked into the stored row and is not recomputed at send time.

**Open Questions:** none.

---

## `sync_schedule_into_db` in src/invoicing/pipeline.py (L54-L117)

**Purpose:** Pulls the external Sheet snapshot into SQLite, creating/updating
`parents`, `students`, and `lessons` rows, and hands the sheet provider an
identity map it needs later for write-back. This is the only place that
constructs the `student_id`-keyed `identity_map` the Sheet provider depends on
for `mark_lessons_billed`.

**Inputs & Assumptions:**
- `db` (Database): trusted internal component.
- `sheet` (SheetProvider): **external/adversarial from the type's perspective** —
  a `Protocol`, so at runtime this could be `GoogleSheetProvider` or `fake.py`'s
  test double. Its `read_schedule()` result is trusted as-is; no validation of
  `record.rate_cents`, `record.first_name`, etc. beyond what pydantic's
  `SheetStudentRecord`/`SheetLessonRecord` enforce (base.py L21-51 — mostly no
  constraints besides `int`/`str`/`date` typing; `rate_cents: int` has no `ge=0`
  here, unlike `Student.rate_cents`).
- Assumes `billing_type == "Private"` is the exact string the provider uses to mark
  billable students (L57, L76); `GoogleSheetProvider.read_schedule` hardcodes every
  row to `billing_type="Private"` (google.py L212), so with the live provider this
  filter is a no-op — it only meaningfully filters when driven by a source that can
  produce other values (e.g. the workbook-backed test double).

**Outputs & Effects:**
- Writes: parents via `db.get_or_create_parent` (L78), students via
  `db.get_or_create_student` (L81-87), lessons via `db.get_or_create_lesson`
  (L109-115).
- Side effect: `sheet.set_identity_map(identity_map)` (L93) — mutates the
  `SheetProvider` instance's internal state (for `GoogleSheetProvider`, sets
  `self._identity_map`, google.py L233-234) — a precondition for that provider's
  `mark_lessons_billed` to resolve any cell (see `_resolve_lesson_cell` analysis).
- Returns: count of lesson records synced (`synced`, incremented at L116 only when
  a matching `student_id` was found — L97-99 `continue`s otherwise, so `synced`
  undercounts relative to `len(snapshot.lessons)` whenever a lesson references a
  first name not present in `student_ids`, silently).

**Block-by-Block:**

```python
# L75-L91
for record in snapshot.students:
    if record.billing_type != BILLABLE_TYPE:
        continue
    parent = db.get_or_create_parent(record.parent_name, record.parent_email)
    assert parent.id is not None
    display_name = f"{record.first_name} {record.last_name}".strip()
    student = db.get_or_create_student(...)
    assert student.id is not None
    assert student.student_id is not None
    student_ids[record.first_name.lower()] = student.id
    identity_map[student.student_id] = record.first_name.lower()
```
- **What:** Builds the `student_ids` (first-name-lower -> internal id) and
  `identity_map` (register id -> first-name-lower) lookup tables, creating/updating
  DB rows as it goes.
- **Assumes:** `record.first_name` is unique (case-insensitively) among billable
  students for this run — **nothing enforces this**. If two billable students share
  a first name, the second overwrites `student_ids[first_name.lower()]` for the
  first (plain dict assignment, L90), so every subsequent lesson lookup by that
  first name (L97) resolves to the *second* student regardless of which one it
  actually belongs to. This is exactly the "two students sharing a first name"
  ambiguity that `GoogleDocProvider`'s sibling function `_parse_blocked_schedule`
  explicitly raises `ValueError` for (google.py L339-347) — but that ambiguity
  check lives in the Sheet-grid parser, not here; this loop over
  `snapshot.students` (the *Student Config* tab, a different sheet range,
  google.py L191-217) has no equivalent check.
- **Assumes:** `db.get_or_create_student` matches on `name` alone (see its
  analysis) — so if `display_name` collides across two different `(first_name,
  last_name)` pairs that happen to render the same string, they resolve to one
  student row.
- **Establishes:** `student_ids` and `identity_map` are consistent with each other
  keyed the same way — depended on by the lesson loop below and by
  `sheet.set_identity_map`.
- **Depended on by:** L93 (`set_identity_map`), L97 (`student_ids.get`).

```python
# L96-L116
for lesson_record in snapshot.lessons:
    student_id = student_ids.get(lesson_record.student_first_name.lower())
    if student_id is None:
        continue
    status = (
        AttendanceStatus.ATTENDED
        if lesson_record.status in RAW_STATUS_ATTENDED
        else AttendanceStatus.ABSENT_UNNOTIFIED
    )
    db.get_or_create_lesson(
        student_id=student_id, ..., pre_billed=lesson_record.status == SheetStatus.INVOICED.value,
    )
    synced += 1
```
- **What:** Joins each lesson record to a student purely by lowercased first name,
  derives attendance from a small allow-list of raw status codes, and marks
  `pre_billed` for legacy `"YI"` rows.
- **Why here:** After the student loop, so `student_ids` is fully populated first.
- **Assumes:** any `status` value not in `{"Y", "YI"}` (`RAW_STATUS_ATTENDED`) means
  absent-unnotified (L100-104) — this silently folds unrecognized/garbage status
  strings (e.g. a typo, or the `"N"` not-billable code) into
  `ABSENT_UNNOTIFIED` rather than surfacing them; there is no explicit handling for
  `SheetStatus.NOT_BILLABLE` ("N") as distinct from a genuine absence.
- **Assumes:** `db.get_or_create_lesson`'s uniqueness key `(student_id,
  lesson_date)` (db.py L307-308, SCHEMA `UNIQUE (student_id, lesson_date)` L74) is
  a correct 1:1 model of "one lesson per student per day" — if a student has two
  lessons on the same date, only the first-synced one is retained (the get-half of
  get_or_create returns the existing row unchanged; see that function's analysis)
  and the second is silently dropped, not counted as an error.
- **Establishes:** for each successfully joined lesson, a `Lesson` row exists with
  correct `pre_billed`/`attendance_status`.
- **Depended on by:** every later read via `db.lessons_in_range`, i.e.
  `preview_period` and `bill_period`.

**Cross-Function Dependencies:**
- Callee `db.get_or_create_parent` (db.py L197-201): matches on **`email`** only
  (L198). If `record.parent_email` is blank/empty for two different families
  (plausible from a hand-maintained sheet), they collapse into one `Parent` row —
  read further below at `db.get_or_create_parent`'s own analysis.
- Callee `db.get_or_create_student` (db.py L236-270): matches on **`name`** alone,
  and re-points `parent_id` on the existing row if it differs — this function's
  docstring (db.py L239-251) explicitly documents this as a deliberate fix for a
  prior double-billing bug, but it means a name collision between two genuinely
  different students (e.g. two unrelated "Alex Smith"s) merges their billing
  histories permanently, with no reconciliation.
- Callee `db.get_or_create_lesson` (db.py L298-320): idempotent per
  `(student_id, lesson_date)`; see block analysis above for the duplicate-lesson
  case.
- Callee `sheet.set_identity_map` (Protocol, base.py L78-87): for
  `GoogleSheetProvider`, a plain attribute assignment (google.py L233-234) — no
  validation that `identity_map` values actually correspond to keys present in
  `self._cell_index` (built earlier by `read_schedule`, google.py L230). If
  `identity_map` and `_cell_index` were built from *different* sheet reads (they
  are not, here — `read_schedule` is called once per `sync_schedule_into_db`
  invocation, implicitly via `snapshot = sheet.read_schedule()` at L66), lookups
  in `mark_lessons_billed` would silently miss.
- **Coupling / ordering precondition:** `GoogleSheetProvider`'s own docstring
  (google.py L165-172) states `read_schedule` must be called, then
  `set_identity_map`, before `mark_lessons_billed` can do anything — this ordering
  is established by `sync_schedule_into_db` running to completion before
  `bill_period` is called, which is a caller-level (CLI) ordering guarantee, not
  something enforced inside either function. Nothing prevents a caller from
  invoking `bill_period` with a `sheet` object that never had `sync_schedule_into_db`
  run against it (e.g. a fresh `GoogleSheetProvider` instance) — in that case
  `_identity_map` is `{}` (google.py L178) and every `mark_lessons_billed` call
  silently no-ops with a logged warning (google.py L242-249), never raising.

**Open Questions:**
- unclear; need to inspect `providers/fake.py` to see whether the test double's
  `mark_lessons_billed`/`set_identity_map` share the same silent-miss-on-mismatch
  behavior or diverge from `GoogleSheetProvider`.
- unclear; need to inspect how `record.parent_email` is validated/required upstream
  (Student Config tab parsing, google.py L191-217) — L214 assigns
  `padded[1].strip()` with no non-empty check, so a blank parent email is possible
  and would flow into `get_or_create_parent`'s email-keyed matching.

---

## `preview_period` in src/invoicing/pipeline.py (L128-L148)

**Purpose:** Read-only projection of what `bill_period` would do for a period,
without any writes — explicitly documented as never mutating state (L131).

**Inputs & Assumptions:**
- `db`, `term_start`, `period_number`: same as `bill_period`'s equivalents.
- Does **not** call `is_period_completed` — unlike `bill_period`, this function
  will preview an in-progress period without error. This is a real divergence in
  behavior between preview and actual billing, not obviously a bug but worth
  noting as an asymmetry: an operator previewing a period that has not finished
  sees only the lessons synced so far, with no indication the period is incomplete.

**Outputs & Effects:**
- Returns `(bounds, list[InvoicePreviewLine])`. No DB writes — confirmed by
  inspection: only `db.lessons_in_range` (read) and `db.get_student` (read) are
  called.

**Cross-Function Dependencies:**
- Same `unbilled_lessons_in_period` / `group_by_student` / `compute_totals` chain
  as `bill_period`, so its notion of "unbilled" is identical (including the
  `pre_billed` quirk noted above).
- Callee `db.get_student` (db.py L230-234): raises `KeyError` if the student id
  isn't found. Since `student_id` here always comes from a `Lesson.student_id` that
  was itself read from the `lessons` table (L133), and `lessons.student_id`
  references `students(id)` via a `REFERENCES` constraint enforced only when
  `PRAGMA foreign_keys = ON` is set (db.py L141, set at connection time) — so under
  normal operation this lookup cannot miss. No exception handling here regardless.

**Open Questions:** none.

---

## `_build_doc_data` in src/invoicing/pipeline.py (L151-L179)

**Purpose:** Assembles the `InvoiceDocData` DTO consumed by `DocProvider.create_invoice_doc`
— translates domain objects (`Student`, `Lesson`) plus a pre-minted invoice number
into the exact shape the Doc-creation provider expects.

**Inputs & Assumptions:**
- `student`, `lessons`: trusted, already validated/queried by the caller
  (`bill_period`).
- `parent_name`, `parent_email` (str): passed through verbatim; `parent_name` falls
  back to `student.name` if falsy (L170) — no equivalent fallback for
  `parent_email`, which is passed straight into `InvoiceDocData.parent_email` even
  if empty.
- `invoice_number`, `today`: caller-supplied, not derived here.

**Outputs & Effects:**
- Returns an `InvoiceDocData`; no I/O, no writes. Calls `compute_totals` internally
  (L168), duplicating the total computed by the caller's own `compute_totals` call
  at pipeline.py L214 for the *same* `student`/`student_lessons` pair — two
  identical computations, not reconciled or asserted equal, feeding two different
  destinations (the Doc, and the persisted `Invoice` row). Since the underlying
  function is pure and deterministic, they cannot diverge given the same inputs —
  but if `student_lessons` supplied to each call ever differed (they do not
  currently — see `bill_period` L214/218), the doc and the DB row could disagree.

**Cross-Function Dependencies:**
- Callee `compute_totals`: see its own analysis. GST is always zero, so the doc
  always shows `$0.00` GST — consistent with the persisted invoice.

**Open Questions:** none.

---

## `bill_period` in src/invoicing/pipeline.py (L182-L258)

**Purpose:** Phase 1 of the pipeline — creates invoices (+ Doc + invoice_lines)
for every currently-unbilled attended lesson group in a period and marks those
lessons billed, both in the DB and (best-effort) back on the Sheet. Documented as
idempotent (L191-195): a second call for the same period creates nothing new
because `unbilled_lessons_in_period` excludes anything with `billed_invoice_id`
set.

**Inputs & Assumptions:**
- `db`, `docs`, `sheet`: `db` trusted/internal; `docs` and `sheet` are `Protocol`
  types — external/adversarial from this function's perspective. `docs` in
  particular is invoked mid-loop (L220) with no try/except — an exception from
  `docs.create_invoice_doc` propagates straight out of `bill_period`, aborting the
  whole loop after any students already processed in this call have already had
  their DB writes committed (each `db.insert_invoice`/`mark_lessons_billed` call
  commits immediately — see `db.py`'s per-call `self.conn.commit()`). So a
  mid-batch Doc-provider failure leaves some students billed (with real `Invoice`
  rows, real `billed_invoice_id` on their lessons) and others in the same run
  untouched — not a rollback, a partial commit. This is consistent with the
  module's documented per-invoice independence for the *email* phase (L9-14) but
  the same independence is not explicitly claimed for the *billing* phase's Doc
  creation step, and a raised exception here means `sheet_refs` accumulated so far
  (L250-253) are never flushed to `sheet.mark_lessons_billed` (L255-256 never
  reached), so the DB and the Sheet can disagree on which lessons are billed even
  for the successfully-committed subset.
- `now` (datetime): caller-supplied; `today = now.date()` (L197) used both for the
  completeness check and for `invoice_number` minting. No enforcement that `now`
  reflects the real clock.
- Precondition (enforced, L199-200): `is_period_completed(bounds.end_date, today)`
  must be true, else raises `ValueError`. This is the only guard against billing
  an in-progress period.

**Outputs & Effects:**
- Writes: `billing_periods` row via `get_or_create_period` (L202); `invoices` rows
  via `insert_invoice` (L222); `invoice_lines` rows via `insert_invoice_lines`
  (L244); `lessons.billed_invoice_id` via `mark_lessons_billed` (L246); external
  Doc creation via `docs.create_invoice_doc` (L220); external Sheet cell writes via
  `sheet.mark_lessons_billed` (L256, only if `sheet_refs` non-empty).
- Returns `(bounds, created: list[Invoice])`.
- Postcondition on full success: every currently-unbilled attended lesson in the
  period now has `billed_invoice_id` set, an `Invoice`+`InvoiceLine` rows exist,
  and (best-effort) the Sheet reflects `YI`.

**Block-by-Block:**

```python
# L202-L203
period = db.get_or_create_period(period_number, bounds.start_date, bounds.end_date)
assert period.id is not None
```
- **What:** Ensures a `billing_periods` row exists for this period.
- **Assumes:** if a row already exists (from a prior call, possibly with different
  `start_date`/`end_date` due to a different `term_start` — see `period_bounds`'
  analysis), `get_or_create_period` returns the **stored** bounds, not the
  newly-computed `bounds` local variable (db.py L347-351 returns `_period_from_row`
  on a hit, ignoring the `start_date`/`end_date` arguments passed in). So `period.id`
  is correct but the in-memory `bounds` used for the rest of this function
  (`bounds.start_date`/`end_date` at L205, and the returned `bounds` at L258) could
  silently diverge from what's stored in `billing_periods` for that `period_number`,
  if `term_start` ever changes between calls. Nothing here detects or guards
  against that divergence.
- **Establishes:** `period.id` valid for use as `Invoice.period_id`.
- **Depended on by:** L227 (`period_id=period.id`).

```python
# L205-L206
lessons = db.lessons_in_range(bounds.start_date, bounds.end_date)
grouped = group_by_student(unbilled_lessons_in_period(lessons, bounds))
```
- **What:** Determines exactly which lessons this call will bill.
- **Assumes:** `db.lessons_in_range` is a plain closed-interval `>=`/`<=` string
  comparison on ISO dates (db.py L322-328) — correct only because `lesson_date`
  is always stored as `date.isoformat()` (db.py L288), which sorts lexicographically
  the same as chronologically.
- **Establishes:** the exact per-student, date-sorted lesson groups the rest of the
  function iterates.

```python
# L211-L253 (loop body, key steps)
for student_id, student_lessons in sorted(grouped.items()):
    student = db.get_student(student_id)
    parent = db.get_parent(student.parent_id)
    totals = compute_totals(student, student_lessons)
    invoice_number = next_invoice_number(today, db.count_invoices_issued_on(today))
    doc_data = _build_doc_data(...)
    doc_id = docs.create_invoice_doc(doc_data)
    invoice = db.insert_invoice(Invoice(...))
    assert invoice.id is not None
    ...
    db.insert_invoice_lines(lines_to_insert)
    lesson_ids = [lesson.id for lesson in student_lessons if lesson.id is not None]
    db.mark_lessons_billed(lesson_ids, invoice.id)
    created.append(invoice)
    sheet_refs.extend(...)
```
- **What:** Per student: mint an invoice number, create the external Doc, persist
  the invoice + lines, mark the lessons billed, queue Sheet refs.
- **Why here:** Order matters for `next_invoice_number`'s uniqueness — see the
  callee analysis below — and for what happens on partial failure (Doc creation is
  the external call, placed before any DB writes for *that* student, so a failure
  for student N doesn't leave a half-written `Invoice` row for N, but does leave
  students `1..N-1` fully committed, as noted above).
- **Assumes:** `next_invoice_number(today, db.count_invoices_issued_on(today))`
  never collides across concurrent processes — see callee analysis; this function
  calls `count_invoices_issued_on` fresh per student, inside the loop, so a
  student's number reflects the true count *at that point in the loop*, correctly
  incrementing (L216, L451-456 in db.py) as long as this process is
  single-threaded and each `insert_invoice` commits before the next
  `count_invoices_issued_on` call — which it does, since `db.insert_invoice`
  commits synchronously (db.py L394) before the loop moves to the next student.
  No such protection exists across two concurrent `bill_period` invocations (e.g.
  two processes) — `count_invoices_issued_on` and the subsequent `insert_invoice`
  are not in a transaction together, so two processes could both read the same
  count and mint the same `invoice_number`, which would then fail on the
  `UNIQUE` constraint on `invoices.invoice_number` (SCHEMA L54) at whichever
  `insert_invoice` runs second — raising `sqlite3.IntegrityError` uncaught here.
- **Assumes:** `lesson.id is not None` for every lesson in `student_lessons` at
  L238; guarded by an `assert` (L238) — every `Lesson` read back from
  `db.lessons_in_range` does have `.id` set (rows always carry their DB-assigned id,
  `_lesson_from_row` db.py L476-485), so this assert should always hold in
  practice, contingent on `lessons_in_range` never returning in-memory-only
  `Lesson` objects.
- **Establishes:** for this student, one committed `Invoice`, N committed
  `InvoiceLine`s, and every one of their `lesson_ids` now has `billed_invoice_id`
  set — this is the state `unbilled_lessons_in_period` checks to make re-runs a
  no-op.
- **Depended on by:** `email_pending_invoices` (reads `invoices` with
  `emailed_at IS NULL`), and the next call to `bill_period`/`preview_period` for
  this period (via `Lesson.is_unbilled`).

```python
# L255-L256
if sheet_refs:
    sheet.mark_lessons_billed(sheet_refs, SheetStatus.INVOICED)
```
- **What:** Best-effort write-back to the Sheet, once, after all DB writes for
  this call have committed.
- **Assumes:** `sheet.mark_lessons_billed` either succeeds for a ref or logs and
  skips it (true for `GoogleSheetProvider`, google.py L236-265 — a lookup miss is
  logged and skipped per-ref, L242-249; but the final `batchUpdate` call at
  L262-265 is a single all-or-nothing API call — if it raises, **every** queued ref
  for this `bill_period` call fails together, including ones that resolved fine),
  but by this point all DB writes have already committed regardless (**established**
  above), so a Sheet write-back failure here does not roll back or retry the DB
  side; it only leaves the Sheet stale until the next `sync_schedule_into_db`
  brings it back in line with reality via `pre_billed`... actually it would not,
  since a lesson billed in the DB but still `Y` on the Sheet would sync back in as
  `ATTENDED`/`pre_billed=False`/no `billed_invoice_id` conflict — `get_or_create_lesson`
  (db.py L306-311) returns the **existing row unchanged** on a re-sync, so the DB's
  `billed_invoice_id` is preserved and `unbilled_lessons_in_period` still correctly
  excludes it. So the Sheet staying at `Y` is cosmetic, not a re-billing risk,
  given `get_or_create_lesson`'s get-half never overwrites.
- **Establishes:** nothing this function's own return value depends on — `created`
  is already finalized by this point (L258 returns `bounds, created` regardless of
  whether this call raises... actually if it raises, it propagates and `created`
  is never returned to the caller at all, even though every invoice in `created`
  is durably committed. The caller only learns about invoices whose Sheet write-back
  didn't throw.)

**Cross-Function Dependencies:**
- Callee `db.get_or_create_period` (db.py L344-362): see block analysis above —
  does not update stored bounds on a cache hit.
- Callee `db.get_student` / `db.get_parent`: raise `KeyError` on miss; uncaught
  here, would abort `bill_period` mid-loop with the same partial-commit
  consequence as a Doc-provider failure.
- Callee `next_invoice_number` (invoice_numbers.py L20-24): raises `ValueError` if
  `already_issued_today + 1 > 99` — i.e. more than 99 invoices billed on the same
  calendar day (`today`, not `now`) will crash `bill_period` on the 100th student
  of that day's loop, after 99 have already committed. Sequence is derived from
  `db.count_invoices_issued_on(today)` (db.py L451-456), a `LIKE` match on
  `invoice_number` prefix `DDMMYY` — collision-safe against *this* process's own
  prior invoices for the day, not against concurrent processes (see above).
- Callee `docs.create_invoice_doc` (Protocol; `GoogleDocProvider.create_invoice_doc`,
  google.py L376-411, external-source-available): copies a Drive template, then
  issues a `batchUpdate` of `replaceAllText` requests built from
  `build_replacements(data)` (not read in this pass — see Open Questions).
  Assumes `settings.doc_template_short_id`/`long_id` are non-`None`
  (`assert template_id is not None`, google.py L385) and
  `settings.drive_output_folder_id` is set; none of that is validated by
  `bill_period` itself, which trusts the `DocProvider` implementation to either
  succeed or raise.
- Callee `sheet.mark_lessons_billed`: see block analysis above and the
  `GoogleSheetProvider.mark_lessons_billed` / `_resolve_lesson_cell` analyses
  (google.py L236-284) — silent per-ref skip on identity/cell-index miss, single
  atomic `batchUpdate` for everything that did resolve.
- Callers: `run_period` (pipeline.py L381-383) — the only caller in this codebase.
  It assumes `bill_period`'s return value `billed` reflects everything durably
  committed even though (per above) an exception partway through would prevent
  `run_period` from ever seeing `billed` for the successfully-committed prefix.

**Open Questions:**
- unclear; need to inspect `templates/invoice_doc.py`'s `build_replacements` to see
  what `InvoiceDocData` fields are trusted verbatim into a Google Docs
  `replaceAllText` request (e.g. whether `parent_name`/`student_display_name`
  containing template-tag-like substrings could interact oddly with the
  replacement mechanism).
- unclear; need to inspect whether any retry/transaction wrapper exists at the
  CLI layer around `bill_period` that would mitigate the partial-commit-on-exception
  behavior documented above, or whether operators are expected to just re-run
  (which, per the idempotency invariant, would be safe for already-committed
  students and pick up where it left off for the rest).
- unclear; need to inspect whether `bill_period` can ever be invoked concurrently
  (e.g. two CLI processes, or a scheduler retry racing a still-running invocation)
  — the `count_invoices_issued_on`/`insert_invoice` race noted above is only live
  under that condition.

---

## `email_pending_invoices` in src/invoicing/pipeline.py (L268-L342)

**Purpose:** Phase 2 — sends every invoice in a period with `emailed_at IS NULL`,
independently, so a failure on one invoice doesn't block others and a re-run
resumes exactly where it left off (rule 7, documented at module level L9-14 and
here L279-284).

**Inputs & Assumptions:**
- `db`, `docs`, `email`: `docs`/`email` are external `Protocol`s.
- `period_id` (int): trusted, expected to be a valid `billing_periods.id` — no
  validation here; `db.invoices_pending_email` (db.py L415-420) simply returns an
  empty list for a nonexistent `period_id`, so a bad id degrades to "nothing to
  send" rather than an error.
- `now` (datetime): used only to stamp `mark_invoice_emailed` (L339); not used for
  any date-completeness check (email phase has no analogue to
  `is_period_completed` — invoices are only created for completed periods by
  `bill_period` in the first place, so this is consistent).

**Outputs & Effects:**
- Writes: `invoices.emailed_at` via `db.mark_invoice_emailed` (L339), **only on the
  success path**, per invoice.
- External interactions: `docs.export_pdf` (L328), `email.send` (L329-334) — both
  inside a `try` (L327).
- Returns `EmailRunResult(sent, failed, skipped_no_contact)` — every invoice in
  `db.invoices_pending_email(period_id)` lands in exactly one of these three lists,
  or is silently absent from all three... check: `skipped_no_contact` on
  `not parent.email` (L292-294, `continue` before entering the try), `failed` on
  exception (L335-337), `sent` on success (L340-341). All three code paths for a
  given invoice are covered; none silently vanish.

**Block-by-Block:**

```python
# L288-L294
for invoice in db.invoices_pending_email(period_id):
    student = db.get_student(invoice.student_id)
    parent = db.get_parent(invoice.parent_id)
    if not parent.email:
        result.skipped_no_contact.append(invoice.invoice_number)
        continue
```
- **What:** Loads the invoice's student/parent, skips invoices whose parent has no
  email.
- **Assumes:** `db.get_student`/`db.get_parent` succeed — both raise `KeyError`
  uncaught here on a missing row (foreign key integrity assumed, as in
  `preview_period`). An exception here would abort the whole loop, **unlike** the
  documented per-invoice independence — this failure mode is not wrapped in the
  `try` at L327, so it is not covered by rule 7's "a failure on one invoice
  doesn't touch the others": a `KeyError` from `get_student`/`get_parent` for one
  invoice aborts processing of every subsequent invoice in this call, including
  ones that would otherwise have sent fine. (Foreign key constraints make this
  unreachable in practice given `PRAGMA foreign_keys = ON`, but the code path
  itself does not treat this failure the same as a doc/email-send failure.)
- **Establishes:** `parent.email` truthy for everything past this point in the
  iteration.
- **Depended on by:** L330 (`to=parent.email`).

```python
# L296-L298
lines = db.invoice_lines_for(invoice.id)
lessons = [db.get_lesson(line.lesson_id) for line in lines]
rate_cents = lines[0].rate_cents if lines else student.rate_cents
```
- **What:** Reconstructs the lesson list and a representative rate for email
  rendering, from persisted `invoice_lines`/`lessons` rather than re-querying
  `bill_period`'s original selection logic.
- **Assumes:** an invoice with zero `invoice_lines` is possible in principle (falls
  back to `student.rate_cents`, L298) — but `bill_period` never creates an invoice
  without at least one lesson (`grouped.items()` only iterates students with
  non-empty `student_lessons`, since `group_by_student` only produces keys for
  lessons present in its input, and `unbilled_lessons_in_period` would have to
  return a non-empty list for that student_id to appear in `grouped` at all,
  transitively via `defaultdict` only gaining a key when appended to). So the
  empty-lines fallback here is defensive but currently unreachable via
  `bill_period`'s own invoice-creation path; it would only matter for an
  invoice created through some other write path not present in these two files.
- **Assumes:** `db.get_lesson(line.lesson_id)` succeeds for every line — same
  `KeyError`-uncaught pattern as above, and again not covered by the `try` block
  at L327 (this code is *before* the try), so a missing lesson row here aborts the
  whole email run, not just this invoice.
- **Establishes:** `lessons`, `rate_cents` used for HTML rendering below.

```python
# L300-L318
totals = InvoiceTotals(subtotal_cents=invoice.subtotal_cents, ...)
resolved_message = personal_message.replace("<student>", student.name.split()[0]).replace(
    "<parent>", (parent.name or student.name).split()[0]
)
html_body = render_invoice_email(...)
```
- **What:** Reconstructs `InvoiceTotals` from the **persisted invoice row**, not by
  recalling `compute_totals` — so if `compute_totals`'s logic ever changed between
  when an invoice was billed and when it's later emailed (e.g. a code deploy in
  between two runs), the email reflects the totals as billed, not as would be
  computed today. This is consistent with financial correctness (email must match
  what was actually invoiced) but is a real divergence point to note: totals shown
  in the email trace back to `invoices.subtotal_cents`/`gst_cents`/`total_cents`
  (db.py, written once at L222-234 in `bill_period`), never recomputed.
- **Assumes:** `student.name.split()[0]` and `(parent.name or
  student.name).split()[0]` do not raise on an empty/whitespace-only name —
  `str.split()` on an empty string returns `[]`, so `[0]` would raise `IndexError`
  if `student.name` were empty; `Student.name: str` has no `min_length` constraint
  in `models.py` L65, so an empty name is representable and would crash this call
  (uncaught, before the `try` block, aborting the whole run for all remaining
  invoices too).
- **Establishes:** `html_body`, the full email content, including the escaped
  personal message (see `render_invoice_email`/`_build_message_html` below).

```python
# L327-L337
try:
    pdf_bytes = docs.export_pdf(invoice.doc_url)
    email.send(
        to=parent.email,
        subject=...,
        html_body=html_body,
        attachment=EmailAttachment(filename=pdf_filename, content=pdf_bytes),
    )
except Exception:
    result.failed.append(invoice.invoice_number)
    continue
db.mark_invoice_emailed(invoice.id, now)
result.sent.append(invoice.invoice_number)
```
- **What:** The only genuinely per-invoice-isolated section — a bare
  `except Exception` (L335) catches anything from `export_pdf` or `email.send`,
  recording failure and moving on without aborting the loop.
- **Why here:** Explicitly documented (L322-326) as treating PDF export and send
  as one failure unit, so a transient Drive or Gmail error degrades to "retry next
  run" rather than crashing the batch.
- **Assumes:** `invoice.doc_url is not None` — enforced immediately above by
  `assert invoice.doc_url is not None` (L320), **outside** the try block, so a
  `None` `doc_url` raises `AssertionError` which propagates uncaught and aborts the
  whole run (not caught by L335's `except Exception` since the assert is before
  the `try`). `doc_url` is `Invoice.doc_url: str | None` (models.py L120) — every
  invoice created by `bill_period` sets it from `docs.create_invoice_doc`'s return
  value (pipeline.py L232, always a `str`, per `DocProvider.create_invoice_doc`'s
  contract at base.py L117-119), so under the current single write path this
  assert should always hold; it is a documented invariant, not a live check.
- **Assumes:** `email.send`'s bare `except Exception` also swallows programming
  errors inside `docs.export_pdf`/`email.send` implementations indistinguishably
  from genuine transient failures (e.g. a malformed `EmailAttachment` would also
  be silently recorded as `failed` rather than surfaced) — this is a structural
  fact about the catch being untyped, not scoped to network/IO exceptions.
- **Establishes:** on success, `invoices.emailed_at` set — the fact
  `db.invoices_pending_email` checks on any future call for this period, making
  re-runs skip already-sent invoices.

**Cross-Function Dependencies:**
- Callee `db.invoices_pending_email` (db.py L415-420): simple `WHERE period_id = ?
  AND emailed_at IS NULL` — this is the entire retry/resume mechanism; it has no
  notion of "currently being sent by another process," so two concurrent calls to
  `email_pending_invoices` for the same `period_id` would both select the same
  pending invoices and could both send the same email twice (no locking/claiming
  step between the `SELECT` and the eventual `mark_invoice_emailed` `UPDATE`).
- Callee `render_invoice_email` (templates/invoice_email.py L35-84): pure
  templating; escapes `personal_message` line-by-line via `markupsafe.escape`
  before joining with `<br>` (`_build_message_html`, L87-92), so
  operator-supplied `personal_message` is HTML-escaped — the one piece of
  less-trusted input reaching the rendered email. `student_display_name` and other
  fields are passed to Jinja2 with `autoescape=select_autoescape(["html"])`
  (L23-26), so they're auto-escaped by the template engine itself, not manually.
- Callee `docs.export_pdf` (Protocol; `GoogleDocProvider.export_pdf`, google.py
  L413-421, external-source-available): loops `downloader.next_chunk()` until
  `done`; any exception from the Drive API surfaces to the `try` at L327 and is
  caught there.
- Callee `email.send` (Protocol; `GoogleEmailProvider.send`, google.py L437-462,
  external-source-available): builds a MIME message and calls Gmail's
  `messages().send`; `msg["Bcc"] = self._settings.sender_email` (google.py L449)
  means every sent invoice email is also BCC'd to the sender — not visible from
  `pipeline.py` at all, a callee-only detail.
- Callee `db.mark_invoice_emailed`: plain `UPDATE ... SET emailed_at = ?`; no
  `WHERE emailed_at IS NULL` guard, so calling it twice for the same invoice (e.g.
  a race between two concurrent runs, per above) just overwrites the timestamp,
  not detected as a double-send.
- Callers: `run_period` (L390-399), only when `send_emails=True` and after
  `bill_period` has already run in the same call — though `email_pending_invoices`
  itself works over `period_id`, not tied to `bill_period` having just run
  (documented explicitly, module docstring L9-14, as also covering leftover
  invoices from a previous run's failed attempts).

**Open Questions:**
- unclear; need to inspect whether any application-level locking exists around
  `email_pending_invoices` to prevent the concurrent-double-send scenario noted
  above (select-then-later-update race), or whether operators are relied upon to
  never run two instances concurrently.
- unclear; need to inspect `providers/fake.py`'s email/doc implementations to see
  whether tests exercise the `AssertionError`/`IndexError` early-abort paths noted
  above (empty `doc_url`, empty `student.name`).

---

## `run_period` in src/invoicing/pipeline.py (L352-L401)

**Purpose:** Top-level orchestration for one period: optionally dry-run, always
bill (when not dry-run), optionally email. The single entry point that ties
`bill_period` and `email_pending_invoices` together with the `dry_run`/`send_emails`
flags described in the module docstring.

**Inputs & Assumptions:**
- `dry_run` (bool): when true, **guarantees zero writes** (L369-371,
  L377-379) — achieved structurally by returning before calling `bill_period` or
  `email_pending_invoices` at all, not by passing a "dry run" flag down into them.
  This is a real invariant: neither `bill_period` nor `email_pending_invoices` has
  any dry-run parameter of their own, so the guarantee rests entirely on this
  early return (L377-379) and would break if any code were ever added between
  L377 and L381 that performed a write.
- `send_emails` (bool): gates only the second phase; billing always happens when
  `dry_run=False`, per docstring (L371) and code (L381-383 unconditional,
  L387-388 conditional).
- All other params passed straight through to `bill_period`/`email_pending_invoices`.

**Outputs & Effects:**
- Returns `RunResult(bounds, invoices_billed, emails)`.
- On `dry_run=True`: `invoices_billed=[]`, `emails=EmailRunResult()` (all empty
  lists) — a caller cannot distinguish "dry run, nothing to preview" from "dry
  run" generically; `bounds` is still computed and returned via `period_bounds`
  directly (L378), not via `preview_period`, so **no lesson data is fetched or
  shown** — this `dry_run` path is not the same as `preview_period`; it returns
  strictly less information (just the bounds, no per-student breakdown). A caller
  wanting an actual preview must call `preview_period` separately.

**Block-by-Block:**

```python
# L384-L385
period = db.get_period(period_number)
assert period is not None and period.id is not None
```
- **What:** Re-fetches the just-created/existing `billing_periods` row by
  `period_number` after `bill_period` returns.
- **Assumes:** `bill_period` always creates (or already found) this row via its
  own `get_or_create_period` call (L202) before returning — true on every
  successful return path of `bill_period`, since that call happens before the
  per-student loop and unconditionally. If `bill_period` raised partway through
  (per its own analysis above), `run_period` never reaches this line at all — the
  exception propagates first. So this assert should never fire in practice, given
  `bill_period`'s only non-exceptional return path having already created the
  period row.
- **Establishes:** `period.id` valid for `email_pending_invoices(..., period.id,
  ...)`.

**Cross-Function Dependencies:**
- Callee `bill_period`: see its analysis, including the partial-commit-on-exception
  behavior — an exception there propagates out of `run_period` too, uncaught,
  so `RunResult` is never constructed and the caller (CLI) sees a raised exception
  rather than a partial `RunResult`.
- Callee `email_pending_invoices`: see its analysis, including the concurrent-run
  and early-abort caveats — same propagation behavior applies.
- Callers: none within these two files; presumably `cli.py`'s `run` command (not
  read in this pass).

**Open Questions:**
- unclear; need to inspect `cli.py` to see how a `run_period` exception (from
  either phase) is surfaced to an operator, and whether the CLI communicates the
  partial-commit state described in `bill_period`'s analysis when that happens.

---

## Cross-cutting observations

- **Idempotency rests on two independently-necessary conditions**, not one:
  `Lesson.is_unbilled` (models.py L91-93) requires *both* `billed_invoice_id is
  None` *and* `not pre_billed`. `bill_period` only ever sets the former
  (`mark_lessons_billed`, db.py L336-341); `pre_billed` is set once, only at sync
  time (pipeline.py L114), and never updated afterward by anything in these two
  files. The two flags model genuinely different histories (billed by this system
  vs. billed by the legacy pre-rebuild pipeline) and neither function conflates
  them, but a reader of `bill_period` alone would not see that `pre_billed` exists
  as a second gate — it's only visible via `unbilled_lessons_in_period` ->
  `Lesson.is_unbilled`.
- **No transactional boundary spans a whole `bill_period` or
  `email_pending_invoices` call.** Every `Database` method commits individually
  (`self.conn.commit()` at the end of nearly every write method in db.py). This
  makes per-student/per-invoice progress durable immediately, which is what makes
  the documented idempotency/resumability claims true — but it also means there is
  no all-or-nothing guarantee at the level of a single `bill_period` invocation;
  the granularity of atomicity is one student (for billing) or one invoice (for
  emailing), not the whole call.
- **`term_start` is never persisted alongside a billing period.** `billing_periods`
  stores only `period_number`, `start_date`, `end_date` (SCHEMA L45-50) — the
  `term_start` used to derive those bounds on first creation is not recorded
  anywhere queryable, so there is no way, from the DB alone, to detect that a
  later call passed a different `term_start` for the same `period_number` (see
  `period_bounds` and `bill_period`'s `get_or_create_period` analyses).
