"""Coordinate validation and GeoJSON helpers.

Pure functions and one value object. No database access, no Flask request
handling - this module is importable and testable on its own.

Coordinate order
----------------

GeoJSON is ``[longitude, latitude]``. Humans, GPS displays and URLs almost
always say "lat, lng". Swapping the two is the single most common GIS bug, and
in Nepal it is not even obviously wrong: latitude ~28 and longitude ~84 are
both plausible-looking numbers, and a swapped pair still lands on the map -
just in the wrong hemisphere's ocean. Every function here therefore names the
order in its signature, and :class:`Coordinates` is the only thing passed
around once parsing is done.

No distance helper
------------------

Deliberately absent. PostGIS computes distance correctly on a spheroid via
``ST_Distance``/``ST_DWithin``; a hand-rolled haversine here would be a second,
less accurate source of truth that quietly disagrees with the database.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

LATITUDE_MIN, LATITUDE_MAX = -90.0, 90.0
LONGITUDE_MIN, LONGITUDE_MAX = -180.0, 180.0

# WGS84, matching app/gis/types.py.
DEFAULT_SRID = 4326

GEOJSON_POINT = "Point"
VALID_GEOMETRY_TYPES = frozenset(
    {
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
        "GeometryCollection",
    }
)


class InvalidCoordinate(ValueError):
    """Raised when a coordinate is missing, unparseable or out of range."""


def _to_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or value is None:
        # bool is an int subclass; True would otherwise validate as 1.0.
        raise InvalidCoordinate(f"{field} is required.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise InvalidCoordinate(f"{field} must be a number.") from None
    # NaN and infinities survive float() and would poison any spatial query.
    if number != number or number in (float("inf"), float("-inf")):
        raise InvalidCoordinate(f"{field} must be a finite number.")
    return number


def validate_latitude(value: Any) -> float:
    latitude = _to_float(value, "latitude")
    if not LATITUDE_MIN <= latitude <= LATITUDE_MAX:
        raise InvalidCoordinate(
            f"latitude must be between {LATITUDE_MIN} and {LATITUDE_MAX}."
        )
    return latitude


def validate_longitude(value: Any) -> float:
    longitude = _to_float(value, "longitude")
    if not LONGITUDE_MIN <= longitude <= LONGITUDE_MAX:
        raise InvalidCoordinate(
            f"longitude must be between {LONGITUDE_MIN} and {LONGITUDE_MAX}."
        )
    return longitude


@dataclass(frozen=True)
class Coordinates:
    """A validated WGS84 point.

    Constructed as ``Coordinates(latitude, longitude)`` - the human order -
    while :meth:`to_geojson` emits the GeoJSON order. Immutable, so a validated
    pair cannot be edited into an invalid one later.
    """

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "latitude", validate_latitude(self.latitude))
        object.__setattr__(self, "longitude", validate_longitude(self.longitude))

    @classmethod
    def parse(cls, latitude: Any, longitude: Any) -> "Coordinates":
        """Build from untrusted input (query string, JSON body)."""
        return cls(latitude=validate_latitude(latitude), longitude=validate_longitude(longitude))

    def to_geojson(self) -> dict[str, Any]:
        """GeoJSON Point. Note the order: longitude first."""
        return {"type": GEOJSON_POINT, "coordinates": [self.longitude, self.latitude]}

    def to_wkt(self) -> str:
        """Well-Known Text, for handing to PostGIS. Longitude first."""
        return f"POINT({self.longitude} {self.latitude})"

    def as_dict(self) -> dict[str, float]:
        """Human-facing representation, keyed so order cannot be misread."""
        return {"latitude": self.latitude, "longitude": self.longitude}


def point_to_geojson(latitude: float, longitude: float) -> dict[str, Any]:
    """Validate a lat/lng pair and return a GeoJSON Point."""
    return Coordinates.parse(latitude, longitude).to_geojson()


def coordinates_from_geojson(geometry: Any) -> Coordinates:
    """Read a GeoJSON Point back into :class:`Coordinates`."""
    if not isinstance(geometry, dict):
        raise InvalidCoordinate("geometry must be a GeoJSON object.")
    if geometry.get("type") != GEOJSON_POINT:
        raise InvalidCoordinate(f"geometry must be a {GEOJSON_POINT}.")

    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        raise InvalidCoordinate("coordinates must be a [longitude, latitude] pair.")

    longitude, latitude = coordinates[0], coordinates[1]
    return Coordinates.parse(latitude=latitude, longitude=longitude)


def is_valid_geometry_type(value: Any) -> bool:
    return isinstance(value, str) and value in VALID_GEOMETRY_TYPES
