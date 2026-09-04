# Sharp Edges Analysis — automated-invoicing-system

Scope: `src/invoicing/cli.py`, `src/invoicing/providers/google.py`, `src/invoicing/providers/fake.py`,
`src/invoicing/config.py`, `src/invoicing/models.py`, `src/invoicing/db.py`,
`deploy/scheduled_preview.py`. Findings validated against source and
`audit-context/functions/*.md`.

## 1. `--no-dry-run` alone bills, creates Docs, and writes back to the live Sheet — `--confirm` only gates email

- **Category:** Configuration Cliff / Dangerous Default
- **Severity:** Critical
- **Location:** `src/invoicing/cli.py:129-195` (esp. `run_period` dispatch at 165-178, docstring at
  line 7), `src/invoicing/pipeline.py` (`bill_period`, ~182-258; `sheet.mark_lessons_billed` call
  ~255-256)
- **Description:** `run`'s docstring says `--no-dry-run` "still won't send a single email unless
  `--confirm` is also passed" (cli.py:7), which reads as a comprehensive safety statement. In
  reality `dry_run=False` unconditionally reaches `bill_period`, which creates real Google Docs
  (network call), inserts DB invoice/invoice-line rows, marks lessons billed, and — whenever
  `sheet_refs` is non-empty — performs a live `batchUpdate` write-back to the production Google
  Sheet. `send_emails=confirm` gates only `email_pending_invoices`. A developer or operator who
  reads the docstring's framing ("two gates protect you") and runs `--real --no-dry-run` without
  `--confirm`, believing this is a safe rehearsal step, has actually billed customers, created
  invoice documents in Drive, and mutated the live spreadsheet — all irreversible in the sense
  that lessons are now flagged `billed_invoice_id` and Sheet status becomes `INVOICED`.
- **Minimal misuse example:**
  ```
  invoicing run --period 3 --real --no-dry-run
  # no --confirm passed; developer believes "nothing external happened yet"
  ```
  This bills the period, creates invoice Docs, writes the Sheet — only the email is skipped.
- **Recommendation:** Rename/restructure the flags so a single flag doesn't imply "still safe."
  E.g., split into `--bill` (writes DB/Docs/Sheet) and `--send-emails` (send), both defaulting off,
  with `run` requiring explicit `--bill` before any write path is reached, and update the
  docstring to state precisely which side effects each flag controls. Alternatively, require
  `--confirm` for *any* non-dry-run write, and add a distinct `--confirm-email` for the email step
  specifically.

## 2. `_load_cached_token` unconditionally deserializes untrusted bytes via `pickle.load`

- **Category:** Silent Failure / Configuration Cliff (arbitrary code execution risk)
- **Severity:** High
- **Location:** `src/invoicing/providers/google.py:104-120` (`open(token_file, "rb"); pickle.load(f)`
  at line 117)
- **Description:** When `Credentials.from_authorized_user_file` raises `ValueError` (any non-JSON
  content, not just legacy pickle), the code falls back to `pickle.load` on the same file's raw
  bytes with zero integrity or origin check. `pickle.load` executes arbitrary code embedded in the
  stream during deserialization. The token file path (`oauth_token_file`) comes from an
  environment variable / `.env`, and — per finding #5 — is never validated for presence, meaning
  attacker-influenced deployment config or a writable shared path could point this at an
  attacker-controlled file. Any process capable of writing to that path (weak file permissions, a
  compromised CI secret, a misconfigured shared directory) achieves code execution the next time
  any CLI command touches Google config.
- **Minimal misuse example:** Place any non-JSON file at `INVOICING_OAUTH_TOKEN_FILE`'s path (e.g.
  a malicious pickle payload) and run any `--real` command that constructs a Google provider
  (`sync --real`, `run --real`, `debug-parse-schedule --real`).
- **Recommendation:** Drop the pickle fallback entirely (require re-auth / a clean migration step
  run once, offline, by an operator) or, at minimum, gate it behind an explicit opt-in flag/env
  var, verify the unpickled object's type before use, and enforce restrictive file
  permissions/ownership checks before trusting the file.

## 3. Unescaped, unquoted `filename=` in `Content-Disposition` built from DB-sourced student name

