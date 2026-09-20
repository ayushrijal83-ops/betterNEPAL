"""Tests for Disaster Incident Service."""
from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.announcement import Announcement
from app.models.disaster_incident import DisasterIncident
from app.models.district import District
from app.models.enums import (
    DisasterIncidentStatus,
    DisasterSeverity,
    DisasterType,
    ReportCategory,
    ReportStatus,
)
from app.models.municipality import Municipality
from app.models.report import Report
from app.models.user import User
from app.services.disaster_incident_service import (
    _correlate_with_existing,
    acknowledge_disaster_incident,
    create_disaster_incident_from_report,
    process_report_for_disaster,
    update_disaster_incident_status,
)
from app.services.ai_dispatch import DisasterAnalysis


@pytest.fixture
def areas(db):
    """One synthetic district with one synthetic municipality.

    Same convention as tests/test_reports.py - SQLite cannot answer a
    point-in-polygon query, so a resolved jurisdiction has to be faked in.
    """
    district = District(name="Testland", province="TestProvince", code="D-01")
    db.session.add(district)
    db.session.commit()
    municipality = Municipality(district_id=district.id, name="Testville", code="M-01")
    db.session.add(municipality)
    db.session.commit()
    return {"district": district, "municipality": municipality}


@pytest.fixture
def located(monkeypatch, areas):
    """Make reverse geocoding resolve, standing in for PostGIS."""
    from app.services import geolocation_service

    def _fake_reverse_geocode(coordinates):
        return {
            "point": coordinates.to_geojson(),
            "coordinates": coordinates.as_dict(),
            "resolved": True,
            "reason": None,
            "province": areas["district"].province,
            "district": areas["district"].to_dict(),
            "municipality": areas["municipality"].to_dict(),
        }

    monkeypatch.setattr(geolocation_service, "reverse_geocode", _fake_reverse_geocode)
    return areas


# --- process_report_for_disaster tests ----------------------------------------


