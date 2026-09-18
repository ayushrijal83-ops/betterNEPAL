"""Phase 8: projects, contractors and the progress ledger.

Runs on SQLite. All authorities, districts and coordinates below are SYNTHETIC
test data, not real Nepali bodies or places.
"""
import uuid
from datetime import date, timedelta

import pytest
import sqlalchemy as sa

from app.models import (
    Authority,
    AuthorityType,
    District,
    GovernmentLevel,
    Incident,
    IncidentStatus,
    Municipality,
    ProgressUpdate,
    Project,
    ProjectStatus,
)
from app.services import project_service

PASSWORD = "correct-horse-battery"

VALID_REPORT = {
    "title": "Collapsed culvert on the link road",
    "description": "The culvert has given way and the road is impassable.",
    "category": "road_damage",
    "lat": 0.5,
    "lng": 0.5,
}


@pytest.fixture
def areas(db):
    district = District(name="Testland", province="TestProvince", code="D-01")
    db.session.add(district)
    db.session.commit()
    municipality = Municipality(district_id=district.id, name="Testville", code="M-01")
    db.session.add(municipality)
    db.session.commit()
    return {"district": district, "municipality": municipality}


@pytest.fixture
def located(monkeypatch, areas):
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


@pytest.fixture
def authority_record(db, areas):
    record = Authority(
        name="Test Roads Office - Testland",
        level=GovernmentLevel.PROVINCIAL,
        type=AuthorityType.DEPARTMENT_OF_ROADS,
        district_id=areas["district"].id,
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture
def citizen_headers(make_user, auth_headers):
    make_user(email="citizen@betternepal.np", role_names=("citizen",))
    return auth_headers("citizen@betternepal.np", PASSWORD)


@pytest.fixture
def authority_headers(make_user, auth_headers):
    make_user(email="officer@betternepal.np", role_names=("authority",))
    return auth_headers("officer@betternepal.np", PASSWORD)


@pytest.fixture
def admin_headers(make_user, auth_headers):
    make_user(email="admin@betternepal.np", role_names=("admin",))
    return auth_headers("admin@betternepal.np", PASSWORD)


@pytest.fixture
def contractor(make_user):
    return make_user(email="builder@betternepal.np", role_names=("contractor",))


@pytest.fixture
def contractor_headers(contractor, auth_headers):
    return auth_headers("builder@betternepal.np", PASSWORD)


@pytest.fixture
def other_contractor(make_user):
    return make_user(email="rival@betternepal.np", role_names=("contractor",))


@pytest.fixture
def other_contractor_headers(other_contractor, auth_headers):
    return auth_headers("rival@betternepal.np", PASSWORD)


def _payload(authority_record, **overrides):
    return {
        "title": "Rebuild the collapsed culvert",
        "description": "Replace the failed culvert and reinstate the road surface.",
        "authority_id": str(authority_record.id),
        **overrides,
    }


def _create_project(client, headers, authority_record, **overrides):
    return client.post(
        "/api/v1/projects", json=_payload(authority_record, **overrides), headers=headers
    )


def _project_id(client, headers, authority_record, **overrides):
    response = _create_project(client, headers, authority_record, **overrides)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["project"]["id"]


def _incident_id(client, citizen_headers, authority_headers):
    report = client.post("/api/v1/reports", json=VALID_REPORT, headers=citizen_headers)
    assert report.status_code == 201
    incident = client.post(
        "/api/v1/incidents/from-report",
        json={"report_id": report.get_json()["data"]["report"]["id"]},
        headers=authority_headers,
    )
    assert incident.status_code == 201
    return incident.get_json()["data"]["incident"]["id"]


# --- creation --------------------------------------------------------------


def test_authority_can_create_a_project(client, authority_headers, authority_record):
    response = _create_project(client, authority_headers, authority_record)
    assert response.status_code == 201

    project = response.get_json()["data"]["project"]
    assert project["title"] == "Rebuild the collapsed culvert"
    assert project["status"] == "planned"
    assert project["contractor_id"] is None
    assert project["authority"] == "Test Roads Office - Testland"


def test_admin_can_create_a_project(client, admin_headers, authority_record):
    assert _create_project(client, admin_headers, authority_record).status_code == 201


def test_creation_uses_the_locked_success_envelope(client, admin_headers, authority_record):
    body = _create_project(client, admin_headers, authority_record).get_json()
    assert set(body) == {"status", "data"}
    assert body["status"] == "success"


def test_project_can_be_linked_to_an_incident(
    client, citizen_headers, authority_headers, located, authority_record
):
    incident_id = _incident_id(client, citizen_headers, authority_headers)
    response = _create_project(
        client, authority_headers, authority_record, incident_id=incident_id
    )
    assert response.status_code == 201
    assert response.get_json()["data"]["project"]["incident"]["id"] == incident_id


def test_proactive_project_needs_no_incident(client, authority_headers, authority_record):
    """Scheduled maintenance is real work with no citizen report behind it."""
    response = _create_project(client, authority_headers, authority_record)
    assert response.status_code == 201
    assert response.get_json()["data"]["project"]["incident_id"] is None


def test_project_with_an_unknown_authority_is_rejected(client, authority_headers, db):
    response = client.post(
        "/api/v1/projects",
        json={
            "title": "Orphan project",
            "description": "This has no commissioning body.",
            "authority_id": str(uuid.uuid4()),
        },
        headers=authority_headers,
    )
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "authority_not_found"


def test_project_with_an_unknown_incident_is_rejected(
    client, authority_headers, authority_record
):
    response = _create_project(
        client, authority_headers, authority_record, incident_id=str(uuid.uuid4())
    )
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "incident_not_found"


def test_dates_are_stored(client, authority_headers, authority_record):
    start = date.today().isoformat()
    end = (date.today() + timedelta(days=30)).isoformat()
    response = _create_project(
        client, authority_headers, authority_record, start_date=start, estimated_end_date=end
    )
    project = response.get_json()["data"]["project"]
    assert project["start_date"] == start
    assert project["estimated_end_date"] == end
    assert project["actual_end_date"] is None


def test_client_cannot_set_the_initial_status(client, authority_headers, authority_record):
    response = _create_project(client, authority_headers, authority_record, status="completed")
    assert response.get_json()["data"]["project"]["status"] == "planned"


def test_client_cannot_set_the_completion_date(client, authority_headers, authority_record):
    """actual_end_date is stamped by the service when work is declared done."""
    response = _create_project(
        client, authority_headers, authority_record, actual_end_date="2020-01-01"
    )
    assert response.get_json()["data"]["project"]["actual_end_date"] is None


# --- authorization ---------------------------------------------------------


def test_citizen_cannot_create_a_project(client, citizen_headers, authority_record):
    response = _create_project(client, citizen_headers, authority_record)
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "permission_denied"


def test_contractor_cannot_create_a_project(client, contractor_headers, authority_record):
    """Contractors do the work; authorities commission it."""
    assert _create_project(client, contractor_headers, authority_record).status_code == 403


def test_unauthenticated_creation_is_401(client, authority_record):
    response = client.post("/api/v1/projects", json=_payload(authority_record))
    assert response.status_code == 401


def test_failed_creation_writes_nothing(client, citizen_headers, authority_record, db):
    _create_project(client, citizen_headers, authority_record)
    assert db.session.scalar(sa.select(sa.func.count()).select_from(Project)) == 0


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize("title", ["", "abc", "x" * 151])
def test_invalid_title_is_rejected(client, authority_headers, authority_record, title):
    response = _create_project(client, authority_headers, authority_record, title=title)
    assert response.status_code == 400
    assert "title" in response.get_json()["error"]["details"]


def test_missing_description_is_rejected(client, authority_headers, authority_record):
    payload = _payload(authority_record)
    del payload["description"]
    response = client.post("/api/v1/projects", json=payload, headers=authority_headers)
    assert response.status_code == 400
    assert "description" in response.get_json()["error"]["details"]


def test_missing_authority_is_rejected(client, authority_headers, authority_record):
    payload = _payload(authority_record)
    del payload["authority_id"]
    response = client.post("/api/v1/projects", json=payload, headers=authority_headers)
    assert response.status_code == 400
    assert "authority_id" in response.get_json()["error"]["details"]


@pytest.mark.parametrize("value", ["01-01-2026", "next tuesday", "2026-13-01", 20260101])
def test_invalid_date_format_is_rejected(client, authority_headers, authority_record, value):
    """Strict ISO only: guessing day-first vs month-first stores wrong dates."""
    response = _create_project(client, authority_headers, authority_record, start_date=value)
    assert response.status_code == 400
    assert "start_date" in response.get_json()["error"]["details"]


def test_end_date_before_start_date_is_rejected(client, authority_headers, authority_record):
    response = _create_project(
        client,
        authority_headers,
        authority_record,
        start_date="2026-06-01",
        estimated_end_date="2026-05-01",
    )
    assert response.status_code == 400
    assert "estimated_end_date" in response.get_json()["error"]["details"]


def test_non_object_body_is_rejected(client, authority_headers, db):
    response = client.post("/api/v1/projects", json=["nope"], headers=authority_headers)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_payload"


# --- contractor assignment -------------------------------------------------


def test_authority_can_assign_a_contractor(
    client, authority_headers, authority_record, contractor
):
    project_id = _project_id(client, authority_headers, authority_record)
    response = client.patch(
        f"/api/v1/projects/{project_id}/contractor",
        json={"contractor_id": str(contractor.id)},
        headers=authority_headers,
    )
    assert response.status_code == 200

    project = response.get_json()["data"]["project"]
    assert project["contractor_id"] == str(contractor.id)
    assert project["contractor"]["full_name"] == contractor.full_name


def test_assigning_a_user_without_the_contractor_role_is_rejected(
    client, authority_headers, authority_record, make_user
):
    """A citizen account must not be awarded work."""
    citizen = make_user(email="nobody@betternepal.np", role_names=("citizen",))
    project_id = _project_id(client, authority_headers, authority_record)

    response = client.patch(
        f"/api/v1/projects/{project_id}/contractor",
        json={"contractor_id": str(citizen.id)},
        headers=authority_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "not_a_contractor"
    assert response.get_json()["error"]["details"]["roles"] == ["citizen"]


def test_assigning_an_authority_user_is_rejected(
    client, authority_headers, authority_record, make_user
):
    officer = make_user(email="other@betternepal.np", role_names=("authority",))
    project_id = _project_id(client, authority_headers, authority_record)
    response = client.patch(
        f"/api/v1/projects/{project_id}/contractor",
        json={"contractor_id": str(officer.id)},
        headers=authority_headers,
    )
    assert response.status_code == 409


def test_assigning_a_disabled_contractor_is_rejected(
    client, authority_headers, authority_record, make_user
):
    disabled = make_user(
        email="gone@betternepal.np", role_names=("contractor",), is_active=False
    )
    project_id = _project_id(client, authority_headers, authority_record)
    response = client.patch(
        f"/api/v1/projects/{project_id}/contractor",
        json={"contractor_id": str(disabled.id)},
        headers=authority_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "contractor_disabled"


def test_assigning_an_unknown_user_returns_404(client, authority_headers, authority_record):
    project_id = _project_id(client, authority_headers, authority_record)
    response = client.patch(
        f"/api/v1/projects/{project_id}/contractor",
        json={"contractor_id": str(uuid.uuid4())},
        headers=authority_headers,
    )
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "user_not_found"


def test_contractor_cannot_assign_themselves(
    client, authority_headers, contractor_headers, authority_record, contractor
):
    project_id = _project_id(client, authority_headers, authority_record)
    response = client.patch(
        f"/api/v1/projects/{project_id}/contractor",
        json={"contractor_id": str(contractor.id)},
        headers=contractor_headers,
    )
    assert response.status_code == 403


def test_contractor_can_be_removed(
    client, authority_headers, authority_record, contractor
):
    project_id = _project_id(client, authority_headers, authority_record)
    client.patch(
        f"/api/v1/projects/{project_id}/contractor",
        json={"contractor_id": str(contractor.id)},
        headers=authority_headers,
    )

    response = client.delete(
        f"/api/v1/projects/{project_id}/contractor", headers=authority_headers
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["project"]["contractor_id"] is None


def test_removing_an_absent_contractor_is_refused(
    client, authority_headers, authority_record
):
    project_id = _project_id(client, authority_headers, authority_record)
    response = client.delete(
        f"/api/v1/projects/{project_id}/contractor", headers=authority_headers
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "no_contractor_assigned"


# --- progress ledger -------------------------------------------------------


@pytest.fixture
def assigned_project(client, authority_headers, authority_record, contractor):
    """A project awarded to the `contractor` fixture."""
    project_id = _project_id(client, authority_headers, authority_record)
    client.patch(
        f"/api/v1/projects/{project_id}/contractor",
        json={"contractor_id": str(contractor.id)},
        headers=authority_headers,
    )
    return project_id


def test_contractor_can_post_a_routine_update(client, contractor_headers, assigned_project, db):
    response = client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Materials delivered to site this morning."},
        headers=contractor_headers,
    )
    assert response.status_code == 201

    update = response.get_json()["data"]["update"]
    assert update["notes"] == "Materials delivered to site this morning."
    assert update["is_status_change"] is False
    assert update["new_status"] is None
    assert db.session.scalar(sa.select(sa.func.count()).select_from(ProgressUpdate)) == 1


def test_a_routine_update_does_not_change_the_status(client, contractor_headers, assigned_project):
    client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Materials delivered to site."},
        headers=contractor_headers,
    )
    project = client.get(f"/api/v1/projects/{assigned_project}").get_json()["data"]["project"]
    assert project["status"] == "planned"


def test_an_update_records_its_author(client, contractor, contractor_headers, assigned_project):
    response = client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Work commenced on site."},
        headers=contractor_headers,
    )
    author = response.get_json()["data"]["update"]["author"]
    assert author["id"] == str(contractor.id)
    assert author["full_name"] == contractor.full_name


def test_a_status_change_writes_a_ledger_entry(
    client, contractor_headers, assigned_project, db
):
    """Status never moves without a record of who moved it and why."""
    response = client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Crew mobilised, work starting.", "new_status": "active"},
        headers=contractor_headers,
    )
    assert response.status_code == 201
    assert response.get_json()["data"]["project"]["status"] == "active"
    assert response.get_json()["data"]["previous_status"] == "planned"

    update = db.session.scalar(sa.select(ProgressUpdate))
    assert update.previous_status == ProjectStatus.PLANNED
    assert update.new_status == ProjectStatus.ACTIVE
    assert update.is_status_change is True


def test_going_active_stamps_the_start_date(client, contractor_headers, assigned_project):
    response = client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Crew mobilised.", "new_status": "active"},
        headers=contractor_headers,
    )
    assert response.get_json()["data"]["project"]["start_date"] == date.today().isoformat()


