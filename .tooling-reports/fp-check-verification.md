# fp-check Standard Verification — automated-invoicing-system sharp-edges findings

Route selection: all 7 findings are single-component, well-understood bug classes
(config cliffs, deserialization, header construction, least-privilege), with no
cross-component chains, no concurrency in the trigger, and straightforward data flow.
Per `standard-verification.md`'s explicit routing criteria, **Standard Verification**
applies to all seven — not Deep Verification. Standard Verification requires: Step 1
Data Flow, Step 2 Exploitability, Step 3 Impact, Step 4 pseudocode PoC (executable/unit
PoCs explicitly optional), Step 5 seven-question devil's-advocate spot-check, Step 6
six-gate review. None of the two escalation checkpoints (3+ trust boundaries/ambiguous
validation after Step 1; genuine unresolved uncertainty after Step 5) fired for any of
the seven, so none required escalation to Deep Verification.

---

## Bug #1 — `--confirm` doesn't gate what it appears to (cli.py `run`)

**Restated claim**: `run --real --no-dry-run` without `--confirm` bills lessons,
creates Google Docs, and writes the live Sheet; the docstring implies `--confirm` gates
all external side effects, but it only gates the email send.

### Step 1 — Data Flow
- Source: CLI flags (`--real`/`--demo`, `--dry-run`/`--no-dry-run`, `--confirm`),
  typed directly by the operator invoking the tool.
- Sink: `bill_period` (`pipeline.py:182-258`) — Doc creation (network), DB writes,
  Sheet `batchUpdate` write-back — reached whenever `dry_run=False`, independent of
  `confirm`.
- Trust boundary: none crossed — flags come from the same operator running the
  command; no external/remote input anywhere in this path. One boundary (operator →
  CLI parser → pipeline), no callbacks/async. **No escalation trigger.**
- API contract: Typer parses flags to booleans exactly as declared; no built-in
  protection layers exist between flag and side effect.

### Step 2 — Exploitability
- **Attacker control**: none. The person invoking `--no-dry-run` is the same
  legitimate operator who would be "attacking" themselves — there is no distinct
  attacker in this scenario, only a misread docstring leading to an unintended action
  by the tool's own legitimate user.
- Bounds/race: N/A.

### Step 3 — Impact
- Real impact if triggered: unintended real invoices, real Google Docs created, real
  Sheet mutation — genuine business/financial consequence.
- Classified against fp-check's specific impact categories (RCE, privesc, info
  disclosure) vs. operational robustness: this is **none of RCE/privesc/info-
  disclosure** — it's an unintended-but-authorized business action by the tool's own
  legitimate operator, which fp-check's Gate 3 treats as an operational-robustness /
  business-logic issue, not a security vulnerability, however severe its consequence.

### Step 4 — PoC Sketch
```
Data Flow: operator types `run --period 3 --real --no-dry-run` (no --confirm)
Attacker controls: nothing — the "attacker" is the tool's own authorized operator
Trigger: dry_run=False reaches bill_period() unconditionally; confirm=False only
         skips email_pending_invoices()
```

### Step 5 — Devil's Advocate (7 questions)
1. Pattern-matching bias? No — confirmed by reading `run_period`'s actual dispatch
   logic (`pipeline.py:352-401`), not just the docstring.
2. Trust boundary confusion? No new confusion — correctly identified as no attacker
   involved from the start.
3. Proven the condition can occur? Yes trivially — any `--no-dry-run` without
   `--confirm` invocation triggers it.
4. Defense-in-depth confusion? Not applicable — there was never a security control
   here, only a UX/documentation gap.
5. Hallucinating? No — the docstring/behavior mismatch is directly readable in
   `cli.py:7` vs. the dispatch logic.
6. Dismissing because exploit seems complex? Not dismissing severity — the finding
   stays Critical as a misuse-resistance defect; only its classification as an
   "attacker-exploitable vulnerability" is being corrected.
7. Inventing unverified mitigations? No — re-read `pipeline.py:352-401` in full;
   confirmed no other gate exists between `dry_run` and the Doc/DB/Sheet writes.

