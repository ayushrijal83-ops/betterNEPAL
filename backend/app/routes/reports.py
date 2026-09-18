"""Report endpoints.

Handlers stay thin: validate, call the service, shape the response.

Reading reports is public - civic transparency is the point of the platform,
and the frontend map must render before anyone logs in. Creating a report needs
an account so submissions are attributable, and changing a report's status is
restricted to authority and admin roles.

Note that ``district_id`` is accepted as a *filter* on the list endpoint but is
never read from a creation body; see ``app/services/report_service.py``.
"""
from __future__ import annotations

from flask import Blueprint, request

from ..models.role import ROLE_ADMIN, ROLE_AUTHORITY, ROLE_CITIZEN, ROLE_TREKKING_GUIDE
from ..services import report_service
from ..utils.decorators import require_roles
from ..utils.helpers import success_response
from ..utils.permissions import current_user
from ..utils.validators import validate_report_creation, validate_status_update

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")


@reports_bp.post("")
@require_roles(ROLE_CITIZEN, ROLE_TREKKING_GUIDE, ROLE_AUTHORITY, ROLE_ADMIN)
def create_report():
    """Submit a report.

    The reporter is taken from the access token, never from the body, so one
    account cannot file a report as another.
    """
    data = validate_report_creation(request.get_json(silent=True))
    report = report_service.create_report(current_user().id, data)
    return success_response({"report": report.to_dict()}, status=201)


@reports_bp.get("")
def list_reports():
    """List reports, newest first. Public.

    Filters: ``category``, ``status``, ``district_id``, ``municipality_id``,
    ``reporter_id``, plus ``page`` and ``per_page``.
    """
    return success_response(
        report_service.get_reports(
            {
                "category": request.args.get("category"),
                "status": request.args.get("status"),
                "district_id": request.args.get("district_id"),
                "municipality_id": request.args.get("municipality_id"),
                "reporter_id": request.args.get("reporter_id"),
                "page": request.args.get("page"),
                "per_page": request.args.get("per_page"),
            }
        )
    )


@reports_bp.get("/statistics")
def report_statistics():
    """Aggregate counts. Public.

    Declared before the ``/<report_id>`` rule so the literal path is not
    swallowed by the identifier converter.
    """
    return success_response(report_service.get_report_statistics())


@reports_bp.get("/<report_id>")
def get_report(report_id: str):
    """Fetch one report. Public."""
    return success_response(
        {"report": report_service.get_report_by_id(report_id).to_dict()}
    )


@reports_bp.patch("/<report_id>/status")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def update_report_status(report_id: str):
    """Move a report along its lifecycle. Authority and admin only.

    A citizen - including the report's own author - cannot verify or reject
    their own submission; that is what makes verification meaningful.
    """
    status = validate_status_update(request.get_json(silent=True))
    report = report_service.update_report_status(report_id, status)
    return success_response({"report": report.to_dict()})
