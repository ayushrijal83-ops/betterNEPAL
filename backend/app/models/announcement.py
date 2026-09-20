"""Announcement - the social feed and the disaster-alert channel.

One model serves both, because they are the same record with a different
audience: an announcement with a ``district_id`` is a local feed item, and one
without is national. Splitting them into two tables would mean two of every
query and two of every permission check for no gain.

Drafts
------

``is_draft`` exists because the platform lets AI *write* alerts but never
*publish* them. A generated disaster warning lands as a draft and stays out of
every public feed until a human with authority clears it. That boundary is the
same one the rest of the platform keeps - AI understands, humans verify - and
it matters most here: an automated alert is the one piece of content that would
reach the largest audience with the least review.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UtcDateTime, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from .district import District
    from .user import User

TITLE_MAX_LENGTH = 200


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False
    )

    title: Mapped[str] = mapped_column(String(TITLE_MAX_LENGTH), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # RESTRICT: who published an alert is part of what makes it authoritative,
    # so the account cannot be removed out from under it.
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_announcements_author_id"),
        nullable=False,
        index=True,
    )

    # NULL means national. Not a sentinel district row: "everywhere" is a real
    # and different thing from "a district that happens to be called Nepal".
    district_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "districts.id", ondelete="RESTRICT", name="fk_announcements_district_id"
        ),
        index=True,
    )

    is_draft: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0", index=True
    )

    author: Mapped["User"] = relationship(back_populates="announcements")
    district: Mapped["District | None"] = relationship(back_populates="announcements")

    __table_args__ = (
        # The feed query is always "published items for this district (or
        # national), newest first".
        Index("ix_announcements_feed", "district_id", "is_draft", "created_at"),
    )

    @property
    def is_national(self) -> bool:
        return self.district_id is None

    def to_dict(self) -> dict[str, Any]:
        """Explicit serialisation; the author is id and name only."""
        return {
            "id": str(self.id),
            "title": self.title,
            "body": self.body,
            "district_id": str(self.district_id) if self.district_id else None,
            "district": self.district.name if self.district else None,
            "scope": "national" if self.is_national else "district",
            "is_draft": self.is_draft,
            "author": {
                "id": str(self.author_id),
                "full_name": self.author.full_name if self.author else None,
            },
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        scope = "national" if self.is_national else str(self.district_id)
        return f"<Announcement {self.title[:30]} ({scope})>"
