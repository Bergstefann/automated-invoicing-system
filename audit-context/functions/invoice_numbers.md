# `src/invoicing/invoice_numbers.py`

## `next_invoice_number(issued_on: date, already_issued_today: int) -> str`

### Signature and role

Pure function, no I/O, no state. Takes a `date` and an integer count of invoices
already issued on that date, and returns a formatted invoice number string
`DDMMYY` + two-digit sequence (e.g. `"15082601"`). Defined at
`src/invoicing/invoice_numbers.py:20-24`.

### Logic

```
seq = already_issued_today + 1                                   # line 21
if seq > MAX_DAILY_SEQUENCE:                                      # line 22 (MAX_DAILY_SEQUENCE = 99, line 17)
    raise ValueError(...)                                         # line 23
return f"{issued_on.strftime('%d%m%y')}{seq:02d}"                 # line 24
```

Single straight-line branch: either seq is `1..99` and a formatted string is
returned, or seq is `>= 100` and a `ValueError` is raised. There is no path
that returns a malformed or unvalidated string — `seq` is always in `1..99`
on the success path, so `{seq:02d}` is always exactly two digits.

### Invariants (established purely inside this function)

- Returned string is exactly `len(strftime('%d%m%y')) + 2 == 8` characters,
  first 6 digits are the caller-supplied `issued_on` formatted as `DDMMYY`,
  last 2 digits are `seq` zero-padded — established by line 24, unconditional
  on the success path.
- `seq` (and therefore the numeric suffix) is in the range `1..99` on every
  successful return — enforced by the `seq > MAX_DAILY_SEQUENCE` check at
  line 22 raising before line 24 can execute with a 3-digit `seq`.
- Monotonic-in-argument: for fixed `issued_on`, increasing
  `already_issued_today` by 1 increases the numeric suffix by 1 (up to the
  99 ceiling). This is a pure arithmetic consequence of line 21, not
  independently enforced elsewhere.

### Assumptions this function makes on its inputs, and what (if anything) establishes them

- **`already_issued_today` accurately reflects the number of invoices already
  issued for `issued_on` at the moment the caller is about to mint the next
  one.** Nothing in this function checks or can check this — it is a bare
  `int` with no provenance information attached. The function trusts the
  caller completely. This is the crux of the uniqueness property described in
  the module docstring (lines 1-10): the module docstring explicitly claims
  the new scheme "can't collide because the count is read from the database,"
  which is a claim about the *caller's* behavior, not something this function
  itself guarantees.
- **`already_issued_today >= 0`.** Not checked. A negative value would produce
  `seq <= 0`, and `{seq:02d}` would render as e.g. `"-1"` or `"00"` /
  `"0"`-prefixed oddities without raising (since the `> MAX_DAILY_SEQUENCE`
  check only bounds the top, not the bottom). Nothing in this function or
  visible in its two call sites (both pass `db.count_invoices_issued_on(...)`,
  which returns `int(row["n"])` from a SQL `COUNT(*)`, always `>= 0`) allows
  a negative value in practice, but the function itself does not defend
  against it.
- **`issued_on` is a genuine calendar date whose `strftime('%d%m%y')` is
  stable and collision-free across the intended usage window.** Not checked;
  assumed to hold for any `date` object (true by construction of the `date`
  type). No explicit bound on how far in the past/future `issued_on` can be —
  the module only cares that two different `date` values it is invoked with
  produce different 6-digit prefixes, which is guaranteed by `date` semantics
  as long as invoices aren't issued more than ~100 years apart on dates that
  alias under `%d%m%y` (century wraparound is a `date`-level concern, out of
  scope for this function).

### Callees

None. This function performs no I/O, calls no other project code, and raises
only the one `ValueError` it constructs itself. `date.strftime` is a stdlib
call with well-defined behavior for any valid `date`.

### Callers and what they depend on this function for

Two call sites, both structurally identical in the load-bearing part:

