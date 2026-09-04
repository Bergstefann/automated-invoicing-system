"""Typer CLI entry point.

Safety posture:
- `sync` and `run` require an explicit --demo or --real — there's no
  default that could send you to live Google APIs because you forgot a flag.
- `run` defaults to --dry-run (zero writes anywhere). Turning that off with
  --no-dry-run still won't send a single email unless --confirm is also
  passed — there is no single flag that both bills and emails by accident.
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
from invoicing.providers.google import (
    OAUTH_SCOPES,
    GoogleDocProvider,
    GoogleEmailProvider,
    GoogleSheetProvider,
)
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


def _require_explicit_mode(demo: bool, real: bool) -> None:
    """Neither flag defaulting to "real" would make it too easy to hit live
    Google APIs by typing the command you always type and forgetting a flag.
    Both commands that can reach Google (`sync`, `run`) require picking one."""
    if demo and real:
        typer.echo("Pass --demo or --real, not both.")
        raise typer.Exit(code=1)
    if not demo and not real:
        typer.echo("Pass --demo (fake providers, synthetic data) or --real (live Google APIs).")
        raise typer.Exit(code=1)


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
    # All three share one cached token file (see providers/google.py's module
    # docstring), so they must all request the same scope set up front —
    # otherwise whichever provider is used second silently gets a token
    # that's insufficiently scoped for it.
    scopes = OAUTH_SCOPES
    return (
        GoogleSheetProvider(settings, scopes=scopes),
        GoogleDocProvider(settings, scopes=scopes),
        GoogleEmailProvider(settings, scopes=scopes),
    )


def _fmt_cents(cents: int) -> str:
    return f"${cents / 100:.2f}"


@app.command()
def sync(
    demo: bool = typer.Option(False, "--demo", help="Seed the synthetic dataset instead."),
    real: bool = typer.Option(False, "--real", help="Pull from the real, live Google Sheet."),
) -> None:
    """Pull the Sheet into SQLite."""
    _require_explicit_mode(demo, real)
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

    typer.echo(f"Period {period}: {bounds.start_date} -> {bounds.end_date}")
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
    real: bool = typer.Option(
        False, "--real", help="Use the real Google Sheet/Docs/Gmail — required instead of --demo."
    ),
    message: str = typer.Option(
        DEFAULT_MESSAGE, "--message", help="Personal message. Supports <student> and <parent>."
    ),
) -> None:
    """Bill unbilled lessons for a period, then email the resulting invoices."""
    _require_explicit_mode(demo, real)
    settings = _load_settings(demo)
    now = datetime.now(UTC)

    with Database(settings.db_path) as db:
        if demo:
            seed_database(db)
        sheet_provider, doc_provider, email_provider = _build_providers(settings, demo)

        if not demo and not dry_run:
            # Sync right before billing (not during dry-run, which must make
            # zero writes) so the schedule is fresh and — for the real
            # GoogleSheetProvider — its row/column index is warm before
            # mark_lessons_billed needs it.
            synced = sync_schedule_into_db(db, sheet_provider)
            logger.info("synced %d lesson(s) from the Sheet before billing", synced)

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


@app.command(name="debug-parse-schedule")
def debug_parse_schedule(
    demo: bool = typer.Option(False, "--demo", help="Use the synthetic dataset."),
    real: bool = typer.Option(False, "--real", help="Parse the real, live Google Sheet."),
) -> None:
    """Read-only: run only read_schedule() and print what it parsed.

    Calls nothing else — no DB writes, no billing, no Sheet write-back, no
    email. Deliberately needs only Sheet + OAuth config (not Drive/Doc
    settings, unlike every other real-mode command) so the parser can be
    checked against a real sheet's actual layout before the rest of the
    setup — Drive folder, Doc templates — even has to exist.
    """
    _require_explicit_mode(demo, real)
    settings = _load_settings(demo)

    sheet_provider: SheetProvider
    if demo:
        sheet_provider = FakeSheetProvider(snapshot=ScheduleSnapshot(students=[], lessons=[]))
    else:
        settings.require_sheet_config()
        sheet_provider = GoogleSheetProvider(settings)

    snapshot = sheet_provider.read_schedule()

    typer.echo(f"Student Config: {len(snapshot.students)} student(s)")
    typer.echo(f"{'First':<14}{'Last':<14}{'Billing':<10}{'Rate':>8}  Parent (name / email)")
    typer.echo("-" * 78)
    for s in snapshot.students:
        typer.echo(
            f"{s.first_name:<14}{s.last_name:<14}{s.billing_type:<10}"
            f"{_fmt_cents(s.rate_cents):>8}  {s.parent_name} <{s.parent_email}>"
        )

    typer.echo(f"\nLesson Schedule: {len(snapshot.lessons)} parsed cell(s)")
    typer.echo(f"{'Date':<12}{'Student':<20}Status")
    typer.echo("-" * 42)
    for lesson in sorted(
        snapshot.lessons, key=lambda lesson: (lesson.lesson_date, lesson.student_first_name)
    ):
        typer.echo(
            f"{lesson.lesson_date.isoformat():<12}{lesson.student_first_name:<20}{lesson.status}"
        )

    if not snapshot.lessons:
        typer.echo("  (nothing parsed — check the parser against the actual sheet layout)")


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
