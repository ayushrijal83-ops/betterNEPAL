"""District model - the second level of Nepal's administrative hierarchy.

Hierarchy: Province -> District -> Municipality.

Why there is no Province model
------------------------------

Province is stored as a plain column on District rather than as its own table.
In this phase a province has no attributes of its own, no geometry, and nothing
references it - a Province table would be a one-column lookup joined for no
reason. Promote it to a model when it gains real state (its own boundary, an
assigned road authority, a budget); the research files point at exactly that
happening in the authority phase, which is where it belongs.

Why ``code`` is nullable
------------------------

Nepal's official district codes are not present in any dataset available to
this project, and inventing them would poison every later reconciliation. A
district is therefore identified by ``name``, which is unique nationally across
all 77, and ``code`` stays NULL until an authoritative dataset supplies it.
The unique constraint still applies to the codes that do exist - SQL treats
NULLs as distinct, so many rows may hold NULL while no two may share a code.

Boundary geometry is nullable for the same reason: a district row is useful for
names, hierarchy and municipality relationships long before an authoritative
boundary polygon has been imported. No geometry is invented here.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..gis.types import DEFAULT_SRID, GeometryColumn
from .base import BaseModel
from .provenance import ProvenanceMixin, verification_status_constraint

if TYPE_CHECKING:  # pragma: no cover
    from .announcement import Announcement
    from .authority import Authority
    from .incident import Incident
    from .municipality import Municipality
    from .report import Report
    from .disaster_incident import DisasterIncident
    from .district_extras import (
        DistrictCorridor,
        DistrictEmergencyContact,
        DistrictHighway,
        DistrictRiskProfile,
    )


class District(BaseModel, ProvenanceMixin):
    __tablename__ = "districts"

    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    # Nepali-script name, when the source dataset provides one.
    name_ne: Mapped[str | None] = mapped_column(String(120))
    # Maithili-script name, when available; falls back to Nepali form.
    name_mai: Mapped[str | None] = mapped_column(String(120))
    province: Mapped[str | None] = mapped_column(String(64), index=True)

    # Official administrative code. See module docstring on nullability.
    code: Mapped[str | None] = mapped_column(String(16), unique=True, index=True)

    # District headquarters (administrative centre).
    headquarters: Mapped[str | None] = mapped_column(String(120))
    # Approximate headquarters coordinates (latitude, longitude).
    # Not a surveyed centroid; for reference/display only.
    latitude: Mapped[float | None] = mapped_column(nullable=True)
    longitude: Mapped[float | None] = mapped_column(nullable=True)

    boundary = mapped_column(GeometryColumn("MULTIPOLYGON", DEFAULT_SRID), nullable=True)

    municipalities: Mapped[list["Municipality"]] = relationship(
        back_populates="district",
        # No cascade: deleting a district that still holds municipalities must
        # raise, matching the RESTRICT foreign key on the child table.
        passive_deletes="all",
    )
    reports: Mapped[list["Report"]] = relationship(
        back_populates="district", passive_deletes="all"
    )
    incidents: Mapped[list["Incident"]] = relationship(
        back_populates="district", passive_deletes="all"
    )
    authorities: Mapped[list["Authority"]] = relationship(
        back_populates="district", passive_deletes="all"
    )
    announcements: Mapped[list["Announcement"]] = relationship(
        back_populates="district", passive_deletes="all"
    )
    disaster_incidents: Mapped[list["DisasterIncident"]] = relationship(
        back_populates="district", passive_deletes="all"
    )
    highways: Mapped[list["DistrictHighway"]] = relationship(
        back_populates="district", passive_deletes="all", order_by="DistrictHighway.code"
    )
    emergency_contacts: Mapped[list["DistrictEmergencyContact"]] = relationship(
        back_populates="district", passive_deletes="all"
    )
    corridors: Mapped[list["DistrictCorridor"]] = relationship(
        back_populates="district", passive_deletes="all", order_by="DistrictCorridor.name"
    )
    risk_profiles: Mapped[list["DistrictRiskProfile"]] = relationship(
        back_populates="district", passive_deletes="all"
    )

    __table_args__ = (
        # District names are unique across Nepal, so this is what makes the
        # reference import idempotent while codes are still unknown.
        UniqueConstraint("name", name="uq_districts_name"),
        verification_status_constraint("districts"),
        # GIST is the index type PostGIS uses for geometry. Without it every
        # point-in-polygon lookup (reverse geocoding a report's coordinates)
        # degrades to a sequential scan over 77 large multipolygons. Rendered
        # only on PostgreSQL; see app/gis/types.py.
        Index("ix_districts_boundary", "boundary", postgresql_using="gist"),
    )

    def to_dict(self, geometry: dict | None = None) -> dict:
        """Explicit serialisation.

        ``geometry`` is passed in rather than read off the model: converting a
        PostGIS column to GeoJSON requires a database function, which is the
        service layer's job, and on SQLite there is nothing to convert.
        """
        return {
            "id": str(self.id),
            "name": self.name,
            "name_ne": self.name_ne,
            "name_mai": self.name_mai,
            "province": self.province,
            "code": self.code,
            "headquarters": self.headquarters,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "geometry": geometry,
            "provenance": self.provenance_dict(),
        }

    def to_full_dict(self, geometry: dict | None = None) -> dict:
        """``to_dict()`` plus highways, corridors, risk profile and emergency
        contacts. Callers must eager-load those relationships first
        (``selectinload``) - this does not query.

        Every emergency-contact kind always appears, even when unverified: a
        district nobody has looked at yet says ``"unknown"`` /
        ``phone: None`` here, never a silently missing key a client could
        mistake for "no contact info exists for this feature".
        """
        from .district_extras import EMERGENCY_CONTACT_KINDS

        base = self.to_dict(geometry=geometry)

        by_kind = {contact.kind: contact for contact in self.emergency_contacts}
        emergency = {}
        for kind in EMERGENCY_CONTACT_KINDS:
            contact = by_kind.get(kind)
            if contact is None:
                emergency[kind] = {
                    "name": None,
                    "phone": None,
                    "provenance": {
                        "source": None,
                        "source_url": None,
                        "source_type": None,
                        "verification_status": "unverified",
                        "last_verified_at": None,
                    },
                }
            else:
                emergency[kind] = {"name": contact.name, "phone": contact.phone,
                                    "provenance": contact.provenance_dict()}

        base.update({
            "highways": [h.to_dict() for h in self.highways],
            "corridors": [c.to_dict() for c in self.corridors],
            "risk_profile": [r.hazard_type for r in self.risk_profiles],
            "emergency": emergency,
        })
        return base

    def __repr__(self) -> str:
        return f"<District {self.name} ({self.province})>"
