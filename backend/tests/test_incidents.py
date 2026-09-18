"""Phase 6: incidents, verification and clustering.

Runs on SQLite. Proximity search therefore takes the documented non-PostGIS
path (bounding box + haversine) and reports ``method: "approximate"``; the
PostGIS path is covered in ``tests/test_postgis.py``-style integration and is
not faked here.

All coordinates and areas below are SYNTHETIC test data, not Nepal.
"""
import uuid

import pytest
import sqlalchemy as sa

from app.models import (
    District,
    Incident,
    IncidentSeverity,
    IncidentStatus,
    Municipality,
    Report,
    ReportCategory,
    ReportStatus,
)
from app.services import incident_service

PASSWORD = "correct-horse-battery"

VALID_REPORT = {
    "title": "Burst water main flooding the street",
    "description": "Water has been pouring out of the road since this morning.",
    "category": "water_leak",
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


@pytest.fixture
def citizen_headers(make_user, auth_headers):
    make_user(email="citizen@betternepal.np", role_names=("citizen",))
    return auth_headers("citizen@betternepal.np", PASSWORD)


@pytest.fixture
def authority(make_user):
    return make_user(email="authority@betternepal.np", role_names=("authority",))


@pytest.fixture
def authority_headers(authority, auth_headers):
    return auth_headers("authority@betternepal.np", PASSWORD)


@pytest.fixture
def admin_headers(make_user, auth_headers):
    make_user(email="admin@betternepal.np", role_names=("admin",))
    return auth_headers("admin@betternepal.np", PASSWORD)


def _make_report(client, headers, **overrides):
    """Create a report and return its id."""
    payload = {**VALID_REPORT, **overrides}
    response = client.post("/api/v1/reports", json=payload, headers=headers)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["report"]["id"]


def _promote(client, headers, report_id, **body):
    return client.post(
        "/api/v1/incidents/from-report",
        json={"report_id": report_id, **body},
        headers=headers,
    )


# --- promotion -------------------------------------------------------------


def test_authority_can_promote_a_report(client, citizen_headers, authority_headers, located):
    report_id = _make_report(client, citizen_headers)
    response = _promote(client, authority_headers, report_id, severity="high")
    assert response.status_code == 201

    incident = response.get_json()["data"]["incident"]
    assert incident["severity"] == "high"
    assert incident["status"] == "open"
    assert incident["category"] == "water_leak"
    assert incident["report_count"] == 1


def test_admin_can_promote_a_report(client, citizen_headers, admin_headers, located):
    report_id = _make_report(client, citizen_headers)
    assert _promote(client, admin_headers, report_id).status_code == 201


def test_promotion_uses_the_locked_success_envelope(client, citizen_headers, authority_headers, located):
    report_id = _make_report(client, citizen_headers)
    body = _promote(client, authority_headers, report_id).get_json()
    assert set(body) == {"status", "data"}
    assert body["status"] == "success"


def test_promoted_report_becomes_verified_as_incident(
    client, citizen_headers, authority_headers, located, db
):
    report_id = _make_report(client, citizen_headers)
    _promote(client, authority_headers, report_id)

    report = db.session.get(Report, uuid.UUID(report_id))
    assert report.status == ReportStatus.VERIFIED_AS_INCIDENT
    assert report.incident_id is not None


def test_incident_inherits_geography_from_the_report(
    client, citizen_headers, authority_headers, located
):
    report_id = _make_report(client, citizen_headers)
    incident = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]

    assert incident["coordinates"] == {"latitude": 0.5, "longitude": 0.5}
    assert incident["district"] == "Testland"
    assert incident["municipality"] == "Testville"
    assert incident["location"] == {"type": "Point", "coordinates": [0.5, 0.5]}


def test_incident_records_who_verified_it(
    client, citizen_headers, authority, authority_headers, located
):
    report_id = _make_report(client, citizen_headers)
    incident = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]
    assert incident["verified_by"]["id"] == str(authority.id)
    assert incident["verified_by"]["full_name"] == authority.full_name


