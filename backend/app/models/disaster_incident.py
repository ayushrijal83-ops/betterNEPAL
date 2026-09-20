"""Disaster Incident model - a verified disaster/hazard event requiring immediate response.

A DisasterIncident represents a classified disaster event (landslide, flood, earthquake, etc.)
that has been triaged by AI and validated by backend policy. It is distinct from the general
Incident model which covers all verified civic problems.

DisasterIncidents carry:
- AI classification (type, severity, confidence)
- Jurisdiction (district) for authority routing
- Dispatch status and audit trail
- Real-time tracking via WebSocket
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, String, Text, JSON
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..gis.types import DEFAULT_SRID, GeometryColumn
from .base import BaseModel, UtcDateTime
from .enums import (
    DisasterIncidentStatus,
    DisasterSeverity,
    DisasterType,
    ReportCategory,
    enum_column,
)

if TYPE_CHECKING:  # pragma: no cover
    from .authority import Authority
    from .district import District
    from .municipality import Municipality
    from .report import Report
    from .user import User

TITLE_MAX_LENGTH = 150


class DisasterIncident(BaseModel):
    __tablename__ = "disaster_incidents"

    # Human-readable title for dashboards and maps
    title: Mapped[str] = mapped_column(String(TITLE_MAX_LENGTH), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # What kind of disaster (from AI classification, validated by backend)
    disaster_type: Mapped[DisasterType] = mapped_column(
        enum_column(DisasterType, "disaster_incident_type"), nullable=False, index=True
    )
    severity: Mapped[DisasterSeverity] = mapped_column(
        enum_column(DisasterSeverity, "disaster_incident_severity"),
        nullable=False,
        default=DisasterSeverity.MODERATE,
        server_default=DisasterSeverity.MODERATE.value,
        index=True,
    )
    status: Mapped[DisasterIncidentStatus] = mapped_column(
        enum_column(DisasterIncidentStatus, "disaster_incident_status"),
        nullable=False,
        default=DisasterIncidentStatus.DETECTED,
        server_default=DisasterIncidentStatus.DETECTED.value,
        index=True,
    )

    # Jurisdiction (resolved from coordinates via reverse geocoding)
    district_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("districts.id", ondelete="RESTRICT"), index=True
    )
    municipality_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("municipalities.id", ondelete="RESTRICT"), index=True
    )

    # Authoritative coordinates
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    location = mapped_column(GeometryColumn("POINT", DEFAULT_SRID), nullable=True)

    # AI classification metadata (advisory only, never authoritative)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ai_analyzed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    ai_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Source report that triggered this incident
    source_report_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reports.id", ondelete="SET NULL", name="fk_disaster_incidents_source_report_id"),
        index=True,
    )

    # Authority assignment (who is responding)
    authority_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "authorities.id", ondelete="SET NULL", name="fk_disaster_incidents_authority_id"
        ),
        index=True,
    )
    assigned_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_disaster_incidents_assigned_by_id"),
        index=True,
    )
    assigned_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)

    # Relationships
    district: Mapped["District | None"] = relationship(back_populates="disaster_incidents")
    municipality: Mapped["Municipality | None"] = relationship(
        back_populates="disaster_incidents"
    )
    source_report: Mapped["Report | None"] = relationship(
        foreign_keys=[source_report_id], back_populates="disaster_incidents"
    )
    authority: Mapped["Authority | None"] = relationship(back_populates="disaster_incidents")
    assigned_by: Mapped["User | None"] = relationship(
        foreign_keys=[assigned_by_id], back_populates="assigned_disaster_incidents"
    )
    dispatches: Mapped[list["DisasterDispatch"]] = relationship(
        back_populates="disaster_incident", order_by="DisasterDispatch.created_at", passive_deletes="all"
    )

    __table_args__ = (
        CheckConstraint(
            "latitude >= -90 AND latitude <= 90", name="ck_disaster_incidents_latitude_range"
        ),
        CheckConstraint(
            "longitude >= -180 AND longitude <= 180", name="ck_disaster_incidents_longitude_range"
        ),
        CheckConstraint(
            "ai_confidence IS NULL OR (ai_confidence >= 0 AND ai_confidence <= 1)",
            name="ck_disaster_incidents_confidence_range",
        ),
        Index("ix_disaster_incidents_created_at", "created_at"),
        Index("ix_disaster_incidents_district_status", "district_id", "status"),
        Index("ix_disaster_incidents_location", "location", postgresql_using="gist"),
    )

    def to_geojson_point(self) -> dict[str, Any]:
        """GeoJSON Point. Note the order: longitude first."""
        return {"type": "Point", "coordinates": [self.longitude, self.latitude]}

    def to_public_dict(self) -> dict[str, Any]:
        """Safe serialisation for public map feed."""
        return {
            "id": str(self.id),
            "title": self.title,
            "description": self.description,
            "disaster_type": self.disaster_type.value,
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
            "ai_confidence": self.ai_confidence,
            "source_report_id": str(self.source_report_id) if self.source_report_id else None,
            "authority_id": str(self.authority_id) if self.authority_id else None,
            "authority": self.authority.to_dict() if self.authority else None,
            "assigned_at": self.assigned_at.isoformat() if self.assigned_at else None,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_authority_dict(self) -> dict[str, Any]:
        """Extended serialisation for authority dashboard."""
        base = self.to_public_dict()
        base.update({
            "ai_reason": self.ai_reason,
            "ai_evidence": self.ai_evidence,
            "ai_analyzed_at": self.ai_analyzed_at.isoformat() if self.ai_analyzed_at else None,
            "ai_provider": self.ai_provider,
            "assigned_by": (
                {
                    "id": str(self.assigned_by_id),
                    "full_name": self.assigned_by.full_name if self.assigned_by else None,
                }
                if self.assigned_by_id
                else None
            ),
            "dispatches": [
                {
                    "id": str(d.id),
                    "authority_user_id": str(d.authority_user_id),
                    "status": d.status,
                    "notified_at": d.notified_at.isoformat() if d.notified_at else None,
                    "acknowledged_at": d.acknowledged_at.isoformat() if d.acknowledged_at else None,
                    "channel": d.channel,
                }
                for d in self.dispatches
            ],
        })
        return base

    def __repr__(self) -> str:
        return f"<DisasterIncident {self.id} {self.disaster_type.value} {self.severity.value} {self.status.value}>"


class DisasterDispatch(BaseModel):
    """Record of a dispatch notification sent to an authority user.

    Creates an audit trail instead of simply firing notifications.
    """
    __tablename__ = "disaster_dispatches"

    disaster_incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("disaster_incidents.id", ondelete="CASCADE", name="fk_disaster_dispatches_incident_id"),
        nullable=False,
        index=True,
    )
    authority_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_disaster_dispatches_user_id"),
        nullable=False,
        index=True,
    )

    # Delivery status
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)  # websocket, email, sms, in_app
    notified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    failed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    # Relationships
    disaster_incident: Mapped["DisasterIncident"] = relationship(back_populates="dispatches")
    authority_user: Mapped["User"] = relationship(back_populates="disaster_dispatches")

    __table_args__ = (
        Index("ix_disaster_dispatches_incident_user", "disaster_incident_id", "authority_user_id"),
        Index("ix_disaster_dispatches_status", "status"),
        # Prevent duplicate dispatch records for same incident/user/channel
        sa.UniqueConstraint(
            "disaster_incident_id", "authority_user_id", "channel",
            name="uq_disaster_dispatches_incident_user_channel",
        ),
    )

    def __repr__(self) -> str:
        return f"<DisasterDispatch incident={self.disaster_incident_id} user={self.authority_user_id} status={self.status}>"