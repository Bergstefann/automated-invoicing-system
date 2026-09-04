# `src/invoicing/config.py` and `src/invoicing/models.py`

---

## `Settings` (pydantic model) in src/invoicing/config.py (L18-L109)

**Purpose:** Central configuration object for the pipeline. Holds all environment-derived values with
defaults that make demo mode work with zero configuration, while deferring validation of the Google-specific
fields to explicit `require_*` calls so that non-Google code paths (parsing, demo, tests) never need real
credentials. Comment at L1-L6 states this design intent directly.

**Inputs & Assumptions:**
- All fields have defaults (L19-L35); none are required at construction. Trust: values populated via
  `from_env` come from environment variables (semi-trusted — process environment, not attacker input over a
  network boundary, but influenced by `.env` file contents and deployment configuration).
- `term_start` (str): validated at construction by `_validate_term_start` (L37-L41) via
  `date.fromisoformat(v)`, which raises `ValueError` (pydantic surfaces as `ValidationError`) on malformed
  input. This is the only field-level validator; nothing similarly validates `sheet_id`, the OAuth file paths,
  or the Drive/Doc IDs for shape (e.g. no check that `oauth_client_secret_file` exists, or that IDs are
  non-empty strings vs. paths).
- `db_path`, `oauth_client_secret_file`, `oauth_token_file` are `Path` values. Nothing in this class checks
  that they are within any particular directory or that they don't point outside the project (path
  traversal is not this class's concern, but nothing here forecloses it either — see Open Questions).

**Outputs & Effects:** No side effects from construction itself (no I/O beyond what pydantic does).

---

### `Settings._validate_term_start` (L37-L41)

**Purpose:** Enforces that `term_start` is a valid ISO date string at construction time, since `term_start`
is stored as `str` (not `date`) but is later parsed by `term_start_date` (L44-L45) and consumed throughout
billing (`period_bounds`, `period_number_for_date` in billing.py) as a `date`.

**Block-by-Block:**
```python
# L39-L41
def _validate_term_start(cls, v: str) -> str:
    date.fromisoformat(v)
    return v
```
- **What:** Parses `v` and discards the result, returning the original string unchanged.
- **Why here:** Runs at model construction (both `from_env` and `for_demo`/direct `Settings(...)` calls),
  so any invalid `INVOICING_TERM_START` fails fast rather than at first billing-period computation.
- **Assumes:** `date.fromisoformat` is a sufficient validity check for downstream use of `term_start_date`.
- **Establishes:** `self.term_start` is always re-parseable by `date.fromisoformat` — i.e. `term_start_date`
  (L44-L45) can never raise due to malformed input, only `KeyError`-style issues if the field were somehow
  mutated post-construction (it can't be: `Settings` is a plain `BaseModel`, not frozen, so in principle
  `settings.term_start = "bad"` after construction would bypass this validator — pydantic v2 does not
  re-validate on attribute assignment unless `validate_assignment=True` is set in `model_config`, and no
  `model_config` is set on `Settings` at all, L18).
- **Depended on by:** `term_start_date` property (L44-L45), and transitively every caller of
  `period_bounds`/`period_number_for_date` in billing.py that passes `settings.term_start_date`.

**Open Questions:**
- unclear; need to inspect whether any code path mutates `settings.term_start` after construction (e.g. in
  tests or the CLI) — if so, the re-parseability invariant established here would not hold at the mutation
  site, since `Settings` has no `model_config` enabling `validate_assignment`.

---

## `Settings.term_start_date` in src/invoicing/config.py (L43-L45)

**Purpose:** Provides the `date` form of `term_start` for billing-period arithmetic, which operates on
`datetime.date` (billing.py `period_bounds`, `period_number_for_date`).

**Inputs & Assumptions:**
- Implicit: `self.term_start`. Precondition: parseable by `date.fromisoformat`. Established by
  `_validate_term_start` (L37-L41) at construction time, assuming no post-construction mutation (see above).

**Outputs & Effects:** Returns a freshly parsed `date` each call (not cached); no state mutation.

**Cross-Function Dependencies:**
- Callee `date.fromisoformat` (external, stdlib): can raise `ValueError` if `term_start` were ever invalid
  despite the constructor-time check — the only way to reach that is the mutation path noted above.

---

## `Settings.from_env` in src/invoicing/config.py (L47-L76)

