"""Integration tests: the full pipeline against fake providers and the
synthetic dataset. No test here touches a real network — see conftest.py's
autouse network guard.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from invoicing.billing import period_bounds
from invoicing.db import Database
from invoicing.models import AttendanceStatus, SheetStatus
from invoicing.pipeline import (
    bill_period,
    email_pending_invoices,
    preview_period,
    run_period,
    sync_schedule_into_db,
)
from invoicing.providers.base import ScheduleSnapshot, SheetLessonRecord, SheetStudentRecord
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


def test_sync_excludes_yi_lessons_from_future_billing(db: Database) -> None:
    """Regression test: YI ("Yes, Invoiced") means a lesson was already
    billed by the pre-rebuild pipeline. sync must exclude it from future
    billing even though no Invoice row for it exists in this DB — otherwise
    a real run would re-invoice every already-billed lesson on the sheet."""
    snapshot = ScheduleSnapshot(
        students=[
            SheetStudentRecord(
                first_name="Will",
                last_name="Example",
                billing_type="Private",
                parent_name="Will Parent",
                parent_email="will.parent@example.com",
                rate_cents=4000,
            )
        ],
        lessons=[
            SheetLessonRecord(student_first_name="Will", lesson_date=TERM_START, status="YI"),
            SheetLessonRecord(
                student_first_name="Will",
                lesson_date=TERM_START + timedelta(days=7),
                status="Y",
            ),
        ],
    )
    sheet = FakeSheetProvider(snapshot=snapshot)

    synced = sync_schedule_into_db(db, sheet)
    assert synced == 2

    bounds = period_bounds(TERM_START, 1)
    by_date = {
        lesson.lesson_date: lesson
        for lesson in db.lessons_in_range(bounds.start_date, bounds.end_date)
    }

    already_billed = by_date[TERM_START]
    assert already_billed.attendance_status is AttendanceStatus.ATTENDED
    assert already_billed.billed_invoice_id is None
    assert already_billed.pre_billed is True
    assert already_billed.is_unbilled is False

    still_unbilled = by_date[TERM_START + timedelta(days=7)]
    assert still_unbilled.pre_billed is False
    assert still_unbilled.is_unbilled is True

    _, preview_lines = preview_period(db, TERM_START, 1)
    assert len(preview_lines) == 1
    assert preview_lines[0].lesson_count == 1


def test_rerun_after_partial_email_failure_creates_no_new_invoices_and_sends_only_pending(
    db: Database,
) -> None:
    """Regression test for rule 7: a batch that partially fails to email
    must not be re-billed on the next run — only the still-pending invoices
    get (re-)sent, and re-billing must create zero new invoices."""
    seed_database(db)
    docs, sheet = FakeDocProvider(), _fake_sheet()
    flaky_email = FakeEmailProvider(fail_after=10)  # first 10 sends succeed, rest fail

    first = run_period(
        db, docs, flaky_email, sheet, dry_run=False, send_emails=True, now=NOW, **RUN_KWARGS
    )
    assert len(first.invoices_billed) == 15
    assert len(first.emails.sent) == 10
    assert len(first.emails.failed) == 5

    healthy_email = FakeEmailProvider()
    second = run_period(
        db, docs, healthy_email, sheet, dry_run=False, send_emails=True, now=NOW, **RUN_KWARGS
    )

    assert len(second.invoices_billed) == 0  # nothing re-billed
    assert len(second.emails.sent) == 5  # only the previously-failed 5 get sent

    period = db.get_period(PERIOD)
    assert period is not None and period.id is not None
    assert len(db.list_invoices(period_id=period.id)) == 15  # never doubled


def test_a_pdf_export_failure_fails_only_that_invoice_not_the_whole_batch(db: Database) -> None:
    """Regression test: a PDF export failure for one invoice (e.g. a
    transient Drive error) must degrade like a failed send — recorded as
    failed, retryable later — not crash the rest of the email batch."""
    seed_database(db)
    docs, sheet, email = FakeDocProvider(), _fake_sheet(), FakeEmailProvider()

    result = run_period(
        db, docs, email, sheet, dry_run=False, send_emails=False, now=NOW, **RUN_KWARGS
    )
    assert len(result.invoices_billed) == 15

    period = db.get_period(PERIOD)
    assert period is not None and period.id is not None
    invoices = db.list_invoices(period_id=period.id)
    bad_doc_id = invoices[0].doc_url
    assert bad_doc_id is not None
    docs.fail_export_doc_ids.add(bad_doc_id)

    emails = email_pending_invoices(
        db,
        docs,
        email,
        period.id,
        sender_name="Jane Tutor",
        sender_email="jane@example.com",
        personal_message="Thanks for a great fortnight, <student>!",
        now=NOW,
    )

    assert emails.failed == [invoices[0].invoice_number]
    assert len(emails.sent) == 14


def test_sync_does_not_fork_a_new_student_when_the_parents_email_changes(db: Database) -> None:
    """Regression test for a real double-billing incident: a parent's Sheet
    email changing between syncs (e.g. switching to a +alias for safe
    testing) must not create a second Student/Parent pair for the same kid.
    get_or_create_parent keys on email, so a changed email alone used to
    fork identity in get_or_create_student too — and every lesson for that
    "new" student then looked unbilled and got billed again."""

    def snapshot(parent_email: str) -> ScheduleSnapshot:
        return ScheduleSnapshot(
            students=[
                SheetStudentRecord(
                    first_name="Will",
                    last_name="Example",
                    billing_type="Private",
                    parent_name="Will Parent",
                    parent_email=parent_email,
                    rate_cents=4000,
                )
            ],
            lessons=[
                SheetLessonRecord(student_first_name="Will", lesson_date=TERM_START, status="Y"),
            ],
        )

    sync_schedule_into_db(db, FakeSheetProvider(snapshot=snapshot("will@example.com")))
    docs, sheet, email = FakeDocProvider(), _fake_sheet(), FakeEmailProvider()
    run_period(
        db,
        docs,
        email,
        sheet,
        term_start=TERM_START,
        period_number=1,
        sender_name="Jane Tutor",
        sender_email="jane@example.com",
        personal_message="Thanks!",
        dry_run=False,
        send_emails=True,
        now=NOW,
    )
    assert len(db.list_students()) == 1
    billed_lesson = db.lessons_in_range(TERM_START, TERM_START)[0]
    assert billed_lesson.billed_invoice_id is not None

    # Same kid, new parent contact email — not a new family.
    sync_schedule_into_db(db, FakeSheetProvider(snapshot=snapshot("will.new@example.com")))

    assert len(db.list_students()) == 1  # no forked duplicate
    still_billed = db.lessons_in_range(TERM_START, TERM_START)[0]
    assert still_billed.id == billed_lesson.id
    assert still_billed.billed_invoice_id == billed_lesson.billed_invoice_id

    _, second_billed = bill_period(db, docs, sheet, term_start=TERM_START, period_number=1, now=NOW)
    assert len(second_billed) == 0  # nothing re-billed
