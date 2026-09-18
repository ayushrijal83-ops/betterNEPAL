"""Incident business logic: verification and clustering.

Reports are claims; incidents are findings. This module is where a human with
authority turns the former into the latter, and where several reports of the
same problem get gathered under one incident.

Nothing here assigns an authority, department or contractor - Phase 7.

Proximity search and the two backends
-------------------------------------

``find_nearby_reports`` needs a "within N metres" query, and that has to work
on both backends or clustering cannot be developed or tested locally.

* **PostGIS**: ``ST_DWithin`` over ``geography``, which is a true spheroidal
  distance and the authoritative answer.
* **Anything else**: a bounding-box prefilter on the latitude/longitude floats,
  refined by a Python haversine.

Phase 4 deliberately shipped *no* distance helper, on the grounds that PostGIS
should be the single source of truth and a second implementation would quietly
disagree with it. That reasoning still holds, so the fallback does not pretend
to be equivalent: every result says which ``method`` produced it. The haversine
is spherical rather than spheroidal, so it differs from PostGIS by up to ~0.5%
- about 25cm at a 50m radius, irrelevant for clustering, and not something to
rely on where exactness matters.
"""
from __future__ import annotations

import math
import uuid
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import cast, func, select
from sqlalchemy.orm import selectinload

from ..extensions import db
from ..gis.location import Coordinates
from ..models.enums import (
    IncidentSeverity,
    IncidentStatus,
    ReportStatus,
    enum_values,
    parse_enum,
)
from ..models.incident import Incident
from ..models.report import Report
from ..utils.helpers import ApiError
from . import geolocation_service

# Once closed, an incident stays closed: a problem that recurs is a new
# incident, not a resurrection of the old one, or its history stops meaning
# anything. Everything short of that can move back and forth, because
# "resolved" work does sometimes turn out not to be.
ALLOWED_INCIDENT_TRANSITIONS: dict[IncidentStatus, tuple[IncidentStatus, ...]] = {
    IncidentStatus.OPEN: (
        IncidentStatus.IN_PROGRESS,
        IncidentStatus.RESOLVED,
        IncidentStatus.CLOSED,
    ),
    IncidentStatus.IN_PROGRESS: (
        IncidentStatus.OPEN,
        IncidentStatus.RESOLVED,
        IncidentStatus.CLOSED,
    ),
    IncidentStatus.RESOLVED: (
        IncidentStatus.IN_PROGRESS,
        IncidentStatus.CLOSED,
    ),
    IncidentStatus.CLOSED: (),
}

DEFAULT_CLUSTER_RADIUS_METRES = 50
MAX_CLUSTER_RADIUS_METRES = 5000
MAX_NEARBY_RESULTS = 100

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

# Mean Earth radius, for the non-PostGIS fallback only.
EARTH_RADIUS_METRES = 6_371_008.8
METRES_PER_DEGREE_LATITUDE = 111_320.0


def _parse_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise ApiError(
            f"{field} is not a valid identifier.", status=400, code="invalid_identifier"
        ) from None


def _incident_query():
    return select(Incident).options(
        selectinload(Incident.district),
        selectinload(Incident.municipality),
        selectinload(Incident.verified_by),
        selectinload(Incident.reports),
    )


def _set_point(record, coordinates: Coordinates) -> None:
    """Populate the PostGIS point from the floats. No-op off PostgreSQL."""
    if not geolocation_service.spatial_backend_available():
        return
    record.location = func.ST_SetSRID(
        func.ST_MakePoint(coordinates.longitude, coordinates.latitude), 4326
    )


# --- verification ----------------------------------------------------------


def _load_report(report_id: Any) -> Report:
    report = db.session.get(Report, _parse_uuid(report_id, "report_id"))
    if report is None:
        raise ApiError("Report not found.", status=404, code="report_not_found")
    return report


def get_incident_by_id(incident_id: Any) -> Incident:
    incident = db.session.scalar(
        _incident_query().where(Incident.id == _parse_uuid(incident_id, "incident_id"))
    )
    if incident is None:
        raise ApiError("Incident not found.", status=404, code="incident_not_found")
    return incident


