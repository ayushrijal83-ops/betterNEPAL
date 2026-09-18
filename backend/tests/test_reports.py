"""Phase 5: citizen reports.

Runs on SQLite. The GIS layer honestly reports that it cannot reverse geocode
without PostGIS, so the ``located`` fixture patches ``reverse_geocode`` to
return a resolved result for synthetic test areas. Everything else - auth,
validation, status lifecycle, filtering - is exercised for real.

All coordinates and areas below are SYNTHETIC test data, not Nepal.
"""
import time
import uuid

import pytest
import sqlalchemy as sa

from app.models import District, Municipality, Report, ReportCategory, ReportStatus
from app.services import report_service

PASSWORD = "correct-horse-battery"

VALID_REPORT = {
    "title": "Large pothole on the main road",
    "description": "A deep pothole has opened up and is dangerous for motorcycles.",
    "category": "road_damage",
    "lat": 0.5,
    "lng": 0.5,
}


@pytest.fixture
def areas(db):
    """One synthetic district with one synthetic municipality."""
    district = District(name="Testland", province="TestProvince", code="D-01")
    db.session.add(district)
    db.session.commit()
    municipality = Municipality(
        district_id=district.id, name="Testville", code="M-01"
    )
    db.session.add(municipality)
    db.session.commit()
    return {"district": district, "municipality": municipality}


@pytest.fixture
def located(monkeypatch, areas):
    """Make reverse geocoding resolve, standing in for PostGIS.

    SQLite cannot answer a point-in-polygon query, so without this every test
    would exercise only the unresolved path.
    """
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
def citizen(make_user):
    return make_user(email="citizen@betternepal.np", role_names=("citizen",))


@pytest.fixture
def citizen_headers(citizen, auth_headers):
    return auth_headers("citizen@betternepal.np", PASSWORD)


@pytest.fixture
def authority_headers(make_user, auth_headers):
    make_user(email="authority@betternepal.np", role_names=("authority",))
    return auth_headers("authority@betternepal.np", PASSWORD)


@pytest.fixture
def admin_headers(make_user, auth_headers):
    make_user(email="admin@betternepal.np", role_names=("admin",))
    return auth_headers("admin@betternepal.np", PASSWORD)


def _create(client, headers, **overrides):
    payload = {**VALID_REPORT, **overrides}
    return client.post("/api/v1/reports", json=payload, headers=headers)


# --- creation --------------------------------------------------------------


def test_authenticated_citizen_can_create_a_report(client, citizen_headers, located):
    response = _create(client, citizen_headers)
    assert response.status_code == 201

    report = response.get_json()["data"]["report"]
    assert report["title"] == VALID_REPORT["title"]
    assert report["category"] == "road_damage"
    assert report["status"] == "submitted"
    assert report["coordinates"] == {"latitude": 0.5, "longitude": 0.5}


def test_created_report_uses_the_locked_success_envelope(client, citizen_headers, located):
    body = _create(client, citizen_headers).get_json()
    assert set(body) == {"status", "data"}
    assert body["status"] == "success"


def test_report_location_is_geojson_with_longitude_first(client, citizen_headers, located):
    report = _create(client, citizen_headers).get_json()["data"]["report"]
    assert report["location"] == {"type": "Point", "coordinates": [0.5, 0.5]}

    asymmetric = _create(client, citizen_headers, lat=10.0, lng=20.0)
    location = asymmetric.get_json()["data"]["report"]["location"]
    assert location["coordinates"] == [20.0, 10.0]  # [lng, lat]


def test_reporter_is_taken_from_the_token_not_the_body(client, citizen, citizen_headers, located, db):
    """One account must not be able to file a report as another."""
    other = uuid.uuid4()
    response = _create(client, citizen_headers, reporter_id=str(other))
    assert response.status_code == 201

    report = db.session.scalar(sa.select(Report))
    assert report.reporter_id == citizen.id
    assert report.reporter_id != other


def test_district_is_resolved_by_the_server_not_trusted_from_the_client(
    client, citizen_headers, located, db
):
    """A client-supplied district_id must be ignored entirely."""
    forged = uuid.uuid4()
    response = _create(
        client, citizen_headers, district_id=str(forged), municipality_id=str(forged)
    )
    assert response.status_code == 201

    report = db.session.scalar(sa.select(Report))
    assert report.district_id == located["district"].id
    assert report.municipality_id == located["municipality"].id
    assert report.district_id != forged


def test_report_records_the_resolved_district_name(client, citizen_headers, located):
    report = _create(client, citizen_headers).get_json()["data"]["report"]
    assert report["district"] == "Testland"
    assert report["municipality"] == "Testville"


