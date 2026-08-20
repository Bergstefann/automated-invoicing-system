"""Domain models for the invoicing pipeline.

Money is always integer cents. Dates are ISO strings on the wire and
`datetime.date`/`datetime.datetime` (timezone-aware) in memory.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AttendanceStatus(StrEnum):
    """What happened at a scheduled lesson."""

    ATTENDED = "attended"
    ABSENT_NOTIFIED = "absent_notified"
    ABSENT_UNNOTIFIED = "absent_unnotified"


class SheetStatus(StrEnum):
    """Status codes written to the human-facing Sheets schedule.

    Preserved verbatim from the original spreadsheet-as-database design:
    Y = billable and not yet invoiced, YI = invoiced, N = not billable.
    """

    UNBILLED = "Y"
    INVOICED = "YI"
    NOT_BILLABLE = "N"


def attendance_to_sheet_status(status: AttendanceStatus, *, billed: bool) -> SheetStatus:
    if status is not AttendanceStatus.ATTENDED:
        return SheetStatus.NOT_BILLABLE
    return SheetStatus.INVOICED if billed else SheetStatus.UNBILLED


def _require_tz_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value


class Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class Parent(Model):
    id: int | None = None
    name: str
    email: str


class Student(Model):
    id: int | None = None
    # Sequential, zero-padded, prefixed register id ("S-0001"), assigned once
    # by Database.insert_student the first time a student appears and never
    # reassigned — see docs/SCHEDULE-SCHEMA.md. None only for a Student that
    # hasn't been persisted yet (e.g. built in-memory for a pure computation
    # like compute_totals); every row read back from the database has one.
    student_id: str | None = None
    name: str
    parent_id: int
    instrument: str
    rate_cents: int = Field(ge=0)
    school: str
    active: bool = True


class Lesson(Model):
    id: int | None = None
    student_id: int
    lesson_date: date
    duration_minutes: int = Field(gt=0)
    attendance_status: AttendanceStatus
    billed_invoice_id: int | None = None
    # True for lessons synced from the Sheet with status YI: already invoiced
    # by the pre-rebuild pipeline, before this system (or its Invoice table)
    # existed. No Invoice row backs them — there's no digital record left to
    # attach — so this is a second, disclosed way a lesson can be "billed",
    # alongside billed_invoice_id.
    pre_billed: bool = False

    @property
    def is_billable(self) -> bool:
        return self.attendance_status is AttendanceStatus.ATTENDED

    @property
    def is_unbilled(self) -> bool:
        return self.is_billable and self.billed_invoice_id is None and not self.pre_billed


class BillingPeriod(Model):
    id: int | None = None
    period_number: int = Field(ge=1)
    start_date: date
    end_date: date


class InvoiceLine(Model):
    id: int | None = None
    invoice_id: int
    lesson_id: int
    rate_cents: int = Field(ge=0)


class Invoice(Model):
    id: int | None = None
    invoice_number: str
    student_id: int
    parent_id: int
    period_id: int
    issued_at: datetime
    subtotal_cents: int = Field(ge=0)
    gst_cents: int = Field(ge=0)
    total_cents: int = Field(ge=0)
    doc_url: str | None = None
    emailed_at: datetime | None = None

    @field_validator("issued_at")
    @classmethod
    def _issued_at_tz_aware(cls, v: datetime) -> datetime:
        return _require_tz_aware(v)

    @field_validator("emailed_at")
    @classmethod
    def _emailed_at_tz_aware(cls, v: datetime | None) -> datetime | None:
        return _require_tz_aware(v) if v is not None else None