def _assert_report_is_promotable(report: Report) -> None:
    """A report may join an incident only once, and only if not rejected."""
    if report.incident_id is not None:
        raise ApiError(
            "This report is already linked to an incident.",
            status=409,
            code="report_already_linked",
            details={"incident_id": str(report.incident_id)},
        )
    if report.status == ReportStatus.REJECTED:
        raise ApiError(
            "A rejected report cannot be promoted to an incident.",
            status=409,
            code="report_rejected",
        )


def _mark_verified(report: Report) -> None:
    """Move a report to VERIFIED_AS_INCIDENT.

    Already-verified reports are left alone rather than refused: an authority
    may have set that status in Phase 5 without creating an incident record,
    and attaching one afterwards is exactly the right correction.
    """
    if report.status != ReportStatus.VERIFIED_AS_INCIDENT:
        report.status = ReportStatus.VERIFIED_AS_INCIDENT


def create_incident_from_report(
    report_id: Any,
    severity: IncidentSeverity | None = None,
    user_id: Any = None,
    title: str | None = None,
    description: str | None = None,
) -> Incident:
    """Promote a report into a new incident.

    Coordinates, category and administrative area are inherited from the
    report, not re-supplied: the incident is a finding *about that report*, and
    letting the caller retype the location would allow the two to disagree.
    Title and description may be overridden, since a verifier often has a
    better summary than the original reporter.
    """
    report = _load_report(report_id)
    _assert_report_is_promotable(report)

    verified_by_id = _parse_uuid(user_id, "user_id") if user_id is not None else None

    incident = Incident(
        title=(title or report.title)[:150],
        description=description or report.description,
        category=report.category,
        severity=severity or IncidentSeverity.MEDIUM,
        status=IncidentStatus.OPEN,
        district_id=report.district_id,
        municipality_id=report.municipality_id,
        latitude=report.latitude,
        longitude=report.longitude,
        verified_by_id=verified_by_id,
    )
    _set_point(incident, Coordinates(report.latitude, report.longitude))

    db.session.add(incident)
    db.session.flush()  # assign incident.id before linking the report

    report.incident_id = incident.id
    _mark_verified(report)

    db.session.commit()
    return get_incident_by_id(incident.id)


def link_report_to_incident(report_id: Any, incident_id: Any) -> Incident:
    """Attach an additional report to an existing incident.

    This is the "five citizens, one burst main" path. The incident's own
    coordinates are left untouched - they belong to the report that was
    verified first, and drifting them towards each new report would make the
    incident wander.
    """
    incident = get_incident_by_id(incident_id)
    report = _load_report(report_id)
    _assert_report_is_promotable(report)

    if incident.status == IncidentStatus.CLOSED:
        raise ApiError(
            "Reports cannot be added to a closed incident.",
            status=409,
            code="incident_closed",
        )

    report.incident_id = incident.id
    _mark_verified(report)

    db.session.commit()
    return get_incident_by_id(incident.id)


def get_report_in_incident(report_id: Any, incident_id: uuid.UUID) -> Report:
    """Fetch a report, confirming it belongs to the given incident.

    Scoping the lookup means one incident's id cannot be used to detach a
    report that belongs to a different incident.
    """
    report = _load_report(report_id)
    if report.incident_id != incident_id:
        raise ApiError(
            "This report is not linked to that incident.",
            status=409,
            code="report_not_in_incident",
        )
    return report


def unlink_report(report_id: Any) -> Report:
    """Detach a report from its incident, reverting it to review.

    Verification is a human judgement and humans mis-cluster; without this a
    wrongly attached report would be stuck.
    """
    report = _load_report(report_id)
    if report.incident_id is None:
        raise ApiError(
            "This report is not linked to an incident.",
            status=409,
            code="report_not_linked",
        )

    report.incident_id = None
    report.status = ReportStatus.UNDER_REVIEW
    db.session.commit()
    return report


# --- proximity -------------------------------------------------------------