def test_ledger_is_returned_newest_first(client, contractor_headers, assigned_project):
    for note in ("First note on site.", "Second note on site.", "Third note on site."):
        client.post(
            f"/api/v1/projects/{assigned_project}/updates",
            json={"notes": note},
            headers=contractor_headers,
        )

    updates = client.get(f"/api/v1/projects/{assigned_project}/updates").get_json()["data"][
        "updates"
    ]
    assert len(updates) == 3
    assert updates[0]["notes"] == "Third note on site."
    assert updates[-1]["notes"] == "First note on site."


def test_project_detail_embeds_the_ledger(client, contractor_headers, assigned_project):
    client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Work in progress on site."},
        headers=contractor_headers,
    )
    project = client.get(f"/api/v1/projects/{assigned_project}").get_json()["data"]["project"]
    assert project["update_count"] == 1
    assert len(project["progress_updates"]) == 1


def test_notes_are_required_even_on_a_status_change(client, contractor_headers, assigned_project):
    """A transition with no explanation is the opacity the ledger removes."""
    response = client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"new_status": "active"},
        headers=contractor_headers,
    )
    assert response.status_code == 400
    assert "notes" in response.get_json()["error"]["details"]


@pytest.mark.parametrize("status", ["finished", "done", "paused", 4])
def test_invalid_status_value_is_rejected(client, contractor_headers, assigned_project, status):
    response = client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Attempting a bad transition.", "new_status": status},
        headers=contractor_headers,
    )
    assert response.status_code == 400
    assert "new_status" in response.get_json()["error"]["details"]


