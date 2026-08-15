"""Idempotency tests: re-running billing never re-bills a lesson, never
mints a duplicate invoice number, and never re-touches the Sheet for work
that was already billed."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from invoicing.billing import period_bounds
from invoicing.db import Database
from invoicing.models import AttendanceStatus, Lesson, Parent, Student
from invoicing.pipeline import bill_period
from invoicing.providers.base import ScheduleSnapshot
from invoicing.providers.fake import FakeDocProvider, FakeSheetProvider

TERM_START = date(2026, 2, 2)


def _seed_one_student_two_lessons(db: Database) -> tuple[Student, list[Lesson]]:
    parent = db.insert_parent(Parent(name="Jordan Example", email="jordan@example.com"))
    assert parent.id is not None
    student = db.insert_student(
        Student(
            name="Riley Example",
            parent_id=parent.id,
            instrument="Piano",
            rate_cents=4000,
            school="Example School",
        )
    )
    assert student.id is not None
    bounds = period_bounds(TERM_START, 1)
    lessons = [
        db.insert_lesson(
            Lesson(
                student_id=student.id,
                lesson_date=bounds.start_date,
                duration_minutes=30,
                attendance_status=AttendanceStatus.ATTENDED,
            )
        ),
        db.insert_lesson(
            Lesson(
                student_id=student.id,
                lesson_date=bounds.start_date + timedelta(days=7),
                duration_minutes=30,
                attendance_status=AttendanceStatus.ATTENDED,
            )
        ),
    ]
    return student, lessons


def _fake_sheet() -> FakeSheetProvider:
    return FakeSheetProvider(snapshot=ScheduleSnapshot(students=[], lessons=[]))


def test_billing_a_period_twice_creates_no_duplicate_invoices(
    db: Database, fixed_now: datetime
) -> None:
    _seed_one_student_two_lessons(db)
    docs = FakeDocProvider()
    sheet = _fake_sheet()

    _, first_run = bill_period(
        db, docs, sheet, term_start=TERM_START, period_number=1, now=fixed_now
    )
    _, second_run = bill_period(
        db, docs, sheet, term_start=TERM_START, period_number=1, now=fixed_now
    )

    assert len(first_run) == 1
    assert len(second_run) == 0
    assert len(db.list_invoices()) == 1


def test_a_billed_lesson_is_never_returned_as_unbilled_again(
    db: Database, fixed_now: datetime
) -> None:
    _student, lessons = _seed_one_student_two_lessons(db)
    docs = FakeDocProvider()
    sheet = _fake_sheet()

    bill_period(db, docs, sheet, term_start=TERM_START, period_number=1, now=fixed_now)

    for lesson in lessons:
        assert lesson.id is not None
        refreshed = db.get_lesson(lesson.id)
        assert refreshed.billed_invoice_id is not None
        assert refreshed.is_unbilled is False


def test_double_run_back_to_back_mints_exactly_one_invoice_number(
    db: Database, fixed_now: datetime
) -> None:
    _seed_one_student_two_lessons(db)
    docs = FakeDocProvider()
    sheet = _fake_sheet()

    _, first = bill_period(db, docs, sheet, term_start=TERM_START, period_number=1, now=fixed_now)
    _, second = bill_period(db, docs, sheet, term_start=TERM_START, period_number=1, now=fixed_now)

    invoice_numbers = {inv.invoice_number for inv in first} | {inv.invoice_number for inv in second}
    assert len(invoice_numbers) == 1


def test_re_running_after_billing_does_not_touch_the_sheet_again(
    db: Database, fixed_now: datetime
) -> None:
    _seed_one_student_two_lessons(db)
    docs = FakeDocProvider()
    sheet = _fake_sheet()

    bill_period(db, docs, sheet, term_start=TERM_START, period_number=1, now=fixed_now)
    calls_after_first_run = len(sheet.marked_billed)
    bill_period(db, docs, sheet, term_start=TERM_START, period_number=1, now=fixed_now)

    assert calls_after_first_run > 0
    assert len(sheet.marked_billed) == calls_after_first_run


def test_a_new_unbilled_lesson_after_billing_is_the_only_thing_billed_next_run(
    db: Database, fixed_now: datetime
) -> None:
    student, lessons = _seed_one_student_two_lessons(db)
    docs = FakeDocProvider()
    sheet = _fake_sheet()
    bill_period(db, docs, sheet, term_start=TERM_START, period_number=1, now=fixed_now)

    assert student.id is not None
    bounds = period_bounds(TERM_START, 1)
    new_lesson = db.insert_lesson(
        Lesson(
            student_id=student.id,
            lesson_date=bounds.start_date + timedelta(days=1),
            duration_minutes=30,
            attendance_status=AttendanceStatus.ATTENDED,
        )
    )

    _, second_run = bill_period(
        db, docs, sheet, term_start=TERM_START, period_number=1, now=fixed_now
    )

    assert len(second_run) == 1
    assert new_lesson.id is not None
    assert db.get_lesson(new_lesson.id).billed_invoice_id == second_run[0].id
    for lesson in lessons:
        assert lesson.id is not None
        assert db.get_lesson(lesson.id).billed_invoice_id != second_run[0].id
