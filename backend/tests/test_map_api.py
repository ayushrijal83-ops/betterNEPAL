"""Phase 4: /api/v1/map endpoints.

Runs on SQLite. Geometry is always absent here, so these tests assert the
*shape* of the responses and the honesty of the "no data" paths - never that a
spatial query returned a location.
"""
import uuid

import pytest

from app.models import District, Municipality
from app.services.geolocation_service import (
    REASON_NO_SPATIAL_BACKEND,
    REASON_NO_BOUNDARY_DATA,
)


@pytest.fixture
def geo(db):
    """Two synthetic administrative records. Not Nepal data."""
    district = District(name="Testland", province="TestProvince", code="D-01")
    db.session.add(district)
    db.session.commit()
    municipality = Municipality(
        district_id=district.id,
        name="Testville",
        code="M-01",
        municipality_type="municipality",
    )
    db.session.add(municipality)
    db.session.commit()
    return {"district": district, "municipality": municipality}


# --- envelope --------------------------------------------------------------


def test_districts_use_the_locked_success_envelope(client, db):
    response = client.get("/api/v1/map/districts")
    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == {"status", "data"}
    assert body["status"] == "success"
    assert body["data"]["districts"] == []


def test_errors_use_the_locked_error_envelope(client, db):
    body = client.get("/api/v1/map/districts/not-a-uuid").get_json()
    assert set(body) == {"status", "error"}
    assert body["status"] == "error"
    assert set(body["error"]) >= {"code", "message"}


# --- empty dataset ---------------------------------------------------------


def test_empty_dataset_returns_an_empty_list_not_an_error(client, db):
    """No data is a valid answer, not a failure."""
    assert client.get("/api/v1/map/districts").get_json()["data"]["districts"] == []
    assert (
        client.get("/api/v1/map/municipalities").get_json()["data"]["municipalities"] == []
    )


def test_coverage_reports_an_empty_database_honestly(client, db):
    data = client.get("/api/v1/map/coverage").get_json()["data"]
    assert data == {
        "spatial_backend": False,
        "districts": 0,
        "districts_with_boundary": 0,
        "municipalities": 0,
        "municipalities_with_boundary": 0,
    }


# --- districts -------------------------------------------------------------


def test_list_districts(client, geo):
    districts = client.get("/api/v1/map/districts").get_json()["data"]["districts"]
    assert len(districts) == 1
    assert districts[0]["name"] == "Testland"
    assert districts[0]["geometry"] is None


def test_list_districts_filtered_by_province(client, geo):
    hit = client.get("/api/v1/map/districts?province=TestProvince")
    miss = client.get("/api/v1/map/districts?province=Nowhere")
    assert len(hit.get_json()["data"]["districts"]) == 1
    assert miss.get_json()["data"]["districts"] == []


def test_get_district_by_id(client, geo):
    district_id = str(geo["district"].id)
    response = client.get(f"/api/v1/map/districts/{district_id}")
    assert response.status_code == 200
    assert response.get_json()["data"]["district"]["id"] == district_id


def test_get_district_with_an_invalid_uuid_returns_400(client, db):
    response = client.get("/api/v1/map/districts/not-a-uuid")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_identifier"