# --- ledger authorization --------------------------------------------------


def test_a_contractor_cannot_post_to_another_contractors_project(
    client, other_contractor_headers, assigned_project, db
):
    """Object-level check: the role alone is not enough."""
    response = client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Posting against a competitor's job."},
        headers=other_contractor_headers,
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "not_project_contractor"
    assert db.session.scalar(sa.select(sa.func.count()).select_from(ProgressUpdate)) == 0


def test_a_contractor_cannot_post_to_an_unassigned_project(
    client, contractor_headers, authority_headers, authority_record
):
    project_id = _project_id(client, authority_headers, authority_record)
    response = client.post(
        f"/api/v1/projects/{project_id}/updates",
        json={"notes": "Nobody awarded me this job."},
        headers=contractor_headers,
    )
    assert response.status_code == 403


def test_authority_can_post_to_any_project(client, authority_headers, assigned_project):
    """Authorities oversee all work they commissioned."""
    response = client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Site inspection carried out by the office."},
        headers=authority_headers,
    )
    assert response.status_code == 201


def test_admin_can_post_to_any_project(client, admin_headers, assigned_project):
    response = client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Administrative note added to the record."},
        headers=admin_headers,
    )
    assert response.status_code == 201


def test_citizen_cannot_post_an_update(client, citizen_headers, assigned_project):
    response = client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "I think this is going slowly."},
        headers=citizen_headers,
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "permission_denied"


