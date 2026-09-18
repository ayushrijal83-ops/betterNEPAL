"""Report model - an observation submitted by a citizen or trekking guide.

A report is *what someone said they saw*. It is not yet a confirmed problem;
turning one into an Incident is a human verification step in Phase 6. Nothing
here routes, classifies or groups anything.

Why latitude/longitude are stored alongside ``location``
--------------------------------------------------------

``location`` is a PostGIS ``geometry(POINT, 4326)`` column, which is what makes
spatial queries possible - but Phase 4 established that geometry columns
degrade to TEXT off PostgreSQL so the test suite can run on SQLite, where
nothing can write or read a real point.

So the authoritative coordinates live in two plain ``Float`` columns, which work
everywhere, and ``location`` is *derived* from them at write time by the service
layer. One source of truth (the floats), one index-able projection of it (the
geometry). On SQLite ``location`` simply stays NULL and every non-spatial part
of the feature still works.

The API always builds its GeoJSON point from the floats, so a report's
coordinates read back identically on both backends.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, CheckConstraint, Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..gis.types import DEFAULT_SRID, GeometryColumn
from .base import BaseModel
from .enums import ReportCategory, ReportStatus, enum_column

if TYPE_CHECKING:  # pragma: no cover
    from .district import District
    from .incident import Incident
    from .municipality import Municipality
    from .user import User

TITLE_MAX_LENGTH = 100


class Report(BaseModel):
    __tablename__ = "reports"

    # RESTRICT, not CASCADE: a civic record must not vanish because an account
    # was removed. Deleting a user with reports fails until they are dealt with
    # deliberately.
    reporter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(TITLE_MAX_LENGTH), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    category: Mapped[ReportCategory] = mapped_column(
        enum_column(ReportCategory, "report_category"), nullable=False, index=True
    )
    status: Mapped[ReportStatus] = mapped_column(
        enum_column(ReportStatus, "report_status"),
        nullable=False,
        default=ReportStatus.SUBMITTED,
        server_default=ReportStatus.SUBMITTED.value,
        index=True,
    )

    # Nullable: resolved by reverse geocoding, which legitimately returns
    # nothing when no boundary dataset has been imported or the point falls
    # outside known coverage. A report is still worth keeping in that case.
    district_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("districts.id", ondelete="RESTRICT"), index=True
    )
    municipality_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("municipalities.id", ondelete="RESTRICT"), index=True
    )

    # Authoritative coordinates; see module docstring.
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)

    # Derived from the floats by the service layer. PostGIS only.
    location = mapped_column(GeometryColumn("POINT", DEFAULT_SRID), nullable=True)

    # Set when a human verifies this report and promotes or attaches it to an
    # Incident. NULL means "not yet verified", which is most reports.
    # SET NULL on delete: removing an incident must not destroy the citizen
    # submissions that evidenced it - they fall back to unlinked.
    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        # Named explicitly: an anonymous constraint added by ALTER TABLE cannot
        # be dropped by a generated downgrade, which Alembic warns about.
        ForeignKey(
            "incidents.id", ondelete="SET NULL", name="fk_reports_incident_id"
        ),
        index=True,
    )

    # What the AI *suggested*, never what the platform concluded. Kept beside
    # the human-set `category` rather than overwriting it: the project's rule is
    # that AI understands, the database decides and humans verify, so an
    # automated guess must stay visibly separate from the record of fact.
    # JSON here renders as JSONB on PostgreSQL and as text on SQLite.
    ai_metadata: Mapped[dict | None] = mapped_column(JSON)

    # Set by a human acting on a duplicate suggestion, never by the AI itself.
    # SET NULL: deleting a master report must not destroy the reports that were
    # folded into it - they revert to standing on their own.
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reports.id", ondelete="SET NULL", name="fk_reports_duplicate_of_id"),
        index=True,
    )

    reporter: Mapped["User"] = relationship(back_populates="reports")
    # Self-referential: remote_side names the "one" end of the many-to-one.
    duplicate_of: Mapped["Report | None"] = relationship(
        "Report", remote_side="Report.id", back_populates="duplicates"
    )
    duplicates: Mapped[list["Report"]] = relationship(
        "Report", back_populates="duplicate_of", passive_deletes="all"
    )
    district: Mapped["District | None"] = relationship(back_populates="reports")
    municipality: Mapped["Municipality | None"] = relationship(back_populates="reports")
    incident: Mapped["Incident | None"] = relationship(back_populates="reports")

    __table_args__ = (
        # Defence in depth: the API validates coordinates, and so does the
        # database, so a direct SQL insert cannot store an impossible point.
        # A report cannot be a duplicate of itself; that would make the
        # duplicate chain a cycle and every traversal of it an infinite loop.
        CheckConstraint(
            "duplicate_of_id IS NULL OR duplicate_of_id <> id",
            name="ck_reports_not_self_duplicate",
        ),
        CheckConstraint(
            "latitude >= -90 AND latitude <= 90", name="ck_reports_latitude_range"
        ),
        CheckConstraint(
            "longitude >= -180 AND longitude <= 180", name="ck_reports_longitude_range"
        ),
        # The common list query is "recent reports, optionally filtered by
        # district and status", so the ordering column is indexed too.
        Index("ix_reports_created_at", "created_at"),
        Index("ix_reports_district_status", "district_id", "status"),
        # Spatial index for future "reports near here" queries; PostgreSQL only.
        Index("ix_reports_location", "location", postgresql_using="gist"),
    )

    def to_geojson_point(self) -> dict[str, Any]:
        """GeoJSON Point. Note the order: longitude first."""
        return {"type": "Point", "coordinates": [self.longitude, self.latitude]}

    def to_dict(self, include_reporter: bool = True) -> dict[str, Any]:
        """Explicit serialisation.

        The reporter is reduced to id and name: a report listing is public, and
        dumping the user object would leak an email address (and, if anyone
        later adds a field, worse).
        """
        payload: dict[str, Any] = {
            "id": str(self.id),
            "title": self.title,
            "description": self.description,
            "category": self.category.value,
            "status": self.status.value,
            "location": self.to_geojson_point(),
            "coordinates": {"latitude": self.latitude, "longitude": self.longitude},
            "district_id": str(self.district_id) if self.district_id else None,
            "district": self.district.name if self.district else None,
            "municipality_id": (
                str(self.municipality_id) if self.municipality_id else None
            ),
            "municipality": self.municipality.name if self.municipality else None,
            "incident_id": str(self.incident_id) if self.incident_id else None,
            "duplicate_of_id": (
                str(self.duplicate_of_id) if self.duplicate_of_id else None
            ),
            "is_duplicate": self.duplicate_of_id is not None,
            "ai_metadata": self.ai_metadata,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_reporter:
            payload["reporter"] = {
                "id": str(self.reporter_id),
                "full_name": self.reporter.full_name if self.reporter else None,
            }
        return payload

    def __repr__(self) -> str:
        return f"<Report {self.id} {self.category.value} {self.status.value}>"
