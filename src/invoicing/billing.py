"""Pure billing logic: periods, unbilled detection, totals.

No I/O here — everything operates on in-memory domain objects, so it is
trivially testable without a database or network connection.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from invoicing.models import Lesson, Student

PERIOD_LENGTH_DAYS = 14


@dataclass(frozen=True)
class PeriodBounds:
    period_number: int
    start_date: date
    end_date: date


def period_bounds(term_start: date, period_number: int) -> PeriodBounds:
    """Periods are 14 days, inclusive of both ends, back-to-back with no gaps."""
    if period_number < 1:
        raise ValueError("period_number must be >= 1")
    start = term_start + timedelta(days=(period_number - 1) * PERIOD_LENGTH_DAYS)
    end = start + timedelta(days=PERIOD_LENGTH_DAYS - 1)
    return PeriodBounds(period_number=period_number, start_date=start, end_date=end)


def period_number_for_date(term_start: date, lesson_date: date) -> int:
    """A date on a period boundary lands in exactly one period (the one it opens)."""
    if lesson_date < term_start:
        raise ValueError("date is before the term start")
    return (lesson_date - term_start).days // PERIOD_LENGTH_DAYS + 1


def is_period_completed(period_end: date, today: date) -> bool:
    """A period only counts once it has fully elapsed, not merely started."""
    return period_end < today


def unbilled_lessons_in_period(lessons: list[Lesson], bounds: PeriodBounds) -> list[Lesson]:
    return [
        lesson
        for lesson in lessons
        if bounds.start_date <= lesson.lesson_date <= bounds.end_date and lesson.is_unbilled
    ]


def group_by_student(lessons: list[Lesson]) -> dict[int, list[Lesson]]:
    grouped: dict[int, list[Lesson]] = defaultdict(list)
    for lesson in lessons:
        grouped[lesson.student_id].append(lesson)
    for lesson_list in grouped.values():
        lesson_list.sort(key=lambda lesson: lesson.lesson_date)
    return dict(grouped)


@dataclass(frozen=True)
class InvoiceTotals:
    subtotal_cents: int
    gst_cents: int
    total_cents: int


def compute_totals(student: Student, lessons: list[Lesson]) -> InvoiceTotals:
    """GST is hardcoded to zero.

    This matches the original pipeline exactly — it never computed GST either,
    the Doc template's `$<gst>` placeholder was always filled with "$0.00".
    Carried forward here as a disclosed simplification, not a solved feature.
    """
    subtotal_cents = student.rate_cents * len(lessons)
    gst_cents = 0
    return InvoiceTotals(
        subtotal_cents=subtotal_cents,
        gst_cents=gst_cents,
        total_cents=subtotal_cents + gst_cents,
    )
