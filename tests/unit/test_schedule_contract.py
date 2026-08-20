"""Unit tests for the pure contract-validation rules in
`invoicing.schedule_contract`. These operate on plain dicts (as if already
read out of a workbook row) — no openpyxl involved, so they're fast and
exercise the validation logic in isolation from how a workbook is read.
See `tests/unit/test_workbook.py` for the loader that turns actual `.xlsx`
files into these same dicts.
"""

from __future__ import annotations

from datetime import date, time

import pytest

from invoicing.schedule_contract import (
    SUPPORTED_SCHEMA_VERSIONS,
    ScheduleContractError,
    validate_schedule_rows,
    validate_schema_version,
    validate_students_register,
)

# ── schema version ──────────────────────────────────────────────────────


def test_validate_schema_version_accepts_a_supported_version() -> None:
    assert validate_schema_version(1) == 1


def test_validate_schema_version_rejects_an_unsupported_version() -> None:
    with pytest.raises(ScheduleContractError, match="unsupported schema_version 2") as exc_info:
        validate_schema_version(2)
    assert str(sorted(SUPPORTED_SCHEMA_VERSIONS)) in str(exc_info.value)


def test_validate_schema_version_rejects_a_non_integer() -> None:
    with pytest.raises(ScheduleContractError, match="must be an integer"):
        validate_schema_version("1")


def test_validate_schema_version_rejects_a_bool() -> None:
    # bool is a subclass of int in Python; True/False must not silently
    # pass as schema version 1/0.
    with pytest.raises(ScheduleContractError, match="must be an integer"):
        validate_schema_version(True)


# ── students register ───────────────────────────────────────────────────


def _student_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "student_id": "S-0001",
        "name": "Aria Smith",
        "rate_cents": 4000,
        "parent_name": "Smith Parent",
        "parent_email": "smith@example.com",
    }
    row.update(overrides)
    return row


def test_validate_students_register_accepts_valid_rows() -> None:
    register = validate_students_register([_student_row()])
    assert register["S-0001"].name == "Aria Smith"
    assert register["S-0001"].rate_cents == 4000


def test_validate_students_register_rejects_a_missing_required_value() -> None:
    with pytest.raises(ScheduleContractError, match="Students row 2: missing required value"):
        validate_students_register([_student_row(parent_email="")])


def test_validate_students_register_rejects_a_non_integer_rate() -> None:
    with pytest.raises(ScheduleContractError, match="rate_cents must be a non-negative integer"):
        validate_students_register([_student_row(rate_cents="forty dollars")])


def test_validate_students_register_rejects_a_negative_rate() -> None:
    with pytest.raises(ScheduleContractError, match="rate_cents must be a non-negative integer"):
        validate_students_register([_student_row(rate_cents=-100)])


def test_validate_students_register_rejects_a_duplicate_student_id() -> None:
    rows = [_student_row(), _student_row(name="Dax Jones")]
    with pytest.raises(
        ScheduleContractError, match="Students row 3: duplicate student_id 'S-0001'"
    ):
        validate_students_register(rows)


def test_validate_students_register_error_names_the_exact_row() -> None:
    rows = [_student_row(), _student_row(student_id="S-0002", parent_name="")]
    with pytest.raises(ScheduleContractError, match="Students row 3"):
        validate_students_register(rows)


# ── schedule rows ───────────────────────────────────────────────────────


def _schedule_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "student_id": "S-0001",
        "lesson_date": date(2026, 3, 2),
        "start_time": time(16, 0),
        "duration_minutes": 30,
        "status": "Y",
    }
    row.update(overrides)
    return row


@pytest.fixture
def register() -> dict[str, object]:
    return validate_students_register([_student_row()])  # type: ignore[return-value]


def test_validate_schedule_rows_accepts_a_valid_row(register: dict[str, object]) -> None:
    parsed = validate_schedule_rows([_schedule_row()], register)  # type: ignore[arg-type]
    assert len(parsed) == 1
    assert parsed[0].student_id == "S-0001"
    assert parsed[0].status == "Y"


def test_validate_schedule_rows_rejects_a_missing_required_value(
    register: dict[str, object],
) -> None:
    with pytest.raises(ScheduleContractError, match="Schedule row 2: missing required value"):
        validate_schedule_rows([_schedule_row(duration_minutes=None)], register)  # type: ignore[arg-type]


def test_validate_schedule_rows_rejects_an_unresolvable_student_id(
    register: dict[str, object],
) -> None:
    with pytest.raises(
        ScheduleContractError, match=r"Row 2: student_id 'S-0031' not found in register"
    ):
        validate_schedule_rows([_schedule_row(student_id="S-0031")], register)  # type: ignore[arg-type]


def test_validate_schedule_rows_rejects_a_bad_date(register: dict[str, object]) -> None:
    with pytest.raises(ScheduleContractError, match="lesson_date must be a date"):
        validate_schedule_rows([_schedule_row(lesson_date="2/3/2026")], register)  # type: ignore[arg-type]


def test_validate_schedule_rows_rejects_a_bad_time(register: dict[str, object]) -> None:
    with pytest.raises(ScheduleContractError, match="start_time must be a time"):
        validate_schedule_rows([_schedule_row(start_time="4pm")], register)  # type: ignore[arg-type]


def test_validate_schedule_rows_rejects_a_non_positive_duration(
    register: dict[str, object],
) -> None:
    with pytest.raises(ScheduleContractError, match="duration_minutes must be a positive integer"):
        validate_schedule_rows([_schedule_row(duration_minutes=0)], register)  # type: ignore[arg-type]


def test_validate_schedule_rows_rejects_an_unknown_status(register: dict[str, object]) -> None:
    with pytest.raises(ScheduleContractError, match="status 'Maybe' is not one of"):
        validate_schedule_rows([_schedule_row(status="Maybe")], register)  # type: ignore[arg-type]


def test_validate_schedule_rows_status_is_case_insensitive(register: dict[str, object]) -> None:
    parsed = validate_schedule_rows([_schedule_row(status="yi")], register)  # type: ignore[arg-type]
    assert parsed[0].status == "YI"


def test_validate_schedule_rows_an_attribute_change_still_resolves_to_one_student(
    register: dict[str, object],
) -> None:
    """Directly echoes the incident: the register attribute (here,
    parent_email) can change between two reads, but the schedule still
    joins to exactly one student_id, not a second forked one — because
    identity was never derived from the attribute in the first place."""
    changed_register = validate_students_register(
        [_student_row(parent_email="smith.new@example.com")]
    )
    parsed = validate_schedule_rows([_schedule_row()], changed_register)  # type: ignore[arg-type]
    assert len(parsed) == 1
    assert parsed[0].student_id == "S-0001"
