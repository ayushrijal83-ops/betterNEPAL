"""Phase 7: authorities and incident routing.

Runs on SQLite. All authorities, districts and coordinates below are SYNTHETIC
test data - they are not real Nepali government bodies and their contact
details are deliberately non-routable.
"""
import uuid

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
)
from app.services import authority_service

PASSWORD = "correct-horse-battery"

VALID_AUTHORITY = {
    "name": "Test Roads Office - Testland",
    "level": "provincial",
    "type": "department_of_roads",
    "contact_email": "roads@test.invalid",
    "contact_phone": "+977 9800000000",
}

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
def citizen_headers(make_user, auth_headers):
    make_user(email="citizen@betternepal.np", role_names=("citizen",))
    return auth_headers("citizen@betternepal.np", PASSWORD)


@pytest.fixture
def authority_user(make_user):
    return make_user(email="officer@betternepal.np", role_names=("authority",))


@pytest.fixture
def authority_headers(authority_user, auth_headers):
    return auth_headers("officer@betternepal.np", PASSWORD)


@pytest.fixture
def admin_headers(make_user, auth_headers):
    make_user(email="admin@betternepal.np", role_names=("admin",))
    return auth_headers("admin@betternepal.np", PASSWORD)


@pytest.fixture
def authority_record(db, areas):
    """A synthetic district-scoped roads authority."""
    record = Authority(
        name="Test Roads Office - Testland",
        level=GovernmentLevel.PROVINCIAL,
        type=AuthorityType.DEPARTMENT_OF_ROADS,
        contact_email="roads@test.invalid",
        district_id=areas["district"].id,
    )
    db.session.add(record)
    db.session.commit()
    return record


def _create_authority(client, headers, **overrides):
    return client.post(
        "/api/v1/authorities", json={**VALID_AUTHORITY, **overrides}, headers=headers
    )


def _incident(client, citizen_headers, authority_headers, **overrides):
    """Create a report and promote it, returning the incident id."""
    report = client.post(
        "/api/v1/reports", json={**VALID_REPORT, **overrides}, headers=citizen_headers
    )
    assert report.status_code == 201, report.get_json()
    report_id = report.get_json()["data"]["report"]["id"]

    incident = client.post(
        "/api/v1/incidents/from-report",
        json={"report_id": report_id},
        headers=authority_headers,
    )
    assert incident.status_code == 201, incident.get_json()
    return incident.get_json()["data"]["incident"]["id"]


# --- creation --------------------------------------------------------------


def test_admin_can_create_an_authority(client, admin_headers, db):
    response = _create_authority(client, admin_headers)
    assert response.status_code == 201

    authority = response.get_json()["data"]["authority"]
    assert authority["name"] == VALID_AUTHORITY["name"]
    assert authority["level"] == "provincial"
    assert authority["type"] == "department_of_roads"
    assert authority["is_national"] is True  # no district supplied


def test_creation_uses_the_locked_success_envelope(client, admin_headers, db):
    body = _create_authority(client, admin_headers).get_json()
    assert set(body) == {"status", "data"}
    assert body["status"] == "success"


def test_authority_can_be_scoped_to_a_district(client, admin_headers, areas):
    response = _create_authority(
        client, admin_headers, district_id=str(areas["district"].id)
    )
    assert response.status_code == 201

    authority = response.get_json()["data"]["authority"]
    assert authority["district"] == "Testland"
    assert authority["is_national"] is False


def test_authority_with_an_unknown_district_is_rejected(client, admin_headers, db):
    response = _create_authority(client, admin_headers, district_id=str(uuid.uuid4()))
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "district_not_found"


def test_duplicate_authority_name_is_rejected(client, admin_headers, db):
    _create_authority(client, admin_headers)
    response = _create_authority(client, admin_headers)
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "authority_name_taken"


@pytest.mark.parametrize("level", [level.value for level in GovernmentLevel])
def test_every_level_is_accepted(client, admin_headers, db, level):
    response = _create_authority(client, admin_headers, level=level, name=f"Office {level}")
    assert response.status_code == 201
    assert response.get_json()["data"]["authority"]["level"] == level


@pytest.mark.parametrize("kind", [kind.value for kind in AuthorityType])
def test_every_type_is_accepted(client, admin_headers, db, kind):
    response = _create_authority(client, admin_headers, type=kind, name=f"Office {kind}")
    assert response.status_code == 201
    assert response.get_json()["data"]["authority"]["type"] == kind


