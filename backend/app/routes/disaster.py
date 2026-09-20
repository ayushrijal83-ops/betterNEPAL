"""Disaster Incident API endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime

from flask import Blueprint, request

from ..extensions import db
from ..models.authority import Authority
from ..models.role import ROLE_ADMIN, ROLE_AUTHORITY
from ..services import disaster_incident_service
from ..utils.decorators import require_roles
from ..utils.helpers import ApiError, success_response
from ..utils.permissions import current_user
from ..utils.validators import ValidationErrors, _as_text, require_json

disaster_bp = Blueprint("disaster", __name__, url_prefix="/disaster")


@disaster_bp.post("/reports/<report_id>/evaluate")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def evaluate_report(report_id: str):
    """Trigger AI disaster evaluation for a report.

    Authority/admin only - AI analysis costs resources.
    """
    result = disaster_incident_service.process_report_for_disaster(
        report_id, user_id=current_user().id
    )
    return success_response(result)


@disaster_bp.get("/incidents")
def list_disaster_incidents():
    """List disaster incidents. Public read access."""
    return success_response(
        disaster_incident_service.get_disaster_incidents(
            {
                "status": request.args.get("status"),
                "disaster_type": request.args.get("disaster_type"),
                "severity": request.args.get("severity"),
                "district_id": request.args.get("district_id"),
                "municipality_id": request.args.get("municipality_id"),
                "authority_id": request.args.get("authority_id"),
                "page": request.args.get("page"),
                "per_page": request.args.get("per_page"),
            }
        )
    )


@disaster_bp.get("/incidents/statistics")
def disaster_statistics():
    """Aggregate statistics. Public."""
    return success_response(disaster_incident_service.get_disaster_incident_statistics())


@disaster_bp.get("/incidents/<incident_id>")
def get_disaster_incident(incident_id: str):
    """Get a disaster incident. Public for basic info, authority for full."""
    incident = disaster_incident_service.get_disaster_incident_by_id(incident_id)
    user = current_user()
    if user and user.has_role("authority", "admin"):
        return success_response({"incident": incident.to_authority_dict()})
    return success_response({"incident": incident.to_public_dict()})


@disaster_bp.post("/incidents/<incident_id>/acknowledge")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def acknowledge_incident(incident_id: str):
    """Authority acknowledges a dispatched incident."""
    incident = disaster_incident_service.acknowledge_disaster_incident(incident_id, current_user().id)
    return success_response({"incident": incident.to_authority_dict()})


@disaster_bp.patch("/incidents/<incident_id>")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def update_disaster_incident_status(incident_id: str):
    """Update disaster incident status/severity."""
    from ..models.enums import DisasterIncidentStatus, DisasterSeverity, parse_enum

    data = require_json(request.get_json(silent=True))
    errors = ValidationErrors()

    status = None
    severity = None

    if "status" in data:
        status = parse_enum(DisasterIncidentStatus, _as_text(data, "status"))
        if status is None:
            errors.add("status", "Invalid status value.")

    if "severity" in data:
        severity = parse_enum(DisasterSeverity, _as_text(data, "severity"))
        if severity is None:
            errors.add("severity", "Invalid severity value.")

    errors.raise_if_any()

    incident = disaster_incident_service.update_disaster_incident_status(
        incident_id, status=status, severity=severity, user_id=current_user().id
    )
    return success_response({"incident": incident.to_authority_dict()})


@disaster_bp.post("/incidents/<incident_id>/assign")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def assign_disaster_incident(incident_id: str):
    """Assign a disaster incident to an authority.

    The authority must cover the incident's jurisdiction (district) or be a national authority.
    """
    from datetime import datetime
    import uuid
    from ..models.authority import Authority
    from ..models.enums import DisasterIncidentStatus

    data = require_json(request.get_json(silent=True))
    errors = ValidationErrors()

    authority_id_str = _as_text(data, "authority_id")
    if not authority_id_str:
        errors.add("authority_id", "authority_id is required.")
    else:
        try:
            authority_id = uuid.UUID(authority_id_str)
        except (ValueError, AttributeError, TypeError):
            errors.add("authority_id", "authority_id is not a valid identifier.")
    errors.raise_if_any()

    incident = disaster_incident_service.get_disaster_incident_by_id(incident_id)

    # Verify authority exists
    authority = db.session.get(Authority, authority_id)
    if authority is None:
        raise ApiError("Authority not found.", status=404, code="authority_not_found")

    # Verify authority covers this incident's jurisdiction
    # National authorities (district_id is None) can be assigned to any incident
    # District-scoped authorities must match the incident's district
    if authority.district_id is not None and incident.district_id is not None:
        if authority.district_id != incident.district_id:
            raise ApiError(
                "This authority does not cover the incident's district.",
                status=403,
                code="authority_jurisdiction_mismatch",
                details={
                    "authority_district_id": str(authority.district_id),
                    "incident_district_id": str(incident.district_id),
                },
            )

    # Update authority assignment
    incident.authority_id = authority.id
    incident.assigned_at = datetime.utcnow()
    incident.assigned_by_id = current_user().id

    if incident.status == DisasterIncidentStatus.DETECTED:
        incident.status = DisasterIncidentStatus.DISPATCHED

    db.session.commit()
    return success_response({"incident": incident.to_authority_dict()})