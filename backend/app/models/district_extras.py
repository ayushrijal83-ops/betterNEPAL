"""District enrichment: highways, emergency contacts, corridors, risk profile.

Normalized child tables rather than JSON arrays on ``District`` itself, matching
how the rest of the schema already handles one-to-many facts (see
``DisasterDispatch``, ``Municipality``). Each row carries ``ProvenanceMixin`` so
a highway reference or a phone number can be told apart from an invented one -
see that module's docstring for why that distinction is enforced in the
database, not just trusted in Python.

Absence is meaningful here: a district with no ``DistrictEmergencyContact`` row
has an *unverified* contact, not a blank one - the service layer is the thing
that turns "no row" into an honest "DEOC unavailable" rather than the API
silently returning nothing where a citizen would expect a phone number.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import BaseModel
from .provenance import ProvenanceMixin, verification_status_constraint

if TYPE_CHECKING:  # pragma: no cover
    from .district import District

EMERGENCY_CONTACT_KINDS: tuple[str, ...] = ("deoc", "police", "hospital")


class DistrictHighway(BaseModel, ProvenanceMixin):
    """A national highway reference known to pass through a district.

    ``code`` and ``name`` follow Department of Roads references (e.g. ``H04``,
    "Prithvi Highway") - never an invented route number.
    """

    __tablename__ = "district_highways"

    district_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("districts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    district: Mapped["District"] = relationship(back_populates="highways")

    __table_args__ = (
        UniqueConstraint("district_id", "code", name="uq_district_highways_district_code"),
        verification_status_constraint("district_highways"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "provenance": self.provenance_dict(),
        }


class DistrictEmergencyContact(BaseModel, ProvenanceMixin):
    """A district-level emergency contact (DEOC, police, or hospital)."""

    __tablename__ = "district_emergency_contacts"

    district_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("districts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    # Nullable on purpose: a verified-unavailable contact is not the same as a
    # row that has not been looked at yet, but neither one is a phone number.
    phone: Mapped[str | None] = mapped_column(String(32))

    district: Mapped["District"] = relationship(back_populates="emergency_contacts")

    __table_args__ = (
        UniqueConstraint(
            "district_id", "kind", name="uq_district_emergency_contacts_district_kind"
        ),
        CheckConstraint(
            "kind IN ('deoc', 'police', 'hospital')",
            name="ck_district_emergency_contacts_kind",
        ),
        verification_status_constraint("district_emergency_contacts"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "phone": self.phone,
            "provenance": self.provenance_dict(),
        }


class DistrictCorridor(BaseModel, ProvenanceMixin):
    """A named travel corridor associated with a district.

    Sourced from the project's existing rivers/roads reference document (see
    ``geography_reference_service.road_corridors_for_district``) - not invented
    per district.
    """

    __tablename__ = "district_corridors"

    district_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("districts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    district: Mapped["District"] = relationship(back_populates="corridors")

    __table_args__ = (
        UniqueConstraint("district_id", "name", name="uq_district_corridors_district_name"),
        verification_status_constraint("district_corridors"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "provenance": self.provenance_dict(),
        }


class DistrictRiskProfile(BaseModel, ProvenanceMixin):
    """A hazard type known to recur in a district.

    ``hazard_type`` reuses the platform's own disaster-type vocabulary
    (``app.models.enums.DisasterType``) rather than a free-text label, so a
    district's risk profile and the AI disaster classifier speak the same
    language everywhere else already does.
    """

    __tablename__ = "district_risk_profiles"

    district_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("districts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    hazard_type: Mapped[str] = mapped_column(String(32), nullable=False)

    district: Mapped["District"] = relationship(back_populates="risk_profiles")

    __table_args__ = (
        UniqueConstraint(
            "district_id", "hazard_type", name="uq_district_risk_profiles_district_hazard"
        ),
        verification_status_constraint("district_risk_profiles"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "hazard_type": self.hazard_type,
            "provenance": self.provenance_dict(),
        }