def test_contact_details_are_optional(client, admin_headers, db):
    """The research file marks some district offices 'Not listed'.

    Storing an empty string or somebody else's number would turn missing data
    into a false claim, so absent stays absent.
    """
    payload = {
        k: v
        for k, v in VALID_AUTHORITY.items()
        if k not in {"contact_email", "contact_phone"}
    }
    response = client.post("/api/v1/authorities", json=payload, headers=admin_headers)
    assert response.status_code == 201

    authority = response.get_json()["data"]["authority"]
    assert authority["contact_email"] is None
    assert authority["contact_phone"] is None


# --- authorization ---------------------------------------------------------


def test_citizen_cannot_create_an_authority(client, citizen_headers, db):
    response = _create_authority(client, citizen_headers)
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "permission_denied"


def test_authority_role_cannot_create_an_authority(client, authority_headers, db):
    """The registry is the thing routing trusts; only admins edit it."""
    response = _create_authority(client, authority_headers)
    assert response.status_code == 403


def test_unauthenticated_creation_is_401(client, db):
    response = client.post("/api/v1/authorities", json=VALID_AUTHORITY)
    assert response.status_code == 401


def test_failed_creation_writes_nothing(client, citizen_headers, db):
    _create_authority(client, citizen_headers)
    assert db.session.scalar(sa.select(sa.func.count()).select_from(Authority)) == 0


def test_citizen_cannot_update_an_authority(client, citizen_headers, authority_record):
    response = client.patch(
        f"/api/v1/authorities/{authority_record.id}",
        json={"name": "Renamed by a citizen"},
        headers=citizen_headers,
    )
    assert response.status_code == 403


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "ab", "x" * 151])
def test_invalid_name_is_rejected(client, admin_headers, db, name):
    response = _create_authority(client, admin_headers, name=name)
    assert response.status_code == 400
    assert "name" in response.get_json()["error"]["details"]


@pytest.mark.parametrize("level", ["", "municipal", "regional", 7])
def test_invalid_level_is_rejected(client, admin_headers, db, level):
    response = _create_authority(client, admin_headers, level=level)
    assert response.status_code == 400
    assert "level" in response.get_json()["error"]["details"]


@pytest.mark.parametrize("kind", ["", "roads", "sewage", 3])
def test_invalid_type_is_rejected(client, admin_headers, db, kind):
    response = _create_authority(client, admin_headers, type=kind)
    assert response.status_code == 400
    assert "type" in response.get_json()["error"]["details"]


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "spaces in@test.invalid"])
def test_invalid_contact_email_is_rejected(client, admin_headers, db, email):
    response = _create_authority(client, admin_headers, contact_email=email)
    assert response.status_code == 400
    assert "contact_email" in response.get_json()["error"]["details"]


def test_invalid_contact_phone_is_rejected(client, admin_headers, db):
    response = _create_authority(client, admin_headers, contact_phone="abc")
    assert response.status_code == 400
    assert "contact_phone" in response.get_json()["error"]["details"]


def test_all_validation_errors_are_reported_at_once(client, admin_headers, db):
    response = client.post(
        "/api/v1/authorities",
        json={"name": "x", "level": "bad", "type": "bad", "contact_email": "nope"},
        headers=admin_headers,
    )
    details = response.get_json()["error"]["details"]
    assert {"name", "level", "type", "contact_email"} <= set(details)


def test_non_object_body_is_rejected(client, admin_headers, db):
    response = client.post("/api/v1/authorities", json=["nope"], headers=admin_headers)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_payload"


# --- reading ---------------------------------------------------------------


def test_listing_authorities_is_public(client, authority_record):
    response = client.get("/api/v1/authorities")
    assert response.status_code == 200
    assert len(response.get_json()["data"]["authorities"]) == 1


def test_empty_list_is_not_an_error(client, db):
    data = client.get("/api/v1/authorities").get_json()["data"]
    assert data["authorities"] == []
    assert data["pagination"]["total"] == 0


def test_getting_an_authority_is_public(client, authority_record):
    response = client.get(f"/api/v1/authorities/{authority_record.id}")
    assert response.status_code == 200
    assert response.get_json()["data"]["authority"]["id"] == str(authority_record.id)


