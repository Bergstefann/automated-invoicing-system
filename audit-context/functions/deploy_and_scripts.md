# Analysis: deploy/scheduled_preview.py, scripts/build_schedule_template.py, scripts/render_workbook_screenshots.py

## Trigger, privileges, and credentials for scheduled_preview.py

`deploy/scheduled_preview.py` is invoked by the "Scheduled preview" GitHub
Actions workflow, `.github/workflows/scheduled-preview.yml`:

- Triggers: `schedule: cron "0 8 * * MON"` (weekly, Mondays 08:00 UTC) and
  `workflow_dispatch` (manual, any repo actor with write/trigger access) —
  lines 11-17 of the workflow.
- Runner: `ubuntu-latest`, GitHub-hosted, ephemeral (workflow lines 21-22).
- Credential provisioning (workflow lines 32-39): the job writes two GitHub
  Actions secrets to disk before running the script —
  `secrets.INVOICING_OAUTH_CLIENT_SECRET_JSON` → `credentials/credentials.json`
  and `secrets.INVOICING_OAUTH_TOKEN_JSON` → `credentials/token.json`. These
  are a real, already-authorized OAuth token (not a fresh flow — the runner
  is headless and non-interactive, so `_oauth_credentials`'s
  `InstalledAppFlow.run_local_server` path in `providers/google.py:134-137`
  cannot be reached; the cached token at `token.json` must already be valid
  or refreshable via `creds.refresh(Request())`, `google.py:131-132`).
- OAuth scope granted to that token is whatever was granted when the token
  secret was minted, but the code that consumes it requests
  `OAUTH_SCOPES` (`google.py:61-66`): full Sheets, Docs, Drive, and
  `https://mail.google.com/` (full Gmail — send, read, and modify, not the
  narrower `gmail.send`). The token used by this workflow is therefore
  provisioned with mail-account-wide reach even though
  `scheduled_preview.py` itself only calls `GoogleSheetProvider` (read +
  write-back via `sync_schedule_into_db`) and `GoogleEmailProvider.send`
  (workflow-triggered code path never calls `GoogleDocProvider`, but the
  credential is capable of it since it is the same OAuth client/token used
  by `invoicing run`).
- Env vars passed to the script (workflow lines 43-50): `INVOICING_TERM_START`
  (a repo *variable*, not secret), `INVOICING_SHEET_ID` (secret),
  `INVOICING_SCHEDULE_TAB` (variable), the two credential file paths, and
  sender name/email (variables). `settings.require_sheet_config()`
  (`config.py:82-97`) only demands `sheet_id` and
  `oauth_client_secret_file`; Drive/Doc template IDs are not required for
  this path since `bill_period`/`GoogleDocProvider` are never invoked here.
- Effective privilege of a run: whatever the OAuth token secret permits
  against the target Google account — a compromised or leaked
  `INVOICING_OAUTH_TOKEN_JSON` secret (or a compromise of the workflow file
  itself, e.g. via a malicious PR that edits the workflow, which
  `workflow_dispatch`/`schedule` triggers do not gate behind approval the
  way `pull_request_target` review gates would) grants Sheets write, Docs
  write, Drive write, and full Gmail send/read/modify — not merely "send a
  preview email".

## `deploy/scheduled_preview.py`

### `_fmt_cents(cents: int) -> str`
Pure formatting, `f"${cents / 100:.2f}"` (line 29). No validation of sign;
a negative `cents` renders as `$-x.xx` rather than erroring — nothing
upstream in this file guarantees `cents >= 0` at this call site.

### `_report_for_completed_period(settings, period_number) -> tuple[str, str]`
- Opens a `Database` context (line 33) scoped only to the `preview_period`
  call — closed again before any email I/O, so DB access is bounded and
  read-only for this function (see `preview_period` callee notes below:
  it performs zero writes).
- Branches on `if not lines` (line 36): empty-state email vs. table email.
  No lower bound check on `period_number` here — that precondition is the
  caller's responsibility (see `main`).
- `total_cents = sum(line.total_cents for line in lines)` (line 44) is
  simple aggregation over data already computed by `preview_period`/
  `compute_totals`; no re-derivation of rates or lesson counts.
