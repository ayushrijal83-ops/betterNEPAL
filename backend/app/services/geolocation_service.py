"""Geographic lookup against the database.

This is the only layer that combines SQL with geography. ``app/gis/`` stays
pure, models stay declarative, routes stay thin.

PostGIS dependence
------------------

Listing and fetching administrative records works on any backend - those are
ordinary relational queries. Anything *spatial* (reverse geocoding a point,
converting a stored boundary to GeoJSON) needs PostGIS, and this module never
pretends otherwise: :func:`spatial_backend_available` is checked first, and
callers get an explicit "unavailable" result instead of a fabricated location.

No AI is involved in any of this. A point either falls inside an authoritative
boundary polygon or it does not; that is a database question with a definite
answer, and guessing would be worse than returning nothing.
"""
from __future__ import annotations

import json
import math
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from flask import current_app
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from ..extensions import db
from ..gis.location import Coordinates
from ..models.district import District
from ..models.municipality import Municipality
from ..utils.helpers import ApiError

# Reasons a spatial lookup could not run, surfaced to the client verbatim so
# the frontend can tell "nothing here" from "cannot answer".
REASON_NO_SPATIAL_BACKEND = "spatial_backend_unavailable"
REASON_NO_BOUNDARY_DATA = "no_boundary_data"
REASON_OUTSIDE_COVERAGE = "outside_known_boundaries"

# A resolved result that did NOT come from real polygon containment - see
# _dev_nearest_district(). Distinct from `None` (unresolved) specifically so
# nothing downstream can mistake this for a real PostGIS match.
REASON_DEV_FALLBACK = "dev_nearest_centroid_fallback"

_SPATIAL_FLAG = "_betternepal_spatial_available"


def spatial_backend_available() -> bool:
    """True only when the bound database is PostgreSQL with PostGIS installed.

    Cached per application, since the answer cannot change while the process
    is running and the probe costs a round trip.
    """
    cached = current_app.extensions.get(_SPATIAL_FLAG)
    if cached is not None:
        return cached

    available = False
    if db.engine.dialect.name == "postgresql":
        # Ask PostGIS to identify itself. The function only exists once the
        # extension is installed, so this answers both questions at once.
        try:
            db.session.execute(select(func.postgis_version()))
            available = True
        except SQLAlchemyError:
            db.session.rollback()
            available = False

    current_app.extensions[_SPATIAL_FLAG] = available
    return available


def _parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise ApiError(
            f"{field} is not a valid identifier.", status=400, code="invalid_identifier"
        ) from None


# --- geometry serialisation ------------------------------------------------


def _geometry_as_geojson(model, record_id: uuid.UUID) -> dict | None:
    """Convert a stored boundary to GeoJSON via PostGIS.

    Returns None off PostGIS or when the row has no boundary. The conversion
    runs in the database (``ST_AsGeoJSON``) rather than in Python because the
    column's binary representation is PostGIS's, not ours to decode.
    """
    if not spatial_backend_available():
        return None
    raw = db.session.execute(
        select(func.ST_AsGeoJSON(model.boundary)).where(model.id == record_id)
    ).scalar()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _serialise(record, include_geometry: bool) -> dict:
    geometry = (
        _geometry_as_geojson(type(record), record.id) if include_geometry else None
    )
    return record.to_dict(geometry=geometry)


# --- districts -------------------------------------------------------------


def list_districts(province: str | None = None, include_geometry: bool = False) -> list[dict]:
    statement = select(District).order_by(District.province, District.name)
    if province:
        statement = statement.where(District.province == province)
    records = db.session.scalars(statement).all()
    return [_serialise(record, include_geometry) for record in records]


def get_district(district_id: str, include_geometry: bool = True) -> dict:
    record = db.session.get(District, _parse_uuid(district_id, "district_id"))
    if record is None:
        raise ApiError("District not found.", status=404, code="district_not_found")
    return _serialise(record, include_geometry)


def find_district_by_name(name: str) -> District | None:
    return db.session.scalar(select(District).where(District.name == name))


# --- municipalities --------------------------------------------------------


def list_municipalities(
    district_id: str | None = None, include_geometry: bool = False
) -> list[dict]:
    statement = select(Municipality).order_by(Municipality.name)
    if district_id:
        statement = statement.where(
            Municipality.district_id == _parse_uuid(district_id, "district_id")
        )
    records = db.session.scalars(statement).all()
    return [_serialise(record, include_geometry) for record in records]


def get_municipality(municipality_id: str, include_geometry: bool = True) -> dict:
    record = db.session.get(Municipality, _parse_uuid(municipality_id, "municipality_id"))
    if record is None:
        raise ApiError(
            "Municipality not found.", status=404, code="municipality_not_found"
        )
    return _serialise(record, include_geometry)


# --- hierarchy -------------------------------------------------------------


def administrative_hierarchy(municipality: Municipality) -> dict:
    """Province -> District -> Municipality for one local unit."""
    district = municipality.district
    return {
        "province": district.province if district else None,
        "district": district.to_dict() if district else None,
        "municipality": municipality.to_dict(),
    }


# --- reverse geocoding -----------------------------------------------------


def _unresolved(coordinates: Coordinates, reason: str) -> dict:
    """A structured "could not resolve" answer.

    Explicitly not a guess: ``district`` and ``municipality`` are null and
    ``reason`` says why, so the caller can distinguish a missing dataset from a
    point genuinely outside Nepal.
    """
    return {
        "point": coordinates.to_geojson(),
        "coordinates": coordinates.as_dict(),
        "resolved": False,
        "reason": reason,
        "province": None,
        "district": None,
        "municipality": None,
    }