**Purpose:** Builds a `Settings` instance from environment variables (with an `.env` file merged in first),
providing the "real" configuration path used by every CLI invocation that isn't `--demo` (`cli.py:62`).

**Inputs & Assumptions:**
- Implicit: process environment (`os.environ`), and any `.env` file discoverable by `load_dotenv()` (L52).
  Trust: semi-trusted — whoever controls the environment/`.env` file controls every `Settings` field,
  including which OAuth token/secret file paths are loaded and which Drive/Doc IDs are targeted.
- `load_dotenv()` (L52) is documented (L49-L51) as walking up from this file's directory to find `.env`,
  and as a no-op if none is found — it does not raise. This is an assumption about `python-dotenv`'s
  behavior, not verified in this file (external library).
- Each field is read via the `env`/`opt_env`/`opt_path` closures (L54-L62): `env` returns a string default
  when the variable is unset; `opt_env` returns `None` for unset **or empty-string** values (`or None` at
  L58) — so `INVOICING_SHEET_ID=""` is indistinguishable from unset. `opt_path` wraps `opt_env` the same way.
- No validation of URL-like or ID-like shape for `sheet_id`, `drive_output_folder_id`,
  `doc_template_short_id`, `doc_template_long_id` beyond "non-empty string or None" — any string reaching
  here is passed through to `Settings(...)` construction and later to Google API calls
  (`providers/google.py`, not analyzed here) unchecked.

**Outputs & Effects:** Returns a new `Settings`. No mutation of environment; `load_dotenv()` does mutate
`os.environ` as a side effect of the call (adds variables from `.env` if not already present — dotenv's
default behavior does not override existing env vars, per its documented semantics, not verified here).

**Block-by-Block:**
```python
# L64-L76
return cls(
    db_path=Path(env("DB_PATH", "invoicing.db")),
    ...
)
```
- **What:** Constructs `Settings` from the closures' outputs.
- **Assumes:** the field-level validator on `term_start` (L37-L41) will catch a malformed
  `INVOICING_TERM_START`; nothing here pre-validates it.
- **Establishes:** a fully-populated `Settings`, but with Google-specific fields potentially `None` — by
  design, per the module docstring (L1-L6).
- **Depended on by:** `_build_providers` in cli.py (L65-L76), which calls `require_google_config()` before
  constructing real Google providers when not in demo mode.

**Cross-Function Dependencies:**
- Callee `load_dotenv` (external-black-box, `dotenv` package): assumed not to raise on missing `.env`
  (stated as intentional at L49-L51) and assumed not to override already-set real environment variables.
  Neither is verified in this codebase.
- Callers: `cli._load_settings` (`cli.py:62`) — calls `from_env()` only when `--real`/not `--demo`; the
  resulting `Settings` then has `require_google_config()` called on it (`cli.py:75`) before any Google
  provider is constructed. `debug-parse-schedule`-style commands (referenced in config.py's docstring at
  L86-L89) instead call only `require_sheet_config()`.

**Open Questions:**
- unclear; need to inspect `providers/google.py` for whether `sheet_id`/`drive_output_folder_id`/template
  IDs are used in a way that assumes any particular format (e.g. Google Drive ID charset) beyond
  non-emptiness — `require_google_config` only checks truthiness (L107), not shape.

---

## `Settings.for_demo` in src/invoicing/config.py (L78-L80)

**Purpose:** Produces a `Settings` for `--demo` mode: separate DB file (`demo.db`), `demo=True`, and every
Google-related field left at its `None`/default — by construction, no `require_google_config()` call is ever
reachable on this instance in the current call graph (cli.py never calls it when `demo=True`, per
`_build_providers` L68-L73 taking the `demo` branch first).

**Inputs & Assumptions:** None beyond the class defaults.

**Outputs & Effects:** Returns a new `Settings(db_path=Path("demo.db"), demo=True)`. Note: this bypasses
`from_env` entirely — `.env`/environment variables have **no effect** on a demo-mode run's `db_path`,
`term_start`, sender identity, etc.; every non-Google field falls back to the class-level default (e.g.
`term_start="2026-02-02"`, `sender_name="Jane Tutor"`), not whatever might be set in the environment.

**Cross-Function Dependencies:**
- Callers: `cli._load_settings` (`cli.py:62`), selected when `demo=True`.

