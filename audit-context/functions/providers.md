# `src/invoicing/providers/base.py`, `fake.py`, `google.py`

## Overview

`base.py` defines the `SheetProvider` / `DocProvider` / `EmailProvider`
`Protocol`s and the DTOs that cross the provider boundary (`base.py:1-143`).
`pipeline.py` imports only these Protocols and DTOs, never a concrete
implementation (`base.py:1-7`). Two implementations exist: `fake.py`
(in-memory, used by every test and `--demo`) and `google.py` (live Google
Sheets/Docs/Drive/Gmail, used only in real, non-demo runs — `google.py:1-6`).
`cli._build_providers` (`cli.py:65-76`) is the single place that chooses
between them, keyed on the `--demo`/`--real` flag; nothing inside `pipeline.py`
or the fakes/google module itself can tell which one is wired up.

---

## `base.py`

### DTOs (Pydantic `BaseModel`s) — invariants

These are plain data containers; the invariants that matter are about
*meaning*, not runtime checks, since Pydantic only enforces field types/
presence, not the semantic rules the docstrings describe:

- `SheetStudentRecord.student_id` (`base.py:32`) — documented convention:
  `None` unless the source is a "schema-contract" workbook; `GoogleSheetProvider`
  always leaves it `None` (`base.py:24-29`). Nothing in `base.py` enforces this
  — it's a contract observed by convention across implementations, not a
  runtime check. `GoogleSheetProvider.read_schedule` (`google.py:208-217`)
  indeed never sets it (defaults to `None` via the pydantic field default).
- `SheetLessonRecord.student_id` (`base.py:47`) — same pattern; `google.py`'s
  `_parse_blocked_schedule` builds `SheetLessonRecord` without a `student_id`
  argument (`google.py:352-355`), so it's always `None` from that source too.
- `SheetLessonRef` (`base.py:58-72`) is *deliberately* keyed on
  `student_id` + `lesson_date`, not a display name or row/column indices —
  stated rationale ties directly to `docs/POSTMORTEM-double-billing.md`
  (`base.py:63-68`). This is a design invariant the whole write-back path
  (`pipeline.bill_period` → `sheet.mark_lessons_billed`) depends on; nothing
  in `base.py` enforces `student_id` actually being a real, resolvable id —
  that's on the caller (`pipeline.py:249-251` asserts
  `student.student_id is not None` before building the ref).

### `SheetProvider.set_identity_map` — protocol-level ordering contract

The docstring at `base.py:78-87` states the contract precisely: called once,
after `read_schedule`, by `sync_schedule_into_db`, so `mark_lessons_billed`
can resolve a write-back target without re-deriving identity from a display
name. **This ordering is not enforced by the `Protocol` itself** — `Protocol`
only constrains method signatures, not call order. Each implementation is
independently responsible for behaving safely if the contract is violated
(see per-implementation analysis below); a caller that never calls
`set_identity_map`, or calls `mark_lessons_billed` before it, is not rejected
by anything at this layer.

### `DocProvider` / `EmailProvider`

Pure protocols, no invariants beyond signatures (`base.py:116-142`).
`DocProvider.create_invoice_doc` documents it "returns the created doc's id"
(`base.py:117-119`) — that id is later fed back into `export_pdf` by
`pipeline.email_pending_invoices` (`pipeline.py:328`, sourced from
`invoice.doc_url`, `pipeline.py:320-321`). Nothing in `base.py` constrains the
id's format; it is treated as an opaque token by every caller.

---

## `fake.py`

### `FakeSheetProvider` (`fake.py:22-35`)

- `read_schedule` (`fake.py:27-28`) returns the `snapshot` field verbatim,
  every call — no mutation, no simulated I/O failure path. Whatever the
  caller constructed the fake with is exactly what `sync_schedule_into_db`
  will see; there is no way to simulate a Sheet-read error via this fake.
- `set_identity_map` (`fake.py:30-31`) stores the map unconditionally;
  no validation that it was preceded by a `read_schedule` call, no check
  against `snapshot`'s contents. The ordering contract from `base.py:78-87`
  is not enforced here either — nothing would break if `set_identity_map`
  were skipped, since `mark_lessons_billed` (below) never reads
  `identity_map`.
- `mark_lessons_billed` (`fake.py:33-35`) simply appends every
  `(ref, status)` pair to `marked_billed` — **it never consults
  `self.identity_map`, `self.snapshot`, or performs any resolution.** This is
  a structural divergence from `GoogleSheetProvider.mark_lessons_billed`,
  which resolves each ref through `_identity_map` and `_cell_index` and can
  silently drop refs that don't resolve (`google.py:236-259`). The fake
  therefore *cannot* reproduce the "no Sheet cell found" drop path
  (`google.py:242-249`) that the real provider has — any test relying on the
  fake to model that behavior would be testing something the fake doesn't
  implement.