def test_verifier_cannot_be_forged_from_the_body(
    client, citizen_headers, authority, authority_headers, located, db
):
    report_id = _make_report(client, citizen_headers)
    _promote(client, authority_headers, report_id, verified_by_id=str(uuid.uuid4()))

    incident = db.session.scalar(sa.select(Incident))
    assert incident.verified_by_id == authority.id


def test_incident_geography_cannot_be_forged_from_the_body(
    client, citizen_headers, authority_headers, located, db
):
    """Coordinates come from the report, so evidence and finding cannot diverge."""
    report_id = _make_report(client, citizen_headers)
    _promote(
        client,
        authority_headers,
        report_id,
        lat=80.0,
        lng=170.0,
        district_id=str(uuid.uuid4()),
    )

    incident = db.session.scalar(sa.select(Incident))
    assert incident.latitude == 0.5
    assert incident.longitude == 0.5


def test_title_and_description_may_be_overridden(client, citizen_headers, authority_headers, located):
    report_id = _make_report(client, citizen_headers)
    incident = _promote(
        client,
        authority_headers,
        report_id,
        title="Verified: mains rupture on the high street",
        description="Confirmed on site; the pipe has failed at a joint.",
    ).get_json()["data"]["incident"]

    assert incident["title"] == "Verified: mains rupture on the high street"
    assert "Confirmed on site" in incident["description"]


def test_default_severity_is_medium(client, citizen_headers, authority_headers, located):
    report_id = _make_report(client, citizen_headers)
    incident = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]
    assert incident["severity"] == "medium"


@pytest.mark.parametrize("severity", [s.value for s in IncidentSeverity])
def test_every_severity_is_accepted(client, citizen_headers, authority_headers, located, severity):
    report_id = _make_report(client, citizen_headers)
    response = _promote(client, authority_headers, report_id, severity=severity)
    assert response.status_code == 201
    assert response.get_json()["data"]["incident"]["severity"] == severity


# --- authorization ---------------------------------------------------------


def test_citizen_cannot_promote_a_report(client, citizen_headers, located):
    report_id = _make_report(client, citizen_headers)
    response = _promote(client, citizen_headers, report_id)
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "permission_denied"


def test_citizen_cannot_verify_their_own_report(client, citizen_headers, located, db):
    """Self-verification would make verification meaningless."""
    report_id = _make_report(client, citizen_headers)
    _promote(client, citizen_headers, report_id)

    assert db.session.scalar(sa.select(sa.func.count()).select_from(Incident)) == 0
    assert db.session.get(Report, uuid.UUID(report_id)).status == ReportStatus.SUBMITTED


def test_trekking_guide_cannot_promote(client, citizen_headers, make_user, auth_headers, located):
    make_user(email="guide@betternepal.np", role_names=("trekking_guide",))
    report_id = _make_report(client, citizen_headers)
    response = _promote(client, auth_headers("guide@betternepal.np", PASSWORD), report_id)
    assert response.status_code == 403


def test_unauthenticated_promotion_is_401(client, citizen_headers, located):
    report_id = _make_report(client, citizen_headers)
    response = client.post("/api/v1/incidents/from-report", json={"report_id": report_id})
    assert response.status_code == 401


def test_citizen_cannot_link_a_report(client, citizen_headers, authority_headers, located):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, first).get_json()["data"]["incident"]["id"]

    response = client.post(
        f"/api/v1/incidents/{incident_id}/link-report",
        json={"report_id": second},
        headers=citizen_headers,
    )
    assert response.status_code == 403


def test_citizen_cannot_patch_an_incident(client, citizen_headers, authority_headers, located):
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]["id"]

    response = client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "resolved"},
        headers=citizen_headers,
    )
    assert response.status_code == 403


# --- clustering ------------------------------------------------------------


def test_multiple_reports_can_be_linked_to_one_incident(
    client, citizen_headers, authority_headers, located, db
):
    """Five citizens, one burst main: five reports, one incident."""
    report_ids = [_make_report(client, citizen_headers) for _ in range(5)]
    incident_id = _promote(client, authority_headers, report_ids[0]).get_json()["data"][
        "incident"
    ]["id"]

    for report_id in report_ids[1:]:
        response = client.post(
            f"/api/v1/incidents/{incident_id}/link-report",
            json={"report_id": report_id},
            headers=authority_headers,
        )
        assert response.status_code == 200

    incident = client.get(f"/api/v1/incidents/{incident_id}").get_json()["data"]["incident"]
    assert incident["report_count"] == 5
    assert len(incident["reports"]) == 5
    assert db.session.scalar(sa.select(sa.func.count()).select_from(Incident)) == 1


