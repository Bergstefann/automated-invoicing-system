"""SQLite persistence layer — the pipeline's source of truth.

The schema mirrors the original Sheet's concepts (students, lessons,
attendance, invoicing) but makes billing state a first-class, queryable
column (`lessons.billed_invoice_id`) instead of a cell value that has to be
parsed back out of a spreadsheet.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from invoicing.models import (
    AttendanceStatus,
    BillingPeriod,
    Invoice,
    InvoiceLine,
    Lesson,
    Parent,
    Student,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS parents (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS students (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT,
    name       TEXT NOT NULL,
    parent_id  INTEGER NOT NULL REFERENCES parents(id),
    instrument TEXT NOT NULL,
    rate_cents INTEGER NOT NULL,
    school     TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    UNIQUE (name, parent_id)
);

CREATE TABLE IF NOT EXISTS billing_periods (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    period_number INTEGER NOT NULL UNIQUE,
    start_date    TEXT NOT NULL,
    end_date      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_number  TEXT NOT NULL UNIQUE,
    student_id      INTEGER NOT NULL REFERENCES students(id),
    parent_id       INTEGER NOT NULL REFERENCES parents(id),
    period_id       INTEGER NOT NULL REFERENCES billing_periods(id),
    issued_at       TEXT NOT NULL,
    subtotal_cents  INTEGER NOT NULL,
    gst_cents       INTEGER NOT NULL,
    total_cents     INTEGER NOT NULL,
    doc_url         TEXT,
    emailed_at      TEXT
);

CREATE TABLE IF NOT EXISTS lessons (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id         INTEGER NOT NULL REFERENCES students(id),
    lesson_date        TEXT NOT NULL,
    duration_minutes   INTEGER NOT NULL,
    attendance_status  TEXT NOT NULL,
    billed_invoice_id  INTEGER REFERENCES invoices(id),
    pre_billed         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (student_id, lesson_date)
);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    lesson_id  INTEGER NOT NULL REFERENCES lessons(id),
    rate_cents INTEGER NOT NULL
);
"""

# CREATE TABLE IF NOT EXISTS only builds the *current* shape of a table that
# doesn't exist yet — it never adds a column to a table that's already
# there. That's exactly the gap that let an existing local .db silently
# miss the `pre_billed` column when it was added. SQLite's built-in
# `PRAGMA user_version` (an integer stored in the file itself, 0 by
# default) tracks how far a given database has been brought forward;
# MIGRATIONS lists each step in order, applied only to a database that
# already existed below that version. A freshly created database is
# stamped straight to SCHEMA_VERSION in `create_schema`, since its tables
# were just built from the current SCHEMA above and have nothing to
# migrate.
SCHEMA_VERSION = 2


