"""Read-optimised aggregation for maps, dashboards and public metrics.

Two rules shape every query in this module.

**Select columns, not objects.** Loading ORM entities to serialise five fields
pulls whole rows plus their relationships, and a map feed doing that for a
thousand points issues thousands of queries. Everything here selects explicit
columns and aggregates in SQL with ``GROUP BY``, so a district summary is one
round trip regardless of how many districts exist.

**A public feed can only expose what it selected.** Naming columns explicitly
is not just faster, it is the privacy control: a reporter's email address is
not omitted by a serialiser that could be edited to include it - it is never
fetched. ``MAP_POINT_COLUMNS`` below is the complete list of what leaves this
module on a public endpoint.

Reports that are rejected or marked as duplicates are excluded from public
feeds by default. They are still real records, but a map that plots the same
pothole five times misrepresents how much is wrong.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import and_, func, select

from ..extensions import db
from ..models.authority import Authority
from ..models.district import District
from ..models.enums import (
    IncidentSeverity,
    IncidentStatus,
    ProjectStatus,
    ReportCategory,
    ReportStatus,
    enum_values,
    parse_enum,
)
from ..models.incident import Incident
from ..models.project import Project
from ..models.report import Report
from ..utils.helpers import ApiError

# Everything a public map point may contain. Nothing about the reporter appears
# here, and nothing can be added by accident - a field absent from the SELECT
# cannot be serialised.
MAP_POINT_COLUMNS = (
    "id",
    "type",
    "title",
    "category",
    "status",
    "latitude",
    "longitude",
    "district_id",
    "created_at",
)

# Incidents carry one field more. Listed separately rather than folded into the
# shared set, so the report contract stays exactly as narrow as it is. Severity
# is safe to publish: an incident is already a verified public finding, and
# severity is what drives pin colour on a map.
INCIDENT_POINT_COLUMNS = MAP_POINT_COLUMNS + ("severity",)

# An unbounded map feed is a denial-of-service vector and useless to a client
# that has to render it.
MAX_MAP_POINTS = 2000
DEFAULT_MAP_POINTS = 1000

# Incident states that count as "still someone's problem".
OPEN_INCIDENT_STATUSES = (IncidentStatus.OPEN, IncidentStatus.IN_PROGRESS)
CLOSED_INCIDENT_STATUSES = (IncidentStatus.RESOLVED, IncidentStatus.CLOSED)

SECONDS_PER_DAY = 86400.0


def _parse_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise ApiError(
            f"{field} is not a valid identifier.", status=400, code="invalid_identifier"
        ) from None


def _parse_date_bound(value: Any, field: str, end_of_day: bool = False):
    """Parse an ISO date or datetime filter bound.

    A bare ``YYYY-MM-DD`` as an end bound means *the end of* that day -
    otherwise ``?end_date=2026-09-18`` would silently exclude everything
    reported on the 18th, which is not what anybody means by it.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        moment = time.max if end_of_day else time.min
        return datetime.combine(value, moment, tzinfo=timezone.utc)
    if not isinstance(value, str):
        raise ApiError(
            f"{field} must be an ISO date (YYYY-MM-DD).",
            status=400,
            code="invalid_date",
        )

    text = value.strip()
    try:
        if len(text) == 10:
            parsed = date.fromisoformat(text)
            moment = time.max if end_of_day else time.min
            return datetime.combine(parsed, moment, tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(
            f"{field} must be an ISO date (YYYY-MM-DD).",
            status=400,
            code="invalid_date",
        ) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_enum_filter(value: Any, enum_class, field: str):
    if value in (None, ""):
        return None
    member = parse_enum(enum_class, value)
    if member is None:
        raise ApiError(
            f"{field} must be one of: {', '.join(enum_values(enum_class))}.",
            status=400,
            code="invalid_filter",
        )
    return member


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


# --- public map feed -------------------------------------------------------


def _apply_common_filters(statement, model, filters: dict[str, Any]):
    """Date, district and category filters shared by reports and incidents."""
    start = _parse_date_bound(filters.get("start_date"), "start_date")
    end = _parse_date_bound(filters.get("end_date"), "end_date", end_of_day=True)

    if start and end and start > end:
        raise ApiError(
            "start_date cannot be after end_date.", status=400, code="invalid_date_range"
        )
    if start:
        statement = statement.where(model.created_at >= start)
    if end:
        statement = statement.where(model.created_at <= end)

    district_id = filters.get("district_id")
    if district_id:
        statement = statement.where(
            model.district_id == _parse_uuid(district_id, "district_id")
        )

    category = _parse_enum_filter(filters.get("category"), ReportCategory, "category")
    if category:
        statement = statement.where(model.category == category)

    return statement


def get_map_points(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Lightweight points for client-side map rendering.

    Selects nine columns and nothing else. No descriptions, no reporter, no
    history - a map needs a pin and a label, and shipping more would be both
    slow and a privacy leak waiting to be noticed.
    """
    filters = filters or {}
    limit = min(MAX_MAP_POINTS, max(1, _as_int(filters.get("limit"), DEFAULT_MAP_POINTS)))

    requested = (filters.get("type") or "all").strip().lower()
    if requested not in {"all", "reports", "incidents"}:
        raise ApiError(
            "type must be one of: all, reports, incidents.",
            status=400,
            code="invalid_filter",
        )

    points: list[dict[str, Any]] = []
    truncated = False

    if requested in {"all", "reports"}:
        statement = select(
            Report.id,
            Report.title,
            Report.category,
            Report.status,
            Report.latitude,
            Report.longitude,
            Report.district_id,
            Report.created_at,
        )
        statement = _apply_common_filters(statement, Report, filters)

        status = _parse_enum_filter(filters.get("status"), ReportStatus, "status")
        if status:
            statement = statement.where(Report.status == status)
        elif not filters.get("include_inactive"):
            # Three kinds of report are not a separate problem on the ground:
            # a rejected one, one folded into another as a duplicate, and one
            # already verified into an incident - that last is now represented
            # by the incident, so plotting both would show the same pothole
            # twice on a `type=all` map.
            statement = statement.where(
                Report.status.notin_(
                    (ReportStatus.REJECTED, ReportStatus.VERIFIED_AS_INCIDENT)
                ),
                Report.duplicate_of_id.is_(None),
            )

        rows = db.session.execute(
            statement.order_by(Report.created_at.desc()).limit(limit + 1)
        ).all()
        truncated = truncated or len(rows) > limit
        points.extend(
            {
                "id": str(row.id),
                "type": "report",
                "title": row.title,
                "category": row.category.value,
                "status": row.status.value,
                "latitude": row.latitude,
                "longitude": row.longitude,
                "district_id": str(row.district_id) if row.district_id else None,
                "created_at": _iso(row.created_at),
            }
            for row in rows[:limit]
        )

    if requested in {"all", "incidents"}:
        statement = select(
            Incident.id,
            Incident.title,
            Incident.category,
            Incident.status,
            Incident.severity,
            Incident.latitude,
            Incident.longitude,
            Incident.district_id,
            Incident.created_at,
        )
        statement = _apply_common_filters(statement, Incident, filters)

        status = _parse_enum_filter(filters.get("status"), IncidentStatus, "status")
        if status:
            statement = statement.where(Incident.status == status)
        elif not filters.get("include_inactive"):
            statement = statement.where(Incident.status.in_(OPEN_INCIDENT_STATUSES))

        rows = db.session.execute(
            statement.order_by(Incident.created_at.desc()).limit(limit + 1)
        ).all()
        truncated = truncated or len(rows) > limit
        points.extend(
            {
                "id": str(row.id),
                "type": "incident",
                "title": row.title,
                "category": row.category.value,
                "status": row.status.value,
                # Severity drives pin colour, and an incident is already a
                # verified public finding, so it is safe to publish.
                "severity": row.severity.value,
                "latitude": row.latitude,
                "longitude": row.longitude,
                "district_id": str(row.district_id) if row.district_id else None,
                "created_at": _iso(row.created_at),
            }
            for row in rows[:limit]
        )

    points.sort(key=lambda point: point["created_at"] or "", reverse=True)

    return {
        "points": points,
        "count": len(points),
        "limit": limit,
        # Told plainly, so a client never mistakes a capped feed for the whole
        # picture and quietly draws an incomplete map.
        "truncated": truncated,
    }


def to_geojson(result: dict[str, Any]) -> dict[str, Any]:
    """Render a map-point result as a GeoJSON FeatureCollection.

    Coordinates are ``[longitude, latitude]`` - the GeoJSON order, not the
    human one. See ``app/gis/location.py``.
    """
    features = []
    for point in result["points"]:
        properties = {
            key: value
            for key, value in point.items()
            if key not in {"latitude", "longitude"}
        }
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [point["longitude"], point["latitude"]],
                },
                "properties": properties,
            }
        )

    return {
        "type": "FeatureCollection",
        "features": features,
        "count": result["count"],
        "truncated": result["truncated"],
    }


# --- district aggregation --------------------------------------------------


def _count_by_district(model, *conditions):
    """``{district_id: count}`` in one grouped query."""
    statement = select(model.district_id, func.count()).group_by(model.district_id)
    if conditions:
        statement = statement.where(and_(*conditions))
    return {row[0]: row[1] for row in db.session.execute(statement).all()}


def get_district_summary() -> dict[str, Any]:
    """Report and incident counts per district, with resolution rates.

    Four grouped queries plus one district fetch, regardless of how many
    districts exist - not one query per district.
    """
    districts = db.session.execute(
        select(District.id, District.name, District.province).order_by(
            District.province, District.name
        )
    ).all()

    report_counts = _count_by_district(
        Report,
        Report.status != ReportStatus.REJECTED,
        Report.duplicate_of_id.is_(None),
    )
    incident_counts = _count_by_district(Incident)
    open_counts = _count_by_district(
        Incident, Incident.status.in_(OPEN_INCIDENT_STATUSES)
    )
    resolved_counts = _count_by_district(
        Incident, Incident.status.in_(CLOSED_INCIDENT_STATUSES)
    )

    rows = []
    for district in districts:
        incidents = incident_counts.get(district.id, 0)
        resolved = resolved_counts.get(district.id, 0)
        rows.append(
            {
                "district_id": str(district.id),
                "district_name": district.name,
                "province": district.province,
                "report_count": report_counts.get(district.id, 0),
                "incident_count": incidents,
                "open_incident_count": open_counts.get(district.id, 0),
                "resolved_incident_count": resolved,
                # None rather than 0 when there is nothing to divide: a district
                # with no incidents has no resolution rate, and showing 0% would
                # read as total failure rather than "no data".
                "resolution_rate": (
                    round(resolved / incidents * 100, 1) if incidents else None
                ),
            }
        )

    # Reports whose district could not be resolved by reverse geocoding are a
    # real coverage gap, so they are surfaced rather than silently dropped.
    return {
        "districts": rows,
        "count": len(rows),
        "unlocated_reports": report_counts.get(None, 0),
        "unlocated_incidents": incident_counts.get(None, 0),
    }


# --- overview --------------------------------------------------------------


def _count(model, *conditions) -> int:
    statement = select(func.count()).select_from(model)
    if conditions:
        statement = statement.where(and_(*conditions))
    return db.session.scalar(statement) or 0


def get_overview_stats() -> dict[str, Any]:
    """System-wide headline metrics."""
    total_reports = _count(Report)
    total_incidents = _count(Incident)
    resolved_incidents = _count(Incident, Incident.status.in_(CLOSED_INCIDENT_STATUSES))

    return {
        "reports": {
            "total": total_reports,
            "pending": _count(
                Report,
                Report.status.in_((ReportStatus.SUBMITTED, ReportStatus.UNDER_REVIEW)),
            ),
            "verified": _count(Report, Report.status == ReportStatus.VERIFIED_AS_INCIDENT),
            "rejected": _count(Report, Report.status == ReportStatus.REJECTED),
            "duplicates": _count(Report, Report.duplicate_of_id.isnot(None)),
        },
        "incidents": {
            "total": total_incidents,
            "active": _count(Incident, Incident.status.in_(OPEN_INCIDENT_STATUSES)),
            "resolved": resolved_incidents,
            "unassigned": _count(Incident, Incident.authority_id.is_(None)),
            "resolution_rate": (
                round(resolved_incidents / total_incidents * 100, 1)
                if total_incidents
                else None
            ),
        },
        "projects": {
            "total": _count(Project),
            "active": _count(Project, Project.status == ProjectStatus.ACTIVE),
            "completed": _count(Project, Project.status == ProjectStatus.COMPLETED),
            "without_contractor": _count(Project, Project.contractor_id.is_(None)),
        },
        "authorities": {"total": _count(Authority)},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# --- authority performance -------------------------------------------------


def _resolution_seconds_column():
    """Seconds between an incident's creation and its resolution.

    ``julianday`` on SQLite and ``EXTRACT(EPOCH ...)`` on PostgreSQL compute
    this differently, so the arithmetic is done in Python instead. The cost is
    one column of timestamps rather than a single scalar, which is the right
    trade for a metric that must read the same on both backends.
    """
    return Incident.created_at, Incident.resolved_at


def _summarise_durations(pairs) -> dict[str, Any]:
    """Average, median and range over (created_at, resolved_at) pairs."""
    durations = [
        (resolved - created).total_seconds() / SECONDS_PER_DAY
        for created, resolved in pairs
        if created is not None and resolved is not None and resolved >= created
    ]
    if not durations:
        return {
            "measured_count": 0,
            "average_days": None,
            "median_days": None,
            "fastest_days": None,
            "slowest_days": None,
        }

    durations.sort()
    middle = len(durations) // 2
    median = (
        durations[middle]
        if len(durations) % 2
        else (durations[middle - 1] + durations[middle]) / 2
    )

    return {
        "measured_count": len(durations),
        "average_days": round(sum(durations) / len(durations), 2),
        "median_days": round(median, 2),
        "fastest_days": round(durations[0], 2),
        "slowest_days": round(durations[-1], 2),
    }


def get_authority_performance(authority_id: Any = None) -> dict[str, Any]:
    """Resolution timing, backlog and workload, per authority.

    Timings come from ``Incident.resolved_at``, which is stamped when the
    incident is actually declared resolved. Incidents resolved before that
    column existed have no timestamp and are reported as ``unmeasured`` rather
    than being folded into the average - a mean computed over whatever happens
    to have data is worse than a mean with a stated denominator.
    """
    created_col, resolved_col = _resolution_seconds_column()

    authority_filter = None
    if authority_id is not None:
        parsed = _parse_uuid(authority_id, "authority_id")
        if db.session.get(Authority, parsed) is None:
            raise ApiError(
                "Authority not found.", status=404, code="authority_not_found"
            )
        authority_filter = parsed

    authorities = db.session.execute(
        select(Authority.id, Authority.name, Authority.level, Authority.type)
        .where(Authority.id == authority_filter if authority_filter else True)
        .order_by(Authority.name)
    ).all()

    # One grouped query per metric, then looked up per authority in Python.
    assigned = _group_count(Incident.authority_id, Incident)
    backlog = _group_count(
        Incident.authority_id, Incident, Incident.status.in_(OPEN_INCIDENT_STATUSES)
    )
    resolved = _group_count(
        Incident.authority_id, Incident, Incident.status.in_(CLOSED_INCIDENT_STATUSES)
    )
    active_projects = _group_count(
        Project.authority_id, Project, Project.status == ProjectStatus.ACTIVE
    )
    total_projects = _group_count(Project.authority_id, Project)
    completed_projects = _group_count(
        Project.authority_id, Project, Project.status == ProjectStatus.COMPLETED
    )

    # All resolution timestamps in one query, grouped in Python.
    duration_rows = db.session.execute(
        select(Incident.authority_id, created_col, resolved_col).where(
            Incident.status.in_(CLOSED_INCIDENT_STATUSES)
        )
    ).all()
    by_authority: dict[Any, list] = {}
    for row in duration_rows:
        by_authority.setdefault(row[0], []).append((row[1], row[2]))

    rows = []
    for authority in authorities:
        pairs = by_authority.get(authority.id, [])
        timings = _summarise_durations(pairs)
        resolved_count = resolved.get(authority.id, 0)
        rows.append(
            {
                "authority_id": str(authority.id),
                "authority_name": authority.name,
                "level": authority.level.value,
                "type": authority.type.value,
                "assigned_incidents": assigned.get(authority.id, 0),
                "backlog": backlog.get(authority.id, 0),
                "resolved_incidents": resolved_count,
                "active_projects": active_projects.get(authority.id, 0),
                "total_projects": total_projects.get(authority.id, 0),
                "completed_projects": completed_projects.get(authority.id, 0),
                "resolution": {
                    **timings,
                    # The honest denominator: how many resolved incidents could
                    # not be timed at all.
                    "unmeasured_count": resolved_count - timings["measured_count"],
                },
            }
        )

    return {
        "authorities": rows,
        "count": len(rows),
        "unassigned_incidents": assigned.get(None, 0),
    }


def _group_count(column, model, *conditions) -> dict[Any, int]:
    statement = select(column, func.count()).select_from(model).group_by(column)
    if conditions:
        statement = statement.where(and_(*conditions))
    return {row[0]: row[1] for row in db.session.execute(statement).all()}


# --- category distribution -------------------------------------------------


def get_category_distribution() -> dict[str, Any]:
    """Reports and incidents broken down by category, for charts.

    Two grouped queries, then zipped against the enum so every category appears
    even at zero - a chart with a silently missing slice is a misleading chart.
    """
    report_rows = dict(
        db.session.execute(
            select(Report.category, func.count())
            .where(Report.duplicate_of_id.is_(None))
            .group_by(Report.category)
        ).all()
    )
    incident_rows = dict(
        db.session.execute(
            select(Incident.category, func.count()).group_by(Incident.category)
        ).all()
    )
    severity_rows = dict(
        db.session.execute(
            select(Incident.severity, func.count()).group_by(Incident.severity)
        ).all()
    )

    total_reports = sum(report_rows.values())
    total_incidents = sum(incident_rows.values())

    categories = []
    for member in ReportCategory:
        reports = report_rows.get(member, 0)
        incidents = incident_rows.get(member, 0)
        categories.append(
            {
                "category": member.value,
                "report_count": reports,
                "incident_count": incidents,
                "report_share": (
                    round(reports / total_reports * 100, 1) if total_reports else None
                ),
                # How many reports in this category survived verification.
                "escalation_rate": (
                    round(incidents / reports * 100, 1) if reports else None
                ),
            }
        )

    return {
        "categories": categories,
        "totals": {"reports": total_reports, "incidents": total_incidents},
        "by_severity": {
            member.value: severity_rows.get(member, 0) for member in IncidentSeverity
        },
    }


def get_trend(days: int = 30) -> dict[str, Any]:
    """Daily report and incident counts over a recent window.

    ``func.date`` renders on both backends; the arithmetic that does not
    (interval maths) is done in Python.
    """
    from datetime import timedelta

    days = max(1, min(365, _as_int(days, 30)))
    since = datetime.now(timezone.utc) - timedelta(days=days)

    def _daily(model):
        return {
            str(row[0]): row[1]
            for row in db.session.execute(
                select(func.date(model.created_at), func.count())
                .where(model.created_at >= since)
                .group_by(func.date(model.created_at))
            ).all()
        }

    report_days = _daily(Report)
    incident_days = _daily(Incident)

    series = []
    for offset in range(days, -1, -1):
        day = (datetime.now(timezone.utc) - timedelta(days=offset)).date().isoformat()
        series.append(
            {
                "date": day,
                "reports": report_days.get(day, 0),
                "incidents": incident_days.get(day, 0),
            }
        )

    return {"days": days, "series": series}