### Step 6 — Gate Review
| Gate | Verdict | Evidence |
|---|---|---|
| 1. Process | PASS | Documented above |
| 2. Reachability | N/A (no attacker) | Triggered solely by the tool's own authorized operator |
| 3. Real Impact | **FAIL** (as vuln) | Business/operational consequence, not RCE/privesc/info-disclosure |
| 4. PoC Validation | PASS (mechanism) | PoC shows the exact flag-gap mechanism |
| 5. Math Bounds | N/A | Not a bounds bug |
| 6. Environment | PASS | No environmental protection blocks it |

**Verdict: FALSE POSITIVE as an attacker-exploitable vulnerability (Gate 3 fails —
no attacker, no RCE/privesc/info-disclosure). Remains a valid, Critical-severity
misuse-resistance / dangerous-default defect** under sharp-edges' own framing, which is
the correct lens for this finding regardless of the fp-check vulnerability question.

---

## Bug #2 — `pickle.load` fallback on the OAuth token cache

**Restated claim**: `_load_cached_token` falls back to `pickle.load` on the token
file's raw bytes when JSON parsing fails, with no integrity check — a deserialization
RCE risk if the file is ever attacker-influenced.

### Step 1 — Data Flow
- Source: `settings.oauth_token_file` (path from env var) → file content read at
  `google.py:114/116`.
- Sink: `pickle.load(f)` at `google.py:117`.
- Trust boundary: the file's write-access boundary. Traced concretely for the one
  documented deployment: `.github/workflows/scheduled-preview.yml:32-36` — `mkdir -p
  credentials`, then `printf '%s' "$OAUTH_TOKEN_JSON" > credentials/token.json`,
  writing a **fresh valid JSON file from a GitHub Actions secret on every run**,
  strictly before the Python script executes (sequential job steps, same ephemeral
  runner). Confirmed via reading the actual workflow file, not assumed.
- **Escalation check**: single boundary (CI secret → file → app), no ambiguity for
  the CI path. No escalation trigger.

### Step 2 — Exploitability
- **Attacker control, CI deployment**: `from_authorized_user_file` is called on a file
  that is *always* freshly-written valid JSON in this deployment (line 114) — it never
  raises `ValueError`, so the `pickle.load` fallback (line 115-117) is **provably
  unreached** in the one concretely-evidenced deployment path.
- **Attacker control, other/local deployments**: the function's own docstring
  (`google.py:105-111`) describes this as a one-time legacy-format migration for token
  caches predating a format change — i.e., it exists to read *the app's own previously
  self-written* pickle files, not arbitrary input. For a non-CI (e.g. local developer)
  deployment, triggering the fallback with attacker content requires attacker write
  access to that specific local file path.

### Step 3 — Impact
- If ever reached with attacker-controlled bytes: real impact is severe (arbitrary
  code execution via `pickle.load`), a genuine RCE class primary-control failure —
  this would pass Gate 3 if reachability were established.

### Step 4 — PoC Sketch
```
Data Flow (CI): GH secret --printf--> credentials/token.json (always valid JSON)
                --> from_authorized_user_file succeeds --> pickle.load never called
Data Flow (local, hypothetical): attacker write access to <local path> -->
                a non-JSON file there --> ValueError --> pickle.load(attacker bytes)
                --> arbitrary code execution
Attacker controls (CI): nothing reaches the pickle path — proven unreachable
Attacker controls (local): requires pre-existing write access to one specific file
```

### Step 5 — Devil's Advocate (7 questions)
1. Pattern-matching bias? Partially present in the original sharp-edges framing
   ("pickle.load looks dangerous") — corrected by actually tracing the CI write
   sequence rather than assuming the fallback is live.
2. Trust boundary confusion? Corrected here — for CI, traced the exact write step
   instead of assuming attacker reachability.
3. Proven the condition can occur? For CI: proven it **cannot** occur (fallback
   unreachable given always-fresh JSON). For local/other: not proven either way from
   this repo's source — deployment-dependent.
4. Defense-in-depth confusion? The dangerous API itself is real regardless of current
   reachability — worth removing as defense-in-depth even though the primary CI path
   doesn't reach it.
5. Hallucinating? No — the sink and fallback mechanism are real code, correctly read.
6. Dismissing because exploit seems complex? Not dismissing — flagging as a real
   latent defect in the API design even though the one evidenced deployment doesn't
   reach it.