def _boundary_count(model) -> int:
    return db.session.scalar(
        select(func.count()).select_from(model).where(model.boundary.isnot(None))
    ) or 0


# --- dev-only jurisdiction fallback -----------------------------------------
#
# SQLite (dev/test) has no point-in-polygon support at all - not "not imported
# yet", genuinely absent, see app/gis/types.py. Without *something*, every
# report in dev resolves to "unresolved" forever, which means the real
# jurisdiction -> policy -> dispatch -> feed chain can never be exercised
# end-to-end outside a real PostgreSQL+PostGIS deployment.
#
# This is a coarse, explicit, deterministic stand-in for that one case only:
# nearest-known-district-reference-point, not a boundary lookup. It is gated
# on spatial_backend_available() being False, which is true only off
# PostgreSQL - and ProductionConfig.validate() already refuses to boot
# production on anything but PostgreSQL, so this code path is structurally
# unreachable in production. It never claims to be a real match: the reason
# code is REASON_DEV_FALLBACK, distinct from the real "resolved" reason
# (``None``), a WARNING is logged every time it fires, and a point too far
# from every known reference (150km) is left unresolved rather than forced
# onto whatever is nearest.

_DEV_FALLBACK_POINTS_FILE = (
    Path(__file__).resolve().parents[1] / "data" / "districts" / "nepal_district_reference_points_dev_fallback.json"
)
_DEV_FALLBACK_MAX_DISTANCE_METRES = 150_000


@lru_cache(maxsize=1)
def _dev_fallback_points() -> dict[str, tuple[float, float]]:
    with open(_DEV_FALLBACK_POINTS_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    return {
        name: (point["latitude"], point["longitude"])
        for name, point in raw["points"].items()
    }


def _haversine_metres(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r1, r2 = math.radians(lat1), math.radians(lat2)
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(d_lng / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(math.sqrt(a))


def _dev_nearest_district(coordinates: Coordinates) -> District | None:
    """Nearest district, by reference point, among districts actually seeded
    in this database - never a district this deployment doesn't know about,
    and never a match beyond the distance cutoff.
    """
    reference_points = _dev_fallback_points()
    candidates = db.session.scalars(select(District)).all()

    nearest: District | None = None
    nearest_distance = None
    for district in candidates:
        point = reference_points.get(district.name)
        if point is None:
            continue
        distance = _haversine_metres(coordinates.latitude, coordinates.longitude, *point)
        if nearest_distance is None or distance < nearest_distance:
            nearest, nearest_distance = district, distance

    if nearest is None or nearest_distance > _DEV_FALLBACK_MAX_DISTANCE_METRES:
        return None
    return nearest


def reverse_geocode(coordinates: Coordinates) -> dict:
    """Resolve a point to its administrative area.

    Uses ``ST_Contains`` against imported boundary polygons. Returns a
    structured unresolved result - never an invented location - when PostGIS is
    absent, when no boundaries have been imported, or when the point falls
    outside every known boundary.

    Off PostGIS (dev/test only - see ``_dev_nearest_district``), falls back to
    a coarse nearest-reference-point match instead of always reporting
    unresolved, so the real jurisdiction/policy/dispatch/feed chain has
    something to run against locally. The result says so plainly via
    ``reason=REASON_DEV_FALLBACK`` - it is never presented as a real match.
    """
    if not spatial_backend_available():
        district = _dev_nearest_district(coordinates)
        if district is None:
            return _unresolved(coordinates, REASON_NO_SPATIAL_BACKEND)
        current_app.logger.warning(
            "Jurisdiction resolved via dev nearest-centroid fallback (no PostGIS available): "
            "district=%s. This never happens in production - see ProductionConfig.validate().",
            district.name,
        )
        return {
            "point": coordinates.to_geojson(),
            "coordinates": coordinates.as_dict(),
            "resolved": True,
            "reason": REASON_DEV_FALLBACK,
            "province": district.province,
            "district": district.to_dict(),
            # No municipality-level reference points exist for this fallback -
            # inventing one would be exactly the kind of guess this function
            # exists to avoid.
            "municipality": None,
        }

    if not _boundary_count(District) and not _boundary_count(Municipality):
        return _unresolved(coordinates, REASON_NO_BOUNDARY_DATA)

    # Built by PostGIS from validated floats; no SQL is assembled by hand.
    point = func.ST_SetSRID(
        func.ST_MakePoint(coordinates.longitude, coordinates.latitude), 4326
    )

    district = db.session.scalar(
        select(District).where(
            District.boundary.isnot(None), func.ST_Contains(District.boundary, point)
        )
    )
    municipality = db.session.scalar(
        select(Municipality).where(
            Municipality.boundary.isnot(None),
            func.ST_Contains(Municipality.boundary, point),
        )
    )

    if district is None and municipality is None:
        return _unresolved(coordinates, REASON_OUTSIDE_COVERAGE)

    # A municipality's parent is authoritative for the district when both hit.
    if district is None and municipality is not None:
        district = municipality.district

    return {
        "point": coordinates.to_geojson(),
        "coordinates": coordinates.as_dict(),
        "resolved": True,
        "reason": None,
        "province": district.province if district else None,
        "district": district.to_dict() if district else None,
        "municipality": municipality.to_dict() if municipality else None,
    }


def coverage_summary() -> dict[str, Any]:
    """What geographic data actually exists, for clients and health checks."""
    return {
        "spatial_backend": spatial_backend_available(),
        "districts": db.session.scalar(select(func.count()).select_from(District)) or 0,
        "districts_with_boundary": _boundary_count(District),
        "municipalities": db.session.scalar(
            select(func.count()).select_from(Municipality)
        )
        or 0,
        "municipalities_with_boundary": _boundary_count(Municipality),
    }
