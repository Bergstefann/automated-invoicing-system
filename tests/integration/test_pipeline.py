"""Integration tests: the full pipeline against fake providers and the
synthetic dataset. No test here touches a real network — see conftest.py's
autouse network guard.
"""

from __future__ import annotations

from datetime import UTC, datetime

from invoicing.billing import period_bounds
from invoicing.db import Database
from invoicing.models import SheetStatus
from invoicing.pipeline import email_pending_invoices, run_period
from invoicing.providers.base import ScheduleSnapshot
from invoicing.providers.fake import FakeDocProvider, FakeEmailProvider, FakeSheetProvider
from invoicing.seed import TERM_START, seed_database

NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)
PERIOD = 3  # seeded with 15 unbilled attended lessons, one per student

RUN_KWARGS = {
    "term_start": TERM_START,
    "period_number": PERIOD,
    "sender_name": "Jane Tutor",
    "sender_email": "jane@example.com",
    "personal_message": "Thanks for a great fortnight, <student>!",
}


def _fake_sheet() -> FakeSheetProvider:
    return FakeSheetProvider(snapshot=ScheduleSnapshot(students=[], lessons=[]))


def test_run_bills_every_unbilled_attended_lesson_and_sends_matching_emails(
    db: Database,
) -> None:
    seed_database(db)
    docs, sheet, email = FakeDocProvider(), _fake_sheet(), FakeEmailProvider()

    result = run_period(
        db, docs, email, sheet, dry_run=False, send_emails=True, now=NOW, **RUN_KWARGS
    )

    assert len(result.invoices_billed) == 15
    assert len(result.emails.sent) == 15
    assert len(email.sent) == 15

    bounds = period_bounds(TERM_START, PERIOD)
    for lesson in db.lessons_in_range(bounds.start_date, bounds.end_date):
        if lesson.is_billable:
            assert lesson.id is not None
            assert db.get_lesson(lesson.id).billed_invoice_id is not None


def test_dry_run_makes_zero_writes_to_db_sheet_or_email(db: Database) -> None:
    seed_database(db)
    docs, sheet, email = FakeDocProvider(), _fake_sheet(), FakeEmailProvider()
    invoices_before = db.list_invoices()

    result = run_period(
        db, docs, email, sheet, dry_run=True, send_emails=True, now=NOW, **RUN_KWARGS
    )

    assert result.invoices_billed == []
    assert result.emails.sent == []
    assert db.list_invoices() == invoices_before
    assert sheet.marked_billed == []
    assert email.sent == []
    assert docs.created_docs == {}


def test_rerun_after_a_successful_run_produces_zero_new_invoices(db: Database) -> None:
    seed_database(db)
    docs, sheet, email = FakeDocProvider(), _fake_sheet(), FakeEmailProvider()

    first = run_period(
        db, docs, email, sheet, dry_run=False, send_emails=True, now=NOW, **RUN_KWARGS
    )
    second = run_period(
        db, docs, email, sheet, dry_run=False, send_emails=True, now=NOW, **RUN_KWARGS
    )

    assert len(first.invoices_billed) == 15
    assert len(second.invoices_billed) == 0
    assert len(second.emails.sent) == 0
    period = db.get_period(PERIOD)
    assert period is not None and period.id is not None
    assert len(db.list_invoices(period_id=period.id)) == 15


def test_email_failure_mid_batch_keeps_sent_invoices_sent_and_lets_a_later_run_finish_the_rest(
    db: Database,
) -> None:
    seed_database(db)
    docs, sheet = FakeDocProvider(), _fake_sheet()
    flaky_email = FakeEmailProvider(fail_after=1)  # first send succeeds, the rest fail

    result = run_period(
        db, docs, flaky_email, sheet, dry_run=False, send_emails=True, now=NOW, **RUN_KWARGS
    )

    assert len(result.invoices_billed) == 15  # billing always completes in full
    assert len(result.emails.sent) == 1
    assert len(result.emails.failed) == 14

    period = db.get_period(PERIOD)
    assert period is not None and period.id is not None

    sent_numbers = set(result.emails.sent)
    for invoice in db.list_invoices(period_id=period.id):
        if invoice.invoice_number in sent_numbers:
            assert invoice.emailed_at is not None
        else:
            assert invoice.emailed_at is None

    pending_before_retry = db.invoices_pending_email(period.id)
    assert len(pending_before_retry) == 14

    healthy_email = FakeEmailProvider()
    retry = email_pending_invoices(
        db,
        docs,
        healthy_email,
        period.id,
        sender_name="Jane Tutor",
        sender_email="jane@example.com",
        personal_message="Thanks for a great fortnight, <student>!",
        now=NOW,
    )

    assert len(retry.sent) == 14
    assert len(healthy_email.sent) == 14
    assert db.invoices_pending_email(period.id) == []
    # the invoice sent in the first (partial) run was never re-sent
    assert set(retry.sent) == set(result.emails.failed)


def test_sheet_write_back_uses_the_preserved_invoiced_status_code(db: Database) -> None:
    seed_database(db)
    docs, sheet, email = FakeDocProvider(), _fake_sheet(), FakeEmailProvider()

    run_period(db, docs, email, sheet, dry_run=False, send_emails=True, now=NOW, **RUN_KWARGS)

    assert len(sheet.marked_billed) == 15
    for _ref, status in sheet.marked_billed:
        assert status is SheetStatus.INVOICED
        assert status.value == "YI"
