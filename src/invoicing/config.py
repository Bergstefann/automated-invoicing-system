"""Environment-based settings, validated at load time.

Real (non-demo) runs need Google configuration; --demo mode needs none of
it, so validation of the Google fields is deferred to `require_google_config`
rather than enforced unconditionally at construction.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from pydantic import BaseModel, field_validator


class Settings(BaseModel):
    db_path: Path = Path("invoicing.db")
    term_start: str = "2026-02-02"

    sheet_id: str | None = None
    sheet_service_account_file: Path | None = None
    schedule_tab: str = "Term 1 2026"

    oauth_client_secret_file: Path | None = None
    oauth_token_file: Path | None = None

    drive_output_folder_id: str | None = None
    doc_template_short_id: str | None = None
    doc_template_long_id: str | None = None

    sender_name: str = "Jane Tutor"
    sender_email: str = "jane@example.com"

    demo: bool = False

    @field_validator("term_start")
    @classmethod
    def _validate_term_start(cls, v: str) -> str:
        date.fromisoformat(v)
        return v

    @property
    def term_start_date(self) -> date:
        return date.fromisoformat(self.term_start)

    @classmethod
    def from_env(cls) -> Settings:
        def env(name: str, default: str = "") -> str:
            return os.environ.get(f"INVOICING_{name}", default)

        def opt_env(name: str) -> str | None:
            return os.environ.get(f"INVOICING_{name}") or None

        def opt_path(name: str) -> Path | None:
            value = opt_env(name)
            return Path(value) if value else None

        return cls(
            db_path=Path(env("DB_PATH", "invoicing.db")),
            term_start=env("TERM_START", "2026-02-02"),
            sheet_id=opt_env("SHEET_ID"),
            sheet_service_account_file=opt_path("SHEET_SERVICE_ACCOUNT_FILE"),
            schedule_tab=env("SCHEDULE_TAB", "Term 1 2026"),
            oauth_client_secret_file=opt_path("OAUTH_CLIENT_SECRET_FILE"),
            oauth_token_file=opt_path("OAUTH_TOKEN_FILE"),
            drive_output_folder_id=opt_env("DRIVE_OUTPUT_FOLDER_ID"),
            doc_template_short_id=opt_env("DOC_TEMPLATE_SHORT_ID"),
            doc_template_long_id=opt_env("DOC_TEMPLATE_LONG_ID"),
            sender_name=env("SENDER_NAME", "Jane Tutor"),
            sender_email=env("SENDER_EMAIL", "jane@example.com"),
        )

    @classmethod
    def for_demo(cls) -> Settings:
        return cls(db_path=Path("demo.db"), demo=True)

    def require_google_config(self) -> None:
        """Raise if live Google configuration is incomplete. Never called in --demo mode."""
        required: dict[str, str | Path | None] = {
            "INVOICING_SHEET_ID": self.sheet_id,
            "INVOICING_SHEET_SERVICE_ACCOUNT_FILE": self.sheet_service_account_file,
            "INVOICING_OAUTH_CLIENT_SECRET_FILE": self.oauth_client_secret_file,
            "INVOICING_DRIVE_OUTPUT_FOLDER_ID": self.drive_output_folder_id,
            "INVOICING_DOC_TEMPLATE_SHORT_ID": self.doc_template_short_id,
            "INVOICING_DOC_TEMPLATE_LONG_ID": self.doc_template_long_id,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError("Missing required configuration for live mode: " + ", ".join(missing))
