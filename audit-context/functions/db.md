# `src/invoicing/db.py` — persistence layer

File-level role: sole SQLite access point for the pipeline. Wraps a single
`sqlite3.Connection`, owns schema creation/migration, and exposes one method
per query/mutation used by the rest of the codebase (`cli.py`, sync/billing
modules not read in this pass). No ORM, no query builder — every statement
is a literal string in this file.

## Credential / connection-parameter sourcing

`Database.__init__` (line 137-142) takes only a filesystem `path`
(`str | Path`) and calls `sqlite3.connect(self.path, detect_types=0)`
(line 139). There are no credentials in this file — SQLite is a local file,
not a networked/authenticated store, so "credentials" here reduce to "which
file". That file path comes from `Settings.db_path`
(`src/invoicing/config.py:19,65`), which is sourced from the environment
variable `INVOICING_DB_PATH` (default `"invoicing.db"`, relative to CWD) via
`Settings.from_env` (`config.py:47-76`), or hardcoded to `"demo.db"` in
`Settings.for_demo` (`config.py:79-80`). `Database` itself does not validate,
canonicalize, or sandbox this path — whatever string arrives is handed
straight to `sqlite3.connect`. All four call sites are in `cli.py` (lines 91,
108, 152, 252), all as `with Database(settings.db_path) as db:`.

`self.conn.row_factory = sqlite3.Row` (line 140) and
`PRAGMA foreign_keys = ON` (line 141) are set once per connection, before
`create_schema()` runs. Every downstream row-parsing helper
(`_parent_from_row`, `_student_from_row`, etc.) depends on `sqlite3.Row`
column-name access (`row["id"]`, etc.) — this pragma is the invariant that
makes those helpers valid; nothing re-asserts it later, but nothing else in
the file changes `row_factory` either, so it holds for the connection's
whole lifetime.

`PRAGMA foreign_keys = ON` is the enforcement point for every
`REFERENCES` constraint declared in `SCHEMA` (parents/students/invoices/
lessons/invoice_lines FKs, lines 37, 55-57, 68, 79-80). If this pragma were
absent or failed silently, SQLite would accept orphan foreign keys with no
error — the whole referential-integrity story for this module rests on this
one line executing successfully before any insert.

## SQL construction

Every query in this file is a static string literal with `?` placeholders
bound via the DB-API parameter tuple (e.g. lines 184-187, 192, 209-220,
252, 306-309, 322-327, 347-349, 376-393, 400-403, 416-419, 423-424,
437, 444-448, 453-455). No f-string / `.format()` / `%`-interpolation is
used to build a WHERE/VALUES clause from caller-supplied data anywhere in
the file — confirmed by grep-level read of every `conn.execute(...)` call.

Two exceptions to "fully static SQL text," both structural, not
data-driven:
- `list_students` (line 272-277): the base query string is conditionally
  extended with a literal `" WHERE active = 1"` fragment based on a
  boolean parameter (`active_only`), not a value — no user-controlled
  string enters the query text itself.
- `list_invoices` (line 442-449): similarly branches between two fully
  literal query strings depending on whether `period_id is None`; the id
  itself is always passed as a bound parameter, never spliced in.
- `count_invoices_issued_on` (line 451-456): builds `prefix = day.strftime("%d%m%y")` from a `date` object (line 452) and uses it as a bound
  `LIKE` parameter (`f"{prefix}%"`, line 454) — the `%` wildcard suffix is
  appended in Python before binding, but the value itself is still passed
  through the parameter slot, not concatenated into the SQL text. Since
  `day` is a `date`, `strftime` output is constrained to digits by
  construction; this is not attacker-controlled string interpolation.
- `create_schema`/`_migrate` (lines 163, 175): `f"PRAGMA user_version = {SCHEMA_VERSION}"` / `f"PRAGMA user_version = {version}"` — these are
  f-strings building SQL text, but `SCHEMA_VERSION` (line 96, literal `2`)
  and `version` (drawn only from the hardcoded `MIGRATIONS` list, line
  130-133) are both file-internal constants, never derived from caller
  input, DB content, or environment. `PRAGMA` also does not accept `?`
  parameter substitution in sqlite3, which is presumably why these are
  built this way rather than parameterized.

`SCHEMA` (lines 26-83) and the `ALTER TABLE` statements in
`_add_pre_billed_column` / `_add_student_id_column` (lines 102, 115) are
static strings with no interpolation at all.