def test_linking_moves_every_report_to_verified(
    client, citizen_headers, authority_headers, located, db
):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, first).get_json()["data"]["incident"]["id"]

    client.post(
        f"/api/v1/incidents/{incident_id}/link-report",
        json={"report_id": second},
        headers=authority_headers,
    )

    statuses = db.session.scalars(sa.select(Report.status)).all()
    assert all(status == ReportStatus.VERIFIED_AS_INCIDENT for status in statuses)


def test_a_report_cannot_be_linked_twice(client, citizen_headers, authority_headers, located):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, first).get_json()["data"]["incident"]["id"]

    client.post(
        f"/api/v1/incidents/{incident_id}/link-report",
        json={"report_id": second},
        headers=authority_headers,
    )
    response = client.post(
        f"/api/v1/incidents/{incident_id}/link-report",
        json={"report_id": second},
        headers=authority_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "report_already_linked"


def test_an_already_promoted_report_cannot_be_promoted_again(
    client, citizen_headers, authority_headers, located
):
    report_id = _make_report(client, citizen_headers)
    _promote(client, authority_headers, report_id)

    response = _promote(client, authority_headers, report_id)
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "report_already_linked"


def test_a_rejected_report_cannot_be_promoted(
    client, citizen_headers, authority_headers, located
):
    report_id = _make_report(client, citizen_headers)
    client.patch(
        f"/api/v1/reports/{report_id}/status",
        json={"status": "rejected"},
        headers=authority_headers,
    )

    response = _promote(client, authority_headers, report_id)
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "report_rejected"


def test_reports_cannot_be_added_to_a_closed_incident(
    client, citizen_headers, authority_headers, located
):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, first).get_json()["data"]["incident"]["id"]

    client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "closed"},
        headers=authority_headers,
    )
    response = client.post(
        f"/api/v1/incidents/{incident_id}/link-report",
        json={"report_id": second},
        headers=authority_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "incident_closed"


def test_unlinking_returns_a_report_to_review(
    client, citizen_headers, authority_headers, located, db
):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, first).get_json()["data"]["incident"]["id"]
    client.post(
        f"/api/v1/incidents/{incident_id}/link-report",
        json={"report_id": second},
        headers=authority_headers,
    )

    response = client.post(
        f"/api/v1/incidents/{incident_id}/unlink-report",
        json={"report_id": second},
        headers=authority_headers,
    )
    assert response.status_code == 200

    report = db.session.get(Report, uuid.UUID(second))
    assert report.incident_id is None
    assert report.status == ReportStatus.UNDER_REVIEW


def test_unlinking_a_report_from_the_wrong_incident_is_refused(
    client, citizen_headers, authority_headers, located
):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers)
    incident_a = _promote(client, authority_headers, first).get_json()["data"]["incident"]["id"]
    incident_b = _promote(client, authority_headers, second).get_json()["data"]["incident"]["id"]

    response = client.post(
        f"/api/v1/incidents/{incident_a}/unlink-report",
        json={"report_id": second},
        headers=authority_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "report_not_in_incident"
    assert incident_b  # both incidents exist; neither was disturbed


# --- proximity -------------------------------------------------------------


def test_nearby_reports_reports_its_method(client, citizen_headers, authority_headers, located):
    """On SQLite the answer is approximate, and must say so."""
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]["id"]

    data = client.get(f"/api/v1/incidents/{incident_id}/nearby-reports").get_json()["data"]
    assert data["method"] == "approximate"
    assert data["radius_meters"] == 50


def test_nearby_reports_finds_a_close_unlinked_report(
    client, citizen_headers, authority_headers, located
):
    """~11m away at this latitude: inside the 50m default."""
    anchor = _make_report(client, citizen_headers)
    _make_report(client, citizen_headers, lat=0.5001, lng=0.5)
    incident_id = _promote(client, authority_headers, anchor).get_json()["data"]["incident"]["id"]

    data = client.get(f"/api/v1/incidents/{incident_id}/nearby-reports").get_json()["data"]
    assert data["count"] == 1
    assert data["reports"][0]["distance_meters"] < 50


