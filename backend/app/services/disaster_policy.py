"""Backend safety policy for AI disaster dispatch.

Centralized policy so thresholds can be changed without modifying the LLM service.
The AI's requires_immediate_dispatch flag is NOT sufficient on its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import current_app

from ..models.enums import DisasterSeverity, DisasterType


@dataclass
class DispatchDecision:
    """Result of evaluating whether a disaster analysis warrants dispatch."""
    should_dispatch: bool
    reason: str
    severity_met: bool
    confidence_met: bool
    type_qualifies: bool
    location_resolved: bool
    policy_details: dict[str, Any]


def _normalize_severity(value: str | None) -> DisasterSeverity | None:
    """Convert string to DisasterSeverity enum."""
    if not value:
        return None
    try:
        return DisasterSeverity(value.lower())
    except ValueError:
        return None


def _normalize_type(value: str | None) -> DisasterType | None:
    """Convert string to DisasterType enum."""
    if not value:
        return None
    try:
        return DisasterType(value.lower())
    except ValueError:
        return None


def severity_meets_threshold(severity: str | None, min_severity: str) -> bool:
    """Check if severity meets or exceeds the minimum threshold."""
    sev = _normalize_severity(severity)
    if sev is None:
        return False
    min_sev = _normalize_severity(min_severity)
    if min_sev is None:
        return False
    # Order: LOW < MODERATE < HIGH < CRITICAL
    order = {"low": 0, "moderate": 1, "high": 2, "critical": 3}
    return order[sev.value] >= order[min_sev.value]


def confidence_meets_threshold(confidence: float | None, min_confidence: float) -> bool:
    """Check if confidence meets or exceeds the minimum threshold."""
    if confidence is None:
        return False
    return confidence >= min_confidence


def disaster_type_qualifies(disaster_type: str | None) -> bool:
    """Check if the disaster type qualifies for dispatch consideration.

    'none' and invalid types never qualify.
    """
    if not disaster_type:
        return False
    dtype = _normalize_type(disaster_type)
    return dtype is not None and dtype != DisasterType.NONE


def evaluate_dispatch_policy(
    analysis,
    latitude: float | None = None,
    longitude: float | None = None,
    district_id: Any | None = None,
) -> DispatchDecision:
    """Evaluate whether a disaster analysis warrants immediate dispatch.

    All conditions must be met:
    1. AI says it's a disaster (is_disaster=True)
    2. Disaster type is not 'none' and is a recognized type
    3. Severity meets minimum threshold (configurable)
    4. Confidence meets minimum threshold (configurable)
    5. Valid coordinates provided
    6. Jurisdiction (district) successfully resolved
    """
    config = current_app.config

    min_confidence = config.get("AI_DISPATCH_MIN_CONFIDENCE", 0.85)
    min_severity = config.get("AI_DISPATCH_MIN_SEVERITY", "HIGH")

    is_disaster = getattr(analysis, "is_disaster", False)
    disaster_type = getattr(analysis, "disaster_type", None)
    severity = getattr(analysis, "severity", None)
    confidence = getattr(analysis, "confidence", None)

    type_qualifies = disaster_type_qualifies(disaster_type)
    severity_met = severity_meets_threshold(severity, min_severity)
    confidence_met = confidence_meets_threshold(confidence, min_confidence)
    location_resolved = district_id is not None

    should_dispatch = (
        is_disaster
        and type_qualifies
        and severity_met
        and confidence_met
        and location_resolved
    )

    reasons = []
    if not is_disaster:
        reasons.append("AI classification: not a disaster")
    if not type_qualifies:
        reasons.append(f"disaster type '{disaster_type}' does not qualify")
    if not severity_met:
        reasons.append(f"severity '{severity}' below minimum '{min_severity}'")
    if not confidence_met:
        confidence_text = f"{confidence:.2f}" if confidence is not None else "unavailable"
        reasons.append(f"confidence {confidence_text} below minimum {min_confidence}")
    if not location_resolved:
        reasons.append("jurisdiction could not be resolved")

    reason = "; ".join(reasons) if reasons else "All policy checks passed"

    return DispatchDecision(
        should_dispatch=should_dispatch,
        reason=reason,
        severity_met=severity_met,
        confidence_met=confidence_met,
        type_qualifies=type_qualifies,
        location_resolved=location_resolved,
        policy_details={
            "min_confidence": min_confidence,
            "min_severity": min_severity,
            "ai_is_disaster": is_disaster,
            "ai_disaster_type": disaster_type,
            "ai_severity": severity,
            "ai_confidence": confidence,
            "ai_requires_dispatch": getattr(analysis, "requires_immediate_dispatch", False),
        },
    )