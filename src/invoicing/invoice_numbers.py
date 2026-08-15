"""Invoice numbering scheme.

Preserves the original's visible format — DDMMYY followed by a two-digit
sequence — but grounds the sequence in persisted state (how many invoices
have already been issued that calendar day, per `Database.count_invoices_issued_on`)
rather than a per-process in-memory rank. The original computed the sequence
as a student's alphabetical rank within a single run, which meant two
separate runs on the same day could mint the same invoice number; this
version can't collide because the count is read from the database, not
recomputed from scratch each run.
"""

from __future__ import annotations

from datetime import date

MAX_DAILY_SEQUENCE = 99


def next_invoice_number(issued_on: date, already_issued_today: int) -> str:
    seq = already_issued_today + 1
    if seq > MAX_DAILY_SEQUENCE:
        raise ValueError("more than 99 invoices in a single day is not representable")
    return f"{issued_on.strftime('%d%m%y')}{seq:02d}"
