"""Applying AI suggestions to reports.

The division of labour this phase exists to enforce:

* **AI understands** - reads free text and offers a category, a severity and an
  opinion on whether two reports describe one problem.
* **GIS locates** - :func:`find_nearby_reports` from Phase 6 decides what is
  physically close enough to be worth comparing at all.
* **The database decides** - nothing here writes to ``category``, ``status`` or
  any other authoritative field on the strength of a model's answer.
* **Humans verify** - a duplicate is only ever marked by a person, through
  :func:`mark_as_duplicate`.

Duplicate detection deliberately runs spatial filtering *first*. Proximity is a
cheap, deterministic fact; asking a model to compare every pair of reports in
the country would be expensive, slow, and no more correct.
"""
from __future__ import annotations

import uuid
from typing import Any

from flask import current_app

from ..extensions import db
from ..models.base import utcnow
from ..models.enums import ReportStatus
from ..models.report import Report
from ..utils.helpers import ApiError
from .ai_service import AIUnavailable, get_ai_service
from .incident_service import find_nearby_reports

# Below this, a model's "yes" is not worth putting in front of a human.
MIN_DUPLICATE_CONFIDENCE = 0.5

# Cap on how many nearby reports get sent to the model for one request. Each
# comparison is a separate billable call, so an unbounded loop over a dense
# cluster is a cost incident waiting to happen.
MAX_COMPARISONS = 10


def _parse_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise ApiError(
            f"{field} is not a valid identifier.", status=400, code="invalid_identifier"
        ) from None


def _load_report(report_id: Any) -> Report:
    report = db.session.get(Report, _parse_uuid(report_id, "report_id"))
    if report is None:
        raise ApiError("Report not found.", status=404, code="report_not_found")
    return report


def _unavailable(exc: AIUnavailable) -> ApiError:
    """Turn a missing or unreachable provider into an honest 503.

    503 rather than 500: nothing is broken, a capability is simply not
    configured, and the caller can reasonably retry once it is.
    """
    return ApiError(str(exc), status=503, code="ai_unavailable")


def analyze_report(report_id: Any) -> dict[str, Any]:
    """Ask the AI to classify a report, and store what it said.

    The result lands in ``ai_metadata`` only. ``category``, ``status`` and
    everything else the platform treats as fact are left exactly as they were -
    a suggestion is evidence for a human decision, not the decision.
    """
    report = _load_report(report_id)
    service = get_ai_service()

    try:
        analysis = service.analyze_report_text(report.title, report.description)
    except AIUnavailable as exc:
        raise _unavailable(exc) from None

    metadata = dict(report.ai_metadata or {})
    metadata["analysis"] = {
        **analysis.to_dict(),
        "analyzed_at": utcnow().isoformat(),
        "provider": service.name,
    }
    # Reassigned rather than mutated: SQLAlchemy does not track in-place edits
    # to a plain JSON column, so an in-place update would never be written.
    report.ai_metadata = metadata
    db.session.commit()

    return {
        "report_id": str(report.id),
        "analysis": metadata["analysis"],
        # Named so no client mistakes this for a change to the record.
        "applied_to_report": False,
        "current_category": report.category.value,
    }


def find_potential_duplicates(
    report_id: Any, radius_meters: float | None = None
) -> dict[str, Any]:
    """Nearby reports the AI believes describe the same problem.

    Spatial first, then semantic. Returns candidates with confidence scores for
    a human to accept or dismiss; nothing is marked automatically.
    """
    report = _load_report(report_id)
    service = get_ai_service()

    radius = radius_meters or current_app.config.get("AI_DUPLICATE_RADIUS_METRES", 100)

    nearby = find_nearby_reports(
        lat=report.latitude,
        lng=report.longitude,
        radius_meters=radius,
        exclude_linked=False,
        exclude_report_ids=[report.id],
        limit=MAX_COMPARISONS,
    )

    candidates: list[dict[str, Any]] = []
    compared = 0
    ai_failed: str | None = None

    for entry in nearby["reports"]:
        other = db.session.get(Report, uuid.UUID(entry["id"]))
        if other is None or other.duplicate_of_id is not None:
            # Already folded into something else; not a candidate master.
            continue

        verdict = None
        if service.available and ai_failed is None:
            try:
                verdict = service.compare_reports(report, other)
                compared += 1
            except AIUnavailable as exc:
                # Degrade to proximity-only rather than failing the request:
                # "here is what is nearby" is still useful without the model.
                ai_failed = str(exc)

        if verdict is None:
            continue
        if verdict.is_duplicate and verdict.confidence >= MIN_DUPLICATE_CONFIDENCE:
            candidates.append(
                {
                    "report": other.to_dict(),
                    "distance_meters": entry["distance_meters"],
                    "ai": verdict.to_dict(),
                }
            )

    candidates.sort(key=lambda row: row["ai"]["confidence"], reverse=True)

    return {
        "report_id": str(report.id),
        "radius_meters": nearby["radius_meters"],
        # Phase 6's honesty flag: "postgis" or "approximate".
        "proximity_method": nearby["method"],
        "nearby_count": nearby["count"],
        "compared_count": compared,
        "ai_available": service.available and ai_failed is None,
        "ai_error": ai_failed,
        "candidates": candidates,
        "count": len(candidates),
    }