- No persistence, no thread-safety concerns beyond ordinary Python list/dict
  mutation — single-threaded test/demo use is assumed throughout, nothing
  enforces it.

### `FakeDocProvider` (`fake.py:38-55`)

- `create_invoice_doc` (`fake.py:44-48`): id generation is
  `f"fake-doc-{self._next_id}"`, monotonically incrementing, unique within
  one instance's lifetime (never reused, never reset). Stores the full
  `InvoiceDocData` for later retrieval by `export_pdf`.
- `export_pdf` (`fake.py:50-55`): two explicit simulated-failure paths ordered
  before the success path — `doc_id in fail_export_doc_ids` raises
  `RuntimeError` first; only then is `doc_id not in created_docs` checked
  (raises `KeyError`). This ordering means a caller can register a doc_id in
  `fail_export_doc_ids` that was *never* created and still get the
  `RuntimeError` rather than `KeyError` — i.e. `fail_export_doc_ids`
  membership is checked unconditionally, independent of whether the doc
  exists. Success path returns a deterministic placeholder byte string
  (`f"%PDF-FAKE for {doc_id}".encode()`, `fake.py:55`) — not a real PDF,
  callers that inspect PDF structure (none observed in `pipeline.py`) would
  need a different fixture.

### `SentEmail` / `FakeEmailProvider` (`fake.py:58-91`)

- `send` (`fake.py:76-90`): failure condition is
  `fail_after is not None and len(self.sent) >= fail_after` — checked
  **before** appending to `self.sent`, so `fail_after=N` means the (N+1)-th
  call (0-indexed: calls `0..N-1` succeed, call `N` raises) fails, and every
  call after that also fails permanently (since `len(self.sent)` only grows
  via successful appends, once `>= fail_after` it stays `>= fail_after`).
  Docstring (`fake.py:68-70`) describes this as "the Nth send raises" —
  consistent with the code: once `len(sent) == fail_after`, that call and
  all subsequent calls raise, never recovering. This is a permanent-failure
  simulation, not a transient one-shot failure.
- On success, appends a `SentEmail` capturing exactly the four parameters
  passed, then returns a fresh monotonically-increasing `fake-msg-N` id.
  No validation of `to`/`subject`/`html_body` content, no simulated Gmail
  quota/rate-limit behavior.

**Fake-provider contract to callers, generally**: all three fakes are
call-recording proxies with no network access, deterministic ids, and
explicit, caller-configured failure injection points (`fail_export_doc_ids`,
`fail_after`) — they do **not** attempt to replicate every behavior of the
corresponding Google provider (notably: no cell-resolution logic in
`FakeSheetProvider.mark_lessons_billed`, no OAuth, no HTTP failure modes
other than the ones explicitly modeled).

---

## `google.py`

### Module-level auth model (`google.py:8-13`, `61-66`)

Single shared OAuth **installed-app** flow (`InstalledAppFlow`,
`google.py:43`) covering all four APIs (Sheets, Docs, Drive, Gmail) via one
scope list, `OAUTH_SCOPES` (`google.py:61-66`):
`spreadsheets`, `documents`, `drive` (full, not `drive.file`), and
`https://mail.google.com/` (full Gmail access, not a narrower send-only
scope). All three provider classes (`GoogleSheetProvider`, `GoogleDocProvider`,
`GoogleEmailProvider`) request the *same* full scope set regardless of which
subset of APIs that particular class actually calls — e.g.
`GoogleEmailProvider` obtains credentials scoped to Sheets/Docs/Drive/full-Gmail
even though it only ever calls Gmail's `send` (`google.py:437-462`). This
follows structurally from `_oauth_credentials` being called with the same
module-level `OAUTH_SCOPES` from every `_service`/`_sheets`/`_services`
accessor (`google.py:180-184`, `369-374`, `431-435`), each independently
calling `_oauth_credentials(self._settings)`.

### Credential sourcing — `_oauth_credentials` (`google.py:123-139`)

```
assert settings.oauth_client_secret_file is not None   # line 124
assert settings.oauth_token_file is not None            # line 125
```

- Both are bare `assert`s, not `Settings.require_*` calls. **Nothing upstream
  guarantees `oauth_token_file` is set** before this function runs:
  `Settings.require_sheet_config` (`config.py:82-97`) only checks
  `INVOICING_SHEET_ID` and `INVOICING_OAUTH_CLIENT_SECRET_FILE` — it never
  checks `oauth_token_file`. `require_google_config` (`config.py:99-109`)
  calls `require_sheet_config` plus Drive/Doc-template checks, and likewise
  never validates `oauth_token_file`. So a real run with
  `INVOICING_OAUTH_TOKEN_FILE` unset passes both `require_sheet_config()` and
  `require_google_config()` cleanly (`cli.py:75`, `cli.py:218`) and only fails
  later, inside `_oauth_credentials`, with a bare `AssertionError` (no
  message) the first time any provider's `_sheets`/`_services`/`_service`
  accessor is invoked. This is an **unenforced assumption**: "settings passed
  to any Google provider always have `oauth_token_file` set" — established
  nowhere before the assert that depends on it.