7. Inventing unverified mitigations? No — the "always fresh JSON" claim is backed by
   reading the actual workflow YAML, not assumed.

### Step 6 — Gate Review
| Gate | Verdict | Evidence |
|---|---|---|
| 1. Process | PASS | Documented above, including reading the actual CI workflow |
| 2. Reachability | **FAIL (CI)** / uncertain (other deployments) | CI: fallback provably unreached; other: no other deployment's file-permission boundary is established in this repo |
| 3. Real Impact | PASS if reachable | RCE-class impact, would satisfy Gate 3 given reachability |
| 4. PoC Validation | PASS (mechanism) | Mechanism confirmed, CI path confirmed unreachable |
| 5. Math Bounds | N/A | |
| 6. Environment | PASS (mechanism unprotected) / FAIL (CI specifically, given always-fresh-JSON) | |

**Verdict: FALSE POSITIVE as a proven-exploitable vulnerability in the one concretely-
evidenced deployment (CI) — the pickle path is provably unreachable there.** Retained
as a **valid defensive-programming defect**: the API is inherently dangerous and its
safety in any other deployment (local dev, a future CI change, a manually-placed
legacy token file) depends entirely on an unenforced, undocumented file-permission
assumption. Recommend removing the fallback regardless of current non-reachability,
since dangerous APIs whose safety depends on deployment-specific assumptions are
exactly what sharp-edges exists to flag. Severity: downgraded from High to Medium
(defense-in-depth) given CI-path unreachability is now proven, not assumed.

---

## Bug #3 — Unescaped `Content-Disposition` filename

**Restated claim**: unescaped `filename=` built from `student.name` enables MIME
header injection via CRLF.

### Step 1 — Data Flow
- Source: `student.name`, a free-text field from the Google Sheet's Student Config
  tab, unsanitized (`google.py:199-217`).
- Sink: `part.add_header("Content-Disposition", f"attachment; filename={...}")`
  (`google.py:456`), serialized via `msg.as_bytes()` (`google.py:459`) before being
  base64-encoded and sent through the Gmail API.
- Trust boundary: Sheet content is operator-editable in this business's actual usage
  (staff enter student names), not directly attacker-facing, but is treated here as
  a worst-case "untrusted content" input for the header-injection question — the
  question is whether the *mechanism* works at all, independent of who can reach it.
  One boundary. No escalation trigger.

### Step 2 — Exploitability (empirically tested, not just reasoned)
- Directly tested against the same stdlib version/path the code uses:
  `email.message.Message.add_header` accepts an embedded CRLF at construction time
  with no error (confirmed).
  `msg.as_bytes()` (the exact call at `google.py:459`) — however — runs through
  `email.generator`'s header folding, which calls `policy.fold(..., sanitize=True)`
  and **raises `email.errors.HeaderParseError`** on an embedded CRLF (confirmed by
  direct execution, traceback captured). The `send()` path never catches this
  exception itself, but `pipeline.py`'s per-invoice `try/except` around the send call
  (per `billing_and_pipeline.md`) isolates the failure to that one invoice.
- **Attacker control**: even granting worst-case attacker control of `student.name`,
  the CRLF-based header-injection mechanism does not function against this code path.

### Step 3 — Impact
- As header injection: none — mechanism doesn't work.
- Residual: an unescaped literal `"` (no CRLF) is still accepted uncaught and could
  malform the `Content-Disposition` value for some mail client parsers — a much
  weaker, cosmetic/robustness defect, not injection.

### Step 4 — PoC Sketch
```
Tested: Message().add_header('Content-Disposition', 'attachment; filename=evil\r\nX-Injected: yes')
        -> accepted at add_header time
        -> msg.as_bytes() -> email.errors.HeaderParseError raised
        -> send() aborts for this one invoice (caught by pipeline.py's per-invoice try/except)
Result: injection mechanism disproven; not merely assumed safe
```

### Step 5 — Devil's Advocate (7 questions)
1. Pattern-matching bias? This is exactly the bias that produced the original
   overstated claim — corrected by empirical test rather than pattern-matching on
   "unescaped string in header = injection."
2. Trust boundary confusion? Set aside — tested worst-case attacker control and still
   found the mechanism doesn't work.