def test_trekking_guide_can_create_a_report(client, make_user, auth_headers, located):
    make_user(email="guide@betternepal.np", role_names=("trekking_guide",))
    response = _create(client, auth_headers("guide@betternepal.np", PASSWORD))
    assert response.status_code == 201


def test_report_is_stored_without_a_district_when_geocoding_cannot_resolve(
    client, citizen_headers, db
):
    """No PostGIS, no boundaries: keep the observation, admit the location.

    This is the real behaviour on SQLite - no patching here.
    """
    response = _create(client, citizen_headers)
    assert response.status_code == 201

    report = db.session.scalar(sa.select(Report))
    assert report.district_id is None
    assert report.municipality_id is None
    assert report.latitude == 0.5  # coordinates are still kept
    assert response.get_json()["data"]["report"]["district"] is None


def test_location_geometry_column_is_null_on_sqlite(client, citizen_headers, db):
    """Documented Phase 4 limitation: geometry needs PostGIS."""
    _create(client, citizen_headers)
    assert db.session.scalar(sa.select(Report)).location is None


@pytest.mark.parametrize("category", [c.value for c in ReportCategory])
def test_every_category_is_accepted(client, citizen_headers, located, category):
    response = _create(client, citizen_headers, category=category)
    assert response.status_code == 201
    assert response.get_json()["data"]["report"]["category"] == category


def test_category_is_accepted_case_insensitively(client, citizen_headers, located):
    response = _create(client, citizen_headers, category="ROAD_DAMAGE")
    assert response.status_code == 201
    assert response.get_json()["data"]["report"]["category"] == "road_damage"


# --- authentication and authorization --------------------------------------


def test_unauthenticated_creation_is_rejected(client, db, located):
    response = client.post("/api/v1/reports", json=VALID_REPORT)
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "authentication_required"