**Invariant**: no method in `Database` accepts a raw SQL fragment, table
name, or column name from a caller. The only place a computed string
reaches SQL as anything other than a bound parameter is the two
`PRAGMA user_version` f-strings, both fed exclusively by module-internal
integers.

## Schema / migration lifecycle

`create_schema()` (lines 153-167) is called unconditionally from
`__init__` (line 142) on every `Database(...)` construction — there is no
opt-out.

- Line 154-157: checks `sqlite_master` for a `lessons` table to decide
  `already_existed`. This is a proxy for "the whole schema already
  existed," not a per-table check — if a partially-created DB existed
  (e.g. `lessons` present but `invoice_lines` missing, which normal use
  can't produce but a corrupted/hand-edited file could), `already_existed`
  would be `True` and no migration-repair beyond the two `MIGRATIONS`
  steps would run for missing tables; `CREATE TABLE IF NOT EXISTS` in the
  subsequent `executescript(SCHEMA)` (line 159) would still create any
  genuinely absent tables, so this is more a note on migration scope than
  a broken invariant.
- Line 159: `executescript(SCHEMA)` always runs (both fresh and existing
  DB), relying on `IF NOT EXISTS` to make it a no-op against existing
  tables. Comment at lines 85-95 explicitly documents that
  `CREATE TABLE IF NOT EXISTS` cannot add columns to a pre-existing table
  — this is the documented gap `MIGRATIONS` exists to cover.
- Fresh DB path (line 162-165): stamps `user_version` straight to
  `SCHEMA_VERSION` (2) without running any migration function, on the
  reasoning that a schema just built from current `SCHEMA` already has
  everything migrations would add. **Assumption**: every entry in
  `MIGRATIONS` is idempotent-safe to skip for a fresh DB, i.e. `SCHEMA`
  itself already reflects the end-state each migration function produces.
  This holds by inspection — `SCHEMA` already declares `pre_billed`
  (line 73) and the fresh-DB path in `insert_student` always sets
  `student_id` (line 223-226) — but nothing in code cross-checks `SCHEMA`
  against `MIGRATIONS` content; if a future migration added a column not
  mirrored in `SCHEMA`, a fresh DB would silently miss it forever, since
  it's stamped to `SCHEMA_VERSION` and no migration would run against it
  afterward (`_migrate` only runs when `already_existed`, line 167).
- Existing-DB path (line 167 → `_migrate`, lines 169-176): reads
  `PRAGMA user_version` (line 170, defaults to 0 for any DB never
  stamped), then for each `(version, migrate)` in `MIGRATIONS` in list
  order, skips if `version <= current_version` (line 172), else runs the
  migration and immediately stamps+commits that version (lines 174-176).
  Each migration function independently re-checks actual table shape
  before acting (`PRAGMA table_info`, lines 100, 113) rather than trusting
  `user_version` alone — comment at 125-129 explains this guards against a
  DB that acquired the column before the versioning scheme existed. This
  makes each migration function idempotent against being re-run, which is
  the safety net if `user_version` and actual schema ever disagree.
- **Unenforced assumption**: `MIGRATIONS` list order (line 130-133) must
  match ascending version order and each version number must be unique —
  nothing validates this list's structure; a manually-reordered or
  duplicate-version entry would change which migrations run/skip silently.
  Currently correct (1, 2) by inspection only.
- Commit granularity: `executescript(SCHEMA)` + commit (line 159-160) is
  one transaction boundary; each migration's mutation + version stamp is
  committed separately per migration (line 176) — so a crash between two
  migrations leaves the DB at a valid intermediate version, re-resumable
  on next open. A crash *inside* one migration function's own statements
  (e.g. `_add_student_id_column`'s `ALTER TABLE` + `executemany`, lines
  115-122) before the version-stamp commit would leave the ALTER applied
  but `user_version` unbumped — on next open, `_add_student_id_column`
  runs again; its `ALTER TABLE ... ADD COLUMN` (line 115) is guarded by
  the `if "student_id" not in columns` check (line 114) so it won't
  re-add, but the backfill UPDATE (lines 116-122) re-running is harmless
  since it's `WHERE student_id IS NULL` and idempotent for already-set
  rows.

## Method-by-method

### `__init__` (137-142)
Establishes: `self.path`, `self.conn` (row_factory=Row, FK enforcement on),
schema present. No exception handling around `sqlite3.connect` — a bad path
(unwritable dir, permissions) propagates `sqlite3.OperationalError` to the
caller uncaught.

### `close` / `__enter__` / `__exit__` (144-151)
Standard context-manager wiring. `__exit__` ignores `exc_info` and always
closes — no rollback of an in-flight uncommitted transaction is
performed explicitly, but since every mutating method in this class calls
`self.conn.commit()` itself immediately after its own statement(s) (see
below), there should be no uncommitted state outstanding at `__exit__` time
under normal use of the public API.

### `create_schema` / `_migrate` — see above.

### `is_empty` (178-180)
`SELECT COUNT(*) FROM students` — treats "no students" as "empty DB" as a
proxy for the whole database being unseeded; doesn't check other tables.
Used presumably by CLI seeding logic (not in this file) to decide whether
to run demo-seed / initial sync.

### `insert_parent` (183-189)
Depends on `parents.email` UNIQUE constraint (line 30) for correctness of
`get_or_create_parent`'s dedup logic — but `insert_parent` itself does not
check for an existing email first; a direct call with a duplicate email
raises `sqlite3.IntegrityError` uncaught. Returns a new `Parent` via
`model_copy` with `id=cur.lastrowid` (line 189) — trusts `lastrowid` is the
id of the row just inserted, valid because this connection/cursor pair is
not shared across threads/concurrent writers here (single-connection,
synchronous, no threading in this file).

### `get_parent` (191-195)
Raises `KeyError` on missing id (line 193-194) rather than returning
`None` — differs from `get_period`, which returns `None` (line 368). This
asymmetry is a caller-facing contract difference worth noting: callers of
`get_parent`/`get_student`/`get_lesson`/`get_invoice` must be prepared to
catch `KeyError`; callers of `get_period` check for `None`.

### `get_or_create_parent` (197-201)
Matches on `email` only (line 198). Depends on `insert_parent`'s reliance
on the UNIQUE(email) constraint — if two concurrent calls raced past the
`SELECT` before either `INSERT` committed, the second `insert_parent`
would raise `IntegrityError` from the UNIQUE violation rather than
returning the first caller's row; this file is not written for concurrent
writers (single sqlite3.Connection, no locking primitives visible), so
this is a documented-by-omission single-writer assumption.

### `insert_student` (204-228)
Mints `student_id` (`f"S-{new_id:04d}"`, line 223) from the row's own
freshly-assigned `id` (`cur.lastrowid`, line 221, asserted non-None line
222) — comment (205-208) explicitly states this is the only place
`student_id` is minted and that the incoming `student.student_id` field is
ignored on the way in (confirmed: the INSERT column list at lines 210-211
does not include `student_id`). Two statements (INSERT then UPDATE, lines
209-226) committed together (line 227) — not wrapped in an explicit
transaction beyond SQLite's own default connection-level transaction, so a
crash between the two would leave a student row with `student_id IS NULL`
until `_add_student_id_column`-style backfill logic (not otherwise present
in this file) reconciles it. `Student.rate_cents` is validated `ge=0` at
the Pydantic model layer (`models.py:68`) before this method ever sees it,
provided callers construct `Student` via the pydantic model — `Database`
itself performs no further bounds-checking on the values it inserts.

### `get_student` (230-234)
Same `KeyError`-on-missing pattern as `get_parent`.

### `get_or_create_student` (236-270)
Matches existing student on `name` alone (line 252), deliberately not
`(name, parent_id)` — extensive comment (239-251) documents this was
changed after a real double-billing incident caused by parent-email
aliasing forking duplicate parent rows and, under the old
`(name, parent_id)` match, duplicate student rows. Depends on
`get_or_create_parent` upstream (called by the caller of this method, not
here) to supply a `parent_id` that may differ from the student's existing
one; if it differs, this method repoints the existing student row
in-place (lines 255-260) rather than creating a new one, and explicitly
leaves the old parent row unreferenced rather than deleting it (comment
line 249-250) — an orphaned-but-intentional row, not a dangling-reference
bug given FK constraints only require the *referenced* row to exist, not
that every parent be referenced.
**Unenforced-by-this-method invariant**: relies on `students.name` being
a meaningful stable identity for the caller's domain (i.e., no two
distinct real students share an exact name string) — nothing in the
schema enforces name uniqueness (only `UNIQUE(name, parent_id)`, line 42,
which is weaker), so this is a domain assumption, not a DB-enforced one.

