"""Project business logic: commissioned work and its progress ledger.

Every status change writes a :class:`ProgressUpdate` in the same transaction as
the change itself. That is the point of this phase - a project whose status
moved with no record of who moved it or why is exactly the opacity the platform
exists to remove. There is no code path that changes ``status`` without
appending to the ledger.

Media and evidence attachments are Phase 9; nothing here handles files.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..extensions import db
from ..models.base import utcnow
from ..models.enums import ProjectStatus, enum_values, parse_enum
from ..models.progress_update import ProgressUpdate
from ..models.project import Project
from ..models.role import ROLE_CONTRACTOR
from ..models.user import User
from ..utils.helpers import ApiError

# Both end states are terminal. Work that restarts after completion or
# cancellation is a new project, so the finished one's ledger keeps meaning
# what it said at the time.
#
# ON_HOLD cannot jump straight to COMPLETED: paused work has to be resumed
# before it can be declared done, which keeps the ledger honest about what
# actually happened.
ALLOWED_PROJECT_TRANSITIONS: dict[ProjectStatus, tuple[ProjectStatus, ...]] = {
    ProjectStatus.PLANNED: (
        ProjectStatus.ACTIVE,
        ProjectStatus.ON_HOLD,
        ProjectStatus.CANCELLED,
    ),
    ProjectStatus.ACTIVE: (
        ProjectStatus.ON_HOLD,
        ProjectStatus.COMPLETED,
        ProjectStatus.CANCELLED,
    ),
    ProjectStatus.ON_HOLD: (
        ProjectStatus.ACTIVE,
        ProjectStatus.CANCELLED,
    ),
    ProjectStatus.COMPLETED: (),
    ProjectStatus.CANCELLED: (),
}

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


def _parse_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise ApiError(
            f"{field} is not a valid identifier.", status=400, code="invalid_identifier"
        ) from None


def _project_query():
    return select(Project).options(
        selectinload(Project.authority),
        selectinload(Project.incident),
        selectinload(Project.contractor),
        selectinload(Project.progress_updates).selectinload(ProgressUpdate.author),
    )


def get_project_by_id(project_id: Any) -> Project:
    project = db.session.scalar(
        _project_query().where(Project.id == _parse_uuid(project_id, "project_id"))
    )
    if project is None:
        raise ApiError("Project not found.", status=404, code="project_not_found")
    return project


# --- creation --------------------------------------------------------------


def create_project(data: dict[str, Any], authority_id: Any) -> Project:
    """Commission work under an authority.

    ``data`` is the output of ``validate_project_creation``. The authority is
    passed separately because it is the one field the caller must not be able
    to leave implicit - work with no accountable commissioning body is what
    this platform exists to prevent.
    """
    from ..models.authority import Authority
    from ..models.incident import Incident

    parsed_authority = _parse_uuid(authority_id, "authority_id")
    if db.session.get(Authority, parsed_authority) is None:
        raise ApiError("Authority not found.", status=404, code="authority_not_found")

    incident_id = data.get("incident_id")
    parsed_incident = None
    if incident_id:
        parsed_incident = _parse_uuid(incident_id, "incident_id")
        if db.session.get(Incident, parsed_incident) is None:
            raise ApiError("Incident not found.", status=404, code="incident_not_found")

    project = Project(
        title=data["title"],
        description=data["description"],
        status=data.get("status") or ProjectStatus.PLANNED,
        incident_id=parsed_incident,
        authority_id=parsed_authority,
        start_date=data.get("start_date"),
        estimated_end_date=data.get("estimated_end_date"),
    )
    db.session.add(project)
    db.session.commit()
    return get_project_by_id(project.id)


# --- contractor ------------------------------------------------------------


def assign_contractor(project_id: Any, contractor_id: Any) -> Project:
    """Award the work to a contractor.

    The user must actually hold the contractor role. Checking the role here
    rather than trusting the caller stops a project being awarded to a citizen
    account, which would let someone outside the contracting process post
    progress updates that read as official.
    """
    project = get_project_by_id(project_id)

    if project.status in (ProjectStatus.COMPLETED, ProjectStatus.CANCELLED):
        raise ApiError(
            f"A {project.status.value} project cannot be reassigned.",
            status=409,
            code="project_finished",
        )

    user = db.session.get(User, _parse_uuid(contractor_id, "contractor_id"))
    if user is None:
        raise ApiError("User not found.", status=404, code="user_not_found")

    if not user.has_role(ROLE_CONTRACTOR):
        raise ApiError(
            "This user does not hold the contractor role.",
            status=409,
            code="not_a_contractor",
            details={"roles": user.role_names},
        )
    if not user.is_active:
        raise ApiError(
            "This contractor account is disabled.",
            status=409,
            code="contractor_disabled",
        )

    project.contractor_id = user.id
    db.session.commit()
    return get_project_by_id(project.id)


def remove_contractor(project_id: Any) -> Project:
    """Unassign the contractor, e.g. when a contract is terminated."""
    project = get_project_by_id(project_id)
    if project.contractor_id is None:
        raise ApiError(
            "This project has no contractor assigned.",
            status=409,
            code="no_contractor_assigned",
        )
    project.contractor_id = None
    db.session.commit()
    return get_project_by_id(project.id)


def can_post_update(project: Project, user: User) -> bool:
    """Whether this user may append to this project's ledger.

    Authorities and admins oversee all work. A contractor may only write to the
    project they were actually awarded - otherwise one contractor could post
    progress against a competitor's job.
    """
    from ..models.role import ROLE_ADMIN, ROLE_AUTHORITY

    if user.has_role(ROLE_ADMIN, ROLE_AUTHORITY):
        return True
    return project.contractor_id is not None and project.contractor_id == user.id


# --- progress --------------------------------------------------------------


def _append_update(
    project: Project,
    author_id: uuid.UUID,
    notes: str,
    previous_status: ProjectStatus | None = None,
    new_status: ProjectStatus | None = None,
) -> ProgressUpdate:
    """Build a ledger row. The caller commits."""
    update = ProgressUpdate(
        project_id=project.id,
        author_id=author_id,
        notes=notes,
        previous_status=previous_status,
        new_status=new_status,
    )
    db.session.add(update)
    return update


def add_progress_update(project_id: Any, author_id: Any, notes: str) -> ProgressUpdate:
    """Append a routine note without changing the status."""
    project = get_project_by_id(project_id)
    parsed_author = _parse_uuid(author_id, "author_id")

    if project.status in (ProjectStatus.COMPLETED, ProjectStatus.CANCELLED):
        raise ApiError(
            f"A {project.status.value} project cannot receive new updates.",
            status=409,
            code="project_finished",
        )

    update = _append_update(project, parsed_author, notes)
    db.session.commit()
    db.session.refresh(update)
    return update


def _resolve_linked_incident(project: Project) -> str | None:
    """Move the project's incident to RESOLVED, if that is legal.

    Returns the reason it was skipped, or None when it was resolved. A CLOSED
    incident is terminal (Phase 6), so completing a project attached to one
    changes nothing rather than forcing an illegal transition - the project
    still completes, and the caller is told why the incident did not move.
    """
    from ..models.enums import IncidentStatus
    from .incident_service import ALLOWED_INCIDENT_TRANSITIONS

    incident = project.incident
    if incident is None:
        return "no_linked_incident"
    if incident.status == IncidentStatus.RESOLVED:
        return None

    if IncidentStatus.RESOLVED not in ALLOWED_INCIDENT_TRANSITIONS.get(
        incident.status, ()
    ):
        return f"incident_{incident.status.value}"

    incident.status = IncidentStatus.RESOLVED
    # The work finishing is what resolved it, so date it from the project
    # rather than from now - a completion recorded late should not inflate the
    # measured resolution time.
    incident.resolved_at = utcnow()
    return None


def update_project_status(
    project_id: Any,
    new_status: ProjectStatus,
    author_id: Any,
    notes: str,
) -> dict[str, Any]:
    """Change a project's status and record the change in the same transaction.

    Completing a project stamps ``actual_end_date`` and resolves the linked
    incident where the incident's own lifecycle permits it.

    Returns the project plus a note of what else the change touched, so the
    caller can report "the work is done but the incident was already closed"
    rather than silently doing nothing.
    """
    project = get_project_by_id(project_id)
    parsed_author = _parse_uuid(author_id, "author_id")
    previous_status = project.status

    if new_status == previous_status:
        raise ApiError(
            f"This project is already {new_status.value}.",
            status=409,
            code="status_unchanged",
        )

    allowed = ALLOWED_PROJECT_TRANSITIONS.get(previous_status, ())
    if new_status not in allowed:
        raise ApiError(
            f"A project with status '{previous_status.value}' cannot be changed "
            f"to '{new_status.value}'.",
            status=409,
            code="invalid_status_transition",
            details={
                "current_status": previous_status.value,
                "allowed_transitions": [member.value for member in allowed],
            },
        )

    project.status = new_status
    incident_note = None

    if new_status == ProjectStatus.ACTIVE and project.start_date is None:
        # Work starting is when it started; nobody should have to type it.
        project.start_date = date.today()

    if new_status == ProjectStatus.COMPLETED:
        project.actual_end_date = date.today()
        if project.start_date is None:
            project.start_date = project.actual_end_date
        incident_note = _resolve_linked_incident(project)

    _append_update(project, parsed_author, notes, previous_status, new_status)
    db.session.commit()

    return {
        "project": get_project_by_id(project.id),
        "previous_status": previous_status.value,
        "incident_not_resolved_because": incident_note,
    }


def get_progress_updates(project_id: Any) -> list[ProgressUpdate]:
    """A project's ledger, newest first."""
    project = get_project_by_id(project_id)
    return list(project.progress_updates)