def test_get_district_that_does_not_exist_returns_404(client, db):
    response = client.get(f"/api/v1/map/districts/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "district_not_found"


def test_district_response_carries_provenance(client, geo):
    district = client.get(
        f"/api/v1/map/districts/{geo['district'].id}"
    ).get_json()["data"]["district"]
    assert district["provenance"]["verification_status"] == "unverified"


# --- municipalities --------------------------------------------------------


def test_list_municipalities(client, geo):
    data = client.get("/api/v1/map/municipalities").get_json()["data"]
    assert len(data["municipalities"]) == 1
    assert data["municipalities"][0]["name"] == "Testville"


def test_list_municipalities_filtered_by_district(client, geo):
    district_id = str(geo["district"].id)
    hit = client.get(f"/api/v1/map/municipalities?district_id={district_id}")
    miss = client.get(f"/api/v1/map/municipalities?district_id={uuid.uuid4()}")
    assert len(hit.get_json()["data"]["municipalities"]) == 1
    assert miss.get_json()["data"]["municipalities"] == []


def test_list_municipalities_with_an_invalid_district_filter_returns_400(client, db):
    response = client.get("/api/v1/map/municipalities?district_id=nope")
    assert response.status_code == 400


def test_get_municipality_by_id(client, geo):
    municipality_id = str(geo["municipality"].id)
    response = client.get(f"/api/v1/map/municipalities/{municipality_id}")
    assert response.status_code == 200

    payload = response.get_json()["data"]["municipality"]
    assert payload["name"] == "Testville"
    assert payload["district_id"] == str(geo["district"].id)


def test_get_municipality_with_an_invalid_uuid_returns_400(client, db):
    assert client.get("/api/v1/map/municipalities/nope").status_code == 400


def test_get_municipality_that_does_not_exist_returns_404(client, db):
    response = client.get(f"/api/v1/map/municipalities/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "municipality_not_found"


# --- reverse geocoding -----------------------------------------------------


def test_reverse_geocode_reports_that_it_cannot_resolve(client, db):
    """Without PostGIS the endpoint must say so, not guess a district."""
    response = client.get("/api/v1/map/reverse-geocode?lat=27.7&lng=85.3")
    assert response.status_code == 200

    data = response.get_json()["data"]
    assert data["resolved"] is False
    assert data["reason"] == REASON_NO_SPATIAL_BACKEND
    assert data["district"] is None
    assert data["municipality"] is None
    assert data["province"] is None


def test_reverse_geocode_echoes_the_point_in_geojson_order(client, db):
    data = client.get("/api/v1/map/reverse-geocode?lat=27.7&lng=85.3").get_json()["data"]
    assert data["point"] == {"type": "Point", "coordinates": [85.3, 27.7]}
    assert data["coordinates"] == {"latitude": 27.7, "longitude": 85.3}


def test_reverse_geocode_accepts_lon_as_well_as_lng(client, db):
    response = client.get("/api/v1/map/reverse-geocode?lat=27.7&lon=85.3")
    assert response.status_code == 200


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?lat=27.7",
        "?lng=85.3",
        "?lat=&lng=",
        "?lat=abc&lng=85.3",
        "?lat=27.7&lng=abc",
        "?lat=91&lng=85.3",
        "?lat=27.7&lng=181",
        "?lat=-91&lng=0",
        "?lat=nan&lng=0",
    ],
)
def test_reverse_geocode_rejects_bad_coordinates(client, db, query):
    response = client.get(f"/api/v1/map/reverse-geocode{query}")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_coordinates"


def test_reverse_geocode_never_reports_resolved_without_boundary_data(client, geo):
    """Records exist, but none has geometry. Still unresolved."""
    data = client.get("/api/v1/map/reverse-geocode?lat=27.7&lng=85.3").get_json()["data"]
    assert data["resolved"] is False
    assert data["reason"] in {REASON_NO_SPATIAL_BACKEND, REASON_NO_BOUNDARY_DATA}


# --- access control --------------------------------------------------------


def test_map_endpoints_are_public(client, geo):
    """Administrative names and boundaries are public information."""
    for path in [
        "/api/v1/map/districts",
        "/api/v1/map/municipalities",
        "/api/v1/map/coverage",
        "/api/v1/map/reverse-geocode?lat=27.7&lng=85.3",
        f"/api/v1/map/districts/{geo['district'].id}",
        f"/api/v1/map/municipalities/{geo['municipality'].id}",
    ]:
        assert client.get(path).status_code == 200, path


def test_map_exposes_no_mutation_endpoints(app):
    """This phase is read-only.

    Fails the moment someone adds a write route without an authorization
    decorator, which is exactly when it should.
    """
    writable = [
        rule
        for rule in app.url_map.iter_rules()
        if rule.rule.startswith("/api/v1/map")
        and {"POST", "PUT", "PATCH", "DELETE"} & rule.methods
    ]
    assert writable == []


def test_previous_phase_endpoints_still_work(client, db):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/hello").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