def test_unauthenticated_update_is_401(client, assigned_project):
    response = client.post(
        f"/api/v1/projects/{assigned_project}/updates", json={"notes": "Anonymous note."}
    )
    assert response.status_code == 401


# --- completion cascade ----------------------------------------------------


@pytest.fixture
def project_with_incident(
    client, citizen_headers, authority_headers, located, authority_record, contractor
):
    """An ACTIVE project linked to an OPEN incident, awarded to the contractor."""
    incident_id = _incident_id(client, citizen_headers, authority_headers)
    project_id = _project_id(
        client, authority_headers, authority_record, incident_id=incident_id
    )
    client.patch(
        f"/api/v1/projects/{project_id}/contractor",
        json={"contractor_id": str(contractor.id)},
        headers=authority_headers,
    )
    client.post(
        f"/api/v1/projects/{project_id}/updates",
        json={"notes": "Crew mobilised on site.", "new_status": "active"},
        headers=authority_headers,
    )
    return {"project_id": project_id, "incident_id": incident_id}


def test_completing_a_project_resolves_the_linked_incident(
    client, contractor_headers, project_with_incident, db
):
    response = client.post(
        f"/api/v1/projects/{project_with_incident['project_id']}/updates",
        json={"notes": "Culvert rebuilt and road reinstated.", "new_status": "completed"},
        headers=contractor_headers,
    )
    assert response.status_code == 201
    assert response.get_json()["data"]["project"]["status"] == "completed"
    assert response.get_json()["data"]["incident_not_resolved_because"] is None

    incident = db.session.get(Incident, uuid.UUID(project_with_incident["incident_id"]))
    assert incident.status == IncidentStatus.RESOLVED


