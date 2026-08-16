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

Sheet layout: the real test Sheet used to build and verify this provider is
a flat grid — row 1 is a header (col A label, then one column per lesson
date), and each subsequent row is one student (col A = first name, then one
status cell per date column). That's simpler than the original's
weekly-block layout (see Step 0 report) and is treated here as the current
source of truth for what a "Lesson Schedule" tab looks like; the original's
nested date-block parser was not reused.
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
from typing import Any

from google.auth.transport.requests import Request
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

OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
    "https://mail.google.com/",
]

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


def _col_letter(zero_based_index: int) -> str:
    result = ""
    idx = zero_based_index + 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def _oauth_credentials(settings: Settings) -> Any:
    assert settings.oauth_client_secret_file is not None
    assert settings.oauth_token_file is not None

    creds = None
    if settings.oauth_token_file.exists():
        with open(settings.oauth_token_file, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(settings.oauth_client_secret_file), OAUTH_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(settings.oauth_token_file, "wb") as f:
            pickle.dump(creds, f)
    return creds


class GoogleSheetProvider:
    """Wraps the Sheets API via the shared OAuth flow.

    Lesson Schedule tab is a flat grid: row 1 = header (col A + one column
    per lesson date), row 2+ = one student per row (col A = first name,
    then one status cell per date column). `read_schedule` builds
    `self._cell_index`, a (first_name.lower(), date) -> (row, col) map
    (both 0-based) that `mark_lessons_billed` depends on to know which
    cell to write back to — so `read_schedule` must have been called at
    least once in this provider's lifetime before `mark_lessons_billed` can
    do anything. `pipeline.sync_schedule_into_db` calling `read_schedule`
    before billing (see cli.py's `run` command) guarantees that ordering.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._service: Any | None = None
        self._cell_index: dict[tuple[str, date], tuple[int, int]] = {}

    def _sheets(self) -> Any:
        if self._service is None:
            creds = _oauth_credentials(self._settings)
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

        lessons, self._cell_index = _parse_flat_schedule(grid, year=date.today().year)
        return ScheduleSnapshot(students=students, lessons=lessons)

    def mark_lessons_billed(self, lesson_refs: list[SheetLessonRef], status: SheetStatus) -> None:
        data: list[dict[str, Any]] = []
        for ref in lesson_refs:
            # student_key comes from the DB's full "First Last" display
            # name; the sheet only knows first names, so match on that.
            first_name_key = ref.student_key.split()[0].lower() if ref.student_key else ""
            cell = self._cell_index.get((first_name_key, ref.lesson_date))
            if cell is None:
                logger.warning(
                    "no Sheet cell found for %s on %s — DB is billed, Sheet won't reflect it "
                    "until the next sync",
                    ref.student_key,
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


def _parse_flat_schedule(
    grid: list[list[str]], year: int | None = None
) -> tuple[list[SheetLessonRecord], dict[tuple[str, date], tuple[int, int]]]:
    """Row 0 = header (col 0 + one date per remaining column). Row 1+ = one
    student per row (col 0 = first name, remaining cols = status per date).
    Returns the parsed lessons plus a (first_name.lower(), date) -> (row,
    col) index (both 0-based) for `mark_lessons_billed` to write back to.
    """
    if not grid:
        return [], {}

    year = year if year is not None else date.today().year
    header = grid[0]
    date_columns: dict[int, date] = {}
    for col_index, cell in enumerate(header):
        if col_index == 0:
            continue
        parsed = _parse_date_cell(cell, year)
        if parsed:
            date_columns[col_index] = parsed

    lessons: list[SheetLessonRecord] = []
    cell_index: dict[tuple[str, date], tuple[int, int]] = {}

    for row_index, row in enumerate(grid):
        if row_index == 0 or not row or not row[0].strip():
            continue
        first_name = row[0].strip()
        for col_index, lesson_date in date_columns.items():
            status_val = row[col_index].strip().upper() if col_index < len(row) else ""
            cell_index[(first_name.lower(), lesson_date)] = (row_index, col_index)
            if not status_val:
                continue
            lessons.append(
                SheetLessonRecord(
                    student_first_name=first_name, lesson_date=lesson_date, status=status_val
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
            creds = _oauth_credentials(self._settings)
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
            creds = _oauth_credentials(self._settings)
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