def mark_as_duplicate(
    report_id: Any, duplicate_of_id: Any, user_id: Any, reason: str | None = None
) -> Report:
    """Fold one report into another, as a human decision.

    Sets ``duplicate_of_id`` and rejects the report, recording who decided and
    why. The original is never deleted - five citizens reporting one pothole is
    itself a signal about how bad the pothole is.
    """
    report = _load_report(report_id)
    master = db.session.get(Report, _parse_uuid(duplicate_of_id, "duplicate_of_id"))
    if master is None:
        raise ApiError(
            "The report to merge into was not found.",
            status=404,
            code="master_report_not_found",
        )

    if report.id == master.id:
        raise ApiError(
            "A report cannot be a duplicate of itself.",
            status=409,
            code="self_duplicate",
        )

    if report.duplicate_of_id is not None:
        raise ApiError(
            "This report is already marked as a duplicate.",
            status=409,
            code="already_duplicate",
            details={"duplicate_of_id": str(report.duplicate_of_id)},
        )

    if master.duplicate_of_id is not None:
        # Chains turn a flat "these are the same" into a graph that every
        # traversal has to walk. Point at the master directly instead.
        raise ApiError(
            "That report is itself a duplicate; merge into the original instead.",
            status=409,
            code="master_is_duplicate",
            details={"merge_into_id": str(master.duplicate_of_id)},
        )

    if report.status == ReportStatus.VERIFIED_AS_INCIDENT or report.incident_id:
        # Rejecting it now would leave the incident it evidenced standing on
        # a rejected report. Unlink it from the incident first.
        raise ApiError(
            "This report has already been verified into an incident and cannot "
            "be marked as a duplicate.",
            status=409,
            code="report_already_verified",
        )

    if report.status == ReportStatus.REJECTED:
        raise ApiError(
            "This report has already been rejected.",
            status=409,
            code="report_already_rejected",
        )

    report.duplicate_of_id = master.id
    report.status = ReportStatus.REJECTED

    metadata = dict(report.ai_metadata or {})
    metadata["duplicate_decision"] = {
        "duplicate_of_id": str(master.id),
        "reason": (reason or "Marked as a duplicate of an existing report.")[:500],
        "decided_by_id": str(_parse_uuid(user_id, "user_id")),
        "decided_at": utcnow().isoformat(),
    }
    report.ai_metadata = metadata

    db.session.commit()
    return report


def unmark_duplicate(report_id: Any) -> Report:
    """Undo a duplicate decision, returning the report to review.

    Humans mis-merge; without this a wrongly folded report would be stuck
    rejected with no way back.
    """
    report = _load_report(report_id)
    if report.duplicate_of_id is None:
        raise ApiError(
            "This report is not marked as a duplicate.",
            status=409,
            code="not_a_duplicate",
        )

    report.duplicate_of_id = None
    report.status = ReportStatus.UNDER_REVIEW

    metadata = dict(report.ai_metadata or {})
    metadata.pop("duplicate_decision", None)
    report.ai_metadata = metadata or None

    db.session.commit()
    return report


def get_duplicates_of(report_id: Any) -> list[Report]:
    """Every report folded into this one."""
    report = _load_report(report_id)
    return list(report.duplicates)


def get_analysis_statistics() -> dict[str, Any]:
    """How much of the corpus has been analysed, and how much is duplicated."""
    from sqlalchemy import func, select

    total = db.session.scalar(select(func.count()).select_from(Report)) or 0
    analysed = db.session.scalar(
        select(func.count()).select_from(Report).where(Report.ai_metadata.isnot(None))
    ) or 0
    duplicates = db.session.scalar(
        select(func.count()).select_from(Report).where(Report.duplicate_of_id.isnot(None))
    ) or 0

    return {
        "ai_available": get_ai_service().available,
        "provider": get_ai_service().name,
        "total_reports": total,
        "analyzed_reports": analysed,
        "unanalyzed_reports": total - analysed,
        "duplicate_reports": duplicates,
    }