---

## `Settings.require_sheet_config` in src/invoicing/config.py (L82-L97)

**Purpose:** Narrow precondition check for Sheet-reading operations (e.g. `debug-parse-schedule`, per the
docstring L86-L89) that need a sheet ID and OAuth client secret but not Drive/Doc output configuration.

**Inputs & Assumptions:**
- Implicit: `self.sheet_id`, `self.oauth_client_secret_file`.

**Outputs & Effects:** Raises `ValueError` listing every missing-and-falsy required field (L95-L97); returns
`None` (no exception) otherwise. Pure validation — no state mutation.

**Block-by-Block:**
```python
# L91-L97
required: dict[str, str | Path | None] = {
    "INVOICING_SHEET_ID": self.sheet_id,
    "INVOICING_OAUTH_CLIENT_SECRET_FILE": self.oauth_client_secret_file,
}
missing = [name for name, value in required.items() if not value]
if missing:
    raise ValueError(...)
```
- **What:** Checks both fields for truthiness (`not value`), not for validity — a `Path("")` or a `sheet_id`
  containing only whitespace would need to be individually falsy to be caught; a syntactically-invalid but
  non-empty sheet ID or a nonexistent file path passes this check.
- **Assumes:** "non-empty/non-None" is a sufficient proxy for "usable configuration". Does not check that
  `oauth_client_secret_file` exists on disk or is readable — that failure, if it occurs, surfaces later
  wherever the OAuth flow actually opens the file (`providers/google.py`, not analyzed here).
- **Establishes:** on successful return, `self.sheet_id` and `self.oauth_client_secret_file` are both
  truthy. This is the entirety of what "sheet config is present" means to this codebase.
- **Depended on by:** `require_google_config` (L101, calls this first) and directly by `cli.py:218`.

**Open Questions:**
- unclear; need to inspect `cli.py` around L218 to see what command calls `require_sheet_config()` alone and
  whether that command later dereferences `oauth_token_file` (which is *not* checked by either
  `require_sheet_config` or `require_google_config`) — if `oauth_token_file` is `None` at that point, the
  failure mode downstream is unclear from this file.

---

## `Settings.require_google_config` in src/invoicing/config.py (L99-L109)

**Purpose:** Full precondition check for live Google operations (Sheets + Docs + Drive), called once before
constructing real Google providers (`cli.py:75`), and — per its own docstring (L100) — never called in
`--demo` mode.

**Inputs & Assumptions:**
- Implicit: `self.drive_output_folder_id`, `self.doc_template_short_id`, `self.doc_template_long_id`, plus
  everything `require_sheet_config` checks (delegated at L101).
- Precondition assumed by the docstring: "Never called in --demo mode." Nothing in this function enforces
  that — it is enforced only by the caller (`cli._build_providers`, L68-L76, which branches on `demo` before
  reaching L75). If some other call site invoked this on a demo `Settings` (all Google fields `None` by
  `for_demo`, L80), it would correctly raise — so the *consequence* of violating the stated precondition is
  just an exception, not silent wrong behavior. The precondition is more a documented intent than something
  whose violation is unsafe.

**Outputs & Effects:** Raises `ValueError` (via `require_sheet_config` at L101, or directly at L108-L109)
listing all missing fields across both the sheet and the Drive/Doc groups if `require_sheet_config` doesn't
raise first — note L101 raises immediately and independently if the sheet-level fields are missing, so a
caller with both sheet and Drive fields missing sees only the sheet-level error message, not a combined one.

**Block-by-Block:**
```python
# L101
self.require_sheet_config()
```
- **What:** Delegates the sheet/OAuth-secret check.
- **Establishes:** if this doesn't raise, `sheet_id` and `oauth_client_secret_file` are truthy.
- **Depended on by:** the remainder of this function only reaches L102-L109 if this doesn't raise — so a
  caller catching only around `require_google_config()` gets one exception per call, from whichever group
  fails first (sheet-level always checked first).

