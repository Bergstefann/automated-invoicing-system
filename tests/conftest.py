"""Shared test fixtures.

`_block_network` is autouse and session-wide: no test in this suite may open
a real network socket. That's not a nice-to-have — it's the whole point of
the provider-interface architecture. If a test ever needed a real socket, it
would mean the pipeline had stopped depending only on the Protocols in
providers/base.py.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from invoicing.db import Database
from invoicing.models import Parent, Student


class NetworkBlockedError(RuntimeError):
    pass


def _guard(*_args: Any, **_kwargs: Any) -> Any:
    raise NetworkBlockedError("network access is not allowed in this test suite")


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _guard)
    monkeypatch.setattr(socket.socket, "connect_ex", _guard)


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database(":memory:")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 4, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def parent(db: Database) -> Parent:
    return db.insert_parent(Parent(name="Jordan Example", email="jordan@example.com"))


@pytest.fixture
def student(db: Database, parent: Parent) -> Student:
    assert parent.id is not None
    return db.insert_student(
        Student(
            name="Riley Example",
            parent_id=parent.id,
            instrument="Piano",
            rate_cents=4000,
            school="Example School",
        )
    )