1. `src/invoicing/pipeline.py:216`, inside `bill_period`'s per-student loop
   (`src/invoicing/pipeline.py:182-234`):
   ```python
   invoice_number = next_invoice_number(today, db.count_invoices_issued_on(today))
   ```
   followed immediately by `db.insert_invoice(Invoice(invoice_number=invoice_number, ...))`
   at line 222, which commits synchronously (`src/invoicing/db.py:394`,
   `self.conn.commit()` inside `insert_invoice`).

2. `src/invoicing/seed.py:159`, inside `_bill_and_email_period` (fixture/seed
   data generation, `src/invoicing/seed.py:146-174`), same pattern:
   `next_invoice_number(issued_on, db.count_invoices_issued_on(issued_on))`
   immediately followed by `db.insert_invoice(...)`.

Both callers depend on this function for:
- Producing the correctly formatted 8-character invoice number string given
  a day and a count.
- Raising `ValueError` (not silently truncating or wrapping) once more than
  99 invoices would be issued for the same day — this propagates uncaught
  through both `bill_period` and `_bill_and_email_period` (no `try/except`
  around either call site), so it surfaces as a hard failure of the whole
  batch operation rather than skipping just the 100th invoice.

Neither caller re-derives or re-validates `already_issued_today` after
calling this function and before the corresponding `insert_invoice` call —
the count is read once via `db.count_invoices_issued_on(...)` (`src/invoicing/db.py:451-456`,
`SELECT COUNT(*) ... WHERE invoice_number LIKE 'DDMMYY%'`) and used
immediately. The read-then-insert pair is two separate SQL statements with no
transaction wrapping them (`insert_invoice` calls `self.conn.commit()` on the
insert alone; there is no `BEGIN`/lock spanning the `COUNT(*)` read and the
subsequent `INSERT`). Within a single `bill_period`/`_bill_and_email_period`
call, sequential loop iterations still see correct counts because
`insert_invoice`'s commit happens before the next loop iteration's
`count_invoices_issued_on` call — this is why the module docstring and the
`test_invoice_numbers_stay_unique_across_separate_bill_period_calls_same_day`
test (`tests/unit/test_invoice_numbers.py:78-94`) can observe uniqueness
across *sequential* calls on the same `Database`/connection. Nothing at this
layer (this function, `bill_period`, or `count_invoices_issued_on`) enforces
atomicity between two *concurrent* read-count-then-insert sequences; the only
backstop against a duplicate invoice_number reaching persisted state is the
`UNIQUE` constraint on the `invoices.invoice_number` column
(`src/invoicing/db.py:54`), which `insert_invoice` would violate with an
uncaught `sqlite3.IntegrityError` (no `try/except sqlite3.IntegrityError`
found anywhere in the codebase — confirmed via grep) rather than this module
resolving the collision by re-reading and retrying.

### Open questions

- Is `bill_period` / `_bill_and_email_period` ever invoked concurrently
  (multiple processes/threads against the same SQLite file, or the same
  `Database` object from multiple threads)? If so, the count-then-insert gap
  between `db.count_invoices_issued_on(today)` and `db.insert_invoice(...)`
  (`src/invoicing/pipeline.py:216-222`) is an unenforced-by-this-module
  window; the only thing standing between that race and either a crash
  (`IntegrityError` surfacing uncaught) or, if SQLite's default isolation
  level plus WAL/journal settings behave differently than assumed, a silent
  duplicate-number invoice, is the DB-level `UNIQUE` constraint at
  `src/invoicing/db.py:54` — need to inspect `Database.__init__` /
  connection setup for isolation level and whether callers ever share one
  `Database`/connection across threads or processes.
- `count_invoices_issued_on`'s `LIKE 'DDMMYY%'` prefix match
  (`src/invoicing/db.py:451-456`) assumes no invoice_number outside the
  intended format could ever share a `DDMMYY` prefix with a legitimately
  generated one for a different reason (e.g. manually inserted test/fixture
  data, or a future format change) — not verified here; would need to check
  all `insert_invoice` call sites and any migration/backfill code for
  invoice_number values that don't originate from `next_invoice_number`.
- No lower-bound check on `already_issued_today` in this function (see
  Assumptions above) — currently unreachable via the two known call sites
  since `COUNT(*)` can't be negative, but the function has no independent
  defense if a future caller passes a negative or otherwise out-of-band
  value.
