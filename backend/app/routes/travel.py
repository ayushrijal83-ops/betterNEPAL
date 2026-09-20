"""Travel planner endpoint.

Public: knowing whether a road has a landslide on it should not require an
account, and it is the query most likely to matter to someone on a phone with
patchy signal about to set off.
"""
from __future__ import annotations

from flask import Blueprint, request

from ..services import travel_service
from ..utils.helpers import ApiError, success_response

travel_bp = Blueprint("travel", __name__, url_prefix="/travel")


def _float_arg(name: str, required: bool = True):
    raw = request.args.get(name)
    if raw is None or not str(raw).strip():
        if required:
            raise ApiError(
                f"{name} is required.",
                status=400,
                code="validation_error",
                details={name: f"{name} is required."},
            )
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ApiError(
            f"{name} must be a number.",
            status=400,
            code="validation_error",
            details={name: f"{name} must be a number."},
        ) from None


@travel_bp.get("/route")
def route():
    """Active incidents along a route, and a danger level. Public.

    Query: ``start_lat``, ``start_lng``, ``end_lat``, ``end_lng``, and an
    optional ``corridor`` in metres (default 5000).

    The response states ``is_straight_line_corridor: true`` and which
    ``method`` produced it. Both matter: this searches a band around the direct
    line between two points, not along a road, because the platform has no road
    geometry to follow. A caller that mistook it for real routing could report
    a clear road that in fact detours through a hazard.
    """
    result = travel_service.plan_route(
        start_lat=_float_arg("start_lat"),
        start_lng=_float_arg("start_lng"),
        end_lat=_float_arg("end_lat"),
        end_lng=_float_arg("end_lng"),
        corridor_metres=_float_arg("corridor", required=False),
    )
    return success_response(result)


@travel_bp.get("/plan")
def plan():
    """A structured, duration-based itinerary for a real district. Public.

    Query: ``destination`` (district name or a known city, e.g. "Pokhara"),
    ``days`` (1-30), optional ``interest`` (free text, stored/echoed only -
    never used to invent attractions).
    """
    result = travel_service.plan_trip(
        destination=request.args.get("destination"),
        days=request.args.get("days"),
        interest=request.args.get("interest"),
    )
    return success_response(result)