def test_unknown_authority_returns_404(client, db):
    response = client.get(f"/api/v1/authorities/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "authority_not_found"


def test_invalid_authority_id_returns_400(client, db):
    response = client.get("/api/v1/authorities/not-a-uuid")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_identifier"


def test_contact_details_are_published(client, authority_record):
    """These are office contacts, not personal data - publishing is the point."""
    authority = client.get(
        f"/api/v1/authorities/{authority_record.id}"
    ).get_json()["data"]["authority"]
    assert authority["contact_email"] == "roads@test.invalid"


# --- filtering -------------------------------------------------------------


@pytest.fixture
def mixed_authorities(db, areas):
    records = [
        Authority(
            name="National Roads Department",
            level=GovernmentLevel.FEDERAL,
            type=AuthorityType.DEPARTMENT_OF_ROADS,
        ),
        Authority(
            name="Testville Municipal Office",
            level=GovernmentLevel.LOCAL,
            type=AuthorityType.MUNICIPAL_OFFICE,
            district_id=areas["district"].id,
        ),
        Authority(
            name="Test Water Board",
            level=GovernmentLevel.UTILITY,
            type=AuthorityType.WATER_AUTHORITY,
            district_id=areas["district"].id,
        ),
    ]
    db.session.add_all(records)
    db.session.commit()
    return records


def test_filter_by_level(client, mixed_authorities):
    data = client.get("/api/v1/authorities?level=federal").get_json()["data"]
    assert len(data["authorities"]) == 1
    assert data["authorities"][0]["level"] == "federal"


def test_filter_by_type(client, mixed_authorities):
    data = client.get("/api/v1/authorities?type=water_authority").get_json()["data"]
    assert len(data["authorities"]) == 1


def test_filter_by_district(client, mixed_authorities, areas):
    district_id = str(areas["district"].id)
    data = client.get(f"/api/v1/authorities?district_id={district_id}").get_json()["data"]
    assert len(data["authorities"]) == 2  # the federal one has no district


def test_district_filter_can_include_national_bodies(client, mixed_authorities, areas):
    """A national body owns problems inside districts without being scoped to one."""
    district_id = str(areas["district"].id)
    data = client.get(
        f"/api/v1/authorities?district_id={district_id}&include_national=true"
    ).get_json()["data"]
    assert len(data["authorities"]) == 3


def test_search_by_name(client, mixed_authorities):
    data = client.get("/api/v1/authorities?search=Water").get_json()["data"]
    assert len(data["authorities"]) == 1


def test_unknown_filter_value_is_a_400(client, db):
    response = client.get("/api/v1/authorities?level=galactic")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_filter"


def test_pagination(client, mixed_authorities):
    data = client.get("/api/v1/authorities?page=1&per_page=2").get_json()["data"]
    assert len(data["authorities"]) == 2
    assert data["pagination"] == {"page": 1, "per_page": 2, "total": 3, "pages": 2}


# --- updating --------------------------------------------------------------


def test_admin_can_update_an_authority(client, admin_headers, authority_record):
    response = client.patch(
        f"/api/v1/authorities/{authority_record.id}",
        json={"contact_phone": "+977 9811111111"},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["authority"]["contact_phone"] == "+977 9811111111"


def test_update_leaves_unmentioned_fields_alone(client, admin_headers, authority_record):
    response = client.patch(
        f"/api/v1/authorities/{authority_record.id}",
        json={"contact_phone": "+977 9811111111"},
        headers=admin_headers,
    )
    assert response.get_json()["data"]["authority"]["contact_email"] == "roads@test.invalid"


def test_an_explicit_null_clears_a_stale_contact(client, admin_headers, authority_record):
    """Distinguishing 'absent' from 'present and null' is why the sentinel exists."""
    response = client.patch(
        f"/api/v1/authorities/{authority_record.id}",
        json={"contact_email": None},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["authority"]["contact_email"] is None


def test_an_empty_update_is_rejected(client, admin_headers, authority_record):
    response = client.patch(
        f"/api/v1/authorities/{authority_record.id}", json={}, headers=admin_headers
    )
    assert response.status_code == 400


def test_update_to_a_taken_name_is_rejected(client, admin_headers, mixed_authorities):
    response = client.patch(
        f"/api/v1/authorities/{mixed_authorities[0].id}",
        json={"name": "Test Water Board"},
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "authority_name_taken"


# --- assignment ------------------------------------------------------------


def test_authority_user_can_assign_an_incident(
    client, citizen_headers, authority_headers, located, authority_record
):
    incident_id = _incident(client, citizen_headers, authority_headers)
    response = client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={"authority_id": str(authority_record.id)},
        headers=authority_headers,
    )
    assert response.status_code == 200

    incident = response.get_json()["data"]["incident"]
    assert incident["authority_id"] == str(authority_record.id)
    assert incident["authority"]["name"] == "Test Roads Office - Testland"


def test_admin_can_assign_an_incident(
    client, citizen_headers, authority_headers, admin_headers, located, authority_record
):
    incident_id = _incident(client, citizen_headers, authority_headers)
    response = client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={"authority_id": str(authority_record.id)},
        headers=admin_headers,
    )
    assert response.status_code == 200


def test_assigning_an_open_incident_moves_it_to_in_progress(
    client, citizen_headers, authority_headers, located, authority_record
):
    """Having an owner is the work starting."""
    incident_id = _incident(client, citizen_headers, authority_headers)
    assert (
        client.get(f"/api/v1/incidents/{incident_id}").get_json()["data"]["incident"][
            "status"
        ]
        == "open"
    )

    response = client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={"authority_id": str(authority_record.id)},
        headers=authority_headers,
    )
    assert response.get_json()["data"]["incident"]["status"] == "in_progress"


def test_assignment_records_who_routed_it(
    client, citizen_headers, authority_user, authority_headers, located, authority_record
):
    incident_id = _incident(client, citizen_headers, authority_headers)
    incident = client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={"authority_id": str(authority_record.id)},
        headers=authority_headers,
    ).get_json()["data"]["incident"]

    assert incident["assigned_by"]["id"] == str(authority_user.id)
    assert incident["assigned_at"] is not None


