"""Role model and the user<->role association table.

Roles are a fixed vocabulary (see ``ROLE_NAMES``), not free-form strings. The
association table is a plain ``Table`` rather than a model because it carries no
data of its own beyond the pairing.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Column, ForeignKey, String, Table
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, BaseModel

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers only
    from .user import User

# The five application roles. Anything outside this set is rejected before it
# reaches the database.
ROLE_CITIZEN = "citizen"
ROLE_TREKKING_GUIDE = "trekking_guide"
ROLE_AUTHORITY = "authority"
ROLE_CONTRACTOR = "contractor"
ROLE_ADMIN = "admin"

ROLE_NAMES: tuple[str, ...] = (
    ROLE_CITIZEN,
    ROLE_TREKKING_GUIDE,
    ROLE_AUTHORITY,
    ROLE_CONTRACTOR,
    ROLE_ADMIN,
)

# The only role a public registration may be granted.
DEFAULT_ROLE = ROLE_CITIZEN

ROLE_DESCRIPTIONS: dict[str, str] = {
    ROLE_CITIZEN: "Submits reports about problems they observe.",
    ROLE_TREKKING_GUIDE: "Reports trail, hazard and conservation issues from the field.",
    ROLE_AUTHORITY: "Verifies reports, assigns responsibility and tracks resolution.",
    ROLE_CONTRACTOR: "Carries out assigned project work and reports progress.",
    ROLE_ADMIN: "Administers the platform.",
}


user_roles = Table(
    "user_roles",
    Base.metadata,
    Column(
        "user_id",
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "role_id",
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Role(BaseModel):
    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(255))

    users: Mapped[list["User"]] = relationship(
        secondary=user_roles, back_populates="roles"
    )

    def __repr__(self) -> str:
        return f"<Role {self.name}>"