def test_completing_stamps_the_actual_end_date(client, contractor_headers, project_with_incident):
    response = client.post(
        f"/api/v1/projects/{project_with_incident['project_id']}/updates",
        json={"notes": "All works finished and signed off.", "new_status": "completed"},
        headers=contractor_headers,
    )
    assert (
        response.get_json()["data"]["project"]["actual_end_date"]
        == date.today().isoformat()
    )


def test_completing_a_project_with_no_incident_is_fine(
    client, contractor_headers, assigned_project
):
    client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Work started on the maintenance job.", "new_status": "active"},
        headers=contractor_headers,
    )
    response = client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Maintenance completed.", "new_status": "completed"},
        headers=contractor_headers,
    )
    assert response.status_code == 201
    assert response.get_json()["data"]["incident_not_resolved_because"] == "no_linked_incident"


def test_a_closed_incident_is_not_forced_open_by_completion(
    client, authority_headers, contractor_headers, project_with_incident, db
):
    """CLOSED is terminal (Phase 6). The project still completes; the caller is
    told why the incident did not move."""
    client.patch(
        f"/api/v1/incidents/{project_with_incident['incident_id']}",
        json={"status": "closed"},
        headers=authority_headers,
    )

    response = client.post(
        f"/api/v1/projects/{project_with_incident['project_id']}/updates",
        json={"notes": "Works completed after the incident was closed.", "new_status": "completed"},
        headers=contractor_headers,
    )
    assert response.status_code == 201
    assert response.get_json()["data"]["project"]["status"] == "completed"
    assert (
        response.get_json()["data"]["incident_not_resolved_because"] == "incident_closed"
    )

    incident = db.session.get(Incident, uuid.UUID(project_with_incident["incident_id"]))
    assert incident.status == IncidentStatus.CLOSED


def test_cancelling_does_not_resolve_the_incident(
    client, contractor_headers, authority_headers, project_with_incident, db
):
    """Abandoned work is not fixed work."""
    client.post(
        f"/api/v1/projects/{project_with_incident['project_id']}/updates",
        json={"notes": "Contract terminated, work abandoned.", "new_status": "cancelled"},
        headers=authority_headers,
    )
    incident = db.session.get(Incident, uuid.UUID(project_with_incident["incident_id"]))
    assert incident.status != IncidentStatus.RESOLVED


# --- status transitions ----------------------------------------------------


def test_full_lifecycle(client, authority_headers, assigned_project):
    for status in ("active", "on_hold", "active", "completed"):
        response = client.post(
            f"/api/v1/projects/{assigned_project}/updates",
            json={"notes": f"Moving the project to {status}.", "new_status": status},
            headers=authority_headers,
        )
        assert response.status_code == 201, status
    assert response.get_json()["data"]["project"]["status"] == "completed"