def test_nearby_reports_excludes_distant_ones(client, citizen_headers, authority_headers, located):
    """~1.1km away: well outside the default radius."""
    anchor = _make_report(client, citizen_headers)
    _make_report(client, citizen_headers, lat=0.51, lng=0.5)
    incident_id = _promote(client, authority_headers, anchor).get_json()["data"]["incident"]["id"]

    data = client.get(f"/api/v1/incidents/{incident_id}/nearby-reports").get_json()["data"]
    assert data["count"] == 0


def test_a_wider_radius_picks_up_the_distant_report(
    client, citizen_headers, authority_headers, located
):
    anchor = _make_report(client, citizen_headers)
    _make_report(client, citizen_headers, lat=0.51, lng=0.5)
    incident_id = _promote(client, authority_headers, anchor).get_json()["data"]["incident"]["id"]

    data = client.get(
        f"/api/v1/incidents/{incident_id}/nearby-reports?radius=2000"
    ).get_json()["data"]
    assert data["count"] == 1


def test_nearby_reports_excludes_already_linked_reports(
    client, citizen_headers, authority_headers, located
):
    anchor = _make_report(client, citizen_headers)
    neighbour = _make_report(client, citizen_headers, lat=0.5001, lng=0.5)
    incident_id = _promote(client, authority_headers, anchor).get_json()["data"]["incident"]["id"]

    before = client.get(f"/api/v1/incidents/{incident_id}/nearby-reports").get_json()["data"]
    assert before["count"] == 1

    client.post(
        f"/api/v1/incidents/{incident_id}/link-report",
        json={"report_id": neighbour},
        headers=authority_headers,
    )
    after = client.get(f"/api/v1/incidents/{incident_id}/nearby-reports").get_json()["data"]
    assert after["count"] == 0


def test_nearby_reports_excludes_rejected_reports(
    client, citizen_headers, authority_headers, located
):
    anchor = _make_report(client, citizen_headers)
    rejected = _make_report(client, citizen_headers, lat=0.5001, lng=0.5)
    client.patch(
        f"/api/v1/reports/{rejected}/status",
        json={"status": "rejected"},
        headers=authority_headers,
    )
    incident_id = _promote(client, authority_headers, anchor).get_json()["data"]["incident"]["id"]

    data = client.get(f"/api/v1/incidents/{incident_id}/nearby-reports").get_json()["data"]
    assert data["count"] == 0


def test_nearby_results_are_sorted_by_distance(client, citizen_headers, authority_headers, located):
    anchor = _make_report(client, citizen_headers)
    _make_report(client, citizen_headers, lat=0.5003, lng=0.5)  # ~33m
    _make_report(client, citizen_headers, lat=0.5001, lng=0.5)  # ~11m
    incident_id = _promote(client, authority_headers, anchor).get_json()["data"]["incident"]["id"]

    reports = client.get(
        f"/api/v1/incidents/{incident_id}/nearby-reports"
    ).get_json()["data"]["reports"]
    distances = [report["distance_meters"] for report in reports]
    assert distances == sorted(distances)


@pytest.mark.parametrize("radius", ["abc", "-5", "0"])
def test_invalid_radius_is_rejected(client, citizen_headers, authority_headers, located, radius):
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]["id"]

    response = client.get(f"/api/v1/incidents/{incident_id}/nearby-reports?radius={radius}")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_radius"


def test_haversine_matches_a_known_distance():
    """One degree of latitude is ~111km; sanity-check the fallback maths."""
    metres = incident_service._haversine_metres(0.0, 0.0, 1.0, 0.0)
    assert 110_000 < metres < 112_000


def test_haversine_is_zero_for_the_same_point():
    assert incident_service._haversine_metres(27.7, 85.3, 27.7, 85.3) == pytest.approx(0)


