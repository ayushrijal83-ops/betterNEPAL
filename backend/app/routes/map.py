"""Geographic (map) endpoints.

Read-only and public: administrative boundaries and names are public
information, and the frontend needs them before a citizen has logged in.

There are deliberately no write endpoints in this phase. Geographic data is
loaded through the authenticated-by-shell ``flask gis import-*`` CLI commands,
which require filesystem and database access rather than an HTTP session. When
an admin-facing editing API arrives it must be guarded with
``@require_roles("admin")``; see ``app/utils/decorators.py``.
"""
from __future__ import annotations

from flask import Blueprint, request

from ..gis.location import Coordinates, InvalidCoordinate
from ..services import geolocation_service
from ..utils.helpers import ApiError, success_response

map_bp = Blueprint("map", __name__, url_prefix="/map")

TRUTHY = {"1", "true", "yes", "on"}


def _wants_geometry(default: bool = False) -> bool:
    """Geometry is opt-in on list endpoints.

    A district multipolygon is large; returning 77 of them by default would
    make the common "populate a dropdown" request enormous.
    """
    raw = request.args.get("include_geometry")
    if raw is None:
        return default
    return raw.strip().lower() in TRUTHY


@map_bp.get("/districts")
def list_districts():
    return success_response(
        {
            "districts": geolocation_service.list_districts(
                province=request.args.get("province") or None,
                include_geometry=_wants_geometry(),
            )
        }
    )


@map_bp.get("/districts/<district_id>")
def get_district(district_id: str):
    # Taken as a plain string, not Flask's <uuid:...> converter, so a malformed
    # id returns a 400 explaining the problem rather than a bare 404.
    return success_response(
        {"district": geolocation_service.get_district(district_id, _wants_geometry(True))}
    )


@map_bp.get("/municipalities")
def list_municipalities():
    return success_response(
        {
            "municipalities": geolocation_service.list_municipalities(
                district_id=request.args.get("district_id") or None,
                include_geometry=_wants_geometry(),
            )
        }
    )


@map_bp.get("/municipalities/<municipality_id>")
def get_municipality(municipality_id: str):
    return success_response(
        {
            "municipality": geolocation_service.get_municipality(
                municipality_id, _wants_geometry(True)
            )
        }
    )


@map_bp.get("/reverse-geocode")
def reverse_geocode():
    """Resolve ?lat= & ?lng= to an administrative area.

    Accepts ``lng`` or ``lon`` for longitude. Returns a structured unresolved
    result rather than a guess when the data or the spatial backend is missing.
    """
    longitude = request.args.get("lng")
    if longitude is None:
        longitude = request.args.get("lon")

    try:
        coordinates = Coordinates.parse(
            latitude=request.args.get("lat"), longitude=longitude
        )
    except InvalidCoordinate as exc:
        raise ApiError(str(exc), status=400, code="invalid_coordinates") from None

    return success_response(geolocation_service.reverse_geocode(coordinates))


@map_bp.get("/coverage")
def coverage():
    """What geographic data is actually loaded.

    Exists so the frontend can tell "we have no boundaries yet" from "the
    service is broken" without parsing a failed lookup.
    """
    return success_response(geolocation_service.coverage_summary())
