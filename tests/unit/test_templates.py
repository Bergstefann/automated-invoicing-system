"""Unit tests for the two preserved templates.

templates/invoice_email.py: the HTML widget renders with every dynamic slot
filled in, and — since the original manually escaped user text before
inlining it — the personal message must still be escaped here too.

templates/invoice_doc.py: build_replacements produces a value for every
placeholder tag the Doc template uses, including blanking unused rows, and
none of those tags can survive into a rendered document.
"""

from __future__ import annotations

import re
from datetime import date

from invoicing.billing import InvoiceTotals
from invoicing.models import AttendanceStatus, Lesson
from invoicing.providers.base import InvoiceDocData, InvoiceDocLine
from invoicing.templates.invoice_doc import (
    LONG_TEMPLATE_ROWS,
    SHORT_TEMPLATE_ROWS,
    build_replacements,
    template_row_capacity,
)
from invoicing.templates.invoice_email import render_invoice_email

# ── invoice_email.html / invoice_email.py ──────────────────────────────────


def _lesson(day: date) -> Lesson:
    return Lesson(
        student_id=1,
        lesson_date=day,
        duration_minutes=30,
        attendance_status=AttendanceStatus.ATTENDED,
    )


def _render(personal_message: str = "Thanks <student>!") -> str:
    lessons = [_lesson(date(2026, 2, 2)), _lesson(date(2026, 2, 9))]
    totals = InvoiceTotals(subtotal_cents=8000, gst_cents=0, total_cents=8000)
    return render_invoice_email(
        personal_message=personal_message,
        student_display_name="Riley Example",
        lessons=lessons,
        rate_cents=4000,
        invoice_number="15082601",
        invoice_date=date(2026, 8, 15),
        totals=totals,
        sender_name="Jane Tutor",
        sender_email="jane@example.com",
    )


def test_render_invoice_email_fills_every_dynamic_field() -> None:
    html = _render()
    assert "Riley Example" in html
    assert "15082601" in html
    assert "$80.00" in html
    assert "jane@example.com" in html
    assert "Jane Tutor" in html


def test_render_invoice_email_leaves_no_unrendered_jinja_expressions() -> None:
    html = _render()
    assert "{{" not in html
    assert "}}" not in html
    assert "{%" not in html


def test_render_invoice_email_escapes_html_in_the_personal_message() -> None:
    html = _render(personal_message="5 < 10 & <b>bold</b>")
    assert "<b>bold</b>" not in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html
    assert "5 &lt; 10 &amp; " in html


def test_render_invoice_email_converts_newlines_to_br() -> None:
    html = _render(personal_message="line one\nline two")
    assert "line one<br>line two" in html


def test_render_invoice_email_never_contains_the_original_real_payment_details() -> None:
    html = _render()
    # The four real values from the original are never allowed to appear,
    # even as a regression check on the redaction itself.
    assert "062-170" not in html
    assert "1019 2875" not in html
    assert "thomas.mountstephens@gmail.com" not in html
    assert "Commonwealth Bank" not in html


def test_render_invoice_email_lesson_rows_alternate_background_colour() -> None:
    html = _render()
    rows_section = html[html.index("Rate</th>") :]
    first_row_bg = rows_section.index("background:#ffffff")
    second_row_bg = rows_section.index("background:#f8fafa")
    assert second_row_bg > first_row_bg


# ── invoice_doc.py ───────────────────────────────────────────────────────


def _doc_data(line_count: int) -> InvoiceDocData:
    lines = [
        InvoiceDocLine(
            day_of_week="Monday",
            date_str=f"0{i}/02",
            student_display_name="Riley Example",
            rate_cents=4000,
        )
        for i in range(1, line_count + 1)
    ]
    return InvoiceDocData(
        parent_name="Jordan Example",
        parent_email="jordan@example.com",
        invoice_number="15082601",
        invoice_date=date(2026, 8, 15),
        student_display_name="Riley Example",
        subtotal_cents=4000 * line_count,
        gst_cents=0,
        total_cents=4000 * line_count,
        lines=lines,
    )


def test_template_row_capacity_uses_short_template_up_to_four_lines() -> None:
    assert template_row_capacity(1) == SHORT_TEMPLATE_ROWS
    assert template_row_capacity(4) == SHORT_TEMPLATE_ROWS


def test_template_row_capacity_uses_long_template_above_four_lines() -> None:
    assert template_row_capacity(5) == LONG_TEMPLATE_ROWS
    assert template_row_capacity(8) == LONG_TEMPLATE_ROWS


def test_build_replacements_covers_every_header_placeholder() -> None:
    replacements = build_replacements(_doc_data(2))
    for tag in (
        "<parentName>",
        "<parentEmail>",
        "<invID>",
        "<invoiceDay>",
        "<invoiceMonth>",
        "<invoiceYear>",
        "<stuName>",
        "<subtotal>",
        "<gst>",
        "<invoiceTotal>",
    ):
        assert tag in replacements
        assert replacements[tag] != ""


def test_build_replacements_fills_real_rows_and_blanks_the_rest() -> None:
    replacements = build_replacements(_doc_data(2))
    assert replacements["<name1>"] == "Riley Example"
    assert replacements["<name2>"] == "Riley Example"
    assert replacements["<rate1>"] == "$40.00"
    # rows 3-4 exist (short template capacity) but must be blanked
    assert replacements["<name3>"] == ""
    assert replacements["<dayOfWeek4>"] == ""
    assert "<name5>" not in replacements  # outside the short template's capacity


def test_build_replacements_switches_to_the_long_template_above_four_lines() -> None:
    replacements = build_replacements(_doc_data(6))
    assert replacements["<name6>"] == "Riley Example"
    assert replacements["<name7>"] == ""
    assert replacements["<name8>"] == ""
    assert "<name9>" not in replacements


def test_build_replacements_gst_is_always_zero_dollars() -> None:
    replacements = build_replacements(_doc_data(3))
    assert replacements["<gst>"] == "$0.00"


def test_applying_replacements_leaves_no_placeholder_tags_in_a_template() -> None:
    data = _doc_data(3)
    replacements = build_replacements(data)

    # A synthetic stand-in for the real (external) Doc template text, using
    # the short-template's full tag set — everything build_replacements
    # promises to fill in.
    template_text = " ".join(replacements.keys())

    rendered = template_text
    for tag, value in replacements.items():
        rendered = rendered.replace(tag, value)

    assert not re.search(r"<[a-zA-Z/]+\d*>", rendered)