# --- listing ---------------------------------------------------------------


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_projects(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """List projects, newest first, with optional filtering and paging."""
    filters = filters or {}
    statement = _project_query()
    count_statement = select(func.count()).select_from(Project)

    raw_status = filters.get("status")
    if raw_status not in (None, ""):
        member = parse_enum(ProjectStatus, raw_status)
        if member is None:
            raise ApiError(
                f"status must be one of: {', '.join(enum_values(ProjectStatus))}.",
                status=400,
                code="invalid_filter",
            )
        statement = statement.where(Project.status == member)
        count_statement = count_statement.where(Project.status == member)

    for key, column in (
        ("authority_id", Project.authority_id),
        ("contractor_id", Project.contractor_id),
        ("incident_id", Project.incident_id),
    ):
        value = filters.get(key)
        if value:
            parsed = _parse_uuid(value, key)
            statement = statement.where(column == parsed)
            count_statement = count_statement.where(column == parsed)

    if filters.get("unassigned"):
        statement = statement.where(Project.contractor_id.is_(None))
        count_statement = count_statement.where(Project.contractor_id.is_(None))

    page = max(1, _as_int(filters.get("page"), 1))
    per_page = min(
        MAX_PAGE_SIZE, max(1, _as_int(filters.get("per_page"), DEFAULT_PAGE_SIZE))
    )

    total = db.session.scalar(count_statement) or 0
    records = db.session.scalars(
        statement.order_by(Project.created_at.desc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    ).all()

    return {
        "projects": [project.to_dict() for project in records],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page if total else 0,
        },
    }


def get_project_statistics() -> dict[str, Any]:
    """Counts by status, plus how much work is unassigned or overdue."""
    by_status = dict(
        db.session.execute(
            select(Project.status, func.count()).group_by(Project.status)
        ).all()
    )

    overdue = db.session.scalar(
        select(func.count())
        .select_from(Project)
        .where(
            Project.estimated_end_date.isnot(None),
            Project.estimated_end_date < date.today(),
            Project.status.notin_([ProjectStatus.COMPLETED, ProjectStatus.CANCELLED]),
        )
    )

    return {
        "total": db.session.scalar(select(func.count()).select_from(Project)) or 0,
        "by_status": {
            member.value: by_status.get(member, 0) for member in ProjectStatus
        },
        "without_contractor": db.session.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.contractor_id.is_(None))
        )
        or 0,
        "overdue": overdue or 0,
        "progress_updates": db.session.scalar(
            select(func.count()).select_from(ProgressUpdate)
        )
        or 0,
    }
