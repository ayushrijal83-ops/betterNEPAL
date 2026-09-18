"""Declarative base and the shared model foundation.

Architectural decisions:

* Primary keys are UUIDs via SQLAlchemy 2.0's portable ``Uuid`` type, which
  renders as native ``UUID`` on PostgreSQL and ``CHAR(32)`` on SQLite. That
  keeps the PostgreSQL target clean while letting the test suite run on
  in-memory SQLite without a custom type.
* Timestamps use ``UtcDateTime`` (below) rather than a bare
  ``DateTime(timezone=True)``. PostgreSQL returns aware datetimes from
  ``TIMESTAMPTZ``, but SQLite discards the offset and hands back naive values —
  so without this the test suite and production would disagree about whether a
  timestamp is aware. Normalising on the way in and out removes that split.
* Defaults are applied in Python, not as server defaults, for the same
  portability reason: SQLite's ``CURRENT_TIMESTAMP`` is a naive UTC string.

This module deliberately imports nothing from the Flask app so that
``extensions.py`` can build the SQLAlchemy instance around ``Base``.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    """Timezone-aware current time, used for model timestamp defaults."""
    return datetime.now(timezone.utc)


class UtcDateTime(TypeDecorator):
    """A DateTime that is always stored and returned as aware UTC.

    Naive values are assumed to be UTC — every datetime in this application
    originates from :func:`utcnow`, so there is no local-time source to confuse
    it with.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base holding the metadata Alembic autogenerates against."""


class BaseModel(Base):
    """Abstract parent for every domain model: UUID id plus audit timestamps."""

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id}>"
