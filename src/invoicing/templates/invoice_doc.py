"""Google Doc invoice template field mapping.

Placeholder tags and table layout are preserved exactly from the original
template: per-row placeholders `<dayOfWeek{n}>`, `<DD/MM{n}>`, `<name{n}>`,
`<rate{n}>` for n up to the template's row capacity, plus header
placeholders `<parentName>`, `<parentEmail>`, `<invID>`, `<invoiceDay>`,
`<invoiceMonth>`, `<invoiceYear>`, `<stuName>`, `$<subtotal>`, `$<gst>`,
`$<invoiceTotal>`. Rows beyond the actual lesson count are blanked, not
removed — same behaviour as the original.
"""

from __future__ import annotations

from invoicing.providers.base import InvoiceDocData

SHORT_TEMPLATE_ROWS = 4
LONG_TEMPLATE_ROWS = 8


def template_row_capacity(line_count: int) -> int:
    return SHORT_TEMPLATE_ROWS if line_count <= SHORT_TEMPLATE_ROWS else LONG_TEMPLATE_ROWS


def build_replacements(data: InvoiceDocData) -> dict[str, str]:
    """Maps invoice data to the exact placeholder tags used by the template.

    Returns a flat dict of tag -> replacement text; providers/google.py turns
    this into `replaceAllText` batchUpdate requests. Kept as a plain dict here
    so the mapping is testable without any Google API objects.
    """
    capacity = template_row_capacity(len(data.lines))

    replacements: dict[str, str] = {
        "<parentName>": data.parent_name,
        "<parentEmail>": data.parent_email,
        "<invID>": data.invoice_number,
        "<invoiceDay>": f"{data.invoice_date.day:02d}",
        "<invoiceMonth>": f"{data.invoice_date.month:02d}",
        "<invoiceYear>": str(data.invoice_date.year),
        "<stuName>": data.student_display_name,
        "$<subtotal>": _fmt_cents(data.subtotal_cents),
        "$<gst>": _fmt_cents(data.gst_cents),
        "$<invoiceTotal>": _fmt_cents(data.total_cents),
    }

    for i, line in enumerate(data.lines, start=1):
        replacements[f"<dayOfWeek{i}>"] = line.day_of_week
        replacements[f"<DD/MM{i}>"] = line.date_str
        replacements[f"<name{i}>"] = line.student_display_name
        replacements[f"<rate{i}>"] = _fmt_cents(line.rate_cents)

    for i in range(len(data.lines) + 1, capacity + 1):
        replacements[f"<dayOfWeek{i}>"] = ""
        replacements[f"<DD/MM{i}>"] = ""
        replacements[f"<name{i}>"] = ""
        replacements[f"<rate{i}>"] = ""

    return replacements


def _fmt_cents(cents: int) -> str:
    return f"${cents / 100:.2f}"