### `list_students` (272-277)
String concatenation of a literal WHERE clause based on a bool, not
user data (see SQL construction section above). Result ordered by `id`.

### `insert_lesson` (280-296)
No existence check for `student_id` beyond the FK constraint (line 68) —
relies on `PRAGMA foreign_keys = ON` (line 141) to reject inserts
referencing a nonexistent student; if that pragma had failed to apply,
this insert would silently succeed with a dangling reference (no
application-level check backs it up). `UNIQUE(student_id, lesson_date)`
(line 74) is the constraint `get_or_create_lesson` depends on to make its
select-then-insert pattern safe against exact-duplicate lessons for the
same student/day (still racy under concurrent writers, same caveat as
`get_or_create_parent`).

### `get_or_create_lesson` (298-320)
Selects on `(student_id, lesson_date)` (lines 306-309) matching the
UNIQUE constraint exactly — this is the one place in the file where a
select-before-insert dedup key exactly mirrors a DB-level uniqueness
constraint, making the race narrower (though not eliminated) than
`get_or_create_parent`/`get_or_create_student` (which key on a subset of
their respective unique constraints or none at all).

### `lessons_in_range` (322-328)
`lesson_date >= ? AND lesson_date <= ?` on TEXT-stored ISO dates
(`lesson_date.isoformat()` used at write time, e.g. line 288) — depends
on lexicographic string comparison agreeing with date-order for
ISO-8601 `YYYY-MM-DD` strings, which it does by construction; this is an
invariant on the storage format, not enforced by any CHECK constraint in
`SCHEMA` (lesson_date is plain `TEXT NOT NULL`, line 69) — nothing stops
a hypothetical direct write of a non-ISO string from breaking range
queries, but no method in this file constructs a lesson_date any other
way.

