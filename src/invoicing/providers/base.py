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
    """One row of the original 'Student Config' tab.

    `student_id` is only ever populated by a source that actually has a
    register id to give — the schema-contract workbook (see
    `invoicing.workbook`). The live Google Sheet's Student Config tab has no
    such column, so `GoogleSheetProvider` always leaves this `None`; that
    source's identity is resolved downstream, in the database (see
    `docs/SCHEDULE-SCHEMA.md`).
    """

    student_id: str | None = None
    first_name: str
    last_name: str
    billing_type: str
    parent_name: str
    parent_email: str
    rate_cents: int


class SheetLessonRecord(BaseModel):
    """One lesson. `student_id` is populated by contract-validated sources
    (the workbook); sources with no register of their own, like the live
    Sheet's blocked grid, leave it `None` and are joined to a roster by
    `student_first_name` instead — see `SheetStudentRecord`."""

    student_id: str | None = None
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
    the pipeline should have to carry around. Keyed on the database's stable
    `student_id` — never on a display name — for exactly the reason
    `docs/POSTMORTEM-double-billing.md` names as its one remaining open
    risk: a name is mutable and reconstructable-wrong, an issued-once
    register id isn't.
    """

    student_id: str
    lesson_date: date


class SheetProvider(Protocol):
    def read_schedule(self) -> ScheduleSnapshot: ...

    def set_identity_map(self, identity_map: dict[str, str]) -> None:
        """Tells the provider how each stable `student_id` maps back to
        whatever identity its own source natively uses (for the Sheet: a
        lowercased first name), so `mark_lessons_billed` can resolve a
        write-back target without ever re-deriving that mapping from a
        display name. Called once, after `read_schedule`, by
        `sync_schedule_into_db` — the one place that already does the
        canonical join from source identity to `student_id`.
        """
        ...

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
