"""Incident endpoints.

Handlers stay thin: validate, call the service, shape the response.

Reading is public - a verified civic problem is exactly the thing citizens
should be able to see. Verifying, clustering and moving an incident through its
lifecycle are restricted to authority and admin roles: the value of an incident
is that somebody accountable said it was real.
"""
from __future__ import annotations

from flask import Blueprint, request

from ..models.role import ROLE_ADMIN, ROLE_AUTHORITY
from ..services import incident_service
from ..utils.decorators import require_roles
from ..utils.helpers import ApiError, success_response
from ..utils.permissions import current_user
from ..utils.validators import (
    validate_assignment,
    validate_incident_from_report,
    validate_incident_update,
    validate_link_report,
)

incidents_bp = Blueprint("incidents", __name__, url_prefix="/incidents")


@incidents_bp.post("/from-report")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def create_incident_from_report():
    """Promote a verified report into a new incident.

    The verifier is taken from the access token, so the record of who confirmed
    the problem cannot be forged in the body.
    """
    data = validate_incident_from_report(request.get_json(silent=True))
    incident = incident_service.create_incident_from_report(
        report_id=data["report_id"],
        severity=data["severity"],
        user_id=current_user().id,
        title=data["title"],
        description=data["description"],
    )
    return success_response({"incident": incident.to_dict(include_reports=True)}, status=201)


@incidents_bp.post("/<incident_id>/link-report")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def link_report(incident_id: str):
    """Attach an additional citizen report to an existing incident."""
    report_id = validate_link_report(request.get_json(silent=True))
    incident = incident_service.link_report_to_incident(report_id, incident_id)
    return success_response({"incident": incident.to_dict(include_reports=True)})


@incidents_bp.post("/<incident_id>/unlink-report")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def unlink_report(incident_id: str):
    """Detach a wrongly clustered report, returning it to review."""
    report_id = validate_link_report(request.get_json(silent=True))
    # Confirms the incident exists and that the report really belongs to it,
    # so one incident's id cannot be used to detach another's report.
    incident = incident_service.get_incident_by_id(incident_id)
    report = incident_service.get_report_in_incident(report_id, incident.id)
    return success_response({"report": incident_service.unlink_report(report.id).to_dict()})


@incidents_bp.post("/<incident_id>/assign")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def assign_incident(incident_id: str):
    """Route an incident to the authority responsible for fixing it.

    An OPEN incident becomes IN_PROGRESS - having an owner is the work
    starting. The assigner is taken from the access token, so the record of who
    made the routing decision cannot be forged.
    """
    authority_id = validate_assignment(request.get_json(silent=True))
    incident = incident_service.assign_incident_to_authority(
        incident_id, authority_id, assigned_by_user_id=current_user().id
    )
    return success_response({"incident": incident.to_dict(include_reports=True)})


@incidents_bp.post("/<incident_id>/unassign")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def unassign_incident(incident_id: str):
    """Undo a misrouted assignment, returning the incident to OPEN."""
    incident = incident_service.unassign_incident(incident_id)
    return success_response({"incident": incident.to_dict(include_reports=True)})


@incidents_bp.get("")
def list_incidents():
    """List incidents, newest first. Public.

    Filters: ``status``, ``severity``, ``category``, ``district_id``,
    ``municipality_id``, plus ``page`` and ``per_page``.
    """
    return success_response(
        incident_service.get_incidents(
            {
                "status": request.args.get("status"),
                "severity": request.args.get("severity"),
                "category": request.args.get("category"),
                "district_id": request.args.get("district_id"),
                "municipality_id": request.args.get("municipality_id"),
                "authority_id": request.args.get("authority_id"),
                "page": request.args.get("page"),
                "per_page": request.args.get("per_page"),
            }
        )
    )


@incidents_bp.get("/statistics")
def incident_statistics():
    """Aggregate counts. Public.

    Declared before ``/<incident_id>`` so the literal path is not swallowed by
    the identifier converter.
    """
    return success_response(incident_service.get_incident_statistics())


@incidents_bp.get("/<incident_id>")
def get_incident(incident_id: str):
    """Fetch one incident with summaries of every linked report. Public."""
    incident = incident_service.get_incident_by_id(incident_id)
    return success_response({"incident": incident.to_dict(include_reports=True)})


@incidents_bp.get("/<incident_id>/nearby-reports")
def nearby_reports(incident_id: str):
    """Unlinked reports near this incident, as clustering candidates. Public.

    ``?radius=`` overrides the 50m default. The response names the ``method``
    used, so an approximate non-PostGIS answer is never mistaken for an
    authoritative one.
    """
    raw_radius = request.args.get("radius")
    radius = incident_service.DEFAULT_CLUSTER_RADIUS_METRES
    if raw_radius is not None:
        try:
            radius = float(raw_radius)
        except (TypeError, ValueError):
            raise ApiError(
                "radius must be a number of metres.", status=400, code="invalid_radius"
            ) from None
        if radius <= 0:
            raise ApiError(
                "radius must be greater than zero.", status=400, code="invalid_radius"
            )

    return success_response(
        incident_service.find_nearby_reports_for_incident(incident_id, radius)
    )


@incidents_bp.patch("/<incident_id>")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def update_incident(incident_id: str):
    """Update status and/or severity. Authority and admin only."""
    data = validate_incident_update(request.get_json(silent=True))
    incident = incident_service.update_incident_status(
        incident_id, status=data["status"], severity=data["severity"]
    )
    return success_response({"incident": incident.to_dict(include_reports=True)})
