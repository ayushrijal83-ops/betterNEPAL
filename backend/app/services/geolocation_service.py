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
import uuid
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


def reverse_geocode(coordinates: Coordinates) -> dict:
    """Resolve a point to its administrative area.

    Uses ``ST_Contains`` against imported boundary polygons. Returns a
    structured unresolved result - never an invented location - when PostGIS is
    absent, when no boundaries have been imported, or when the point falls
    outside every known boundary.
    """
    if not spatial_backend_available():
        return _unresolved(coordinates, REASON_NO_SPATIAL_BACKEND)

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