- Builds an HTML email body embedding `line.student.name` raw into a `<td>`
  (line 48) with no HTML-escaping. **Assumption**: student names are safe
  to interpolate unescaped into HTML. Established by: nothing in this file
  or in `preview_period`/`compute_totals`/`db.get_student` — student `name`
  is sourced from the Sheet's "Student Config" tab column A/E
  (`google.py:199-217`, `sync_schedule_into_db` pipeline.py:80) with no
  sanitization at parse time (`google.py:210-211`) or at DB read time
  (`db.py:463-473`, `_student_from_row` passes the stored `TEXT` straight
  through). Whoever can edit the Google Sheet's Student Config tab controls
  the literal HTML injected into this operator-facing email body.
- Return value is a `(subject, body)` tuple consumed only by `main`'s
  `email.send` call; no further validation of length/content before send.

### `main() -> None`
- `settings = Settings.from_env()` (line 68) — loads `.env` if present via
  `load_dotenv()` (`config.py:52`, no-op if absent) then real env vars
  (workflow-injected in CI). See `Settings.from_env` callee notes.
- `settings.require_sheet_config()` (line 69) — raises `ValueError` if
  `sheet_id` or `oauth_client_secret_file` is missing (`config.py:95-97`).
  This is the *only* enforced precondition before Google API calls; it does
  **not** check `oauth_token_file` is set or exists — that is asserted
  later, inside `_oauth_credentials` (`google.py:124-125`,
  `assert settings.oauth_token_file is not None`), which raises
  `AssertionError` rather than the more informative `ValueError` if the
  workflow ever omitted `INVOICING_OAUTH_TOKEN_FILE`.
- `sheet = GoogleSheetProvider(settings)` (line 71) — lazy: no network call
  yet, `_sheets()` builds the client on first use (`google.py:180-184`).
- `with Database(settings.db_path) as db: sync_schedule_into_db(db, sheet)`
  (lines 72-73) — this is the only *write* path in this script: it mutates
  local SQLite state (creates/updates parents, students, lessons) and, via
  `sheet.set_identity_map`, prepares (but in this script never triggers,
  since `bill_period`/`mark_lessons_billed` are never called here) the
  Sheets write-back path. **Invariant relied on**: `sync_schedule_into_db`
  never marks anything billed and never sends a Sheets `batchUpdate` itself
  — confirmed at `pipeline.py:54-117`: no call to
  `sheet.mark_lessons_billed` anywhere in that function. So despite holding
  a Sheets-write-capable OAuth scope, this script's actual Sheets calls are
  read-only (`read_schedule`) at the API-call level.
- `today = datetime.now(UTC).date()` / `current_period =
  period_number_for_date(settings.term_start_date, today)` (lines 75-76).
  See `period_number_for_date` callee notes — raises `ValueError` if
  `today < term_start`; uncaught here, would crash the workflow run (visible
  as a failed Action, not silently swallowed).
- `if current_period <= 1: ... else: _report_for_completed_period(settings,
  current_period - 1)` (lines 78-82). **Invariant**: this always previews
  the period *before* the current one — i.e. only a period whose end date
  has already passed relative to `today`. This mirrors `preview_period`'s
  own read-only contract and is consistent with the "never touch an
  in-progress period" design documented in the module docstring; nothing
  here calls `is_period_completed` directly (that check lives in
  `bill_period`, `pipeline.py:199-200`, and is never reached from this
  script since `bill_period` is never called).
