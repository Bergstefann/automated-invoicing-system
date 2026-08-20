"""Reads the schema-contract `.xlsx` workbook and validates it against
`invoicing.schedule_contract`.

This is the "any source validates against the same contract" half of the
design: `GoogleSheetProvider` translates the live Sheet's native wrapped
grid into database identity internally (see its class docstring), while a
workbook built to this contract — like `templates/lesson_schedule.xlsx` —
is read and validated *directly*, with no translation step, because it's
already shaped as one row per lesson with an explicit `student_id`.

Sheet layout (see `docs/SCHEDULE-SCHEMA.md` for the full spec):

- "Legend" — cell `B2` holds the integer schema version.
- "Students" — the identity register. Header row 1, one student per row
  after it, columns named per `schedule_contract.REQUIRED_STUDENT_COLUMNS`
  (column order doesn't matter — headers are read by name).
- "Schedule" — one row per lesson, columns named per
  `schedule_contract.REQUIRED_SCHEDULE_COLUMNS`.

A sheet is read until the first fully-blank row, so trailing notes or
formatting below the data don't get parsed as rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from invoicing.providers.base import ScheduleSnapshot, SheetLessonRecord, SheetStudentRecord
from invoicing.schedule_contract import (
    REQUIRED_SCHEDULE_COLUMNS,
    REQUIRED_STUDENT_COLUMNS,
    ScheduleContractError,
    ScheduleRow,
    StudentRegisterEntry,
    validate_schedule_rows,
    validate_schema_version,
    validate_students_register,
)

LEGEND_SHEET = "Legend"
STUDENTS_SHEET = "Students"
SCHEDULE_SHEET = "Schedule"
SCHEMA_VERSION_CELL = "B2"


@dataclass(frozen=True)
class WorkbookSchedule:
    schema_version: int
    register: dict[str, StudentRegisterEntry]
    lessons: list[ScheduleRow]


def _require_sheet(workbook: Any, name: str) -> Worksheet:
    try:
        return workbook[name]
    except KeyError:
        raise ScheduleContractError(f"missing required sheet '{name}'") from None


def _header_columns(sheet: Worksheet, required: tuple[str, ...]) -> dict[str, int]:
    columns: dict[str, int] = {}
    for cell in sheet[1]:
        if cell.value is None:
            continue
        columns[str(cell.value).strip().lower()] = cell.column

    missing = [name for name in required if name not in columns]
    if missing:
        raise ScheduleContractError(
            f"'{sheet.title}' sheet is missing required column(s): {sorted(missing)}"
        )
    return columns


def _read_rows(sheet: Worksheet, required: tuple[str, ...]) -> list[dict[str, object]]:
    columns = _header_columns(sheet, required)
    rows: list[dict[str, object]] = []
    for row_cells in sheet.iter_rows(min_row=2):
        values = {name: row_cells[col_index - 1].value for name, col_index in columns.items()}
        if all(v is None or str(v).strip() == "" for v in values.values()):
            continue
        rows.append(values)
    return rows


def _split_datetime_fields(rows: list[dict[str, object]]) -> None:
    """openpyxl returns `datetime.datetime` for some date- or
    time-formatted cells depending on the exact number format applied;
    `schedule_contract` validates against plain `date`/`time`, so both get
    narrowed here, at the one place that knows which field is which. A
    `start_time` cell that comes back as a bare `date` (no time component
    at all — the wrong kind of cell) is left untouched, so the contract's
    `isinstance(value, time)` check reports it rather than this module
    silently coercing it to midnight."""
    for row in rows:
        lesson_date = row.get("lesson_date")
        if isinstance(lesson_date, datetime):
            row["lesson_date"] = lesson_date.date()
        start_time = row.get("start_time")
        if isinstance(start_time, datetime):
            row["start_time"] = start_time.time()


def load_schedule_workbook(source: str | Path | IO[bytes]) -> WorkbookSchedule:
    """Loads and fully validates a schedule workbook. Raises
    `ScheduleContractError` on the first violation found — an unsupported
    schema version, a missing sheet or column, or any row-level violation
    from `schedule_contract` (bad type, unresolvable student_id, duplicate
    student_id, ...). A `WorkbookSchedule` is only ever returned once every
    row in it is known-good.
    """
    workbook = load_workbook(source, data_only=True, read_only=True)

    legend = _require_sheet(workbook, LEGEND_SHEET)
    version = validate_schema_version(legend[SCHEMA_VERSION_CELL].value)

    students_sheet = _require_sheet(workbook, STUDENTS_SHEET)
    student_rows = _read_rows(students_sheet, REQUIRED_STUDENT_COLUMNS)
    for row in student_rows:
        if row.get("student_id") is not None:
            row["student_id"] = str(row["student_id"]).strip()
    register = validate_students_register(student_rows)

    schedule_sheet = _require_sheet(workbook, SCHEDULE_SHEET)
    schedule_rows = _read_rows(schedule_sheet, REQUIRED_SCHEDULE_COLUMNS)
    for row in schedule_rows:
        if row.get("student_id") is not None:
            row["student_id"] = str(row["student_id"]).strip()
    _split_datetime_fields(schedule_rows)
    lessons = validate_schedule_rows(schedule_rows, register)

    return WorkbookSchedule(schema_version=version, register=register, lessons=lessons)


def to_schedule_snapshot(workbook_schedule: WorkbookSchedule) -> ScheduleSnapshot:
    """Converts a validated workbook into the same `ScheduleSnapshot` shape
    every `SheetProvider` produces, so a workbook can stand in anywhere a
    live Sheet read would — e.g. `sync_schedule_into_db` doesn't need to
    know or care which source it came from.

    Unlike `GoogleSheetProvider`, every record here already carries its
    real `student_id` — there is no first-name join to do, because the
    workbook's identity was never anything but `student_id` in the first
    place.
    """
    students = [
        SheetStudentRecord(
            student_id=entry.student_id,
            first_name=entry.name.split()[0] if entry.name.split() else entry.name,
            last_name=" ".join(entry.name.split()[1:]),
            billing_type="Private",
            parent_name=entry.parent_name,
            parent_email=entry.parent_email,
            rate_cents=entry.rate_cents,
        )
        for entry in workbook_schedule.register.values()
    ]
    lessons = [
        SheetLessonRecord(
            student_id=row.student_id,
            student_first_name=workbook_schedule.register[row.student_id].name.split()[0],
            lesson_date=row.lesson_date,
            status=row.status,
        )
        for row in workbook_schedule.lessons
    ]
    return ScheduleSnapshot(students=students, lessons=lessons)
