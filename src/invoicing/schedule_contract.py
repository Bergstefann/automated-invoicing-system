"""The lesson-schedule schema contract.

This module is the single source of truth for what a valid schedule *is* —
independent of where it came from. It knows nothing about Excel, the
Sheets API, or any other storage format; `invoicing.workbook` is the `.xlsx`
reader that produces the raw rows this module validates.

The contract has exactly two register-level rules, both aimed at the same
failure mode `docs/POSTMORTEM-double-billing.md` describes:

- `student_id` is the only identity a lesson row may reference. Name, rate,
  and contact details are attributes of a student, not identifiers of one —
  they can change freely across syncs without ever creating a second person.
- Every schedule row's `student_id` must already exist in the register.
  There is no fallback: an unresolvable id is a contract violation, not a
  skipped row.

See `docs/SCHEDULE-SCHEMA.md` for the full prose spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

VALID_STATUSES = frozenset({"Y", "YI", "N"})

REQUIRED_STUDENT_COLUMNS = ("student_id", "name", "rate_cents", "parent_name", "parent_email")
REQUIRED_SCHEDULE_COLUMNS = (
    "student_id",
    "lesson_date",
    "start_time",
    "duration_minutes",
    "status",
)


class ScheduleContractError(ValueError):
    """Raised for any contract violation. The message is always specific
    enough to act on without re-reading the source file — a row number and
    what was found there, not a generic "invalid schedule"."""


@dataclass(frozen=True)
class StudentRegisterEntry:
    """One row of the Students register — the identity register. Everything
    but `student_id` is an attribute: it can change across syncs without
    ever being treated as a different person."""

    student_id: str
    name: str
    rate_cents: int
    parent_name: str
    parent_email: str


@dataclass(frozen=True)
class ScheduleRow:
    """One row of the Schedule sheet: one lesson, already validated against
    the register — `student_id` is guaranteed resolvable by the time this
    is constructed."""

    student_id: str
    lesson_date: date
    start_time: time
    duration_minutes: int
    status: str


def validate_schema_version(version: object) -> int:
    """Raises unless `version` is an int in `SUPPORTED_SCHEMA_VERSIONS`.
    Returns it (typed as int) so callers don't need a second isinstance
    check — a workbook's version cell is untyped user-editable input, so
    "not an int at all" is exactly as real a violation as "an unsupported
    int"."""
    if not isinstance(version, int) or isinstance(version, bool):
        raise ScheduleContractError(
            f"schema_version must be an integer, found {version!r} "
            f"(supported versions: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ScheduleContractError(
            f"unsupported schema_version {version} "
            f"(supported versions: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )
    return version


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ScheduleContractError(message)


def validate_students_register(
    rows: list[dict[str, object]],
) -> dict[str, StudentRegisterEntry]:
    """Validates the Students sheet and returns the register, keyed by
    `student_id`. Raises on the first violation found, in row order:

    - a required column missing or blank on a row
    - `rate_cents` not a non-negative integer
    - a `student_id` repeated across two rows (the one identity rule the
      register itself must never break)
    """
    register: dict[str, StudentRegisterEntry] = {}
    for line_number, row in enumerate(rows, start=2):  # header is row 1
        for column in REQUIRED_STUDENT_COLUMNS:
            value = row.get(column)
            _require(
                value is not None and str(value).strip() != "",
                f"Students row {line_number}: missing required value for '{column}'",
            )

        student_id = str(row["student_id"]).strip()
        rate_cents = row["rate_cents"]
        if isinstance(rate_cents, bool) or not isinstance(rate_cents, int) or rate_cents < 0:
            raise ScheduleContractError(
                f"Students row {line_number}: rate_cents must be a non-negative integer, "
                f"found {rate_cents!r}"
            )
        _require(
            student_id not in register,
            f"Students row {line_number}: duplicate student_id '{student_id}' "
            f"(already registered at an earlier row)",
        )

        register[student_id] = StudentRegisterEntry(
            student_id=student_id,
            name=str(row["name"]).strip(),
            rate_cents=rate_cents,
            parent_name=str(row["parent_name"]).strip(),
            parent_email=str(row["parent_email"]).strip(),
        )
    return register


def validate_schedule_rows(
    rows: list[dict[str, object]],
    register: dict[str, StudentRegisterEntry],
) -> list[ScheduleRow]:
    """Validates the Schedule sheet against an already-validated register.
    Raises on the first violation, in row order:

    - a required column missing or blank on a row
    - `student_id` not present in the register (fails loudly, by design —
      see the module docstring; this is the check that replaces the old
      Sheets parser's silent `continue` on an unmatched name)
    - `lesson_date` not a real date, `start_time` not a real time,
      `duration_minutes` not a positive integer
    - `status` outside the known vocabulary {Y, YI, N}
    """
    schedule: list[ScheduleRow] = []
    for line_number, row in enumerate(rows, start=2):  # header is row 1
        for column in REQUIRED_SCHEDULE_COLUMNS:
            value = row.get(column)
            _require(
                value is not None and str(value).strip() != "",
                f"Schedule row {line_number}: missing required value for '{column}'",
            )

        student_id = str(row["student_id"]).strip()
        _require(
            student_id in register,
            f"Row {line_number}: student_id '{student_id}' not found in register",
        )

        lesson_date = row["lesson_date"]
        _require(
            isinstance(lesson_date, date),
            f"Schedule row {line_number}: lesson_date must be a date, found {lesson_date!r}",
        )

        start_time = row["start_time"]
        _require(
            isinstance(start_time, time),
            f"Schedule row {line_number}: start_time must be a time, found {start_time!r}",
        )

        duration_minutes = row["duration_minutes"]
        if (
            isinstance(duration_minutes, bool)
            or not isinstance(duration_minutes, int)
            or duration_minutes <= 0
        ):
            raise ScheduleContractError(
                f"Schedule row {line_number}: duration_minutes must be a positive integer, "
                f"found {duration_minutes!r}"
            )

        status = str(row["status"]).strip().upper()
        _require(
            status in VALID_STATUSES,
            f"Schedule row {line_number}: status '{row['status']}' is not one of "
            f"{sorted(VALID_STATUSES)}",
        )

        assert isinstance(lesson_date, date)
        assert isinstance(start_time, time)
        schedule.append(
            ScheduleRow(
                student_id=student_id,
                lesson_date=lesson_date,
                start_time=start_time,
                duration_minutes=duration_minutes,
                status=status,
            )
        )
    return schedule
