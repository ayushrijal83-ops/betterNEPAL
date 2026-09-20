"""Travel planner: what is going wrong along a route.

Given two points, find the active incidents near the straight line between
them and score how risky the journey looks.

The line is a straight line, deliberately
--------------------------------------------

This is not routing. There is no road graph in the database - Phase 4 imported
district boundaries and nothing else - so the segment between start and end is
a *corridor of interest*, not the path a vehicle would take. A 5km buffer around
it is wide enough that the real road, which rarely departs far from the direct
line over these distances, falls inside it.

Saying that plainly matters: a caller who believes this follows the highway
would trust "no hazards" on a route that detours through a landslide. The
response therefore carries ``method`` and ``is_straight_line_corridor`` so the
limitation travels with the data.

Two backends
------------

* **PostGIS**: ``ST_DWithin`` over ``geography``, with a real LineString. True
  spheroidal distance, and the GIST index does the work.
* **Anything else**: a bounding-box prefilter on the latitude/longitude floats,
  then point-to-segment distance computed in Python. Same reasoning as the
  Phase 6 clustering fallback, and flagged the same way - every result says
  which produced it.
"""
from __future__ import annotations

import json
import math
from typing import Any

from flask import current_app
from geoalchemy2 import Geography
from sqlalchemy import cast, func, select
from sqlalchemy.orm import selectinload

from ..extensions import db
from ..gis.location import Coordinates
from ..models.district import District
from ..models.enums import IncidentSeverity, IncidentStatus
from ..models.incident import Incident
from ..services import geolocation_service, routing_service
from ..services.incident_service import _haversine_metres
from ..services.routing_service import RouteUnavailable
from ..utils.helpers import ApiError

# How far either side of the line counts as "on the route".
#
# 20km, not the 5km you might expect from a road width, because this searches a
# band around the *straight line* between two points and Nepal's highways do
# not run straight. Measured case: a critical landslide at Mugling sits 14.6km
# from the Kathmandu-Pokhara line, because the Prithvi Highway loops south
# through the Trishuli valley rather than crossing the hills. At 5km that
# hazard was invisible and the route reported LOW; at 20km it is found and the
# route reports HIGH.
#
# The cost is precision - a wider band admits hazards on genuinely different
# roads - and that is the right way round: a false alarm makes a traveller
# check, a missed landslide does not.
DEFAULT_CORRIDOR_METRES = 20000
MAX_CORRIDOR_METRES = 50000

# Refuse absurd journeys rather than scanning the whole table for them.
MAX_ROUTE_LENGTH_METRES = 1_000_000  # 1000 km; Nepal is ~885 km end to end

MAX_HAZARDS = 100

METRES_PER_DEGREE_LATITUDE = 111_320.0

# How close a district's reference point must sit to the actual route line
# (not just inside the wider corridor buffer) to count as DIRECTLY traversed
# rather than merely buffer-adjacent. Only meaningful once real route
# geometry exists (see corridor_analysis).
#
# On PostGIS with real boundary polygons imported, "directly traversed" is an
# exact polygon intersection and this number barely matters. Off PostGIS
# (this project's only environment so far - no boundary GeoJSON has ever been
# imported here), it is a proxy: distance from a district's *headquarters
# point* to the route, because there is no polygon to test against. Measured
# on the Kathmandu-Kaski route this corridor is built for: Kathmandu/Kaski
# (route endpoints) sit at 0km, Dhading and Tanahun's headquarters at
# 5.4-5.9km, then a real gap to Bhaktapur/Gorkha/Syangja at 11.6-16km. 10km
# sits in that gap. The one known false positive this accepts is a
# same-valley neighbour whose headquarters happens to sit close to the route
# without the route actually running through it (e.g. Lalitpur, adjacent to
# Kathmandu) that the buffer catches instead - a limitation to accept, not
# hide: it is exactly what ``method: "approximate"`` already discloses.
DIRECT_TRAVERSAL_METRES = 10000

