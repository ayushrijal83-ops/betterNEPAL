"""Threat signal models - raw signals that feed into correlation and assessment."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..gis.types import DEFAULT_SRID, GeometryColumn
from .base import BaseModel, UtcDateTime
from .enums import (
    SignalSourceType,
    SignalReliability,
    ThreatStatus,
    BroadcastAuthorizationState,
    BroadcastDeliveryStatus,
    enum_column,
)

if TYPE_CHECKING:  # pragma: no cover
    from .district import District
    from .municipality import Municipality
    from .report import Report
    from .incident import Incident
    from .disaster_incident import DisasterIncident
    from .user import User


class ThreatSignal(BaseModel):
    """A raw threat signal from any source.

    Signals are the atomic unit of the early warning system. They are NOT
    assessments - they are observations that may be correlated into a
    ThreatAssessment.
    """

    __tablename__ = "threat_signals"

    # Source identification
    source_type: Mapped[SignalSourceType] = mapped_column(
        enum_column(SignalSourceType, "threat_signal_source_type"),
        nullable=False,
        index=True,
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        String(36), index=True, nullable=True
    )  # ID in source system (report_id, incident_id, etc.)

    # Timing
    received_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=datetime.utcnow, index=True
    )
    occurred_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # Location
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    location = mapped_column(GeometryColumn("POINT", DEFAULT_SRID), nullable=True)

    # Jurisdiction (resolved from coordinates)
    district_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("districts.id", ondelete="SET NULL"), index=True, nullable=True
    )
    municipality_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("municipalities.id", ondelete="SET NULL"), index=True, nullable=True
    )

    # Hazard classification hint (from source)
    hazard_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    severity_hint: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Content
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Trust
    source_reliability: Mapped[SignalReliability] = mapped_column(
        enum_column(SignalReliability, "threat_signal_reliability"),
        nullable=False,
        default=SignalReliability.UNKNOWN,
        server_default=SignalReliability.UNKNOWN.value,
    )

    # Processing state
    verification_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default="pending",
    )  # pending, verified, rejected, duplicate

    # Correlation
    threat_assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("threat_assessments.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    # Relationships
    district: Mapped["District | None"] = relationship()
    municipality: Mapped["Municipality | None"] = relationship()
    threat_assessment: Mapped["ThreatAssessment | None"] = relationship(
        back_populates="signals"
    )

    __table_args__ = (
        CheckConstraint(
            "latitude >= -90 AND latitude <= 90", name="ck_threat_signals_latitude_range"
        ),
        CheckConstraint(
            "longitude >= -180 AND longitude <= 180",
            name="ck_threat_signals_longitude_range",
        ),
        Index("ix_threat_signals_created_at", "created_at"),
        Index("ix_threat_signals_district_source", "district_id", "source_type"),
        # received_at already gets ix_threat_signals_received_at from the
        # column's own index=True - an explicit duplicate here collides on
        # the same name when SQLAlchemy creates the schema directly (as the
        # test fixtures do), even though Alembic's autogenerate silently
        # coalesces it into one CREATE INDEX.
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "source_type": self.source_type.value,
            "source_id": str(self.source_id) if self.source_id else None,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "location": {
                "latitude": self.latitude,
                "longitude": self.longitude,
            },
            "district_id": str(self.district_id) if self.district_id else None,
            "district": self.district.name if self.district else None,
            "municipality_id": str(self.municipality_id) if self.municipality_id else None,
            "municipality": self.municipality.name if self.municipality else None,
            "hazard_type": self.hazard_type,
            "severity_hint": self.severity_hint,
            "source_reliability": self.source_reliability.value,
            "verification_status": self.verification_status,
            "threat_assessment_id": (
                str(self.threat_assessment_id) if self.threat_assessment_id else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ThreatAssessment(BaseModel):
    """A correlated threat assessment from multiple signals.

    This is the core object that represents a potential or confirmed threat.
    It is created by the ThreatCorrelator and evaluated by the DisasterPolicy.
    """

    __tablename__ = "threat_assessments"

    # Classification
    hazard_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[ThreatStatus] = mapped_column(
        enum_column(ThreatStatus, "threat_assessment_status"),
        nullable=False,
        default=ThreatStatus.OBSERVING,
        server_default=ThreatStatus.OBSERVING.value,
        index=True,
    )

    # Location (centroid of correlated signals)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    location = mapped_column(GeometryColumn("POINT", DEFAULT_SRID), nullable=True)

    affected_radius_km: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Jurisdiction
    district_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("districts.id", ondelete="SET NULL"), index=True, nullable=True
    )
    municipality_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("municipalities.id", ondelete="SET NULL"), index=True, nullable=True
    )

    # Severity and confidence
    severity: Mapped[str] = mapped_column(String(32), nullable=False, default="MODERATE")
    threat_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ai_classification_confidence: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )

    # Evidence tracking
    evidence_count: Mapped[int] = mapped_column(default=0)
    independent_evidence_count: Mapped[int] = mapped_column(default=0)
    duplicate_count: Mapped[int] = mapped_column(default=0)

    # Source reliability (highest among signals)
    source_reliability: Mapped[SignalReliability] = mapped_column(
        enum_column(SignalReliability, "threat_assessment_reliability"),
        nullable=False,
        default=SignalReliability.UNKNOWN,
        server_default=SignalReliability.UNKNOWN.value,
    )

    # Confidence basis (deterministic explanation)
    confidence_basis: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Timing
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # Broadcast linkage. use_alter breaks the create/drop-order cycle: this
    # table and social_broadcasts each hold a FK to the other
    # (this column, and SocialBroadcast.threat_assessment_id), so one of the
    # two constraints has to be added/dropped as a separate ALTER rather than
    # inline in CREATE TABLE.
    broadcast_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "social_broadcasts.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_threat_assessments_broadcast_id",
        ),
        index=True,
        nullable=True,
    )

    # Relationships
    signals: Mapped[list["ThreatSignal"]] = relationship(
        back_populates="threat_assessment", order_by="ThreatSignal.received_at"
    )
    district: Mapped["District | None"] = relationship()
    municipality: Mapped["Municipality | None"] = relationship()
    # Two independent FKs link these tables in opposite directions
    # (this assessment's currently-active broadcast vs. a broadcast's
    # originating assessment) - not one bidirectional relationship, so each
    # side names its own column rather than back_populates-ing the other.
    broadcast: Mapped["SocialBroadcast | None"] = relationship(
        foreign_keys=[broadcast_id]
    )

    __table_args__ = (
        CheckConstraint(
            "latitude >= -90 AND latitude <= 90", name="ck_threat_assessments_latitude_range"
        ),
        CheckConstraint(
            "longitude >= -180 AND longitude <= 180",
            name="ck_threat_assessments_longitude_range",
        ),
        CheckConstraint(
            "threat_confidence >= 0 AND threat_confidence <= 1",
            name="ck_threat_assessments_confidence_range",
        ),
        Index("ix_threat_assessments_created_at", "created_at"),
        Index("ix_threat_assessments_district_status", "district_id", "status"),
        Index("ix_threat_assessments_hazard_status", "hazard_type", "status"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "hazard_type": self.hazard_type,
            "status": self.status.value,
            "location": {
                "latitude": self.latitude,
                "longitude": self.longitude,
            },
            "affected_radius_km": self.affected_radius_km,
            "district_id": str(self.district_id) if self.district_id else None,
            "district": self.district.name if self.district else None,
            "municipality_id": str(self.municipality_id) if self.municipality_id else None,
            "municipality": self.municipality.name if self.municipality else None,
            "severity": self.severity,
            "threat_confidence": self.threat_confidence,
            "ai_classification_confidence": self.ai_classification_confidence,
            "evidence_count": self.evidence_count,
            "independent_evidence_count": self.independent_evidence_count,
            "duplicate_count": self.duplicate_count,
            "source_reliability": self.source_reliability.value,
            "confidence_basis": self.confidence_basis,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "broadcast_id": str(self.broadcast_id) if self.broadcast_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class SocialBroadcast(BaseModel):
    """An authorized emergency broadcast for public dissemination."""

    __tablename__ = "social_broadcasts"

    # Link to threat assessment
    threat_assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("threat_assessments.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    disaster_incident_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("disaster_incidents.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    # Jurisdiction
    district_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("districts.id", ondelete="SET NULL"), index=True, nullable=True
    )
    municipality_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("municipalities.id", ondelete="SET NULL"), index=True, nullable=True
    )

    # Content
    hazard_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)

    # Trilingual content
    title_en: Mapped[str] = mapped_column(Text, nullable=False)
    title_ne: Mapped[str] = mapped_column(Text, nullable=False)
    title_mai: Mapped[str] = mapped_column(Text, nullable=False)

    content_en: Mapped[str] = mapped_column(Text, nullable=False)
    content_ne: Mapped[str] = mapped_column(Text, nullable=False)
    content_mai: Mapped[str] = mapped_column(Text, nullable=False)

    action_en: Mapped[str] = mapped_column(Text, nullable=False)
    action_ne: Mapped[str] = mapped_column(Text, nullable=False)
    action_mai: Mapped[str] = mapped_column(Text, nullable=False)

    # Targeting
    platforms_targeted: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # Authorization
    authorization_state: Mapped[BroadcastAuthorizationState] = mapped_column(
        enum_column(BroadcastAuthorizationState, "social_broadcast_auth_state"),
        nullable=False,
        default=BroadcastAuthorizationState.PENDING_REVIEW,
        server_default=BroadcastAuthorizationState.PENDING_REVIEW.value,
        index=True,
    )
    authorized_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    authorized_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    authorization_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Dispatch
    dispatch_status: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    dispatch_status_summary: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )

    # AI metadata
    ai_classification_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    threat_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Immutable evidence snapshot at time of authorization
    evidence_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # External platform IDs
    external_post_ids: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Versioning
    parent_broadcast_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("social_broadcasts.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    version: Mapped[int] = mapped_column(default=1)

    # Timing
    approved_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # Relationships
    threat_assessment: Mapped["ThreatAssessment | None"] = relationship(
        foreign_keys=[threat_assessment_id]
    )
    disaster_incident: Mapped["DisasterIncident | None"] = relationship()
    district: Mapped["District | None"] = relationship()
    municipality: Mapped["Municipality | None"] = relationship()
    authorized_by: Mapped["User | None"] = relationship()
    deliveries: Mapped[list["BroadcastDelivery"]] = relationship(
        back_populates="broadcast", order_by="BroadcastDelivery.platform"
    )
    parent_broadcast: Mapped["SocialBroadcast | None"] = relationship(
        remote_side="SocialBroadcast.id", back_populates="versions"
    )
    versions: Mapped[list["SocialBroadcast"]] = relationship(
        back_populates="parent_broadcast", order_by="SocialBroadcast.version"
    )

    __table_args__ = (
        Index("ix_social_broadcasts_created_at", "created_at"),
        Index("ix_social_broadcasts_district_status", "district_id", "authorization_state"),
        Index("ix_social_broadcasts_hazard_severity", "hazard_type", "severity"),
    )

    def to_dict(self, include_deliveries: bool = False) -> dict[str, Any]:
        payload = {
            "id": str(self.id),
            "threat_assessment_id": (
                str(self.threat_assessment_id) if self.threat_assessment_id else None
            ),
            "disaster_incident_id": (
                str(self.disaster_incident_id) if self.disaster_incident_id else None
            ),
            "hazard_type": self.hazard_type,
            "severity": self.severity,
            "district_id": str(self.district_id) if self.district_id else None,
            "district": self.district.name if self.district else None,
            "municipality_id": str(self.municipality_id) if self.municipality_id else None,
            "municipality": self.municipality.name if self.municipality else None,
            "title_en": self.title_en,
            "title_ne": self.title_ne,
            "title_mai": self.title_mai,
            "content_en": self.content_en,
            "content_ne": self.content_ne,
            "content_mai": self.content_mai,
            "action_en": self.action_en,
            "action_ne": self.action_ne,
            "action_mai": self.action_mai,
            "platforms_targeted": self.platforms_targeted,
            "authorization_state": self.authorization_state.value,
            "dispatch_status": self.dispatch_status,
            "dispatch_status_summary": self.dispatch_status_summary,
            "ai_classification_confidence": self.ai_classification_confidence,
            "threat_confidence": self.threat_confidence,
            "evidence_snapshot": self.evidence_snapshot,
            "external_post_ids": self.external_post_ids,
            "version": self.version,
            "parent_broadcast_id": (
                str(self.parent_broadcast_id) if self.parent_broadcast_id else None
            ),
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_deliveries:
            payload["deliveries"] = [d.to_dict() for d in self.deliveries]
        return payload


class BroadcastDelivery(BaseModel):
    """Per-platform delivery record for a broadcast."""

    __tablename__ = "broadcast_deliveries"

    broadcast_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("social_broadcasts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    status: Mapped[BroadcastDeliveryStatus] = mapped_column(
        enum_column(BroadcastDeliveryStatus, "broadcast_delivery_status"),
        nullable=False,
        default=BroadcastDeliveryStatus.PENDING,
        server_default=BroadcastDeliveryStatus.PENDING.value,
    )

    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False, index=True)

    # Relationships
    broadcast: Mapped["SocialBroadcast"] = relationship(back_populates="deliveries")

    __table_args__ = (
        # One delivery per broadcast per platform
        sa.UniqueConstraint(
            "broadcast_id", "platform", name="uq_broadcast_deliveries_broadcast_platform"
        ),
        Index("ix_broadcast_deliveries_status", "status"),
        Index("ix_broadcast_deliveries_idempotency", "idempotency_key"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "broadcast_id": str(self.broadcast_id),
            "platform": self.platform,
            "status": self.status.value,
            "external_id": self.external_id,
            "attempt_count": self.attempt_count,
            "last_error": self.last_error,
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }