"""Live Google Sheets / Docs / Drive / Gmail provider implementations.

This module is never imported by the test suite and never exercised by
`--demo` mode — it is only reached when the CLI runs without `--demo` and
`Settings.require_google_config()` has already confirmed real credentials
are configured. OAuth scopes are preserved exactly from the original
pipeline (see Step 0 report): a spreadsheets-only service-account scope for
Sheets, and a broad Docs + Drive + full-Gmail scope for the OAuth flow.
"""

from __future__ import annotations

import base64
import io
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
from google.oauth2 import service_account
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

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
    "https://mail.google.com/",
]

DAY_COLUMN_PAIRS = [(0, 1), (2, 3), (4, 5), (6, 7), (8, 9)]
BILLABLE_TYPE = "Private"
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
    return sum(1 for cell in row if _parse_date_cell(cell, year) is not None) >= min_dates


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
    """Wraps the Sheets API. Auth is a service account, scoped to Sheets only."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._service: Any | None = None

    def _sheets(self) -> Any:
        if self._service is None:
            assert self._settings.sheet_service_account_file is not None
            creds = service_account.Credentials.from_service_account_file(
                str(self._settings.sheet_service_account_file), scopes=SHEETS_SCOPES
            )
            self._service = build("sheets", "v4", credentials=creds).spreadsheets()
        return self._service

    def read_schedule(self) -> ScheduleSnapshot:
        settings = self._settings
        assert settings.sheet_id is not None
        service = self._sheets()

        config_result = service.values().get(
            spreadsheetId=settings.sheet_id, range="Student Config!A:F"
        ).execute()
        config_rows: list[list[str]] = config_result.get("values", [])

        students: list[SheetStudentRecord] = []
        for row in config_rows[1:]:
            if not row or not row[0].strip():
                continue
            padded = row + [""] * max(0, 6 - len(row))
            raw_rate = padded[3].strip() or "40"
            rate_dollars = float("".join(c for c in raw_rate if c.isdigit() or c == ".") or 40)
            students.append(
                SheetStudentRecord(
                    first_name=padded[0].strip(),
                    last_name=padded[5].strip(),
                    billing_type=(padded[1].strip() or "Action"),
                    parent_name=padded[4].strip(),
                    parent_email=padded[2].strip(),
                    rate_cents=round(rate_dollars * 100),
                )
            )

        grid_result = service.values().get(
            spreadsheetId=settings.sheet_id,
            range=f"'{settings.schedule_tab}'",
            valueRenderOption="FORMATTED_VALUE",
        ).execute()
        grid: list[list[str]] = grid_result.get("values", [])

        billable_first_names = {
            s.first_name.lower() for s in students if s.billing_type == BILLABLE_TYPE
        }
        year = date.today().year
        lessons: list[SheetLessonRecord] = []
        current_col_dates: dict[int, date] = {}

        for row in grid:
            padded = row + [""] * max(0, 10 - len(row))
            if _is_header_row(padded, year):
                current_col_dates = {}
                for name_col, _status_col in DAY_COLUMN_PAIRS:
                    parsed = _parse_date_cell(padded[name_col], year)
                    if parsed:
                        current_col_dates[name_col] = parsed
                continue

            if not current_col_dates:
                continue

            for name_col, status_col in DAY_COLUMN_PAIRS:
                if name_col not in current_col_dates:
                    continue
                name_val = padded[name_col].strip()
                status_val = padded[status_col].strip().upper() if status_col < len(padded) else ""
                if not name_val or not status_val or name_val.lower() not in billable_first_names:
                    continue
                lessons.append(
                    SheetLessonRecord(
                        student_first_name=name_val,
                        lesson_date=current_col_dates[name_col],
                        status=status_val,
                    )
                )

        return ScheduleSnapshot(students=students, lessons=lessons)

    def mark_lessons_billed(self, lesson_refs: list[SheetLessonRef], status: SheetStatus) -> None:
        # A real write-back needs each ref's sheet row/column, which this
        # provider resolves from its own index built during read_schedule
        # (an internal detail, deliberately not part of the Protocol).
        raise NotImplementedError(
            "wire up row/column resolution against a live sheet before enabling live runs"
        )


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

        copy = drive.files().copy(
            fileId=template_id,
            body={
                "name": f"Invoice {data.invoice_number} - {data.student_display_name}",
                "parents": [settings.drive_output_folder_id],
            },
            supportsAllDrives=True,
        ).execute()
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
