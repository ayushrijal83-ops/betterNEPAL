"""Report business logic.

Scope of this phase: ingest what a citizen observed, locate it, store it, and
let a human move it along a small status lifecycle. No grouping into incidents,
no authority routing, no AI - those are Phases 6, 7 and 10.

Geographic trust
----------------

The client sends coordinates and nothing else about location. ``district_id``
and ``municipality_id`` are resolved server-side by
``geolocation_service.reverse_geocode`` and are never read from the request
body. A phone can be wrong about which district it is standing in, and a
malicious client can simply lie - but neither can move a report into a district
it did not happen in.

When reverse geocoding cannot answer (no PostGIS, no boundary dataset imported,
or a point outside known coverage), the report is stored with NULL district and
municipality. That is the honest outcome: the observation and its coordinates
are still worth keeping, and the geography can be backfilled once authoritative
boundaries exist.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..extensions import db
from ..gis.location import Coordinates
from ..models.enums import ReportCategory, ReportStatus
from ..models.report import Report
from ..models.user import User
from ..utils.helpers import ApiError
from . import geolocation_service

# Which status changes a human may make. Terminal states stay terminal: once a
# report has been rejected or promoted to an incident, reopening it would
# detach it from whatever Phase 6 created, so it is not allowed here.
ALLOWED_STATUS_TRANSITIONS: dict[ReportStatus, tuple[ReportStatus, ...]] = {
    ReportStatus.SUBMITTED: (
        ReportStatus.UNDER_REVIEW,
        ReportStatus.VERIFIED_AS_INCIDENT,
        ReportStatus.REJECTED,
    ),
    ReportStatus.UNDER_REVIEW: (
        ReportStatus.VERIFIED_AS_INCIDENT,
        ReportStatus.REJECTED,
    ),
    ReportStatus.VERIFIED_AS_INCIDENT: (),
    ReportStatus.REJECTED: (),
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


def _base_query():
    """Eager-load the relationships every serialised report touches.

    Without this, listing N reports issues 3N extra queries for reporter,
    district and municipality names.
    """
    return select(Report).options(
        selectinload(Report.reporter),
        selectinload(Report.district),
        selectinload(Report.municipality),
    )


def _resolve_location(coordinates: Coordinates) -> dict[str, Any]:
    """Ask the GIS layer where this point is.

    Returns the ids to store plus the raw result, so callers can report how the
    location was (or was not) determined without guessing.
    """
    result = geolocation_service.reverse_geocode(coordinates)

    district_id = None
    municipality_id = None
    if result.get("resolved"):
        district = result.get("district") or {}
        municipality = result.get("municipality") or {}
        if district.get("id"):
            district_id = _parse_uuid(district["id"], "district_id")
        if municipality.get("id"):
            municipality_id = _parse_uuid(municipality["id"], "municipality_id")

    return {
        "district_id": district_id,
        "municipality_id": municipality_id,
        "resolved": bool(result.get("resolved")),
        "reason": result.get("reason"),
    }


def _set_point(report: Report, coordinates: Coordinates) -> None:
    """Populate the PostGIS point from the stored floats.

    No-op off PostgreSQL, where the column cannot hold geometry; the floats
    remain the source of truth either way. Parameterised through PostGIS
    functions - no SQL is assembled by hand.
    """
    if not geolocation_service.spatial_backend_available():
        return
    report.location = func.ST_SetSRID(
        func.ST_MakePoint(coordinates.longitude, coordinates.latitude), 4326
    )


def create_report(user_id: uuid.UUID | str, data: dict[str, Any]) -> Report:
    """Create a report for an authenticated user.

    ``data`` is the output of ``validate_report_creation``: already-validated
    title, description, category and :class:`Coordinates`.
    """
    reporter_id = _parse_uuid(user_id, "user_id")
    if db.session.get(User, reporter_id) is None:
        raise ApiError("Reporter not found.", status=404, code="user_not_found")

    coordinates: Coordinates = data["coordinates"]
    location = _resolve_location(coordinates)

    report = Report(
        reporter_id=reporter_id,
        title=data["title"],
        description=data["description"],
        category=data["category"],
        status=ReportStatus.SUBMITTED,
        latitude=coordinates.latitude,
        longitude=coordinates.longitude,
        district_id=location["district_id"],
        municipality_id=location["municipality_id"],
    )
    _set_point(report, coordinates)

    db.session.add(report)
    db.session.commit()
    return report


def get_report_by_id(report_id: Any) -> Report:
    report = db.session.scalar(
        _base_query().where(Report.id == _parse_uuid(report_id, "report_id"))
    )
    if report is None:
        raise ApiError("Report not found.", status=404, code="report_not_found")
    return report


def get_reports(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """List reports, newest first, with optional filtering and paging.

    Unknown filter values raise 400 rather than silently returning everything -
    a typo in ``?status=`` must not look like "no matching reports".
    """
    from ..models.enums import enum_values, parse_enum

    filters = filters or {}
    statement = _base_query()
    count_statement = select(func.count()).select_from(Report)

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

    _enum_filter("status", ReportStatus, Report.status)
    _enum_filter("category", ReportCategory, Report.category)

    district_id = filters.get("district_id")
    if district_id:
        parsed = _parse_uuid(district_id, "district_id")
        statement = statement.where(Report.district_id == parsed)
        count_statement = count_statement.where(Report.district_id == parsed)

    municipality_id = filters.get("municipality_id")
    if municipality_id:
        parsed = _parse_uuid(municipality_id, "municipality_id")
        statement = statement.where(Report.municipality_id == parsed)
        count_statement = count_statement.where(Report.municipality_id == parsed)

    reporter_id = filters.get("reporter_id")
    if reporter_id:
        parsed = _parse_uuid(reporter_id, "reporter_id")
        statement = statement.where(Report.reporter_id == parsed)
        count_statement = count_statement.where(Report.reporter_id == parsed)

    page = max(1, _as_int(filters.get("page"), 1))
    per_page = min(MAX_PAGE_SIZE, max(1, _as_int(filters.get("per_page"), DEFAULT_PAGE_SIZE)))

    total = db.session.scalar(count_statement) or 0
    records = db.session.scalars(
        statement.order_by(Report.created_at.desc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    ).all()

    return {
        "reports": [report.to_dict() for report in records],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page if total else 0,
        },
    }


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def update_report_status(report_id: Any, new_status: ReportStatus) -> Report:
    """Move a report along its lifecycle.

    Caller is responsible for authorization; the route restricts this to
    authority and admin roles. This function enforces which transitions are
    legal, which is a domain rule rather than a permissions one.
    """
    report = get_report_by_id(report_id)

    if report.status == new_status:
        return report

    allowed = ALLOWED_STATUS_TRANSITIONS.get(report.status, ())
    if new_status not in allowed:
        raise ApiError(
            f"A report with status '{report.status.value}' cannot be changed to "
            f"'{new_status.value}'.",
            status=409,
            code="invalid_status_transition",
            details={
                "current_status": report.status.value,
                "allowed_transitions": [status.value for status in allowed],
            },
        )

    report.status = new_status
    db.session.commit()
    return report


def get_report_statistics() -> dict[str, Any]:
    """Counts by status and category, plus how many lack a resolved district."""
    by_status = dict(
        db.session.execute(
            select(Report.status, func.count()).group_by(Report.status)
        ).all()
    )
    by_category = dict(
        db.session.execute(
            select(Report.category, func.count()).group_by(Report.category)
        ).all()
    )
    return {
        "total": db.session.scalar(select(func.count()).select_from(Report)) or 0,
        "by_status": {
            status.value: by_status.get(status, 0) for status in ReportStatus
        },
        "by_category": {
            category.value: by_category.get(category, 0) for category in ReportCategory
        },
        "without_district": db.session.scalar(
            select(func.count()).select_from(Report).where(Report.district_id.is_(None))
        )
        or 0,
    }