def test_process_report_for_disaster_unauthorized(db, make_user):
    """Non-authority user cannot trigger evaluation."""
    citizen = make_user(role_names=("citizen",))
    report = Report(
        reporter_id=citizen.id,
        title="Test report",
        description="Test description",
        category=ReportCategory.ROAD_DAMAGE,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(report)
    db.session.commit()

    with pytest.raises(Exception) as exc_info:
        process_report_for_disaster(report.id, user_id=citizen.id)

    # Check the ApiError code and status
    assert exc_info.value.code == "insufficient_role"
    assert exc_info.value.status == 403


def test_process_report_for_disaster_authorized(db, make_user, roles):
    """Authority user can trigger evaluation."""
    authority = make_user(role_names=("authority",))
    report = Report(
        reporter_id=authority.id,
        title="Landslide on highway",
        description="Major landslide blocking road",
        category=ReportCategory.NATURAL_DISASTER,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(report)
    db.session.commit()

    from datetime import datetime
    from app.services.ai_dispatch import DisasterAnalysis

    # Create a proper analysis object with datetime
    mock_analysis = DisasterAnalysis(
        is_disaster=True,
        disaster_type="landslide",
        severity="high",
        confidence=0.9,
        requires_immediate_dispatch=True,
        reason="Active landslide",
        evidence=["road blocked"],
        model="qwen2.5:3b",
        ai_analyzed_at=datetime.utcnow(),
    )

    with patch("app.services.disaster_incident_service.evaluate_disaster_threat") as mock_eval:
        mock_eval.return_value = mock_analysis

        # Mock jurisdiction resolution
        with patch("app.services.disaster_incident_service.resolve_jurisdiction") as mock_jur:
            mock_jur.return_value = {
                "district_id": None,
                "municipality_id": None,
                "resolved": False,
                "reason": "spatial_backend_unavailable",
                "district_name": None,
                "municipality_name": None,
            }

            # Mock policy evaluation
            with patch("app.services.disaster_incident_service.evaluate_dispatch_policy") as mock_policy:
                mock_policy.return_value = MagicMock(
                    should_dispatch=False,
                    reason="jurisdiction not resolved",
                    severity_met=True,
                    confidence_met=True,
                    type_qualifies=True,
                    location_resolved=False,
                    policy_details={},
                )

                result = process_report_for_disaster(report.id, user_id=authority.id)

    assert result["action"] in ("created", "correlated", "none")


def test_process_report_for_disaster_ai_unavailable(db, make_user, roles):
    """When AI unavailable, report still processed."""
    authority = make_user(role_names=("authority",))
    report = Report(
        reporter_id=authority.id,
        title="Test report",
        description="Test",
        category=ReportCategory.ROAD_DAMAGE,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(report)
    db.session.commit()

    with patch("app.services.disaster_incident_service.evaluate_disaster_threat") as mock_eval:
        mock_eval.return_value = MagicMock(
            is_disaster=False,
            disaster_type="none",
            confidence=None,
            reason="AI unavailable",
            evidence=[],
            model=None,
        )

        with patch("app.services.disaster_incident_service.resolve_jurisdiction") as mock_jur:
            mock_jur.return_value = {
                "district_id": None,
                "resolved": False,
                "reason": "spatial_backend_unavailable",
            }

            with patch("app.services.disaster_incident_service.evaluate_dispatch_policy") as mock_policy:
                mock_policy.return_value = MagicMock(
                    should_dispatch=False,
                    reason="AI not disaster",
                )

                result = process_report_for_disaster(report.id, user_id=authority.id)

    assert result["action"] == "none"


def test_process_report_for_disaster_resolves_jurisdiction_and_dispatches(
    db, make_user, roles, located
):
    """End-to-end: a real report's persisted lat/lng reaches the real
    jurisdiction resolver and the real policy engine (only the AI call is
    mocked - that is the one genuine external boundary). With a district that
    actually covers the point (the ``located`` fixture standing in for
    PostGIS) and a high-confidence disaster classification, policy must
    dispatch and the incident must carry the resolved district.
    """
    authority = make_user(role_names=("authority",))
    report = Report(
        reporter_id=authority.id,
        title="Severe flooding near Bagmati river",
        description="Heavy rainfall overflow, roads underwater.",
        category=ReportCategory.NATURAL_DISASTER,
        latitude=27.7172,
        longitude=85.3240,
    )
    db.session.add(report)
    db.session.commit()

    mock_analysis = DisasterAnalysis(
        is_disaster=True,
        disaster_type="flood",
        severity="high",
        confidence=0.95,
        requires_immediate_dispatch=True,
        reason="Active flooding near a residential area.",
        evidence=["roads underwater"],
        model="qwen2.5:3b",
        ai_analyzed_at=datetime.utcnow(),
    )

    with patch(
        "app.services.disaster_incident_service.evaluate_disaster_threat",
        return_value=mock_analysis,
    ):
        result = process_report_for_disaster(report.id, user_id=authority.id)

    assert result["jurisdiction"]["resolved"] is True
    assert result["jurisdiction"]["district_id"] == str(located["district"].id)
    assert result["policy_decision"]["should_dispatch"] is True
    assert result["action"] == "created"

    incident = db.session.get(DisasterIncident, uuid.UUID(result["incident_id"]))
    assert incident.district_id == located["district"].id
    assert incident.status == DisasterIncidentStatus.DISPATCHED

    # A dispatched incident publishes a local feed post immediately - no
    # separate "generate alert" step, and no draft sitting unpublished.
    posts = db.session.scalars(
        select(Announcement).where(Announcement.district_id == located["district"].id)
    ).all()
    assert len(posts) == 1
    assert posts[0].is_draft is False
    assert posts[0].author_id == authority.id
    assert "Kathmandu" not in posts[0].body or located["district"].name in posts[0].body
    assert "Flood" in posts[0].title
    assert f"Severity: {incident.severity.value.capitalize()}" in posts[0].body


def test_process_report_for_disaster_is_idempotent_for_feed_posts(db, make_user, roles, located):
    """Calling evaluate twice for the same report must not double-post."""
    authority = make_user(role_names=("authority",))
    report = Report(
        reporter_id=authority.id,
        title="Severe flooding near Bagmati river",
        description="Heavy rainfall overflow, roads underwater.",
        category=ReportCategory.NATURAL_DISASTER,
        latitude=27.7172,
        longitude=85.3240,
    )
    db.session.add(report)
    db.session.commit()

    mock_analysis = DisasterAnalysis(
        is_disaster=True,
        disaster_type="flood",
        severity="high",
        confidence=0.95,
        requires_immediate_dispatch=True,
        reason="Active flooding.",
        evidence=[],
        model="qwen2.5:3b",
        ai_analyzed_at=datetime.utcnow(),
    )

    with patch(
        "app.services.disaster_incident_service.evaluate_disaster_threat",
        return_value=mock_analysis,
    ):
        process_report_for_disaster(report.id, user_id=authority.id)
        process_report_for_disaster(report.id, user_id=authority.id)

    posts = db.session.scalars(select(Announcement)).all()
    assert len(posts) == 1


def test_critical_dispatched_incident_also_posts_nationally(db, make_user, roles, located):
    """A CRITICAL, dispatched incident gets a second, national post; a merely
    HIGH-severity one does not - a fixed, non-AI threshold decides "broader
    relevance", not a judgement call.
    """
    authority = make_user(role_names=("authority",))
    report = Report(
        reporter_id=authority.id,
        title="Catastrophic flooding near Bagmati river",
        description="Major flood event.",
        category=ReportCategory.NATURAL_DISASTER,
        latitude=27.7172,
        longitude=85.3240,
    )
    db.session.add(report)
    db.session.commit()

    mock_analysis = DisasterAnalysis(
        is_disaster=True,
        disaster_type="flood",
        severity="critical",
        confidence=0.98,
        requires_immediate_dispatch=True,
        reason="Catastrophic flooding.",
        evidence=[],
        model="qwen2.5:3b",
        ai_analyzed_at=datetime.utcnow(),
    )

    with patch(
        "app.services.disaster_incident_service.evaluate_disaster_threat",
        return_value=mock_analysis,
    ):
        process_report_for_disaster(report.id, user_id=authority.id)

    local_posts = db.session.scalars(
        select(Announcement).where(Announcement.district_id == located["district"].id)
    ).all()
    national_posts = db.session.scalars(
        select(Announcement).where(Announcement.district_id.is_(None))
    ).all()
    assert len(local_posts) == 1
    assert len(national_posts) == 1
    assert national_posts[0].is_draft is False
    assert "Disaster Alert" in national_posts[0].title


def test_not_dispatched_disaster_report_posts_local_draft_only(db, make_user, roles):
    """AI classifies it as a disaster but policy does not dispatch (no
    resolved jurisdiction here) - so no post is made at all, since there is
    nowhere honest to scope a local post to and it has not cleared the bar
    for going out nationally. AI output alone never reaches the public feed.
    """
    authority = make_user(role_names=("authority",))
    report = Report(
        reporter_id=authority.id,
        title="Severe flooding near Bagmati river",
        description="Heavy rainfall overflow.",
        category=ReportCategory.NATURAL_DISASTER,
        latitude=27.7172,
        longitude=85.3240,
    )
    db.session.add(report)
    db.session.commit()

    mock_analysis = DisasterAnalysis(
        is_disaster=True,
        disaster_type="flood",
        severity="high",
        confidence=0.95,
        requires_immediate_dispatch=True,
        reason="Active flooding.",
        evidence=[],
        model="qwen2.5:3b",
        ai_analyzed_at=datetime.utcnow(),
    )

    with patch(
        "app.services.disaster_incident_service.evaluate_disaster_threat",
        return_value=mock_analysis,
    ):
        process_report_for_disaster(report.id, user_id=authority.id)

    assert db.session.scalars(select(Announcement)).all() == []


def test_non_disaster_report_never_posts_to_feed(db, make_user, roles, located):
    """A normal, non-disaster report must produce no feed post at all."""
    authority = make_user(role_names=("authority",))
    report = Report(
        reporter_id=authority.id,
        title="Pothole on Ring Road",
        description="A pothole has formed.",
        category=ReportCategory.ROAD_DAMAGE,
        latitude=27.7172,
        longitude=85.3240,
    )
    db.session.add(report)
    db.session.commit()

    mock_analysis = DisasterAnalysis(
        is_disaster=False,
        disaster_type="none",
        reason="Not a disaster.",
        evidence=[],
        model="qwen2.5:3b",
    )

    with patch(
        "app.services.disaster_incident_service.evaluate_disaster_threat",
        return_value=mock_analysis,
    ):
        result = process_report_for_disaster(report.id, user_id=authority.id)

    assert result["action"] == "none"
    assert db.session.scalars(select(Announcement)).all() == []


def test_process_report_for_disaster_unresolved_jurisdiction_blocks_dispatch(
    db, make_user, roles
):
    """Same real pipeline, but WITHOUT ``located`` - the genuine SQLite
    behaviour (no PostGIS, so reverse geocoding honestly reports itself
    unresolved). Even with a high-confidence disaster classification, the
    real policy engine must refuse to dispatch, specifically because
    jurisdiction could not be resolved - and it must not be bypassed to force
    a dispatch.
    """
    authority = make_user(role_names=("authority",))
    report = Report(
        reporter_id=authority.id,
        title="Severe flooding near Bagmati river",
        description="Heavy rainfall overflow, roads underwater.",
        category=ReportCategory.NATURAL_DISASTER,
        latitude=27.7172,
        longitude=85.3240,
    )
    db.session.add(report)
    db.session.commit()

    mock_analysis = DisasterAnalysis(
        is_disaster=True,
        disaster_type="flood",
        severity="high",
        confidence=0.95,
        requires_immediate_dispatch=True,
        reason="Active flooding near a residential area.",
        evidence=["roads underwater"],
        model="qwen2.5:3b",
        ai_analyzed_at=datetime.utcnow(),
    )

    with patch(
        "app.services.disaster_incident_service.evaluate_disaster_threat",
        return_value=mock_analysis,
    ):
        result = process_report_for_disaster(report.id, user_id=authority.id)

    assert result["jurisdiction"]["resolved"] is False
    assert result["jurisdiction"]["district_id"] is None
    assert result["policy_decision"]["should_dispatch"] is False
    assert "jurisdiction" in result["policy_decision"]["reason"]

    # Still tracked for review - is_disaster stays true even when not dispatched.
    assert result["action"] == "created"
    incident = db.session.get(DisasterIncident, uuid.UUID(result["incident_id"]))
    assert incident.district_id is None
    assert incident.status == DisasterIncidentStatus.DETECTED


# --- create_disaster_incident_from_report tests -------------------------------


def test_create_disaster_incident_from_report(db, make_user, roles):
    """Create disaster incident from report."""
    from datetime import datetime
    from app.services.ai_dispatch import DisasterAnalysis

    authority = make_user(role_names=("authority",))
    report = Report(
        reporter_id=authority.id,
        title="Landslide blocks road",
        description="Major landslide",
        category=ReportCategory.NATURAL_DISASTER,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(report)
    db.session.commit()

    analysis = DisasterAnalysis(
        disaster_type="landslide",
        severity="high",
        confidence=0.95,
        reason="Active landslide",
        evidence=["road blocked", "debris"],
        model="qwen2.5:3b",
        ai_analyzed_at=datetime.utcnow(),
    )

    jurisdiction = {
        "district_id": None,
        "municipality_id": None,
    }

    dispatch_decision = MagicMock()
    dispatch_decision.should_dispatch = False

    incident = create_disaster_incident_from_report(
        report_id=report.id,
        analysis=analysis,
        jurisdiction=jurisdiction,
        dispatch_decision=dispatch_decision,
    )

    assert incident.source_report_id == report.id
    assert incident.disaster_type == DisasterType.LANDSLIDE
    assert incident.severity == DisasterSeverity.HIGH
    assert incident.ai_confidence == 0.95
    assert incident.status == DisasterIncidentStatus.DETECTED


def test_create_disaster_incident_dispatches_when_policy_allows(db, make_user, roles):
    """Incident created with DISPATCHED status when policy allows."""
    authority = make_user(role_names=("authority",))
    report = Report(
        reporter_id=authority.id,
        title="Major landslide",
        description="Blocks highway",
        category=ReportCategory.NATURAL_DISASTER,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(report)
    db.session.commit()

    from datetime import datetime
    from app.services.ai_dispatch import DisasterAnalysis

    analysis = DisasterAnalysis(
        disaster_type="landslide",
        severity="critical",
        confidence=0.98,
        reason="Major landslide",
        evidence=["highway blocked"],
        model="qwen2.5:3b",
        ai_analyzed_at=datetime.utcnow(),
    )

    jurisdiction = {
        "district_id": None,
        "municipality_id": None,
    }

    dispatch_decision = MagicMock()
    dispatch_decision.should_dispatch = True

    with patch("app.services.disaster_incident_service.find_authority_users_for_jurisdiction") as mock_users:
        mock_users.return_value = []

        with patch("app.services.disaster_incident_service.notify_authorities_for_incident") as mock_notify:
            mock_notify.return_value = {"notified": 0, "failed": 0, "details": []}

            incident = create_disaster_incident_from_report(
                report_id=report.id,
                analysis=analysis,
                jurisdiction=jurisdiction,
                dispatch_decision=dispatch_decision,
            )

    assert incident.status == DisasterIncidentStatus.DISPATCHED
    assert incident.assigned_at is not None


# --- acknowledge_disaster_incident tests --------------------------------------


def test_acknowledge_disaster_incident_success(db, make_user, roles):
    """Authority acknowledges dispatched incident."""
    authority = make_user(role_names=("authority",))
    from app.models.disaster_incident import DisasterIncident, DisasterDispatch

    incident = DisasterIncident(
        title="Test incident",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DISPATCHED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.flush()

    dispatch = DisasterDispatch(
        disaster_incident_id=incident.id,
        authority_user_id=authority.id,
        status="delivered",
        channel="websocket",
        notified_at=datetime.utcnow(),
    )
    db.session.add(dispatch)
    db.session.commit()

    incident = acknowledge_disaster_incident(incident.id, authority.id)

    assert incident.status == DisasterIncidentStatus.ACKNOWLEDGED
    assert incident.acknowledged_at is not None
    assert dispatch.status == "acknowledged"
    assert dispatch.acknowledged_at is not None


def test_acknowledge_disaster_incident_wrong_status(db, make_user, roles):
    """Cannot acknowledge non-dispatched incident."""
    authority = make_user(role_names=("authority",))
    from app.models.disaster_incident import DisasterIncident

    incident = DisasterIncident(
        title="Test incident",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DETECTED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.commit()

    with pytest.raises(Exception) as exc_info:
        acknowledge_disaster_incident(incident.id, authority.id)

    assert exc_info.value.code == "invalid_status_transition"
    assert exc_info.value.status == 409


def test_acknowledge_disaster_incident_not_authorized(db, make_user, roles):
    """User not dispatched cannot acknowledge."""
    authority1 = make_user(email="authority1@test.np", role_names=("authority",))
    authority2 = make_user(email="authority2@test.np", role_names=("authority",))
    from app.models.disaster_incident import DisasterIncident, DisasterDispatch

    incident = DisasterIncident(
        title="Test incident",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DISPATCHED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.flush()

    dispatch = DisasterDispatch(
        disaster_incident_id=incident.id,
        authority_user_id=authority1.id,
        status="delivered",
        channel="websocket",
        notified_at=datetime.utcnow(),
    )
    db.session.add(dispatch)
    db.session.commit()

    with pytest.raises(Exception) as exc_info:
        acknowledge_disaster_incident(incident.id, authority2.id)

    assert exc_info.value.code == "not_authorized_for_incident"
    assert exc_info.value.status == 403


def test_acknowledge_updates_dispatch_status(db, make_user, roles):
    """Acknowledging updates dispatch record."""
    authority = make_user(role_names=("authority",))
    from app.models.disaster_incident import DisasterIncident, DisasterDispatch

    incident = DisasterIncident(
        title="Test incident",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DISPATCHED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.flush()

    dispatch = DisasterDispatch(
        disaster_incident_id=incident.id,
        authority_user_id=authority.id,
        status="delivered",
        channel="websocket",
        notified_at=datetime.utcnow(),
    )
    db.session.add(dispatch)
    db.session.commit()

    acknowledge_disaster_incident(incident.id, authority.id)

    db.session.refresh(dispatch)
    assert dispatch.status == "acknowledged"
    assert dispatch.acknowledged_at is not None


# --- update_disaster_incident_status tests ------------------------------------


def test_update_status_valid_transition(db, make_user, roles):
    """Valid status transition succeeds."""
    authority = make_user(role_names=("authority",))
    from app.models.disaster_incident import DisasterIncident

    incident = DisasterIncident(
        title="Test incident",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DETECTED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.commit()

    incident = update_disaster_incident_status(
        incident.id, DisasterIncidentStatus.DISPATCHED, user_id=authority.id
    )

    assert incident.status == DisasterIncidentStatus.DISPATCHED
    assert incident.assigned_at is not None
    assert incident.assigned_by_id == authority.id


def test_update_status_invalid_transition(db, make_user, roles):
    """Invalid status transition rejected."""
    authority = make_user(role_names=("authority",))
    from app.models.disaster_incident import DisasterIncident

    incident = DisasterIncident(
        title="Test incident",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.RESOLVED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.commit()

    with pytest.raises(Exception) as exc_info:
        update_disaster_incident_status(
            incident.id, DisasterIncidentStatus.DETECTED, user_id=authority.id
        )

    assert exc_info.value.code == "invalid_status_transition"
    assert exc_info.value.status == 409


def test_update_status_non_authority_rejected(db, make_user, roles):
    """Non-authority user cannot update status."""
    citizen = make_user(email="citizen2@test.np", role_names=("citizen",))
    from app.models.disaster_incident import DisasterIncident

    incident = DisasterIncident(
        title="Test incident",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DETECTED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.commit()

    with pytest.raises(Exception) as exc_info:
        update_disaster_incident_status(
            incident.id, DisasterIncidentStatus.DISPATCHED, user_id=citizen.id
        )

    assert exc_info.value.code == "insufficient_role"
    assert exc_info.value.status == 403


def test_update_status_acknowledge_requires_dispatch(db, make_user, roles):
    """Acknowledging requires user was dispatched."""
    authority1 = make_user(email="authority1@test.np", role_names=("authority",))
    authority2 = make_user(email="authority2@test.np", role_names=("authority",))
    from app.models.disaster_incident import DisasterIncident, DisasterDispatch

    incident = DisasterIncident(
        title="Test incident",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DISPATCHED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.flush()

    dispatch = DisasterDispatch(
        disaster_incident_id=incident.id,
        authority_user_id=authority1.id,
        status="delivered",
        channel="websocket",
        notified_at=datetime.utcnow(),
    )
    db.session.add(dispatch)
    db.session.commit()

    # authority2 tries to acknowledge but wasn't dispatched
    with pytest.raises(Exception) as exc_info:
        update_disaster_incident_status(
            incident.id, DisasterIncidentStatus.ACKNOWLEDGED, user_id=authority2.id
        )

    assert exc_info.value.code == "not_authorized_for_incident"
    assert exc_info.value.status == 403


def test_update_status_resolved_sets_resolved_at(db, make_user, roles):
    """Resolving incident sets resolved_at timestamp."""
    authority = make_user(role_names=("authority",))
    from app.models.disaster_incident import DisasterIncident

    incident = DisasterIncident(
        title="Test incident",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.RESPONDING,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.commit()

    incident = update_disaster_incident_status(
        incident.id, DisasterIncidentStatus.RESOLVED, user_id=authority.id
    )

    assert incident.status == DisasterIncidentStatus.RESOLVED
    assert incident.resolved_at is not None


# --- _correlate_with_existing tests -------------------------------------------


def test_correlate_with_existing_same_type(db, make_user):
    """Correlates with existing incident of same disaster type."""
    from app.models.disaster_incident import DisasterIncident

    # Create existing active incident
    existing = DisasterIncident(
        title="Existing landslide",
        description="Existing",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DISPATCHED,
        latitude=27.7172,
        longitude=85.3240,
    )
    db.session.add(existing)
    db.session.commit()

    # Create new report at same location
    report = Report(
        reporter_id=make_user(role_names=("citizen",)).id,
        title="New landslide report",
        description="Same location",
        category=ReportCategory.NATURAL_DISASTER,
        latitude=27.7172,
        longitude=85.3240,
    )
    db.session.add(report)
    db.session.commit()

    with patch("app.services.disaster_incident_service._find_nearby_active_incidents") as mock_nearby:
        mock_nearby.return_value = [existing]

        correlated = _correlate_with_existing(
            report.latitude, report.longitude, "landslide"
        )

    assert correlated is not None
    assert correlated.id == existing.id


def test_correlate_with_existing_different_type(db, make_user):
    """Correlates with closest incident when no same-type match found."""
    from app.models.disaster_incident import DisasterIncident

    existing = DisasterIncident(
        title="Existing flood",
        description="Existing",
        disaster_type=DisasterType.FLOOD,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DISPATCHED,
        latitude=27.7172,
        longitude=85.3240,
    )
    db.session.add(existing)
    db.session.commit()

    with patch("app.services.disaster_incident_service._find_nearby_active_incidents") as mock_nearby:
        mock_nearby.return_value = [existing]

        correlated = _correlate_with_existing(
            27.7172, 85.3240, "landslide"
        )

    # When no same-type match, returns closest incident
    assert correlated is not None
    assert correlated.id == existing.id


def test_correlate_no_existing_incident(app):
    """Returns None when no nearby incidents."""
    with app.app_context():
        with patch("app.services.disaster_incident_service._find_nearby_active_incidents") as mock_nearby:
            mock_nearby.return_value = []

            correlated = _correlate_with_existing(
                27.7172, 85.3240, "landslide"
            )

        assert correlated is None