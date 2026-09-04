# `src/invoicing/cli.py` — Function Analysis

Typer CLI entry point for the invoicing pipeline. Five commands (`sync`, `preview`, `run`,
`debug-parse-schedule`, `status`) plus five private helpers and a `main` callback. All commands
funnel through the same `Settings` → `Database` → provider-construction → `pipeline.py` call chain;
the CLI's job is almost entirely argument gating and wiring, with the actual domain logic living in
`pipeline.py`, `db.py`, `billing.py`, `seed.py`, and `providers/*`.

---

## `_configure_logging(verbose: bool) -> None` (line 34)

Calls `logging.basicConfig` unconditionally. No return value, no error path — `basicConfig` is a
no-op if handlers are already configured on the root logger (stdlib behavior, not visible in this
file), so a second CLI invocation within the same process would silently not reconfigure level. Not
relevant for normal single-shot CLI use but relevant if this module is ever imported and `main()`
invoked more than once in-process (e.g. tests driving the Typer app repeatedly).

**Invariant:** none established beyond "logging is configured to DEBUG or INFO before any command
body runs" (line 46, called from `main`, the `@app.callback()`).

---

## `main(verbose: bool) -> None` (line 42)

Typer's app-level callback — runs before every subcommand. Only effect is
`_configure_logging(verbose)` (line 46). No validation, no settings loaded here.

**Assumption relied on by every subcommand:** logging is initialized exactly once per invocation,
before subcommand code runs. Established by Typer's callback mechanism, not by anything in this
file — nothing in `cli.py` verifies `main` actually ran before a command body executes (it's
Typer/Click framework behavior).

---

## `_require_explicit_mode(demo: bool, real: bool) -> None` (line 49)

Enforces the file's documented safety posture: exactly one of `--demo`/`--real` must be passed.

- Both true → echo + `typer.Exit(code=1)` (lines 53-55).
- Both false → echo + `typer.Exit(code=1)` (lines 56-58).
- Exactly one true → returns normally (implicit).

**Invariant established for callers:** after this returns without raising, `demo != real`
(exactly one is `True`). This is a true invariant — there is no path through the function that
returns normally with `demo == real`.

**Callers depending on this invariant:** `sync` (line 89), `run` (line 148), `debug_parse_schedule`
(line 211). Each uses the now-guaranteed-exclusive `demo` flag downstream to pick `_load_settings`,
`_build_providers` / provider construction, and (in `run`) whether to sync-before-bill. `preview`
and `status` do **not** call this — they only take `--demo` (no `--real` flag exists for those
commands at all; see below), so the exclusivity invariant is irrelevant to them by construction
rather than by this check.

**Note on typer.Exit:** `typer.Exit(code=1)` raises `click.exceptions.Exit`-derived exception that
Typer's runner catches to set the process exit code. Nothing after `raise typer.Exit` in this
function executes; there's no code after the two exit points besides the fallthrough "both are
correctly set" case, so this is exhaustive over the four `(demo, real)` boolean combinations by
inspection (lines 53, 56, and implicit fallthrough).

---

## `_load_settings(demo: bool) -> Settings` (line 61)

Thin dispatch: `Settings.for_demo()` if `demo` else `Settings.from_env()` (line 62).

**What the callees establish:**
- `Settings.for_demo()` (config.py:78-80): returns a `Settings` with `db_path=Path("demo.db")`,
  `demo=True`, and every other field at its class default (including `sender_name`,
  `sender_email`, `term_start="2026-02-02"` implicitly via the model defaults). No I/O, cannot
  fail.
- `Settings.from_env()` (config.py:47-76): calls `load_dotenv()` (no-op if no `.env` found,
  config.py:52), reads `INVOICING_*` env vars, and constructs a `Settings`. The only validation
  performed here is pydantic's `field_validator` on `term_start` (config.py:37-41), which calls
  `date.fromisoformat(v)` and will raise if `INVOICING_TERM_START` is set to an unparseable string.
  **All Google-specific fields (`sheet_id`, `oauth_client_secret_file`,
  `drive_output_folder_id`, `doc_template_short_id`, `doc_template_long_id`) are left `None` if
  their env vars are unset — `from_env()` itself never raises for missing Google config.**