- Credential resolution order (`google.py:127-139`):
  1. If `oauth_token_file` exists on disk, load it via `_load_cached_token`
     (line 128-129).
  2. If no cached creds, or cached creds are not `.valid`: if expired *and*
     carrying a `refresh_token`, refresh in place (`creds.refresh(Request())`,
     line 132) — network call to Google's token endpoint. Otherwise (no
     cached creds at all, or invalid with no usable refresh token), fall
     back to the full interactive `InstalledAppFlow.from_client_secrets_file`
     + `flow.run_local_server(port=0)` (lines 134-137) — this **opens a local
     HTTP listener and a browser-based consent flow**, which blocks until a
     human completes it. There is no non-interactive failure mode: if this
     code path is reached in a headless/CI environment, `run_local_server`
     will hang or fail depending on environment, not raise a clean error.
  3. Either way, the resulting `creds` (fresh, refreshed, or newly
     authorized) is written back to `oauth_token_file` as JSON
     (`creds.to_json()`, line 138) — this happens even on the refresh path,
     so a refreshed token is persisted for next time, but *not* on the
     "loaded from cache and already valid" path (that branch skips the
     `if not creds or not creds.valid` block entirely, `google.py:130`,
     leaving `settings.oauth_token_file` untouched — correct, since nothing
     changed).
- `_oauth_credentials` is called independently by each provider class's
  lazy accessor (`GoogleSheetProvider._sheets`, `GoogleDocProvider._services`,
  `GoogleEmailProvider._service`) — each provider instance caches its own
  `build(...)` result (`self._service` / `self._docs`,`self._drive` /
  `self._gmail`) but **does not cache `Credentials` across provider
  instances**. Three separate `GoogleXProvider` instances (as constructed by
  `cli._build_providers`, `cli.py:76`) each independently call
  `_oauth_credentials`, each independently reading/writing
  `oauth_token_file`. Since all three read the same file and the token is
  normally already valid after the first provider's construction populates
  it, in practice only the first call in a process is likely to hit the
  interactive-flow or refresh branches — but nothing enforces this; a
  concurrent scenario (unlikely given the CLI's synchronous single-process
  design, `cli.py:65-76` runs sequentially) could race on read-then-write to
  the same token file. `cli.py`'s current call pattern is entirely
  synchronous/single-threaded, so this is a structural note, not an observed
  race.

### `_load_cached_token` (`google.py:104-120`)

- Primary path: `Credentials.from_authorized_user_file` (line 114) — a
  library call, assumed to correctly parse a JSON token file into a
  `Credentials` object with scopes checked against `OAUTH_SCOPES`. Not
  independently verified here (external library, google-auth) — the
  assumption "the JSON file's stored scopes are compatible with
  `OAUTH_SCOPES`" is delegated entirely to that library call; if the file has
  narrower/different scopes, whatever `from_authorized_user_file` does
  (accept, silently narrow, or raise) is inherited without modification here.
- Migration path (lines 115-120): catches `ValueError` (the doc comment
  explains it covers both `json.JSONDecodeError` and `UnicodeDecodeError`
  subclasses, `google.py:108-111`) and falls back to `pickle.load` on the
  raw file bytes. **`pickle.load` on a file path taken directly from
  `Settings.oauth_token_file`, which is sourced from an environment variable
  (`INVOICING_OAUTH_TOKEN_FILE`, `config.py:70`)** — this function trusts the
  token file's contents completely; nothing validates that the bytes being
  unpickled are a legitimate previously-serialized `Credentials` object
  rather than arbitrary pickle data. The catch is broad (any `ValueError`
  from the JSON path triggers pickle deserialization of the same file's raw
  bytes), so any file that isn't valid JSON is unconditionally deserialized
  as a pickle. No signature/integrity check on the file's origin exists at
  this layer or, as far as this analysis extends, anywhere upstream — the
  token file is treated as a trusted local artifact by construction (it's
  written by this same code at line 118, and by `_oauth_credentials` at line
  138), but the trust boundary depends entirely on filesystem permissions
  that are outside this module's control.
- After a successful pickle load, `creds.to_json()` is written back to the
  same path (`google.py:118`) and the migration is logged (line 119) — no
  validation that the unpickled object is actually a `Credentials` instance
  before calling `.to_json()` on it; a malformed/unexpected pickle payload
  would surface as an `AttributeError` here rather than a clean error at the
  point of trust violation.