3. Proven the condition can/cannot occur? Proven empirically that CRLF injection
   cannot occur through this call path.
4. Defense-in-depth confusion? N/A.
5. Hallucinating? The original claim was the overstated one; corrected via direct
   execution, not further reasoning-only analysis.
6. Dismissing because exploit seems complex? Not dismissing arbitrarily — disproven
   by direct test, and the weaker residual defect (bare `"`) is still reported.
7. Inventing unverified mitigations? No — `email.generator`'s sanitize-on-fold
   behavior is demonstrated, not assumed from documentation.

### Step 6 — Gate Review
| Gate | Verdict | Evidence |
|---|---|---|
| 1. Process | PASS | |
| 2. Reachability | PASS (mechanism reachable) | `student.name` does reach the sink |
| 3. Real Impact | **FAIL (as injection)** | `HeaderParseError` proven to block CRLF injection |
| 4. PoC Validation | FAIL (as injection) / confirms residual defect | Empirical test is the PoC |
| 5. Math Bounds | N/A | |
| 6. Environment | **FAIL (blocks it)** | `email.generator`'s sanitize-on-fold is an environmental protection that entirely prevents CRLF injection |

**Verdict: FALSE POSITIVE as header injection (Gate 3 and Gate 6 both fail — the
Python stdlib's own header-folding sanitization blocks it entirely). TRUE POSITIVE as
a much weaker defect**: an unescaped `"` could still malform the header for some
client parsers. Severity downgraded Medium → Low.

---

## Bug #4 — Excess OAuth scope on the scheduled-preview CI credential

**Restated claim**: the CI job's minted token carries full Gmail/Drive/Docs/Sheets
scope though the job only reads Sheets and sends one email; a leaked secret grants
disproportionate access.

### Step 1 — Data Flow
- Source: `OAUTH_SCOPES` (`google.py:61-66`), a single module-level list shared by
  all three provider classes.
- Sink: `InstalledAppFlow.from_client_secrets_file(..., OAUTH_SCOPES)` /
  `Credentials.from_authorized_user_file(..., OAUTH_SCOPES)` — the scope list is
  baked into the minted/loaded token regardless of which provider actually uses it.
- Trust boundary crossed: this **is** a genuine external boundary — the secret
  (`INVOICING_OAUTH_TOKEN_JSON`) lives in GitHub's secret store, a system distinct
  from this application's own runtime, with its own separate leak surface (workflow
  logs, a compromised/malicious `workflow_dispatch` triggered by an external PR,
  runner compromise, GitHub-side incident). One clear boundary between "this secret
  leaks" and "this application's own logic runs correctly."

