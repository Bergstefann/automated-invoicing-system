# `src/invoicing/seed.py` and `data/seed_synthetic.py`

## Scope and reachability

Both files are dev/test-only tooling, not part of the production (`--real`) code path.

- `seed_database` (src/invoicing/seed.py:86) is called from four places in `src/invoicing/cli.py`
  (lines 93, 110, 154, 254), and in every case only inside an `if demo:` branch.
- `_require_explicit_mode` (src/invoicing/cli.py:49, referenced from seed.py's module docstring
  context) forces every CLI subcommand that can seed to be invoked with either `--demo` or `--real`,
  never neither, and errors if both are passed (cli.py:53-57). There is no code path in cli.py where
  `seed_database` runs without the caller having explicitly typed `--demo`.
- `data/seed_synthetic.py` is a standalone script (`python data/seed_synthetic.py [path]`) that
  imports `seed_database` directly and always calls it (seed_synthetic.py:25), with no mode gating
  at all — but it is a top-level script under `data/`, not something imported or executed by the
  package or CLI; it is a manual/local tool for producing an inspectable demo DB file.
- Nothing in `invoicing.providers.google` (the real Google Sheets/Docs/Gmail integration) or the
  `--real` path imports or reaches `seed.py`. The module docstring itself frames the data as
  "fabricated" fixture data (seed.py:1-5).

Given this, the invariants below matter for correctness of the demo/test fixture and for the
reliability of anything (docs, onboarding, CI) that depends on the demo DB having a specific shape —
not for production data integrity.

## `seed_database(db: Database) -> None` (seed.py:86-143)

### Structure
1. Early-return guard: `if not db.is_empty(): return` (seed.py:87-88).
2. Seeds a deterministic `random.Random(SEED)` PRNG (seed.py:90, `SEED = 20260202` at seed.py:32).
3. Inserts 15 parents (seed.py:93-98), then 25 students (one per `FIRST_NAMES` entry, seed.py:101-114).
4. For each of `TOTAL_PERIODS` (=6) periods (seed.py:116-143):
   - computes bounds via `period_bounds(TERM_START, period_number)` (billing.py:25-31)
   - gets/creates the `BillingPeriod` row via `db.get_or_create_period` (db.py:344-362)
   - for each student, ~85% chance (`rng.random() > 0.85` skips, so ~15% skip / ~85% get a lesson)
     of inserting one `Lesson` with a randomly chosen date offset and attendance status
     (seed.py:121-140)
   - if `period_number <= FULLY_PROCESSED_PERIODS` (=2), calls `_bill_and_email_period` to
     synthesize already-invoiced state for that period (seed.py:142-143).

### Invariants relied upon / established
- **Idempotency via `is_empty()`**: the whole function assumes calling it twice is a no-op the
  second time, guarded solely by `db.is_empty()` (seed.py:87), which is defined as
  `COUNT(*) FROM students == 0` (db.py:178-180). This checks only the `students` table. If some
  other seeding path ever inserted parents/lessons/invoices without inserting students, or if a
  caller inserts a student directly before seeding, `is_empty()` would report false-empty/non-empty
  inconsistently with the other tables' actual contents — nothing in seed.py cross-checks that
  parents/lessons/invoices are also empty.