def test_assigner_cannot_be_forged_from_the_body(
    client, citizen_headers, authority_user, authority_headers, located, authority_record, db
):
    incident_id = _incident(client, citizen_headers, authority_headers)
    client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={
            "authority_id": str(authority_record.id),
            "assigned_by_id": str(uuid.uuid4()),
        },
        headers=authority_headers,
    )
    assert db.session.scalar(sa.select(Incident)).assigned_by_id == authority_user.id


def test_reassignment_does_not_reset_progress(
    client, citizen_headers, authority_headers, located, authority_record, mixed_authorities
):
    """Correcting a misroute is not the same as restarting the work."""
    incident_id = _incident(client, citizen_headers, authority_headers)
    client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={"authority_id": str(authority_record.id)},
        headers=authority_headers,
    )
    client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "resolved"},
        headers=authority_headers,
    )

    response = client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={"authority_id": str(mixed_authorities[0].id)},
        headers=authority_headers,
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["incident"]["status"] == "resolved"


def test_citizen_cannot_assign_an_incident(
    client, citizen_headers, authority_headers, located, authority_record
):
    incident_id = _incident(client, citizen_headers, authority_headers)
    response = client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={"authority_id": str(authority_record.id)},
        headers=citizen_headers,
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "permission_denied"


def test_unauthenticated_assignment_is_401(
    client, citizen_headers, authority_headers, located, authority_record
):
    incident_id = _incident(client, citizen_headers, authority_headers)
    response = client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={"authority_id": str(authority_record.id)},
    )
    assert response.status_code == 401


def test_a_closed_incident_cannot_be_assigned(
    client, citizen_headers, authority_headers, located, authority_record
):
    incident_id = _incident(client, citizen_headers, authority_headers)
    client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "closed"},
        headers=authority_headers,
    )

    response = client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={"authority_id": str(authority_record.id)},
        headers=authority_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "incident_closed"


def test_assigning_to_an_unknown_authority_returns_404(
    client, citizen_headers, authority_headers, located
):
    incident_id = _incident(client, citizen_headers, authority_headers)
    response = client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={"authority_id": str(uuid.uuid4())},
        headers=authority_headers,
    )
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "authority_not_found"


def test_assignment_requires_an_authority_id(
    client, citizen_headers, authority_headers, located
):
    incident_id = _incident(client, citizen_headers, authority_headers)
    response = client.post(
        f"/api/v1/incidents/{incident_id}/assign", json={}, headers=authority_headers
    )
    assert response.status_code == 400
    assert "authority_id" in response.get_json()["error"]["details"]


def test_assigning_an_unknown_incident_returns_404(client, authority_headers, authority_record):
    response = client.post(
        f"/api/v1/incidents/{uuid.uuid4()}/assign",
        json={"authority_id": str(authority_record.id)},
        headers=authority_headers,
    )
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "incident_not_found"


# --- unassignment ----------------------------------------------------------


