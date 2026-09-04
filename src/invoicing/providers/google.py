"""Live Google Sheets / Docs / Drive / Gmail provider implementations.

This module is never imported by the test suite and never exercised by
`--demo` mode — it is only reached when the CLI runs without `--demo` and
`Settings.require_google_config()` has already confirmed real credentials
are configured.

Auth model: everything — Sheets, Docs, Drive, Gmail — goes through a single
shared OAuth installed-app flow (`credentials.json` + a locally cached
token). The original pipeline used a *separate* service account for Sheets;
this rebuild's real test environment was provisioned with one OAuth desktop
client covering all four APIs, so Sheets moved onto the same flow rather
than requiring a second credential file nobody asked for.

Sheet layout: the real test Sheet's Lesson Schedule tab turned out to match
the original's nested weekly-block layout (see Step 0 report), not a flat
grid — dates wrap downward in blocks: a header row holds one date per
name-column position, and the rows below it hold [name, status] pairs until
the next header row appears, repeating every ~9 rows in the test data. The
parser below mirrors the original's mechanism (block detection via
"how many cells in this row parse as a date") but doesn't hardcode the
day-column count the way the original's `DAY_COLUMN_PAIRS` did, since that
count wasn't verified against the live sheet.
"""

from __future__ import annotations

import base64
import io
import logging
import pickle
import re
from datetime import date
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from invoicing.config import Settings
from invoicing.models import SheetStatus
from invoicing.providers.base import (
    EmailAttachment,
    InvoiceDocData,
    ScheduleSnapshot,
    SheetLessonRecord,
    SheetLessonRef,
    SheetStudentRecord,
)
from invoicing.templates.invoice_doc import build_replacements

logger = logging.getLogger("invoicing.providers.google")

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DOCS_DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]
EMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# Least-privilege split: each provider requests only the scopes it uses (see
# docs/POSTMORTEM-double-billing.md sibling finding — the scheduled-preview CI
# credential used to mint a token with full Gmail + Drive access despite only
# reading Sheets and sending mail). Kept here as the union for reference; no
# code path requests this directly.
OAUTH_SCOPES = SHEETS_SCOPES + DOCS_DRIVE_SCOPES + EMAIL_SCOPES

STUDENT_CONFIG_RANGE = "Student Config!A:E"
_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})$")


def _parse_date_cell(cell_text: str, year: int) -> date | None:
    text = str(cell_text).strip().replace(" ", "").replace("\xa0", "")
    match = _DATE_RE.match(text)
    if not match:
        return None
    day, month = int(match.group(1)), int(match.group(2))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _is_header_row(row: list[str], year: int, min_dates: int = 2) -> bool:
    """A header row is one where at least `min_dates` name-column positions
    (even indices: 0, 2, 4, ...) hold a parseable date — mirrors the
    original's block-detection heuristic exactly."""
    name_position_cells = row[0::2]
    return (
        sum(1 for cell in name_position_cells if _parse_date_cell(cell, year) is not None)
        >= min_dates
    )


def _col_letter(zero_based_index: int) -> str:
    result = ""
    idx = zero_based_index + 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def _load_cached_token(token_file: Path, scopes: list[str]) -> Credentials:
    """Loads the cached token, migrating a legacy pickle in place if that's
    what's found. Token files used to be written with `pickle.dump` despite
    the documented `.json` extension; `from_authorized_user_file` expects
    real JSON and raises `ValueError` (via `json.JSONDecodeError` or a
    `UnicodeDecodeError` decoding pickle's binary bytes as text — both
    `ValueError` subclasses) on the old format, which is the migration
    trigger below.
    """
    try:
        return Credentials.from_authorized_user_file(str(token_file), scopes)
    except ValueError:
        with open(token_file, "rb") as f:
            creds: Credentials = pickle.load(f)
        token_file.write_text(creds.to_json())
        logger.info("migrated OAuth token cache from pickle to JSON at %s", token_file)
        return creds