def test_bounding_box_is_wider_in_longitude_near_the_poles():
    """Longitude degrees shrink towards the poles, so the box must widen."""
    from app.gis.location import Coordinates

    _, _, min_lng_eq, max_lng_eq = incident_service._bounding_box(
        Coordinates(latitude=0.0, longitude=0.0), 1000
    )
    _, _, min_lng_hi, max_lng_hi = incident_service._bounding_box(
        Coordinates(latitude=80.0, longitude=0.0), 1000
    )
    assert (max_lng_hi - min_lng_hi) > (max_lng_eq - min_lng_eq)


def test_bounding_box_does_not_explode_at_the_pole():
    from app.gis.location import Coordinates

    _, _, min_lng, max_lng = incident_service._bounding_box(
        Coordinates(latitude=90.0, longitude=0.0), 1000
    )
    assert min_lng >= -180.0 - 1e-9
    assert max_lng <= 180.0 + 1e-9


# --- listing and reading ---------------------------------------------------


def test_listing_incidents_is_public(client, citizen_headers, authority_headers, located):
    report_id = _make_report(client, citizen_headers)
    _promote(client, authority_headers, report_id)

    response = client.get("/api/v1/incidents")
    assert response.status_code == 200
    assert len(response.get_json()["data"]["incidents"]) == 1


def test_empty_incident_list_is_not_an_error(client, db):
    data = client.get("/api/v1/incidents").get_json()["data"]
    assert data["incidents"] == []
    assert data["pagination"]["total"] == 0


def test_getting_an_incident_is_public(client, citizen_headers, authority_headers, located):
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]["id"]

    response = client.get(f"/api/v1/incidents/{incident_id}")
    assert response.status_code == 200
    assert response.get_json()["data"]["incident"]["id"] == incident_id


def test_incident_detail_includes_linked_report_summaries(
    client, citizen_headers, authority_headers, located
):
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]["id"]

    reports = client.get(f"/api/v1/incidents/{incident_id}").get_json()["data"]["incident"][
        "reports"
    ]
    assert len(reports) == 1
    assert set(reports[0]) == {"id", "title", "status", "category", "coordinates", "created_at"}


def test_incident_response_does_not_leak_reporter_emails(
    client, citizen_headers, authority_headers, located
):
    report_id = _make_report(client, citizen_headers)
    _promote(client, authority_headers, report_id)

    body = client.get("/api/v1/incidents").get_data(as_text=True)
    assert "citizen@betternepal.np" not in body
    assert "password_hash" not in body


