"""Renders the preserved HTML email widget.

Markup, layout, and CSS are byte-for-byte the original (`invoice_email.html`).
The values that were real personal/financial data in the original — sender
name, sender email, BSB, account number, bank name — are now render
parameters, either sourced from Settings or a fixed, obviously-fake
placeholder. None of the original real data appears here or anywhere else
in this repository.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from invoicing.billing import InvoiceTotals
from invoicing.models import Lesson

_TEMPLATE_DIR = Path(__file__).parent
_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
)

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

PLACEHOLDER_BANK_NAME = "Example Bank"
PLACEHOLDER_BSB = "000-000"
PLACEHOLDER_ACCOUNT_NUMBER = "0000 0000"


def render_invoice_email(
    *,
    personal_message: str,
    student_display_name: str,
    lessons: list[Lesson],
    rate_cents: int,
    invoice_number: str,
    invoice_date: date,
    totals: InvoiceTotals,
    sender_name: str,
    sender_email: str,
) -> str:
    template = _env.get_template("invoice_email.html")
    sorted_lessons = sorted(lessons, key=lambda lesson: lesson.lesson_date)

    lesson_rows = [
        {
            "index": i,
            "bg": "#f8fafa" if i % 2 == 0 else "#ffffff",
            "day": DAY_NAMES[lesson.lesson_date.weekday()],
            "date_str": lesson.lesson_date.strftime("%d/%m"),
            "rate_str": _fmt_cents(rate_cents),
        }
        for i, lesson in enumerate(sorted_lessons, start=1)
    ]

    if sorted_lessons:
        period = (
            f"{sorted_lessons[0].lesson_date.strftime('%d %b')} – "
            f"{sorted_lessons[-1].lesson_date.strftime('%d %b %Y')}"
        )
    else:
        period = ""

    rendered: str = template.render(
        business_name=f"{sender_name} Tutoring",
        message_html=_build_message_html(personal_message),
        student_display_name=student_display_name,
        period=period,
        invoice_date_str=invoice_date.strftime("%d / %m / %Y"),
        invoice_number=invoice_number,
        lesson_rows=lesson_rows,
        total_str=_fmt_cents(totals.total_cents),
        sender_name=sender_name,
        sender_email=sender_email,
        bsb=PLACEHOLDER_BSB,
        account_number=PLACEHOLDER_ACCOUNT_NUMBER,
        bank_name=PLACEHOLDER_BANK_NAME,
    )
    return rendered


def _build_message_html(personal_message: str) -> Markup:
    """Escape user-entered text, then join with <br> — matching the original's
    manual escape-and-join, just done once in Python instead of via Jinja's
    join filter (which does not escape plain-string items reliably)."""
    escaped_lines = [str(escape(line)) for line in personal_message.splitlines()]
    return Markup("<br>".join(escaped_lines))


def _fmt_cents(cents: int) -> str:
    return f"${cents / 100:.2f}"