def test_on_hold_cannot_jump_straight_to_completed(client, authority_headers, assigned_project):
    """Paused work must be resumed before it can be declared done."""
    for status in ("active", "on_hold"):
        client.post(
            f"/api/v1/projects/{assigned_project}/updates",
            json={"notes": f"Moving to {status}.", "new_status": status},
            headers=authority_headers,
        )

    response = client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Declaring done from hold.", "new_status": "completed"},
        headers=authority_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "invalid_status_transition"
    assert response.get_json()["error"]["details"]["current_status"] == "on_hold"


def test_a_completed_project_is_terminal(client, authority_headers, assigned_project):
    for status in ("active", "completed"):
        client.post(
            f"/api/v1/projects/{assigned_project}/updates",
            json={"notes": f"Moving to {status}.", "new_status": status},
            headers=authority_headers,
        )

    response = client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Trying to reopen the project.", "new_status": "active"},
        headers=authority_headers,
    )
    assert response.status_code == 409


def test_a_completed_project_accepts_no_further_notes(client, authority_headers, assigned_project):
    for status in ("active", "completed"):
        client.post(
            f"/api/v1/projects/{assigned_project}/updates",
            json={"notes": f"Moving to {status}.", "new_status": status},
            headers=authority_headers,
        )

    response = client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "A late note after completion."},
        headers=authority_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "project_finished"


def test_a_cancelled_project_cannot_be_reassigned(
    client, authority_headers, assigned_project, other_contractor
):
    client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Contract cancelled.", "new_status": "cancelled"},
        headers=authority_headers,
    )
    response = client.patch(
        f"/api/v1/projects/{assigned_project}/contractor",
        json={"contractor_id": str(other_contractor.id)},
        headers=authority_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "project_finished"


def test_setting_the_same_status_is_refused(client, authority_headers, assigned_project):
    response = client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Restating the current status.", "new_status": "planned"},
        headers=authority_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "status_unchanged"


# --- reading and filtering -------------------------------------------------


def test_listing_projects_is_public(client, authority_headers, authority_record):
    _project_id(client, authority_headers, authority_record)
    response = client.get("/api/v1/projects")
    assert response.status_code == 200
    assert len(response.get_json()["data"]["projects"]) == 1


def test_empty_list_is_not_an_error(client, db):
    data = client.get("/api/v1/projects").get_json()["data"]
    assert data["projects"] == []
    assert data["pagination"]["total"] == 0


def test_getting_a_project_is_public(client, authority_headers, authority_record):
    project_id = _project_id(client, authority_headers, authority_record)
    response = client.get(f"/api/v1/projects/{project_id}")
    assert response.status_code == 200
    assert response.get_json()["data"]["project"]["id"] == project_id


def test_unknown_project_returns_404(client, db):
    response = client.get(f"/api/v1/projects/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "project_not_found"


def test_invalid_project_id_returns_400(client, db):
    response = client.get("/api/v1/projects/not-a-uuid")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_identifier"


def test_project_response_does_not_leak_emails(
    client, authority_headers, assigned_project, contractor_headers
):
    client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Work progressing on site."},
        headers=contractor_headers,
    )
    body = client.get(f"/api/v1/projects/{assigned_project}").get_data(as_text=True)
    assert "builder@betternepal.np" not in body
    assert "password_hash" not in body
    assert "argon2" not in body


def test_filter_by_status(client, authority_headers, authority_record, assigned_project):
    _project_id(client, authority_headers, authority_record, title="A second planned job")
    client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Starting work.", "new_status": "active"},
        headers=authority_headers,
    )

    active = client.get("/api/v1/projects?status=active").get_json()["data"]
    planned = client.get("/api/v1/projects?status=planned").get_json()["data"]
    assert len(active["projects"]) == 1
    assert len(planned["projects"]) == 1


def test_filter_by_contractor(client, authority_headers, authority_record, assigned_project, contractor):
    _project_id(client, authority_headers, authority_record, title="An unassigned job here")

    data = client.get(f"/api/v1/projects?contractor_id={contractor.id}").get_json()["data"]
    assert len(data["projects"]) == 1
    assert data["projects"][0]["id"] == assigned_project


def test_filter_by_authority(client, authority_headers, authority_record):
    _project_id(client, authority_headers, authority_record)
    hit = client.get(f"/api/v1/projects?authority_id={authority_record.id}").get_json()["data"]
    miss = client.get(f"/api/v1/projects?authority_id={uuid.uuid4()}").get_json()["data"]
    assert len(hit["projects"]) == 1
    assert miss["projects"] == []