- **Determinism**: the demo dataset's exact shape (which students get lessons, which attendance
  status, which dates) depends on `random.Random(SEED)` (seed.py:90) being called in exactly the
  same sequence every run — 15 `rng.choice` calls for parents, then per-student `rng.choice`/`rng.choice`/`rng.choice`
  for instrument/rate/school, then per-period per-student `rng.random()`/`rng.choice()`/`rng.random()`.
  Any change to the loop structure or the order of `rng` calls silently changes the entire
  downstream dataset (no test asserts specific output values were checked, so this is an assumption
  about the module's own stability across edits, not something enforced by seed.py itself).
- **Non-null IDs after insert**: `parent.id`, `student.id`, `period.id` are asserted non-None right
  after insertion (seed.py:103, 119, 124) rather than checked with a real error. This assumes
  `db.insert_parent`/`db.insert_student`/`db.get_or_create_period` always populate `.id` from
  `cur.lastrowid`. Confirmed by db.py:189 (`model_copy(update={"id": cur.lastrowid})`) and
  db.py:357-362 (`BillingPeriod(id=cur.lastrowid, ...)`) — `lastrowid` is guaranteed non-None here
  because these are single-row `INSERT` statements on tables with `INTEGER PRIMARY KEY AUTOINCREMENT`,
  so the assumption holds structurally, but only because of that constant relationship; nothing in
  seed.py itself would catch a future change to `insert_parent`/`insert_student` that stopped setting
  `id`. Using `assert` also means these checks are compiled out entirely when Python is run with
  `-O`, at which point a None id would instead surface later as a less obvious type/DB error.
- **`period_bounds` never raises here**: `period_bounds(TERM_START, period_number)` (seed.py:117)
  raises `ValueError` only if `period_number < 1` (billing.py:27-28); the loop's `range(1, TOTAL_PERIODS + 1)`
  (seed.py:116) never produces such a value, so this path is dead in practice but not statically
  excluded — a future edit to the loop bounds could resurrect it.
- **Rate/duration model constraints**: `Student.rate_cents` requires `ge=0` (models.py:68) and
  `Lesson.duration_minutes` requires `gt=0` (models.py:77), both enforced by Pydantic at model
  construction. `seed_database` always uses `rng.choice(RATE_OPTIONS_CENTS)` (all positive,
  seed.py:82) and a hardcoded `duration_minutes=30` (seed.py:137), so these validators never reject
  seed data — but they are the actual enforcement point, not anything in seed.py.
- **`UNIQUE (student_id, lesson_date)` on `lessons`** (db.py:74) and **`UNIQUE (name, parent_id)` on
  `students`** (db.py:42): seed_database relies on never generating a duplicate `(student, lesson_date)`
  pair within a period, since `insert_lesson` (db.py:280-296) does a raw `INSERT` with no
  get-or-create semantics and would raise `sqlite3.IntegrityError` on a UNIQUE violation. Because
  each student gets at most one lesson per period (the per-period loop body runs at most once per
  student, seed.py:121-140) and `DAY_OFFSETS` values are within one period's date range, and periods
  don't overlap (`period_bounds` is contiguous, non-overlapping — billing.py:26,29-31), no student
  can get two lessons on the same date across the whole seeding run. This is an emergent property of
  the loop structure, not a checked invariant — a future change to allow multiple lessons per
  student per period could hit the UNIQUE constraint (a crash, not silent corruption, since
  `insert_lesson` doesn't catch it).
- **Parent uniqueness by email**: `email=f"parent{i+1}@example.com"` for `i in range(15)`
  (seed.py:97) is guaranteed unique per call since `i` is unique and the table has `UNIQUE(email)`
  (db.py:30). Only 15 parents are ever inserted per seeding run, and `is_empty()` prevents a second
  run from attempting to insert again, so the UNIQUE constraint is never actually exercised as a
  failure path here.

### `_bill_and_email_period(db, bounds, period_id)` (seed.py:146-186)

Synthesizes fully-processed (billed + emailed) invoices for a period directly against the DB,
bypassing `Doc`/`Email` provider interfaces, per its own docstring (seed.py:147-149).

- Pulls `lessons_in_range(bounds.start_date, bounds.end_date)` (db.py:322-328) — an inclusive range
  query — then filters to unbilled ones and groups by student via `group_by_student(unbilled_lessons_in_period(...))`
  (billing.py:46-51, 54-60).
  - `unbilled_lessons_in_period` depends on `Lesson.is_unbilled` (models.py:92-93): billable
    (`ATTENDED`), `billed_invoice_id is None`, and `not pre_billed`. Since seed-created lessons
    always have `billed_invoice_id=None` and `pre_billed=False` by construction (seed.py:133-140
    never sets either), every `ATTENDED` lesson in-period is picked up. This is consistent because
    `_bill_and_email_period` is only ever called once per period (seed.py:142-143, before any other
    billing touches these lessons).
  - **Caller depends on `lessons_in_range` performing an inclusive `>=`/`<=` comparison on
    ISO-format date strings** (db.py:324) matching `bounds.start_date`/`end_date` exactly, and on
    `bounds` from `period_bounds` being closed inclusive `[start, end]` (billing.py:26,30-31) —
    consistent, but the coupling is implicit (nothing asserts the two callees agree on inclusivity
    other than both being written that way).
- For each `(student_id, student_lessons)` pair, sorted by `student_id` (seed.py:156, `sorted(grouped.items())`
  — deterministic ordering, matters for `count_invoices_issued_on` sequencing below):
  - `db.get_student(student_id)` (db.py:230-234) — raises `KeyError` if the student row is missing.
    Caller assumes every `student_id` present in `grouped` (derived from lessons just inserted by
    `seed_database` for real students, seed.py:121-140) refers to a student that exists — true here
    since lessons are only ever inserted with `student.id` values obtained from `db.insert_student`
    in the same run (seed.py:104-114, 135).
  - `compute_totals(student, student_lessons)` (billing.py:70-83): `subtotal_cents = rate_cents * len(lessons)`,
    `gst_cents` hardcoded to 0 (billing.py:78, documented as a disclosed simplification carried from
    the original system, not a bug in this function). Caller relies on this to build `Invoice.subtotal_cents`/`gst_cents`/`total_cents`,
    all of which have `ge=0` Pydantic validators (models.py:117-119) — satisfied since `rate_cents >= 0`
    and lesson count `>= 0`.
  - `next_invoice_number(issued_on, db.count_invoices_issued_on(issued_on))` (invoice_numbers.py:20-24):
    - **Read-then-use race**: `count_invoices_issued_on` (db.py:451-456) is a separate `SELECT COUNT(*)`
      query, and its result is used to compute the next sequence number, then a separate
      `db.insert_invoice` (db.py:375-395) call inserts the row. There is no transaction spanning the
      count-read and the insert-write, and no unique constraint tying invoice_number generation to
      atomicity beyond the `UNIQUE(invoice_number)` column constraint (db.py:54) which would only
      surface a collision as a crash, not prevent it. Within `_bill_and_email_period` itself this is
      safe because the loop is single-threaded and sequential (each iteration's insert is committed,
      db.py:394, before the next iteration's count query, seed.py:156-183), but the function does not
      itself guarantee non-concurrent access to the `Database` object — that's an assumption about the
      caller's environment, not enforced here.
    - `next_invoice_number` raises `ValueError` if the daily sequence would exceed 99
      (invoice_numbers.py:22-23). For seed data (2 periods × up to 25 students = at most 50 invoices,
      all with `issued_on` derived from period end dates that differ by 14 days per period,
      seed.py:153) this can never be hit — well under 99 per day and periods don't share `issued_on`.
  - `db.insert_invoice(...)` builds the `Invoice` with `doc_url=f"seed-doc-{invoice_number}"` (seed.py:170)
    and `emailed_at=issued_at` (seed.py:171) — i.e., the invoice is marked emailed at construction
    time, not via a separate `mark_invoice_emailed` call. This means `invoices_pending_email`
    (db.py:415-420, filters `WHERE emailed_at IS NULL`) will never return these seed invoices,
    which is the intended effect (periods 1-2 are meant to look fully processed, per seed.py:8-10)
    but is achieved by direct field assignment, not by exercising the same code path a real run
    would use (`mark_invoice_emailed`, db.py:408-413) — consistent with the function's documented
    intent to bypass provider interfaces (seed.py:147-149), but also means this fixture path doesn't
    exercise `mark_invoice_emailed`.
    - `Invoice.issued_at`/`emailed_at` require timezone-aware datetimes (models.py:123-131,
      `_require_tz_aware`). `issued_at` is built via `datetime.combine(issued_on, datetime.min.time(), tzinfo=UTC)`
      (seed.py:154), which is tz-aware, so both fields pass validation.
  - `assert invoice.id is not None` (seed.py:174) — same pattern/caveat as above: relies on
    `insert_invoice`'s `cur.lastrowid` always being set (db.py:395), true for a single-row INSERT on
    an autoincrement PK, but unchecked by a real conditional.
  - Builds `InvoiceLine` per lesson with `assert lesson.id is not None` (seed.py:177) — relies on
    every `Lesson` object in `student_lessons` (sourced from `db.lessons_in_range`, seed.py:150,
    which always populates `id` from the DB row via `_lesson_from_row`, db.py:476-485) having a
    real id. Holds structurally since these lessons were read back from the DB, not freshly
    constructed in memory.
  - `db.insert_invoice_lines(lines_to_insert)` (db.py:397-406) — no uniqueness constraint on
    `invoice_lines` beyond its own PK, so no collision risk here.
  - `db.mark_lessons_billed([...], invoice.id)` (seed.py:184-186, db.py:336-341): sets
    `billed_invoice_id` for each lesson id. Filters `if lesson.id is not None` (seed.py:185) —
    defensive but redundant given the `assert lesson.id is not None` two lines above already
    guarantees this for every element of `student_lessons`.

## `data/seed_synthetic.py`

### `main() -> None` (seed_synthetic.py:22-27)
- Reads `sys.argv[1]` as an optional DB path, defaulting to `"demo.db"` (seed_synthetic.py:23) —
  no validation of the argument (e.g., could be any string, including one containing path traversal
  segments); this is a local CLI convenience script, not a network-facing entrypoint, and the
  resulting `Path` is handed straight to `Database(db_path)` (db.py:137-138) which opens/creates a
  SQLite file at that path via `sqlite3.connect`.
- `sys.path.insert(0, ...)` (seed_synthetic.py:16) prepends the sibling `src/` directory so the
  script can import `invoicing.*` without the package being installed — assumes the script's own
  file location (`Path(__file__).resolve().parent.parent / "src"`) is `<repo-root>/src`, i.e. that
  this script stays at `<repo-root>/data/seed_synthetic.py`. Moving either directory breaks the
  import silently at `ModuleNotFoundError` time (not a security-relevant assumption, but a
  structural fragility worth noting for context).
- Delegates all logic to `seed_database(db)` (seed_synthetic.py:25) inside a `with Database(db_path) as db:`
  block — `Database.__exit__` always calls `self.close()` (db.py:150-151) regardless of whether
  `seed_database` raised, so the connection is cleaned up on any exit path. No error handling around
  `seed_database` itself — an exception (e.g., an `assert` failure, a `ValueError` from
  `next_invoice_number`, a `sqlite3.IntegrityError`) propagates straight out of `main()` and out of
  the script with a traceback; there is no partial-seed rollback beyond whatever SQLite auto-commits
  already happened per statement (each `Database` method calls `self.conn.commit()` individually,
  e.g. db.py:188, 227, 295, 341 — so a mid-run failure can leave a partially-seeded, non-empty DB
  that would then make future `is_empty()` checks (db.py:178-180) report "not empty" and skip
  re-seeding, even though seeding didn't complete).

## Open questions
- Whether any test suite pins exact expected values derived from the `SEED=20260202` PRNG sequence
  (e.g., specific student names/rates/attendance patterns) is unclear from this file alone; if so,
  the ordering-sensitivity noted above (any reordering of `rng.*` calls changes all downstream draws)
  would be a test-fragility point, not inspected here — need to check the test directory.
  All test-related conclusions are out of scope for this pass but are unclear; need to inspect
  `tests/` for any assertions tied to seeded values.
  Also unclear: whether `Database` is ever shared across threads/processes in any tooling that
  wraps `seed_database`, which would make the non-atomic count-then-insert pattern in
  `_bill_and_email_period` (seed.py:159-160) relevant; nothing in the two files under analysis
  suggests concurrent use, but it isn't ruled out by anything here either.
