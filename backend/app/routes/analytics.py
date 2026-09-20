"""Analytics, dashboard and map-feed endpoints.

Most of these are public, deliberately. Aggregate civic data - how many
problems were reported, how many were fixed, how long each district takes - is
the transparency this platform exists to provide, and a login wall around it
would defeat the point.

What makes that safe is the service layer, not this one: every public figure
comes from a query that selects named columns and aggregates in SQL, so a
reporter's identity is never fetched in the first place. See
``app/services/analytics_service.py``.

Authority performance is the exception. Per-body resolution timings are
operational management data, and publishing a public league table of named
offices by response time is an editorial decision for the platform's owners,
not a default. It is behind authority/admin until they make it.
"""
from __future__ import annotations

from flask import Blueprint, request

from ..extensions import limiter
from ..models.role import ROLE_ADMIN, ROLE_AUTHORITY
from ..services import analytics_service
from ..utils.decorators import require_roles
from ..utils.helpers import success_response

analytics_bp = Blueprint("analytics", __name__, url_prefix="/analytics")

# Generous: these are public, unauthenticated, and a live map legitimately
# polls. The cap is here to stop scraping from crowding out real readers, not
# to ration normal use.
MAP_LIMIT = "60 per minute"

TRUTHY = {"1", "true", "yes", "on"}


def _map_filters() -> dict:
    return {
        "type": request.args.get("type"),
        "category": request.args.get("category"),
        "status": request.args.get("status"),
        "district_id": request.args.get("district_id"),
        "start_date": request.args.get("start_date"),
        "end_date": request.args.get("end_date"),
        "limit": request.args.get("limit"),
        "include_inactive": (
            request.args.get("include_inactive", "").strip().lower() in TRUTHY
        ),
    }


@analytics_bp.get("/map/points")
@limiter.limit(MAP_LIMIT)
def map_points():
    """Lightweight points for map rendering. Public.

    Filters: ``type`` (all|reports|incidents), ``category``, ``status``,
    ``district_id``, ``start_date``, ``end_date``, ``limit``.

    ``?format=geojson`` returns a FeatureCollection instead of a flat array;
    its coordinates are ``[longitude, latitude]`` per the spec.

    Rejected and duplicate reports, and closed incidents, are excluded unless
    ``include_inactive=true`` - a map that plots the same pothole five times
    misrepresents how much is actually wrong.
    """
    result = analytics_service.get_map_points(_map_filters())

    if (request.args.get("format") or "").strip().lower() == "geojson":
        return success_response(analytics_service.to_geojson(result))
    return success_response(result)


@analytics_bp.get("/map/districts")
@limiter.limit(MAP_LIMIT)
def map_districts():
    """Per-district report and incident density, with resolution rates. Public."""
    return success_response(analytics_service.get_district_summary())


@analytics_bp.get("/overview")
def overview():
    """Headline system metrics. Public."""
    return success_response(analytics_service.get_overview_stats())


@analytics_bp.get("/categories")
def categories():
    """Report and incident distribution by category. Public."""
    return success_response(analytics_service.get_category_distribution())


@analytics_bp.get("/trends")
def trends():
    """Daily report and incident counts over a recent window. Public."""
    return success_response(
        analytics_service.get_trend(request.args.get("days", 30))
    )


@analytics_bp.get("/authorities/performance")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def authority_performance():
    """Resolution timing, backlog and workload per authority.

    Authority and admin only; see the module docstring. ``?authority_id=``
    narrows it to one body.
    """
    return success_response(
        analytics_service.get_authority_performance(request.args.get("authority_id"))
    )