def test_unassigning_returns_the_incident_to_open(
    client, citizen_headers, authority_headers, located, authority_record
):
    incident_id = _incident(client, citizen_headers, authority_headers)
    client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={"authority_id": str(authority_record.id)},
        headers=authority_headers,
    )

    response = client.post(
        f"/api/v1/incidents/{incident_id}/unassign", headers=authority_headers
    )
    assert response.status_code == 200

    incident = response.get_json()["data"]["incident"]
    assert incident["authority_id"] is None
    assert incident["assigned_at"] is None
    assert incident["status"] == "open"


def test_unassigning_an_unassigned_incident_is_refused(
    client, citizen_headers, authority_headers, located
):
    incident_id = _incident(client, citizen_headers, authority_headers)
    response = client.post(
        f"/api/v1/incidents/{incident_id}/unassign", headers=authority_headers
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "incident_not_assigned"


def test_citizen_cannot_unassign(
    client, citizen_headers, authority_headers, located, authority_record
):
    incident_id = _incident(client, citizen_headers, authority_headers)
    client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={"authority_id": str(authority_record.id)},
        headers=authority_headers,
    )
    response = client.post(
        f"/api/v1/incidents/{incident_id}/unassign", headers=citizen_headers
    )
    assert response.status_code == 403


# --- suggestions -----------------------------------------------------------


def test_suggestions_rank_a_matching_type_first(client, mixed_authorities, areas):
    data = client.get(
        f"/api/v1/authorities/suggestions?category=water_leak"
        f"&district_id={areas['district'].id}"
    ).get_json()["data"]

    assert data["suggestions"][0]["type"] == "water_authority"
    assert "handles water_leak problems" in data["suggestions"][0]["match_reasons"]


def test_suggestions_explain_every_match(client, mixed_authorities, areas):
    """A ranking aid must show its reasoning so a human can disagree."""
    data = client.get(
        f"/api/v1/authorities/suggestions?category=road_damage"
        f"&district_id={areas['district'].id}"
    ).get_json()["data"]

    assert data["suggestions"]
    for suggestion in data["suggestions"]:
        assert suggestion["match_reasons"]
        assert suggestion["match_score"] > 0


def test_suggestions_include_national_bodies(client, mixed_authorities, areas):
    data = client.get(
        f"/api/v1/authorities/suggestions?category=road_damage"
        f"&district_id={areas['district'].id}"
    ).get_json()["data"]

    names = [suggestion["name"] for suggestion in data["suggestions"]]
    assert "National Roads Department" in names


def test_suggestions_assign_nothing(
    client, citizen_headers, authority_headers, located, mixed_authorities, db
):
    """Suggesting is not deciding."""
    incident_id = _incident(client, citizen_headers, authority_headers)
    client.get("/api/v1/authorities/suggestions?category=road_damage")

    incident = db.session.get(Incident, uuid.UUID(incident_id))
    assert incident.authority_id is None
    assert incident.status == IncidentStatus.OPEN


def test_suggestions_require_a_valid_category(client, db):
    assert client.get("/api/v1/authorities/suggestions").status_code == 400
    assert (
        client.get("/api/v1/authorities/suggestions?category=nonsense").status_code == 400
    )


def test_other_category_yields_no_type_match(client, mixed_authorities, areas):
    """OTHER has no plausible owner, so nothing is suggested on type alone."""
    data = client.get(
        f"/api/v1/authorities/suggestions?category=other"
        f"&district_id={areas['district'].id}"
    ).get_json()["data"]

    for suggestion in data["suggestions"]:
        assert not any("handles" in reason for reason in suggestion["match_reasons"])


# --- model and database ----------------------------------------------------