def _haversine_metres(
    lat1: float, lng1: float, lat2: float, lng2: float
) -> float:
    """Great-circle distance. Fallback only; see the module docstring."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METRES * math.asin(math.sqrt(a))


def _bounding_box(
    coordinates: Coordinates, radius_metres: float
) -> tuple[float, float, float, float]:
    """A generous lat/lng box around a point, to prefilter in SQL.

    Longitude degrees shrink towards the poles, so the span is divided by
    cos(latitude); the clamp stops that exploding to infinity at the poles,
    where the box simply becomes the whole longitude range.
    """
    lat_delta = radius_metres / METRES_PER_DEGREE_LATITUDE
    cos_lat = math.cos(math.radians(coordinates.latitude))
    if abs(cos_lat) < 1e-9:
        lng_delta = 180.0
    else:
        lng_delta = min(180.0, radius_metres / (METRES_PER_DEGREE_LATITUDE * abs(cos_lat)))

    return (
        max(-90.0, coordinates.latitude - lat_delta),
        min(90.0, coordinates.latitude + lat_delta),
        coordinates.longitude - lng_delta,
        coordinates.longitude + lng_delta,
    )


def find_nearby_reports(
    lat: float,
    lng: float,
    radius_meters: float = DEFAULT_CLUSTER_RADIUS_METRES,
    exclude_linked: bool = True,
    exclude_report_ids: list[uuid.UUID] | None = None,
    limit: int = MAX_NEARBY_RESULTS,
) -> dict[str, Any]:
    """Candidate reports near a point, for clustering.

    Returns the matches plus the ``method`` used, so a caller can tell an
    authoritative PostGIS answer from the approximate fallback.
    """
    coordinates = Coordinates.parse(latitude=lat, longitude=lng)
    radius = max(1.0, min(float(radius_meters), MAX_CLUSTER_RADIUS_METRES))
    limit = max(1, min(int(limit), MAX_NEARBY_RESULTS))

    statement = select(Report).options(
        selectinload(Report.reporter), selectinload(Report.district)
    )
    if exclude_linked:
        statement = statement.where(Report.incident_id.is_(None))
    # A rejected report is not a clustering candidate.
    statement = statement.where(Report.status != ReportStatus.REJECTED)
    if exclude_report_ids:
        statement = statement.where(Report.id.notin_(exclude_report_ids))

    if geolocation_service.spatial_backend_available():
        point = func.ST_SetSRID(
            func.ST_MakePoint(coordinates.longitude, coordinates.latitude), 4326
        )
        # Cast to geography so the radius is in metres on the spheroid rather
        # than in degrees, which would be meaningless as a distance.
        statement = statement.where(
            Report.location.isnot(None),
            func.ST_DWithin(
                cast(Report.location, Geography),
                cast(point, Geography),
                radius,
            ),
        )
        records = db.session.scalars(statement.limit(limit)).all()
        method = "postgis"
        matches = [
            (
                report,
                _haversine_metres(
                    coordinates.latitude, coordinates.longitude,
                    report.latitude, report.longitude,
                ),
            )
            for report in records
        ]
    else:
        min_lat, max_lat, min_lng, max_lng = _bounding_box(coordinates, radius)
        statement = statement.where(
            Report.latitude.between(min_lat, max_lat),
            Report.longitude.between(min_lng, max_lng),
        )
        # The box is a prefilter, not the answer: its corners lie outside the
        # circle, so each candidate is re-checked against the true distance.
        matches = []
        for report in db.session.scalars(statement).all():
            distance = _haversine_metres(
                coordinates.latitude, coordinates.longitude,
                report.latitude, report.longitude,
            )
            if distance <= radius:
                matches.append((report, distance))
        method = "approximate"

    matches.sort(key=lambda pair: pair[1])
    matches = matches[:limit]

    return {
        "center": coordinates.as_dict(),
        "radius_meters": radius,
        "method": method,
        "count": len(matches),
        "reports": [
            {**report.to_dict(), "distance_meters": round(distance, 2)}
            for report, distance in matches
        ],
    }


def find_nearby_reports_for_incident(
    incident_id: Any, radius_meters: float = DEFAULT_CLUSTER_RADIUS_METRES
) -> dict[str, Any]:
    """Unlinked reports near an incident, as clustering candidates."""
    incident = get_incident_by_id(incident_id)
    result = find_nearby_reports(
        lat=incident.latitude,
        lng=incident.longitude,
        radius_meters=radius_meters,
        exclude_linked=True,
        exclude_report_ids=[report.id for report in incident.reports],
    )
    result["incident_id"] = str(incident.id)
    return result


# --- listing ---------------------------------------------------------------


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_incidents(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """List incidents, newest first, with optional filtering and paging."""
    from ..models.enums import ReportCategory

    filters = filters or {}
    statement = _incident_query()
    count_statement = select(func.count()).select_from(Incident)

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

    _enum_filter("status", IncidentStatus, Incident.status)
    _enum_filter("severity", IncidentSeverity, Incident.severity)
    _enum_filter("category", ReportCategory, Incident.category)

    for key, column in (
        ("district_id", Incident.district_id),
        ("municipality_id", Incident.municipality_id),
    ):
        value = filters.get(key)
        if value:
            parsed = _parse_uuid(value, key)
            statement = statement.where(column == parsed)
            count_statement = count_statement.where(column == parsed)

    page = max(1, _as_int(filters.get("page"), 1))
    per_page = min(
        MAX_PAGE_SIZE, max(1, _as_int(filters.get("per_page"), DEFAULT_PAGE_SIZE))
    )

    total = db.session.scalar(count_statement) or 0
    records = db.session.scalars(
        statement.order_by(Incident.created_at.desc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    ).all()

    return {
        "incidents": [incident.to_dict() for incident in records],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page if total else 0,
        },
    }


# --- lifecycle -------------------------------------------------------------


def update_incident_status(
    incident_id: Any,
    status: IncidentStatus | None = None,
    severity: IncidentSeverity | None = None,
) -> Incident:
    """Update an incident's status and/or severity.

    Severity may be changed freely - new information legitimately makes a
    problem look worse or better. Status is constrained by
    ``ALLOWED_INCIDENT_TRANSITIONS``.
    """
    if status is None and severity is None:
        raise ApiError(
            "Provide at least one of 'status' or 'severity'.",
            status=400,
            code="nothing_to_update",
        )

    incident = get_incident_by_id(incident_id)

    if status is not None and status != incident.status:
        allowed = ALLOWED_INCIDENT_TRANSITIONS.get(incident.status, ())
        if status not in allowed:
            raise ApiError(
                f"An incident with status '{incident.status.value}' cannot be "
                f"changed to '{status.value}'.",
                status=409,
                code="invalid_status_transition",
                details={
                    "current_status": incident.status.value,
                    "allowed_transitions": [member.value for member in allowed],
                },
            )
        incident.status = status

    if severity is not None:
        incident.severity = severity

    db.session.commit()
    return get_incident_by_id(incident.id)


def get_incident_statistics() -> dict[str, Any]:
    """Counts by status and severity, plus clustering reach."""
    from ..models.enums import ReportCategory

    by_status = dict(
        db.session.execute(
            select(Incident.status, func.count()).group_by(Incident.status)
        ).all()
    )
    by_severity = dict(
        db.session.execute(
            select(Incident.severity, func.count()).group_by(Incident.severity)
        ).all()
    )
    by_category = dict(
        db.session.execute(
            select(Incident.category, func.count()).group_by(Incident.category)
        ).all()
    )

    return {
        "total": db.session.scalar(select(func.count()).select_from(Incident)) or 0,
        "by_status": {
            member.value: by_status.get(member, 0) for member in IncidentStatus
        },
        "by_severity": {
            member.value: by_severity.get(member, 0) for member in IncidentSeverity
        },
        "by_category": {
            member.value: by_category.get(member, 0) for member in ReportCategory
        },
        "linked_reports": db.session.scalar(
            select(func.count()).select_from(Report).where(Report.incident_id.isnot(None))
        )
        or 0,
    }
