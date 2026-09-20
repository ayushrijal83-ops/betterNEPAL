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

import math
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import cast, func, select
from sqlalchemy.orm import selectinload

from ..extensions import db
from ..gis.location import Coordinates
from ..models.district import District
from ..models.enums import IncidentSeverity, IncidentStatus
from ..models.incident import Incident
from ..services import geolocation_service
from ..services.incident_service import _haversine_metres
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