def test_filter_by_incident(
    client, citizen_headers, authority_headers, located, authority_record
):
    incident_id = _incident_id(client, citizen_headers, authority_headers)
    _project_id(client, authority_headers, authority_record, incident_id=incident_id)
    _project_id(client, authority_headers, authority_record, title="Unrelated maintenance job")

    data = client.get(f"/api/v1/projects?incident_id={incident_id}").get_json()["data"]
    assert len(data["projects"]) == 1


def test_filter_unassigned(client, authority_headers, authority_record, assigned_project):
    _project_id(client, authority_headers, authority_record, title="An unassigned job here")
    data = client.get("/api/v1/projects?unassigned=true").get_json()["data"]
    assert len(data["projects"]) == 1
    assert data["projects"][0]["contractor_id"] is None


def test_unknown_filter_value_is_a_400(client, db):
    response = client.get("/api/v1/projects?status=nonsense")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_filter"


def test_pagination(client, authority_headers, authority_record):
    for index in range(3):
        _project_id(client, authority_headers, authority_record, title=f"Job number {index} here")

    data = client.get("/api/v1/projects?page=1&per_page=2").get_json()["data"]
    assert len(data["projects"]) == 2
    assert data["pagination"] == {"page": 1, "per_page": 2, "total": 3, "pages": 2}


# --- overdue ---------------------------------------------------------------


def test_a_past_due_project_is_flagged_overdue(client, authority_headers, authority_record):
    past = (date.today() - timedelta(days=5)).isoformat()
    project_id = _project_id(
        client, authority_headers, authority_record, estimated_end_date=past
    )
    project = client.get(f"/api/v1/projects/{project_id}").get_json()["data"]["project"]
    assert project["is_overdue"] is True


def test_a_completed_project_is_never_overdue(client, authority_headers, authority_record, contractor):
    past = (date.today() - timedelta(days=5)).isoformat()
    project_id = _project_id(
        client, authority_headers, authority_record, estimated_end_date=past
    )
    for status in ("active", "completed"):
        client.post(
            f"/api/v1/projects/{project_id}/updates",
            json={"notes": f"Moving to {status}.", "new_status": status},
            headers=authority_headers,
        )

    project = client.get(f"/api/v1/projects/{project_id}").get_json()["data"]["project"]
    assert project["is_overdue"] is False


def test_a_project_without_an_estimate_is_never_overdue(client, authority_headers, authority_record):
    project_id = _project_id(client, authority_headers, authority_record)
    project = client.get(f"/api/v1/projects/{project_id}").get_json()["data"]["project"]
    assert project["is_overdue"] is False


# --- model and database ----------------------------------------------------


def test_project_requires_an_authority(db, areas):
    db.session.add(
        Project(title="No commissioning body", description="x" * 20)
    )
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_project_cannot_reference_a_nonexistent_authority(db):
    db.session.add(
        Project(
            title="Ghost authority", description="x" * 20, authority_id=uuid.uuid4()
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_database_rejects_finishing_before_starting(db, authority_record):
    """You cannot complete work before you began it."""
    db.session.add(
        Project(
            title="Time travelling project",
            description="x" * 20,
            authority_id=authority_record.id,
            start_date=date(2026, 6, 1),
            actual_end_date=date(2026, 5, 1),
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_a_project_may_start_after_its_estimated_end_date(db, authority_record):
    """A late start is exactly what "overdue" means - it must be recordable.

    The creation validator still rejects the two dates arriving inverted in one
    request; the database does not forbid the state arising later.
    """
    project = Project(
        title="Started late",
        description="x" * 20,
        authority_id=authority_record.id,
        estimated_end_date=date(2026, 5, 1),
        start_date=date(2026, 6, 1),
    )
    db.session.add(project)
    db.session.commit()
    assert project.start_date > project.estimated_end_date


def test_deleting_an_authority_with_projects_is_refused(db, authority_record):
    db.session.add(
        Project(title="Ongoing works", description="x" * 20, authority_id=authority_record.id)
    )
    db.session.commit()

    db.session.delete(authority_record)
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_deleting_a_project_deletes_its_ledger(
    client, contractor_headers, assigned_project, db
):
    """CASCADE: the ledger belongs to the project and is meaningless without it."""
    client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "A note that should not outlive the project."},
        headers=contractor_headers,
    )
    assert db.session.scalar(sa.select(sa.func.count()).select_from(ProgressUpdate)) == 1

    db.session.delete(db.session.get(Project, uuid.UUID(assigned_project)))
    db.session.commit()
    assert db.session.scalar(sa.select(sa.func.count()).select_from(ProgressUpdate)) == 0


def test_deleting_an_incident_preserves_its_project(
    client, citizen_headers, authority_headers, located, authority_record, db
):
    """SET NULL: the work outlives the incident record it was raised from."""
    incident_id = _incident_id(client, citizen_headers, authority_headers)
    project_id = _project_id(
        client, authority_headers, authority_record, incident_id=incident_id
    )

    db.session.delete(db.session.get(Incident, uuid.UUID(incident_id)))
    db.session.commit()

    project = db.session.get(Project, uuid.UUID(project_id))
    assert project is not None
    assert project.incident_id is None


def test_deleting_a_ledger_author_is_refused(
    client, contractor, contractor_headers, assigned_project, db
):
    """RESTRICT: who said this is the point of the record."""
    client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "A note tied to its author."},
        headers=contractor_headers,
    )
    db.session.delete(contractor)
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_progress_update_has_no_updated_at_column():
    """Ledger rows are never edited, so they carry no modification timestamp."""
    assert "updated_at" not in ProgressUpdate.__table__.columns
    assert "created_at" in ProgressUpdate.__table__.columns


