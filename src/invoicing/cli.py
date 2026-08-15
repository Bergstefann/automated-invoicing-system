"""Typer CLI entry point.

Safety posture: `run` defaults to --dry-run (zero writes anywhere). Turning
that off with --no-dry-run still won't send a single email unless --confirm
is also passed — there is no single flag that both bills and emails by
accident.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import typer

from invoicing.config import Settings
from invoicing.db import Database
from invoicing.pipeline import preview_period, run_period, sync_schedule_into_db
from invoicing.providers.base import DocProvider, EmailProvider, ScheduleSnapshot, SheetProvider
from invoicing.providers.fake import FakeDocProvider, FakeEmailProvider, FakeSheetProvider
from invoicing.providers.google import GoogleDocProvider, GoogleEmailProvider, GoogleSheetProvider
from invoicing.seed import seed_database

DEFAULT_MESSAGE = (
    "Thanks for a great fortnight of lessons! Please find the attached invoice for <student>."
)

app = typer.Typer(add_completion=False, help="Fortnightly tutoring invoicing pipeline.")
logger = logging.getLogger("invoicing")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", help="Enable debug logging."),
) -> None:
    _configure_logging(verbose)


def _load_settings(demo: bool) -> Settings:
    return Settings.for_demo() if demo else Settings.from_env()


def _build_providers(
    settings: Settings, demo: bool
) -> tuple[SheetProvider, DocProvider, EmailProvider]:
    if demo:
        # --demo never syncs from a real Sheet — the synthetic dataset is
        # seeded straight into SQLite. The fake SheetProvider exists only so
        # status write-back after billing has somewhere harmless to land.
        sheet: SheetProvider = FakeSheetProvider(snapshot=ScheduleSnapshot(students=[], lessons=[]))
        return sheet, FakeDocProvider(), FakeEmailProvider()

    settings.require_google_config()
    return GoogleSheetProvider(settings), GoogleDocProvider(settings), GoogleEmailProvider(settings)


def _fmt_cents(cents: int) -> str:
    return f"${cents / 100:.2f}"


@app.command()
def sync(
    demo: bool = typer.Option(False, "--demo", help="Seed the synthetic dataset instead."),
) -> None:
    """Pull the Sheet into SQLite."""
    settings = _load_settings(demo)
    with Database(settings.db_path) as db:
        if demo:
            seed_database(db)
            typer.echo(f"Demo mode: seeded synthetic dataset into {settings.db_path}")
            return
        sheet_provider, _doc, _email = _build_providers(settings, demo)
        count = sync_schedule_into_db(db, sheet_provider)
        typer.echo(f"Synced {count} lesson(s) into {settings.db_path}")


@app.command()
def preview(
    period: int = typer.Option(..., "--period", min=1, help="Period number to preview."),
    demo: bool = typer.Option(False, "--demo", help="Use the synthetic dataset."),
) -> None:
    """Show what would be invoiced for a period. Read-only — makes no writes."""
    settings = _load_settings(demo)
    with Database(settings.db_path) as db:
        if demo:
            seed_database(db)
        bounds, lines = preview_period(db, settings.term_start_date, period)

    typer.echo(f"Period {period}: {bounds.start_date} → {bounds.end_date}")
    if not lines:
        typer.echo("  Nothing unbilled for this period.")
        return

    header = f"{'Student':<28}{'Lessons':>9}{'Subtotal':>12}{'Total':>12}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for line in lines:
        typer.echo(
            f"{line.student.name:<28}{line.lesson_count:>9}"
            f"{_fmt_cents(line.subtotal_cents):>12}{_fmt_cents(line.total_cents):>12}"
        )


@app.command()
def run(
    period: int = typer.Option(..., "--period", min=1, help="Period number to run."),
    dry_run: bool = typer.Option(
        True, "--dry-run/--no-dry-run", help="Dry-run by default: no writes anywhere."
    ),
    confirm: bool = typer.Option(
        False, "--confirm", help="Required (with --no-dry-run) to actually send email."
    ),
    demo: bool = typer.Option(
        False, "--demo", help="Use fake providers and the synthetic dataset."
    ),
    message: str = typer.Option(
        DEFAULT_MESSAGE, "--message", help="Personal message. Supports <student> and <parent>."
    ),
) -> None:
    """Bill unbilled lessons for a period, then email the resulting invoices."""
    settings = _load_settings(demo)
    now = datetime.now(UTC)

    with Database(settings.db_path) as db:
        if demo:
            seed_database(db)
        sheet_provider, doc_provider, email_provider = _build_providers(settings, demo)

        result = run_period(
            db,
            doc_provider,
            email_provider,
            sheet_provider,
            term_start=settings.term_start_date,
            period_number=period,
            sender_name=settings.sender_name,
            sender_email=settings.sender_email,
            personal_message=message,
            dry_run=dry_run,
            send_emails=confirm,
            now=now,
        )

    if dry_run:
        typer.echo(f"[dry-run] Period {period}: no writes made. Re-run with --no-dry-run to bill.")
        return

    typer.echo(f"Period {period}: billed {len(result.invoices_billed)} new invoice(s).")
    if not confirm:
        typer.echo("No emails sent (pass --confirm to send). Invoices remain queued for email.")
        return

    typer.echo(f"Emails sent: {len(result.emails.sent)}")
    if result.emails.failed:
        typer.echo(f"Emails failed (will retry next run): {', '.join(result.emails.failed)}")
    if result.emails.skipped_no_contact:
        typer.echo(
            f"Skipped, no parent email on file: {', '.join(result.emails.skipped_no_contact)}"
        )


@app.command()
def status(
    demo: bool = typer.Option(False, "--demo", help="Use the synthetic dataset."),
) -> None:
    """Summary of periods and their invoiced/uninvoiced counts."""
    settings = _load_settings(demo)
    with Database(settings.db_path) as db:
        if demo:
            seed_database(db)
        periods = db.list_periods()
        if not periods:
            typer.echo("No billing periods yet — run `invoicing sync` (or `run --demo`) first.")
            return

        header = f"{'Period':<8}{'Range':<24}{'Invoices':>10}{'Emailed':>10}{'Pending':>10}"
        typer.echo(header)
        typer.echo("-" * len(header))
        for p in periods:
            assert p.id is not None
            invoices = db.list_invoices(period_id=p.id)
            emailed = sum(1 for inv in invoices if inv.emailed_at is not None)
            pending = len(invoices) - emailed
            date_range = f"{p.start_date} - {p.end_date}"
            typer.echo(
                f"{p.period_number:<8}{date_range:<24}{len(invoices):>10}{emailed:>10}{pending:>10}"
            )


if __name__ == "__main__":
    app()