# What each severity contributes to the risk score. Critical is weighted far
# above the rest on purpose: ten low-severity potholes are an annoyance, one
# critical landslide closes the road.
SEVERITY_WEIGHT = {
    IncidentSeverity.LOW: 1,
    IncidentSeverity.MEDIUM: 3,
    IncidentSeverity.HIGH: 8,
    IncidentSeverity.CRITICAL: 20,
}

# Score thresholds, chosen so each level is reachable by a realistic journey:
#
#   1 low (1)                     -> LOW        a pothole is not a warning
#   3 low (3) or 1 medium (3)     -> MODERATE   several niggles, or one real one
#   1 high (8), 2 high (16)       -> MODERATE
#   3 high (24) or 1 critical(20) -> HIGH       enough to reconsider the trip
#
# One critical incident alone reaching HIGH is deliberate: a single closed road
# is the whole answer to "is this route passable".
DANGER_THRESHOLDS = [(0, "LOW"), (3, "MODERATE"), (20, "HIGH")]

# Only unresolved problems are hazards. A resolved incident is a repaired road.
ACTIVE_STATUSES = (IncidentStatus.OPEN, IncidentStatus.IN_PROGRESS)

# Risk status labels for corridor summary
RISK_STATUS_LABELS = {
    "LOW": "CLEAR",
    "MODERATE": "CAUTION",
    "HIGH": "HIGH",
    "CRITICAL": "CRITICAL",
}


def _risk_status(danger_level: str) -> str:
    """Map danger level to risk status label."""
    return RISK_STATUS_LABELS.get(danger_level, "CLEAR")


def _find_districts_along_route(
    start: Coordinates,
    end: Coordinates,
    corridor_metres: float,
    route_coords: list[tuple[float, float]] | None = None,
) -> list[dict]:
    """Find districts that the route corridor passes through.

    ``route_coords`` is the real route polyline (lat, lng pairs) when one is
    available (see ``corridor_analysis``); without it, this behaves exactly
    as before - a straight line between ``start`` and ``end``. Uses a simple
    point-in-polygon check against district headquarters as a practical
    fallback when true boundary intersection is not available.
    """
    coords = route_coords or [
        (start.latitude, start.longitude),
        (end.latitude, end.longitude),
    ]
    districts_along = []

    if geolocation_service.spatial_backend_available():
        # Use PostGIS to find districts whose boundaries intersect the corridor
        line = func.ST_SetSRID(
            func.ST_GeomFromGeoJSON(json.dumps(_route_line_geojson(coords))), 4326
        )
        corridor_polygon = func.ST_Buffer(
            cast(line, Geography), corridor_metres
        )
        stmt = select(District).where(
            District.boundary.isnot(None),
            func.ST_Intersects(District.boundary, corridor_polygon),
        )
        districts = db.session.scalars(stmt).all()
        # A second, tighter pass identifies which of those the route line
        # itself (not just the wider buffer) actually runs through.
        direct_polygon = func.ST_Buffer(cast(line, Geography), DIRECT_TRAVERSAL_METRES)
        direct_ids = set(
            db.session.scalars(
                select(District.id).where(
                    District.id.in_([d.id for d in districts]),
                    func.ST_Intersects(District.boundary, direct_polygon),
                )
            ).all()
        )
        for d in districts:
            districts_along.append(
                {
                    "id": str(d.id),
                    "name": d.name,
                    "name_ne": d.name_ne,
                    "province": d.province,
                    "code": d.code,
                    "headquarters": d.headquarters,
                    "latitude": d.latitude,
                    "longitude": d.longitude,
                    "directly_traversed": d.id in direct_ids,
                }
            )
    else:
        # Fallback: find districts whose headquarters are within corridor of
        # the route polyline.
        all_districts = db.session.scalars(
            select(District).where(
                District.latitude.isnot(None), District.longitude.isnot(None)
            )
        ).all()
        for d in all_districts:
            distance = _distance_to_polyline_metres((d.latitude, d.longitude), coords)
            if distance <= corridor_metres:
                districts_along.append(
                    {
                        "id": str(d.id),
                        "name": d.name,
                        "name_ne": d.name_ne,
                        "province": d.province,
                        "code": d.code,
                        "headquarters": d.headquarters,
                        "latitude": d.latitude,
                        "longitude": d.longitude,
                        "distance_from_route_km": round(distance / 1000, 1),
                        "directly_traversed": distance <= DIRECT_TRAVERSAL_METRES,
                    }
                )

    # Sort by distance along the route (approximate using projection parameter)
    def project_param(d):
        mean_lat = math.radians(
            (start.latitude + end.latitude + d["latitude"]) / 3
        )
        scale = math.cos(mean_lat)
        ax = start.longitude * scale * METRES_PER_DEGREE_LATITUDE
        ay = start.latitude * METRES_PER_DEGREE_LATITUDE
        bx = end.longitude * scale * METRES_PER_DEGREE_LATITUDE
        by = end.latitude * METRES_PER_DEGREE_LATITUDE
        px = d["longitude"] * scale * METRES_PER_DEGREE_LATITUDE
        py = d["latitude"] * METRES_PER_DEGREE_LATITUDE
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return 0
        t = max(
            0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy))
        )
        return t

    districts_along.sort(key=project_param)
    return districts_along


