"""Protocol interfaces the pipeline depends on.

The pipeline (`pipeline.py`) only ever imports these Protocols and the DTOs
below it — never the concrete Google classes in `google.py`. That is what
makes the whole pipeline testable, and demoable, with zero network access:
every test and every `--demo` run wires up `fake.py` instead.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from pydantic import BaseModel

from invoicing.models import SheetStatus

# ── Sheets ───────────────────────────────────────────────────────────────


class SheetStudentRecord(BaseModel):
    """One row of the original 'Student Config' tab."""

    first_name: str
    last_name: str
    billing_type: str
    parent_name: str
    parent_email: str
    rate_cents: int


class SheetLessonRecord(BaseModel):
    """One [Student Name][Status] cell pair from the schedule grid."""

    student_first_name: str
    lesson_date: date
    status: str


class ScheduleSnapshot(BaseModel):
    students: list[SheetStudentRecord]
    lessons: list[SheetLessonRecord]


class SheetLessonRef(BaseModel):
    """Identifies a lesson cell to write a status back to.

    Deliberately identity-based (student + date) rather than row/column
    indices: row/column bookkeeping is an internal detail of whichever
    SheetProvider implementation actually talks to the grid, not something
    the pipeline should have to carry around.
    """

    student_key: str
    lesson_date: date


class SheetProvider(Protocol):
    def read_schedule(self) -> ScheduleSnapshot: ...

    def mark_lessons_billed(
        self, lesson_refs: list[SheetLessonRef], status: SheetStatus
    ) -> None: ...


# ── Docs ─────────────────────────────────────────────────────────────────


class InvoiceDocLine(BaseModel):
    day_of_week: str
    date_str: str
    student_display_name: str
    rate_cents: int


class InvoiceDocData(BaseModel):
    parent_name: str
    parent_email: str
    invoice_number: str
    invoice_date: date
    student_display_name: str
    subtotal_cents: int
    gst_cents: int
    total_cents: int
    lines: list[InvoiceDocLine]


class DocProvider(Protocol):
    def create_invoice_doc(self, data: InvoiceDocData) -> str:
        """Returns the created doc's id."""
        ...

    def export_pdf(self, doc_id: str) -> bytes: ...


# ── Email ────────────────────────────────────────────────────────────────


class EmailAttachment(BaseModel):
    filename: str
    content: bytes
    mime_type: str = "application/pdf"


class EmailProvider(Protocol):
    def send(
        self,
        to: str,
        subject: str,
        html_body: str,
        attachment: EmailAttachment | None = None,
    ) -> str:
        """Returns a provider message id."""
        ...