def test_project_defaults(db, authority_record):
    project = Project(
        title="Defaults check", description="x" * 20, authority_id=authority_record.id
    )
    db.session.add(project)
    db.session.commit()
    assert project.status == ProjectStatus.PLANNED
    assert project.contractor_id is None
    assert project.actual_end_date is None
    assert project.update_count == 0


def test_relationships_are_navigable(
    client, contractor, contractor_headers, authority_record, assigned_project, db
):
    client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "Checking the relationship graph."},
        headers=contractor_headers,
    )

    project = db.session.get(Project, uuid.UUID(assigned_project))
    update = db.session.scalar(sa.select(ProgressUpdate))

    assert project.authority is authority_record
    assert project in authority_record.projects
    assert project.contractor is contractor
    assert project in contractor.contracted_projects
    assert update.project is project
    assert update in project.progress_updates
    assert update.author is contractor
    assert update in contractor.progress_updates


def test_can_post_update_helper(db, authority_record, contractor, other_contractor, make_user):
    project = Project(
        title="Permission check",
        description="x" * 20,
        authority_id=authority_record.id,
        contractor_id=contractor.id,
    )
    db.session.add(project)
    db.session.commit()

    admin = make_user(email="boss@betternepal.np", role_names=("admin",))
    assert project_service.can_post_update(project, contractor) is True
    assert project_service.can_post_update(project, other_contractor) is False
    assert project_service.can_post_update(project, admin) is True


# --- statistics ------------------------------------------------------------


def test_statistics_on_an_empty_database(client, db):
    data = client.get("/api/v1/projects/statistics").get_json()["data"]
    assert data["total"] == 0
    assert set(data["by_status"]) == {status.value for status in ProjectStatus}
    assert data["progress_updates"] == 0


def test_statistics_count_projects_and_updates(
    client, authority_headers, authority_record, assigned_project, contractor_headers
):
    _project_id(client, authority_headers, authority_record, title="An unassigned job here")
    client.post(
        f"/api/v1/projects/{assigned_project}/updates",
        json={"notes": "One note for the ledger."},
        headers=contractor_headers,
    )

    data = client.get("/api/v1/projects/statistics").get_json()["data"]
    assert data["total"] == 2
    assert data["by_status"]["planned"] == 2
    assert data["without_contractor"] == 1
    assert data["progress_updates"] == 1


def test_statistics_count_overdue(client, authority_headers, authority_record):
    past = (date.today() - timedelta(days=5)).isoformat()
    _project_id(client, authority_headers, authority_record, estimated_end_date=past)
    _project_id(client, authority_headers, authority_record, title="A job with no deadline")

    data = client.get("/api/v1/projects/statistics").get_json()["data"]
    assert data["overdue"] == 1


def test_statistics_path_is_not_shadowed_by_the_id_route(client, db):
    assert client.get("/api/v1/projects/statistics").status_code == 200


# --- earlier phases still work ---------------------------------------------


def test_previous_endpoints_are_unaffected(client, db):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/map/districts").status_code == 200
    assert client.get("/api/v1/reports").status_code == 200
    assert client.get("/api/v1/incidents").status_code == 200
    assert client.get("/api/v1/authorities").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