def _danger_level(score: int) -> str:
    level = "LOW"
    for threshold, name in DANGER_THRESHOLDS:
        if score >= threshold:
            level = name
    return level


def _distance_to_segment_metres(
    point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]
) -> float:
    """Shortest distance from a point to a line segment, in metres.

    Uses an equirectangular projection centred on the segment: over a few
    hundred kilometres the error is well under the 5km corridor, and it avoids
    the cost and complexity of a proper geodesic cross-track calculation for a
    result that is already an approximation of a road.
    """
    mean_lat = math.radians((start[0] + end[0] + point[0]) / 3)
    scale = math.cos(mean_lat)

    def project(coordinate):
        return (
            coordinate[1] * scale * METRES_PER_DEGREE_LATITUDE,
            coordinate[0] * METRES_PER_DEGREE_LATITUDE,
        )

    px, py = project(point)
    ax, ay = project(start)
    bx, by = project(end)

    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        # Degenerate segment: start and end are the same place.
        return _haversine_metres(point[0], point[1], start[0], start[1])

    # Projection parameter, clamped so it lands on the segment rather than the
    # infinite line - an incident "before" the start is not on the route.
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    nearest = (ax + t * dx, ay + t * dy)
    return math.hypot(px - nearest[0], py - nearest[1])


def _distance_to_polyline_metres(
    point: tuple[float, float], route_coords: list[tuple[float, float]]
) -> float:
    """Shortest distance from a point to a multi-point route (each leg via
    :func:`_distance_to_segment_metres`). Falls back cleanly to a single
    segment when the route is just start/end."""
    if len(route_coords) < 2:
        return _haversine_metres(point[0], point[1], *route_coords[0])
    return min(
        _distance_to_segment_metres(point, route_coords[i], route_coords[i + 1])
        for i in range(len(route_coords) - 1)
    )


def _route_line_geojson(route_coords: list[tuple[float, float]]) -> dict[str, Any]:
    """GeoJSON LineString from (lat, lng) pairs - note the lng/lat flip."""
    return {
        "type": "LineString",
        "coordinates": [[lng, lat] for lat, lng in route_coords],
    }


