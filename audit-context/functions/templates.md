# Function Micro-Analysis: invoice_doc.py and invoice_email.py

## File: src/invoicing/templates/invoice_doc.py

### `template_row_capacity(line_count: int) -> int`  (lines 24-25)

**Purpose**: Picks which of two fixed Google Doc template layouts (4-row
"short" or 8-row "long") an invoice needs, based on lesson-line count.

**Structure**: A single ternary: `line_count <= SHORT_TEMPLATE_ROWS` (4) picks
`SHORT_TEMPLATE_ROWS`, else `LONG_TEMPLATE_ROWS` (8).

**Invariant established**: return value is always one of `{4, 8}` — never
scaled to `line_count` itself.

**Unenforced assumption**: `line_count <= LONG_TEMPLATE_ROWS` (8). Nothing in
this function, or in `build_replacements` (line 35), caps the number of
lesson lines fed in. If `data.lines` has more than 8 entries, `build_replacements`
(see below) will still emit `<name9>`, `<rate9>`, etc., tags that don't exist
in either physical Google Doc template, so those replacements are silently
dropped by `replaceAllText` (confirmed at
`src/invoicing/providers/google.py:403-404`, which does a literal
`containsText` substring match per tag — a tag with no match in the doc is a
no-op, not an error). The function itself has no failure path; it always
returns a value. Where the 8-line cap on `data.lines` is supposed to be
enforced upstream is not visible from this file — open question below.

### `build_replacements(data: InvoiceDocData) -> dict[str, str]`  (lines 28-62)

**Purpose**: Flattens an `InvoiceDocData` DTO into the literal placeholder-tag
→ replacement-text pairs that `providers/google.py` turns into
`replaceAllText` batchUpdate requests against a Google Docs template.

**Inputs** (`InvoiceDocData`, `src/invoicing/providers/base.py:104-113`):
plain pydantic `BaseModel` with `str` fields for `parent_name`,
`parent_email`, `invoice_number`, `student_display_name`, and a nested
`InvoiceDocLine` list, each line having `day_of_week: str`, `date_str: str`,
`student_display_name: str`, `rate_cents: int`. None of these `str` fields
carry a pydantic `constr`/regex/length constraint — any string value (empty,
very long, containing "<", ">", newlines, or even substrings that happen to
look like other placeholder tags, e.g. a parent literally named
`<invID>`) passes model construction.

**What it does**:
- Builds a header dict with 10 fixed keys (lines 37-48), directly assigning
  `data.parent_name`, `data.parent_email`, `data.invoice_number`,
  `data.student_display_name` verbatim (no escaping/sanitizing — none is
  needed for a Docs `replaceAllText` textual replacement, since Google Docs
  is not an HTML or formula-interpreting sink for this API).
- `$<subtotal>`, `$<gst>`, `$<invoiceTotal>` keys bake a literal `$` into the
  search tag itself (documented at lines 6-13), relying on the physical
  template having `$<subtotal>` etc. as contiguous text — an out-of-file
  assumption about the Doc's static layout, not verifiable from this
  function.
- Loop at lines 50-54 emits 4 tags per line (`<dayOfWeek{i}>`, `<DD/MM{i}>`,
  `<name{i}>`, `<rate{i}>`) for every entry in `data.lines`, 1-indexed.
- Loop at lines 56-60 blanks out the *unused* row tags from
  `len(data.lines)+1` through `capacity` (4 or 8) — this is what keeps
  leftover template rows from showing stale/placeholder text when an invoice
  has fewer lines than the template's row capacity.

**Invariant established**: for `0 <= len(data.lines) <= capacity`, every row
tag in the chosen template (1..capacity) has *some* entry in the returned
dict — either real data or `""`. This invariant is what row-blanking exists
to provide; it holds only under the unenforced assumption noted above that
`len(data.lines) <= LONG_TEMPLATE_ROWS`.

**Untrusted-data flow into a generated document**: `parent_name`,
`parent_email`, `student_display_name`, and per-line `student_display_name`,
`day_of_week`, `date_str` are all free-form strings, ultimately sourced from
the scheduling Sheet / database (student and parent records — see
`SheetStudentRecord` in `providers/base.py:21-38`) rather than validated
input. They land as literal replacement text via `containsText` /
`replaceAllText` (`google.py:403-410`), which is a plain substring-match
text substitution, not a template/formula language — Google Docs does not
execute the replacement text, so there is no server-side templating-injection
sink here. The one thing this function does *not* control: if a value like
`data.parent_name` itself contains a substring identical to another
placeholder tag (e.g. a name of literally `<stuName>`), a naive
implementation of the caller could re-match against it on a second pass;
whether `google.py` builds one `batchUpdate` request per tag from a
single upfront dict (safe — Docs applies all requests against the original
document text, not against outputs of prior requests in the same batch,
per Docs API semantics) or iterates and re-scans matters for whether this
is exploitable — see open question.

