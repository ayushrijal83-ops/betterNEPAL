"""Project model - the physical work commissioned to fix something.

An Incident says *what is broken*; an Authority says *who owns it*; a Project is
*what is being done about it*, and by whom.

``incident_id`` is nullable because not all work is reactive. A municipality
resurfacing a road on a maintenance schedule has a real project with no citizen
report behind it, and forcing a synthetic incident to hang it from would
corrupt the incident record.

``authority_id`` is NOT NULL and RESTRICT: work with no accountable
commissioning body is exactly what this platform exists to prevent, and the
record of who commissioned it must survive.

``contractor_id`` is nullable until the work is awarded, and SET NULL on
delete - a project outlives the contractor account attached to it.

Money is deliberately absent. The brief for this platform is explicit that
budget figures must originate from authoritative database records rather than
being typed in alongside progress notes; a `budget` column here would invite
exactly that. It belongs with procurement data, not with work tracking.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import BaseModel
from .enums import ProjectStatus, enum_column

if TYPE_CHECKING:  # pragma: no cover
    from .authority import Authority
    from .incident import Incident
    from .progress_update import ProgressUpdate
    from .user import User

TITLE_MAX_LENGTH = 150


class Project(BaseModel):
    __tablename__ = "projects"

    title: Mapped[str] = mapped_column(String(TITLE_MAX_LENGTH), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[ProjectStatus] = mapped_column(
        enum_column(ProjectStatus, "project_status"),
        nullable=False,
        default=ProjectStatus.PLANNED,
        server_default=ProjectStatus.PLANNED.value,
        index=True,
    )

    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "incidents.id", ondelete="SET NULL", name="fk_projects_incident_id"
        ),
        index=True,
    )
    authority_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "authorities.id", ondelete="RESTRICT", name="fk_projects_authority_id"
        ),
        nullable=False,
        index=True,
    )
    contractor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "users.id", ondelete="SET NULL", name="fk_projects_contractor_id"
        ),
        index=True,
    )

    start_date: Mapped[date | None] = mapped_column(Date)
    estimated_end_date: Mapped[date | None] = mapped_column(Date)
    # Written by the service when the project completes, never typed in.
    actual_end_date: Mapped[date | None] = mapped_column(Date)

    incident: Mapped["Incident | None"] = relationship(back_populates="projects")
    authority: Mapped["Authority"] = relationship(back_populates="projects")
    contractor: Mapped["User | None"] = relationship(back_populates="contracted_projects")

    # CASCADE: the ledger is part of the project, meaningless without it.
    progress_updates: Mapped[list["ProgressUpdate"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProgressUpdate.created_at.desc()",
    )

    __table_args__ = (
        # There is deliberately NO constraint requiring estimated_end_date to
        # follow start_date. Work legitimately begins after its original
        # estimated finish - that is precisely what an overdue project is, and
        # a constraint here would make late starts unrecordable. The creation
        # validator still rejects an end date before a start date supplied in
        # the same request, which catches the typo without forbidding reality.
        #
        # Actual completion is different: you cannot finish before you begin.
        CheckConstraint(
            "start_date IS NULL OR actual_end_date IS NULL "
            "OR actual_end_date >= start_date",
            name="ck_projects_actual_end_after_start",
        ),
        Index("ix_projects_created_at", "created_at"),
        # The authority dashboard asks "what is active on my desk".
        Index("ix_projects_authority_status", "authority_id", "status"),
    )

    @property
    def update_count(self) -> int:
        return len(self.progress_updates)

    @property
    def is_overdue(self) -> bool:
        """Past its estimated end date and not finished.

        Computed rather than stored: a stored flag would need a nightly job to
        stay true, and would be wrong between runs.
        """
        if self.estimated_end_date is None:
            return False
        if self.status in (ProjectStatus.COMPLETED, ProjectStatus.CANCELLED):
            return False
        return self.estimated_end_date < date.today()

    def to_dict(self, include_updates: bool = False) -> dict[str, Any]:
        """Explicit serialisation.

        The contractor is reduced to id and name: project listings are public,
        and dumping the user object would leak an email address.
        """
        payload: dict[str, Any] = {
            "id": str(self.id),
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "incident_id": str(self.incident_id) if self.incident_id else None,
            "incident": (
                {
                    "id": str(self.incident.id),
                    "title": self.incident.title,
                    "status": self.incident.status.value,
                }
                if self.incident
                else None
            ),
            "authority_id": str(self.authority_id),
            "authority": self.authority.name if self.authority else None,
            "contractor_id": str(self.contractor_id) if self.contractor_id else None,
            "contractor": (
                {
                    "id": str(self.contractor_id),
                    "full_name": self.contractor.full_name if self.contractor else None,
                }
                if self.contractor_id
                else None
            ),
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "estimated_end_date": (
                self.estimated_end_date.isoformat() if self.estimated_end_date else None
            ),
            "actual_end_date": (
                self.actual_end_date.isoformat() if self.actual_end_date else None
            ),
            "is_overdue": self.is_overdue,
            "update_count": self.update_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_updates:
            payload["progress_updates"] = [
                update.to_dict() for update in self.progress_updates
            ]
        return payload

    def __repr__(self) -> str:
        return f"<Project {self.title} ({self.status.value})>"