def test_unknown_incident_returns_404(client, db):
    response = client.get(f"/api/v1/incidents/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "incident_not_found"


def test_invalid_incident_id_returns_400(client, db):
    response = client.get("/api/v1/incidents/not-a-uuid")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_identifier"


def test_promotion_of_an_unknown_report_returns_404(client, authority_headers, db):
    response = _promote(client, authority_headers, str(uuid.uuid4()))
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "report_not_found"


def test_promotion_requires_a_report_id(client, authority_headers, db):
    response = client.post("/api/v1/incidents/from-report", json={}, headers=authority_headers)
    assert response.status_code == 400
    assert "report_id" in response.get_json()["error"]["details"]


@pytest.mark.parametrize("severity", ["extreme", "urgent", 5])
def test_invalid_severity_is_rejected(client, citizen_headers, authority_headers, located, severity):
    report_id = _make_report(client, citizen_headers)
    response = _promote(client, authority_headers, report_id, severity=severity)
    assert response.status_code == 400
    assert "severity" in response.get_json()["error"]["details"]


def test_a_blank_optional_field_means_omitted(client, citizen_headers, authority_headers, located):
    """Project-wide convention since Phase 3: blank optional field == absent.

    So an empty severity falls back to the default rather than erroring.
    """
    report_id = _make_report(client, citizen_headers)
    response = _promote(client, authority_headers, report_id, severity="")
    assert response.status_code == 201
    assert response.get_json()["data"]["incident"]["severity"] == "medium"


# --- filtering -------------------------------------------------------------


def _two_incidents(client, citizen_headers, authority_headers):
    first = _make_report(client, citizen_headers, category="water_leak")
    second = _make_report(client, citizen_headers, category="road_damage")
    a = _promote(client, authority_headers, first, severity="critical").get_json()["data"][
        "incident"
    ]["id"]
    b = _promote(client, authority_headers, second, severity="low").get_json()["data"][
        "incident"
    ]["id"]
    return a, b


def test_filter_by_severity(client, citizen_headers, authority_headers, located):
    _two_incidents(client, citizen_headers, authority_headers)
    data = client.get("/api/v1/incidents?severity=critical").get_json()["data"]
    assert len(data["incidents"]) == 1
    assert data["incidents"][0]["severity"] == "critical"


def test_filter_by_category(client, citizen_headers, authority_headers, located):
    _two_incidents(client, citizen_headers, authority_headers)
    data = client.get("/api/v1/incidents?category=road_damage").get_json()["data"]
    assert len(data["incidents"]) == 1


def test_filter_by_status(client, citizen_headers, authority_headers, located):
    incident_a, _ = _two_incidents(client, citizen_headers, authority_headers)
    client.patch(
        f"/api/v1/incidents/{incident_a}",
        json={"status": "in_progress"},
        headers=authority_headers,
    )

    assert len(client.get("/api/v1/incidents?status=open").get_json()["data"]["incidents"]) == 1
    assert (
        len(client.get("/api/v1/incidents?status=in_progress").get_json()["data"]["incidents"])
        == 1
    )


def test_filter_by_district(client, citizen_headers, authority_headers, located):
    _two_incidents(client, citizen_headers, authority_headers)
    district_id = str(located["district"].id)

    hit = client.get(f"/api/v1/incidents?district_id={district_id}").get_json()["data"]
    miss = client.get(f"/api/v1/incidents?district_id={uuid.uuid4()}").get_json()["data"]
    assert len(hit["incidents"]) == 2
    assert miss["incidents"] == []


def test_unknown_filter_value_is_a_400(client, db):
    response = client.get("/api/v1/incidents?severity=apocalyptic")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_filter"


def test_incident_pagination(client, citizen_headers, authority_headers, located):
    for _ in range(3):
        report_id = _make_report(client, citizen_headers)
        _promote(client, authority_headers, report_id)

    data = client.get("/api/v1/incidents?page=1&per_page=2").get_json()["data"]
    assert len(data["incidents"]) == 2
    assert data["pagination"] == {"page": 1, "per_page": 2, "total": 3, "pages": 2}


# --- lifecycle -------------------------------------------------------------


def test_authority_can_update_status(client, citizen_headers, authority_headers, located):
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]["id"]

    response = client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "in_progress"},
        headers=authority_headers,
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["incident"]["status"] == "in_progress"


def test_severity_can_be_changed_freely(client, citizen_headers, admin_headers, located):
    """New information legitimately makes a problem look worse."""
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, admin_headers, report_id, severity="low").get_json()["data"][
        "incident"
    ]["id"]

    response = client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"severity": "critical"},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["incident"]["severity"] == "critical"


def test_status_and_severity_can_be_updated_together(
    client, citizen_headers, authority_headers, located
):
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]["id"]

    incident = client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "resolved", "severity": "high"},
        headers=authority_headers,
    ).get_json()["data"]["incident"]
    assert incident["status"] == "resolved"
    assert incident["severity"] == "high"


def test_an_empty_update_is_rejected(client, citizen_headers, authority_headers, located):
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]["id"]

    response = client.patch(f"/api/v1/incidents/{incident_id}", json={}, headers=authority_headers)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "nothing_to_update"


def test_a_closed_incident_cannot_be_reopened(client, citizen_headers, authority_headers, located):
    """A problem that recurs is a new incident, not a resurrection."""
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]["id"]
    client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "closed"},
        headers=authority_headers,
    )

    response = client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "open"},
        headers=authority_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "invalid_status_transition"
    assert response.get_json()["error"]["details"]["current_status"] == "closed"


def test_a_resolved_incident_can_be_put_back_in_progress(
    client, citizen_headers, authority_headers, located
):
    """Work declared finished sometimes turns out not to be."""
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]["id"]
    client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "resolved"},
        headers=authority_headers,
    )

    response = client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "in_progress"},
        headers=authority_headers,
    )
    assert response.status_code == 200


