"""Incident model - a verified civic problem on the ground.

A Report is *what someone said they saw*. An Incident is *what a human with
authority confirmed is actually there*. The distinction is the whole point of
the verification step: reports are claims, incidents are findings.

One incident may gather many reports. Five citizens reporting the same burst
water main produce five reports and one incident, so the platform counts the
problem once while preserving every individual account of it.

Coordinates follow the Phase 5 dual strategy: authoritative ``latitude`` and
``longitude`` floats that work on any backend, plus a ``location`` PostGIS point
derived from them for spatial indexing. See ``app/models/report.py`` for why.

Phase 7 added ``authority_id``: who owns fixing this. Contractors, projects
and budgets remain out of scope (Phase 8).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..gis.types import DEFAULT_SRID, GeometryColumn
from .base import BaseModel, UtcDateTime
from .enums import IncidentSeverity, IncidentStatus, ReportCategory, enum_column

if TYPE_CHECKING:  # pragma: no cover
    from .authority import Authority
    from .project import Project
    from .district import District
    from .municipality import Municipality
    from .report import Report
    from .user import User

TITLE_MAX_LENGTH = 150


class Incident(BaseModel):
    __tablename__ = "incidents"

    title: Mapped[str] = mapped_column(String(TITLE_MAX_LENGTH), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    category: Mapped[ReportCategory] = mapped_column(
        enum_column(ReportCategory, "incident_category"), nullable=False, index=True
    )
    severity: Mapped[IncidentSeverity] = mapped_column(
        enum_column(IncidentSeverity, "incident_severity"),
        nullable=False,
        default=IncidentSeverity.MEDIUM,
        server_default=IncidentSeverity.MEDIUM.value,
        index=True,
    )
    status: Mapped[IncidentStatus] = mapped_column(
        enum_column(IncidentStatus, "incident_status"),
        nullable=False,
        default=IncidentStatus.OPEN,
        server_default=IncidentStatus.OPEN.value,
        index=True,
    )

    # Inherited from the report that was promoted; nullable for the same reason
    # reports are - reverse geocoding legitimately cannot always answer.
    district_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("districts.id", ondelete="RESTRICT"), index=True
    )
    municipality_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("municipalities.id", ondelete="RESTRICT"), index=True
    )

    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    location = mapped_column(GeometryColumn("POINT", DEFAULT_SRID), nullable=True)

    # Who owns fixing this. NULL until a human routes it; see Phase 7.
    # SET NULL on delete: removing an authority record must not destroy the
    # incident, which exists independently of who was assigned to it.
    authority_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "authorities.id", ondelete="SET NULL", name="fk_incidents_authority_id"
        ),
        index=True,
    )
    # Accountability for the routing decision itself, kept separate from the
    # verifier: confirming a problem and choosing its owner are different acts.
    assigned_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_incidents_assigned_by_id"),
        index=True,
    )
    assigned_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    # When the problem was actually declared fixed. Stamped by the service on
    # the transition into RESOLVED and cleared if it moves back out.
    #
    # This exists because `updated_at` cannot answer "how long did this take":
    # it moves whenever anything on the row changes, so a severity correction
    # months later would silently rewrite the resolution time. Without a column
    # of its own the metric would have to be fabricated.
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)

    # Who confirmed this was real. Kept because verification is an accountable
    # act; RESTRICT so that record cannot be erased by deleting the account.
    verified_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )

    district: Mapped["District | None"] = relationship(back_populates="incidents")
    municipality: Mapped["Municipality | None"] = relationship(
        back_populates="incidents"
    )
    verified_by: Mapped["User | None"] = relationship(
        foreign_keys=[verified_by_id], back_populates="verified_incidents"
    )
    authority: Mapped["Authority | None"] = relationship(back_populates="incidents")
    assigned_by: Mapped["User | None"] = relationship(
        foreign_keys=[assigned_by_id], back_populates="assigned_incidents"
    )

    # No cascade: unlinking is Phase 6's job, deletion is nobody's. Deleting an
    # incident that still holds reports is refused by the RESTRICT-free
    # nullable FK only if the application says so, so the service unlinks first.
    reports: Mapped[list["Report"]] = relationship(
        back_populates="incident", order_by="Report.created_at"
    )
    # SET NULL on the child side: a project outlives the incident record it
    # was raised from, so no cascade here.
    projects: Mapped[list["Project"]] = relationship(
        back_populates="incident", passive_deletes="all"
    )

    __table_args__ = (
        CheckConstraint(
            "latitude >= -90 AND latitude <= 90", name="ck_incidents_latitude_range"
        ),
        CheckConstraint(
            "longitude >= -180 AND longitude <= 180", name="ck_incidents_longitude_range"
        ),
        Index("ix_incidents_created_at", "created_at"),
        # The dashboard query is "open incidents in this district, worst first".
        Index("ix_incidents_district_status", "district_id", "status"),
        # Spatial index for clustering lookups; PostgreSQL only.
        Index("ix_incidents_location", "location", postgresql_using="gist"),
    )

    @property
    def report_count(self) -> int:
        return len(self.reports)

    def to_geojson_point(self) -> dict[str, Any]:
        """GeoJSON Point. Note the order: longitude first."""
        return {"type": "Point", "coordinates": [self.longitude, self.latitude]}

    def to_dict(self, include_reports: bool = False) -> dict[str, Any]:
        """Explicit serialisation.

        ``include_reports`` attaches a summary of each linked report - enough to
        show "5 citizens reported this" without dumping five full bodies, and
        without any reporter's email address.
        """
        payload: dict[str, Any] = {
            "id": str(self.id),
            "title": self.title,
            "description": self.description,
            "category": self.category.value,
            "severity": self.severity.value,
            "status": self.status.value,
            "location": self.to_geojson_point(),
            "coordinates": {"latitude": self.latitude, "longitude": self.longitude},
            "district_id": str(self.district_id) if self.district_id else None,
            "district": self.district.name if self.district else None,
            "municipality_id": (
                str(self.municipality_id) if self.municipality_id else None
            ),
            "municipality": self.municipality.name if self.municipality else None,
            "report_count": self.report_count,
            "authority_id": str(self.authority_id) if self.authority_id else None,
            "authority": self.authority.to_dict() if self.authority else None,
            "assigned_at": self.assigned_at.isoformat() if self.assigned_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "assigned_by": (
                {
                    "id": str(self.assigned_by_id),
                    "full_name": self.assigned_by.full_name if self.assigned_by else None,
                }
                if self.assigned_by_id
                else None
            ),
            "verified_by": (
                {
                    "id": str(self.verified_by_id),
                    "full_name": self.verified_by.full_name if self.verified_by else None,
                }
                if self.verified_by_id
                else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_reports:
            payload["reports"] = [
                {
                    "id": str(report.id),
                    "title": report.title,
                    "status": report.status.value,
                    "category": report.category.value,
                    "coordinates": {
                        "latitude": report.latitude,
                        "longitude": report.longitude,
                    },
                    "created_at": (
                        report.created_at.isoformat() if report.created_at else None
                    ),
                }
                for report in self.reports
            ]
        return payload

    def __repr__(self) -> str:
        return f"<Incident {self.id} {self.severity.value} {self.status.value}>"
