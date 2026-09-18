"""User model.

``password_hash`` is deliberately never exposed: serialisation goes through
:meth:`User.to_public_dict`, which builds an explicit dictionary rather than
dumping model attributes, so a future column cannot leak by accident.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from .base import BaseModel, UtcDateTime
from .role import Role, user_roles

if TYPE_CHECKING:  # pragma: no cover
    from .media_attachment import MediaAttachment
    from .progress_update import ProgressUpdate
    from .project import Project
    from .incident import Incident
    from .report import Report


class User(BaseModel):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    roles: Mapped[list[Role]] = relationship(
        secondary=user_roles, back_populates="users", lazy="selectin"
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
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }

    def __repr__(self) -> str:
        return f"<User {self.email}>"
