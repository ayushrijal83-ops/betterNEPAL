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


def _json_float(name: str, data: dict, required: bool = True, default: float | None = None):
    """Extract a float from JSON body."""
    val = data.get(name, default)
    if val is None:
        if required:
            raise ApiError(
                f"{name} is required.",
                status=400,
                code="validation_error",
                details={name: f"{name} is required."},
            )
        return default
    try:
        return float(val)
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


@travel_bp.post("/corridor-analysis")
def corridor_analysis():
    """Comprehensive corridor analysis with districts, incidents, and risk summary.

    Body (JSON):
    - start_lat, start_lng: origin coordinates
    - end_lat, end_lng: destination coordinates
    - corridor: buffer in metres (default 20000, max 50000)
    - transport_mode: "driving" | "public_bus" | "trekking" (default "driving")

    Returns route geometry, districts along corridor, active incidents,
    risk status (CLEAR/CAUTION/HIGH/CRITICAL), and summary.
    """
    data = request.get_json(silent=True) or {}

    result = travel_service.corridor_analysis(
        start_lat=_json_float("start_lat", data),
        start_lng=_json_float("start_lng", data),
        end_lat=_json_float("end_lat", data),
        end_lng=_json_float("end_lng", data),
        corridor_metres=_json_float("corridor", data, required=False),
        transport_mode=data.get("transport_mode", "driving"),
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


@travel_bp.post("/advisory")
def travel_advisory():
    """Generate a short AI advisory for a travel corridor.

    Body (JSON): the same corridor analysis parameters, or pass the full
    analysis result from /corridor-analysis to avoid recomputation.

    Returns a grounded advisory (max ~80 words) using ONLY the supplied facts.
    If AI is unavailable, returns a clear fallback message.
    """
    data = request.get_json(silent=True) or {}

    # If the caller already has analysis results, use them; otherwise compute.
    if "hazards" in data and "risk_status" in data:
        analysis = data
    else:
        analysis = travel_service.corridor_analysis(
            start_lat=_json_float("start_lat", data),
            start_lng=_json_float("start_lng", data),
            end_lat=_json_float("end_lat", data),
            end_lng=_json_float("end_lng", data),
            corridor_metres=_json_float("corridor", data, required=False),
            transport_mode=data.get("transport_mode", "driving"),
        )

    # Build structured facts for the AI
    facts = {
        "route": f"{analysis['route']['start'].get('name', 'Origin')} → {analysis['route']['end'].get('name', 'Destination')}",
        "distance_km": analysis["summary"]["distance_km"],
        "duration_hours": analysis["summary"]["duration_hours"],
        "districts": [d["name"] for d in analysis["districts"]],
        "district_count": analysis["summary"]["district_count"],
        "active_alerts": analysis["summary"]["active_alerts"],
        "risk_status": analysis["summary"]["risk_status"],
        "highest_severity": analysis["summary"]["highest_severity"],
        "affected_districts": analysis["affected_districts"],
        "nearest_hazard": analysis["nearest_hazard"],
    }

    advisory = _generate_ai_advisory(facts)

    return success_response({"advisory": advisory, "facts_used": facts})


def _generate_ai_advisory(facts: dict) -> dict:
    """Generate a short grounded advisory using the AI service.

    Returns dict with keys: text, generated_by ("ai" | "fallback"), timestamp.
    """
    from datetime import datetime, timezone
    from ..services import ai_service

    # Build a strict prompt with ONLY the supplied facts
    prompt = f"""You are a Nepal travel safety advisor. Summarize the supplied travel intelligence.

Use ONLY the supplied facts. Do not invent roads, incidents, weather, closures, contacts or travel conditions.

Maximum 80 words. Be direct and practical.

FACTS:
- Route: {facts['route']}
- Distance: {facts['distance_km']} km
- Estimated duration: {facts['duration_hours']} hours
- Districts on corridor: {', '.join(facts['districts']) if facts['districts'] else 'unknown'}
- Active alerts: {facts['active_alerts']}
- Corridor status: {facts['risk_status']}
- Highest severity: {facts['highest_severity'] or 'none'}
- Affected districts: {', '.join(facts['affected_districts']) if facts['affected_districts'] else 'none'}
- Nearest hazard: {facts['nearest_hazard']['title'] if facts['nearest_hazard'] else 'none'} ({facts['nearest_hazard']['severity'] if facts['nearest_hazard'] else 'N/A'}, {facts['nearest_hazard']['distance_from_route_km'] if facts['nearest_hazard'] else 'N/A'} km off route)

Write a concise travel advisory."""

    try:
        ai = ai_service.get_ai_service()
        if not ai.available:
            raise ai_service.AIUnavailable("No AI provider configured")
        text = ai.chat(prompt)
        return {
            "text": text.strip(),
            "generated_by": "ai",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        # Fallback: deterministic summary from facts
        fallback_lines = [
            f"TRAVEL ADVISORY",
            f"",
            f"Route: {facts['route']}",
            f"Distance: {facts['distance_km']} km (~{facts['duration_hours']}h)",
            f"Districts: {facts['district_count']}",
            f"Active alerts: {facts['active_alerts']}",
            f"Corridor status: {facts['risk_status']}",
        ]
        if facts["affected_districts"]:
            fallback_lines.append(f"Affected districts: {', '.join(facts['affected_districts'])}")
        if facts["nearest_hazard"]:
            h = facts["nearest_hazard"]
            fallback_lines.append(f"Nearest hazard: {h['title']} ({h['severity']}, {h['distance_from_route_km']} km off route)")
        fallback_lines.append("")
        fallback_lines.append("LIVE AI ADVISORY UNAVAILABLE")
        fallback_lines.append("Verified route intelligence remains available.")
        return {
            "text": "\n".join(fallback_lines),
            "generated_by": "fallback",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
