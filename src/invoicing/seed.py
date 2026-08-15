"""Synthetic dataset generator, used by `--demo` mode and `data/seed_synthetic.py`.

Everything here is fabricated: fictional names, @example.com addresses,
placeholder rates. Nothing in this module is derived from, or resembles,
data belonging to the real business this pipeline was rebuilt from.

Generates ~25 students, ~15 parents, and lessons across 6 fortnightly
periods starting 2026-02-02. Periods 1-2 are seeded fully billed and
emailed, so a fresh clone can immediately run `invoicing run --period 3
--demo` and see real, unbilled work get processed.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from invoicing.billing import (
    PeriodBounds,
    compute_totals,
    group_by_student,
    period_bounds,
    unbilled_lessons_in_period,
)
from invoicing.db import Database
from invoicing.invoice_numbers import next_invoice_number
from invoicing.models import AttendanceStatus, Invoice, InvoiceLine, Lesson, Parent, Student

TERM_START = datetime(2026, 2, 2, tzinfo=UTC).date()
FULLY_PROCESSED_PERIODS = 2
TOTAL_PERIODS = 6
SEED = 20260202

FIRST_NAMES = [
    "Aria",
    "Beckett",
    "Cora",
    "Dax",
    "Elowen",
    "Finn",
    "Greta",
    "Hugo",
    "Ivy",
    "Jasper",
    "Kiri",
    "Lachlan",
    "Maeve",
    "Nico",
    "Opal",
    "Percy",
    "Quinn",
    "Rosalind",
    "Silas",
    "Tansy",
    "Ursula",
    "Vale",
    "Wren",
    "Xander",
    "Yara",
]
LAST_NAMES = [
    "Ashworth",
    "Brambleton",
    "Caldwell",
    "Delacroix",
    "Ellery",
    "Faircloth",
    "Greaves",
    "Hawthorne",
    "Iversen",
    "Jonquil",
    "Kestrel",
    "Larkspur",
    "Merriweather",
    "Norwood",
    "Osgood",
    "Pellinor",
]
PARENT_FIRST_NAMES = ["Alex", "Jordan", "Sam", "Morgan", "Casey", "Riley", "Taylor", "Jamie"]
INSTRUMENTS = ["Piano", "Guitar", "Violin", "Voice", "Drums", "Cello"]
SCHOOLS = ["Riverside College", "Northgate High", "St. Aldwyn's", "Fernbrook Grammar"]
RATE_OPTIONS_CENTS = [3500, 4000, 4500, 5000]
DAY_OFFSETS = [0, 2, 4, 7, 9, 11]


def seed_database(db: Database) -> None:
    if not db.is_empty():
        return

    rng = random.Random(SEED)

    parents: list[Parent] = []
    for i in range(15):
        last = LAST_NAMES[i % len(LAST_NAMES)]
        first = rng.choice(PARENT_FIRST_NAMES)
        parents.append(
            db.insert_parent(Parent(name=f"{first} {last}", email=f"parent{i + 1}@example.com"))
        )

    students: list[Student] = []
    for i, first in enumerate(FIRST_NAMES):
        parent = rng.choice(parents)
        assert parent.id is not None
        students.append(
            db.insert_student(
                Student(
                    name=f"{first} {LAST_NAMES[i % len(LAST_NAMES)]}",
                    parent_id=parent.id,
                    instrument=rng.choice(INSTRUMENTS),
                    rate_cents=rng.choice(RATE_OPTIONS_CENTS),
                    school=rng.choice(SCHOOLS),
                )
            )
        )

    for period_number in range(1, TOTAL_PERIODS + 1):
        bounds = period_bounds(TERM_START, period_number)
        period = db.get_or_create_period(period_number, bounds.start_date, bounds.end_date)
        assert period.id is not None

        for student in students:
            if rng.random() > 0.85:
                continue
            assert student.id is not None
            lesson_date = bounds.start_date + timedelta(days=rng.choice(DAY_OFFSETS))
            roll = rng.random()
            if roll < 0.85:
                status = AttendanceStatus.ATTENDED
            elif roll < 0.93:
                status = AttendanceStatus.ABSENT_NOTIFIED
            else:
                status = AttendanceStatus.ABSENT_UNNOTIFIED
            db.insert_lesson(
                Lesson(
                    student_id=student.id,
                    lesson_date=lesson_date,
                    duration_minutes=30,
                    attendance_status=status,
                )
            )

        if period_number <= FULLY_PROCESSED_PERIODS:
            _bill_and_email_period(db, bounds, period.id)


def _bill_and_email_period(db: Database, bounds: PeriodBounds, period_id: int) -> None:
    """Directly synthesizes already-processed invoices for seed periods,
    bypassing the real Doc/Email providers — this is fixture data, not a
    pipeline run, so it has no business depending on those interfaces."""
    lessons = db.lessons_in_range(bounds.start_date, bounds.end_date)
    grouped = group_by_student(unbilled_lessons_in_period(lessons, bounds))

    issued_on = bounds.end_date + timedelta(days=1)
    issued_at = datetime.combine(issued_on, datetime.min.time(), tzinfo=UTC)

    for student_id, student_lessons in sorted(grouped.items()):
        student = db.get_student(student_id)
        totals = compute_totals(student, student_lessons)
        invoice_number = next_invoice_number(issued_on, db.count_invoices_issued_on(issued_on))
        invoice = db.insert_invoice(
            Invoice(
                invoice_number=invoice_number,
                student_id=student_id,
                parent_id=student.parent_id,
                period_id=period_id,
                issued_at=issued_at,
                subtotal_cents=totals.subtotal_cents,
                gst_cents=totals.gst_cents,
                total_cents=totals.total_cents,
                doc_url=f"seed-doc-{invoice_number}",
                emailed_at=issued_at,
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
        db.mark_lessons_billed(
            [lesson.id for lesson in student_lessons if lesson.id is not None], invoice.id
        )
