"""Unit tests for billing.py: pure period math, unbilled detection, totals."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from invoicing.billing import (
    PERIOD_LENGTH_DAYS,
    compute_totals,
    group_by_student,
    is_period_completed,
    period_bounds,
    period_number_for_date,
    unbilled_lessons_in_period,
)
from invoicing.models import AttendanceStatus, Lesson, Student

TERM_START = date(2026, 2, 2)


def test_period_bounds_first_period_is_fourteen_days_inclusive() -> None:
    bounds = period_bounds(TERM_START, 1)
    assert bounds.start_date == date(2026, 2, 2)
    assert bounds.end_date == date(2026, 2, 15)
    assert (bounds.end_date - bounds.start_date).days == PERIOD_LENGTH_DAYS - 1


def test_period_bounds_second_period_starts_immediately_after_first() -> None:
    first = period_bounds(TERM_START, 1)
    second = period_bounds(TERM_START, 2)
    assert second.start_date == first.end_date + timedelta(days=1)


def test_period_bounds_rejects_period_number_below_one() -> None:
    with pytest.raises(ValueError):
        period_bounds(TERM_START, 0)


def test_period_number_for_date_on_term_start_is_period_one() -> None:
    assert period_number_for_date(TERM_START, TERM_START) == 1


def test_period_number_for_date_on_last_day_of_period_one_is_still_period_one() -> None:
    assert period_number_for_date(TERM_START, date(2026, 2, 15)) == 1


def test_period_number_for_date_on_first_day_of_period_two_is_period_two() -> None:
    assert period_number_for_date(TERM_START, date(2026, 2, 16)) == 2


def test_period_number_for_date_rejects_dates_before_term_start() -> None:
    with pytest.raises(ValueError):
        period_number_for_date(TERM_START, TERM_START - timedelta(days=1))


def test_is_period_completed_requires_period_end_strictly_before_today() -> None:
    bounds = period_bounds(TERM_START, 1)
    assert is_period_completed(bounds.end_date, bounds.end_date + timedelta(days=1)) is True
    assert is_period_completed(bounds.end_date, bounds.end_date) is False
    assert is_period_completed(bounds.end_date, bounds.end_date - timedelta(days=1)) is False


def _lesson(
    student_id: int, day: date, status: AttendanceStatus, billed: int | None = None
) -> Lesson:
    return Lesson(
        student_id=student_id,
        lesson_date=day,
        duration_minutes=30,
        attendance_status=status,
        billed_invoice_id=billed,
    )


def test_unbilled_lessons_in_period_excludes_lessons_outside_the_window() -> None:
    bounds = period_bounds(TERM_START, 1)
    inside = _lesson(1, bounds.start_date, AttendanceStatus.ATTENDED)
    before = _lesson(1, bounds.start_date - timedelta(days=1), AttendanceStatus.ATTENDED)
    after = _lesson(1, bounds.end_date + timedelta(days=1), AttendanceStatus.ATTENDED)
    result = unbilled_lessons_in_period([inside, before, after], bounds)
    assert result == [inside]


def test_unbilled_lessons_in_period_excludes_absences() -> None:
    bounds = period_bounds(TERM_START, 1)
    attended = _lesson(1, bounds.start_date, AttendanceStatus.ATTENDED)
    absent_notified = _lesson(1, bounds.start_date, AttendanceStatus.ABSENT_NOTIFIED)
    absent_unnotified = _lesson(1, bounds.start_date, AttendanceStatus.ABSENT_UNNOTIFIED)
    result = unbilled_lessons_in_period([attended, absent_notified, absent_unnotified], bounds)
    assert result == [attended]


def test_unbilled_lessons_in_period_excludes_already_billed_lessons() -> None:
    bounds = period_bounds(TERM_START, 1)
    billed = _lesson(1, bounds.start_date, AttendanceStatus.ATTENDED, billed=99)
    unbilled = _lesson(1, bounds.start_date, AttendanceStatus.ATTENDED)
    result = unbilled_lessons_in_period([billed, unbilled], bounds)
    assert result == [unbilled]


def test_group_by_student_sorts_each_students_lessons_by_date() -> None:
    later = _lesson(1, date(2026, 2, 10), AttendanceStatus.ATTENDED)
    earlier = _lesson(1, date(2026, 2, 3), AttendanceStatus.ATTENDED)
    other_student = _lesson(2, date(2026, 2, 5), AttendanceStatus.ATTENDED)
    grouped = group_by_student([later, earlier, other_student])
    assert [lesson.lesson_date for lesson in grouped[1]] == [date(2026, 2, 3), date(2026, 2, 10)]
    assert [lesson.lesson_date for lesson in grouped[2]] == [date(2026, 2, 5)]


def _student(rate_cents: int) -> Student:
    return Student(
        id=1,
        name="Test Student",
        parent_id=1,
        instrument="Piano",
        rate_cents=rate_cents,
        school="Test School",
    )


def test_compute_totals_subtotal_is_rate_times_lesson_count() -> None:
    lessons = [_lesson(1, date(2026, 2, 2), AttendanceStatus.ATTENDED) for _ in range(3)]
    totals = compute_totals(_student(4000), lessons)
    assert totals.subtotal_cents == 12000


def test_compute_totals_gst_is_zero_and_total_equals_subtotal() -> None:
    lessons = [_lesson(1, date(2026, 2, 2), AttendanceStatus.ATTENDED)]
    totals = compute_totals(_student(4500), lessons)
    assert totals.gst_cents == 0
    assert totals.total_cents == totals.subtotal_cents


def test_compute_totals_uses_integer_cents_with_no_float_drift() -> None:
    lessons = [_lesson(1, date(2026, 2, 2), AttendanceStatus.ATTENDED) for _ in range(7)]
    totals = compute_totals(_student(3333), lessons)
    assert totals.subtotal_cents == 23331
    assert isinstance(totals.subtotal_cents, int)
