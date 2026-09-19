"""AI analysis endpoints.

Mounted under ``/reports`` alongside the Phase 5 blueprint, because these act
on reports; they live in their own module because they are the only routes that
reach an external model.

All of them require authority or admin. Two reasons, and the second is the one
that matters: analysis is a judgement aid for the people who verify reports,
and every call costs money to a third-party provider. A public endpoint that
bills per request is a denial-of-wallet waiting to happen.

Nothing here writes to a report's authoritative fields on the AI's say-so. The
analyze endpoint stores suggestions; the duplicate endpoints return candidates.
Only ``mark-duplicate``, an explicit human action, changes a report's status.
"""
from __future__ import annotations

from flask import Blueprint, request

from ..extensions import limiter
from ..models.role import ROLE_ADMIN, ROLE_AUTHORITY
from ..services import report_analysis_service
from ..utils.decorators import require_roles
from ..utils.helpers import ApiError, success_response
from ..utils.permissions import current_user
from ..utils.validators import ValidationErrors, _as_text, require_json

ai_bp = Blueprint("ai", __name__, url_prefix="/reports")

# Every call here occupies a model. On a self-hosted node that means a GPU or a
# CPU core is busy for the duration, so this protects capacity rather than a
# bill - one impatient dashboard must not starve everyone else's analysis.
AI_LIMIT = "20 per minute"


@ai_bp.post("/<report_id>/analyze")
@limiter.limit(AI_LIMIT)
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def analyze(report_id: str):
    """Classify a report with the AI and store the suggestion.

    Returns ``applied_to_report: false`` explicitly, so no client mistakes a
    suggestion for a change to the record.
    """
    return success_response(report_analysis_service.analyze_report(report_id))


@ai_bp.get("/<report_id>/potential-duplicates")
@limiter.limit(AI_LIMIT)
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def potential_duplicates(report_id: str):
    """Nearby reports the AI believes describe the same problem.

    ``?radius=`` overrides the configured default in metres. The response
    reports which proximity method was used and whether the AI was reachable,
    so a proximity-only answer is never mistaken for a verified one.
    """
    raw_radius = request.args.get("radius")
    radius = None
    if raw_radius is not None:
        try:
            radius = float(raw_radius)
        except (TypeError, ValueError):
            raise ApiError(
                "radius must be a number of metres.", status=400, code="invalid_radius"
            ) from None
        if radius <= 0:
            raise ApiError(
                "radius must be greater than zero.", status=400, code="invalid_radius"
            )

    return success_response(
        report_analysis_service.find_potential_duplicates(report_id, radius)
    )


@ai_bp.post("/<report_id>/mark-duplicate")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def mark_duplicate(report_id: str):
    """Fold this report into another. A human decision, recorded as such."""
    data = require_json(request.get_json(silent=True))
    errors = ValidationErrors()

    duplicate_of_id = _as_text(data, "duplicate_of_id")
    if not duplicate_of_id:
        errors.add("duplicate_of_id", "duplicate_of_id is required.")
    errors.raise_if_any()

    report = report_analysis_service.mark_as_duplicate(
        report_id=report_id,
        duplicate_of_id=duplicate_of_id,
        user_id=current_user().id,
        reason=_as_text(data, "reason") or None,
    )
    return success_response({"report": report.to_dict()})


@ai_bp.post("/<report_id>/unmark-duplicate")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def unmark_duplicate(report_id: str):
    """Undo a duplicate decision, returning the report to review."""
    report = report_analysis_service.unmark_duplicate(report_id)
    return success_response({"report": report.to_dict()})


@ai_bp.get("/<report_id>/duplicates")
def duplicates_of(report_id: str):
    """Reports folded into this one. Public.

    Read-only and costs nothing, so it needs no role check: how many people
    independently reported the same problem is public interest information.
    """
    duplicates = report_analysis_service.get_duplicates_of(report_id)
    return success_response(
        {
            "report_id": report_id,
            "duplicates": [report.to_dict() for report in duplicates],
            "count": len(duplicates),
        }
    )


@ai_bp.get("/ai-statistics")
def statistics():
    """Analysis coverage and provider availability. Public.

    Declared here rather than on the reports blueprint so the literal path is
    matched before ``/<report_id>``.
    """
    return success_response(report_analysis_service.get_analysis_statistics())
