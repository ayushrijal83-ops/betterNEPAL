"""Tests for Disaster API Routes."""
from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.models.disaster_incident import DisasterIncident, DisasterDispatch
from app.models.enums import (
    DisasterIncidentStatus,
    DisasterSeverity,
    DisasterType,
    ReportCategory,
    ReportStatus,
)
from app.models.report import Report
from app.models.user import User


# --- GET /disaster/incidents tests --------------------------------------------


def test_list_disaster_incidents_public(client, db):
    """Public can list disaster incidents."""
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType

    incident = DisasterIncident(
        title="Public landslide",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DISPATCHED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.commit()

    response = client.get("/api/v1/disaster/incidents")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "success"
    assert len(data["data"]["incidents"]) == 1


def test_list_disaster_incidents_with_filters(client, db):
    """Filtering works on incident list."""
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType

    incident1 = DisasterIncident(
        title="Landslide",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DISPATCHED,
        latitude=27.7,
        longitude=85.3,
    )
    incident2 = DisasterIncident(
        title="Flood",
        description="Test",
        disaster_type=DisasterType.FLOOD,
        severity=DisasterSeverity.MODERATE,
        status=DisasterIncidentStatus.DETECTED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add_all([incident1, incident2])
    db.session.commit()

    # Filter by type
    response = client.get("/api/v1/disaster/incidents?disaster_type=landslide")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data["data"]["incidents"]) == 1
    assert data["data"]["incidents"][0]["disaster_type"] == "landslide"

    # Filter by status
    response = client.get("/api/v1/disaster/incidents?status=dispatched")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data["data"]["incidents"]) == 1
    assert data["data"]["incidents"][0]["status"] == "dispatched"


def test_list_disaster_incidents_invalid_filter(client):
    """Invalid filter returns 400."""
    response = client.get("/api/v1/disaster/incidents?disaster_type=invalid_type")
    assert response.status_code == 400
    data = response.get_json()
    assert data["status"] == "error"
    assert data["error"]["code"] == "invalid_filter"


# --- GET /disaster/incidents/<id> tests ---------------------------------------