def test_authority_name_is_unique_at_the_database_level(db, areas):
    db.session.add(
        Authority(
            name="Duplicate Office",
            level=GovernmentLevel.LOCAL,
            type=AuthorityType.MUNICIPAL_OFFICE,
        )
    )
    db.session.commit()
    db.session.add(
        Authority(
            name="Duplicate Office",
            level=GovernmentLevel.FEDERAL,
            type=AuthorityType.OTHER,
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_authority_cannot_reference_a_nonexistent_district(db):
    db.session.add(
        Authority(
            name="Ghost Office",
            level=GovernmentLevel.LOCAL,
            type=AuthorityType.MUNICIPAL_OFFICE,
            district_id=uuid.uuid4(),
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_deleting_a_district_with_authorities_is_refused(db, areas, authority_record):
    db.session.delete(areas["district"])
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_deleting_an_authority_preserves_the_incident(
    client, citizen_headers, authority_headers, located, authority_record, db
):
    """SET NULL: an incident exists independently of who was assigned to it."""
    incident_id = _incident(client, citizen_headers, authority_headers)
    client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={"authority_id": str(authority_record.id)},
        headers=authority_headers,
    )

    db.session.delete(db.session.get(Authority, authority_record.id))
    db.session.commit()

    incident = db.session.get(Incident, uuid.UUID(incident_id))
    assert incident is not None
    assert incident.authority_id is None


def test_relationships_are_navigable(
    client, citizen_headers, authority_user, authority_headers, located, authority_record, db
):
    incident_id = _incident(client, citizen_headers, authority_headers)
    client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={"authority_id": str(authority_record.id)},
        headers=authority_headers,
    )

    incident = db.session.get(Incident, uuid.UUID(incident_id))
    assert incident.authority is authority_record
    assert incident in authority_record.incidents
    assert incident.assigned_by.id == authority_user.id
    assert incident in authority_user.assigned_incidents
    assert authority_record in located["district"].authorities


def test_verified_by_and_assigned_by_stay_distinct(
    client, citizen_headers, authority_user, authority_headers, admin_headers,
    located, authority_record, db,
):
    """Confirming a problem and choosing its owner are different acts."""
    incident_id = _incident(client, citizen_headers, authority_headers)
    client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={"authority_id": str(authority_record.id)},
        headers=admin_headers,
    )

    incident = db.session.get(Incident, uuid.UUID(incident_id))
    assert incident.verified_by_id == authority_user.id
    assert incident.assigned_by_id != incident.verified_by_id


def test_incident_defaults_to_unassigned(db):
    from app.models import ReportCategory

    incident = Incident(
        title="Unassigned by default",
        description="x" * 20,
        category=ReportCategory.OTHER,
        latitude=0.0,
        longitude=0.0,
    )
    db.session.add(incident)
    db.session.commit()
    assert incident.authority_id is None
    assert incident.assigned_at is None


# --- filtering incidents by authority --------------------------------------


def test_incidents_can_be_filtered_by_authority(
    client, citizen_headers, authority_headers, located, authority_record
):
    assigned = _incident(client, citizen_headers, authority_headers)
    _incident(client, citizen_headers, authority_headers, lat=0.6, lng=0.6)
    client.post(
        f"/api/v1/incidents/{assigned}/assign",
        json={"authority_id": str(authority_record.id)},
        headers=authority_headers,
    )

    data = client.get(
        f"/api/v1/incidents?authority_id={authority_record.id}"
    ).get_json()["data"]
    assert len(data["incidents"]) == 1
    assert data["incidents"][0]["id"] == assigned


def test_incident_statistics_count_unassigned(
    client, citizen_headers, authority_headers, located, authority_record
):
    assigned = _incident(client, citizen_headers, authority_headers)
    _incident(client, citizen_headers, authority_headers, lat=0.6, lng=0.6)
    client.post(
        f"/api/v1/incidents/{assigned}/assign",
        json={"authority_id": str(authority_record.id)},
        headers=authority_headers,
    )

    data = client.get("/api/v1/incidents/statistics").get_json()["data"]
    assert data["total"] == 2
    assert data["unassigned"] == 1


# --- statistics ------------------------------------------------------------


def test_authority_statistics_on_an_empty_database(client, db):
    data = client.get("/api/v1/authorities/statistics").get_json()["data"]
    assert data["total"] == 0
    assert set(data["by_level"]) == {level.value for level in GovernmentLevel}
    assert set(data["by_type"]) == {kind.value for kind in AuthorityType}


def test_authority_statistics_count_missing_contacts(client, mixed_authorities):
    """Visibility of how much of the registry has no published contact."""
    data = client.get("/api/v1/authorities/statistics").get_json()["data"]
    assert data["total"] == 3
    assert data["without_contact"] == 3
    assert data["by_level"]["federal"] == 1


def test_statistics_path_is_not_shadowed_by_the_id_route(client, db):
    assert client.get("/api/v1/authorities/statistics").status_code == 200


def test_suggestions_path_is_not_shadowed_by_the_id_route(client, db):
    assert (
        client.get("/api/v1/authorities/suggestions?category=other").status_code == 200
    )


# --- earlier phases still work ---------------------------------------------


def test_previous_endpoints_are_unaffected(client, db):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/map/districts").status_code == 200
    assert client.get("/api/v1/reports").status_code == 200
    assert client.get("/api/v1/incidents").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
