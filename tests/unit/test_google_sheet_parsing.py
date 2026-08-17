"""Unit tests for the pure parsing helpers in providers/google.py.

These are plain functions with no Google API calls in them — `_parse_blocked_schedule`
never touches a network, so it's safe to test directly, unlike the rest of
that module (which is deliberately never exercised by this suite). This is
the highest-risk new code ahead of the first live run against a real Sheet,
so it gets its own coverage rather than relying on the "google.py stays
untested" default.

The real test Sheet's Lesson Schedule tab wraps dates downward in blocks —
a header row holds one date per name-column position, and the rows below
hold [name, status] pairs until the next header row appears (confirmed
against the live layout, unlike the flat-grid assumption this replaced).
"""

from __future__ import annotations

from datetime import date

import pytest

from invoicing.providers.google import (
    _col_letter,
    _is_header_row,
    _parse_blocked_schedule,
    _parse_date_cell,
)


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


def test_is_header_row_true_when_two_name_positions_parse_as_dates() -> None:
    assert _is_header_row(["2/3", "", "9/3", ""], 2026) is True


def test_is_header_row_false_for_a_normal_student_row() -> None:
    assert _is_header_row(["Aria", "Y", "Cora", ""], 2026) is False


def test_is_header_row_false_with_only_one_date() -> None:
    assert _is_header_row(["2/3", "", "Cora", "Y"], 2026) is False


def test_parse_blocked_schedule_on_empty_grid_returns_nothing() -> None:
    lessons, index = _parse_blocked_schedule([], year=2026)
    assert lessons == []
    assert index == {}


def _one_block_grid() -> list[list[str]]:
    return [
        ["2/3", "", "9/3", ""],  # header: two day-columns, dates at cols 0 and 2
        ["Aria", "Y", "Cora", ""],
        ["Dax", "", "Elowen", "YI"],
    ]


def test_parse_blocked_schedule_reads_dates_from_name_column_positions() -> None:
    lessons, _index = _parse_blocked_schedule(_one_block_grid(), year=2026)
    dates = {lesson.lesson_date for lesson in lessons}
    assert dates == {date(2026, 3, 2), date(2026, 3, 9)}


def test_parse_blocked_schedule_only_emits_lessons_for_non_blank_status_cells() -> None:
    lessons, _index = _parse_blocked_schedule(_one_block_grid(), year=2026)
    assert len(lessons) == 2  # Cora's and Dax's blank-status cells contribute nothing
    by_student = {lesson.student_first_name: lesson for lesson in lessons}
    assert by_student["Aria"].status == "Y"
    assert by_student["Aria"].lesson_date == date(2026, 3, 2)
    assert by_student["Elowen"].status == "YI"
    assert by_student["Elowen"].lesson_date == date(2026, 3, 9)


def test_parse_blocked_schedule_ignores_rows_before_the_first_header() -> None:
    grid = [["stray", "leftover", "row", ""], *_one_block_grid()]
    lessons, _index = _parse_blocked_schedule(grid, year=2026)
    assert len(lessons) == 2  # the pre-header row is never treated as data


def test_parse_blocked_schedule_handles_a_new_header_block_further_down() -> None:
    # Mirrors the real sheet: a new set of date headers appears again after
    # several student rows (every ~9 rows in the live data; the exact
    # cadence doesn't matter to the algorithm, which just watches for the
    # next header-shaped row).
    grid = [
        *_one_block_grid(),
        ["16/3", "", "23/3", ""],  # second header block, new dates
        ["Aria", "Y", "", ""],
    ]
    lessons, index = _parse_blocked_schedule(grid, year=2026)

    aria_dates = {lesson.lesson_date for lesson in lessons if lesson.student_first_name == "Aria"}
    assert aria_dates == {date(2026, 3, 2), date(2026, 3, 16)}
    assert ("aria", date(2026, 3, 16)) in index


def test_parse_blocked_schedule_skips_blank_name_cells() -> None:
    grid = [*_one_block_grid(), ["", "Y", "", "Y"]]
    lessons, _index = _parse_blocked_schedule(grid, year=2026)
    assert len(lessons) == 2  # the blank-name row contributes nothing


def test_parse_blocked_schedule_builds_index_for_every_name_status_pair() -> None:
    # The index must cover blank-status cells too — mark_lessons_billed
    # needs to resolve a cell for a lesson that *was* "Y" and is about to
    # become "YI".
    _lessons, index = _parse_blocked_schedule(_one_block_grid(), year=2026)
    assert index[("aria", date(2026, 3, 2))] == (1, 1)
    assert index[("cora", date(2026, 3, 9))] == (1, 3)
    assert index[("dax", date(2026, 3, 2))] == (2, 1)
    assert index[("elowen", date(2026, 3, 9))] == (2, 3)


def test_parse_blocked_schedule_index_keys_are_case_insensitive_lowercase() -> None:
    _lessons, index = _parse_blocked_schedule(_one_block_grid(), year=2026)
    assert ("Aria", date(2026, 3, 2)) not in index
    assert ("aria", date(2026, 3, 2)) in index


def test_parse_blocked_schedule_raises_on_two_same_named_students_same_date() -> None:
    # Two different day-columns can share a header date (e.g. two lesson
    # slots on the same day). If both have a student named "Aria" that
    # date, the (first_name, date) index can't tell them apart — this must
    # fail loudly rather than silently pick one cell for mark_lessons_billed
    # to write back to on behalf of both.
    grid = [
        ["2/3", "", "2/3", ""],
        ["Aria", "Y", "Aria", "YI"],
    ]
    with pytest.raises(ValueError, match="ambiguous student reference"):
        _parse_blocked_schedule(grid, year=2026)
