"""Scheduled dry-run preview: syncs the Sheet, previews the most recently
completed billing period, and emails the result to the operator for
review. Never bills, never sends an invoice — `run --confirm` stays a
deliberate, manual step every time. See
docs/POSTMORTEM-double-billing.md for why unattended billing is the
wrong design for this system: the last time this pipeline ran
unsupervised against a Sheet that had changed underneath it, it billed
and emailed the same students twice.

Run by the "Scheduled preview" GitHub Actions workflow
(.github/workflows/scheduled-preview.yml). Can also be run locally
against a real .env for testing:

    python deploy/scheduled_preview.py
"""

from __future__ import annotations

from datetime import UTC, datetime

from invoicing.billing import period_number_for_date
from invoicing.config import Settings
from invoicing.db import Database
from invoicing.pipeline import InvoicePreviewLine, preview_period, sync_schedule_into_db
from invoicing.providers.google import (
    EMAIL_SCOPES,
    SHEETS_SCOPES,
    GoogleEmailProvider,
    GoogleSheetProvider,
)

# This job only reads the Sheet and sends one preview email — never touches
# Docs/Drive — so the token it mints (and the GitHub secret that holds it)
# should carry only these two scopes, not the full set `cli.py`'s `run
# --real` requests. Both providers below must request this same combined
# set since they share one cached token file.
SCOPES = SHEETS_SCOPES + EMAIL_SCOPES


def _fmt_cents(cents: int) -> str:
    return f"${cents / 100:.2f}"


def _report_for_completed_period(settings: Settings, period_number: int) -> tuple[str, str]:
    with Database(settings.db_path) as db:
        bounds, lines = preview_period(db, settings.term_start_date, period_number)

    if not lines:
        subject = f"Invoicing preview — period {period_number}: nothing pending"
        body = (
            f"<p>Period {period_number} ({bounds.start_date} to {bounds.end_date}) "
            "is complete. Nothing unbilled — nothing to do.</p>"
        )
        return subject, body

    total_cents = sum(line.total_cents for line in lines)

    def _row(line: InvoicePreviewLine) -> str:
        return (
            f"<tr><td>{line.student.name}</td><td>{line.lesson_count}</td>"
            f"<td>{_fmt_cents(line.total_cents)}</td></tr>"
        )

    subject = f"Invoicing preview — period {period_number}: {len(lines)} student(s) ready"
    body = (
        f"<p>Period {period_number} ({bounds.start_date} to {bounds.end_date}) is complete "
        f"and ready to bill: {len(lines)} student(s), {_fmt_cents(total_cents)} total.</p>"
        "<table border='1' cellpadding='4' cellspacing='0'>"
        "<tr><th>Student</th><th>Lessons</th><th>Total</th></tr>"
        f"{''.join(_row(line) for line in lines)}"
        "</table>"
        "<p>This is a preview only — nothing has been billed or sent. Review it, then "
        "run this yourself when you're ready:</p>"
        f"<pre>invoicing run --period {period_number} --real --no-dry-run --confirm</pre>"
    )
    return subject, body


def main() -> None:
    settings = Settings.from_env()
    settings.require_sheet_config()

    sheet = GoogleSheetProvider(settings, scopes=SCOPES)
    with Database(settings.db_path) as db:
        sync_schedule_into_db(db, sheet)

    today = datetime.now(UTC).date()
    current_period = period_number_for_date(settings.term_start_date, today)

    if current_period <= 1:
        subject = "Invoicing preview: nothing completed yet"
        body = "<p>The term's first period hasn't completed yet — nothing to review.</p>"
    else:
        subject, body = _report_for_completed_period(settings, current_period - 1)

    email = GoogleEmailProvider(settings, scopes=SCOPES)
    email.send(to=settings.sender_email, subject=subject, html_body=body)


if __name__ == "__main__":
    main()