def _bounding_box(
    start: Coordinates, end: Coordinates, corridor_metres: float
) -> tuple[float, float, float, float]:
    """A box containing the segment plus its corridor, for SQL prefiltering."""
    lat_pad = corridor_metres / METRES_PER_DEGREE_LATITUDE
    mean_lat = math.radians((start.latitude + end.latitude) / 2)
    cos_lat = math.cos(mean_lat)
    lng_pad = (
        180.0
        if abs(cos_lat) < 1e-9
        else min(180.0, corridor_metres / (METRES_PER_DEGREE_LATITUDE * abs(cos_lat)))
    )

    return (
        min(start.latitude, end.latitude) - lat_pad,
        max(start.latitude, end.latitude) + lat_pad,
        min(start.longitude, end.longitude) - lng_pad,
        max(start.longitude, end.longitude) + lng_pad,
    )


def _incident_query():
    return (
        select(Incident)
        .options(selectinload(Incident.district), selectinload(Incident.authority))
        .where(Incident.status.in_(ACTIVE_STATUSES))
    )


def plan_route(
    start_lat: Any,
    start_lng: Any,
    end_lat: Any,
    end_lng: Any,
    corridor_metres: float | None = None,
) -> dict[str, Any]:
    """Hazards along a route, and how dangerous it looks.

    Coordinates are validated by :class:`Coordinates`, so a swapped or
    out-of-range pair is refused before any query runs.
    """
    from ..gis.location import InvalidCoordinate

    try:
        start = Coordinates.parse(latitude=start_lat, longitude=start_lng)
        end = Coordinates.parse(latitude=end_lat, longitude=end_lng)
    except InvalidCoordinate as exc:
        raise ApiError(str(exc), status=400, code="invalid_coordinates") from None

    corridor = float(corridor_metres or DEFAULT_CORRIDOR_METRES)
    corridor = max(100.0, min(corridor, MAX_CORRIDOR_METRES))

    route_length = _haversine_metres(
        start.latitude, start.longitude, end.latitude, end.longitude
    )
    if route_length > MAX_ROUTE_LENGTH_METRES:
        raise ApiError(
            "That route is too long to analyse. Split it into shorter legs.",
            status=400,
            code="route_too_long",
            details={"length_km": round(route_length / 1000, 1)},
        )

    hazards: list[tuple[Incident, float]] = []

    if geolocation_service.spatial_backend_available():
        # A real LineString, buffered by the corridor on the spheroid.
        line = func.ST_SetSRID(
            func.ST_MakeLine(
                func.ST_MakePoint(start.longitude, start.latitude),
                func.ST_MakePoint(end.longitude, end.latitude),
            ),
            4326,
        )
        statement = _incident_query().where(
            Incident.location.isnot(None),
            func.ST_DWithin(
                cast(Incident.location, Geography), cast(line, Geography), corridor
            ),
        )
        records = db.session.scalars(statement.limit(MAX_HAZARDS)).all()
        method = "postgis"
        hazards = [
            (
                incident,
                _distance_to_segment_metres(
                    (incident.latitude, incident.longitude),
                    (start.latitude, start.longitude),
                    (end.latitude, end.longitude),
                ),
            )
            for incident in records
        ]
    else:
        min_lat, max_lat, min_lng, max_lng = _bounding_box(start, end, corridor)
        statement = _incident_query().where(
            Incident.latitude.between(min_lat, max_lat),
            Incident.longitude.between(min_lng, max_lng),
        )
        # The box is a prefilter: its corners sit outside the corridor, so each
        # candidate is re-checked against the true distance to the segment.
        for incident in db.session.scalars(statement).all():
            distance = _distance_to_segment_metres(
                (incident.latitude, incident.longitude),
                (start.latitude, start.longitude),
                (end.latitude, end.longitude),
            )
            if distance <= corridor:
                hazards.append((incident, distance))
        method = "approximate"

    hazards.sort(key=lambda pair: pair[1])
    hazards = hazards[:MAX_HAZARDS]

    score = sum(SEVERITY_WEIGHT.get(incident.severity, 1) for incident, _ in hazards)
    by_severity = {member.value: 0 for member in IncidentSeverity}
    for incident, _ in hazards:
        by_severity[incident.severity.value] += 1

    return {
        "route": {
            "start": start.as_dict(),
            "end": end.as_dict(),
            "length_km": round(route_length / 1000, 2),
            "corridor_metres": corridor,
            # Stated in the payload so no client mistakes this for road routing.
            "is_straight_line_corridor": True,
        },
        "danger_level": _danger_level(score),
        "danger_score": score,
        "hazard_count": len(hazards),
        "by_severity": by_severity,
        # "postgis" or "approximate", so a caller can tell an exact answer from
        # the fallback.
        "method": method,
        "hazards": [
            {
                **incident.to_dict(),
                "distance_from_route_metres": round(distance, 1),
            }
            for incident, distance in hazards
        ],
    }


