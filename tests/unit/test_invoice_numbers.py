"""Unit tests for invoice_numbers.py: format, sequencing, and uniqueness.

Uniqueness across *runs* (not just within one process) is the property the
original pipeline didn't actually guarantee — see invoice_numbers.py's
docstring. The DB-backed tests here exercise that specifically.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from invoicing.billing import period_bounds
from invoicing.db import Database
from invoicing.invoice_numbers import MAX_DAILY_SEQUENCE, next_invoice_number
from invoicing.models import AttendanceStatus, Lesson, Parent, Student
from invoicing.pipeline import bill_period
from invoicing.providers.base import ScheduleSnapshot
from invoicing.providers.fake import FakeDocProvider, FakeSheetProvider


def test_invoice_number_format_is_ddmmyy_plus_two_digit_sequence() -> None:
    assert next_invoice_number(date(2026, 8, 15), 0) == "15082601"


def test_invoice_number_sequence_starts_at_01_for_first_invoice_of_day() -> None:
    number = next_invoice_number(date(2026, 3, 1), already_issued_today=0)
    assert number.endswith("01")


def test_invoice_number_increments_for_successive_invoices_same_day() -> None:
    numbers = [next_invoice_number(date(2026, 3, 1), n) for n in range(5)]
    assert numbers == ["01032601", "01032602", "01032603", "01032604", "01032605"]


def test_invoice_number_rejects_more_than_max_daily_sequence() -> None:
    next_invoice_number(date(2026, 3, 1), MAX_DAILY_SEQUENCE - 1)  # last valid seq: fine
    with pytest.raises(ValueError):
        next_invoice_number(date(2026, 3, 1), MAX_DAILY_SEQUENCE)


def test_invoice_numbers_differ_across_days_even_at_the_same_sequence_position() -> None:
    day_one = next_invoice_number(date(2026, 3, 1), 0)
    day_two = next_invoice_number(date(2026, 3, 2), 0)
    assert day_one != day_two


TERM_START = date(2026, 2, 2)


def _seeded_student_with_lesson(db: Database, period_number: int) -> None:
    parent = db.insert_parent(
        Parent(name=f"Parent {period_number}", email=f"parent{period_number}@example.com")
    )
    assert parent.id is not None
    student = db.insert_student(
        Student(
            name=f"Student {period_number}",
            parent_id=parent.id,
            instrument="Piano",
            rate_cents=4000,
            school="Example School",
        )
    )
    assert student.id is not None
    bounds = period_bounds(TERM_START, period_number)
    db.insert_lesson(
        Lesson(
            student_id=student.id,
            lesson_date=bounds.start_date,
            duration_minutes=30,
            attendance_status=AttendanceStatus.ATTENDED,
        )
    )


def test_invoice_numbers_stay_unique_across_separate_bill_period_calls_same_day(
    db: Database, fixed_now: datetime
) -> None:
    _seeded_student_with_lesson(db, period_number=1)
    _seeded_student_with_lesson(db, period_number=2)
    docs = FakeDocProvider()
    sheet = FakeSheetProvider(snapshot=ScheduleSnapshot(students=[], lessons=[]))

    _, first_batch = bill_period(
        db, docs, sheet, term_start=TERM_START, period_number=1, now=fixed_now
    )
    _, second_batch = bill_period(
        db, docs, sheet, term_start=TERM_START, period_number=2, now=fixed_now
    )

    all_numbers = [inv.invoice_number for inv in first_batch + second_batch]
    assert len(all_numbers) == len(set(all_numbers))


def test_invoice_number_sequence_continues_across_calls_not_reset_per_call(
    db: Database, fixed_now: datetime
) -> None:
    _seeded_student_with_lesson(db, period_number=1)
    _seeded_student_with_lesson(db, period_number=2)
    docs = FakeDocProvider()
    sheet = FakeSheetProvider(snapshot=ScheduleSnapshot(students=[], lessons=[]))

    _, first_batch = bill_period(
        db, docs, sheet, term_start=TERM_START, period_number=1, now=fixed_now
    )
    _, second_batch = bill_period(
        db, docs, sheet, term_start=TERM_START, period_number=2, now=fixed_now
    )

    assert first_batch[0].invoice_number.endswith("01")
    assert second_batch[0].invoice_number.endswith("02")


def test_invoice_numbers_use_the_issue_date_not_the_lesson_date(
    db: Database, fixed_now: datetime
) -> None:
    _seeded_student_with_lesson(db, period_number=1)
    docs = FakeDocProvider()
    sheet = FakeSheetProvider(snapshot=ScheduleSnapshot(students=[], lessons=[]))

    _, invoices = bill_period(
        db, docs, sheet, term_start=TERM_START, period_number=1, now=fixed_now
    )

    expected_prefix = fixed_now.date().strftime("%d%m%y")
    assert invoices[0].invoice_number.startswith(expected_prefix)
    # sanity: the lesson itself was billed weeks before the fixed "now"
    bounds = period_bounds(TERM_START, 1)
    assert bounds.start_date < fixed_now.date() - timedelta(days=1)
