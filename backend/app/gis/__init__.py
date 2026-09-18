"""Geographic utilities.

Pure geometry/coordinate helpers and dataset validation. No database queries
and no Flask request handling live here - those belong to
``app/services/geolocation_service.py`` and ``app/routes/map.py``.
"""
from .datasets import DatasetError
from .location import (
    Coordinates,
    InvalidCoordinate,
    coordinates_from_geojson,
    point_to_geojson,
    validate_latitude,
    validate_longitude,
)
from .types import DEFAULT_SRID, GeometryColumn

__all__ = [
    "Coordinates",
    "DEFAULT_SRID",
    "DatasetError",
    "GeometryColumn",
    "InvalidCoordinate",
    "coordinates_from_geojson",
    "point_to_geojson",
    "validate_latitude",
    "validate_longitude",
]