**Assumption `_load_settings` callers must not make:** that a returned `Settings` in non-demo mode
has valid Google configuration. Nothing in `_load_settings` or `Settings.from_env` enforces that;
it is deferred to `Settings.require_sheet_config()` / `Settings.require_google_config()`
(config.py:82-109), which are called separately and only on specific paths (see `_build_providers`
and `debug_parse_schedule` below). A caller that uses `settings.sheet_id` or similar directly
without going through `_build_providers` or `require_*_config` first would get `None` silently.

---

## `_build_providers(settings, demo) -> tuple[SheetProvider, DocProvider, EmailProvider]` (line 65)

- `demo=True` path (lines 68-73): returns `FakeSheetProvider(snapshot=ScheduleSnapshot(students=[],
  lessons=[]))`, `FakeDocProvider()`, `FakeEmailProvider()`. The empty snapshot is deliberate per
  the inline comment (lines 69-71) — demo mode never syncs from a fake Sheet; the fake
  `SheetProvider` exists only so `bill_period`'s `sheet.mark_lessons_billed(...)` call
  (pipeline.py:256) has a harmless target to write to. **Structural fact worth noting:** this means
  in demo mode, `sync_schedule_into_db` is never called on this fake sheet in practice from the CLI
  (demo branch of `sync` returns early via `seed_database`, line 92-95, before reaching
  `_build_providers`/`sync_schedule_into_db`), so the empty-snapshot fake sheet's `read_schedule()`
  is not actually exercised by `sync --demo`. It's exercised implicitly by `run --demo` only insofar
  as `_build_providers` is called there too (line 155) but `sync_schedule_into_db` is gated to
  `not demo` (line 157), so again `read_schedule()` on the empty fake sheet is never invoked from
  the CLI's `run` command either — the fake sheet's only real use from the CLI is as the
  `mark_lessons_billed` target inside `bill_period`.
- `demo=False` path (lines 75-76): calls `settings.require_google_config()` **before** constructing
  any real provider. This is the only enforcement point in this file that guarantees
  `GoogleSheetProvider`, `GoogleDocProvider`, `GoogleEmailProvider` are constructed with non-`None`
  `sheet_id`, `oauth_client_secret_file`, `drive_output_folder_id`, `doc_template_short_id`,
  `doc_template_long_id` (config.py:99-109, which itself calls `require_sheet_config` at
  config.py:101 for the sheet/oauth subset then checks the three Drive/Doc fields).

**Invariant established for callers:** if `_build_providers` returns (does not raise) with
`demo=False`, then `settings.require_google_config()` succeeded, so all five Google config fields
are non-`None`/non-falsy. Callers (`sync` line 96, `run` line 155) rely on this before passing
`settings` into `GoogleSheetProvider(settings)` etc. — but note the *provider constructors
themselves* are not shown to re-validate; they trust the fields are populated (config.py:174-179 in
`GoogleSheetProvider.__init__`, not fully read here, but no evidence of redundant validation).

**Unenforced assumption:** `require_google_config` (config.py:99) checks only that the config
values are non-empty strings/paths — it does not check that `oauth_client_secret_file` exists on
disk, that `sheet_id` refers to a real accessible sheet, or that OAuth credentials are valid. Those
failures surface later, inside `GoogleSheetProvider`/`GoogleDocProvider`/`GoogleEmailProvider`
methods (google.py), not in `_build_providers`. `cli.py` has no try/except around
`_build_providers` or the provider calls — an exception there propagates uncaught out of the
Typer command (Typer will report a traceback / non-zero exit, but there's no CLI-level graceful
handling).

---

## `_fmt_cents(cents: int) -> str` (line 79)

Pure formatting: `f"${cents / 100:.2f}"`. No validation of sign or magnitude — a negative `cents`
would format as `$-12.34`; nothing in this function or its known callers (`preview`,
`debug_parse_schedule`) is shown to ever pass a negative value, but the function itself does not
guard against it.

---

## `sync(demo, real) -> None` (line 83)