- **Category:** Stringly-Typed Security / injection
- **Severity:** Medium
- **Location:** `src/invoicing/providers/google.py:456`
  (`part.add_header("Content-Disposition", f"attachment; filename={attachment.filename}")`);
  filename constructed at `pipeline.py:321` from `student.name` (free-text, sourced from the
  Google Sheet's Student Config tab with no sanitization, `google.py:199-217`)
- **Description:** `student.name` is operator-editable spreadsheet content, not validated for
  header-breaking characters (`"`, CR/LF, etc.). It flows unsanitized into a MIME header.
  Depending on the email client parsing the message, a crafted name (e.g. containing a `"` to
  break out of the filename token, or literal newlines if the underlying stdlib `email` package
  doesn't reject them) could alter attachment metadata or, in more permissive parsers, be used for
  header injection. This is invoked on every invoice email sent to a real parent.
- **Recommendation:** Use `email.utils.encode_rfc2231`/`Message.add_header(..., filename=...)`'s
  built-in parameter-encoding (the `email` package supports passing `filename` as a keyword to
  `add_header`, which handles quoting/RFC 2231 encoding automatically) instead of manual f-string
  interpolation, and/or sanitize `student.name` to a safe filename charset before use.

## 4. `deploy/scheduled_preview.py` runs with a token scoped to full Sheets+Docs+Drive+entire Gmail, though it only reads Sheets and sends one email

- **Category:** Dangerous Defaults / Configuration Cliff (excess privilege)
- **Severity:** Medium-High
- **Location:** `src/invoicing/providers/google.py:61-66` (`OAUTH_SCOPES` — single shared scope
  list including `https://mail.google.com/`, full Gmail, not `gmail.send`);
  `.github/workflows/scheduled-preview.yml` (secrets `INVOICING_OAUTH_TOKEN_JSON`);
  `deploy/scheduled_preview.py`
- **Description:** All three provider classes request the same module-level `OAUTH_SCOPES`, so the
  OAuth token minted for this read-mostly, single-outbound-email CI job carries full Gmail
  read/send/modify access plus full (non-`drive.file`) Drive access and Docs/Sheets write, even
  though the script never constructs `GoogleDocProvider` and never calls `mark_lessons_billed`. If
  the `INVOICING_OAUTH_TOKEN_JSON` GitHub secret leaks (log exposure, compromised runner,
  malicious workflow-file PR via `workflow_dispatch`), the blast radius is the entire mailbox and
  Drive, not "can send one email."
- **Recommendation:** Split `OAUTH_SCOPES` per provider class (Sheets read/write scope for
  `GoogleSheetProvider`, `drive.file` + Docs for `GoogleDocProvider`, `gmail.send` only for
  `GoogleEmailProvider`), and mint/store a narrowly-scoped token specifically for the
  scheduled-preview workflow.

## 5. `_oauth_credentials` bare-asserts `oauth_token_file` is non-`None`, but no `Settings.require_*` validates it

- **Category:** Silent Failure / Configuration Cliff
- **Severity:** Medium
- **Location:** `src/invoicing/providers/google.py:123-125`
  (`assert settings.oauth_client_secret_file is not None`,
  `assert settings.oauth_token_file is not None`); `src/invoicing/config.py:82-109`
  (`require_sheet_config` checks only `sheet_id`/`oauth_client_secret_file`; `require_google_config`
  adds only Drive/Doc-template checks) — `oauth_token_file` is never in either required-field dict.
- **Description:** Every real-mode CLI/CI call site (`cli.py:75`, `cli.py:218`,
  `deploy/scheduled_preview.py:69`) calls `require_sheet_config()`/`require_google_config()` and
  treats a clean return as "safe to construct Google providers." But if
  `INVOICING_OAUTH_TOKEN_FILE` is unset while `INVOICING_OAUTH_CLIENT_SECRET_FILE` is set, both
  checks pass, and the failure only surfaces later as a bare `AssertionError` (no message, and
  `assert` statements are stripped entirely under `python -O`) the first time any provider's lazy
  accessor runs — instead of a clean `ValueError` at the configuration-check boundary the codebase
  otherwise uses consistently.
- **Recommendation:** Add `oauth_token_file` to the field dict in `require_sheet_config` (or
  `require_google_config`), or, if it's meant to be optional (auto-created on first OAuth flow),
  replace the bare `assert` with an explicit, documented `if ... is None: raise ValueError(...)`
  and default it to a sensible path when unset rather than requiring the caller to set it.

## 6. `opt_env`/`opt_path` fold empty-string env vars into "unset" — silent config bypass

- **Category:** Dangerous Defaults / Silent Failure
- **Severity:** Low-Medium
- **Location:** `src/invoicing/config.py:54-62` (`opt_env` returns `None` via `or None` for both
  unset and empty-string values), consumed by `sheet_id`, `oauth_client_secret_file`,
  `drive_output_folder_id`, `doc_template_short_id`, `doc_template_long_id`, `oauth_token_file`.
- **Description:** `INVOICING_SHEET_ID=""` (e.g. from a CI variable that resolves to empty, or a
  `.env` line with a stray `=` and nothing after it) is silently treated identically to the
  variable being entirely absent. `require_sheet_config`/`require_google_config` will then
  correctly raise (since falsy values are rejected there), so this specific instance degrades to a
  clear error rather than a silent bypass — but it does mean a deployment can't distinguish
  "operator explicitly cleared this value" from "operator forgot to set it," and any future
  required-field check that used `is None` instead of falsy-truthiness would silently treat an
  intentionally-empty override as unset. Flagging as a design smell adjacent to the
  config-validation gaps above.
- **Recommendation:** Have `opt_env` distinguish "unset" (`os.environ.get(name)` returns `None`)
  from "set but empty" (raise or warn explicitly) rather than collapsing both to `None`.

## 7. No re-processing guard on `mark_lessons_billed` / `mark_invoice_emailed`

- **Category:** Configuration Cliff / Silent Failure (idempotency gap)
- **Severity:** Low-Medium
- **Location:** `src/invoicing/db.py:336-341` (`mark_lessons_billed` — `UPDATE ... WHERE id = ?`,
  no `billed_invoice_id IS NULL` guard) and `db.py:408-413` (`mark_invoice_emailed` — no "not
  already emailed" guard).
- **Description:** Both methods will silently overwrite an already-billed lesson's
  `billed_invoice_id` or re-stamp an already-emailed invoice's `emailed_at` if called with such an
  id — a no-op on a nonexistent id, but a *silent overwrite* (not an error) on an id that's already
  in the "done" state. Current call sites (`bill_period`, `email_pending_invoices`) appear to only
  pass freshly-selected unbilled/pending ids, so this isn't exploitable through the current CLI
  paths, but nothing in `db.py` itself enforces the invariant — a future caller (retry logic, a new
  CLI command, a bugfix that recomputes ids incorrectly) could silently double-process or corrupt
  billing state with no exception raised anywhere.
- **Recommendation:** Add `AND billed_invoice_id IS NULL` / `AND emailed_at IS NULL` to the
  respective `WHERE` clauses and have the methods return/raise on a rowcount mismatch (expected N
  updated, got fewer), turning silent no-ops into a loud signal that the caller's assumed
  unbilled/pending set was stale.

## Additional lower-severity notes (not separately numbered above)

- `preview` and `status` require no `--real`/`--demo` gate and no Google-config check at all, but
  `run` and `sync` do — inconsistent safety posture across commands (Low; `cli.py` cross-cutting
  observation, no live-API consequence today but a maintenance trap if either command's scope
  grows).
- `bill_period`'s `ValueError` for an incomplete period, and every provider network exception,
  propagate as raw Python tracebacks with no CLI-level try/except (Low — operational rather than
  security, but obscures the one validation error a human operator most needs a clean message
  for).
- `scheduled_preview.py` embeds `line.student.name` raw into an HTML email body with no escaping
  (Low-Medium; stored-HTML-injection into an operator-facing email if the Sheet's Student Config
  tab is ever editable by a lower-trust party).

## Note on provenance

This report was written by the orchestrating session from the `sharp-edges-analyzer`
subagent's returned findings text — the subagent's own operating constraints prevent it
from writing report files directly, so it returned findings in its final message instead.
