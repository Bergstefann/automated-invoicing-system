"""Integration tests for the CLI's safety posture: dry-run by default,
--confirm required to actually send, --demo needs no credentials and
touches nothing external. Drives the real Typer app via CliRunner rather
than calling pipeline functions directly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from invoicing.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings.for_demo() always points at ./demo.db — an isolated cwd per
    # test keeps that file from leaking state between tests (or into the repo).
    monkeypatch.chdir(tmp_path)


def _invoice_count_for_period(period_number: int) -> int:
    con = sqlite3.connect("demo.db")
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM invoices WHERE period_id = "
            "(SELECT id FROM billing_periods WHERE period_number = ?)",
            (period_number,),
        ).fetchone()
        return int(row[0])
    finally:
        con.close()


def test_status_on_a_freshly_seeded_demo_database_lists_periods() -> None:
    result = runner.invoke(app, ["status", "--demo"])
    assert result.exit_code == 0
    assert "Period" in result.stdout
    assert "Pending" in result.stdout


def test_run_defaults_to_dry_run_and_makes_no_database_changes() -> None:
    dry = runner.invoke(app, ["run", "--period", "3", "--demo"])
    assert dry.exit_code == 0
    assert "dry-run" in dry.stdout.lower()
    assert _invoice_count_for_period(3) == 0


def test_no_dry_run_without_confirm_refuses_to_send_but_still_bills() -> None:
    result = runner.invoke(app, ["run", "--period", "3", "--no-dry-run", "--demo"])
    assert result.exit_code == 0
    assert "billed 15 new invoice" in result.stdout
    assert "No emails sent" in result.stdout
    assert _invoice_count_for_period(3) == 15


def test_no_dry_run_with_confirm_bills_and_sends() -> None:
    result = runner.invoke(app, ["run", "--period", "3", "--no-dry-run", "--confirm", "--demo"])
    assert result.exit_code == 0
    assert "Emails sent: 15" in result.stdout


def test_rerunning_a_confirmed_run_bills_and_sends_nothing_new() -> None:
    first = runner.invoke(app, ["run", "--period", "3", "--no-dry-run", "--confirm", "--demo"])
    second = runner.invoke(app, ["run", "--period", "3", "--no-dry-run", "--confirm", "--demo"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "billed 0 new invoice" in second.stdout
    assert "Emails sent: 0" in second.stdout
    assert _invoice_count_for_period(3) == 15


def test_preview_is_read_only() -> None:
    result = runner.invoke(app, ["preview", "--period", "3", "--demo"])
    assert result.exit_code == 0
    assert "Student" in result.stdout
    assert "Lessons" in result.stdout
    assert _invoice_count_for_period(3) == 0


def test_run_without_demo_or_real_refuses_to_guess() -> None:
    result = runner.invoke(app, ["run", "--period", "3"])
    assert result.exit_code == 1
    assert "--demo" in result.stdout
    assert "--real" in result.stdout


def test_run_with_both_demo_and_real_refuses_the_ambiguity() -> None:
    result = runner.invoke(app, ["run", "--period", "3", "--demo", "--real"])
    assert result.exit_code == 1


def test_sync_without_demo_or_real_refuses_to_guess() -> None:
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1
    assert "--demo" in result.stdout
    assert "--real" in result.stdout
