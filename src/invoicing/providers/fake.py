"""In-memory fake providers.

Used by every test and by `--demo` mode. Each fake records what was asked of
it so tests can assert on behaviour without any network access ever being
possible in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from invoicing.models import SheetStatus
from invoicing.providers.base import (
    EmailAttachment,
    InvoiceDocData,
    ScheduleSnapshot,
    SheetLessonRef,
)


@dataclass
class FakeSheetProvider:
    snapshot: ScheduleSnapshot
    marked_billed: list[tuple[SheetLessonRef, SheetStatus]] = field(default_factory=list)

    def read_schedule(self) -> ScheduleSnapshot:
        return self.snapshot

    def mark_lessons_billed(self, lesson_refs: list[SheetLessonRef], status: SheetStatus) -> None:
        for ref in lesson_refs:
            self.marked_billed.append((ref, status))


@dataclass
class FakeDocProvider:
    created_docs: dict[str, InvoiceDocData] = field(default_factory=dict)
    _next_id: int = 1

    def create_invoice_doc(self, data: InvoiceDocData) -> str:
        doc_id = f"fake-doc-{self._next_id}"
        self._next_id += 1
        self.created_docs[doc_id] = data
        return doc_id

    def export_pdf(self, doc_id: str) -> bytes:
        if doc_id not in self.created_docs:
            raise KeyError(f"unknown doc_id {doc_id}")
        return f"%PDF-FAKE for {doc_id}".encode()


@dataclass
class SentEmail:
    to: str
    subject: str
    html_body: str
    attachment: EmailAttachment | None


@dataclass
class FakeEmailProvider:
    """`fail_after` simulates a batch failing partway through: the Nth send
    (0-indexed, counting successes so far) raises instead of succeeding, so
    tests can verify partial-failure recovery."""

    sent: list[SentEmail] = field(default_factory=list)
    fail_after: int | None = None
    _next_id: int = 1

    def send(
        self,
        to: str,
        subject: str,
        html_body: str,
        attachment: EmailAttachment | None = None,
    ) -> str:
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise RuntimeError("simulated email provider failure")
        self.sent.append(
            SentEmail(to=to, subject=subject, html_body=html_body, attachment=attachment)
        )
        message_id = f"fake-msg-{self._next_id}"
        self._next_id += 1
        return message_id