```python
# L102-L109
required = {
    "INVOICING_DRIVE_OUTPUT_FOLDER_ID": self.drive_output_folder_id,
    "INVOICING_DOC_TEMPLATE_SHORT_ID": self.doc_template_short_id,
    "INVOICING_DOC_TEMPLATE_LONG_ID": self.doc_template_long_id,
}
missing = [...]
if missing:
    raise ValueError(...)
```
- **What:** Same truthiness-only check as `require_sheet_config`, applied to the three Drive/Doc fields.
- **Assumes:** same as above — non-empty string/Path stands in for "valid ID".
- **Establishes:** on full success (no raise anywhere in the call), all five Google-related fields
  (`sheet_id`, `oauth_client_secret_file`, `drive_output_folder_id`, `doc_template_short_id`,
  `doc_template_long_id`) are truthy. **Notably absent from both this and `require_sheet_config`:**
  `oauth_token_file` (L26) is never validated by either method, despite being a `Settings` field alongside
  `oauth_client_secret_file`.

**Cross-Function Dependencies:**
- Callee `require_sheet_config` (internal, this file, L82-L97): read above; establishes the sheet-level
  half of the postcondition, on the path where it doesn't raise.
- Callers: `cli._build_providers` (`cli.py:75`), called only on the non-demo branch, immediately before
  constructing `GoogleSheetProvider`/`GoogleDocProvider`/`GoogleEmailProvider`
  (`cli.py:76`). Those providers' constructors are assumed (by this call ordering) to be able to rely on
  the five checked fields being present — `providers/google.py:5` states this directly ("`Settings.require_google_config()` has already confirmed real credentials").

**Open Questions:**
- unclear; need to inspect `providers/google.py` to confirm none of the three Google provider constructors
  or their methods dereference `oauth_token_file` in a way that assumes it is non-`None` — since neither
  `require_sheet_config` nor `require_google_config` checks it, a `None` `oauth_token_file` in real mode
  would fail only wherever that provider code first touches it (if at all — it may be legitimately optional,
  e.g. auto-created on first OAuth flow).

---

# src/invoicing/models.py

## `AttendanceStatus`, `SheetStatus` (StrEnum) in src/invoicing/models.py (L15-L32)

**Purpose:** `AttendanceStatus` is the in-memory/domain representation of what happened at a lesson;
`SheetStatus` is the wire representation written back to the human-facing Google Sheet. The module docstring
(L1-L4) and the `SheetStatus` docstring (L26-L28) establish these as deliberately distinct vocabularies:
`Y`/`YI`/`N` on the Sheet side is described as "preserved verbatim from the original spreadsheet-as-database
design," implying an external/legacy constraint on `SheetStatus`'s values rather than free choice.

**Invariants:** Both are `StrEnum` (stdlib), so instances compare equal to their string values. Nothing in
this file constrains what happens if a raw string from the Sheet doesn't match any `SheetStatus` member —
that conversion (`schedule_contract.py`/`pipeline.py`, not analyzed here) is out of scope for this file but
is a boundary this enum sits at.

---

## `attendance_to_sheet_status` in src/invoicing/models.py (L35-L38)

**Purpose:** Maps a domain `AttendanceStatus` plus a `billed` flag to the `SheetStatus` written back to the
spreadsheet — the single place this mapping's logic lives, per the "preserved verbatim" framing above.