def test_creation_with_a_garbage_token_is_rejected(client, db, located):
    response = client.post(
        "/api/v1/reports",
        json=VALID_REPORT,
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401


def test_unauthenticated_creation_writes_nothing(client, db, located):
    client.post("/api/v1/reports", json=VALID_REPORT)
    assert db.session.scalar(sa.select(sa.func.count()).select_from(Report)) == 0


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize("latitude", [95, -95, 91, -91, "abc", None, "", 1e400])
def test_invalid_latitude_is_rejected(client, citizen_headers, located, latitude):
    response = _create(client, citizen_headers, lat=latitude)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "validation_error"
    assert "lat" in response.get_json()["error"]["details"]


@pytest.mark.parametrize("longitude", [181, -181, 360, "xyz", None, ""])
def test_invalid_longitude_is_rejected(client, citizen_headers, located, longitude):
    response = _create(client, citizen_headers, lng=longitude)
    assert response.status_code == 400
    assert "lng" in response.get_json()["error"]["details"]


def test_missing_coordinates_are_rejected(client, citizen_headers, located):
    payload = {k: v for k, v in VALID_REPORT.items() if k not in {"lat", "lng"}}
    response = client.post("/api/v1/reports", json=payload, headers=citizen_headers)
    assert response.status_code == 400


def test_invalid_coordinates_write_nothing(client, citizen_headers, located, db):
    _create(client, citizen_headers, lat=95)
    assert db.session.scalar(sa.select(sa.func.count()).select_from(Report)) == 0


def test_boundary_coordinates_are_accepted(client, citizen_headers, located):
    """90/-90 and 180/-180 are valid, not off-by-one rejections."""
    assert _create(client, citizen_headers, lat=90, lng=180).status_code == 201
    assert _create(client, citizen_headers, lat=-90, lng=-180).status_code == 201


def test_missing_title_is_rejected(client, citizen_headers, located):
    payload = {k: v for k, v in VALID_REPORT.items() if k != "title"}
    response = client.post("/api/v1/reports", json=payload, headers=citizen_headers)
    assert response.status_code == 400
    assert "title" in response.get_json()["error"]["details"]


def test_overlong_title_is_rejected(client, citizen_headers, located):
    response = _create(client, citizen_headers, title="x" * 101)
    assert response.status_code == 400
    assert "title" in response.get_json()["error"]["details"]


def test_missing_description_is_rejected(client, citizen_headers, located):
    payload = {k: v for k, v in VALID_REPORT.items() if k != "description"}
    response = client.post("/api/v1/reports", json=payload, headers=citizen_headers)
    assert response.status_code == 400
    assert "description" in response.get_json()["error"]["details"]


@pytest.mark.parametrize("category", ["", "not_a_category", "pothole", 42, None])
def test_invalid_category_is_rejected(client, citizen_headers, located, category):
    response = _create(client, citizen_headers, category=category)
    assert response.status_code == 400
    assert "category" in response.get_json()["error"]["details"]


def test_invalid_category_error_lists_the_accepted_values(client, citizen_headers, located):
    response = _create(client, citizen_headers, category="pothole")
    assert "road_damage" in response.get_json()["error"]["details"]["category"]


def test_all_validation_errors_are_reported_at_once(client, citizen_headers, located):
    response = client.post(
        "/api/v1/reports",
        json={"title": "x", "description": "y", "category": "bad", "lat": 95, "lng": 0},
        headers=citizen_headers,
    )
    details = response.get_json()["error"]["details"]
    assert {"title", "description", "category", "lat"} <= set(details)


def test_non_object_body_is_rejected(client, citizen_headers, located):
    response = client.post("/api/v1/reports", json=["nope"], headers=citizen_headers)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_payload"


def test_client_cannot_choose_the_initial_status(client, citizen_headers, located):
    response = _create(client, citizen_headers, status="verified_as_incident")
    assert response.status_code == 201
    assert response.get_json()["data"]["report"]["status"] == "submitted"


# --- reading ---------------------------------------------------------------


def test_listing_reports_is_public(client, citizen_headers, located):
    _create(client, citizen_headers)
    response = client.get("/api/v1/reports")
    assert response.status_code == 200
    assert len(response.get_json()["data"]["reports"]) == 1


def test_empty_list_is_not_an_error(client, db):
    response = client.get("/api/v1/reports")
    assert response.status_code == 200
    assert response.get_json()["data"]["reports"] == []
    assert response.get_json()["data"]["pagination"]["total"] == 0


def test_getting_a_single_report_is_public(client, citizen_headers, located):
    report_id = _create(client, citizen_headers).get_json()["data"]["report"]["id"]
    response = client.get(f"/api/v1/reports/{report_id}")
    assert response.status_code == 200
    assert response.get_json()["data"]["report"]["id"] == report_id


def test_unknown_report_returns_404(client, db):
    response = client.get(f"/api/v1/reports/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "report_not_found"


def test_invalid_report_id_returns_400(client, db):
    response = client.get("/api/v1/reports/not-a-uuid")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_identifier"


def test_report_response_does_not_leak_the_reporter_email(client, citizen_headers, located):
    """A public listing must not expose personal contact details."""
    _create(client, citizen_headers)
    body = client.get("/api/v1/reports").get_data(as_text=True)
    assert "citizen@betternepal.np" not in body
    assert "password_hash" not in body
    assert "argon2" not in body


def test_report_reporter_block_is_id_and_name_only(client, citizen_headers, located):
    report = _create(client, citizen_headers).get_json()["data"]["report"]
    assert set(report["reporter"]) == {"id", "full_name"}


# --- filtering -------------------------------------------------------------


def test_filter_by_category(client, citizen_headers, located):
    _create(client, citizen_headers, category="road_damage")
    _create(client, citizen_headers, category="water_leak")

    roads = client.get("/api/v1/reports?category=road_damage").get_json()["data"]
    assert len(roads["reports"]) == 1
    assert roads["reports"][0]["category"] == "road_damage"


def test_filter_by_status(client, citizen_headers, admin_headers, located):
    report_id = _create(client, citizen_headers).get_json()["data"]["report"]["id"]
    _create(client, citizen_headers)
    client.patch(
        f"/api/v1/reports/{report_id}/status",
        json={"status": "under_review"},
        headers=admin_headers,
    )

    submitted = client.get("/api/v1/reports?status=submitted").get_json()["data"]
    reviewing = client.get("/api/v1/reports?status=under_review").get_json()["data"]
    assert len(submitted["reports"]) == 1
    assert len(reviewing["reports"]) == 1


def test_filter_by_district_id(client, citizen_headers, located):
    _create(client, citizen_headers)
    district_id = str(located["district"].id)

    hit = client.get(f"/api/v1/reports?district_id={district_id}").get_json()["data"]
    miss = client.get(f"/api/v1/reports?district_id={uuid.uuid4()}").get_json()["data"]
    assert len(hit["reports"]) == 1
    assert miss["reports"] == []


def test_unknown_filter_value_is_a_400_not_an_empty_list(client, db):
    """A typo in ?status= must not look like 'no matching reports'."""
    response = client.get("/api/v1/reports?status=not_a_status")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_filter"


def test_invalid_district_filter_returns_400(client, db):
    assert client.get("/api/v1/reports?district_id=nope").status_code == 400


def test_pagination(client, citizen_headers, located):
    for index in range(5):
        _create(client, citizen_headers, title=f"Pothole number {index} on the road")

    page = client.get("/api/v1/reports?page=1&per_page=2").get_json()["data"]
    assert len(page["reports"]) == 2
    assert page["pagination"] == {"page": 1, "per_page": 2, "total": 5, "pages": 3}


def test_pagination_size_is_capped(client, db):
    page = client.get("/api/v1/reports?per_page=99999").get_json()["data"]
    assert page["pagination"]["per_page"] == report_service.MAX_PAGE_SIZE


def test_reports_are_returned_newest_first(client, citizen_headers, located, db):
    first = _create(client, citizen_headers, title="First report filed today")
    # Clock granularity: without this the two rows can share a created_at and
    # the ordering assertion becomes a coin flip.
    time.sleep(0.01)
    second = _create(client, citizen_headers, title="Second report filed today")

    listed = client.get("/api/v1/reports").get_json()["data"]["reports"]
    ids = [report["id"] for report in listed]
    assert ids.index(second.get_json()["data"]["report"]["id"]) < ids.index(
        first.get_json()["data"]["report"]["id"]
    )


# --- status updates --------------------------------------------------------


def test_authority_can_update_status(client, citizen_headers, authority_headers, located):
    report_id = _create(client, citizen_headers).get_json()["data"]["report"]["id"]
    response = client.patch(
        f"/api/v1/reports/{report_id}/status",
        json={"status": "under_review"},
        headers=authority_headers,
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["report"]["status"] == "under_review"


def test_admin_can_update_status(client, citizen_headers, admin_headers, located):
    report_id = _create(client, citizen_headers).get_json()["data"]["report"]["id"]
    response = client.patch(
        f"/api/v1/reports/{report_id}/status",
        json={"status": "verified_as_incident"},
        headers=admin_headers,
    )
    assert response.status_code == 200


def test_citizen_cannot_update_status(client, citizen_headers, located):
    report_id = _create(client, citizen_headers).get_json()["data"]["report"]["id"]
    response = client.patch(
        f"/api/v1/reports/{report_id}/status",
        json={"status": "verified_as_incident"},
        headers=citizen_headers,
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "permission_denied"


def test_a_citizen_cannot_verify_their_own_report(client, citizen_headers, located, db):
    """Self-verification would make verification meaningless."""
    report_id = _create(client, citizen_headers).get_json()["data"]["report"]["id"]
    client.patch(
        f"/api/v1/reports/{report_id}/status",
        json={"status": "verified_as_incident"},
        headers=citizen_headers,
    )
    assert db.session.scalar(sa.select(Report)).status == ReportStatus.SUBMITTED


def test_trekking_guide_cannot_update_status(client, citizen_headers, make_user, auth_headers, located):
    make_user(email="guide@betternepal.np", role_names=("trekking_guide",))
    report_id = _create(client, citizen_headers).get_json()["data"]["report"]["id"]
    response = client.patch(
        f"/api/v1/reports/{report_id}/status",
        json={"status": "rejected"},
        headers=auth_headers("guide@betternepal.np", PASSWORD),
    )
    assert response.status_code == 403


def test_unauthenticated_status_update_is_401_not_403(client, citizen_headers, located):
    report_id = _create(client, citizen_headers).get_json()["data"]["report"]["id"]
    response = client.patch(
        f"/api/v1/reports/{report_id}/status", json={"status": "rejected"}
    )
    assert response.status_code == 401


@pytest.mark.parametrize("status", ["", "not_a_status", "deleted", None])
def test_invalid_status_value_is_rejected(client, citizen_headers, admin_headers, located, status):
    report_id = _create(client, citizen_headers).get_json()["data"]["report"]["id"]
    response = client.patch(
        f"/api/v1/reports/{report_id}/status",
        json={"status": status},
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert "status" in response.get_json()["error"]["details"]


def test_status_update_on_unknown_report_returns_404(client, admin_headers, db):
    response = client.patch(
        f"/api/v1/reports/{uuid.uuid4()}/status",
        json={"status": "rejected"},
        headers=admin_headers,
    )
    assert response.status_code == 404


def test_terminal_status_cannot_be_reopened(client, citizen_headers, admin_headers, located):
    """Rejected and verified are end states; reopening would orphan Phase 6 work."""
    report_id = _create(client, citizen_headers).get_json()["data"]["report"]["id"]
    client.patch(
        f"/api/v1/reports/{report_id}/status",
        json={"status": "rejected"},
        headers=admin_headers,
    )

    response = client.patch(
        f"/api/v1/reports/{report_id}/status",
        json={"status": "under_review"},
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "invalid_status_transition"
    assert response.get_json()["error"]["details"]["current_status"] == "rejected"


def test_setting_the_same_status_is_a_no_op(client, citizen_headers, admin_headers, located):
    report_id = _create(client, citizen_headers).get_json()["data"]["report"]["id"]
    response = client.patch(
        f"/api/v1/reports/{report_id}/status",
        json={"status": "submitted"},
        headers=admin_headers,
    )
    assert response.status_code == 200


def test_full_lifecycle_submitted_to_verified(client, citizen_headers, authority_headers, located):
    report_id = _create(client, citizen_headers).get_json()["data"]["report"]["id"]
    for status in ("under_review", "verified_as_incident"):
        response = client.patch(
            f"/api/v1/reports/{report_id}/status",
            json={"status": status},
            headers=authority_headers,
        )
        assert response.status_code == 200, status
    assert response.get_json()["data"]["report"]["status"] == "verified_as_incident"


# --- model and database ----------------------------------------------------


def test_report_requires_a_reporter(db, areas):
    db.session.add(
        Report(
            title="No reporter",
            description="x" * 20,
            category=ReportCategory.OTHER,
            latitude=0.0,
            longitude=0.0,
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_report_cannot_reference_a_nonexistent_reporter(db, areas):
    db.session.add(
        Report(
            reporter_id=uuid.uuid4(),
            title="Ghost reporter",
            description="x" * 20,
            category=ReportCategory.OTHER,
            latitude=0.0,
            longitude=0.0,
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_database_rejects_an_out_of_range_latitude(db, citizen):
    """Defence in depth: the CHECK constraint, not just the API validator."""
    db.session.add(
        Report(
            reporter_id=citizen.id,
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


def test_database_rejects_an_unknown_category(db, citizen):
    """`validate_strings=True` refuses a value outside the enum on write."""
    db.session.add(
        Report(
            reporter_id=citizen.id,
            title="Bad category",
            description="x" * 20,
            category="not_a_category",
            latitude=0.0,
            longitude=0.0,
        )
    )
    with pytest.raises((LookupError, sa.exc.StatementError, sa.exc.IntegrityError)):
        db.session.commit()
    db.session.rollback()


def test_deleting_a_user_with_reports_is_refused(db, citizen, client, citizen_headers, located):
    """Civic records must not vanish because an account was removed."""
    _create(client, citizen_headers)
    db.session.delete(citizen)
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_deleting_a_district_with_reports_is_refused(db, client, citizen_headers, located):
    _create(client, citizen_headers)
    db.session.delete(located["district"])
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_relationships_are_navigable(db, client, citizen, citizen_headers, located):
    _create(client, citizen_headers)
    report = db.session.scalar(sa.select(Report))

    assert report.reporter.id == citizen.id
    assert report.district.name == "Testland"
    assert report.municipality.name == "Testville"
    assert report in citizen.reports
    assert report in located["district"].reports
    assert report in located["municipality"].reports


def test_default_status_is_submitted(db, citizen):
    report = Report(
        reporter_id=citizen.id,
        title="Defaults check",
        description="x" * 20,
        category=ReportCategory.OTHER,
        latitude=0.0,
        longitude=0.0,
    )
    db.session.add(report)
    db.session.commit()
    assert report.status == ReportStatus.SUBMITTED


# --- statistics ------------------------------------------------------------


def test_statistics_on_an_empty_database(client, db):
    data = client.get("/api/v1/reports/statistics").get_json()["data"]
    assert data["total"] == 0
    assert data["by_status"]["submitted"] == 0
    assert set(data["by_category"]) == {c.value for c in ReportCategory}


def test_statistics_count_reports(client, citizen_headers, located):
    _create(client, citizen_headers, category="road_damage")
    _create(client, citizen_headers, category="water_leak")

    data = client.get("/api/v1/reports/statistics").get_json()["data"]
    assert data["total"] == 2
    assert data["by_status"]["submitted"] == 2
    assert data["by_category"]["road_damage"] == 1
    assert data["without_district"] == 0


def test_statistics_track_unlocated_reports(client, citizen_headers, db):
    """No patching: on SQLite nothing resolves, and that must be visible."""
    _create(client, citizen_headers)
    data = client.get("/api/v1/reports/statistics").get_json()["data"]
    assert data["without_district"] == 1


def test_statistics_path_is_not_shadowed_by_the_id_route(client, db):
    """`/reports/statistics` must not be parsed as `/reports/<id>`."""
    assert client.get("/api/v1/reports/statistics").status_code == 200


# --- earlier phases still work ---------------------------------------------


def test_previous_endpoints_are_unaffected(client, db):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/map/districts").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