# --- trip planner ------------------------------------------------------------
#
# Destination + duration + interest -> a structured itinerary skeleton.
#
# There is no attractions/points-of-interest dataset in this project - only
# administrative boundaries, incidents, and the rivers/roads research file
# (see geography_reference_service). So this does not invent day-by-day
# attractions: the itinerary is a generic, duration-shaped framework, and
# everything specific in the response - rivers, road corridors, active
# hazards - comes from real project data. `is_curated_recommendations: false`
# says so explicitly, rather than letting a generic framework read as if it
# were locally researched.

MAX_TRIP_DAYS = 30


def _itinerary_skeleton(destination_name: str, days: int) -> list[dict[str, Any]]:
    days = max(1, min(days, MAX_TRIP_DAYS))
    if days == 1:
        return [{"day": 1, "focus": f"Day trip to {destination_name}"}]

    plan = [{"day": 1, "focus": f"Arrival and orientation in {destination_name}"}]
    for d in range(2, days):
        plan.append({"day": d, "focus": f"Explore {destination_name} and the surrounding area"})
    plan.append({"day": days, "focus": "Final day and departure"})
    return plan


def plan_trip(destination: Any, days: Any, interest: Any = None) -> dict[str, Any]:
    """Build a structured, duration-based itinerary for a real Nepali district.

    ``destination`` is matched to a real administrative district (directly, or
    via a small known-city alias such as "Pokhara" -> Kaski); an unmatched
    name is a 404, never a guess. Safety context comes from active incidents
    actually recorded against that district - the same severity weighting the
    route hazard-checker above uses - not from AI or invented conditions.
    """
    from . import geography_reference_service as geo_ref
    from ..utils.helpers import ApiError

    if not isinstance(destination, str) or not destination.strip():
        raise ApiError(
            "destination is required.", status=400, code="validation_error",
            details={"destination": "destination is required."},
        )
    try:
        days_int = int(days)
    except (TypeError, ValueError):
        raise ApiError(
            "days must be a whole number.", status=400, code="validation_error",
            details={"days": "days must be a whole number."},
        ) from None
    if not 1 <= days_int <= MAX_TRIP_DAYS:
        raise ApiError(
            f"days must be between 1 and {MAX_TRIP_DAYS}.", status=400, code="validation_error",
            details={"days": f"days must be between 1 and {MAX_TRIP_DAYS}."},
        )

    district_name = geo_ref.resolve_district_name(destination)
    if district_name is None:
        raise ApiError(
            f"'{destination}' does not match a known Nepali district.",
            status=404,
            code="destination_not_found",
            details={"destination": "Try a district name, e.g. 'Kaski' or 'Kathmandu'."},
        )

    district = db.session.scalar(select(District).where(District.name == district_name))

    # Real active hazards in this district only - reuses the same severity
    # weighting as plan_route() above, so "how risky does this look" answers
    # the same way whether you ask by two points or by district.
    hazards: list[Incident] = []
    if district is not None:
        hazards = list(
            db.session.scalars(
                _incident_query().where(Incident.district_id == district.id)
            ).all()
        )
    score = sum(SEVERITY_WEIGHT.get(incident.severity, 1) for incident in hazards)

    interest_text = interest.strip() if isinstance(interest, str) and interest.strip() else None

    return {
        "destination": {
            "query": destination,
            "district": district_name,
            "province": district.province if district else None,
            "district_known_to_platform": district is not None,
        },
        "duration_days": days_int,
        "interest": interest_text,
        "itinerary": _itinerary_skeleton(destination.strip(), days_int),
        "is_curated_recommendations": False,
        "geography": {
            "major_rivers": geo_ref.rivers_for_district(district_name),
            "road_corridors": geo_ref.road_corridors_for_district(district_name),
            "province_road_authority": geo_ref.province_road_authority(
                district.province if district else None
            ),
            "source": geo_ref.dataset_meta(),
        },
        "safety_context": {
            "active_hazard_count": len(hazards),
            "danger_level": _danger_level(score),
            "hazards": [incident.to_dict() for incident in hazards[:MAX_HAZARDS]],
            "note": (
                "Reflects reports already verified into incidents for this district. "
                "Absence of a hazard here is not a guarantee of safety."
            ),
        },
    }


