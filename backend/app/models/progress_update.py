"""ProgressUpdate - an append-only ledger of what happened on a project.

Every entry records who said what, and when. Status changes write an entry
automatically, capturing both the old and new value, so the history of a
project is reconstructable from the ledger alone rather than inferred from a
single mutable ``status`` column.

Why this does not inherit BaseModel
-----------------------------------

``BaseModel`` supplies ``updated_at``, and a ledger row must never be edited.
Carrying a column that should always equal ``created_at`` would invite exactly
the editing it is meant to forbid, and would make an altered row look
legitimate. The id and timestamp are therefore declared here directly, and the
absence of ``updated_at`` is the documentation.

Rows are immutable by convention rather than by database trigger - this phase
does not expose any endpoint that edits or deletes one, and there is no service
function to do so either.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UtcDateTime, utcnow
from .enums import ProjectStatus, enum_column

if TYPE_CHECKING:  # pragma: no cover
    from .project import Project
    from .user import User

NOTES_MAX_LENGTH = 5000


class ProgressUpdate(Base):
    __tablename__ = "progress_updates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False
    )

    # CASCADE: the ledger belongs to the project and is meaningless without it.
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "projects.id", ondelete="CASCADE", name="fk_progress_updates_project_id"
        ),
        nullable=False,
        index=True,
    )
    # RESTRICT: who said this is the point of the record, so the account cannot
    # be deleted out from under it.
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "users.id", ondelete="RESTRICT", name="fk_progress_updates_author_id"
        ),
        nullable=False,
        index=True,
    )

    notes: Mapped[str] = mapped_column(Text, nullable=False)

    # Both NULL on a routine note; both set when the entry records a transition.
    previous_status: Mapped[ProjectStatus | None] = mapped_column(
        enum_column(ProjectStatus, "progress_previous_status")
    )
    new_status: Mapped[ProjectStatus | None] = mapped_column(
        enum_column(ProjectStatus, "progress_new_status")
    )

    project: Mapped["Project"] = relationship(back_populates="progress_updates")
    author: Mapped["User"] = relationship(back_populates="progress_updates")

    __table_args__ = (
        # The ledger is always read newest-first for one project.
        Index("ix_progress_updates_project_created", "project_id", "created_at"),
    )

    @property
    def is_status_change(self) -> bool:
        return self.new_status is not None

    def to_dict(self) -> dict[str, Any]:
        """Explicit serialisation; the author is id and name only."""
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "notes": self.notes,
            "previous_status": (
                self.previous_status.value if self.previous_status else None
            ),
            "new_status": self.new_status.value if self.new_status else None,
            "is_status_change": self.is_status_change,
            "author": {
                "id": str(self.author_id),
                "full_name": self.author.full_name if self.author else None,
            },
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        return f"<ProgressUpdate {self.id} project={self.project_id}>"