**Inputs & Assumptions:**
- `status` (AttendanceStatus): Trust: caller-supplied, assumed already a valid enum member (Python's type
  system, not runtime-checked here since there's no isinstance guard).
- `billed` (bool, keyword-only per `*`): caller-supplied; this function has no way to verify it reflects
  reality (e.g. that a corresponding `Invoice`/`billed_invoice_id` actually exists) — it trusts the caller's
  claim entirely.

**Outputs & Effects:** Pure function, returns a `SheetStatus`. No I/O, no mutation.

**Block-by-Block:**
```python
# L36-L38
if status is not AttendanceStatus.ATTENDED:
    return SheetStatus.NOT_BILLABLE
return SheetStatus.INVOICED if billed else SheetStatus.UNBILLED
```
- **What:** Any non-`ATTENDED` status (i.e. `ABSENT_NOTIFIED` or `ABSENT_UNNOTIFIED`) collapses to
  `NOT_BILLABLE` regardless of `billed`; only `ATTENDED` distinguishes `INVOICED` vs `UNBILLED`.
- **Assumes:** `billed=True` is never passed alongside a non-`ATTENDED` status in a way that should matter —
  since the non-`ATTENDED` branch ignores `billed` entirely, a caller that computed `billed=True` for an
  absent lesson gets silently overridden to `NOT_BILLABLE` with no error. Whether that's ever attempted is a
  caller-side question (see Open Questions).
- **Establishes:** the returned `SheetStatus` faithfully encodes "is this attended-and-invoiced,
  attended-and-not-yet-invoiced, or not billable at all" — but *only* if the caller's `billed` argument is
  itself correct. This function establishes no independent truth about billing state; it only relabels
  whatever the caller asserts.

**Open Questions:**
- unclear; need to inspect callers (likely in `pipeline.py`, given `pre_billed=lesson_record.status ==
  SheetStatus.INVOICED.value` at `pipeline.py:114`) to see what value of `billed` is actually passed and
  whether it derives from `Lesson.is_unbilled`/`billed_invoice_id` or from a separately-tracked flag that
  could drift from the `Lesson` row's own state.

---

## `_require_tz_aware` in src/invoicing/models.py (L41-L44)

**Purpose:** Shared validator used by both `Invoice.issued_at` and `Invoice.emailed_at` field validators to
enforce the module-level invariant stated at L4: "datetime.date/datetime.datetime (timezone-aware) in
memory."

**Inputs & Assumptions:**
- `value` (datetime): Trust: whatever was passed to `Invoice(...)` construction — could originate from a
  DB row reconstruction (`db.py`), a freshly-computed `datetime.now(UTC)`-style call, or a test.

**Outputs & Effects:** Returns `value` unchanged if `value.tzinfo is not None`; raises `ValueError`
otherwise. Pydantic wraps this as part of `ValidationError` at `Invoice` construction.

**Block-by-Block:**
```python
# L42-L44
if value.tzinfo is None:
    raise ValueError("datetime must be timezone-aware")
return value
```
- **What:** Rejects naive datetimes.
- **Assumes:** a non-`None` `tzinfo` implies a *correct* timezone (e.g. actually UTC, not some other
  offset) — this only checks awareness, not which timezone. Nothing here normalizes to UTC or checks
  `value.utcoffset() == timedelta(0)`; two `Invoice`s could legitimately hold `issued_at` values in different
  offsets and this validator accepts both.
- **Establishes:** `value.tzinfo is not None` for whichever field called it.
- **Depended on by:** `Invoice._issued_at_tz_aware` (L123-L126) and `Invoice._emailed_at_tz_aware`
  (L128-L131) — and transitively, any code that computes duration/ordering across `Invoice.issued_at` values
  assuming they're all comparably timezone-aware and in the same zone.

**Open Questions:**
- unclear; need to inspect where `Invoice.issued_at`/`emailed_at` are constructed (`pipeline.py`, `db.py`)
  to confirm they consistently use `datetime.now(UTC)` or equivalent — this validator would silently accept
  a timezone-aware-but-non-UTC value (e.g. local time with an offset) if one were ever passed.

---

## `Model` in src/invoicing/models.py (L47-L48)

**Purpose:** Common base for every domain model, setting `frozen=True` via `ConfigDict` — establishes
immutability for `Parent`, `Student`, `Lesson`, `BillingPeriod`, `InvoiceLine`, `Invoice` uniformly. This is
the mechanism (not `Settings`, which has no such config — see above) that guarantees domain objects can't be
mutated after construction; any "update" in this codebase must be a `model_copy(update=...)`-style
reconstruction or a fresh object, not in-place mutation. No code in this file does such a copy, so that
pattern (if used) lives in callers (`db.py`, `pipeline.py`, `billing.py`).

**Invariants:** All subclasses are frozen pydantic models — hashable (pydantic frozen models are), safe to
use as dict keys or in sets, and safe to share across the "pure" computation functions in `billing.py`
without aliasing-mutation concerns.

---

## `Parent`, `Student`, `Lesson`, `BillingPeriod`, `InvoiceLine`, `Invoice` (data classes) in src/invoicing/models.py (L51-L132)

**Purpose:** Row-shaped domain records. `id: int | None = None` on each is the DB-assigned primary key,
`None` before persistence — this is a convention every one of these six classes follows identically
(L52, L58, L74, L97, L104, L111).

**Field-level invariants enforced by pydantic (not custom validators):**
- `Student.rate_cents`, `InvoiceLine.rate_cents`, `Invoice.subtotal_cents`/`gst_cents`/`total_cents`: all
  `Field(ge=0)` (L68, L107, L117-L119) — non-negative cents, enforced at construction, raises
  `ValidationError` otherwise. Nothing enforces `subtotal_cents + gst_cents == total_cents` at the model
  level — that arithmetic relationship, if it must hold, is established (or not) wherever `Invoice` is
  constructed (`billing.compute_totals`, not in this file) and is **not** a `Model`-level invariant.
- `Lesson.duration_minutes`: `Field(gt=0)` (L77) — strictly positive.
- `BillingPeriod.period_number`: `Field(ge=1)` (L98).
- No cross-field validators anywhere in this file (e.g. nothing checks `BillingPeriod.start_date <=
  end_date`, nothing checks `Invoice.subtotal_cents <= total_cents`).

**Student.student_id (L64):** Comment (L59-L63) documents this as assigned once by
`Database.insert_student`, never reassigned, and `None` only for not-yet-persisted `Student` instances (e.g.
built for `compute_totals` in billing.py, which takes a `Student` but only reads `rate_cents` — grep confirms
`compute_totals` signature at `billing.py:70`). This file only declares the type (`str | None`); the
"assigned once, sequential, zero-padded, prefixed" invariant is entirely established elsewhere
(`db.py`, per the comment) — **nothing in this file enforces the format** (`S-0001`-style) even though the
comment asserts it.

---

## `Lesson.is_billable` in src/invoicing/models.py (L87-L89)

**Purpose:** Single source of truth for "did this lesson generate revenue" — `True` iff
`attendance_status is AttendanceStatus.ATTENDED`.

**Block-by-Block:**
```python
# L88-L89
def is_billable(self) -> bool:
    return self.attendance_status is AttendanceStatus.ATTENDED
```
- **Assumes:** `attendance_status` is always one of the three `AttendanceStatus` members (guaranteed by
  pydantic validation on the enum-typed field at construction, L78).
- **Establishes:** `is_billable` is derived, not stored — cannot drift from `attendance_status` since it's a
  computed property on a frozen model.

---

## `Lesson.is_unbilled` in src/invoicing/models.py (L91-L93)

**Purpose:** Determines whether a lesson should be picked up by billing — consumed directly by
`billing.unbilled_lessons_in_period` (`billing.py:50`) as the sole "is this lesson eligible" predicate,
combined there with a date-range check.

**Block-by-Block:**
```python
# L92-L93
def is_unbilled(self) -> bool:
    return self.is_billable and self.billed_invoice_id is None and not self.pre_billed
```
- **What:** Three-way AND: attended, no invoice ID recorded, and not flagged as pre-billed by the legacy
  pipeline.
- **Assumes:** `billed_invoice_id is None` and `pre_billed is False` are both reliable signals that no
  invoice/pre-existing bill covers this lesson. Both are plain stored fields on a frozen model — their
  truthfulness rests entirely on whoever constructs/updates the `Lesson` (`db.py` row-to-model mapping at
  `db.py:483-484`, and whatever sets `billed_invoice_id` via the `UPDATE lessons SET billed_invoice_id = ?`
  statement noted at `db.py:338`). This property itself establishes nothing about *why* those two fields are
  correct — it only combines them.
- **Establishes:** the eligibility predicate consumed by `billing.unbilled_lessons_in_period` (`billing.py:
  46-51`), which is described there (via `billing.py`'s pipeline usage, per `pipeline.py:193`'s comment "a
  lesson already carrying a `billed_invoice_id` is excluded") as the idempotency mechanism preventing
  double-billing across pipeline runs.
- **Depended on by:** every downstream consumer of "unbilled lessons" — ultimately, the set of lessons that
  get included in a new invoice. If `pre_billed` or `billed_invoice_id` were ever wrong (stale, or set out
  of order relative to a crash/retry), this property would silently produce the wrong billing set with no
  internal check against, e.g., an `InvoiceLine` table.

**Open Questions:**
- unclear; need to inspect `db.py:338`'s `UPDATE lessons SET billed_invoice_id = ?` call site and its
  transactional context (is it in the same transaction as the `Invoice`/`InvoiceLine` insert that creates
  the invoice being pointed to?) — if not atomic, a crash between invoice creation and this update could
  leave a lesson `is_unbilled` (eligible for re-billing) despite an `Invoice` already existing for it, or
  vice versa. This file only defines the predicate; the atomicity question lives in `db.py`/`pipeline.py`.
