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


def _write_pre_student_id_students_table(db_path: Path) -> None:
    """Builds a `students` table shaped like it was before the register id
    column existed, with rows already in it, to exercise the backfill."""
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE parents (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            name  TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE
        );
        CREATE TABLE students (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            parent_id  INTEGER NOT NULL REFERENCES parents(id),
            instrument TEXT NOT NULL,
            rate_cents INTEGER NOT NULL,
            school     TEXT NOT NULL,
            active     INTEGER NOT NULL DEFAULT 1,
            UNIQUE (name, parent_id)
        );
        CREATE TABLE lessons (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id         INTEGER NOT NULL,
            lesson_date        TEXT NOT NULL,
            duration_minutes   INTEGER NOT NULL,
            attendance_status  TEXT NOT NULL,
            billed_invoice_id  INTEGER,
            pre_billed         INTEGER NOT NULL DEFAULT 0,
            UNIQUE (student_id, lesson_date)
        );
        """
    )
    raw.execute("INSERT INTO parents (id, name, email) VALUES (1, 'Jordan', 'jordan@example.com')")
    raw.execute(
        "INSERT INTO students (id, name, parent_id, instrument, rate_cents, school) "
        "VALUES (5, 'Riley Example', 1, 'Piano', 4000, 'Example School')"
    )
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()


def test_opening_a_pre_student_id_database_backfills_it_from_the_row_id(tmp_path: Path) -> None:
    db_path = tmp_path / "no-student-id.db"
    _write_pre_student_id_students_table(db_path)

    db = Database(db_path)
    try:
        version = db.conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION
        student = db.get_student(5)
        assert student.student_id == "S-0005"
    finally:
        db.close()


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