**Assumption / where established**: rate/amount fields are integer cents
(`InvoiceDocLine.rate_cents: int`, `InvoiceDocData.subtotal_cents` etc.), so
`_fmt_cents` (below) never has to parse or sanitize a currency string —
formatting-injection into the numeric fields isn't reachable through this
function; the *type* enforces "is an int" at the pydantic-model boundary
(`base.py:97-113`), not this file.

### `_fmt_cents(cents: int) -> str`  (lines 65-66)

**Purpose**: Formats integer cents as `"$X.XX"`.

**Structure**: `f"${cents / 100:.2f}"` — pure, no branching, no I/O.

**Invariant**: for any `int` input the output matches `^\$-?\d+\.\d{2}$`
(sign present if `cents` negative). No bound on `cents` is enforced here; a
negative or absurdly large `cents` value (nothing in this function rejects
either) still formats without error — the string is arithmetically
consistent with the input, just not validated as "sensible for an invoice".
That upstream validation, if any, is outside this file.

---

## File: src/invoicing/templates/invoice_email.py

### Module setup (lines 22-32)

`_env` is a Jinja2 `Environment` with `autoescape=select_autoescape(["html"])`
(line 23-26) — autoescaping is active because the loaded template file is
named `invoice_email.html`, matching the `["html"]` extension list. This is
the load-bearing configuration for every `{{ var }}` interpolation in
`invoice_email.html` being HTML-escaped by default. `PLACEHOLDER_*` constants
(lines 30-32) are fixed, non-secret strings, not from user input.

### `render_invoice_email(...)`  (lines 35-84)

**Purpose**: Renders the full customer-facing invoice-email HTML body from
lesson/invoice/sender data.

**Parameters and their trust level** (all keyword-only): `personal_message`
(free text, ultimately operator-authored but see below), `student_display_name`
(str, from DB), `lessons: list[Lesson]`, `rate_cents: int`, `invoice_number: str`,
`invoice_date: date`, `totals: InvoiceTotals` (dataclass of three `int`
fields, `billing.py:63-67`), `sender_name`, `sender_email` (both `str`,
operator/settings-sourced per module docstring lines 4-8).

**What it does**:
- `template = _env.get_template("invoice_email.html")` (line 47) — loads the
  file this analysis also read directly (`invoice_email.html`); no dynamic
  template name, so no template-path injection surface.
- `sorted_lessons = sorted(lessons, key=lambda lesson: lesson.lesson_date)`
  (line 48) — pure reordering; `Lesson.lesson_date: date`
  (`src/invoicing/models.py:76`) is a typed field, not a raw string, so no
  string-comparison surprises.