### Sheet parsing helpers

`_parse_date_cell` (`google.py:72-81`): defensive — strips whitespace/NBSP,
regex-matches `D{1,2}/M{1,2}`, returns `None` on non-match or on a
`date(year, month, day)` `ValueError` (e.g. `32/13`). No exception escapes
this function; every caller can treat `None` as "not a date" uniformly.

`_is_header_row` (`google.py:84-92`): a row is a header iff at least
`min_dates` (default 2) of its even-indexed cells parse as dates. This
threshold is a heuristic, not a structural guarantee about the sheet's
layout — a data row that happens to have ≥2 parseable dates in its
name-column positions (e.g. two students both literally named something
matching `\d{1,2}/\d{1,2}`) would be misclassified as a header. Nothing in
this module rules that input out; it is an unenforced assumption about
the Sheet's actual content, inherited from "mirrors the original's
heuristic exactly" (`google.py:86-87`).

`_col_letter` (`google.py:95-101`): standard 0-based-index → spreadsheet
column-letter conversion (A, B, ..., Z, AA, ...). No bounds issues for any
non-negative `int` input; called only with values derived from `enumerate`/
`range` over parsed row data, so always non-negative in practice — not
independently checked inside the function.

### `_parse_blocked_schedule` (`google.py:287-358`)

- Builds two outputs together in one pass: `lessons` (list of
  `SheetLessonRecord`, only for cells with a non-blank status,
  `google.py:350-355`) and `cell_index` (a `(first_name.lower(), date) ->
  (row, status_col)` map, populated for **every** name/status pair
  encountered regardless of blank status, `google.py:349` — the index write
  happens before the blank-status `continue` at line 350-351). This is a
  documented design point (`google.py:297-301`): the index must exist for a
  lesson even before it has a status, so a later billing run can resolve a
  write-back cell for it.
- **Collision detection is strict, not silent**: if the same
  `(first_name.lower(), lesson_date)` key would map to two different
  `(row, status_col)` cells, `ValueError` is raised immediately
  (`google.py:339-348`), aborting the entire parse. This means one
  first-name collision anywhere in the whole grid makes `read_schedule` fail
  entirely for that call — there is no partial-success mode; the exception
  propagates uncaught through `read_schedule` (`google.py:230`) to whatever
  called it (`sync_schedule_into_db`, `cli.py`'s `sync`/`run`/
  `debug-parse-schedule` commands), and none of those catch it, so it
  surfaces as an unhandled exception terminating the command.
- The `current_col_dates` state resets on every header row encountered
  (`google.py:322`) and rows before the first header row are skipped
  entirely (`if not current_col_dates: continue`, `google.py:329-330`) — so
  any data appearing above the sheet's first parseable header row is
  silently dropped, not an error.
- Row/column bounds: `name_val`/`status_val` extraction guards against
  short rows (`if name_col < len(row) else ""`, lines 334, 337) — a row
  shorter than expected for a given block's column positions degrades to
  treating missing cells as empty strings rather than raising `IndexError`.

### `GoogleSheetProvider` (`google.py:142-266`)

Class-level invariant claimed in the docstring (`google.py:165-172`): the
combination `read_schedule()` then `set_identity_map()` must both have run,
in that order, before `mark_lessons_billed()` is useful — otherwise
`self._identity_map` and/or `self._cell_index` are still their `__init__`
defaults (`{}`, `google.py:177-178`).

- `__init__` (`google.py:174-178`): lazy state — `_service`, `_cell_index`
  (`{}`), `_identity_map` (`{}`). No network call at construction time; the
  first network round-trip happens on first `_sheets()`/`read_schedule()`
  call.
- `_sheets` (`google.py:180-184`): memoizes the built `spreadsheets()`
  resource on `self._service`; `_oauth_credentials` is only invoked the
  first time (or never, if `read_schedule`/`mark_lessons_billed` is never
  called on this instance — e.g. a `GoogleSheetProvider` built but unused
  in a dry-run, `cli.py:157-163` only calls `sync_schedule_into_db` when
  `not demo and not dry_run`).
