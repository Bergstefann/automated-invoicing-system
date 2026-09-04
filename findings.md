# Security Audit Findings — automated-invoicing-system

Phase 1 (read-only analysis), 2026-08-29. Composition: audit-context-builder →
insecure-defaults → supply-chain-risk-auditor → sharp-edges → fp-check.

## fp-check summary

All 7 sharp-edges findings were run through fp-check's full Standard Verification
methodology (Data Flow → Exploitability → Impact → PoC Sketch → 7-question devil's-
advocate → 6-gate review), documented per-finding in
`.tooling-reports/fp-check-verification.md`. Standard (not Deep) Verification is the
skill's own correct route for all seven: each is single-component, a well-understood
bug class, with no concurrency in the trigger and straightforward data flow — none of
the two Standard-route escalation checkpoints fired. Verdicts, as *attacker-exploitable
security vulnerabilities* per fp-check's Gate 3 criteria (RCE/privesc/info-disclosure
vs. operational robustness):

- **1 TRUE POSITIVE** (excess OAuth scope — #4): all six gates pass — this is the one
  finding where the attacker doesn't need any access inside the app's own trust
  boundary, only a leak on GitHub's distinct secret-storage side.
- **1 downgraded on new evidence** (pickle fallback — #2): reading the actual CI
  workflow file (`.github/workflows/scheduled-preview.yml:32-36`) shows
  `credentials/token.json` is freshly written as valid JSON from a GitHub secret on
  every run, strictly before the Python script executes — so `from_authorized_user_file`
  never raises and the `pickle.load` fallback is **provably unreachable in the one
  concretely-evidenced deployment**. Retained as a Medium defense-in-depth defect (the
  API itself stays dangerous for any other deployment whose file-permission boundary
  isn't established here), downgraded from the earlier "unresolved, elevated priority"
  hedge now that CI-path unreachability is proven rather than assumed.
- **1 downgraded on empirical re-test** (#3): directly tested Python's `email` package
  against the exact call path (`msg.as_bytes()`) — it raises `HeaderParseError` on an
  embedded CRLF, so the header-injection mechanism as originally claimed does not work.
  A weaker cosmetic defect (unescaped literal `"`) remains.
- **4 FALSE POSITIVE as vulnerabilities, but valid real defects** (#1, #5, #6, #7) —
  triggered by the legitimate operator's own action or configuration, not external
  attacker input, so they fail Gate 3 (no RCE/privesc/info-disclosure). #1 remains
  Critical-severity as a misuse-resistance defect in sharp-edges' own framing,
  independent of the fp-check vulnerability question.

## Findings

### TRUE POSITIVE — Excess OAuth scope on the scheduled-preview CI credential
`src/invoicing/providers/google.py:61-66` (`OAUTH_SCOPES`), `.github/workflows/scheduled-preview.yml`, `deploy/scheduled_preview.py`

All three provider classes share one `OAUTH_SCOPES` list, including full
`https://mail.google.com/` (entire Gmail, not `gmail.send`) and full Drive/Docs access.
The weekly scheduled-preview CI job only reads Sheets and sends one email, but its
minted token carries the full scope set.

fp-check: unlike the other findings, this **passes** the attacker-control gate — a
leaked `INVOICING_OAUTH_TOKEN_JSON` GitHub secret (log exposure, compromised runner,
malicious `workflow_dispatch` PR) is a distinct, realistic external threat vector,
and the resulting access (full mailbox + Drive) is disproportionate to what the script
needs. **TRUE POSITIVE — least-privilege violation.** Severity: Medium-High.

**Status: code fixed, secret rotation still required.** `OAUTH_SCOPES` is now split
into `SHEETS_SCOPES` / `DOCS_DRIVE_SCOPES` / `EMAIL_SCOPES` (`google.py`), each
provider requests only its own scopes, and `GoogleEmailProvider` now asks for
`gmail.send` instead of full `https://mail.google.com/`. This does **not** by itself
narrow whatever the existing `INVOICING_OAUTH_TOKEN_JSON` secret was already granted —
OAuth scopes are fixed at consent time, not by what a client later requests. Closing
this fully still needs a manual step: revoke the current grant at
https://myaccount.google.com/permissions, re-run the consent flow (which will now only
ask for Sheets + `gmail.send`, since `scheduled_preview.py` only builds
`GoogleSheetProvider`/`GoogleEmailProvider`), and replace the GitHub secret with the
newly-minted token.

### Defense-in-depth defect (not exploitable in the evidenced deployment) — `pickle.load` fallback on the OAuth token cache
`src/invoicing/providers/google.py:104-120` (`_load_cached_token`)

Falls back to `pickle.load` on the token file's raw bytes when JSON parsing fails
(documented as a one-time legacy-format migration, per the function's own docstring —
not a general untrusted-input handler). No integrity/origin check on the file before
deserializing.

fp-check: read the actual CI workflow (`.github/workflows/scheduled-preview.yml:32-36`)
— `credentials/token.json` is freshly written as valid JSON from a GitHub secret on
every run, strictly before the Python script executes. `from_authorized_user_file`
therefore never raises in this deployment, and the `pickle.load` fallback is **provably
unreachable** here — not merely unproven. Full gate review in
`.tooling-reports/fp-check-verification.md`. **FALSE POSITIVE as a proven-exploitable
vulnerability in the one concretely-evidenced deployment.** Retained as a valid
defense-in-depth defect: the API is inherently dangerous, and its safety in any other
deployment (local dev, a future CI change, a manually-placed legacy token file) rests
entirely on an unenforced, undocumented file-permission assumption. Severity: Medium
(downgraded from High now that CI-path unreachability is proven).

Recommendation: drop the pickle fallback (require a one-time, offline, operator-run
migration instead), or at minimum gate it behind an explicit opt-in and verify the
unpickled object's type before use.

### Downgraded on empirical re-test — unescaped `Content-Disposition` filename
`src/invoicing/providers/google.py:456`, filename sourced from `student.name` (`pipeline.py:321`)

Original claim: unescaped `filename=` enables MIME header injection via CRLF in a
student name.

fp-check: **tested directly** — `msg.as_bytes()` (what `google.py:459` calls before
sending) runs through `email.generator`'s folding/sanitization, which raises
`email.errors.HeaderParseError` on an embedded CRLF. The header-injection mechanism as
originally described **does not work**; a crafted name would abort that one email send
with an uncaught exception (isolated per-invoice by `pipeline.py`'s existing
try/except around the send, per `billing_and_pipeline.md`) rather than inject a header.
A weaker defect remains: an unescaped literal `"` in a name is still accepted and could
break `Content-Disposition` parsing in some mail clients (malformed header, not
injection). **FALSE POSITIVE as header injection; TRUE POSITIVE as a minor malformed-
header defect.** Severity: Low (downgraded from Medium).

Recommendation: pass `filename=attachment.filename` as a keyword to `add_header`
(the `email` package handles RFC 2231 quoting/encoding automatically) instead of
manual f-string interpolation.

### Code defect (not an attacker-exploitable vulnerability) — `--confirm` doesn't gate what it appears to
`src/invoicing/cli.py:129-195`, `src/invoicing/pipeline.py` (`bill_period`, ~182-258)

`run --real --no-dry-run` (without `--confirm`) bills lessons, creates real Google
Docs, and writes back to the live Sheet — only the email send is gated by `--confirm`.
The docstring's framing implies broader protection than exists.

fp-check: no external attacker is involved — this is triggered entirely by the
legitimate operator's own command-line choice, and the impact (unintended real billing
action) is business/operational risk, not RCE/privesc/info-disclosure. **FALSE
POSITIVE as a security vulnerability** under fp-check's Gate 3 criteria. **Remains
Critical as a misuse-resistance / dangerous-default defect** — this classification
question doesn't change its priority; it changes only whether it's filed as a
"vulnerability" versus a safety defect, and sharp-edges' own framing (which this
finding came from) is the correct lens for it.

Recommendation: as in sharp-edges.md — split into `--bill` and `--send-emails` flags,
both defaulting off, and correct the docstring to state precisely which side effects
each flag controls.

### Code defect (not an attacker-exploitable vulnerability) — bare `assert` instead of a config-validation error
`src/invoicing/providers/google.py:123-125`, `src/invoicing/config.py:82-109`

`oauth_token_file` is asserted non-`None` but never checked by
`require_sheet_config`/`require_google_config`; a missing env var surfaces as an
unmessaged `AssertionError` (and `assert` is stripped under `python -O`) instead of a
clean config error.

fp-check: purely an operator's-own-misconfiguration issue with no attacker model and
no security impact (error-message quality only). **FALSE POSITIVE as a vulnerability;
valid as a code-quality defect.** Severity: Medium.

### Code defect (not an attacker-exploitable vulnerability) — empty-string env vars silently treated as unset
`src/invoicing/config.py:54-62` (`opt_env`/`opt_path`)

fp-check: same pattern — operator's-own-config issue, and the report itself already
notes this currently degrades to a clear downstream error rather than a silent bypass.
**FALSE POSITIVE as a vulnerability; valid as a design smell.** Severity: Low-Medium.

### Code defect (not an attacker-exploitable vulnerability) — no re-processing guard on billing/email state writes
`src/invoicing/db.py:336-341` (`mark_lessons_billed`), `db.py:408-413` (`mark_invoice_emailed`)

fp-check: current call sites only ever pass freshly-selected ids (confirmed via
audit-context), so this isn't reachable through any attacker or even any current
caller today — it's a latent defensive-programming gap for future callers (retry
logic, a new CLI command). No attacker model applies. **FALSE POSITIVE as a
vulnerability; valid as a data-integrity/defensive-programming gap.** Severity:
Low-Medium.

## Additional correctness findings surfaced by audit-context (not run through fp-check)

These came out of the audit-context pass but fall outside sharp-edges' misuse-
resistance framing (correctness/reliability bugs rather than dangerous-default APIs),
so they were not independently routed through fp-check. Listed for visibility, not as
verified security findings:

- `next_invoice_number`'s cross-run uniqueness relies on a non-atomic
  count-then-insert with no transaction spanning it, and no code path catches
  `sqlite3.IntegrityError` on the `invoices.invoice_number UNIQUE` backstop
  (`invoice_numbers.py`, `db.py:394`).
- `email_pending_invoices`'s documented "one failure doesn't touch the others"
  guarantee is false: two abort points (`KeyError`/`IndexError`) sit *before* the
  per-invoice `try` block, so a single bad invoice aborts the whole batch
  (`pipeline.py` ~289-327).
- `get_or_create_student` matches on name alone (`db.py:236-270`), weaker than the
  schema's actual `UNIQUE(name, parent_id)` — a documented deliberate choice, but
  unlike the Sheet-grid parser (which raises on a first-name collision), the sync path
  joins lessons to students by lowercased first name alone with no collision check,
  risking silent misattribution.
- No cross-field validation anywhere that `subtotal_cents + gst_cents == total_cents`
  on `Invoice` (`models.py`), surfaced independently by both the config_and_models and
  db audit-context passes.
- `schedule_contract.py`'s `isinstance(x, date)` check would silently accept a raw
  `datetime` (subclass) if a caller skipped the date/time split step.
- `template_row_capacity`'s 8-line template cap isn't enforced before
  `build_replacements` emits tags — overflow rows silently drop from the generated
  invoice doc while totals still include them (`templates/invoice_doc.py`).

## Skipped / not run

- **CodeRabbit** — NOT INSTALLED. See `.tooling-reports/coderabbit.md`.
- **karpathy skill** — not applicable. See `.tooling-reports/karpathy.md`.
- **insecure-defaults** — NOT RUN. Required `Workflow` tool unavailable in this
  session. See `.tooling-reports/insecure-defaults.md`.

## Clean

- **supply-chain-risk-auditor**: 12/12 direct PyPI dependencies, no known advisories.
  No lockfile present (versions checked against latest release, not resolved pins).
  OpenSSF Scorecard tier unassessable for 11/12; `openpyxl` has no resolvable source
  repository. See `.tooling-reports/supply-chain/report.md`.
