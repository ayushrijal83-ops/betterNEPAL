"""Disaster Incident service - orchestrates AI triage, jurisdiction resolution, and dispatch."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from flask import current_app

from ..extensions import db
from ..models.disaster_incident import DisasterIncident, DisasterDispatch, DisasterIncidentStatus
from ..models.enums import DisasterType, DisasterSeverity
from ..models.report import Report
from ..models.user import User
from ..services import geolocation_service
from ..services.ai_dispatch import evaluate_disaster_threat
from ..services.disaster_policy import evaluate_dispatch_policy
from ..services.jurisdiction import (
    resolve_jurisdiction,
    find_authorities_for_jurisdiction,
    find_authority_users_for_jurisdiction,
)
from ..services.notification_service import notify_authorities_for_incident
from ..utils.helpers import ApiError


def _set_point(record, lat: float, lng: float) -> None:
    """Populate PostGIS point from floats. No-op off PostgreSQL."""
    if not geolocation_service.spatial_backend_available():
        return
    record.location = func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326)


def _haversine_metres(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance for correlation fallback."""
    import math
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(math.sqrt(a))


def _find_nearby_active_incidents(
    latitude: float,
    longitude: float,
    radius_metres: float,
    window_hours: int,
) -> list[DisasterIncident]:
    """Find active disaster incidents near a point within time window."""
    from ..models.base import utcnow
    from datetime import timedelta

    cutoff = utcnow() - timedelta(hours=window_hours)

    statement = select(DisasterIncident).where(
        DisasterIncident.status.in_([
            DisasterIncidentStatus.DETECTED,
            DisasterIncidentStatus.TRIAGED,
            DisasterIncidentStatus.DISPATCHED,
            DisasterIncidentStatus.ACKNOWLEDGED,
            DisasterIncidentStatus.RESPONDING,
        ]),
        DisasterIncident.created_at >= cutoff,
    )

    if geolocation_service.spatial_backend_available():
        from geoalchemy2 import Geography
        from sqlalchemy import cast

        point = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326)
        statement = statement.where(
            DisasterIncident.location.isnot(None),
            func.ST_DWithin(
                cast(DisasterIncident.location, Geography),
                cast(point, Geography),
                radius_metres,
            ),
        )
    else:
        # Bounding box prefilter
        lat_delta = radius_metres / 111_320.0
        import math
        cos_lat = math.cos(math.radians(latitude))
        if abs(cos_lat) < 1e-9:
            lng_delta = 180.0
        else:
            lng_delta = min(180.0, radius_metres / (111_320.0 * abs(cos_lat)))

        statement = statement.where(
            DisasterIncident.latitude.between(latitude - lat_delta, latitude + lat_delta),
            DisasterIncident.longitude.between(longitude - lng_delta, longitude + lng_delta),
        )

    candidates = db.session.scalars(statement).all()

    # Refine with haversine if using bounding box
    if not geolocation_service.spatial_backend_available():
        refined = []
        for inc in candidates:
            dist = _haversine_metres(latitude, longitude, inc.latitude, inc.longitude)
            if dist <= radius_metres:
                refined.append(inc)
        return refined

    return candidates


def _correlate_with_existing(
    latitude: float,
    longitude: float,
    disaster_type: str | None,
) -> DisasterIncident | None:
    """Check if this report correlates with an existing active disaster incident."""
    config = db.session.get_bind().engine.url if hasattr(db.session, 'get_bind') else None

    from flask import current_app
    radius = current_app.config.get("AI_DISPATCH_CORRELATION_RADIUS_METRES", 200)
    window = current_app.config.get("AI_DISPATCH_CORRELATION_WINDOW_HOURS", 24)

    nearby = _find_nearby_active_incidents(latitude, longitude, radius, window)

    if not nearby:
        return None

    # Prefer same disaster type
    for inc in nearby:
        if disaster_type and inc.disaster_type.value == disaster_type:
            return inc

    # Otherwise return the closest
    return min(
        nearby,
        key=lambda inc: _haversine_metres(latitude, longitude, inc.latitude, inc.longitude),
    )


