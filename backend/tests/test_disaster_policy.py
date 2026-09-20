"""Tests for Disaster Policy Service."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.disaster_policy import (
    DispatchDecision,
    confidence_meets_threshold,
    disaster_type_qualifies,
    evaluate_dispatch_policy,
    severity_meets_threshold,
)


@pytest.fixture
def app():
    from app import create_app
    application = create_app("testing")
    application.config.update(TESTING=True)
    return application


# --- severity threshold tests -------------------------------------------------


@pytest.mark.parametrize(
    "severity,min_severity,expected",
    [
        ("CRITICAL", "HIGH", True),
        ("HIGH", "HIGH", True),
        ("MODERATE", "HIGH", False),
        ("LOW", "HIGH", False),
        ("HIGH", "MODERATE", True),
        ("MODERATE", "MODERATE", True),
        ("LOW", "MODERATE", False),
        ("LOW", "LOW", True),
    ],
)
def test_severity_meets_threshold(severity, min_severity, expected):
    assert severity_meets_threshold(severity, min_severity) == expected


def test_severity_meets_threshold_invalid():
    assert severity_meets_threshold("INVALID", "HIGH") is False
    assert severity_meets_threshold(None, "HIGH") is False


# --- confidence threshold tests -----------------------------------------------


@pytest.mark.parametrize(
    "confidence,min_confidence,expected",
    [
        (0.9, 0.85, True),
        (0.85, 0.85, True),
        (0.84, 0.85, False),
        (1.0, 0.9, True),
        (0.5, 0.5, True),
        (None, 0.85, False),
    ],
)
def test_confidence_meets_threshold(confidence, min_confidence, expected):
    assert confidence_meets_threshold(confidence, min_confidence) == expected


# --- disaster type qualification tests ----------------------------------------


@pytest.mark.parametrize(
    "disaster_type,expected",
    [
        ("earthquake", True),
        ("flood", True),
        ("flash_flood", True),
        ("landslide", True),
        ("forest_fire", True),
        ("storm", True),
        ("lightning", True),
        ("avalanche", True),
        ("wildfire", True),
        ("other", True),
        ("none", False),
        ("", False),
        (None, False),
        ("invalid_type", False),
    ],
)
def test_disaster_type_qualifies(disaster_type, expected):
    assert disaster_type_qualifies(disaster_type) == expected


# --- full policy evaluation tests ---------------------------------------------


def test_evaluate_dispatch_policy_all_pass(app):
    """All conditions met -> should_dispatch=True."""
    with app.app_context():
        analysis = MagicMock()
        analysis.is_disaster = True
        analysis.disaster_type = "landslide"
        analysis.severity = "high"
        analysis.confidence = 0.9
        analysis.requires_immediate_dispatch = True

        decision = evaluate_dispatch_policy(
            analysis,
            latitude=27.7,
            longitude=85.3,
            district_id="12345678-1234-1234-1234-123456789012",
        )

        assert decision.should_dispatch is True
        assert decision.severity_met is True
        assert decision.confidence_met is True
        assert decision.type_qualifies is True
        assert decision.location_resolved is True


def test_evaluate_dispatch_policy_not_disaster(app):
    """is_disaster=False -> should_dispatch=False."""
    with app.app_context():
        analysis = MagicMock()
        analysis.is_disaster = False
        analysis.disaster_type = "landslide"
        analysis.severity = "critical"
        analysis.confidence = 0.99

        decision = evaluate_dispatch_policy(
            analysis,
            latitude=27.7,
            longitude=85.3,
            district_id="12345678-1234-1234-1234-123456789012",
        )

        assert decision.should_dispatch is False
        assert "not a disaster" in decision.reason


def test_evaluate_dispatch_policy_type_none(app):
    """disaster_type=none -> should_dispatch=False."""
    with app.app_context():
        analysis = MagicMock()
        analysis.is_disaster = True
        analysis.disaster_type = "none"
        analysis.severity = "critical"
        analysis.confidence = 0.99

        decision = evaluate_dispatch_policy(
            analysis,
            latitude=27.7,
            longitude=85.3,
            district_id="12345678-1234-1234-1234-123456789012",
        )

        assert decision.should_dispatch is False
        assert "does not qualify" in decision.reason


def test_evaluate_dispatch_policy_low_severity(app):
    """Severity below threshold -> should_dispatch=False."""
    with app.app_context():
        analysis = MagicMock()
        analysis.is_disaster = True
        analysis.disaster_type = "landslide"
        analysis.severity = "moderate"  # Below HIGH threshold
        analysis.confidence = 0.99

        decision = evaluate_dispatch_policy(
            analysis,
            latitude=27.7,
            longitude=85.3,
            district_id="12345678-1234-1234-1234-123456789012",
        )

        assert decision.should_dispatch is False
        assert "severity" in decision.reason
        assert "below minimum" in decision.reason


def test_evaluate_dispatch_policy_low_confidence(app):
    """Confidence below threshold -> should_dispatch=False."""
    with app.app_context():
        analysis = MagicMock()
        analysis.is_disaster = True
        analysis.disaster_type = "landslide"
        analysis.severity = "critical"
        analysis.confidence = 0.5  # Below 0.85 threshold

        decision = evaluate_dispatch_policy(
            analysis,
            latitude=27.7,
            longitude=85.3,
            district_id="12345678-1234-1234-1234-123456789012",
        )

        assert decision.should_dispatch is False
        assert "confidence" in decision.reason
        assert "below minimum" in decision.reason


def test_evaluate_dispatch_policy_no_district(app):
    """No district resolved -> should_dispatch=False."""
    with app.app_context():
        analysis = MagicMock()
        analysis.is_disaster = True
        analysis.disaster_type = "landslide"
        analysis.severity = "critical"
        analysis.confidence = 0.99

        decision = evaluate_dispatch_policy(
            analysis,
            latitude=27.7,
            longitude=85.3,
            district_id=None,
        )

        assert decision.should_dispatch is False
        assert "jurisdiction" in decision.reason
        assert "could not be resolved" in decision.reason


def test_evaluate_dispatch_policy_threshold_boundary(app):
    """Test exact boundary values."""
    with app.app_context():
        analysis = MagicMock()
        analysis.is_disaster = True
        analysis.disaster_type = "landslide"

        # Exactly at confidence threshold
        analysis.severity = "high"
        analysis.confidence = 0.85

        decision = evaluate_dispatch_policy(
            analysis,
            latitude=27.7,
            longitude=85.3,
            district_id="12345678-1234-1234-1234-123456789012",
        )
        assert decision.confidence_met is True
        assert decision.should_dispatch is True

        # Just below threshold
        analysis.confidence = 0.849
        decision = evaluate_dispatch_policy(
            analysis,
            latitude=27.7,
            longitude=85.3,
            district_id="12345678-1234-1234-1234-123456789012",
        )
        assert decision.confidence_met is False
        assert decision.should_dispatch is False


def test_evaluate_dispatch_policy_includes_policy_details(app):
    """Decision includes policy details for debugging."""
    with app.app_context():
        analysis = MagicMock()
        analysis.is_disaster = True
        analysis.disaster_type = "landslide"
        analysis.severity = "high"
        analysis.confidence = 0.9
        analysis.requires_immediate_dispatch = True

        decision = evaluate_dispatch_policy(
            analysis,
            latitude=27.7,
            longitude=85.3,
            district_id="12345678-1234-1234-1234-123456789012",
        )

        assert "min_confidence" in decision.policy_details
        assert "min_severity" in decision.policy_details
        assert "ai_is_disaster" in decision.policy_details
        assert "ai_disaster_type" in decision.policy_details
        assert "ai_severity" in decision.policy_details
        assert "ai_confidence" in decision.policy_details
        assert "ai_requires_dispatch" in decision.policy_details


# --- config threshold tests ---------------------------------------------------


def test_evaluate_dispatch_policy_custom_thresholds(app, monkeypatch):
    """Policy respects custom config thresholds."""
    from app.services import disaster_policy

    with app.app_context():
        analysis = MagicMock()
        analysis.is_disaster = True
        analysis.disaster_type = "landslide"
        analysis.severity = "moderate"  # Would be below default HIGH
        analysis.confidence = 0.7  # Would be below default 0.85

        # Override config to lower thresholds using monkeypatch
        monkeypatch.setattr(
            disaster_policy.current_app.config, "get",
            lambda k, d=None: {
                "AI_DISPATCH_MIN_CONFIDENCE": 0.5,
                "AI_DISPATCH_MIN_SEVERITY": "MODERATE",
            }.get(k, d)
        )

        decision = disaster_policy.evaluate_dispatch_policy(
            analysis,
            latitude=27.7,
            longitude=85.3,
            district_id="12345678-1234-1234-1234-123456789012",
        )

        # With lower thresholds, should pass
        assert decision.confidence_met is True
        assert decision.severity_met is True
        assert decision.should_dispatch is True