def _add_pre_billed_column(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(lessons)")}
    if "pre_billed" not in columns:
        conn.execute("ALTER TABLE lessons ADD COLUMN pre_billed INTEGER NOT NULL DEFAULT 0")


def _add_student_id_column(conn: sqlite3.Connection) -> None:
    """Backfills the register id onto a database created before it existed.

    Assigned from each row's existing `id` (SQLite AUTOINCREMENT never
    reuses a rowid, so this is exactly as "issued once, never reused" as a
    separate counter would be) in `id` order, i.e. the order students were
    originally created — the same rule `insert_student` uses for new rows.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(students)")}
    if "student_id" not in columns:
        conn.execute("ALTER TABLE students ADD COLUMN student_id TEXT")
    rows = conn.execute("SELECT id FROM students WHERE student_id IS NULL ORDER BY id").fetchall()
    conn.executemany(
        "UPDATE students SET student_id = ? WHERE id = ?",
        [(f"S-{row[0]:04d}", row[0]) for row in rows],
    )


# Each step checks the actual table shape before acting, rather than
# trusting `user_version` alone: this column was added to SCHEMA before
# this versioning mechanism existed, so a database created in that window
# already has it but was never stamped — a blind `ALTER TABLE ADD COLUMN`
# would crash on "duplicate column name" for exactly that database.
MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _add_pre_billed_column),
    (2, _add_student_id_column),
]


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path, detect_types=0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.create_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def create_schema(self) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lessons'"
        ).fetchone()
        already_existed = row is not None

        self.conn.executescript(SCHEMA)
        self.conn.commit()

        if not already_existed:
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.conn.commit()
            return

        self._migrate()

    def _migrate(self) -> None:
        current_version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for version, migrate in MIGRATIONS:
            if version <= current_version:
                continue
            migrate(self.conn)
            self.conn.execute(f"PRAGMA user_version = {version}")
            self.conn.commit()

    def is_empty(self) -> bool:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM students").fetchone()
        return bool(row["n"] == 0)

    # ── parents ──────────────────────────────────────────────────────────
    def insert_parent(self, parent: Parent) -> Parent:
        cur = self.conn.execute(
            "INSERT INTO parents (name, email) VALUES (?, ?)",
            (parent.name, parent.email),
        )
        self.conn.commit()
        return parent.model_copy(update={"id": cur.lastrowid})

    def get_parent(self, parent_id: int) -> Parent:
        row = self.conn.execute("SELECT * FROM parents WHERE id = ?", (parent_id,)).fetchone()
        if row is None:
            raise KeyError(f"no parent with id {parent_id}")
        return _parent_from_row(row)

    def get_or_create_parent(self, name: str, email: str) -> Parent:
        row = self.conn.execute("SELECT * FROM parents WHERE email = ?", (email,)).fetchone()
        if row is not None:
            return _parent_from_row(row)
        return self.insert_parent(Parent(name=name, email=email))

    # ── students ─────────────────────────────────────────────────────────
    def insert_student(self, student: Student) -> Student:
        """Assigns the register id here, from the new row's own primary key
        — never from `student.student_id` on the way in, and never from
        anything about the student's name or contact details. This is the
        one place a student_id is ever minted."""
        cur = self.conn.execute(
            "INSERT INTO students (name, parent_id, instrument, rate_cents, school, active) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                student.name,
                student.parent_id,
                student.instrument,
                student.rate_cents,
                student.school,
                int(student.active),
            ),
        )
        new_id = cur.lastrowid
        assert new_id is not None
        student_id = f"S-{new_id:04d}"
        self.conn.execute("UPDATE students SET student_id = ? WHERE id = ?", (student_id, new_id))
        self.conn.commit()
        return student.model_copy(update={"id": new_id, "student_id": student_id})

    def get_student(self, student_id: int) -> Student:
        row = self.conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
        if row is None:
            raise KeyError(f"no student with id {student_id}")
        return _student_from_row(row)

    def get_or_create_student(
        self, name: str, parent_id: int, instrument: str, rate_cents: int, school: str
    ) -> Student:
        """Matches on `name` alone, not `(name, parent_id)`.

        get_or_create_parent keys on email, and a parent's email can
        legitimately change (a real incident: switching the Sheet to
        +alias@gmail.com addresses forked a second parent row per family,
        and this method — previously matching on (name, parent_id) — forked
        a second student to match, so every lesson got billed twice under
        the new copy). Name is the stable identity here — it's also the key
        `sync_schedule_into_db` already uses to join the Lesson Schedule
        grid to a student. If the resolved parent_id has changed, repoint
        the existing student at it instead of forking; the old parent row
        is simply left unreferenced, not deleted.
        """
        row = self.conn.execute("SELECT * FROM students WHERE name = ?", (name,)).fetchone()
        if row is not None:
            existing = _student_from_row(row)
            if existing.parent_id != parent_id:
                self.conn.execute(
                    "UPDATE students SET parent_id = ? WHERE id = ?", (parent_id, existing.id)
                )
                self.conn.commit()
                existing = existing.model_copy(update={"parent_id": parent_id})
            return existing
        return self.insert_student(
            Student(
                name=name,
                parent_id=parent_id,
                instrument=instrument,
                rate_cents=rate_cents,
                school=school,
            )
        )

    def list_students(self, *, active_only: bool = False) -> list[Student]:
        query = "SELECT * FROM students"
        if active_only:
            query += " WHERE active = 1"
        rows = self.conn.execute(query + " ORDER BY id").fetchall()
        return [_student_from_row(r) for r in rows]

    # ── lessons ──────────────────────────────────────────────────────────
    def insert_lesson(self, lesson: Lesson) -> Lesson:
        cur = self.conn.execute(
            "INSERT INTO lessons "
            "(student_id, lesson_date, duration_minutes, attendance_status, "
            " billed_invoice_id, pre_billed) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                lesson.student_id,
                lesson.lesson_date.isoformat(),
                lesson.duration_minutes,
                lesson.attendance_status.value,
                lesson.billed_invoice_id,
                int(lesson.pre_billed),
            ),
        )
        self.conn.commit()
        return lesson.model_copy(update={"id": cur.lastrowid})

    def get_or_create_lesson(
        self,
        student_id: int,
        lesson_date: date,
        duration_minutes: int,
        attendance_status: AttendanceStatus,
        pre_billed: bool = False,
    ) -> Lesson:
        row = self.conn.execute(
            "SELECT * FROM lessons WHERE student_id = ? AND lesson_date = ?",
            (student_id, lesson_date.isoformat()),
        ).fetchone()
        if row is not None:
            return _lesson_from_row(row)
        return self.insert_lesson(
            Lesson(
                student_id=student_id,
                lesson_date=lesson_date,
                duration_minutes=duration_minutes,
                attendance_status=attendance_status,
                pre_billed=pre_billed,
            )
        )

    def lessons_in_range(self, start: date, end: date) -> list[Lesson]:
        rows = self.conn.execute(
            "SELECT * FROM lessons WHERE lesson_date >= ? AND lesson_date <= ? "
            "ORDER BY lesson_date",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return [_lesson_from_row(r) for r in rows]

    def get_lesson(self, lesson_id: int) -> Lesson:
        row = self.conn.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
        if row is None:
            raise KeyError(f"no lesson with id {lesson_id}")
        return _lesson_from_row(row)

    def mark_lessons_billed(self, lesson_ids: list[int], invoice_id: int) -> None:
        self.conn.executemany(
            "UPDATE lessons SET billed_invoice_id = ? WHERE id = ?",
            [(invoice_id, lesson_id) for lesson_id in lesson_ids],
        )
        self.conn.commit()

    # ── billing periods ──────────────────────────────────────────────────
    def get_or_create_period(
        self, period_number: int, start_date: date, end_date: date
    ) -> BillingPeriod:
        row = self.conn.execute(
            "SELECT * FROM billing_periods WHERE period_number = ?", (period_number,)
        ).fetchone()
        if row is not None:
            return _period_from_row(row)
        cur = self.conn.execute(
            "INSERT INTO billing_periods (period_number, start_date, end_date) VALUES (?, ?, ?)",
            (period_number, start_date.isoformat(), end_date.isoformat()),
        )
        self.conn.commit()
        return BillingPeriod(
            id=cur.lastrowid,
            period_number=period_number,
            start_date=start_date,
            end_date=end_date,
        )

    def get_period(self, period_number: int) -> BillingPeriod | None:
        row = self.conn.execute(
            "SELECT * FROM billing_periods WHERE period_number = ?", (period_number,)
        ).fetchone()
        return _period_from_row(row) if row is not None else None

    def list_periods(self) -> list[BillingPeriod]:
        rows = self.conn.execute("SELECT * FROM billing_periods ORDER BY period_number").fetchall()
        return [_period_from_row(r) for r in rows]

    # ── invoices ─────────────────────────────────────────────────────────
    def insert_invoice(self, invoice: Invoice) -> Invoice:
        cur = self.conn.execute(
            "INSERT INTO invoices "
            "(invoice_number, student_id, parent_id, period_id, issued_at, "
            " subtotal_cents, gst_cents, total_cents, doc_url, emailed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                invoice.invoice_number,
                invoice.student_id,
                invoice.parent_id,
                invoice.period_id,
                invoice.issued_at.isoformat(),
                invoice.subtotal_cents,
                invoice.gst_cents,
                invoice.total_cents,
                invoice.doc_url,
                invoice.emailed_at.isoformat() if invoice.emailed_at else None,
            ),
        )
        self.conn.commit()
        return invoice.model_copy(update={"id": cur.lastrowid})

    def insert_invoice_lines(self, lines: list[InvoiceLine]) -> list[InvoiceLine]:
        result = []
        for line in lines:
            cur = self.conn.execute(
                "INSERT INTO invoice_lines (invoice_id, lesson_id, rate_cents) VALUES (?, ?, ?)",
                (line.invoice_id, line.lesson_id, line.rate_cents),
            )
            result.append(line.model_copy(update={"id": cur.lastrowid}))
        self.conn.commit()
        return result

    def mark_invoice_emailed(self, invoice_id: int, emailed_at: datetime) -> None:
        self.conn.execute(
            "UPDATE invoices SET emailed_at = ? WHERE id = ?",
            (emailed_at.isoformat(), invoice_id),
        )
        self.conn.commit()

    def invoices_pending_email(self, period_id: int) -> list[Invoice]:
        rows = self.conn.execute(
            "SELECT * FROM invoices WHERE period_id = ? AND emailed_at IS NULL ORDER BY id",
            (period_id,),
        ).fetchall()
        return [_invoice_from_row(r) for r in rows]

    def invoice_lines_for(self, invoice_id: int) -> list[InvoiceLine]:
        rows = self.conn.execute(
            "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY id", (invoice_id,)
        ).fetchall()
        return [
            InvoiceLine(
                id=r["id"],
                invoice_id=r["invoice_id"],
                lesson_id=r["lesson_id"],
                rate_cents=r["rate_cents"],
            )
            for r in rows
        ]

    def get_invoice(self, invoice_id: int) -> Invoice:
        row = self.conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if row is None:
            raise KeyError(f"no invoice with id {invoice_id}")
        return _invoice_from_row(row)

    def list_invoices(self, *, period_id: int | None = None) -> list[Invoice]:
        if period_id is None:
            rows = self.conn.execute("SELECT * FROM invoices ORDER BY id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM invoices WHERE period_id = ? ORDER BY id", (period_id,)
            ).fetchall()
        return [_invoice_from_row(r) for r in rows]

    def count_invoices_issued_on(self, day: date) -> int:
        prefix = day.strftime("%d%m%y")
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM invoices WHERE invoice_number LIKE ?", (f"{prefix}%",)
        ).fetchone()
        return int(row["n"])


def _parent_from_row(row: sqlite3.Row) -> Parent:
    return Parent(id=row["id"], name=row["name"], email=row["email"])


def _student_from_row(row: sqlite3.Row) -> Student:
    return Student(
        id=row["id"],
        student_id=row["student_id"],
        name=row["name"],
        parent_id=row["parent_id"],
        instrument=row["instrument"],
        rate_cents=row["rate_cents"],
        school=row["school"],
        active=bool(row["active"]),
    )


def _lesson_from_row(row: sqlite3.Row) -> Lesson:
    return Lesson(
        id=row["id"],
        student_id=row["student_id"],
        lesson_date=date.fromisoformat(row["lesson_date"]),
        duration_minutes=row["duration_minutes"],
        attendance_status=AttendanceStatus(row["attendance_status"]),
        billed_invoice_id=row["billed_invoice_id"],
        pre_billed=bool(row["pre_billed"]),
    )


def _period_from_row(row: sqlite3.Row) -> BillingPeriod:
    return BillingPeriod(
        id=row["id"],
        period_number=row["period_number"],
        start_date=date.fromisoformat(row["start_date"]),
        end_date=date.fromisoformat(row["end_date"]),
    )


def _invoice_from_row(row: sqlite3.Row) -> Invoice:
    return Invoice(
        id=row["id"],
        invoice_number=row["invoice_number"],
        student_id=row["student_id"],
        parent_id=row["parent_id"],
        period_id=row["period_id"],
        issued_at=datetime.fromisoformat(row["issued_at"]),
        subtotal_cents=row["subtotal_cents"],
        gst_cents=row["gst_cents"],
        total_cents=row["total_cents"],
        doc_url=row["doc_url"],
        emailed_at=datetime.fromisoformat(row["emailed_at"]) if row["emailed_at"] else None,
    )
