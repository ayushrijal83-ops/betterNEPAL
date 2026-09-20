"""User model.

``password_hash`` is deliberately never exposed: serialisation goes through
:meth:`User.to_public_dict`, which builds an explicit dictionary rather than
dumping model attributes, so a future column cannot leak by accident.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from .base import BaseModel, UtcDateTime
from .role import Role, user_roles

if TYPE_CHECKING:  # pragma: no cover
    from .district import District
    from .announcement import Announcement
    from .media_attachment import MediaAttachment
    from .progress_update import ProgressUpdate
    from .project import Project
    from .incident import Incident
    from .report import Report
    from .disaster_incident import DisasterIncident, DisasterDispatch


class User(BaseModel):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    # Where this person is from, and where they are now. Nepal's internal
    # migration makes the distinction real: someone registered in Jumla and
    # working in Kathmandu wants alerts for both.
    #
    # Nullable at the database level even though citizen registration requires
    # it. Two reasons that cannot be argued away:
    #   * staff accounts (admin, authority, contractor) have no home district,
    #     and inventing one would put fiction in the record;
    #   * NOT NULL cannot be added to a populated table without a backfill
    #     value, and there is no correct district to backfill with.
    # The requirement lives in validate_registration, which is where it means
    # something.
    permanent_district_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "districts.id", ondelete="RESTRICT", name="fk_users_permanent_district_id"
        ),
        index=True,
    )
    # Defaults to the permanent district when registration omits it, so "where
    # am I now" always has an answer rather than being silently absent.
    temporary_district_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "districts.id", ondelete="RESTRICT", name="fk_users_temporary_district_id"
        ),
        index=True,
    )

    roles: Mapped[list[Role]] = relationship(
        secondary=user_roles, back_populates="users", lazy="selectin"
    )
    # Two foreign keys to the same table, so each relationship must name its own.
    permanent_district: Mapped["District | None"] = relationship(
        foreign_keys=[permanent_district_id], lazy="selectin"
    )
    temporary_district: Mapped["District | None"] = relationship(
        foreign_keys=[temporary_district_id], lazy="selectin"
    )
    # No cascade: reports outlive account changes, and the RESTRICT foreign key
    # on reports.reporter_id refuses to delete a user who has filed any.
    reports: Mapped[list["Report"]] = relationship(
        back_populates="reporter", passive_deletes="all"
    )
    verified_incidents: Mapped[list["Incident"]] = relationship(
        foreign_keys="Incident.verified_by_id",
        back_populates="verified_by",
        passive_deletes="all",
    )
    assigned_incidents: Mapped[list["Incident"]] = relationship(
        foreign_keys="Incident.assigned_by_id",
        back_populates="assigned_by",
        passive_deletes="all",
    )
    contracted_projects: Mapped[list["Project"]] = relationship(
        back_populates="contractor", passive_deletes="all"
    )
    progress_updates: Mapped[list["ProgressUpdate"]] = relationship(
        back_populates="author", passive_deletes="all"
    )
    media_attachments: Mapped[list["MediaAttachment"]] = relationship(
        back_populates="uploader", passive_deletes="all"
    )
    announcements: Mapped[list["Announcement"]] = relationship(
        back_populates="author", passive_deletes="all"
    )
    assigned_disaster_incidents: Mapped[list["DisasterIncident"]] = relationship(
        foreign_keys="DisasterIncident.assigned_by_id", back_populates="assigned_by", passive_deletes="all"
    )
    disaster_dispatches: Mapped[list["DisasterDispatch"]] = relationship(
        back_populates="authority_user", passive_deletes="all"
    )

    @validates("email")
    def _normalise_email(self, key: str, value: str) -> str:
        """Normalise at the model boundary so the unique index is meaningful.

        Doing this here rather than only in the service means no code path can
        insert a differently-cased duplicate.
        """
        return value.strip().lower()

    @property
    def role_names(self) -> list[str]:
        return sorted(role.name for role in self.roles)

    def has_role(self, *names: str) -> bool:
        return bool(set(names) & set(self.role_names))

    def to_public_dict(self) -> dict[str, Any]:
        """The only representation of a user that may cross the API boundary."""
        return {
            "id": str(self.id),
            "email": self.email,
            "full_name": self.full_name,
            "phone": self.phone,
            "roles": self.role_names,
            "permanent_district_id": (
                str(self.permanent_district_id) if self.permanent_district_id else None
            ),
            "permanent_district": (
                self.permanent_district.name if self.permanent_district else None
            ),
            "temporary_district_id": (
                str(self.temporary_district_id) if self.temporary_district_id else None
            ),
            "temporary_district": (
                self.temporary_district.name if self.temporary_district else None
            ),
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }

    def __repr__(self) -> str:
        return f"<User {self.email}>"