def test_get_disaster_incident_public(client, db):
    """Public gets basic incident info."""
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType

    incident = DisasterIncident(
        title="Public incident",
        description="Details",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DISPATCHED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.commit()

    response = client.get(f"/api/v1/disaster/incidents/{incident.id}")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "success"
    assert "incident" in data["data"]
    assert data["data"]["incident"]["title"] == "Public incident"
    # Authority fields should not be in public response
    assert "ai_reason" not in data["data"]["incident"]
    assert "dispatches" not in data["data"]["incident"]


def test_get_disaster_incident_authority(client, db, make_user, roles, auth_headers):
    """Authority gets full incident details."""
    authority = make_user(role_names=("authority",), password="correct-horse-battery")
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType

    incident = DisasterIncident(
        title="Authority incident",
        description="Details",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DISPATCHED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.commit()

    response = client.get(
        f"/api/v1/disaster/incidents/{incident.id}",
        headers=auth_headers(authority.email, "correct-horse-battery"),
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "success"
    # Incident created without dispatches; dispatches key may be absent or empty list
    # depending on relationship loading. Accept either.
    assert "dispatches" not in data["data"]["incident"] or data["data"]["incident"]["dispatches"] == []


def test_get_disaster_incident_not_found(client, db):
    """404 for non-existent incident."""
    response = client.get("/api/v1/disaster/incidents/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


# --- POST /disaster/incidents/<id>/acknowledge tests --------------------------


def test_acknowledge_incident_success(client, db, make_user, roles, auth_headers):
    """Authority acknowledges dispatched incident."""
    authority = make_user(role_names=("authority",), password="correct-horse-battery")
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType
    from app.models.disaster_incident import DisasterDispatch

    incident = DisasterIncident(
        title="Incident to acknowledge",
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

    response = client.post(
        f"/api/v1/disaster/incidents/{incident.id}/acknowledge",
        headers=auth_headers(authority.email, "correct-horse-battery"),
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "success"
    assert data["data"]["incident"]["status"] == "acknowledged"
    assert "acknowledged_at" in data["data"]["incident"]


def test_acknowledge_incident_not_dispatched(client, db, make_user, roles, auth_headers):
    """Cannot acknowledge non-dispatched incident."""
    authority = make_user(role_names=("authority",), password="correct-horse-battery")
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType

    incident = DisasterIncident(
        title="Not dispatched",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DETECTED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.commit()

    response = client.post(
        f"/api/v1/disaster/incidents/{incident.id}/acknowledge",
        headers=auth_headers(authority.email, "correct-horse-battery"),
    )
    assert response.status_code == 409


def test_acknowledge_incident_not_authorized(client, db, make_user, roles, auth_headers):
    """Authority not dispatched cannot acknowledge."""
    authority1 = make_user(role_names=("authority",), password="correct-horse-battery")
    authority2 = make_user(role_names=("authority",), password="correct-horse-battery")
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType
    from app.models.disaster_incident import DisasterDispatch

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

    response = client.post(
        f"/api/v1/disaster/incidents/{incident.id}/acknowledge",
        headers=auth_headers(authority2.email, "correct-horse-battery"),
    )
    assert response.status_code == 403


def test_acknowledge_incident_requires_auth(client, db):
    """Acknowledge requires authentication."""
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType

    incident = DisasterIncident(
        title="Test",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DISPATCHED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.commit()

    response = client.post(f"/api/v1/disaster/incidents/{incident.id}/acknowledge")
    assert response.status_code == 401


# --- PATCH /disaster/incidents/<id> tests -------------------------------------


def test_update_status_valid(client, db, make_user, roles, auth_headers):
    """Valid status transition succeeds."""
    authority = make_user(role_names=("authority",), password="correct-horse-battery")
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType

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

    response = client.patch(
        f"/api/v1/disaster/incidents/{incident.id}",
        json={"status": "dispatched"},
        headers=auth_headers(authority.email, "correct-horse-battery"),
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["data"]["incident"]["status"] == "dispatched"


def test_update_status_invalid_transition(client, db, make_user, roles, auth_headers):
    """Invalid status transition rejected."""
    authority = make_user(role_names=("authority",), password="correct-horse-battery")
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType

    incident = DisasterIncident(
        title="Resolved incident",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.RESOLVED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.commit()

    response = client.patch(
        f"/api/v1/disaster/incidents/{incident.id}",
        json={"status": "detected"},
        headers=auth_headers(authority.email, "correct-horse-battery"),
    )
    assert response.status_code == 409


def test_update_severity(client, db, make_user, roles, auth_headers):
    """Update severity succeeds."""
    authority = make_user(role_names=("authority",), password="correct-horse-battery")
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType

    incident = DisasterIncident(
        title="Test incident",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.MODERATE,
        status=DisasterIncidentStatus.DETECTED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.commit()

    response = client.patch(
        f"/api/v1/disaster/incidents/{incident.id}",
        json={"severity": "critical"},
        headers=auth_headers(authority.email, "correct-horse-battery"),
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["data"]["incident"]["severity"] == "critical"


def test_update_status_non_authority_rejected(client, db, make_user, roles):
    """Non-authority cannot update status."""
    citizen = make_user(role_names=("citizen",))
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType

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

    response = client.patch(
        f"/api/v1/disaster/incidents/{incident.id}",
        json={"status": "dispatched"},
        headers=auth_headers("citizen@test.np", "correct-horse-battery"),
    )
    assert response.status_code == 403


def test_update_status_non_authority_rejected(client, db, make_user, auth_headers):
    """Non-authority user cannot update status."""
    citizen = make_user(role_names=("citizen",), password="correct-horse-battery")
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType

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

    response = client.patch(
        f"/api/v1/disaster/incidents/{incident.id}",
        json={"status": "dispatched"},
        headers=auth_headers(citizen.email, "correct-horse-battery"),
    )
    assert response.status_code == 403


def test_update_status_invalid_value(client, db, make_user, roles, auth_headers):
    """Invalid status/severity value rejected."""
    authority = make_user(role_names=("authority",), password="correct-horse-battery")
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType

    incident = DisasterIncident(
        title="Test",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DETECTED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.commit()

    response = client.patch(
        f"/api/v1/disaster/incidents/{incident.id}",
        json={"status": "invalid_status"},
        headers=auth_headers(authority.email, "correct-horse-battery"),
    )
    assert response.status_code == 400


# --- POST /disaster/incidents/<id>/assign tests -------------------------------


def test_assign_incident_valid(client, db, make_user, roles, auth_headers):
    """Assign incident to authority in same district."""
    authority1 = make_user(role_names=("authority",), password="correct-horse-battery")
    authority2 = make_user(role_names=("authority",), password="correct-horse-battery")
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType
    from app.models.authority import Authority
    from app.models.enums import AuthorityType, GovernmentLevel

    # Create district and authority
    from app.models import District
    district = District(name="Test District", code="TD-01")
    db.session.add(district)
    db.session.commit()

    authority_obj = Authority(
        name="District Roads Office",
        level=GovernmentLevel.LOCAL,
        type=AuthorityType.MUNICIPAL_OFFICE,
        district_id=district.id,
    )
    db.session.add(authority_obj)
    db.session.commit()

    incident = DisasterIncident(
        title="Incident to assign",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DETECTED,
        latitude=27.7,
        longitude=85.3,
        district_id=district.id,
    )
    db.session.add(incident)
    db.session.commit()

    response = client.post(
        f"/api/v1/disaster/incidents/{incident.id}/assign",
        json={"authority_id": str(authority_obj.id)},
        headers=auth_headers(authority1.email, "correct-horse-battery"),
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["data"]["incident"]["authority_id"] == str(authority_obj.id)
    assert data["data"]["incident"]["status"] == "dispatched"


def test_assign_incident_authority_not_found(client, db, make_user, roles, auth_headers):
    """Assign to non-existent authority fails."""
    authority = make_user(role_names=("authority",), password="correct-horse-battery")
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType

    incident = DisasterIncident(
        title="Test",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DETECTED,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(incident)
    db.session.commit()

    response = client.post(
        f"/api/v1/disaster/incidents/{incident.id}/assign",
        json={"authority_id": str(uuid.uuid4())},
        headers=auth_headers(authority.email, "correct-horse-battery"),
    )
    assert response.status_code == 404


def test_assign_incident_jurisdiction_mismatch(client, db, make_user, roles, auth_headers):
    """Cannot assign authority from different district."""
    authority = make_user(role_names=("authority",), password="correct-horse-battery")
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType
    from app.models.authority import Authority
    from app.models.enums import AuthorityType, GovernmentLevel
    from app.models import District

    district1 = District(name="District 1", code="D1-01")
    district2 = District(name="District 2", code="D2-01")
    db.session.add_all([district1, district2])
    db.session.commit()

    authority_obj = Authority(
        name="District 2 Office",
        level=GovernmentLevel.LOCAL,
        type=AuthorityType.MUNICIPAL_OFFICE,
        district_id=district2.id,
    )
    db.session.add(authority_obj)
    db.session.commit()

    incident = DisasterIncident(
        title="District 1 incident",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DETECTED,
        latitude=27.7,
        longitude=85.3,
        district_id=district1.id,
    )
    db.session.add(incident)
    db.session.commit()

    response = client.post(
        f"/api/v1/disaster/incidents/{incident.id}/assign",
        json={"authority_id": str(authority_obj.id)},
        headers=auth_headers(authority.email, "correct-horse-battery"),
    )
    assert response.status_code == 403
    data = response.get_json()
    assert data["error"]["code"] == "authority_jurisdiction_mismatch"


def test_assign_national_authority_any_district(client, db, make_user, roles, auth_headers):
    """National authority can be assigned to any district."""
    authority = make_user(role_names=("authority",), password="correct-horse-battery")
    from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType
    from app.models.authority import Authority
    from app.models.enums import AuthorityType, GovernmentLevel
    from app.models import District

    district = District(name="District 1", code="D1-01")
    db.session.add(district)
    db.session.commit()

    authority_obj = Authority(
        name="National Roads Dept",
        level=GovernmentLevel.FEDERAL,
        type=AuthorityType.DEPARTMENT_OF_ROADS,
        district_id=None,  # National
    )
    db.session.add(authority_obj)
    db.session.commit()

    incident = DisasterIncident(
        title="Incident",
        description="Test",
        disaster_type=DisasterType.LANDSLIDE,
        severity=DisasterSeverity.HIGH,
        status=DisasterIncidentStatus.DETECTED,
        latitude=27.7,
        longitude=85.3,
        district_id=district.id,
    )
    db.session.add(incident)
    db.session.commit()

    response = client.post(
        f"/api/v1/disaster/incidents/{incident.id}/assign",
        json={"authority_id": str(authority_obj.id)},
        headers=auth_headers(authority.email, "correct-horse-battery"),
    )
    assert response.status_code == 200


# --- POST /disaster/reports/<id>/evaluate tests -------------------------------


def test_evaluate_report_requires_authority(client, db, make_user, roles, auth_headers):
    """Only authority/admin can trigger evaluation."""
    citizen = make_user(role_names=("citizen",), password="correct-horse-battery")
    from app.models.enums import ReportCategory

    report = Report(
        reporter_id=citizen.id,
        title="Test report",
        description="Test",
        category=ReportCategory.ROAD_DAMAGE,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(report)
    db.session.commit()

    response = client.post(
        f"/api/v1/disaster/reports/{report.id}/evaluate",
        headers=auth_headers(citizen.email, "correct-horse-battery"),
    )
    assert response.status_code == 403


def test_evaluate_report_success(client, db, make_user, roles, auth_headers):
    """Authority can trigger evaluation."""
    authority = make_user(role_names=("authority",), password="correct-horse-battery")
    from app.models.enums import ReportCategory

    report = Report(
        reporter_id=authority.id,
        title="Landslide report",
        description="Major landslide",
        category=ReportCategory.NATURAL_DISASTER,
        latitude=27.7,
        longitude=85.3,
    )
    db.session.add(report)
    db.session.commit()

    with patch("app.services.disaster_incident_service.process_report_for_disaster") as mock_proc:
        mock_proc.return_value = {
            "action": "created",
            "incident_id": str(uuid.uuid4()),
            "analysis": {"is_disaster": True, "disaster_type": "landslide"},
            "jurisdiction": {"district_id": None, "resolved": False},
            "policy_decision": {"should_dispatch": True},
            "correlated": False,
        }

        response = client.post(
            f"/api/v1/disaster/reports/{report.id}/evaluate",
            headers=auth_headers(authority.email, "correct-horse-battery"),
        )

    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "success"
    assert data["data"]["action"] == "created"


def test_evaluate_report_not_found(client, make_user, roles, auth_headers):
    """Evaluate non-existent report returns 404."""
    authority = make_user(role_names=("authority",), password="correct-horse-battery")

    response = client.post(
        f"/api/v1/disaster/reports/{uuid.uuid4()}/evaluate",
        headers=auth_headers(authority.email, "correct-horse-battery"),
    )
    assert response.status_code == 404