def _oauth_credentials(settings: Settings, scopes: list[str]) -> Credentials:
    """Requests credentials scoped to exactly what the calling provider
    needs (see `SHEETS_SCOPES` / `DOCS_DRIVE_SCOPES` / `EMAIL_SCOPES`), not
    the full union — a token minted for one narrowly-scoped run (e.g. the
    scheduled-preview CI job, which only touches Sheets + Gmail send) never
    carries Drive/Docs access it doesn't need.
    """
    assert settings.oauth_client_secret_file is not None
    assert settings.oauth_token_file is not None

    creds: Credentials | None = None
    if settings.oauth_token_file.exists():
        creds = _load_cached_token(settings.oauth_token_file, scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(settings.oauth_client_secret_file), scopes
            )
            creds = flow.run_local_server(port=0)
        settings.oauth_token_file.write_text(creds.to_json())
    return creds


class GoogleSheetProvider:
    """Wraps the Sheets API via the shared OAuth flow.

    Lesson Schedule tab is the original's nested weekly-block layout: dates
    wrap downward, a header row holds one date per name-column position, and
    the rows below hold [name, status] column pairs until the next header
    row appears. `read_schedule` builds `self._cell_index`, a
    (first_name.lower(), date) -> (row, status_col) map (both 0-based).

    That index is keyed by name because that's all the Sheet itself has —
    but `mark_lessons_billed` is handed `SheetLessonRef`s keyed by the
    database's stable `student_id`, not a name (see
    `docs/POSTMORTEM-double-billing.md`'s "remaining risk": the previous
    version of this class re-derived a first name by splitting the
    database's *display* name, `ref.student_key.split()[0]`, which breaks
    silently the moment two students share a first name or a display name
    stops looking like "First Last"). `self._identity_map`, set by
    `set_identity_map`, is the (student_id -> first_name.lower()) mapping
    `sync_schedule_into_db` already builds from the exact same Sheet read
    this class produced — so `mark_lessons_billed` never reconstructs
    anything, it only looks up a value that was captured once, verbatim, at
    sync time.

    Both `self._cell_index` and `self._identity_map` are populated by calls
    `sync_schedule_into_db` makes (`read_schedule` then `set_identity_map`)
    before billing, so `read_schedule` must have been called at least once
    in this provider's lifetime — and `set_identity_map` after it — before
    `mark_lessons_billed` can do anything. `pipeline.sync_schedule_into_db`
    calling both before billing (see cli.py's `run` command) guarantees
    that ordering.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._service: Any | None = None
        self._cell_index: dict[tuple[str, date], tuple[int, int]] = {}
        self._identity_map: dict[str, str] = {}

    def _sheets(self) -> Any:
        if self._service is None:
            creds = _oauth_credentials(self._settings, SHEETS_SCOPES)
            self._service = build("sheets", "v4", credentials=creds).spreadsheets()
        return self._service

    def read_schedule(self) -> ScheduleSnapshot:
        settings = self._settings
        assert settings.sheet_id is not None
        service = self._sheets()

        config_result = (
            service.values()
            .get(spreadsheetId=settings.sheet_id, range=STUDENT_CONFIG_RANGE)
            .execute()
        )
        config_rows: list[list[str]] = config_result.get("values", [])

        students: list[SheetStudentRecord] = []
        for row in config_rows[1:]:
            if not row or not row[0].strip():
                continue
            # Columns: A first name, B parent email, C rate, D parent name,
            # E student last name — this test sheet has no billing-type
            # column, so every listed student is treated as billable.
            padded = row + [""] * max(0, 5 - len(row))
            raw_rate = padded[2].strip() or "40"
            rate_dollars = float("".join(c for c in raw_rate if c.isdigit() or c == ".") or 40)
            students.append(
                SheetStudentRecord(
                    first_name=padded[0].strip(),
                    last_name=padded[4].strip(),
                    billing_type="Private",
                    parent_name=padded[3].strip(),
                    parent_email=padded[1].strip(),
                    rate_cents=round(rate_dollars * 100),
                )
            )

        grid_result = (
            service.values()
            .get(
                spreadsheetId=settings.sheet_id,
                range=f"'{settings.schedule_tab}'",
                valueRenderOption="FORMATTED_VALUE",
            )
            .execute()
        )
        grid: list[list[str]] = grid_result.get("values", [])

        lessons, self._cell_index = _parse_blocked_schedule(grid, year=date.today().year)
        return ScheduleSnapshot(students=students, lessons=lessons)

    def set_identity_map(self, identity_map: dict[str, str]) -> None:
        self._identity_map = identity_map

    def mark_lessons_billed(self, lesson_refs: list[SheetLessonRef], status: SheetStatus) -> None:
        data: list[dict[str, Any]] = []
        for ref in lesson_refs:
            cell = _resolve_lesson_cell(
                self._identity_map, self._cell_index, ref.student_id, ref.lesson_date
            )
            if cell is None:
                logger.warning(
                    "no Sheet cell found for student_id %s on %s — DB is billed, Sheet won't "
                    "reflect it until the next sync",
                    ref.student_id,
                    ref.lesson_date,
                )
                continue
            row, col = cell
            data.append(
                {
                    "range": f"'{self._settings.schedule_tab}'!{_col_letter(col)}{row + 1}",
                    "values": [[status.value]],
                }
            )

        if not data:
            return

        service = self._sheets()
        service.values().batchUpdate(
            spreadsheetId=self._settings.sheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()


def _resolve_lesson_cell(
    identity_map: dict[str, str],
    cell_index: dict[tuple[str, date], tuple[int, int]],
    student_id: str,
    lesson_date: date,
) -> tuple[int, int] | None:
    """Translates a stable `student_id` into a Sheet cell, via the exact
    (first_name.lower() -> student_id) mapping `sync_schedule_into_db` built
    from this same Sheet read — never by re-deriving a first name from a
    database display name (that reconstruction was the bug: see
    `docs/POSTMORTEM-double-billing.md`'s "remaining risk"). Returns `None`
    if either lookup misses, which `mark_lessons_billed` treats as "can't
    write back this run" rather than a guess."""
    first_name_key = identity_map.get(student_id)
    if first_name_key is None:
        return None
    return cell_index.get((first_name_key, lesson_date))


def _parse_blocked_schedule(
    grid: list[list[str]], year: int | None = None
) -> tuple[list[SheetLessonRecord], dict[tuple[str, date], tuple[int, int]]]:
    """Dates wrap downward in blocks. A header row (detected via
    `_is_header_row`) holds one date per name-column position (even
    indices); every row below it — until the next header row — holds
    [name, status] pairs at those same column positions. Mirrors the
    original's `parse_schedule`, generalized to not assume a fixed number
    of day-columns per header row.

    Returns the parsed lessons plus a (first_name.lower(), date) -> (row,
    status_col) index (both 0-based) for `mark_lessons_billed` to write
    back to. The index is built for every [name, status] pair encountered,
    even ones with a blank status, so a cell can always be resolved once a
    lesson there gets billed.

    Raises `ValueError` if the same (first name, date) would resolve to two
    different cells — i.e. two different students sharing a first name each
    have a lesson on the same date. This system has always joined the Sheet
    to the roster by first name alone (see `sync_schedule_into_db`), so that
    collision is a real, structural limit, not something the write-back
    index can silently paper over: guessing which of two cells to mark
    `YI` for a lesson that was actually billed under a specific student
    would misattribute the other student's cell, silently and permanently.
    """
    if not grid:
        return [], {}

    year = year if year is not None else date.today().year
    lessons: list[SheetLessonRecord] = []
    cell_index: dict[tuple[str, date], tuple[int, int]] = {}
    current_col_dates: dict[int, date] = {}

    for row_index, row in enumerate(grid):
        if _is_header_row(row, year):
            current_col_dates = {}
            for name_col in range(0, len(row), 2):
                parsed = _parse_date_cell(row[name_col], year)
                if parsed:
                    current_col_dates[name_col] = parsed
            continue

        if not current_col_dates:
            continue

        for name_col, lesson_date in current_col_dates.items():
            status_col = name_col + 1
            name_val = row[name_col].strip() if name_col < len(row) else ""
            if not name_val:
                continue
            status_val = row[status_col].strip().upper() if status_col < len(row) else ""
            key = (name_val.lower(), lesson_date)
            existing = cell_index.get(key)
            if existing is not None and existing != (row_index, status_col):
                existing_row, existing_col = existing
                raise ValueError(
                    f"ambiguous student reference: '{name_val}' has a lesson on "
                    f"{lesson_date.isoformat()} in two different cells "
                    f"({_col_letter(existing_col)}{existing_row + 1} and "
                    f"{_col_letter(status_col)}{row_index + 1}) — two students "
                    "sharing a first name can't be resolved from the Sheet alone."
                )
            cell_index[key] = (row_index, status_col)
            if not status_val:
                continue
            lessons.append(
                SheetLessonRecord(
                    student_first_name=name_val, lesson_date=lesson_date, status=status_val
                )
            )

    return lessons, cell_index


class GoogleDocProvider:
    """Wraps Docs + Drive. Auth is the shared OAuth installed-app flow."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._docs: Any | None = None
        self._drive: Any | None = None

    def _services(self) -> tuple[Any, Any]:
        if self._docs is None or self._drive is None:
            creds = _oauth_credentials(self._settings, DOCS_DRIVE_SCOPES)
            self._docs = build("docs", "v1", credentials=creds)
            self._drive = build("drive", "v3", credentials=creds)
        return self._docs, self._drive

    def create_invoice_doc(self, data: InvoiceDocData) -> str:
        settings = self._settings
        docs, drive = self._services()

        template_id = (
            settings.doc_template_short_id
            if len(data.lines) <= 4
            else settings.doc_template_long_id
        )
        assert template_id is not None

        copy = (
            drive.files()
            .copy(
                fileId=template_id,
                body={
                    "name": f"Invoice {data.invoice_number} - {data.student_display_name}",
                    "parents": [settings.drive_output_folder_id],
                },
                supportsAllDrives=True,
            )
            .execute()
        )
        doc_id: str = copy["id"]

        requests = [
            {
                "replaceAllText": {
                    "containsText": {"text": tag, "matchCase": True},
                    "replaceText": value,
                }
            }
            for tag, value in build_replacements(data).items()
        ]
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()
        return doc_id

    def export_pdf(self, doc_id: str) -> bytes:
        _docs, drive = self._services()
        request = drive.files().export_media(fileId=doc_id, mimeType="application/pdf")
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()


class GoogleEmailProvider:
    """Wraps Gmail. Auth is the same shared OAuth installed-app flow."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._gmail: Any | None = None

    def _service(self) -> Any:
        if self._gmail is None:
            creds = _oauth_credentials(self._settings, EMAIL_SCOPES)
            self._gmail = build("gmail", "v1", credentials=creds)
        return self._gmail

    def send(
        self,
        to: str,
        subject: str,
        html_body: str,
        attachment: EmailAttachment | None = None,
    ) -> str:
        gmail = self._service()
        msg = MIMEMultipart("mixed")
        msg["To"] = to
        msg["From"] = self._settings.sender_email
        msg["Subject"] = subject
        msg["Bcc"] = self._settings.sender_email
        msg.attach(MIMEText(html_body, "html"))

        if attachment is not None:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(attachment.content)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={attachment.filename}")
            msg.attach(part)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        result = gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
        message_id: str = result["id"]
        return message_id