def corridor_analysis(
    start_lat: Any,
    start_lng: Any,
    end_lat: Any,
    end_lng: Any,
    corridor_metres: float | None = None,
    transport_mode: str = "driving",
) -> dict[str, Any]:
    """Comprehensive corridor analysis with districts, incidents, and risk summary.

    This is the main endpoint for the Travel Intelligence feature.
    """
    from ..gis.location import InvalidCoordinate

    try:
        start = Coordinates.parse(latitude=start_lat, longitude=start_lng)
        end = Coordinates.parse(latitude=end_lat, longitude=end_lng)
    except InvalidCoordinate as exc:
        raise ApiError(str(exc), status=400, code="invalid_coordinates") from None

    corridor = float(corridor_metres or DEFAULT_CORRIDOR_METRES)
    corridor = max(100.0, min(corridor, MAX_CORRIDOR_METRES))

    route_length = _haversine_metres(
        start.latitude, start.longitude, end.latitude, end.longitude
    )
    if route_length > MAX_ROUTE_LENGTH_METRES:
        raise ApiError(
            "That route is too long to analyse. Split it into shorter legs.",
            status=400,
            code="route_too_long",
            details={"length_km": round(route_length / 1000, 1)},
        )

    # Try a real route first; fall back to the straight-line corridor on any
    # failure (unconfigured provider, timeout, no route found). Either way the
    # response says which one actually ran - route.provider and
    # is_straight_line_corridor - so a caller never mistakes one for the
    # other.
    route_provider_name = "straight_line_fallback"
    route_geometry: dict[str, Any] | None = None
    real_distance_km: float | None = None
    real_duration_minutes: float | None = None

    provider = routing_service.get_route_provider()
    if provider is not None:
        try:
            result = provider.get_route(start, end, transport_mode)
        except RouteUnavailable as exc:
            current_app.logger.warning("Route provider unavailable: %s", exc)
        else:
            route_provider_name = result.provider
            route_geometry = result.geometry
            real_distance_km = result.distance_km
            real_duration_minutes = result.duration_minutes

    route_coords: list[tuple[float, float]]
    if route_geometry is not None:
        # GeoJSON coordinates are [lng, lat]; every helper here works in
        # (lat, lng) pairs.
        route_coords = [(lat, lng) for lng, lat in route_geometry["coordinates"]]
    else:
        route_coords = [
            (start.latitude, start.longitude),
            (end.latitude, end.longitude),
        ]
    is_straight_line = route_geometry is None

    line_geojson = _route_line_geojson(route_coords)

    # Get hazards along the route
    hazards: list[tuple[Incident, float]] = []

    if geolocation_service.spatial_backend_available():
        line = func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(line_geojson)), 4326)
        statement = _incident_query().where(
            Incident.location.isnot(None),
            func.ST_DWithin(
                cast(Incident.location, Geography), cast(line, Geography), corridor
            ),
        )
        records = db.session.scalars(statement.limit(MAX_HAZARDS)).all()
        method = "postgis"
        hazards = [
            (
                incident,
                _distance_to_polyline_metres(
                    (incident.latitude, incident.longitude), route_coords
                ),
            )
            for incident in records
        ]
    else:
        min_lat, max_lat, min_lng, max_lng = _bounding_box(start, end, corridor)
        statement = _incident_query().where(
            Incident.latitude.between(min_lat, max_lat),
            Incident.longitude.between(min_lng, max_lng),
        )
        for incident in db.session.scalars(statement).all():
            distance = _distance_to_polyline_metres(
                (incident.latitude, incident.longitude), route_coords
            )
            if distance <= corridor:
                hazards.append((incident, distance))
        method = "approximate"

    hazards.sort(key=lambda pair: pair[1])
    hazards = hazards[:MAX_HAZARDS]

    score = sum(SEVERITY_WEIGHT.get(incident.severity, 1) for incident, _ in hazards)
    by_severity = {member.value: 0 for member in IncidentSeverity}
    for incident, _ in hazards:
        by_severity[incident.severity.value] += 1

    danger_level = _danger_level(score)
    risk_status = _risk_status(danger_level)

    # Find districts along the route
    districts_along = _find_districts_along_route(
        start, end, corridor, route_coords=route_coords
    )
    directly_traversed = [d for d in districts_along if d.get("directly_traversed")]
    buffer_adjacent = [d for d in districts_along if not d.get("directly_traversed")]

    # Build hazard summaries with district info
    hazard_summaries = []
    for incident, distance in hazards:
        hazard_dict = incident.to_dict()
        hazard_dict["distance_from_route_metres"] = round(distance, 1)
        hazard_dict["distance_from_route_km"] = round(distance / 1000, 2)
        hazard_summaries.append(hazard_dict)

    # Determine affected districts from hazards
    affected_district_names = set()
    for incident, _ in hazards:
        if incident.district:
            affected_district_names.add(incident.district.name)

    # Find nearest hazard if any
    nearest_hazard = None
    if hazards:
        nearest = hazards[0]
        nearest_hazard = {
            "title": nearest[0].title,
            "severity": nearest[0].severity.value,
            "category": nearest[0].category.value,
            "district": nearest[0].district.name if nearest[0].district else None,
            "distance_from_route_km": round(nearest[1] / 1000, 2),
        }

    # Real OSRM figures win when we have them; the haversine estimate (and
    # its crude 40km/h assumption) stays only as the fallback it always was.
    final_distance_km = real_distance_km if real_distance_km is not None else round(route_length / 1000, 1)
    final_duration_hours = (
        round(real_duration_minutes / 60, 1)
        if real_duration_minutes is not None
        else round(route_length / 1000 / 40, 1)
    )

    return {
        "route": {
            "start": start.as_dict(),
            "end": end.as_dict(),
            "length_km": round(route_length / 1000, 2),
            "corridor_metres": corridor,
            "is_straight_line_corridor": is_straight_line,
            "transport_mode": transport_mode,
            "provider": route_provider_name,
            "geometry": route_geometry,
        },
        "districts": districts_along,
        "district_count": len(districts_along),
        "districts_by_proximity": {
            "directly_traversed": directly_traversed,
            "buffer_adjacent": buffer_adjacent,
        },
        "danger_level": danger_level,
        "risk_status": risk_status,
        "danger_score": score,
        "hazard_count": len(hazards),
        "by_severity": by_severity,
        "method": method,
        "hazards": hazard_summaries,
        "affected_districts": sorted(list(affected_district_names)),
        "nearest_hazard": nearest_hazard,
        "summary": {
            "distance_km": final_distance_km,
            "duration_hours": final_duration_hours,
            "district_count": len(districts_along),
            "active_alerts": len(hazards),
            "risk_status": risk_status,
            "highest_severity": (
                max(
                    [h["severity"] for h in hazard_summaries],
                    key=lambda s: ["low", "medium", "high", "critical"].index(s),
                )
                if hazard_summaries
                else None
            ),
        },
    }