### Step 2 — Exploitability
- **Attacker control**: an attacker doesn't need to control any application input at
  all — they only need to obtain the token via any leak vector on the *secret
  storage/CI side*, which is architecturally distinct from every other finding in
  this audit (all of which required the attacker to influence something inside the
  app's own trust boundary). This is the one finding where "whoever holds the leaked
  credential" is a meaningfully different, realistic external actor, not the
  legitimate operator themselves.

### Step 3 — Impact
- Real impact: full Gmail (read/send/modify/delete across the entire mailbox, not
  just this app's sends) and full Drive access, when the CI job's actual job needs
  only Sheets-read and single-email-send. This is **information disclosure** (mailbox
  contents) and privilege beyond intended scope (a real least-privilege violation) —
  squarely inside fp-check's Real Impact category, not merely operational robustness.

### Step 4 — PoC Sketch
```
Data Flow: GH secret leak (any vector) -> holder has credentials/token.json bytes
           -> token scoped to OAUTH_SCOPES (google.py:61-66) = full Gmail + full Drive
           -> holder can read/send/delete any mail in the account, not just what
              scheduled_preview.py itself would ever do
Attacker controls: the leaked token itself, once obtained by any means
Trigger: use the leaked token directly against Gmail/Drive APIs
```

### Step 5 — Devil's Advocate (7 questions)
1. Pattern-matching bias? No — verified the actual scope string
   (`https://mail.google.com/`, full mailbox) against what `gmail.send`-only would be,
   a concrete, checkable difference, not just "broad scope looks risky."
2. Trust boundary confusion? No — this is the one finding where the attacker
   genuinely doesn't need any access inside the app's own boundary; the secret store
   is a legitimately separate boundary.
3. Proven the condition can occur? Yes — scope over-grant is provable by reading
   `OAUTH_SCOPES` directly; no conditional logic gates it.
4. Defense-in-depth confusion? This is a primary control (scope = the actual
   authorization boundary for what a leaked token can do), not defense-in-depth.
5. Hallucinating? No — `https://mail.google.com/` vs `gmail.send` is a documented,
   checkable Google API scope distinction.
6. Dismissing because exploit seems complex? Not dismissing — the exploit (using a
   leaked token) requires no sophistication once the secret is obtained.
7. Inventing unverified mitigations? No — confirmed no per-provider scope narrowing
   exists anywhere in the codebase (single shared `OAUTH_SCOPES` constant).

### Step 6 — Gate Review
| Gate | Verdict | Evidence |
|---|---|---|
| 1. Process | PASS | |
| 2. Reachability | **PASS** | Attacker needs only to obtain the secret via any leak vector on GitHub's side — a distinct, realistic external boundary, not "already has equivalent access" |
| 3. Real Impact | **PASS** | Full-mailbox info disclosure + excess Drive access, beyond what the job needs |
| 4. PoC Validation | PASS | Scope-string comparison is a complete, verifiable PoC |
| 5. Math Bounds | N/A | |
| 6. Environment | **PASS** | No environmental protection narrows the token's effective scope once issued |

**Verdict: TRUE POSITIVE — least-privilege violation.** All six gates pass. Severity:
Medium-High (as originally assessed).

---

## Bug #5 — Bare `assert` instead of a config-validation error

### Step 1 — Data Flow
- Source: `settings.oauth_token_file`, an operator-set env var.
- Sink: `assert settings.oauth_token_file is not None` (`google.py:125`).
- Trust boundary: none — operator's own configuration reaching the operator's own
  running process. One boundary, no escalation trigger.

### Step 2 — Exploitability
- No attacker control possible — the only way to trigger this is the operator's own
  incomplete `.env`/environment configuration.

### Step 3 — Impact
- Impact is a worse error message (`AssertionError` with no text, silently stripped
  under `python -O`) instead of a clean `ValueError` — pure operational robustness,
  not RCE/privesc/info-disclosure.

### Step 4 — PoC Sketch
```
Trigger: set INVOICING_OAUTH_CLIENT_SECRET_FILE but leave INVOICING_OAUTH_TOKEN_FILE
         unset -> require_sheet_config()/require_google_config() both pass (neither
         checks oauth_token_file) -> first provider construction hits bare assert
```

### Step 5 — Devil's Advocate (7 questions, abbreviated — same shape as Bug #1)
1-7: No attacker involved at any point; the defect is real (confirmed by reading
`config.py:82-109`'s field dicts directly, which omit `oauth_token_file`) but is a
pure config-UX/error-message quality issue. No question surfaces genuine uncertainty.

### Step 6 — Gate Review
| Gate | Verdict |
|---|---|
| 1. Process | PASS |
| 2. Reachability | N/A (no attacker) |
| 3. Real Impact | **FAIL** — error-message quality only |
| 4. PoC Validation | PASS (mechanism) |
| 5. Math Bounds | N/A |
| 6. Environment | PASS |

**Verdict: FALSE POSITIVE as a vulnerability (Gate 3 fails). Valid code-quality
defect.** Severity: Medium (as originally assessed, for the operational-robustness
question, not a security severity).

---

## Bug #6 — Empty-string env vars silently treated as unset

### Step 1 — Data Flow
- Source: `os.environ.get(name)` in `opt_env` (`config.py:54-58`).
- Sink: `... or None` collapses both "unset" and "set to ''" to `None`.
- Trust boundary: none — operator's own environment configuration. One boundary.

### Step 2 — Exploitability
- No attacker control — purely a config-authoring ambiguity for the operator's own
  deployment.

### Step 3 — Impact
- The report's own original text already establishes this degrades to a clear
  downstream error (via `require_sheet_config`/`require_google_config`'s falsy
  check) rather than a silent bypass — so even the operational impact is bounded.
  Not RCE/privesc/info-disclosure.

### Step 4 — PoC Sketch
```
Trigger: INVOICING_SHEET_ID="" (explicit empty override) -> opt_env returns None
         -> identical downstream behavior to INVOICING_SHEET_ID being entirely unset
```

### Step 5 — Devil's Advocate
No attacker involved; defect confirmed by reading `config.py:54-62` directly; impact
already self-bounded per the report's own text (raises cleanly downstream today).

### Step 6 — Gate Review
| Gate | Verdict |
|---|---|
| 1. Process | PASS |
| 2. Reachability | N/A (no attacker) |
| 3. Real Impact | **FAIL** — no security impact, and even the operational impact is bounded (fails cleanly today) |
| 4-6 | N/A / PASS |

**Verdict: FALSE POSITIVE as a vulnerability. Valid design smell.** Severity:
Low-Medium (as originally assessed).

---

## Bug #7 — No re-processing guard on billing/email state writes

### Step 1 — Data Flow
- Source: ids passed into `mark_lessons_billed`/`mark_invoice_emailed`
  (`db.py:336-341`, `408-413`).
- Sink: unconditional `UPDATE ... WHERE id = ?`, no state-guard in the WHERE clause.
- Trust boundary: none crossed by an external party — traced both current call sites
  (`pipeline.py`'s `bill_period` and `email_pending_invoices`) and confirmed, per
  `db.md` and `billing_and_pipeline.md`, that both only ever pass freshly-selected
  unbilled/pending ids today. One boundary (internal caller → DB layer), no
  escalation trigger.

### Step 2 — Exploitability
- No attacker control — the only way to trigger a double-write is a *future internal
  caller bug* (e.g., retry logic, a new CLI command), not any external input.

### Step 3 — Impact
- If ever triggered: silent state overwrite (data-integrity risk, potential
  double-billing/re-emailing) — real business impact, but not RCE/privesc/info-
  disclosure, and not reachable through any attacker-controlled input today.

### Step 4 — PoC Sketch
```
Trigger (hypothetical future bug): a caller passes an already-billed lesson id to
mark_lessons_billed -> UPDATE succeeds silently, billed_invoice_id overwritten,
no exception, no rowcount check
Current reality: no such caller exists in this codebase today (confirmed via
audit-context tracing of both call sites)
```

### Step 5 — Devil's Advocate
No attacker path exists today; this is correctly framed as a latent
defensive-programming gap for future code, not a live vulnerability. Re-read both
current call sites to confirm neither passes a stale/already-processed id today.

### Step 6 — Gate Review
| Gate | Verdict |
|---|---|
| 1. Process | PASS |
| 2. Reachability | **FAIL** — no current caller (internal or external) can trigger this |
| 3. Real Impact | Plausible but gated behind Gate 2's failure |
| 4-6 | N/A |

**Verdict: FALSE POSITIVE as a vulnerability (not reachable through any current code
path). Valid data-integrity / defensive-programming gap for future callers.**
Severity: Low-Medium (as originally assessed).

---

## Summary table

| # | Finding | Attacker involved? | Gate 3 (Real Impact) | Verdict (as vulnerability) | Retained as |
|---|---|---|---|---|---|
| 1 | `--confirm` gap | No | FAIL | FALSE POSITIVE | Critical misuse-resistance defect |
| 2 | pickle.load fallback | CI: no (unreachable); other: uncertain | PASS if reachable | FALSE POSITIVE (CI, proven unreachable) | Medium defense-in-depth defect |
| 3 | Header injection | Tested, mechanism blocked | FAIL (empirically) | FALSE POSITIVE (injection); TRUE POSITIVE (malformed header) | Low cosmetic defect |
| 4 | Excess OAuth scope | **Yes — distinct external actor** | **PASS** | **TRUE POSITIVE** | Medium-High least-privilege violation |
| 5 | Bare assert | No | FAIL | FALSE POSITIVE | Medium code-quality defect |
| 6 | Empty-string env fold | No | FAIL | FALSE POSITIVE | Low-Medium design smell |
| 7 | No re-processing guard | No (not reachable today) | Gated by Gate 2 fail | FALSE POSITIVE | Low-Medium data-integrity gap |