- `lesson_rows` (lines 50-59): per-lesson dict with `day` derived from
  `DAY_NAMES[lesson.lesson_date.weekday()]` (always a valid index 0-6, since
  `date.weekday()` is guaranteed `0..6` — Python stdlib invariant, not this
  file's), `date_str` via `strftime("%d/%m")`, and `rate_str` via
  `_fmt_cents(rate_cents)` — a single `rate_cents` param applied to every row
  (not a per-lesson rate; the function has no per-lesson-rate input, so if
  lessons in one invoice had different rates that distinction is invisible
  in the rendered table — not this function's concern to validate, since
  `rate_cents` is the caller's chosen value).
- `period` (lines 61-67): built only if `sorted_lessons` is non-empty; empty
  list yields `""` — this is the one path where an empty `lessons` list is
  handled explicitly (no crash, no exception raised elsewhere in the
  function for an empty list either, since the list comprehension and
  `sorted()` both tolerate `[]`).
- `template.render(...)` (lines 69-83) passes every field as a Jinja
  variable. **`message_html` is the one field passed as `Markup`**
  (constructed by `_build_message_html`, line 71) — Jinja does not
  autoescape a `Markup` instance (that is the entire point of `Markup`); all
  other fields (`business_name`, `student_display_name`, `period`,
  `invoice_date_str`, `invoice_number`, `lesson_rows` dict values, `total_str`,
  `sender_name`, `sender_email`, `bsb`, `account_number`, `bank_name`) are
  plain `str`/`int` values and get Jinja's default HTML-escaping when
  interpolated into `invoice_email.html` (confirmed autoescape config,
  above). `sender_email` is additionally interpolated inside an HTML
  attribute (`href="mailto:{{ sender_email }}"`,
  `invoice_email.html:116`) — Jinja's HTML autoescaping escapes `"`, `<`,
  `>`, `&`, `'`, which is sufficient to prevent breaking out of a
  double-quoted attribute, though it does not contextually re-encode for a
  `mailto:` URI scheme specifically (e.g. it would not strip a `?cc=` style
  payload smuggled into the address — that's a mailto-semantics question,
  not an HTML-escaping one).

**Callee dependency — `_build_message_html`**: `render_invoice_email` depends
on this callee to have *already* HTML-escaped `personal_message` before
wrapping it in `Markup`, precisely because passing raw `Markup` bypasses
Jinja's own escaping. This is the one place in the whole render path where
if the callee failed to escape, autoescape would not catch it downstream —
see callee analysis.

**Return**: `rendered: str` — the caller (`pipeline.py:308-318`) uses this as
the email's `html_body` sent via `EmailProvider.send` (`providers/base.py:134-141`),
an unbounded string with no further sanitization station between this
function and the outbound email API.

### `_build_message_html(personal_message: str) -> Markup`  (lines 87-92)

**Purpose**: Escapes each line of free-text `personal_message`, then joins
with literal `<br>` tags, producing a `Markup`-wrapped HTML fragment safe to
interpolate unescaped into the template.

**Structure**:
```
escaped_lines = [str(escape(line)) for line in personal_message.splitlines()]
return Markup("<br>".join(escaped_lines))
```
`escape` is `markupsafe.escape` (imported line 17) — escapes `&`, `<`, `>`,
`"`, `'`. Applied to every line *individually* before the `<br>` join, so the
only unescaped bytes in the final `Markup` are the literal `<br>` separators
this function itself inserts — `personal_message` content can never
contribute a raw `<br>` or any other tag, since `splitlines()` only splits on
newline-family characters and each resulting piece is escaped before
rejoining.

**Invariant established**: the returned `Markup` contains no HTML markup
that did not originate from this function's own `"<br>".join(...)` — every
byte traceable to `personal_message` has passed through `escape()`. This is
what makes it safe for the caller to hand it to Jinja as pre-escaped
`Markup` (bypassing autoescape) at line 71.

**Upstream reliance**: `personal_message` reaching this function has already
had `<student>`/`<parent>` placeholder tokens substituted with
`student.name.split()[0]` / `(parent.name or student.name).split()[0]`
*before* this call (`pipeline.py:305-309`) — i.e. **substitution happens
before escaping**, not after. That ordering is what makes student/parent
names (themselves free-form DB strings, `Student.name: str`,
`models.py`, no length/charset constraint found) safe from HTML injection
here: whatever HTML metacharacters a name contains get escaped along with
the rest of the resolved message when `_build_message_html` runs on the
already-substituted string. If a future caller substituted *after* escaping
instead (escaping `personal_message` first, then interpolating raw names
into the placeholders), that would reintroduce an HTML-injection path
through student/parent names — this function has no way to detect or
prevent that misordering; it only escapes what it is given, in the order
its caller assembled it.

**No caller in this file passes a `personal_message` with either
placeholder pre-resolved a second time** — `_build_message_html` itself does
no placeholder substitution and doesn't need to.

### `_fmt_cents(cents: int) -> str`  (lines 95-96)

Identical logic to `invoice_doc.py`'s `_fmt_cents` (independent copy, not
shared) — see that analysis; same invariant, same lack of bound-checking on
`cents`.

---

## Cross-cutting observations

- **Two independent sinks, two independent trust models.** `invoice_doc.py`
  feeds a non-executing text-substitution API (Google Docs `replaceAllText`)
  — no HTML/script/formula injection surface exists there because the sink
  doesn't interpret the replacement text as markup or formulas. `invoice_email.py`
  feeds an HTML email body rendered by Jinja2 with autoescaping on, plus one
  deliberate escape-then-`Markup` bypass (`_build_message_html`) that is
  correctly ordered relative to placeholder substitution in its only caller.
- **No length or character-class validation observed anywhere in this file
  pair** for `parent_name`, `student_display_name`, `sender_name`,
  `sender_email`, `invoice_number`, or lesson `day_of_week`/`date_str` — all
  plain `str` pydantic/dataclass fields (`InvoiceDocData`, `InvoiceDocLine`
  in `providers/base.py:97-113`; `Student`, `Lesson` in `models.py`). Any
  size or content bound these need is enforced (or not) further upstream,
  outside both files analyzed here.
- **The 8-row template-capacity assumption** (`invoice_doc.py`
  `template_row_capacity`, lines 24-25) is the one place a numeric bound
  matters and nothing in either file enforces `len(data.lines) <= 8` before
  `build_replacements` runs.

## Open questions

- Where (if anywhere) is `len(data.lines)` capped before `build_replacements`
  is called? Not visible in `invoice_doc.py`; needs tracing through whatever
  assembles `InvoiceDocData` (likely `pipeline.py`) to confirm lesson counts
  per invoice period can't exceed 8, or to confirm silent tag-drop on
  overflow (rows 9+ never rendered, but their $-amounts are already summed
  into `subtotal_cents`/`total_cents` — i.e. an invoice could show a total
  that doesn't reconcile with the visible line rows if this cap is ever
  exceeded).
- Does `providers/google.py`'s batchUpdate build all `replaceAllText`
  requests from the *original* `build_replacements` dict in one request
  array against the unmodified document (Docs API's documented behavior —
  requests within one `batchUpdate` apply against the document state as of
  the *start* of that call, not chained against each other's output), or
  does it loop and re-fetch/re-scan between individual replacements? If the
  latter, a value like a parent name containing another tag's literal text
  (e.g. `<invID>`) could be re-substituted on a later iteration. Only
  `google.py` can answer this; not inspected in depth for this analysis.
- Is there any length cap on `personal_message`, `parent_name`, or
  `student_display_name` upstream (CLI input, DB schema, pydantic
  `constr`)? None found in the two files analyzed or in `providers/base.py`.
