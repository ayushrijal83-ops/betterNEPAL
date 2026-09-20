"""Authority model - the government body or utility responsible for fixing things.

An Incident says *what is broken*. An Authority says *who owns it*. Phase 7 is
the bridge between those two questions and nothing more: it does not schedule
work, hire contractors or track budgets (Phase 8).

Scope
-----

``level`` and ``district_id`` together describe how far an authority's remit
reaches:

* a federal body with ``district_id`` NULL is national in scope
* a provincial or local body is normally scoped to one district
* a utility may be either, depending on how it organises its offices

``district_id`` is nullable rather than required because a genuinely national
authority has no district, and forcing one would be inventing a fact. Routing
logic that wants "the authority for this district" therefore has to handle the
national fallback explicitly, which is the honest shape of the problem.

Contact details are nullable on purpose. The project's research file is
explicit that some district offices publish no contact and are marked "Not
listed"; storing an empty string or a provincial number in their place would
turn an absence of data into a false claim.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import BaseModel
from .enums import AuthorityType, GovernmentLevel, enum_column

if TYPE_CHECKING:  # pragma: no cover
    from .district import District
    from .project import Project
    from .incident import Incident
    from .disaster_incident import DisasterIncident

NAME_MAX_LENGTH = 150


class Authority(BaseModel):
    __tablename__ = "authorities"

    # Unique nationally: "Department of Roads - Bagmati" and "Department of
    # Roads - Gandaki" are different rows, and two rows with the same name
    # would make assignment ambiguous.
    name: Mapped[str] = mapped_column(
        String(NAME_MAX_LENGTH), nullable=False, unique=True, index=True
    )

    level: Mapped[GovernmentLevel] = mapped_column(
        enum_column(GovernmentLevel, "authority_level"), nullable=False, index=True
    )
    type: Mapped[AuthorityType] = mapped_column(
        enum_column(AuthorityType, "authority_type"), nullable=False, index=True
    )

    contact_email: Mapped[str | None] = mapped_column(String(120))
    contact_phone: Mapped[str | None] = mapped_column(String(50))

    # RESTRICT: an authority's district is part of its identity, so removing
    # the district must not silently orphan it.
    district_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "districts.id", ondelete="RESTRICT", name="fk_authorities_district_id"
        ),
        index=True,
    )

    district: Mapped["District | None"] = relationship(back_populates="authorities")
    incidents: Mapped[list["Incident"]] = relationship(back_populates="authority")
    projects: Mapped[list["Project"]] = relationship(
        back_populates="authority", passive_deletes="all"
    )
    disaster_incidents: Mapped[list["DisasterIncident"]] = relationship(
        back_populates="authority", passive_deletes="all"
    )

    __table_args__ = (
        # The routing question is always "who covers this district at this
        # level?", so those two columns are looked up together.
        Index("ix_authorities_district_level", "district_id", "level"),
    )

    @property
    def is_national(self) -> bool:
        """True when this authority is not scoped to a single district."""
        return self.district_id is None

    @property
    def incident_count(self) -> int:
        return len(self.incidents)

    def to_dict(self, include_counts: bool = False) -> dict[str, Any]:
        """Explicit serialisation.

        Contact details are included: these are published office contacts, not
        personal data, and putting them in front of citizens is the point.
        """
        payload: dict[str, Any] = {
            "id": str(self.id),
            "name": self.name,
            "level": self.level.value,
            "type": self.type.value,
            "contact_email": self.contact_email,
            "contact_phone": self.contact_phone,
            "district_id": str(self.district_id) if self.district_id else None,
            "district": self.district.name if self.district else None,
            "is_national": self.is_national,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_counts:
            payload["incident_count"] = self.incident_count
        return payload

    def __repr__(self) -> str:
        return f"<Authority {self.name} ({self.level.value})>"
