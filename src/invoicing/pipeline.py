"""Orchestration: sync the Sheet into SQLite, bill a period, email invoices.

Billing and emailing are deliberately two separate, independently-idempotent
phases:

  1. Billing creates an invoice (+ Doc + lines) for every currently-unbilled
     attended lesson group and marks those lessons billed. Once a lesson is
     billed it will never be picked up again — that's rule 1 (idempotency).
  2. Emailing looks up every invoice in the period with `emailed_at IS NULL`
     — which includes invoices billed just now *and* any left over from a
     previous run whose email attempt failed — and tries to send each one
     independently. A failure on one invoice doesn't touch the others: rule 7
     (partial failure doesn't corrupt state) falls out of that independence,
     not from any special-case recovery code.

Splitting it this way means a lesson being billed and its invoice being
emailed are different facts the database can represent separately, so a
failed send is retryable without ever re-billing or double-sending.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from invoicing.billing import (
    InvoiceTotals,
    PeriodBounds,
    compute_totals,
    group_by_student,
    is_period_completed,
    period_bounds,
    unbilled_lessons_in_period,
)
from invoicing.db import Database
from invoicing.invoice_numbers import next_invoice_number
from invoicing.models import AttendanceStatus, Invoice, InvoiceLine, Lesson, SheetStatus, Student
from invoicing.providers.base import (
    DocProvider,
    EmailAttachment,
    EmailProvider,
    InvoiceDocData,
    InvoiceDocLine,
    SheetLessonRef,
    SheetProvider,
)
from invoicing.templates.invoice_email import DAY_NAMES, render_invoice_email

BILLABLE_TYPE = "Private"
RAW_STATUS_ATTENDED = {"Y", "YI"}
DEFAULT_LESSON_DURATION_MINUTES = 30


def sync_schedule_into_db(db: Database, sheet: SheetProvider) -> int:
    """Pulls the Sheet into SQLite.

    Only `billing_type == "Private"` rows are synced — the original pipeline
    never invoiced anyone else. That gate is applied here, at sync time,
    rather than as a stored column, since nothing downstream needs to know a
    student's billing type once a decision has been made to track them.

    The original sheet doesn't record instrument or school, so synced
    students get placeholder values for those two fields; they're only ever
    populated for real by the synthetic seed dataset.
    """
    snapshot = sheet.read_schedule()

    student_ids: dict[str, int] = {}
    for record in snapshot.students:
        if record.billing_type != BILLABLE_TYPE:
            continue
        parent = db.get_or_create_parent(record.parent_name, record.parent_email)
        assert parent.id is not None
        display_name = f"{record.first_name} {record.last_name}".strip()
        student = db.get_or_create_student(
            name=display_name,
            parent_id=parent.id,
            instrument="Unspecified",
            rate_cents=record.rate_cents,
            school="Unspecified",
        )
        assert student.id is not None
        student_ids[record.first_name.lower()] = student.id

    synced = 0
    for lesson_record in snapshot.lessons:
        student_id = student_ids.get(lesson_record.student_first_name.lower())
        if student_id is None:
            continue
        status = (
            AttendanceStatus.ATTENDED
            if lesson_record.status in RAW_STATUS_ATTENDED
            else AttendanceStatus.ABSENT_UNNOTIFIED
        )
        # YI ("Yes, Invoiced") means attended *and already billed* — by the
        # pre-rebuild pipeline, before this DB existed. Without this, a
        # lesson synced as YI would land as ATTENDED with no
        # billed_invoice_id and get billed again on the next real run.
        db.get_or_create_lesson(
            student_id=student_id,
            lesson_date=lesson_record.lesson_date,
            duration_minutes=DEFAULT_LESSON_DURATION_MINUTES,
            attendance_status=status,
            pre_billed=lesson_record.status == SheetStatus.INVOICED.value,
        )
        synced += 1
    return synced


@dataclass(frozen=True)
class InvoicePreviewLine:
    student: Student
    lesson_count: int
    subtotal_cents: int
    total_cents: int


def preview_period(
    db: Database, term_start: date, period_number: int
) -> tuple[PeriodBounds, list[InvoicePreviewLine]]:
    """Read-only: what *would* be billed. Never writes anything."""
    bounds = period_bounds(term_start, period_number)
    lessons = db.lessons_in_range(bounds.start_date, bounds.end_date)
    grouped = group_by_student(unbilled_lessons_in_period(lessons, bounds))

    lines = []
    for student_id, student_lessons in sorted(grouped.items()):
        student = db.get_student(student_id)
        totals = compute_totals(student, student_lessons)
        lines.append(
            InvoicePreviewLine(
                student=student,
                lesson_count=len(student_lessons),
                subtotal_cents=totals.subtotal_cents,
                total_cents=totals.total_cents,
            )
        )
    return bounds, lines


def _build_doc_data(
    student: Student,
    parent_name: str,
    parent_email: str,
    invoice_number: str,
    today: date,
    lessons: list[Lesson],
) -> InvoiceDocData:
    lines = [
        InvoiceDocLine(
            day_of_week=DAY_NAMES[lesson.lesson_date.weekday()],
            date_str=lesson.lesson_date.strftime("%d/%m"),
            student_display_name=student.name,
            rate_cents=student.rate_cents,
        )
        for lesson in lessons
    ]
    totals = compute_totals(student, lessons)
    return InvoiceDocData(
        parent_name=parent_name or student.name,
        parent_email=parent_email,
        invoice_number=invoice_number,
        invoice_date=today,
        student_display_name=student.name,
        subtotal_cents=totals.subtotal_cents,
        gst_cents=totals.gst_cents,
        total_cents=totals.total_cents,
        lines=lines,
    )


def bill_period(
    db: Database,
    docs: DocProvider,
    sheet: SheetProvider,
    *,
    term_start: date,
    period_number: int,
    now: datetime,
) -> tuple[PeriodBounds, list[Invoice]]:
    """Phase 1: create invoices for currently-unbilled attended lessons.

    Idempotent — a lesson already carrying a `billed_invoice_id` is excluded
    by `unbilled_lessons_in_period`, so calling this twice for the same
    period creates zero new invoices the second time.
    """
    today = now.date()
    bounds = period_bounds(term_start, period_number)
    if not is_period_completed(bounds.end_date, today):
        raise ValueError(f"period {period_number} has not finished yet (ends {bounds.end_date})")

    period = db.get_or_create_period(period_number, bounds.start_date, bounds.end_date)
    assert period.id is not None

    lessons = db.lessons_in_range(bounds.start_date, bounds.end_date)
    grouped = group_by_student(unbilled_lessons_in_period(lessons, bounds))

    created: list[Invoice] = []
    sheet_refs: list[SheetLessonRef] = []

    for student_id, student_lessons in sorted(grouped.items()):
        student = db.get_student(student_id)
        parent = db.get_parent(student.parent_id)
        totals = compute_totals(student, student_lessons)

        invoice_number = next_invoice_number(today, db.count_invoices_issued_on(today))
        doc_data = _build_doc_data(
            student, parent.name, parent.email, invoice_number, today, student_lessons
        )
        doc_id = docs.create_invoice_doc(doc_data)

        invoice = db.insert_invoice(
            Invoice(
                invoice_number=invoice_number,
                student_id=student_id,
                parent_id=student.parent_id,
                period_id=period.id,
                issued_at=now,
                subtotal_cents=totals.subtotal_cents,
                gst_cents=totals.gst_cents,
                total_cents=totals.total_cents,
                doc_url=doc_id,
            )
        )
        assert invoice.id is not None
        lines_to_insert: list[InvoiceLine] = []
        for lesson in student_lessons:
            assert lesson.id is not None
            lines_to_insert.append(
                InvoiceLine(
                    invoice_id=invoice.id, lesson_id=lesson.id, rate_cents=student.rate_cents
                )
            )
        db.insert_invoice_lines(lines_to_insert)
        lesson_ids = [lesson.id for lesson in student_lessons if lesson.id is not None]
        db.mark_lessons_billed(lesson_ids, invoice.id)

        created.append(invoice)
        sheet_refs.extend(
            SheetLessonRef(student_key=student.name, lesson_date=lesson.lesson_date)
            for lesson in student_lessons
        )

    if sheet_refs:
        sheet.mark_lessons_billed(sheet_refs, SheetStatus.INVOICED)

    return bounds, created


@dataclass(frozen=True)
class EmailRunResult:
    sent: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped_no_contact: list[str] = field(default_factory=list)


def email_pending_invoices(
    db: Database,
    docs: DocProvider,
    email: EmailProvider,
    period_id: int,
    *,
    sender_name: str,
    sender_email: str,
    personal_message: str,
    now: datetime,
) -> EmailRunResult:
    """Phase 2: send every not-yet-emailed invoice in a period.

    Each invoice is attempted independently and failures don't abort the
    batch — an invoice's `emailed_at` only ever gets set on that invoice's
    own successful send, so re-running this after a partial failure resumes
    exactly where it left off (rule 7).
    """
    result = EmailRunResult()

    for invoice in db.invoices_pending_email(period_id):
        student = db.get_student(invoice.student_id)
        parent = db.get_parent(invoice.parent_id)

        if not parent.email:
            result.skipped_no_contact.append(invoice.invoice_number)
            continue

        lines = db.invoice_lines_for(invoice.id)  # type: ignore[arg-type]
        lessons = [db.get_lesson(line.lesson_id) for line in lines]
        rate_cents = lines[0].rate_cents if lines else student.rate_cents

        totals = InvoiceTotals(
            subtotal_cents=invoice.subtotal_cents,
            gst_cents=invoice.gst_cents,
            total_cents=invoice.total_cents,
        )
        resolved_message = personal_message.replace("<student>", student.name.split()[0]).replace(
            "<parent>", (parent.name or student.name).split()[0]
        )
        html_body = render_invoice_email(
            personal_message=resolved_message,
            student_display_name=student.name,
            lessons=lessons,
            rate_cents=rate_cents,
            invoice_number=invoice.invoice_number,
            invoice_date=invoice.issued_at.date(),
            totals=totals,
            sender_name=sender_name,
            sender_email=sender_email,
        )

        assert invoice.doc_url is not None
        pdf_filename = f"Invoice_{invoice.invoice_number}_{student.name.replace(' ', '_')}.pdf"
        # export_pdf and send are one unit for rule 7's purposes: either can
        # fail transiently (a Drive export hiccup is as likely as a Gmail
        # one), and either failing must degrade to "retry this invoice next
        # run" rather than crash the whole batch and abandon invoices after
        # this one that would otherwise have sent fine.
        try:
            pdf_bytes = docs.export_pdf(invoice.doc_url)
            email.send(
                to=parent.email,
                subject=f"Invoice {invoice.invoice_number} — {student.name}",
                html_body=html_body,
                attachment=EmailAttachment(filename=pdf_filename, content=pdf_bytes),
            )
        except Exception:
            result.failed.append(invoice.invoice_number)
            continue

        db.mark_invoice_emailed(invoice.id, now)  # type: ignore[arg-type]
        result.sent.append(invoice.invoice_number)

    return result


@dataclass(frozen=True)
class RunResult:
    bounds: PeriodBounds
    invoices_billed: list[Invoice]
    emails: EmailRunResult


def run_period(
    db: Database,
    docs: DocProvider,
    email: EmailProvider,
    sheet: SheetProvider,
    *,
    term_start: date,
    period_number: int,
    sender_name: str,
    sender_email: str,
    personal_message: str,
    dry_run: bool,
    send_emails: bool,
    now: datetime,
) -> RunResult:
    """The full pipeline for one period.

    `dry_run=True` performs none of the below and guarantees zero writes to
    the database, the Sheet, or email — it never calls `bill_period` or
    `email_pending_invoices` at all. With `dry_run=False`, billing always
    happens; `send_emails=False` lets you commit invoices without sending
    anything yet (useful for reviewing before the batch goes out) — emails
    can always be sent later by calling this again with `send_emails=True`,
    since `email_pending_invoices` picks up wherever it left off.
    """
    if dry_run:
        bounds = period_bounds(term_start, period_number)
        return RunResult(bounds=bounds, invoices_billed=[], emails=EmailRunResult())

    bounds, billed = bill_period(
        db, docs, sheet, term_start=term_start, period_number=period_number, now=now
    )
    period = db.get_period(period_number)
    assert period is not None and period.id is not None

    if not send_emails:
        return RunResult(bounds=bounds, invoices_billed=billed, emails=EmailRunResult())

    emails = email_pending_invoices(
        db,
        docs,
        email,
        period.id,
        sender_name=sender_name,
        sender_email=sender_email,
        personal_message=personal_message,
        now=now,
    )
    return RunResult(bounds=bounds, invoices_billed=billed, emails=emails)