- `read_schedule` (`google.py:186-231`):
  - `assert settings.sheet_id is not None` (line 188) — same
    unenforced-elsewhere pattern as the OAuth file asserts, except this one
    *is* actually covered: `require_sheet_config` (`config.py:92-97`) does
    check `INVOICING_SHEET_ID`, and both real-mode call sites (`cli.py:75`,
    `cli.py:218`) call `require_sheet_config`/`require_google_config` before
    constructing a `GoogleSheetProvider`. So this particular assert is
    backed by an enforced precondition on both known call paths — unlike the
    `oauth_token_file` assert in `_oauth_credentials`.
  - Two independent API calls in sequence: `Student Config!A:E`
    (`STUDENT_CONFIG_RANGE`, line 68) then the full schedule tab (line
    219-227, `range=f"'{settings.schedule_tab}'"`). Both use `.execute()`
    synchronously; any `HttpError`/network failure from either propagates
    uncaught out of `read_schedule`.
  - Student-config parsing (`google.py:198-217`): `config_rows[1:]` skips
    what's assumed to be a header row (no check that row 0 actually looks
    like a header — index-based, not content-based). Rows with an empty/
    whitespace-only column-A cell are skipped (`google.py:200-201`). Column
    layout is hardcoded by position (A/B/C/D/E — first name, parent email,
    rate, parent name, last name; comment at lines 202-204 notes this test
    sheet has no billing-type column, so **every** listed student is
    unconditionally given `billing_type="Private"` (line 212) — meaning
    `sync_schedule_into_db`'s `billing_type != BILLABLE_TYPE` filter
    (`pipeline.py:76-77`) can never exclude anyone sourced from this
    provider, since it's the provider itself that always supplies
    `"Private"`.
  - Rate parsing (`google.py:206-207`): `raw_rate = padded[2].strip() or
    "40"` then strips everything except digits and `.` from that string
    before `float(...)`; if the digit-stripped result is empty the `or 40`
    fallback yields the *integer* `40` (not `"40"`), which `float(40)`
    accepts fine — no crash, but note the fallback default silently applies
    both when the cell is blank (via the first `or "40"`) and when the cell
    has content that strips down to nothing (e.g. a cell containing only
    letters/symbols) via the second `or 40`. Both fallbacks produce the same
    numeric default with no distinguishing signal to the caller that a rate
    was actually missing/malformed versus genuinely `$40`.
  - Schedule grid parsing delegates entirely to `_parse_blocked_schedule`
    (line 230), which is where the `ValueError` collision risk described
    above lives; `self._cell_index` is assigned unconditionally from that
    call's second return value **before** any exception could be raised by
    it — actually no, if `_parse_blocked_schedule` raises, the assignment at
    line 230 never completes, so `self._cell_index` retains its prior value
    (empty on first call). A `read_schedule` call that raises leaves the
    instance in whatever state it was in before the call — no partial update
    of `self._cell_index` from a failed parse.
- `set_identity_map` (`google.py:233-234`): unconditional overwrite of
  `self._identity_map`, no validation against `self._cell_index`'s keys
  (e.g. no check that every `identity_map` value actually appears as a
  first-name key in `cell_index`) — mismatches are only discovered lazily,
  per-ref, inside `_resolve_lesson_cell` at billing time.
- `mark_lessons_billed` (`google.py:236-265`):
  - For each `lesson_ref`, resolves via `_resolve_lesson_cell` (module
    function, `google.py:268-284`), which does two sequential dict lookups
    (`identity_map.get(student_id)` then `cell_index.get((first_name_key,
    lesson_date))`) and returns `None` on either miss.
  - A miss is **not an error** — it's logged as a warning
    (`google.py:242-248`) and that ref is simply skipped (`continue`,
    implicit via the `if cell is None:` block ending without appending to
    `data`). The warning message states the consequence explicitly: "DB is
    billed, Sheet won't reflect it until the next sync" (line 245) — i.e.
    this function's failure mode is a **silent-to-the-caller, logged-only**
    partial write-back; `bill_period` (`pipeline.py:256`) does not inspect
    any return value from `sheet.mark_lessons_billed` (it returns `None`)
    and has no way to know some refs were dropped.
  - If `data` ends up empty (all refs unresolved), the function returns
    early without ever calling `_sheets()`/making any API request
    (`google.py:258-259`) — meaning **no OAuth credential resolution is
    triggered** in that case either, since `_sheets()` is only reached at
    line 261.
  - Otherwise, a single `batchUpdate` call writes all resolved cells in one
    request (lines 262-265), `valueInputOption="RAW"`. No per-cell error
    handling — a `batchUpdate` failure (e.g. `HttpError`) propagates
    uncaught for the whole batch, even if some of the writes would have
    succeeded individually; this is an all-or-nothing API-level call, unlike
    `email_pending_invoices`'s per-invoice try/except (`pipeline.py:327-337`).

### `_resolve_lesson_cell` (`google.py:268-284`)

Pure function (no I/O, no mutation) — two dict lookups, `None` on either
miss, as described above. Depends entirely on its two dict arguments being
current and mutually consistent (built from the *same* Sheet read) — nothing
inside this function checks that `identity_map` and `cell_index` actually
originated from the same `read_schedule()` call. If a caller passed an
`identity_map` built from an older/different Sheet snapshot than
`cell_index`, this function would silently produce wrong-but-plausible
resolutions (a `first_name_key` from a stale map matching an unrelated
cell in the current index) rather than detecting the mismatch — that
consistency is entirely a caller-side invariant (see `GoogleSheetProvider`
class docstring, `google.py:165-172`), not something enforced here or in
`GoogleSheetProvider` itself.

### `GoogleDocProvider` (`google.py:361-421`)

- `_services` (`google.py:369-374`): memoizes both `self._docs` and
  `self._drive` together, guarded by `if self._docs is None or self._drive
  is None` — both are always set together (no path sets one without the
  other), so this compound condition is equivalent to checking either alone
  in practice, but note it re-triggers `_oauth_credentials` (and rebuilds
  both services) if *either* is somehow `None`.
- `create_invoice_doc` (`google.py:376-411`):
  - Template selection: `short` if `len(data.lines) <= 4` else `long`
    (lines 380-384), both required via `assert template_id is not None`
    (line 385) — this assert is backed by `require_google_config`
    (`config.py:102-106`, checks both `DOC_TEMPLATE_SHORT_ID` and
    `DOC_TEMPLATE_LONG_ID`), which every real-mode caller of
    `GoogleDocProvider` goes through (`cli.py:75-76`; note
    `debug-parse-schedule`, `cli.py:198-230`, never constructs a
    `GoogleDocProvider` at all, only `GoogleSheetProvider`, so this class's
    preconditions are irrelevant to that command).
  - `drive.files().copy(...)` (lines 387-398) copies the template into
    `settings.drive_output_folder_id` — **not asserted non-`None` here**,
    but is covered by `require_google_config`'s check
    (`config.py:103`), same reasoning as above.
  - Text replacement via `replaceAllText` requests built from
    `build_replacements(data)` (line 408, imported from
    `invoicing.templates.invoice_doc` — not analyzed in this file's scope);
    `matchCase: True` (line 404) — if a tag in `build_replacements`' output
    doesn't case-match the template's literal placeholder text, that
    replacement silently does nothing (Docs API behavior, not this code's
    concern) rather than raising.
  - Returns `copy["id"]` (line 399, captured before the `batchUpdate` at
    line 410) — i.e. the returned `doc_id` is valid (the file exists in
    Drive) even if the subsequent `batchUpdate` text-replacement call fails;
    a `batchUpdate` exception here would propagate uncaught, leaving an
    orphaned, un-filled-in copy in Drive with no record of its id returned
    to the caller (the exception prevents `return doc_id` from executing).
- `export_pdf` (`google.py:413-421`): standard `MediaIoBaseDownload` chunked-
  download loop until `done`; no bound on total chunks/size, no timeout —
  relies entirely on the googleapiclient library's own behavior for
  malformed/huge/never-completing downloads. `doc_id` is passed through
  unchecked — no validation that it's actually a doc this instance created
  (this class holds no record of previously created ids, unlike
  `FakeDocProvider.created_docs`), so `export_pdf` can be called with any
  `doc_id` string and will simply forward it to Drive's export endpoint.

### `GoogleEmailProvider` (`google.py:424-462`)

- `_service` (`google.py:431-435`): same lazy-memoize-with-full-scope pattern
  as the other two classes.
- `send` (`google.py:437-462`):
  - Builds a `multipart/mixed` MIME message with `To`, `From` (from
    `self._settings.sender_email`, **not** the `to`/caller-controlled value
    — line 447), `Subject`, and a `Bcc` set to `sender_email` again (line
    449) — every sent email is unconditionally BCC'd to the sender address,
    with no way for a caller to opt out via this interface's parameters.
  - HTML body always attached (line 450); PDF attachment only if
    `attachment is not None` (lines 452-457), base64-encoded via
    `encoders.encode_base64` and a `Content-Disposition` header built with an
    **unescaped, unquoted `filename=`** (`f"attachment; filename=
    {attachment.filename}"`, line 456) — no sanitization of
    `attachment.filename` for header-breaking characters (e.g. a filename
    containing a `"` or newline). The filename passed in practice comes from
    `pipeline.py:321`:
    `f"Invoice_{invoice.invoice_number}_{student.name.replace(' ', '_')}.pdf"`
    — `student.name` is DB-sourced free text with spaces replaced but no
    other character filtering; this function performs no defense of its own
    against whatever characters `student.name` contains.
  - The full RFC822 message is base64url-encoded (line 459) and sent via
    `gmail.users().messages().send(userId="me", ...)` (line 460) — a single
    synchronous API call; any failure (auth, quota, malformed message)
    propagates uncaught as an exception, which is exactly what
    `email_pending_invoices`'s `try/except Exception` (`pipeline.py:327-337`)
    is written to catch per-invoice.
  - Returns `result["id"]` (line 461) — assumes the Gmail API response
    always contains an `"id"` key on success; not defensively checked
    (`KeyError` would propagate if it were ever missing, itself still caught
    by the caller's blanket `except Exception`).

---

## Callees (external / stdlib) and what each provider depends on them for

- `google.oauth2.credentials.Credentials` (`from_authorized_user_file`,
  `.valid`, `.expired`, `.refresh_token`, `.refresh`, `.to_json`) — trusted
  to correctly represent token validity/expiry state; `_oauth_credentials`'s
  branching (`google.py:130-137`) depends entirely on `.valid`/`.expired`/
  `.refresh_token` being accurate. No independent verification of these
  properties in this module.
- `google_auth_oauthlib.flow.InstalledAppFlow` — `run_local_server(port=0)`
  is trusted to complete the interactive OAuth dance and return valid,
  correctly-scoped `Credentials`; this call blocks on human interaction and
  local network binding, with no timeout applied by this module.
  `port=0` means an OS-assigned ephemeral port — not independently
  reconfigurable per environment from this module.
  Depended upon for: producing credentials that actually carry
  `OAUTH_SCOPES`-level access; nothing here re-checks the scopes on the
  returned object.
- `googleapiclient.discovery.build` — trusted to produce a resource object
  whose method chains (`.values().get()`, `.files().copy()`,
  `.documents().batchUpdate()`, `.users().messages().send()`, etc.) behave
  per the Google API Discovery document for the given API/version string.
  Every provider passes the *same* `Credentials` object (full-scope) to
  `build`, regardless of which API is being built — `build` itself does no
  further scope narrowing visible to this module.
- `googleapiclient.http.MediaIoBaseDownload` — trusted to correctly chunk a
  Drive export into `buf`; loop termination is entirely driven by this
  object's `done` flag with no independent size/time bound from
  `GoogleDocProvider.export_pdf`.
- `invoicing.templates.invoice_doc.build_replacements(data)` (`google.py:57`,
  called at line 408) — not read in this pass; `create_invoice_doc` depends
  on it to produce a `dict[str, str]` of `{tag: replacement}` pairs whose
  keys are literal strings expected to appear, case-sensitively, in the
  copied template document. Not verified here whether it can return an
  empty dict, `None` values, or tags containing regex-special characters
  that would matter to the Docs API's `containsText` matching (it's a plain
  substring match per Docs API semantics, not regex, so this is likely
  moot, but `build_replacements`' actual implementation was not inspected in
  this pass).

## Callers and what they depend on each provider for

- `cli._build_providers` (`cli.py:65-76`): depends on
  `settings.require_google_config()` (line 75) having been called
  immediately before constructing any `GoogleXProvider`, to guarantee (for
  every field it checks) that later `assert`s inside those constructors'
  lazy accessors won't fire — **except** `oauth_token_file`, which as noted
  above is never validated by `require_google_config`/`require_sheet_config`
  at all, so this dependency is only partially satisfied.
- `pipeline.sync_schedule_into_db` (`pipeline.py:54-117`): depends on
  `sheet.read_schedule()` returning a `ScheduleSnapshot` whose
  `students`/`lessons` lists are internally consistent enough to join on
  `first_name.lower()` (line 90, 97) — this join key is exactly what
  `GoogleSheetProvider`'s `_cell_index`/parsing produces, and exactly what
  the ambiguous-first-name `ValueError` in `_parse_blocked_schedule`
  (`google.py:339-348`) exists to protect against silently mis-joining. This
  function also depends on `sheet.set_identity_map` (called at line 93)
  actually being consumed by whatever `mark_lessons_billed` implementation
  the same provider instance later runs — true for `GoogleSheetProvider`
  (`self._identity_map`), a no-op dependency for `FakeSheetProvider` (stored
  but never read by its `mark_lessons_billed`).
- `pipeline.bill_period` (`pipeline.py:182-258`): depends on
  `docs.create_invoice_doc` returning a doc id *before* `db.insert_invoice`
  persists `doc_url=doc_id` (line 220-232) — if `create_invoice_doc` raises
  (e.g. `GoogleDocProvider`'s `batchUpdate` failure noted above), no
  `Invoice` row is written for that student and the loop's exception
  propagates uncaught out of `bill_period`, aborting the whole period's
  billing run (no per-student try/except at this layer, unlike
  `email_pending_invoices`'s per-invoice isolation). Also depends on
  `sheet.mark_lessons_billed` (line 256) for write-back, but — per the
  `GoogleSheetProvider` analysis above — a *partial* failure inside that
  call (individual unresolved refs) is invisible to `bill_period`; only a
  full API-level exception from `batchUpdate` would be visible (and would
  arrive after `db.mark_lessons_billed`/`db.insert_invoice` have already
  committed, since those DB writes happen earlier in the same loop iteration,
  lines 244-246, before `sheet.mark_lessons_billed` is called once at the
  end, line 255-256) — so a Sheet-write-back failure at this point cannot
  roll back the DB-side billing that already happened.
- `pipeline.email_pending_invoices` (`pipeline.py:268-342`): depends on
  `docs.export_pdf` and `email.send` for exactly one thing each — bytes and
  a message id — and treats *any* exception from either as equivalent
  ("failed, retry next run", lines 327-337), never distinguishing
  transient/permanent failure modes from either Google API or the fakes.
  Depends on `email.send` actually delivering to `to` (not independently
  verifiable from this module) before considering the invoice "emailed"
  (`db.mark_invoice_emailed`, line 339, called only after `email.send`
  returns without raising).

## Fake vs real: what each guarantees to callers

Both satisfy the same `Protocol` signatures, but differ in what "success"
means:

- **Fakes** guarantee zero network access (`fake.py:1-6`), deterministic
  synchronous responses, and only the failure modes a test explicitly wires
  up (`fail_export_doc_ids`, `fail_after`). They do not model rate limits,
  partial API failures, auth expiry, or Sheet-content ambiguity (no
  equivalent of the `_parse_blocked_schedule` collision `ValueError`
  exists in `FakeSheetProvider` at all, since it doesn't parse anything).
- **Google providers** guarantee nothing about latency, availability, or
  idempotency beyond what the underlying Google APIs themselves provide;
  they add one piece of independent logic not present in any Google API
  (the blocked-schedule parser and its collision detection,
  `google.py:287-358`) and one silent-degradation behavior not present in
  the fakes (unresolved `mark_lessons_billed` refs are dropped with only a
  log line, `google.py:242-249`). The module docstring's framing
  (`google.py:1-6`) that this module is reached "only when ...
  `Settings.require_google_config()` has already confirmed real credentials
  are configured" is accurate for `sheet_id`, the OAuth client secret file,
  the Drive folder, and both Doc templates — but not for `oauth_token_file`,
  which `require_google_config`/`require_sheet_config` never check
  (`config.py:82-109`), leaving `_oauth_credentials`'s
  `assert settings.oauth_token_file is not None` (`google.py:125`) as the
  only thing standing behind that particular field, and it fires with no
  descriptive message if violated.

## Open questions

- Is there any code path (migration script, ops runbook, deployment tooling)
  that sets `INVOICING_OAUTH_TOKEN_FILE` separately from
  `INVOICING_OAUTH_CLIENT_SECRET_FILE`, such that the former could be unset
  while the latter is set in a real deployment? If not, the gap in
  `require_sheet_config`/`require_google_config` (`config.py:82-109`) never
  manifests in practice — but nothing in `config.py` or `google.py` prevents
  it structurally.
- What does `invoicing.templates.invoice_doc.build_replacements` actually
  return — could it produce a tag that collides with literal document text
  outside the intended placeholder (given `matchCase: True` but plain
  substring `containsText` matching, `google.py:401-406`)? Not inspected in
  this pass.
- `_load_cached_token`'s pickle-migration fallback (`google.py:104-120`)
  deserializes arbitrary bytes from `oauth_token_file` via `pickle.load`
  whenever `from_authorized_user_file` raises `ValueError` on the same
  file — what governs the filesystem permissions/ownership of that file
  path in the actual deployment environment? Not visible from this module;
  the trust placed in the file's contents is total.
- Does anything ever call `GoogleSheetProvider.mark_lessons_billed` without
  a prior `read_schedule`/`set_identity_map` on the *same instance* — e.g.
  a future CLI command, a retry path, or test harness that constructs a
  fresh `GoogleSheetProvider` just to mark lessons billed? Every write-back
  ref would then silently resolve to `None` (log-warning-and-skip, entirely
  possible since `_identity_map`/`_cell_index` default to `{}`,
  `google.py:177-178`) with a `batchUpdate` never even attempted line
  258-259. Current `cli.py` call sites don't do this (`run` always syncs
  before billing when not dry-run, `cli.py:157-163`; `debug-parse-schedule`
  never calls `mark_lessons_billed` at all), but nothing at this layer
  prevents a future caller from doing so.
- Are the three `GoogleXProvider` instances built by `cli._build_providers`
  (`cli.py:76`) ever constructed/used across threads or async contexts (vs.
  the observed fully-synchronous, single-process CLI flow)? If so, the
  independent, unsynchronized read-then-write of `oauth_token_file` by up to
  three separate `_oauth_credentials` calls (one per provider instance) is
  an unguarded shared-file race — not observed as reachable in the current
  `cli.py`, which calls everything sequentially in one thread.