def create_disaster_incident_from_report(
    report_id: Any,
    analysis,
    jurisdiction: dict[str, Any],
    dispatch_decision,
    triggered_by_user_id: Any = None,
) -> DisasterIncident:
    """Create a new DisasterIncident from a report and AI analysis.

    Idempotent on ``source_report_id``: a second call for the same report
    returns the existing incident before anything below it runs, so a retried
    evaluation can never double-notify authorities or double-post to the feed.
    """
    report = db.session.get(Report, uuid.UUID(str(report_id)))
    if not report:
        raise ApiError("Report not found.", status=404, code="report_not_found")

    # Check if report already linked to a disaster incident
    existing = db.session.scalar(
        select(DisasterIncident).where(DisasterIncident.source_report_id == report.id)
    )
    if existing:
        return existing

    incident = DisasterIncident(
        title=report.title[:150],
        description=report.description,
        disaster_type=DisasterType(analysis.disaster_type) if analysis.disaster_type else DisasterType.OTHER,
        severity=DisasterSeverity(analysis.severity) if analysis.severity else DisasterSeverity.MODERATE,
        status=DisasterIncidentStatus.DETECTED,
        district_id=jurisdiction.get("district_id"),
        municipality_id=jurisdiction.get("municipality_id"),
        latitude=report.latitude,
        longitude=report.longitude,
        ai_confidence=analysis.confidence,
        ai_reason=analysis.reason,
        ai_evidence={"evidence": analysis.evidence} if analysis.evidence else None,
        ai_analyzed_at=analysis.ai_analyzed_at if hasattr(analysis, "ai_analyzed_at") and analysis.ai_analyzed_at else datetime.utcnow(),
        ai_provider=analysis.model,
        source_report_id=report.id,
    )
    _set_point(incident, report.latitude, report.longitude)

    db.session.add(incident)
    db.session.flush()

    # If policy says dispatch, create dispatch records and notify
    if dispatch_decision.should_dispatch:
        incident.status = DisasterIncidentStatus.DISPATCHED
        incident.assigned_at = datetime.utcnow()

        # Find authorities for this jurisdiction
        district_id = jurisdiction.get("district_id")
        authority_users = find_authority_users_for_jurisdiction(district_id)

        # Notify authorities
        notification_result = notify_authorities_for_incident(incident, authority_users)

        # Log dispatch decision
        current_app.logger.info(
            "Disaster dispatch: incident=%s district=%s authorities_notified=%s failed=%s",
            incident.id,
            district_id,
            notification_result["notified"],
            notification_result["failed"],
        )

    if triggered_by_user_id is not None:
        _publish_disaster_feed_posts(incident, dispatch_decision, triggered_by_user_id)

    db.session.commit()
    return incident


# --- automatic feed publication -----------------------------------------------
#
# A separate object/workflow from the authority notification above, per the
# platform's own draft-gate design (see Announcement's docstring): AI/backend
# writes the wording, a human authorises publication. The wording itself is
# generated deterministically from fields already on the incident - no extra
# model call, and nothing here can invent a fact an LLM made up, because
# nothing here calls one.


def _feed_post_title(incident: DisasterIncident) -> str:
    label = incident.disaster_type.value.replace("_", " ").capitalize()
    return f"{label} reported near {incident.title}"[:200]


def _feed_post_body(incident: DisasterIncident, dispatched: bool) -> str:
    lines = [incident.ai_reason or incident.description[:300]]
    lines.append("")
    area = incident.district.name if incident.district else "an unresolved area"
    lines.append(f"Area: {area}")
    lines.append(f"Severity: {incident.severity.value.capitalize()}")
    lines.append(f"Status: {'Authorities notified' if dispatched else 'Under review'}")
    return "\n".join(lines)