### `get_lesson` (330-334)
`KeyError`-on-missing, consistent with `get_parent`/`get_student`.

### `mark_lessons_billed` (336-341)
`executemany` UPDATE keyed by lesson id, no verification that every id in
`lesson_ids` actually exists or belongs to the stated `invoice_id`'s
student — `UPDATE ... WHERE id = ?` against a nonexistent id is simply a
no-op row-count-0 update, not an error; caller (billing logic elsewhere)
is trusted to pass a coherent, pre-validated list. No check that lessons
aren't already billed (`billed_invoice_id IS NOT NULL`) before
overwriting — this method will silently re-point an already-billed lesson
to a new invoice if called with such an id; whether anything upstream
prevents that is outside this file's scope (open question below).

### `get_or_create_period` (344-362)
Matches on `period_number` (UNIQUE, line 47). On the insert path, returns
a `BillingPeriod` built directly from the input parameters plus
`cur.lastrowid` (lines 357-362), not re-read from the DB (unlike most
other insert methods here, which use `model.model_copy`) — behaviorally
equivalent but structurally different from the rest of the file.

### `get_period` (364-368)
Returns `None` on missing, not `KeyError` — see asymmetry note under
`get_parent`.

### `list_periods` (370-372)
Straightforward, ordered by `period_number`.

### `insert_invoice` (375-395)
Inserts an invoice row; depends on FK constraints for `student_id`,
`parent_id`, `period_id` (lines 55-57) to reject orphan references. No
check here that `subtotal_cents + gst_cents == total_cents` or any other
arithmetic relationship between the three money fields — those relations,
if they hold, are enforced by whatever code computes an `Invoice` before
calling this method (not in this file); `Invoice`'s Pydantic validators
only constrain each field `ge=0` individually (`models.py:117-119`) and
require `issued_at`/`emailed_at` to be timezone-aware (`models.py:123-131`)
— no cross-field check exists even at the model layer, so this is purely
a caller-side arithmetic assumption from `db.py`'s perspective.
`invoice_number` UNIQUE (line 54) is relied on by
`count_invoices_issued_on` (below) as a dedup/counting mechanism but not
directly enforced by this method beyond letting SQLite raise
`IntegrityError` on collision.

### `insert_invoice_lines` (397-406)
Loop of individual inserts, single commit at the end (line 405) — so a
constraint failure partway through (e.g. bad `lesson_id` FK) raises after
some lines have already executed within the same uncommitted transaction;
since no commit happened yet, SQLite's implicit transaction should roll
back the whole batch on the connection being left in an aborted state,
but this method does not explicitly `try/except`+rollback — an unhandled
exception here propagates to the caller with the transaction's fate
dependent on default `sqlite3` autocommit/isolation behavior (isolation
level not explicitly configured anywhere in this file — `sqlite3.connect`
called with only `detect_types=0`, line 139 — so Python's default
deferred-transaction behavior applies).