Command body:
1. `_require_explicit_mode(demo, real)` (line 89) — establishes `demo != real`.
2. `settings = _load_settings(demo)` (line 90).
3. `with Database(settings.db_path) as db:` (line 91) — opens/creates the SQLite file at
   `settings.db_path` and runs schema creation/migration (see `Database.__init__` →
   `create_schema` → `_migrate`, db.py:137-176). This can raise on filesystem errors (e.g.
   unwritable path); uncaught here.
4. If `demo`: `seed_database(db)` (line 93) then early `return` (line 95) — **`sync --demo` never
   calls `_build_providers` or `sync_schedule_into_db`**; it only ever seeds. The `FakeSheetProvider`
   path in `_build_providers` is dead code from this command's perspective when `demo=True`.
5. If not demo: `_build_providers(settings, demo)` (line 96) — as analyzed above, this is where
   `require_google_config` fires. Then `sync_schedule_into_db(db, sheet_provider)`
   (line 97, pipeline.py:54-117) is called and its returned count echoed.

**What `sync_schedule_into_db` establishes / assumes** (pipeline.py:54-117, followed in full):
- Calls `sheet.read_schedule()` once (line 66) — for `GoogleSheetProvider` this is a live network
  call; failures propagate uncaught through `sync`.
- Filters to `billing_type == "Private"` (line 76) — non-Private students are silently never
  synced into the DB at all, on every run, not just first sync.
- For each surviving student record: `db.get_or_create_parent` keyed on email (db.py:197-201),
  then `db.get_or_create_student` keyed on **name alone** (db.py:236-270). The docstring at
  db.py:239-251 documents that this is a deliberate choice to avoid double-billing forks that
  occurred historically when matching on `(name, parent_id)`; if a parent's `parent_id` resolved
  from the sheet differs from what's stored, the existing student row is repurposed
  (`UPDATE students SET parent_id = ...`, db.py:256-258) rather than a new student being created.
  **Consequence for `sync`'s caller:** two different real people who happen to share an identical
  `f"{first_name} {last_name}".strip()` (line 80) would collapse onto one DB student row across
  syncs — nothing in `sync_schedule_into_db` or `get_or_create_student` disambiguates same-name
  distinct students.
- Builds `student_ids` (first_name.lower() → db id) and `identity_map` (db student_id →
  first_name.lower()), then calls `sheet.set_identity_map(identity_map)` (line 93) — this must
  happen before any later `mark_lessons_billed` call on the same provider instance, per the
  `SheetProvider.set_identity_map` docstring contract (providers/base.py:78-87). `sync` alone never
  calls `mark_lessons_billed` itself (that happens inside `bill_period`, only reached from `run`),
  so this ordering constraint doesn't bite within `sync`, but the *provider instance* returned from
  `sync`'s `_build_providers` call is not reused elsewhere in this command — a fresh one is built
  again in `run`.
- Per-lesson: looks up `student_id` by lowercased first name (line 97); **if no match, the lesson
  is silently skipped** (`continue`, line 99) and not counted in `synced`. A lesson for a student
  whose billing_type wasn't "Private" (so never entered `student_ids`) is silently dropped here —
  no error, no log line, just absent from the returned count.
- `db.get_or_create_lesson(...)` (line 109, db.py:298-320) matches on `(student_id, lesson_date)`
  uniqueness (db.py:307-308 query, and schema's `UNIQUE (student_id, lesson_date)` constraint,
  db.py:74) — if a row already exists for that student+date, the **existing** row is returned
  unchanged; `attendance_status`/`pre_billed` from the new sync are **not** applied to an existing
  lesson row. So re-syncing after a status correction in the Sheet (e.g. absent → attended) would
  not update the already-present lesson row. This is a real behavior of `sync`, not a hypothetical.

**Invariant `sync` provides to later `run` invocations (indirectly, via the DB):** every synced
lesson for a "Private" student is present in `lessons` with `billed_invoice_id IS NULL` unless its
Sheet status was `"YI"` (mapped to `pre_billed=True`, line 114) — but note `pre_billed` is stored
and never read anywhere shown in `pipeline.py`'s billing path (`unbilled_lessons_in_period` is in
`billing.py`, not read in this pass — flagged as an open question below on whether `pre_billed`
actually gates billing eligibility or is informational only).

---

## `preview(period, demo) -> None` (line 101)

Read-only per its docstring (line 106). No `_require_explicit_mode` call — `preview` has no
`--real` option at all (only `--demo`, line 104), so it can only run in demo mode or "real settings
but no explicit real-mode gate." Concretely:

- `settings = _load_settings(demo)` (line 107).
- `with Database(settings.db_path) as db:` (line 108) — opens/creates DB, runs migrations, same as
  `sync`.
- If `demo`: `seed_database(db)` (line 110) — seeds if empty (`seed_database` itself checks
  `db.is_empty()`, seed.py:87-88, so calling it against an already-seeded demo DB is a no-op).
- **If not demo, no Google config check happens at all** — `preview` never calls
  `_build_providers`, `require_sheet_config`, or `require_google_config`. It goes straight to
  `preview_period(db, settings.term_start_date, period)` (line 111), which only touches the local
  SQLite DB (pipeline.py:128-148, calls `db.lessons_in_range`, `db.get_student` — no provider
  calls). **Structural consequence:** `preview --period N` against real (non-demo) settings whose
  Google config is entirely unset still runs successfully, reading whatever is already in
  `settings.db_path` (default `invoicing.db`) — it depends on that DB having been populated by a
  prior `sync --real`, but nothing in `preview` checks that a sync ever happened; an empty/unsynced
  real DB just yields "Nothing unbilled for this period" (line 115) indistinguishably from a fully
  synced-and-billed period.

**What `preview_period` establishes** (pipeline.py:128-148): computes `bounds =
period_bounds(term_start, period_number)` (billing.py, not read in this pass) and returns lines
purely from DB state, applying `unbilled_lessons_in_period` + `group_by_student` +
`compute_totals` (all in `billing.py`, out of scope of this pass but exercised here) — no writes,
consistent with the docstring's "makes no writes" claim (line 106), since every DB method it calls
(`lessons_in_range`, `get_student`) is a `SELECT`-only method in db.py.

**Formatting loop (lines 121-125):** iterates `lines` and calls `_fmt_cents` on
`subtotal_cents`/`total_cents`. Assumes `line.student.name` fits in a 28-char field for alignment
purposes only — no truncation or error if longer, just column misalignment, not a correctness
issue for the underlying data.

---

## `run(period, dry_run, confirm, demo, real, message) -> None` (line 128)

The most consequential command — the only one that can write invoices, mutate the Sheet, and send
email. Its safety posture is documented at the top of the file (lines 3-8) and enforced across
three independent gates:

1. `_require_explicit_mode(demo, real)` (line 148) — mode exclusivity, as above.
2. `dry_run` defaults to `True` (line 131-133; note `--dry-run/--no-dry-run` is a Typer boolean
   flag pair, so `dry_run` is `True` unless `--no-dry-run` is explicitly passed).
3. `confirm` defaults to `False` (line 134-136) and is passed through as `send_emails=confirm` to
   `run_period` (line 176) — so emailing requires *both* `--no-dry-run` and `--confirm`.

**Flow:**
- `settings = _load_settings(demo)` (line 149); `now = datetime.now(UTC)` captured once (line 150)
  and threaded through to `run_period` — this is the single timestamp used for `issued_at` on every
  invoice created in this run and for `emailed_at` on every email sent in this run (both computed
  inside `pipeline.py`, not recomputed per-invoice), so all invoices/emails from one `run`
  invocation share an identical timestamp.
- `with Database(settings.db_path) as db:` (line 152).
- If `demo`: `seed_database(db)` (line 154) — no-op if already seeded.
- `_build_providers(settings, demo)` (line 155) — for `demo=False` this is where
  `require_google_config` fires (all five Google fields required, unlike `preview`/`sync --demo`).
  **Note:** this call happens *before* the `if not demo and not dry_run` sync-guard below, so even a
  `--real --dry-run` invocation (which makes zero writes) still requires full Google config to pass
  `_build_providers` — the docstring's "dry-run... zero writes anywhere" (line 6) refers to writes,
  not to config/construction requirements; constructing `GoogleSheetProvider` etc. may itself
  perform lazy authentication (see `_sheets()`/`_services()` in google.py, not fully traced here)
  depending on how those classes are implemented — flagged as an open question below.