def _publish_disaster_feed_posts(
    incident: DisasterIncident, dispatch_decision, author_id: Any
) -> None:
    """Local feed post always (published only once policy has actually
    dispatched; otherwise left as a draft for a human to clear); a second,
    national post only for the smaller set of CRITICAL, dispatched incidents -
    a real, fixed threshold, not an AI judgement call about what is
    "newsworthy".
    """
    from . import announcement_service

    if incident.district_id is None:
        # No resolved jurisdiction - nowhere honest to scope a local post to,
        # and not proven serious enough on its own to go out nationally.
        return

    published = bool(dispatch_decision.should_dispatch)
    announcement_service.create_announcement(
        author_id=author_id,
        title=_feed_post_title(incident),
        body=_feed_post_body(incident, dispatched=published),
        district_id=incident.district_id,
        is_draft=not published,
    )

    if published and incident.severity == DisasterSeverity.CRITICAL:
        announcement_service.create_announcement(
            author_id=author_id,
            title=f"Disaster Alert: {_feed_post_title(incident)}",
            body=(
                f"A {incident.severity.value}-severity {incident.disaster_type.value.replace('_', ' ')} "
                f"incident has been confirmed in {incident.district.name if incident.district else 'Nepal'}.\n\n"
                "Authorities have been notified and the incident is being handled through the "
                "Better Nepal response workflow. Follow official guidance and avoid affected areas."
            ),
            district_id=None,
            is_draft=False,
        )


def process_report_for_disaster(
    report_id: Any,
    user_id: Any | None = None,
) -> dict[str, Any]:
    """Main entry point: process a citizen report for disaster intelligence.

    1. Evaluate with AI
    2. Resolve jurisdiction
    3. Apply backend policy
    4. Create/correlate incident
    5. Dispatch if warranted

    Requires authority or admin role (enforced by caller, but verified here for defense in depth).
    """
    # Verify user has authority/admin role if user_id provided
    if user_id is not None:
        from ..models.user import User
        from ..models.role import Role
        from sqlalchemy import select as sa_select

        user = db.session.get(User, uuid.UUID(str(user_id)))
        if not user or not user.has_role("authority", "admin"):
            raise ApiError(
                "Only authority or admin users can trigger disaster evaluation.",
                status=403,
                code="insufficient_role",
            )

    report = db.session.get(Report, uuid.UUID(str(report_id)))
    if not report:
        raise ApiError("Report not found.", status=404, code="report_not_found")

    # 1. AI disaster triage
    analysis = evaluate_disaster_threat(
        title=report.title,
        description=report.description,
        latitude=report.latitude,
        longitude=report.longitude,
    )

    # 2. Resolve jurisdiction from coordinates
    jurisdiction = resolve_jurisdiction(report.latitude, report.longitude)

    # 3. Apply backend safety policy
    dispatch_decision = evaluate_dispatch_policy(
        analysis,
        latitude=report.latitude,
        longitude=report.longitude,
        district_id=jurisdiction.get("district_id"),
    )

    # 4. Check for correlation with existing incident
    correlated = None
    if analysis.is_disaster and analysis.disaster_type:
        correlated = _correlate_with_existing(
            report.latitude,
            report.longitude,
            analysis.disaster_type,
        )

    incident = None
    if correlated:
        # Attach report to existing incident
        if report.incident_id is None:  # Only if not already linked
            report.disaster_incidents.append(correlated)
            correlated.source_report_id = report.id
            db.session.commit()
        incident = correlated
        action = "correlated"
    elif dispatch_decision.should_dispatch or analysis.is_disaster:
        # Create new incident (even if not dispatching, track for review)
        incident = create_disaster_incident_from_report(
            report_id=report.id,
            analysis=analysis,
            jurisdiction=jurisdiction,
            dispatch_decision=dispatch_decision,
            triggered_by_user_id=user_id,
        )
        action = "created"
    else:
        action = "none"

    return {
        "report_id": str(report.id),
        "action": action,
        "incident_id": str(incident.id) if incident else None,
        "analysis": analysis.to_dict(),
        "jurisdiction": {
            "district_id": str(jurisdiction["district_id"]) if jurisdiction.get("district_id") else None,
            "district": jurisdiction.get("district_name"),
            "municipality_id": str(jurisdiction["municipality_id"]) if jurisdiction.get("municipality_id") else None,
            "municipality": jurisdiction.get("municipality_name"),
            "resolved": jurisdiction.get("resolved"),
            "reason": jurisdiction.get("reason"),
        },
        "policy_decision": {
            "should_dispatch": dispatch_decision.should_dispatch,
            "reason": dispatch_decision.reason,
            "details": dispatch_decision.policy_details,
        },
        "correlated": correlated is not None,
    }


