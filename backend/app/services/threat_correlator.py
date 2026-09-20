"""Threat Correlator - groups signals into threat assessments."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from flask import current_app

from ..extensions import db
from ..models.threat import ThreatAssessment, ThreatSignal, ThreatStatus
from ..models.enums import SignalReliability, SignalSourceType
from ..services import geolocation_service
from ..services.disaster_incident_service import _haversine_metres


# Correlation defaults
DEFAULT_CORRELATION_RADIUS_METRES = 5000
DEFAULT_CORRELATION_WINDOW_HOURS = 24
MAX_SIGNALS_PER_ASSESSMENT = 50


def _get_config(key: str, default: Any) -> Any:
    """Get config value with fallback."""
    return current_app.config.get(key, default)


def _get_correlation_radius() -> int:
    return _get_config("THREAT_CORRELATION_RADIUS_METRES", DEFAULT_CORRELATION_RADIUS_METRES)


def _get_correlation_window() -> int:
    return _get_config("THREAT_CORRELATION_WINDOW_HOURS", DEFAULT_CORRELATION_WINDOW_HOURS)


def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    return _haversine_metres(lat1, lng1, lat2, lng2)


def _signals_match(signal: ThreatSignal, assessment: ThreatAssessment) -> bool:
    """Check if a signal matches an existing assessment."""
    # Same hazard type
    if signal.hazard_type and assessment.hazard_type:
        if signal.hazard_type.lower() != assessment.hazard_type.lower():
            return False

    # Geographic proximity
    dist = _haversine(
        signal.latitude,
        signal.longitude,
        assessment.latitude,
        assessment.longitude,
    )
    if dist > _get_correlation_radius() + (assessment.affected_radius_km * 1000):
        return False

    # Temporal proximity
    window_hours = _get_correlation_window()
    cutoff = datetime.utcnow() - timedelta(hours=window_hours)
    if signal.received_at < cutoff:
        return False

    return True


def _calculate_source_independence(signals: list[ThreatSignal]) -> dict[str, int]:
    """Calculate independent source count vs duplicates.

    Signals from the same source_type with very similar text/location/time
    are considered duplicates.
    """
    if not signals:
        return {"total": 0, "independent": 0, "duplicates": 0}

    # Group by source_type
    by_source: dict[str, list[ThreatSignal]] = {}
    for s in signals:
        by_source.setdefault(s.source_type.value, []).append(s)

    total = len(signals)
    independent = 0
    duplicates = 0

    for source_type, group in by_source.items():
        if len(group) == 1:
            independent += 1
            continue

        # For citizen reports, check text similarity
        if source_type == SignalSourceType.CITIZEN_REPORT.value:
            unique_texts = set()
            for s in group:
                text = (s.raw_text or "").strip().lower()[:200]
                if text and text not in unique_texts:
                    unique_texts.add(text)
                    independent += 1
                else:
                    duplicates += 1
        else:
            # For other sources, each is independent unless same source_id
            seen_ids = set()
            for s in group:
                sid = s.source_id
                if sid and sid in seen_ids:
                    duplicates += 1
                else:
                    if sid:
                        seen_ids.add(sid)
                    independent += 1

    return {"total": total, "independent": independent, "duplicates": duplicates}


def _determine_severity(signals: list[ThreatSignal], assessment: ThreatAssessment) -> str:
    """Determine severity from signals.

    Priority: CRITICAL > HIGH > MODERATE > LOW
    """
    severities = []
    for s in signals:
        if s.severity_hint:
            severities.append(s.severity_hint.upper())

    # Also check assessment's current severity
    if assessment.severity:
        severities.append(assessment.severity.upper())

    order = {"LOW": 0, "MODERATE": 1, "HIGH": 2, "CRITICAL": 3}
    max_sev = "MODERATE"
    max_val = 1
    for sev in severities:
        val = order.get(sev, 1)
        if val > max_val:
            max_val = val
            max_sev = sev
    return max_sev


def _calculate_threat_confidence(
    signals: list[ThreatSignal],
    independent_count: int,
    duplicate_count: int,
    has_verified_source: bool,
    has_sensor: bool,
    hours_span: float,
) -> tuple[float, dict]:
    """Calculate deterministic threat confidence with explanation.

    Base factors:
    - Base evidence: number of signals
    - Independent confirmation: independent sources
    - Verified source bonus
    - Sensor confirmation bonus
    - Temporal consistency
    - Geographic consistency
    - Penalties: duplicates, stale evidence
    """
    basis = {
        "base_evidence": len(signals),
        "independent_sources": independent_count,
        "duplicates": duplicate_count,
        "has_verified_source": has_verified_source,
        "has_sensor": has_sensor,
        "temporal_span_hours": round(hours_span, 2),
    }

    score = 0.0

    # Base evidence (0.1 per signal, max 0.5)
    score += min(0.5, len(signals) * 0.1)

    # Independent sources (0.15 per independent, max 0.45)
    score += min(0.45, independent_count * 0.15)

    # Verified source bonus
    if has_verified_source:
        score += 0.2

    # Sensor confirmation
    if has_sensor:
        score += 0.15

    # Temporal consistency (signals close in time)
    if hours_span <= 6:
        score += 0.1
    elif hours_span <= 24:
        score += 0.05

    # Geographic consistency (all signals close to each other)
    if len(signals) >= 2:
        avg_dist = 0.0
        count = 0
        for i, s1 in enumerate(signals):
            for s2 in signals[i + 1 :]:
                avg_dist += _haversine(s1.latitude, s1.longitude, s2.latitude, s2.longitude)
                count += 1
        if count > 0:
            avg_dist /= count
            if avg_dist <= 2000:
                score += 0.1
            elif avg_dist <= 5000:
                score += 0.05

    # Penalties
    if duplicate_count > 0:
        penalty = min(0.3, duplicate_count * 0.05)
        score -= penalty
        basis["duplicate_penalty"] = round(penalty, 2)

    # Stale evidence
    if hours_span > 48:
        score -= 0.2
        basis["stale_penalty"] = 0.2

    confidence = max(0.0, min(1.0, round(score, 2)))
    basis["raw_score"] = round(score, 2)
    basis["final_confidence"] = confidence

    return confidence, basis


def correlate_signal(signal: ThreatSignal) -> ThreatAssessment | None:
    """Try to correlate a new signal with existing assessments.

    Returns the matched assessment, or None if no match.
    """
    # Find active assessments in the same district/hazard area
    statement = select(ThreatAssessment).where(
        ThreatAssessment.status.in_(
            [
                ThreatStatus.OBSERVING,
                ThreatStatus.POSSIBLE,
                ThreatStatus.CORROBORATED,
                ThreatStatus.WARNING,
                ThreatStatus.CRITICAL,
            ]
        ),
        ThreatAssessment.district_id == signal.district_id,
    )

    if signal.hazard_type:
        statement = statement.where(ThreatAssessment.hazard_type == signal.hazard_type)

    candidates = db.session.scalars(statement).all()

    for assessment in candidates:
        if _signals_match(signal, assessment):
            # Attach signal to assessment
            signal.threat_assessment_id = assessment.id
            signal.verification_status = "verified"
            assessment.signals.append(signal)

            # Recalculate assessment
            _recalculate_assessment(assessment)
            return assessment

    return None


def _recalculate_assessment(assessment: ThreatAssessment) -> None:
    """Recalculate assessment fields from its signals."""
    signals = assessment.signals
    if not signals:
        return

    # Update counts
    source_info = _calculate_source_independence(signals)
    assessment.evidence_count = source_info["total"]
    assessment.independent_evidence_count = source_info["independent"]
    assessment.duplicate_count = source_info["duplicates"]

    # Severity
    assessment.severity = _determine_severity(signals, assessment)

    # Source reliability (highest)
    reliability_order = {
        SignalReliability.VERIFIED_AUTHORITY: 5,
        SignalReliability.VERIFIED_SENSOR: 4,
        SignalReliability.MULTI_SOURCE_CORROBORATED: 3,
        SignalReliability.TRUSTED_REPORT: 2,
        SignalReliability.UNVERIFIED_CITIZEN_REPORT: 1,
        SignalReliability.UNKNOWN: 0,
    }
    assessment.source_reliability = max(
        signals, key=lambda s: reliability_order.get(s.source_reliability, 0)
    ).source_reliability

    # Check for verified sources and sensors
    has_verified = any(
        s.source_reliability in (SignalReliability.VERIFIED_AUTHORITY, SignalReliability.VERIFIED_SENSOR)
        for s in signals
    )
    has_sensor = any(s.source_type == SignalSourceType.SENSOR for s in signals)

    # Temporal span
    times = [s.received_at for s in signals]
    hours_span = (max(times) - min(times)).total_seconds() / 3600 if len(times) > 1 else 0

    # Confidence
    confidence, basis = _calculate_threat_confidence(
        signals,
        source_info["independent"],
        source_info["duplicates"],
        has_verified,
        has_sensor,
        hours_span,
    )
    assessment.threat_confidence = confidence
    assessment.confidence_basis = basis

    # Update location to centroid
    assessment.latitude = sum(s.latitude for s in signals) / len(signals)
    assessment.longitude = sum(s.longitude for s in signals) / len(signals)

    # Update status based on confidence and severity
    _update_assessment_status(assessment)

    # Expiry
    assessment.expires_at = datetime.utcnow() + timedelta(hours=_get_correlation_window())


def _update_assessment_status(assessment: ThreatAssessment) -> None:
    """Update status based on confidence and severity."""
    confidence = assessment.threat_confidence
    severity = assessment.severity.upper()
    independent = assessment.independent_evidence_count

    if confidence >= 0.9 and severity in ("HIGH", "CRITICAL") and independent >= 2:
        assessment.status = ThreatStatus.CRITICAL
    elif confidence >= 0.8 and severity in ("HIGH", "CRITICAL") and independent >= 2:
        assessment.status = ThreatStatus.WARNING
    elif confidence >= 0.7 and independent >= 2:
        assessment.status = ThreatStatus.CORROBORATED
    elif confidence >= 0.5 and independent >= 1:
        assessment.status = ThreatStatus.POSSIBLE
    else:
        assessment.status = ThreatStatus.OBSERVING


def create_assessment_from_signal(signal: ThreatSignal) -> ThreatAssessment:
    """Create a new threat assessment from a single signal."""
    assessment = ThreatAssessment(
        hazard_type=signal.hazard_type or "unknown",
        status=ThreatStatus.OBSERVING,
        latitude=signal.latitude,
        longitude=signal.longitude,
        district_id=signal.district_id,
        municipality_id=signal.municipality_id,
        severity=signal.severity_hint or "MODERATE",
        threat_confidence=0.0,
        evidence_count=1,
        independent_evidence_count=1,
        duplicate_count=0,
        source_reliability=signal.source_reliability,
        expires_at=datetime.utcnow() + timedelta(hours=_get_correlation_window()),
    )
    db.session.add(assessment)
    db.session.flush()

    signal.threat_assessment_id = assessment.id
    signal.verification_status = "verified"
    assessment.signals.append(signal)

    _recalculate_assessment(assessment)
    return assessment


def process_new_signal(signal: ThreatSignal) -> ThreatAssessment:
    """Main entry point: process a new signal through correlation."""
    # Try to correlate with existing assessment
    assessment = correlate_signal(signal)
    if assessment:
        return assessment

    # Create new assessment
    return create_assessment_from_signal(signal)


def get_active_assessments(
    district_id: UUID | None = None,
    hazard_type: str | None = None,
    min_status: ThreatStatus | None = None,
) -> list[ThreatAssessment]:
    """Get active threat assessments with optional filters."""
    statement = select(ThreatAssessment).where(
        ThreatAssessment.status.in_(
            [
                ThreatStatus.OBSERVING,
                ThreatStatus.POSSIBLE,
                ThreatStatus.CORROBORATED,
                ThreatStatus.WARNING,
                ThreatStatus.CRITICAL,
            ]
        )
    )

    if district_id:
        statement = statement.where(ThreatAssessment.district_id == district_id)
    if hazard_type:
        statement = statement.where(ThreatAssessment.hazard_type == hazard_type)
    if min_status:
        # Order: OBSERVING < POSSIBLE < CORROBORATED < WARNING < CRITICAL
        order_map = {
            ThreatStatus.OBSERVING: 0,
            ThreatStatus.POSSIBLE: 1,
            ThreatStatus.CORROBORATED: 2,
            ThreatStatus.WARNING: 3,
            ThreatStatus.CRITICAL: 4,
        }
        min_val = order_map[min_status]
        statement = statement.where(
            ThreatAssessment.status.in_(
                [s for s, v in order_map.items() if v >= min_val]
            )
        )

    statement = statement.order_by(ThreatAssessment.threat_confidence.desc())
    return list(db.session.scalars(statement).all())


def expire_stale_assessments() -> int:
    """Mark expired assessments as EXPIRED.

    Returns count of assessments expired.
    """
    now = datetime.utcnow()
    statement = select(ThreatAssessment).where(
        ThreatAssessment.status.in_(
            [
                ThreatStatus.OBSERVING,
                ThreatStatus.POSSIBLE,
                ThreatStatus.CORROBORATED,
                ThreatStatus.WARNING,
                ThreatStatus.CRITICAL,
            ]
        ),
        ThreatAssessment.expires_at.isnot(None),
        ThreatAssessment.expires_at < now,
    )

    expired = db.session.scalars(statement).all()
    for assessment in expired:
        assessment.status = ThreatStatus.EXPIRED

    if expired:
        db.session.commit()

    return len(expired)