- Lines 157-163: `if not demo and not dry_run:` calls `sync_schedule_into_db(db, sheet_provider)`
  and logs the count. This is a **real-mode-only, non-dry-run-only** pre-billing sync — dry-run
  never syncs (consistent with "dry-run... zero writes"), and demo mode never syncs (consistent
  with demo never touching a real/fake sheet's `read_schedule`, matching the earlier analysis under
  `_build_providers`).
- `result = run_period(...)` (lines 165-178) — always called (not gated by `dry_run` at the call
  site; the gating is internal to `run_period`, see below), passing `dry_run=dry_run`,
  `send_emails=confirm`.

**What `run_period` establishes / the branches `run`'s caller must reason about** (pipeline.py:
352-401, followed on every path):
- `dry_run=True` (lines 377-379): returns immediately with `invoices_billed=[]`,
  `emails=EmailRunResult()` (all empty lists) — **never calls `bill_period` or
  `email_pending_invoices`**, confirming the "zero writes" claim structurally, not just by
  convention: the only DB-mutating pipeline functions are unreachable on this path.
- `dry_run=False`: always calls `bill_period` (line 381) regardless of `confirm`/`send_emails`.
  `bill_period` (pipeline.py:182-258) is where billing actually happens:
  - Raises `ValueError` if `not is_period_completed(bounds.end_date, today)` (line 199-200) —
    **uncaught in `cli.py`'s `run`**, so an attempt to bill a not-yet-finished period propagates as
    an unhandled exception out of the Typer command (traceback shown, non-zero exit) rather than a
    clean `typer.echo` + `Exit`. This is the one validation failure mode in the whole `run` path
    that isn't given a friendly CLI message.
  - Computes `unbilled_lessons_in_period` (excludes already-billed lessons, per the idempotency
    claim at pipeline.py:193-195) and, per student, creates a Doc (`docs.create_invoice_doc`,
    line 220 — network call for `GoogleDocProvider`, uncaught here), inserts the invoice + lines,
    calls `db.mark_lessons_billed` (line 246), and finally — only if `sheet_refs` is non-empty
    (line 255) — calls `sheet.mark_lessons_billed(sheet_refs, SheetStatus.INVOICED)` (line 256).
    **This Sheet write-back happens even when `send_emails=False`/`confirm` not passed** — i.e.
    `run --real --no-dry-run` (without `--confirm`) still writes to the live Sheet and creates
    Docs; only the email step is gated by `confirm`. The CLI's own docstring (line 7: "still won't
    send a single email unless --confirm is also passed") is accurate about email specifically but
    doesn't claim `--confirm` gates Sheet/Doc writes — worth flagging since a reader could
    over-generalize "no single flag both bills and emails by accident" to mean `--confirm` is the
    only thing standing between `--no-dry-run` and any external side effect, when in fact
    `--no-dry-run` alone is sufficient for Sheet + Doc writes.
  - After `bill_period`, `run_period` re-fetches `period = db.get_period(period_number)` (line 384)
    and asserts it's non-`None` — this *should* always succeed since `bill_period` just
    `get_or_create_period`'d it (pipeline.py:202), but relies on no concurrent process having
    deleted it between the two calls (no transaction spans both; each `Database` method commits
    independently, db.py's pattern throughout, e.g. line 356/188/227/etc.).
  - If `send_emails` (i.e. `confirm`) is `False` (line 387-388): returns with the billed invoices
    but `emails=EmailRunResult()` — **no `email_pending_invoices` call at all**, so pre-existing
    unsent invoices from a previous run (not just this run's newly billed ones) are also left
    unsent, consistent with the CLI's echoed message (line 186: "Invoices remain queued for
    email").
  - If `send_emails=True`: calls `email_pending_invoices` (line 390-399, pipeline.py:268-342),
    which iterates **every** invoice in the period with `emailed_at IS NULL`
    (`db.invoices_pending_email`, db.py:415-420) — not just the ones just billed — and per invoice:
    skips (records `skipped_no_contact`) if `not parent.email` (pipeline.py:292-294); otherwise
    calls `docs.export_pdf` then `email.send` inside a `try/except Exception` (lines 327-337) that
    catches **any** exception from either call and records `failed`, continuing to the next
    invoice — `db.mark_invoice_emailed` (line 339) is only reached on success, so a crash/exception
    mid-send never marks the invoice emailed, preserving retryability (rule 7, as documented). This
    matches the CLI's echoed `Emails failed (will retry next run)` message (line 191).
- Back in `cli.py`: after `run_period` returns, `run` echoes based on `dry_run`/`confirm` flags
  (lines 180-195) — these are purely presentational and don't re-derive any invariant; they trust
  `result.invoices_billed`, `result.emails.sent/failed/skipped_no_contact` as returned.

**Unenforced assumption specific to `run`:** the three-way flag combination
(`dry_run=False, confirm=True`) is the only path that reaches `email_pending_invoices`, and that
path is reached unconditionally on `bill_period` succeeding — there is no additional confirmation
prompt (e.g. "N invoices about to be emailed, proceed?") between billing completing and emailing
starting; `--confirm` is a static CLI flag checked once at argument-parse time, not a runtime gate
re-evaluated after seeing how many invoices are about to be emailed.

---

## `debug_parse_schedule(demo, real) -> None` (line 198)

Read-only diagnostic per its docstring (lines 203-210): only ever calls `sheet.read_schedule()`
(line 221) — no DB, no billing, no email, no Sheet write-back, confirmed by inspection (the
function body after `_require_explicit_mode`/`_load_settings` never touches `Database` or any
`DocProvider`/`EmailProvider`).

- `_require_explicit_mode(demo, real)` (line 211).
- `settings = _load_settings(demo)` (line 212).
- If `demo`: constructs a bare `FakeSheetProvider(snapshot=ScheduleSnapshot(students=[],
  lessons=[]))` directly (line 216) — **does not go through `_build_providers`**, so this always
  produces an empty snapshot regardless of any seeded DB state; `debug-parse-schedule --demo`
  structurally can never show non-empty output (confirmed by the final check at line 242-243,
  which is dead in demo mode — always true).
- If not demo: calls `settings.require_sheet_config()` directly (line 218) — **not**
  `require_google_config`. This is the narrower check documented at config.py:82-90: only
  `sheet_id` and `oauth_client_secret_file` are required, not Drive/Doc template IDs. This is the
  one command that can run against real Google Sheets without full Doc/Drive configuration present.
- `snapshot = sheet_provider.read_schedule()` (line 221) — live network call for
  `GoogleSheetProvider`; uncaught here, so failures (auth, network, bad sheet_id) propagate as
  unhandled exceptions.
- Prints students then lessons, sorted by `(lesson_date, student_first_name)` (lines 235-237).
  Trusts `snapshot.students`/`snapshot.lessons` field types as given by the `SheetStudentRecord`/
  `SheetLessonRecord` pydantic models (providers/base.py:21-51) — no additional validation in this
  function.

---

## `status(demo) -> None` (line 246)

Read-only summary. No `--real` flag, no `_require_explicit_mode` call, same shape as `preview`:

- `settings = _load_settings(demo)` (line 251).
- `with Database(settings.db_path) as db:` (line 252).
- If `demo`: `seed_database(db)` (line 254).
- `periods = db.list_periods()` (line 255) — pure `SELECT ... ORDER BY period_number`
  (db.py:370-372).
- If empty, echoes a hint and returns (lines 256-258).
- Otherwise, for each period: `assert p.id is not None` (line 264) — relies on `_period_from_row`
  (db.py:488-494) always populating `id` from a `NOT NULL PRIMARY KEY` column, which the schema
  guarantees (db.py:47: `id INTEGER PRIMARY KEY AUTOINCREMENT`) — this assertion cannot fail given
  the schema's own constraint, effectively unreachable rather than a live risk.
- `db.list_invoices(period_id=p.id)` (line 265) — pure `SELECT`. Computes `emailed` by counting
  `inv.emailed_at is not None` (line 266) and `pending = len(invoices) - emailed` (line 267) — pure
  arithmetic on already-fetched rows, no separate query, so `pending` is guaranteed non-negative and
  consistent with `invoices`/`emailed` by construction (same list, no filtering mismatch possible).

No Google/provider interaction at all in `status`, same as `preview`.

---

## Cross-cutting observations

- **No command wraps its body in `try/except`.** Every uncaught exception from `Settings.from_env`
  (bad `INVOICING_TERM_START`), `Settings.require_*_config` (`ValueError`), `Database.__init__`
  (sqlite errors), any provider network call, or `bill_period`'s `ValueError` for an incomplete
  period propagates as a raw Python traceback via Typer's default exception handling — the only
  CLI-level "clean" error paths are the two `typer.echo` + `typer.Exit(code=1)` calls inside
  `_require_explicit_mode`.
- **`Database` is a context manager** (db.py:147-151) whose `__exit__` unconditionally calls
  `self.close()` regardless of whether the `with` block raised — so a `Database.conn` is always
  closed even on error paths inside `sync`/`preview`/`run`/`status`, but any exception itself still
  propagates out of the `with` block and out of the command function (context managers don't
  swallow exceptions here — `__exit__` returns `None`/falsy implicitly, line 150-151, no
  suppression).
- **Every command that opens a `Database` re-runs `create_schema`/`_migrate`** (db.py:142,
  153-176) on every invocation, including read-only ones (`preview`, `status`,
  `debug-parse-schedule` does *not* open a DB at all, only `sync`/`preview`/`run`/`status` do). This
  is idempotent per the `CREATE TABLE IF NOT EXISTS` + version-gated migration design, not a
  structural risk, but means every CLI command pays a schema-check cost and can, in principle,
  perform a schema migration (`ALTER TABLE`) as a side effect of a "read-only" command like
  `preview` or `status` against an old database file.
- **`demo`/`real` flag pairs are inconsistent across commands:** `sync` and `run` require explicit
  `--demo`/`--real` via `_require_explicit_mode`; `debug-parse-schedule` also requires it;
  `preview` and `status` only expose `--demo` (default `False`) with no `--real` flag and no
  explicit-mode gate — meaning omitting `--demo` on those two silently means "use real settings"
  with no live-API safety prompt, though as noted above neither of those two commands actually
  reaches a Google provider, so the missing gate has no live-API consequence for them specifically.

---

## Open questions

- Does `pre_billed` (set by `sync_schedule_into_db` at pipeline.py:114, stored in the `lessons`
  table at db.py:73) actually influence `unbilled_lessons_in_period` in `billing.py` (not read in
  this pass), or is it purely informational? If it does gate eligibility, `sync`'s YI-handling
  (pipeline.py:105-108) is load-bearing for double-billing prevention across a DB rebuilt from a
  Sheet that already has invoiced lessons; if it doesn't, the docstring's claim at
  pipeline.py:106-108 may not hold. Need to inspect `invoicing/billing.py`.
- Do `GoogleSheetProvider.__init__` / `GoogleDocProvider.__init__` / `GoogleEmailProvider.__init__`
  (google.py:174, 364, 427) perform any eager network/auth calls, or is auth fully lazy (deferred
  to `_sheets()`/`_services()`/`_service()` on first method call)? This determines whether
  `_build_providers` in a `--real --dry-run` invocation of `run` can itself trigger an OAuth flow
  or network error before any billing logic runs. Need to inspect `providers/google.py` lines
  142-360 in full (only signatures were sampled in this pass).
- `Settings.from_env()`'s `term_start` validator (config.py:37-41) only checks ISO-date
  parseability, not that it's a Monday/fortnight-aligned date or otherwise consistent with
  `period_bounds`'s assumptions in `billing.py` (not read in this pass) — whether a misaligned
  `INVOICING_TERM_START` silently produces wrong period boundaries for every command that computes
  `bounds` (`preview`, `run`, `sync`'s downstream billing) is unclear without reading
  `billing.py:period_bounds`.
- `get_or_create_student`'s name-only matching (db.py:236-270), invoked from `sync` via
  `sync_schedule_into_db` (pipeline.py:81), means the CLI's `sync`/`run --real` commands are only as
  safe against student-identity collisions as that matching rule; whether the real Sheet data this
  is designed for actually guarantees globally-unique `"{first} {last}"` strings is outside this
  file's scope but is a direct precondition for `sync`'s correctness.