def get_disaster_incident_by_id(incident_id: Any) -> DisasterIncident:
    incident = db.session.scalar(
        select(DisasterIncident)
        .options(
            selectinload(DisasterIncident.district),
            selectinload(DisasterIncident.municipality),
            selectinload(DisasterIncident.source_report),
            selectinload(DisasterIncident.authority),
            selectinload(DisasterIncident.assigned_by),
            selectinload(DisasterIncident.dispatches).selectinload(DisasterDispatch.authority_user),
        )
        .where(DisasterIncident.id == uuid.UUID(str(incident_id)))
    )
    if not incident:
        raise ApiError("Disaster incident not found.", status=404, code="disaster_incident_not_found")
    return incident


def get_disaster_incidents(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """List disaster incidents with filtering and paging."""
    from ..models.enums import enum_values, parse_enum

    filters = filters or {}
    statement = select(DisasterIncident).options(
        selectinload(DisasterIncident.district),
        selectinload(DisasterIncident.municipality),
        selectinload(DisasterIncident.authority),
    )
    count_statement = select(func.count()).select_from(DisasterIncident)

    def _enum_filter(key: str, enum_class, column):
        nonlocal statement, count_statement
        raw = filters.get(key)
        if raw in (None, ""):
            return
        member = parse_enum(enum_class, raw)
        if member is None:
            raise ApiError(
                f"{key} must be one of: {', '.join(enum_values(enum_class))}.",
                status=400,
                code="invalid_filter",
            )
        statement = statement.where(column == member)
        count_statement = count_statement.where(column == member)

    _enum_filter("status", DisasterIncidentStatus, DisasterIncident.status)
    _enum_filter("disaster_type", DisasterType, DisasterIncident.disaster_type)
    _enum_filter("severity", DisasterSeverity, DisasterIncident.severity)

    for key, column in (
        ("district_id", DisasterIncident.district_id),
        ("municipality_id", DisasterIncident.municipality_id),
        ("authority_id", DisasterIncident.authority_id),
    ):
        value = filters.get(key)
        if value:
            parsed = uuid.UUID(str(value))
            statement = statement.where(column == parsed)
            count_statement = count_statement.where(column == parsed)

    page = max(1, int(filters.get("page") or 1))
    per_page = min(100, max(1, int(filters.get("per_page") or 20)))

    total = db.session.scalar(count_statement) or 0
    records = db.session.scalars(
        statement.order_by(DisasterIncident.created_at.desc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    ).all()

    return {
        "incidents": [inc.to_public_dict() for inc in records],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page if total else 0,
        },
    }


def acknowledge_disaster_incident(incident_id: Any, user_id: Any) -> DisasterIncident:
    """Authority acknowledges a dispatched incident.

    Only the authority user who was dispatched can acknowledge.
    """
    incident = get_disaster_incident_by_id(incident_id)

    if incident.status != DisasterIncidentStatus.DISPATCHED:
        raise ApiError(
            f"Incident must be in DISPATCHED status to acknowledge, currently {incident.status.value}.",
            status=409,
            code="invalid_status_transition",
        )

    # Verify this user was dispatched for this incident
    dispatched = False
    for dispatch in incident.dispatches:
        if dispatch.authority_user_id == uuid.UUID(str(user_id)) and dispatch.status == "delivered":
            dispatched = True
            break

    if not dispatched:
        raise ApiError(
            "You are not authorized to acknowledge this incident.",
            status=403,
            code="not_authorized_for_incident",
        )

    incident.status = DisasterIncidentStatus.ACKNOWLEDGED
    incident.acknowledged_at = datetime.utcnow()

    # Update dispatch records for this user
    for dispatch in incident.dispatches:
        if dispatch.authority_user_id == uuid.UUID(str(user_id)) and dispatch.status == "delivered":
            dispatch.status = "acknowledged"
            dispatch.acknowledged_at = datetime.utcnow()

    db.session.commit()
    return incident


def update_disaster_incident_status(
    incident_id: Any,
    status: DisasterIncidentStatus | None = None,
    severity: DisasterSeverity | None = None,
    user_id: Any | None = None,
) -> DisasterIncident:
    """Update incident status and/or severity with validation.

    Only authority/admin users can update incident status.
    """
    # Verify user has authority/admin role
    if user_id is not None:
        from ..models.user import User
        from ..models.role import Role
        from sqlalchemy import select as sa_select

        user = db.session.get(User, uuid.UUID(str(user_id)))
        if not user or not user.has_role("authority", "admin"):
            raise ApiError(
                "Only authority or admin users can update incident status.",
                status=403,
                code="insufficient_role",
            )

    incident = get_disaster_incident_by_id(incident_id)

    # For ACKNOWLEDGED status, verify user was dispatched
    if status == DisasterIncidentStatus.ACKNOWLEDGED and user_id is not None:
        dispatched = False
        for dispatch in incident.dispatches:
            if dispatch.authority_user_id == uuid.UUID(str(user_id)) and dispatch.status == "delivered":
                dispatched = True
                break
        if not dispatched:
            raise ApiError(
                "You are not authorized to acknowledge this incident.",
                status=403,
                code="not_authorized_for_incident",
            )

    # Valid transitions
    allowed = {
        DisasterIncidentStatus.DETECTED: (DisasterIncidentStatus.TRIAGED, DisasterIncidentStatus.DISPATCHED, DisasterIncidentStatus.FALSE_ALARM),
        DisasterIncidentStatus.TRIAGED: (DisasterIncidentStatus.DISPATCHED, DisasterIncidentStatus.FALSE_ALARM),
        DisasterIncidentStatus.DISPATCHED: (DisasterIncidentStatus.ACKNOWLEDGED, DisasterIncidentStatus.FALSE_ALARM),
        DisasterIncidentStatus.ACKNOWLEDGED: (DisasterIncidentStatus.RESPONDING, DisasterIncidentStatus.FALSE_ALARM),
        DisasterIncidentStatus.RESPONDING: (DisasterIncidentStatus.RESOLVED, DisasterIncidentStatus.FALSE_ALARM),
        DisasterIncidentStatus.RESOLVED: (),
        DisasterIncidentStatus.FALSE_ALARM: (),
    }

    if status is not None and status != incident.status:
        if status not in allowed.get(incident.status, ()):
            raise ApiError(
                f"Cannot transition from {incident.status.value} to {status.value}.",
                status=409,
                code="invalid_status_transition",
            )
        incident.status = status

    if severity is not None and severity != incident.severity:
        incident.severity = severity

    if status == DisasterIncidentStatus.RESOLVED:
        incident.resolved_at = datetime.utcnow()
    elif status == DisasterIncidentStatus.DISPATCHED and incident.assigned_at is None:
        incident.assigned_at = datetime.utcnow()
    if user_id and status in (DisasterIncidentStatus.DISPATCHED, DisasterIncidentStatus.ACKNOWLEDGED):
        incident.assigned_by_id = uuid.UUID(str(user_id))

    db.session.commit()
    return incident


def get_disaster_incident_statistics() -> dict[str, Any]:
    """Aggregate counts for dashboard."""
    from ..models.enums import DisasterType

    by_status = dict(
        db.session.execute(
            select(DisasterIncident.status, func.count()).group_by(DisasterIncident.status)
        ).all()
    )
    by_severity = dict(
        db.session.execute(
            select(DisasterIncident.severity, func.count()).group_by(DisasterIncident.severity)
        ).all()
    )
    by_type = dict(
        db.session.execute(
            select(DisasterIncident.disaster_type, func.count()).group_by(DisasterIncident.disaster_type)
        ).all()
    )

    return {
        "total": db.session.scalar(select(func.count()).select_from(DisasterIncident)) or 0,
        "by_status": {s.value: by_status.get(s, 0) for s in DisasterIncidentStatus},
        "by_severity": {s.value: by_severity.get(s, 0) for s in DisasterSeverity},
        "by_type": {t.value: by_type.get(t, 0) for t in DisasterType},
        "active": sum(
            by_status.get(s, 0)
            for s in (
                DisasterIncidentStatus.DETECTED,
                DisasterIncidentStatus.TRIAGED,
                DisasterIncidentStatus.DISPATCHED,
                DisasterIncidentStatus.ACKNOWLEDGED,
                DisasterIncidentStatus.RESPONDING,
            )
        ),
        "resolved": by_status.get(DisasterIncidentStatus.RESOLVED, 0),
        "false_alarms": by_status.get(DisasterIncidentStatus.FALSE_ALARM, 0),
    }