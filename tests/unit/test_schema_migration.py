"""Tests for the schema-version migration path.

CREATE TABLE IF NOT EXISTS only builds a table that doesn't exist yet — it
never adds a column to one that's already there. This covers the case that
gap actually caused: an existing database, created before `pre_billed`
existed, opened again with current code.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from invoicing.db import SCHEMA_VERSION, Database
from invoicing.models import AttendanceStatus


def _write_pre_pre_billed_lessons_table(db_path: Path) -> None:
    """Builds just enough of the old schema (no `pre_billed` column, no
    `PRAGMA user_version` ever set) to exercise the migration path, without
    duplicating the full historical schema."""
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE lessons (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id         INTEGER NOT NULL,
            lesson_date        TEXT NOT NULL,
            duration_minutes   INTEGER NOT NULL,
            attendance_status  TEXT NOT NULL,
            billed_invoice_id  INTEGER,
            UNIQUE (student_id, lesson_date)
        );
        """
    )
    raw.execute(
        "INSERT INTO lessons "
        "(student_id, lesson_date, duration_minutes, attendance_status, billed_invoice_id) "
        "VALUES (1, '2026-02-02', 30, 'attended', NULL)"
    )
    raw.commit()
    raw.close()


def test_opening_an_old_schema_database_migrates_it_forward(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    _write_pre_pre_billed_lessons_table(db_path)

    db = Database(db_path)
    try:
        version = db.conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION

        lesson = db.get_lesson(1)
        assert lesson.attendance_status is AttendanceStatus.ATTENDED
        assert lesson.billed_invoice_id is None
        # The new column exists and pre-existing rows got its default.
        assert lesson.pre_billed is False
    finally:
        db.close()


def test_reopening_an_already_migrated_database_is_a_no_op(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.db"
    _write_pre_pre_billed_lessons_table(db_path)
    Database(db_path).close()  # first open: migrates

    db = Database(db_path)  # second open: nothing left to migrate
    try:
        version = db.conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION
        assert db.get_lesson(1).pre_billed is False
    finally:
        db.close()


def test_a_freshly_created_database_is_stamped_to_the_current_version(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    db = Database(db_path)
    try:
        version = db.conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION
    finally:
        db.close()