def test_full_lifecycle(client, citizen_headers, authority_headers, located):
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]["id"]

    for status in ("in_progress", "resolved", "closed"):
        response = client.patch(
            f"/api/v1/incidents/{incident_id}",
            json={"status": status},
            headers=authority_headers,
        )
        assert response.status_code == 200, status
    assert response.get_json()["data"]["incident"]["status"] == "closed"


@pytest.mark.parametrize("status", ["archived", "deleted", 3])
def test_invalid_status_value_is_rejected(client, citizen_headers, authority_headers, located, status):
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]["id"]

    response = client.patch(
        f"/api/v1/incidents/{incident_id}", json={"status": status}, headers=authority_headers
    )
    assert response.status_code == 400
    assert "status" in response.get_json()["error"]["details"]


def test_a_blank_status_is_treated_as_no_update(client, citizen_headers, authority_headers, located):
    """Blank == absent, so the body updates nothing and is refused as such."""
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]["id"]

    response = client.patch(
        f"/api/v1/incidents/{incident_id}", json={"status": ""}, headers=authority_headers
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "nothing_to_update"


# --- model and database ----------------------------------------------------


def test_database_rejects_an_out_of_range_incident_latitude(db, areas):
    db.session.add(
        Incident(
            title="Bad coordinates",
            description="x" * 20,
            category=ReportCategory.OTHER,
            latitude=95.0,
            longitude=0.0,
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_incident_defaults(db):
    incident = Incident(
        title="Defaults check",
        description="x" * 20,
        category=ReportCategory.OTHER,
        latitude=0.0,
        longitude=0.0,
    )
    db.session.add(incident)
    db.session.commit()
    assert incident.status == IncidentStatus.OPEN
    assert incident.severity == IncidentSeverity.MEDIUM
    assert incident.report_count == 0


def test_deleting_an_incident_preserves_its_reports(
    client, citizen_headers, authority_headers, located, db
):
    """SET NULL: citizen submissions survive the incident they evidenced."""
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id).get_json()["data"]["incident"]["id"]

    db.session.delete(db.session.get(Incident, uuid.UUID(incident_id)))
    db.session.commit()

    report = db.session.get(Report, uuid.UUID(report_id))
    assert report is not None
    assert report.incident_id is None


def test_relationships_are_navigable(client, citizen_headers, authority, authority_headers, located, db):
    report_id = _make_report(client, citizen_headers)
    _promote(client, authority_headers, report_id)

    incident = db.session.scalar(sa.select(Incident))
    report = db.session.get(Report, uuid.UUID(report_id))

    assert report.incident is incident
    assert report in incident.reports
    assert incident.district.name == "Testland"
    assert incident.verified_by.id == authority.id
    assert incident in authority.verified_incidents
    assert incident in located["district"].incidents


def test_location_column_is_null_on_sqlite(client, citizen_headers, authority_headers, located, db):
    report_id = _make_report(client, citizen_headers)
    _promote(client, authority_headers, report_id)
    assert db.session.scalar(sa.select(Incident)).location is None


# --- statistics ------------------------------------------------------------


def test_statistics_on_an_empty_database(client, db):
    data = client.get("/api/v1/incidents/statistics").get_json()["data"]
    assert data["total"] == 0
    assert set(data["by_severity"]) == {s.value for s in IncidentSeverity}
    assert set(data["by_status"]) == {s.value for s in IncidentStatus}
    assert data["linked_reports"] == 0


def test_statistics_count_incidents_and_linked_reports(
    client, citizen_headers, authority_headers, located
):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, first, severity="high").get_json()["data"][
        "incident"
    ]["id"]
    client.post(
        f"/api/v1/incidents/{incident_id}/link-report",
        json={"report_id": second},
        headers=authority_headers,
    )

    data = client.get("/api/v1/incidents/statistics").get_json()["data"]
    assert data["total"] == 1
    assert data["by_severity"]["high"] == 1
    assert data["by_status"]["open"] == 1
    assert data["linked_reports"] == 2


def test_statistics_path_is_not_shadowed_by_the_id_route(client, db):
    assert client.get("/api/v1/incidents/statistics").status_code == 200


# --- earlier phases still work ---------------------------------------------


def test_previous_endpoints_are_unaffected(client, db):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/map/districts").status_code == 200
    assert client.get("/api/v1/reports").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