### `mark_invoice_emailed` (408-413)
Single UPDATE keyed by id; no check that the invoice exists or was not
already emailed — same no-op-on-missing-id and silent-overwrite pattern
as `mark_lessons_billed`.

### `invoices_pending_email` (415-420)
`WHERE period_id = ? AND emailed_at IS NULL` — depends on `emailed_at`
being NULL exactly when an invoice truly hasn't been emailed, which holds
only because `mark_invoice_emailed` is the sole writer of that column
(besides `insert_invoice`'s initial NULL/value at creation, line 391) and
nothing else in this file clears it back to NULL once set.

### `invoice_lines_for` (422-434)
Ordered by `id`; no cross-check that returned lines' `lesson_id`s
actually belong to lessons for this invoice's student — relies on
`insert_invoice_lines` callers having built consistent `InvoiceLine`
objects.

### `get_invoice` (436-440)
`KeyError`-on-missing, consistent with `get_parent`/`get_student`/
`get_lesson`.

### `list_invoices` (442-449)
Two static query strings branched on `period_id is None`; id always bound
as parameter when present.

### `count_invoices_issued_on` (451-456)
`invoice_number LIKE ?` with pattern `f"{prefix}%"` where
`prefix = day.strftime("%d%m%y")` (lines 452, 454) — depends on
`invoice_number`'s format elsewhere in the codebase (not in this file)
actually starting with a `DDMMYY` date prefix for this count to mean
anything; this file only consumes that convention, doesn't enforce or
document it further. SQLite's default `LIKE` escape handling means a
`prefix` containing `%` or `_` would act as a wildcard, but `strftime`
output for `%d%m%y` is fixed-width digits, so this can't occur through
this call path.

### Row-mapping helpers (`_parent_from_row` … `_invoice_from_row`, 459-511)
Each assumes the row dict-like object has exactly the columns the
corresponding `SELECT *` produced, matched by name (via `sqlite3.Row`,
depends on `row_factory` invariant set in `__init__`, line 140). Type
coercions: `bool(row["active"])`, `bool(row["pre_billed"])` (472, 484,
truthy-int-to-bool, matches how they were written as `int(...)` on
insert, lines 218, 292), `date.fromisoformat(...)` /
`datetime.fromisoformat(...)` (480, 492-493, 504, 509) assume every
stored date/datetime string is valid ISO format — true as long as writes
only ever go through this file's own `insert_*`/`get_or_create_*`
methods, which always call `.isoformat()` on model-layer `date`/
`datetime` values before binding (e.g. lines 288, 354, 386, 391). A
directly-edited or externally-written DB file with a malformed date
string would raise `ValueError` inside these helpers uncaught.

## Open questions

1. Does any caller of `mark_lessons_billed` (line 336) guard against
   passing lesson ids that are already billed, or does this method's lack
   of a `billed_invoice_id IS NULL` guard in the UPDATE's WHERE clause
   rely entirely on upstream selection logic (not in this file) to only
   ever pass unbilled ids? Need to inspect the billing/sync module that
   calls it.
2. Is `Database` ever constructed from more than one thread/process
   against the same file concurrently (e.g. a scheduled job overlapping a
   manual CLI run)? `sqlite3.connect` here uses default
   `check_same_thread=True` and no WAL/busy-timeout pragma is set — file
   contention behavior under concurrent access is unexamined.
3. Where is `Settings.db_path` (hence the file path passed to
   `sqlite3.connect`, line 139) ultimately validated, if at all — could an
   `INVOICING_DB_PATH` environment value pointing outside the intended
   working directory (e.g. `../../etc/...` or an absolute path) be
   accepted without complaint? `config.py` performs no path
   sanitization; need to check whether `cli.py` or deployment tooling
   constrains this env var's origin/trust level.
4. Is there any explicit `rollback()` call anywhere in the codebase for
   the case where a multi-statement method (e.g.
   `insert_invoice_lines`, `insert_student`) raises partway through,
   or is reliance entirely on sqlite3's implicit per-connection
   transaction/autocommit defaults? Not resolved from this file alone.
5. Confirm whether `MIGRATIONS` (lines 130-133) is exercised by any test
   that constructs a pre-migration-shape DB file, to validate the
   "existing DB below version N" path actually runs `_add_pre_billed_column`/`_add_student_id_column` correctly rather than only ever
   being tested against `already_existed=False`.
