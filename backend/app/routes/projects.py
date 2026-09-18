"""Project endpoints.

Reading is public: what is being fixed, by whom, and how far along it is, is
precisely what a civic transparency platform should publish.

Commissioning work and awarding contracts are admin/authority acts. Posting to
a project's progress ledger additionally requires being *that project's*
contractor - a role check alone is not enough, since one contractor must not be
able to post progress against a competitor's job.
"""
from __future__ import annotations

from flask import Blueprint, request

from ..models.role import ROLE_ADMIN, ROLE_AUTHORITY, ROLE_CONTRACTOR
from ..services import project_service
from ..utils.decorators import require_auth, require_roles
from ..utils.helpers import ApiError, success_response
from ..utils.permissions import current_user
from ..utils.validators import (
    validate_contractor_assignment,
    validate_progress_update,
    validate_project_creation,
)

projects_bp = Blueprint("projects", __name__, url_prefix="/projects")

TRUTHY = {"1", "true", "yes", "on"}


@projects_bp.post("")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def create_project():
    """Commission work under an authority. Authority and admin only."""
    data = validate_project_creation(request.get_json(silent=True))
    project = project_service.create_project(data, data["authority_id"])
    return success_response({"project": project.to_dict()}, status=201)


@projects_bp.get("")
def list_projects():
    """List projects, newest first. Public.

    Filters: ``status``, ``authority_id``, ``contractor_id``, ``incident_id``,
    ``unassigned``, plus ``page`` and ``per_page``.
    """
    return success_response(
        project_service.get_projects(
            {
                "status": request.args.get("status"),
                "authority_id": request.args.get("authority_id"),
                "contractor_id": request.args.get("contractor_id"),
                "incident_id": request.args.get("incident_id"),
                "unassigned": (
                    request.args.get("unassigned", "").strip().lower() in TRUTHY
                ),
                "page": request.args.get("page"),
                "per_page": request.args.get("per_page"),
            }
        )
    )


@projects_bp.get("/statistics")
def project_statistics():
    """Aggregate counts. Public.

    Declared before ``/<project_id>`` so the literal path is not swallowed by
    the identifier converter.
    """
    return success_response(project_service.get_project_statistics())


@projects_bp.get("/<project_id>")
def get_project(project_id: str):
    """Fetch one project with its full progress ledger, newest first. Public."""
    project = project_service.get_project_by_id(project_id)
    return success_response({"project": project.to_dict(include_updates=True)})


@projects_bp.get("/<project_id>/updates")
def list_updates(project_id: str):
    """A project's progress ledger, newest first. Public."""
    updates = project_service.get_progress_updates(project_id)
    return success_response(
        {"updates": [update.to_dict() for update in updates], "count": len(updates)}
    )


@projects_bp.patch("/<project_id>/contractor")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def assign_contractor(project_id: str):
    """Award the work to a contractor. Authority and admin only."""
    contractor_id = validate_contractor_assignment(request.get_json(silent=True))
    project = project_service.assign_contractor(project_id, contractor_id)
    return success_response({"project": project.to_dict()})


@projects_bp.delete("/<project_id>/contractor")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def remove_contractor(project_id: str):
    """Unassign the contractor, e.g. on a terminated contract."""
    project = project_service.remove_contractor(project_id)
    return success_response({"project": project.to_dict()})


@projects_bp.post("/<project_id>/updates")
@require_roles(ROLE_CONTRACTOR, ROLE_AUTHORITY, ROLE_ADMIN)
def post_update(project_id: str):
    """Append to the progress ledger, optionally changing the status.

    The role check above gets the caller through the door; the ownership check
    below decides whether this *particular* project is theirs to write to.
    """
    data = validate_progress_update(request.get_json(silent=True))
    user = current_user()

    project = project_service.get_project_by_id(project_id)
    if not project_service.can_post_update(project, user):
        raise ApiError(
            "You are not the contractor assigned to this project.",
            status=403,
            code="not_project_contractor",
        )

    if data["new_status"] is None:
        update = project_service.add_progress_update(
            project_id, user.id, data["notes"]
        )
        return success_response({"update": update.to_dict()}, status=201)

    result = project_service.update_project_status(
        project_id, data["new_status"], user.id, data["notes"]
    )
    return success_response(
        {
            "project": result["project"].to_dict(include_updates=True),
            "previous_status": result["previous_status"],
            # Present when completing the project could not resolve its
            # incident, so the caller learns why rather than assuming it did.
            "incident_not_resolved_because": result["incident_not_resolved_because"],
        },
        status=201,
    )
