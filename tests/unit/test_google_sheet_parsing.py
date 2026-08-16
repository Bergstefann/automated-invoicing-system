"""Unit tests for the pure parsing helpers in providers/google.py.

These are plain functions with no Google API calls in them — `_parse_flat_schedule`
never touches a network, so it's safe to test directly, unlike the rest of
that module (which is deliberately never exercised by this suite). This is
the highest-risk new code ahead of the first live run against a real Sheet,
so it gets its own coverage rather than relying on the "google.py stays
untested" default.
"""

from __future__ import annotations

from datetime import date

from invoicing.providers.google import _col_letter, _parse_date_cell, _parse_flat_schedule


def test_parse_date_cell_accepts_d_slash_m() -> None:
    assert _parse_date_cell("2/3", 2026) == date(2026, 3, 2)


def test_parse_date_cell_accepts_padded_and_spaced_variants() -> None:
    assert _parse_date_cell("02/03", 2026) == date(2026, 3, 2)
    assert _parse_date_cell(" 2 / 3 ", 2026) == date(2026, 3, 2)


def test_parse_date_cell_rejects_non_date_text() -> None:
    assert _parse_date_cell("Aria", 2026) is None
    assert _parse_date_cell("", 2026) is None
    assert _parse_date_cell("YI", 2026) is None


def test_parse_date_cell_rejects_an_impossible_date() -> None:
    assert _parse_date_cell("31/2", 2026) is None


def test_col_letter_matches_spreadsheet_column_naming() -> None:
    assert _col_letter(0) == "A"
    assert _col_letter(1) == "B"
    assert _col_letter(25) == "Z"
    assert _col_letter(26) == "AA"
    assert _col_letter(27) == "AB"


def test_parse_flat_schedule_on_empty_grid_returns_nothing() -> None:
    lessons, index = _parse_flat_schedule([], year=2026)
    assert lessons == []
    assert index == {}


def _grid() -> list[list[str]]:
    return [
        ["Student", "2/3", "9/3"],
        ["Aria", "Y", ""],
        ["Cora", "", "YI"],
    ]


def test_parse_flat_schedule_reads_header_dates_from_row_zero_skipping_col_zero() -> None:
    lessons, _index = _parse_flat_schedule(_grid(), year=2026)
    dates = {lesson.lesson_date for lesson in lessons}
    assert dates == {date(2026, 3, 2), date(2026, 3, 9)}


def test_parse_flat_schedule_only_emits_lessons_for_non_blank_status_cells() -> None:
    lessons, _index = _parse_flat_schedule(_grid(), year=2026)
    assert len(lessons) == 2
    by_student = {lesson.student_first_name: lesson for lesson in lessons}
    assert by_student["Aria"].status == "Y"
    assert by_student["Aria"].lesson_date == date(2026, 3, 2)
    assert by_student["Cora"].status == "YI"
    assert by_student["Cora"].lesson_date == date(2026, 3, 9)


def test_parse_flat_schedule_skips_rows_with_a_blank_first_column() -> None:
    grid = [*_grid(), ["", "Y", "Y"]]
    lessons, _index = _parse_flat_schedule(grid, year=2026)
    assert len(lessons) == 2  # the blank-name row contributes nothing


def test_parse_flat_schedule_builds_a_cell_index_for_every_student_date_pair() -> None:
    # The index must cover blank cells too — mark_lessons_billed needs to
    # resolve a cell for a lesson that *was* "Y" and is about to become "YI";
    # the write-back call itself only ever asks about cells that had a
    # status, but the index isn't allowed to silently omit any grid cell.
    _lessons, index = _parse_flat_schedule(_grid(), year=2026)
    assert index[("aria", date(2026, 3, 2))] == (1, 1)
    assert index[("aria", date(2026, 3, 9))] == (1, 2)
    assert index[("cora", date(2026, 3, 2))] == (2, 1)
    assert index[("cora", date(2026, 3, 9))] == (2, 2)


def test_parse_flat_schedule_index_keys_are_case_insensitive_lowercase() -> None:
    _lessons, index = _parse_flat_schedule(_grid(), year=2026)
    assert ("Aria", date(2026, 3, 2)) not in index
    assert ("aria", date(2026, 3, 2)) in index


def test_parse_flat_schedule_ignores_a_non_date_header_column() -> None:
    grid = [
        ["Student", "notes", "2/3"],
        ["Aria", "left-handed", "Y"],
    ]
    lessons, index = _parse_flat_schedule(grid, year=2026)
    assert len(lessons) == 1
    assert lessons[0].lesson_date == date(2026, 3, 2)
    assert ("aria", date(2026, 3, 2)) in index