- Opens a **second**, separate `Database(settings.db_path)` inside
  `_report_for_completed_period` — sequential, not concurrent (the first
  `with Database` block for sync has already exited by line 73's dedent),
  so no concurrent-connection hazard within this script, though multiple
  independent script invocations (e.g. `workflow_dispatch` fired manually
  while the cron run is still in progress) against the same DB path are
  not excluded by anything here — SQLite's own locking is the only backstop
  (not analyzed further; out of scope for this file).
- `email = GoogleEmailProvider(settings)` then
  `email.send(to=settings.sender_email, ...)` (lines 84-85). Sends to
  **`settings.sender_email`** (the operator), not to any parent — this
  script never reaches parent-facing send paths. `GoogleEmailProvider.send`
  always also BCCs `settings.sender_email` (`google.py:449`), redundant
  here since `to` and the BCC target are the same address.
- No exception handling anywhere in `main`: a `ValueError` from
  `require_sheet_config`, `period_number_for_date`, an `AssertionError` from
  `_oauth_credentials`, or any Google API exception propagates uncaught,
  failing the GitHub Actions step. Nothing here retries, and nothing here
  distinguishes "Sheet sync failed" from "email send failed" — either
  aborts the whole run.

## Callees relied on by `scheduled_preview.py`

### `Settings.from_env()` / `Settings.require_sheet_config()` (`config.py`)
- `from_env` (lines 47-76) reads env vars with `INVOICING_` prefix,
  defaults `term_start` to `"2026-02-02"` and validates it's ISO-parseable
  via `field_validator` (lines 37-41) — raises at model construction if the
  env var is malformed. `require_sheet_config` (lines 82-97) checks only
  `sheet_id` and `oauth_client_secret_file` are truthy; `oauth_token_file`,
  `sender_email`, `schedule_tab` are all left at defaults if unset in env,
  with no validation that `sender_email` is a real deliverable address
  (default `"jane@example.com"`) — if the workflow's
  `INVOICING_SENDER_EMAIL` variable were ever unset, `main` would silently
  address the preview email to a placeholder domain rather than failing.

### `Database.__init__` / `sync_schedule_into_db` (`db.py`, `pipeline.py:54-117`)
- `Database.__init__` (`db.py:137-142`) always calls `create_schema()`,
  which runs `CREATE TABLE IF NOT EXISTS` then migrations (`_migrate`,
  lines 169-176) keyed on `PRAGMA user_version`. **Assumption**: the
  on-disk DB file at `settings.db_path` (default `invoicing.db`, workflow
  doesn't override `INVOICING_DB_PATH`) is either fresh (checked out via
  `actions/checkout`, i.e. whatever committed `.db` file exists in the repo
  at that path, if any) or already at a migratable schema version. No
  corruption/lock handling beyond what `sqlite3.connect` itself provides.
- `sync_schedule_into_db` (pipeline.py:54-117): pulls `sheet.read_schedule()`
  once (line 66), filters to `billing_type == "Private"` (lines 76-77 —
  always true in the live `GoogleSheetProvider` per `google.py:212`, since
  the test/production sheet has no billing-type column and every row is
  hardcoded `"Private"`), then for every student calls
  `db.get_or_create_parent` and `db.get_or_create_student`. **Established
  invariant** (relied on downstream by `preview_period` via `db.get_student`):
  every synced lesson has a resolvable `student_id` FK into `students`,
  because `get_or_create_student` (db.py:236-270) either returns an
  existing row or inserts one via `insert_student`, both of which always
  populate `id`/`student_id` — no path returns a partially-constructed
  `Student`. **Un-enforced assumption** noted in the source itself
  (pipeline.py:239-250, db.py:239-250 docstring): identity is resolved by
  student **name** alone; two students who legitimately share a display
  name would collide into one DB row, silently merging their billing
  history — nothing in `get_or_create_student` or `sync_schedule_into_db`
  detects or rejects this.
- Lessons with Sheet status `YI` are synced with `pre_billed=True`
  (pipeline.py:105-115), which makes `Lesson.is_unbilled` false
  (`models.py:92-93`) regardless of `billed_invoice_id` — this is what
  keeps `preview_period` from re-surfacing already-invoiced-by-the-old-
  system lessons. This function performs **only** DB writes; the
  `SheetProvider.mark_lessons_billed` write-back path is never invoked here
  (grep of pipeline.py confirms no such call), matching the workflow
  comment's claim that this script "only ever runs `sync`... and
  `preview`".

### `period_number_for_date` / `preview_period` / `compute_totals` (`billing.py`, `pipeline.py:128-148`)
- `period_number_for_date(term_start, lesson_date)` (billing.py:34-38):
  raises `ValueError` if `lesson_date < term_start` — uncaught in
  `scheduled_preview.py`, so a misconfigured `INVOICING_TERM_START` set in
  the future relative to the runner's clock crashes the whole workflow run
  rather than degrading gracefully.
- `preview_period` (pipeline.py:128-148) is documented and structurally
  read-only: it calls `db.lessons_in_range` (a `SELECT`), filters via
  `unbilled_lessons_in_period` (billing.py:46-51, pure), groups via
  `group_by_student` (pure), and for each group calls `db.get_student` +
  `compute_totals` (pure, billing.py:70-83). No `db.insert_*`/`db.mark_*`
  call appears anywhere in this function body — confirmed by reading all of
  pipeline.py:128-148. This is the invariant `_report_for_completed_period`
  and, transitively, `main` depend on for the module's core safety claim
  ("never bills, never sends an invoice").
- `compute_totals` hardcodes `gst_cents = 0` (billing.py:78) — disclosed
  simplification, not derived from any config; `total_cents` in the preview
  email is therefore always exactly `subtotal_cents`.
- `db.get_student(student_id)` (db.py:230-234) raises `KeyError` if the id
  is missing — cannot occur here since `student_id` values come from
  `group_by_student`, which only groups lessons already carrying a
  `student_id` FK inserted by `get_or_create_lesson` against a real
  `students.id` (FK constraint `PRAGMA foreign_keys = ON`, db.py:141,
  enforced at the SQLite layer too).

### `GoogleSheetProvider.read_schedule` / OAuth (`google.py`)
- `read_schedule` (lines 186-231) makes two API calls: one for
  `STUDENT_CONFIG_RANGE` ("Student Config!A:E") and one for the full
  `schedule_tab` grid with `valueRenderOption="FORMATTED_VALUE"`. Header
  row of Student Config is unconditionally skipped (`config_rows[1:]`,
  line 199) — **assumption**: row 1 is always a header; nothing validates
  this, so if the Sheet's structure ever loses its header row, the first
  real student is silently dropped.
- Rate parsing (lines 206-207) strips everything but digits/`.` and
  defaults to `"40"` if the cell is blank — a malformed rate cell (e.g.
  text, symbols) degrades to `$40.00`/lesson rather than raising, so a
  Sheet data-entry error silently produces a wrong rate rather than
  failing loud.
- `_parse_blocked_schedule` (lines 287-358) can raise `ValueError` on an
  "ambiguous student reference" (lines 340-347) — this propagates up
  through `read_schedule` → `sync_schedule_into_db` → `main`, uncaught,
  crashing the workflow run rather than silently mis-syncing.
- `_oauth_credentials` (lines 123-139): three paths —
  (1) cached token exists and is valid → used as-is;
  (2) cached token exists, expired, has `refresh_token` → refreshed via
      `creds.refresh(Request())` (line 132) and rewritten to
      `oauth_token_file` (line 138);
  (3) no cached token, or invalid with no refresh token → falls into
      `InstalledAppFlow.run_local_server(port=0)` (line 137), which opens a
      local browser/port for interactive consent. **On the GitHub Actions
      runner this path is unreachable in practice** (headless, no browser,
      no open inbound port reachable by a human) — if the provisioned
      `INVOICING_OAUTH_TOKEN_JSON` secret ever expires without a usable
      `refresh_token`, this call hangs or fails inside CI rather than
      degrading. Nothing in `scheduled_preview.py` or the workflow guards
      against or times out this case.
- `_load_cached_token` (lines 104-120) migrates a legacy pickle-format
  token to JSON in place by writing back to `token_file` — on the CI
  runner this write lands on the ephemeral `credentials/token.json` written
  moments earlier from the secret, not back into the GitHub secret store,
  so a migration (or a refresh, per point above) that happens during a CI
  run is **not persisted** back to `INVOICING_OAUTH_TOKEN_JSON` — every
  run re-seeds from the same secret value. If that secret's token is close
  to requiring re-consent, in-CI refreshes do not fix the stored secret for
  next week's run.

### `GoogleEmailProvider.send` (`google.py:424-462`)
- Builds a MIME message with `To`, `From` (`settings.sender_email`), BCC
  (also `settings.sender_email`), HTML body, optional attachment
  (line 437-457) — `scheduled_preview.py` never passes `attachment`, so
  that branch is dead for this caller.
- No exception handling inside `send`; a Gmail API error (auth failure,
  rate limit, quota) propagates to `main`, which does not catch it either
  — the whole workflow step fails, and since email is the very last
  statement (line 85), a failure here occurs *after* `sync_schedule_into_db`
  has already committed its DB writes (each `Database` write method calls
  `self.conn.commit()` immediately, e.g. db.py:227, 295) — so a sync can
  succeed and persist while the notification email fails, and nothing
  retries the email specifically (the whole script would need to be re-run,
  which would attempt the sync again, redundantly but idempotently, since
  `get_or_create_*` methods are idempotent).

## `scripts/build_schedule_template.py`

Offline, developer-invoked generator (no scheduled trigger found; not
referenced by any workflow — confirmed no hits in
`.github/workflows/*.yml` for this filename). Writes
`templates/lesson_schedule.xlsx`, which is later loaded by a CI test
(`tests/unit/test_workbook.py`'s
`test_the_committed_template_loads_and_validates`, per the module
docstring lines 10-13) — this is the enforcement mechanism ensuring the
generated column names/sheet names/version cell stay in sync with
`invoicing.schedule_contract`/`invoicing.workbook`, not anything inside
this script itself.

### `_style_header_row`, `_autosize` — pure `openpyxl` formatting helpers,
no branching of note; write cell values/styles positionally from
`enumerate(headers, start=1)` (line 61) — column order in `STUDENT_HEADERS`
/`SCHEDULE_HEADERS` (lines 47-48) is the sole source of truth for column
position; nothing cross-checks these lists against
`invoicing.schedule_contract`'s expected column order at *generation* time
(only the committed-output test in the other repo module catches drift,
per the docstring).

### `_build_legend_sheet(wb)`
Writes the schema version into `SCHEMA_VERSION_CELL` (`"B2"`, imported from
`invoicing.workbook`) as `CURRENT_SCHEMA_VERSION = min(SUPPORTED_SCHEMA_VERSIONS)`
(line 40) — **invariant**: this script always stamps the *minimum*
supported version, not the maximum/latest, so the committed template
exercises the oldest-supported-contract code path in the loader test,
not necessarily the newest schema features. This is a deliberate choice
visible only from reading `invoicing.schedule_contract.SUPPORTED_SCHEMA_VERSIONS`
(not opened in this pass — noted as an open question below).

### `_build_students_sheet(wb)` / `_build_schedule_sheet(wb, students_sheet)`
- Writes example rows then forces `number_format = "@"` (text) on
  `student_id` columns across 200 extra blank rows (lines 148-149,
  165-167) so Excel never auto-converts `"S-0001"`-style ids to numbers —
  a formatting-only safeguard for spreadsheet editors, not a data
  validation the parser depends on (the parser is not part of this file).
- Data validation dropdowns (lines 170-190) reference
  `={students_sheet.title}!$A$2:$A$201` — hardcoded to 200 rows
  (`max_row = 2 + len(EXAMPLE_SCHEDULE) + 200`, line 163, and the
  students sheet loop range `2, 2 + len(EXAMPLE_STUDENTS) + 200`, line 148)
  — **assumption baked into the generated artifact**: no more than ~200
  students/schedule rows will ever be entered by hand before the template
  needs regenerating; nothing enforces this beyond the dropdown silently
  not covering rows beyond 201 (Excel-side UX limit, not a data-integrity
  one, since `showErrorMessage`/`error` (lines 174-176, 185-187) only rejects
  values *within* the validated range that aren't in the list — free text
  is otherwise unconstrained past row 201).

### `build()` (lines 195-203)
Always constructs a **new** `Workbook()` (line 196, `openpyxl.Workbook()`
starts with one default sheet, retitled to `LEGEND_SHEET` at
`_build_legend_sheet` line 76) and overwrites `OUTPUT_PATH` unconditionally
(`wb.save(OUTPUT_PATH)`, line 202) — no diffing, no confirmation; running
this script always clobbers the committed template file. `mkdir(parents=True,
exist_ok=True)` (line 201) means the parent `templates/` dir is created if
absent, so this never fails on a fresh checkout.

## `scripts/render_workbook_screenshots.py`

Also offline/developer-invoked (no workflow reference found); reads the
**committed output** of `build_schedule_template.py` via
`load_workbook(WORKBOOK_PATH)` (line 173) and renders three PNGs into
`docs/images/`.

### Font loading (module scope, lines 27-30, and `_font`, lines 43-44)
`FONT_DIR = Path("C:/Windows/Fonts")` is a **hardcoded Windows path**;
`_font` calls `ImageFont.truetype(str(path), size)` with no existence
check or fallback — on any non-Windows environment, or a Windows box
missing Calibri, this raises `OSError` at first use (inside
`render_sheet`, line 112) rather than at import time, so `build()`'s two
early `wb["Legend"]`/etc. lookups (lines 176, 181, 186) would already have
succeeded before the failure surfaces. **Assumption**: this script only
ever runs on the author's Windows machine with Calibri installed; nothing
in the script enforces or checks this, and no CI workflow was found
invoking it, consistent with it being a manual "regenerate docs images"
step per the module docstring (lines 9-12).

### `_col_pixel_width`, `_cell_text_color`, `_cell_fill_color`, `_format_value`
Pure read-side helpers over `openpyxl` cell objects (lines 47-86). Each
defensively handles `None`/absent styling (`dim is None`, `fill.fgColor is
None`, `color is None`) by falling back to defaults (`10.0` width,
`DEFAULT_TEXT`, `None` fill) — no assumption here that every cell carries
explicit styling, consistent with `build_schedule_template.py` only
styling header rows and specific cells, not every cell in the 200-row
padding range.

### `render_sheet(sheet, out_path, *, max_row, max_col)`
- Two-pass layout: first pass (lines 92-104) computes `row_heights` by
  wrapping every non-empty cell's text and taking the tallest requirement
  per row; second pass (lines 129-166) actually draws using those
  precomputed heights. **Invariant**: `max_row`/`max_col` passed by
  `build()` must bound the sheet's real content, since `render_sheet` never
  reads `sheet.max_row`/`sheet.max_column` itself — it trusts the caller's
  hardcoded values (`build()` lines 178, 183, 188: `max_row=18, max_col=3`
  for Legend; `max_row=3, max_col=5` for Students/Schedule, matching the
  2-example-row layout `build_schedule_template.py` produces). If
  `build_schedule_template.py`'s example-row count or legend layout ever
  grows without `render_workbook_screenshots.py`'s hardcoded `max_row`
  values being updated to match, output images silently truncate — nothing
  cross-checks the two scripts' constants against each other.
- Text color for a filled cell is forced to white via
  `_cell_fill_color(cell) and (255, 255, 255) or _cell_text_color(cell)`
  (line 154) — a short-circuit idiom, not an `if`/`else`; **latent
  edge case**: if `_cell_fill_color` ever returned a falsy-but-non-None
  tuple like `(0, 0, 0)` (pure black fill), the `and` short-circuits to
  `False`-like on the *tuple* itself only if the tuple is empty (it never
  is, tuples of 3 ints are always truthy) — so this specific idiom happens
  to work for all realistic RGB tuples, but relies on Python truthiness of
  a 3-tuple always being `True`, not on an explicit `is not None` check.

### `build() -> list[Path]`
- `wb["Legend"]`, `wb["Students"]`, `wb["Schedule"]` (lines 176, 181, 186)
  — plain `dict`-style KeyError if the sheet names ever drift from
  `invoicing.workbook.LEGEND_SHEET`/`STUDENTS_SHEET`/`SCHEDULE_SHEET`
  (this script imports the raw string literals `"Legend"`/`"Students"`/
  `"Schedule"` directly rather than importing the same constants
  `build_schedule_template.py` uses — **unenforced assumption**: these
  three literals stay in sync with `invoicing.workbook`'s constants;
  nothing imports or cross-checks them here, unlike
  `build_schedule_template.py` which does import
  `LEGEND_SHEET`/`STUDENTS_SHEET`/`SCHEDULE_SHEET` from `invoicing.workbook`
  at lines 33-36).
- `OUTPUT_DIR.mkdir(parents=True, exist_ok=True)` happens inside
  `render_sheet` (line 168) on every call — redundant across the three
  calls in `build()` but harmless (idempotent).

## Open questions

- `invoicing.schedule_contract.SUPPORTED_SCHEMA_VERSIONS` was not opened in
  this pass — unclear whether `min(SUPPORTED_SCHEMA_VERSIONS)` (used as
  `CURRENT_SCHEMA_VERSION`, build_schedule_template.py:40) is deliberate
  (always generate the oldest still-supported contract) or a naming/intent
  mismatch with a variable called "CURRENT".
- `invoicing.workbook.validate_schema_version` and the rest of the loader
  (`workbook.py:120-130` and beyond) were not read — unclear exactly what
  it does when the version cell is missing/non-numeric, which would matter
  for judging what `render_workbook_screenshots.py`/`build_schedule_template.py`
  jointly guarantee about the committed artifact's validity beyond "the one
  committed test passes."
- No workflow file references either `scripts/build_schedule_template.py`
  or `scripts/render_workbook_screenshots.py` (only `scheduled-preview.yml`
  and `ci.yml` exist under `.github/workflows`; `ci.yml` itself was not
  opened in this pass) — unclear whether `ci.yml` runs
  `test_the_committed_template_loads_and_validates` on every PR (the
  docstring implies yes) or only on some subset of triggers; not confirmed
  by direct inspection.
- Whether `settings.db_path` (default `invoicing.db`, relative to CWD) in
  the GitHub Actions run resolves to a path that persists between weekly
  runs (i.e. is the SQLite file committed to the repo, or written to a
  fresh ephemeral runner each time and never persisted) was not resolved —
  this materially affects whether `sync_schedule_into_db`'s "idempotent
  re-sync" invariant is actually exercised weekly against accumulated state
  or effectively starts from empty every run. `actions/checkout@v4`
  (workflow line 23) would restore whatever `invoicing.db` is committed in
  the repo, if any — not confirmed whether such a file is tracked in git
  (this repo is not a git working copy in this environment per the task
  metadata, so `git ls-files` could not be used to check).
