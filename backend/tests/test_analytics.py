"""Phase 11: analytics, dashboards and map feeds.

Runs on SQLite. The heaviest emphasis here is on two things that are easy to
get quietly wrong: what a *public* feed exposes, and whether an aggregate is
computed over the denominator it claims.

All districts, authorities and coordinates are SYNTHETIC test data, not Nepal.
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
    Project,
    ProjectStatus,
    Report,
    ReportStatus,
)
from app.models.base import utcnow
from app.services import analytics_service

PASSWORD = "correct-horse-battery"

REPORT = {
    "title": "Deep pothole on the main road",
    "description": "A large pothole has opened and is dangerous for motorcycles.",
    "category": "road_damage",
    "lat": 0.5,
    "lng": 0.5,
}


@pytest.fixture
def areas(db):
    first = District(name="Testland", province="TestProvince", code="D-01")
    second = District(name="Otherland", province="TestProvince", code="D-02")
    db.session.add_all([first, second])
    db.session.commit()
    municipality = Municipality(district_id=first.id, name="Testville", code="M-01")
    db.session.add(municipality)
    db.session.commit()
    return {"district": first, "other_district": second, "municipality": municipality}


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
def authority_headers(make_user, auth_headers):
    make_user(email="officer@betternepal.np", role_names=("authority",))
    return auth_headers("officer@betternepal.np", PASSWORD)


@pytest.fixture
def admin_headers(make_user, auth_headers):
    make_user(email="admin@betternepal.np", role_names=("admin",))
    return auth_headers("admin@betternepal.np", PASSWORD)


@pytest.fixture
def authority_record(db, areas):
    record = Authority(
        name="Test Roads Office",
        level=GovernmentLevel.PROVINCIAL,
        type=AuthorityType.DEPARTMENT_OF_ROADS,
        district_id=areas["district"].id,
    )
    db.session.add(record)
    db.session.commit()
    return record


def _make_report(client, headers, **overrides):
    response = client.post("/api/v1/reports", json={**REPORT, **overrides}, headers=headers)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["report"]["id"]


def _promote(client, headers, report_id, **body):
    response = client.post(
        "/api/v1/incidents/from-report",
        json={"report_id": report_id, **body},
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["incident"]["id"]


# --- map points: privacy ---------------------------------------------------


def test_map_points_never_expose_the_reporter(client, citizen_headers, located):
    """The privacy control is the SELECT list, not a serialiser."""
    _make_report(client, citizen_headers)

    body = client.get("/api/v1/analytics/map/points").get_data(as_text=True)
    assert "citizen@betternepal.np" not in body
    assert "password_hash" not in body
    assert "argon2" not in body
    assert "reporter" not in body


def test_report_points_carry_only_the_declared_fields(client, citizen_headers, located):
    _make_report(client, citizen_headers)
    point = client.get("/api/v1/analytics/map/points?type=reports").get_json()["data"][
        "points"
    ][0]
    assert set(point) == set(analytics_service.MAP_POINT_COLUMNS)


def test_incident_points_carry_only_the_declared_fields(
    client, citizen_headers, authority_headers, located
):
    report_id = _make_report(client, citizen_headers)
    _promote(client, authority_headers, report_id)

    point = client.get("/api/v1/analytics/map/points?type=incidents").get_json()["data"][
        "points"
    ][0]
    assert set(point) == set(analytics_service.INCIDENT_POINT_COLUMNS)


def test_map_points_omit_the_description(client, citizen_headers, located):
    """A map needs a pin and a label; shipping bodies is slow and leaky."""
    _make_report(client, citizen_headers, description="Internal detail nobody needs here.")
    body = client.get("/api/v1/analytics/map/points").get_data(as_text=True)
    assert "Internal detail nobody needs here." not in body


def test_incident_points_omit_the_description(client, citizen_headers, authority_headers, located):
    report_id = _make_report(client, citizen_headers)
    _promote(client, authority_headers, report_id)
    body = client.get("/api/v1/analytics/map/points?type=incidents").get_data(as_text=True)
    assert REPORT["description"] not in body


def test_map_points_are_public(client, citizen_headers, located):
    _make_report(client, citizen_headers)
    assert client.get("/api/v1/analytics/map/points").status_code == 200


# --- map points: content and filtering -------------------------------------


def test_map_points_include_reports_and_incidents(
    client, citizen_headers, authority_headers, located
):
    first = _make_report(client, citizen_headers)
    _make_report(client, citizen_headers, lat=0.6)
    _promote(client, authority_headers, first)

    points = client.get("/api/v1/analytics/map/points").get_json()["data"]["points"]
    kinds = {point["type"] for point in points}
    assert kinds == {"report", "incident"}


@pytest.mark.parametrize("kind,expected", [("reports", "report"), ("incidents", "incident")])
def test_type_filter(client, citizen_headers, authority_headers, located, kind, expected):
    report_id = _make_report(client, citizen_headers)
    _promote(client, authority_headers, report_id)
    # A second, unpromoted report: the first is now represented by its incident
    # and no longer appears in the reports feed.
    _make_report(client, citizen_headers, lat=0.6)

    points = client.get(f"/api/v1/analytics/map/points?type={kind}").get_json()["data"][
        "points"
    ]
    assert points
    assert all(point["type"] == expected for point in points)


def test_filter_by_category(client, citizen_headers, located):
    _make_report(client, citizen_headers, category="road_damage")
    _make_report(client, citizen_headers, category="water_leak", lat=0.6)

    points = client.get(
        "/api/v1/analytics/map/points?category=water_leak"
    ).get_json()["data"]["points"]
    assert len(points) == 1
    assert points[0]["category"] == "water_leak"


def test_filter_by_district(client, citizen_headers, located, areas, db):
    _make_report(client, citizen_headers)
    # Move one report into the other district directly.
    other = _make_report(client, citizen_headers, lat=0.6)
    record = db.session.get(Report, uuid.UUID(other))
    record.district_id = areas["other_district"].id
    db.session.commit()

    first = client.get(
        f"/api/v1/analytics/map/points?district_id={areas['district'].id}"
    ).get_json()["data"]
    second = client.get(
        f"/api/v1/analytics/map/points?district_id={areas['other_district'].id}"
    ).get_json()["data"]

    assert first["count"] == 1
    assert second["count"] == 1
    assert second["points"][0]["id"] == other


def test_filter_by_status(client, citizen_headers, authority_headers, located):
    first = _make_report(client, citizen_headers)
    _make_report(client, citizen_headers, lat=0.6)
    client.patch(
        f"/api/v1/reports/{first}/status",
        json={"status": "under_review"},
        headers=authority_headers,
    )

    points = client.get(
        "/api/v1/analytics/map/points?type=reports&status=under_review"
    ).get_json()["data"]["points"]
    assert len(points) == 1
    assert points[0]["id"] == first


def test_filter_by_date_range(client, citizen_headers, located, db):
    old = _make_report(client, citizen_headers)
    recent = _make_report(client, citizen_headers, lat=0.6)

    record = db.session.get(Report, uuid.UUID(old))
    record.created_at = utcnow() - timedelta(days=60)
    db.session.commit()

    cutoff = (date.today() - timedelta(days=7)).isoformat()
    points = client.get(
        f"/api/v1/analytics/map/points?type=reports&start_date={cutoff}"
    ).get_json()["data"]["points"]
    assert [point["id"] for point in points] == [recent]


def test_an_end_date_includes_that_whole_day(client, citizen_headers, located):
    """`?end_date=today` must not exclude everything reported today."""
    _make_report(client, citizen_headers)
    today = date.today().isoformat()

    points = client.get(
        f"/api/v1/analytics/map/points?type=reports&end_date={today}"
    ).get_json()["data"]["points"]
    assert len(points) == 1


def test_start_after_end_is_rejected(client, db):
    response = client.get(
        "/api/v1/analytics/map/points?start_date=2026-09-20&end_date=2026-09-01"
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_date_range"


@pytest.mark.parametrize("value", ["not-a-date", "01-01-2026", "2026-13-99"])
def test_an_invalid_date_is_rejected(client, db, value):
    response = client.get(f"/api/v1/analytics/map/points?start_date={value}")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_date"


@pytest.mark.parametrize("value", ["banana", "reports,incidents", "REPORT"])
def test_an_invalid_type_is_rejected(client, db, value):
    response = client.get(f"/api/v1/analytics/map/points?type={value}")
    assert response.status_code == 400


def test_an_invalid_category_filter_is_rejected(client, db):
    response = client.get("/api/v1/analytics/map/points?category=nonsense")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_filter"


def test_an_invalid_district_filter_is_rejected(client, db):
    assert client.get("/api/v1/analytics/map/points?district_id=nope").status_code == 400


def test_an_empty_map_is_not_an_error(client, db):
    data = client.get("/api/v1/analytics/map/points").get_json()["data"]
    assert data["points"] == []
    assert data["count"] == 0
    assert data["truncated"] is False


# --- map points: what is excluded ------------------------------------------


def test_rejected_reports_are_excluded_by_default(
    client, citizen_headers, authority_headers, located
):
    keep = _make_report(client, citizen_headers)
    drop = _make_report(client, citizen_headers, lat=0.6)
    client.patch(
        f"/api/v1/reports/{drop}/status",
        json={"status": "rejected"},
        headers=authority_headers,
    )

    ids = [
        point["id"]
        for point in client.get("/api/v1/analytics/map/points?type=reports").get_json()[
            "data"
        ]["points"]
    ]
    assert ids == [keep]


def test_duplicate_reports_are_excluded_by_default(
    client, citizen_headers, authority_headers, located
):
    """Plotting the same pothole five times overstates how much is wrong."""
    master = _make_report(client, citizen_headers)
    duplicate = _make_report(client, citizen_headers, lat=0.5001)
    client.post(
        f"/api/v1/reports/{duplicate}/mark-duplicate",
        json={"duplicate_of_id": master},
        headers=authority_headers,
    )

    ids = [
        point["id"]
        for point in client.get("/api/v1/analytics/map/points?type=reports").get_json()[
            "data"
        ]["points"]
    ]
    assert ids == [master]


def test_include_inactive_brings_them_back(
    client, citizen_headers, authority_headers, located
):
    _make_report(client, citizen_headers)
    drop = _make_report(client, citizen_headers, lat=0.6)
    client.patch(
        f"/api/v1/reports/{drop}/status",
        json={"status": "rejected"},
        headers=authority_headers,
    )

    data = client.get(
        "/api/v1/analytics/map/points?type=reports&include_inactive=true"
    ).get_json()["data"]
    assert data["count"] == 2


def test_closed_incidents_are_excluded_by_default(
    client, citizen_headers, authority_headers, located
):
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id)
    client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "closed"},
        headers=authority_headers,
    )

    data = client.get("/api/v1/analytics/map/points?type=incidents").get_json()["data"]
    assert data["count"] == 0


# --- limits ----------------------------------------------------------------


def test_the_feed_is_capped(client, citizen_headers, located):
    for index in range(5):
        _make_report(client, citizen_headers, lat=0.5 + index / 1000)

    data = client.get("/api/v1/analytics/map/points?limit=2").get_json()["data"]
    assert data["count"] == 2
    assert data["limit"] == 2


def test_truncation_is_reported_honestly(client, citizen_headers, located):
    """A client must never mistake a capped feed for the whole picture."""
    for index in range(3):
        _make_report(client, citizen_headers, lat=0.5 + index / 1000)

    capped = client.get("/api/v1/analytics/map/points?limit=2").get_json()["data"]
    whole = client.get("/api/v1/analytics/map/points?limit=50").get_json()["data"]
    assert capped["truncated"] is True
    assert whole["truncated"] is False


def test_the_limit_cannot_exceed_the_maximum(client, db):
    data = client.get("/api/v1/analytics/map/points?limit=999999").get_json()["data"]
    assert data["limit"] == analytics_service.MAX_MAP_POINTS


# --- GeoJSON ---------------------------------------------------------------


def test_geojson_format(client, citizen_headers, located):
    _make_report(client, citizen_headers)
    data = client.get("/api/v1/analytics/map/points?format=geojson").get_json()["data"]

    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 1
    assert data["features"][0]["type"] == "Feature"
    assert data["features"][0]["geometry"]["type"] == "Point"


def test_geojson_uses_longitude_latitude_order(client, citizen_headers, located):
    """The single most common GIS bug, checked once more at the boundary."""
    _make_report(client, citizen_headers, lat=10.0, lng=20.0)
    feature = client.get("/api/v1/analytics/map/points?format=geojson").get_json()["data"][
        "features"
    ][0]
    assert feature["geometry"]["coordinates"] == [20.0, 10.0]


def test_geojson_properties_exclude_raw_coordinates(client, citizen_headers, located):
    _make_report(client, citizen_headers)
    feature = client.get("/api/v1/analytics/map/points?format=geojson").get_json()["data"][
        "features"
    ][0]
    assert "latitude" not in feature["properties"]
    assert "longitude" not in feature["properties"]
    assert feature["properties"]["type"] == "report"


def test_geojson_never_exposes_the_reporter(client, citizen_headers, located):
    _make_report(client, citizen_headers)
    body = client.get("/api/v1/analytics/map/points?format=geojson").get_data(as_text=True)
    assert "citizen@betternepal.np" not in body


# --- district summary ------------------------------------------------------


def test_district_summary_counts(client, citizen_headers, authority_headers, located, areas):
    first = _make_report(client, citizen_headers)
    _make_report(client, citizen_headers, lat=0.6)
    _promote(client, authority_headers, first)

    rows = client.get("/api/v1/analytics/map/districts").get_json()["data"]["districts"]
    testland = next(row for row in rows if row["district_name"] == "Testland")

    assert testland["report_count"] == 2
    assert testland["incident_count"] == 1
    assert testland["open_incident_count"] == 1


def test_district_summary_lists_every_district_even_at_zero(client, areas):
    """A missing row reads as missing data, not as zero."""
    rows = client.get("/api/v1/analytics/map/districts").get_json()["data"]["districts"]
    assert {row["district_name"] for row in rows} == {"Testland", "Otherland"}


def test_resolution_rate_is_none_when_there_is_nothing_to_divide(client, areas):
    """0% would read as total failure rather than 'no incidents yet'."""
    rows = client.get("/api/v1/analytics/map/districts").get_json()["data"]["districts"]
    assert all(row["resolution_rate"] is None for row in rows)


def test_resolution_rate_is_calculated(
    client, citizen_headers, authority_headers, located
):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.6)
    resolved = _promote(client, authority_headers, first)
    _promote(client, authority_headers, second)

    client.patch(
        f"/api/v1/incidents/{resolved}",
        json={"status": "resolved"},
        headers=authority_headers,
    )

    rows = client.get("/api/v1/analytics/map/districts").get_json()["data"]["districts"]
    testland = next(row for row in rows if row["district_name"] == "Testland")
    assert testland["resolved_incident_count"] == 1
    assert testland["resolution_rate"] == 50.0


def test_unlocated_reports_are_surfaced(client, citizen_headers, db, areas):
    """Reverse geocoding failures are a coverage gap worth seeing."""
    # No `located` fixture, so nothing resolves to a district.
    client.post("/api/v1/reports", json=REPORT, headers=citizen_headers)
    data = client.get("/api/v1/analytics/map/districts").get_json()["data"]
    assert data["unlocated_reports"] == 1


def test_district_summary_excludes_duplicates(
    client, citizen_headers, authority_headers, located
):
    master = _make_report(client, citizen_headers)
    duplicate = _make_report(client, citizen_headers, lat=0.5001)
    client.post(
        f"/api/v1/reports/{duplicate}/mark-duplicate",
        json={"duplicate_of_id": master},
        headers=authority_headers,
    )

    rows = client.get("/api/v1/analytics/map/districts").get_json()["data"]["districts"]
    testland = next(row for row in rows if row["district_name"] == "Testland")
    assert testland["report_count"] == 1


# --- overview --------------------------------------------------------------


def test_overview_on_an_empty_system(client, db):
    data = client.get("/api/v1/analytics/overview").get_json()["data"]
    assert data["reports"]["total"] == 0
    assert data["incidents"]["total"] == 0
    assert data["incidents"]["resolution_rate"] is None
    assert data["projects"]["total"] == 0
    assert data["authorities"]["total"] == 0


def test_overview_counts_are_accurate(
    client, citizen_headers, authority_headers, located, authority_record, db
):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.6)
    third = _make_report(client, citizen_headers, lat=0.7)

    incident_id = _promote(client, authority_headers, first)
    client.patch(
        f"/api/v1/reports/{third}/status",
        json={"status": "rejected"},
        headers=authority_headers,
    )

    db.session.add(
        Project(
            title="Culvert reconstruction",
            description="Rebuild the failed culvert.",
            authority_id=authority_record.id,
            status=ProjectStatus.ACTIVE,
        )
    )
    db.session.commit()

    data = client.get("/api/v1/analytics/overview").get_json()["data"]
    assert data["reports"]["total"] == 3
    assert data["reports"]["verified"] == 1
    assert data["reports"]["rejected"] == 1
    assert data["reports"]["pending"] == 1  # `second` is still submitted
    assert data["incidents"]["total"] == 1
    assert data["incidents"]["active"] == 1
    assert data["incidents"]["unassigned"] == 1
    assert data["projects"]["total"] == 1
    assert data["projects"]["active"] == 1
    assert data["authorities"]["total"] == 1
    assert incident_id


def test_overview_resolution_rate(client, citizen_headers, authority_headers, located):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.6)
    resolved = _promote(client, authority_headers, first)
    _promote(client, authority_headers, second)
    client.patch(
        f"/api/v1/incidents/{resolved}",
        json={"status": "resolved"},
        headers=authority_headers,
    )

    data = client.get("/api/v1/analytics/overview").get_json()["data"]
    assert data["incidents"]["resolution_rate"] == 50.0


def test_overview_counts_duplicates(client, citizen_headers, authority_headers, located):
    master = _make_report(client, citizen_headers)
    duplicate = _make_report(client, citizen_headers, lat=0.5001)
    client.post(
        f"/api/v1/reports/{duplicate}/mark-duplicate",
        json={"duplicate_of_id": master},
        headers=authority_headers,
    )

    data = client.get("/api/v1/analytics/overview").get_json()["data"]
    assert data["reports"]["duplicates"] == 1


def test_overview_is_public(client, db):
    assert client.get("/api/v1/analytics/overview").status_code == 200


def test_overview_leaks_nothing_personal(client, citizen_headers, located):
    _make_report(client, citizen_headers)
    body = client.get("/api/v1/analytics/overview").get_data(as_text=True)
    assert "citizen@betternepal.np" not in body


# --- resolution timing -----------------------------------------------------


def test_resolved_at_is_stamped_on_resolution(
    client, citizen_headers, authority_headers, located, db
):
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id)

    assert db.session.get(Incident, uuid.UUID(incident_id)).resolved_at is None
    client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "resolved"},
        headers=authority_headers,
    )
    assert db.session.get(Incident, uuid.UUID(incident_id)).resolved_at is not None


def test_resolved_at_is_cleared_when_work_reopens(
    client, citizen_headers, authority_headers, located, db
):
    """Work that turns out not to be finished must not keep a completion date."""
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id)

    client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "resolved"},
        headers=authority_headers,
    )
    client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "in_progress"},
        headers=authority_headers,
    )
    assert db.session.get(Incident, uuid.UUID(incident_id)).resolved_at is None


def test_completing_a_project_stamps_the_incident(
    client, citizen_headers, authority_headers, located, authority_record, db, make_user
):
    """The Phase 8 cascade must record when, not just that."""
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id)

    project = client.post(
        "/api/v1/projects",
        json={
            "title": "Culvert reconstruction works",
            "description": "Rebuild the failed culvert and reinstate the road.",
            "authority_id": str(authority_record.id),
            "incident_id": incident_id,
        },
        headers=authority_headers,
    ).get_json()["data"]["project"]["id"]

    for status in ("active", "completed"):
        client.post(
            f"/api/v1/projects/{project}/updates",
            json={"notes": f"Moving to {status}.", "new_status": status},
            headers=authority_headers,
        )

    incident = db.session.get(Incident, uuid.UUID(incident_id))
    assert incident.status == IncidentStatus.RESOLVED
    assert incident.resolved_at is not None


def test_duration_summary_maths():
    """Average, median and range over known durations."""
    created = utcnow()
    pairs = [
        (created, created + timedelta(days=2)),
        (created, created + timedelta(days=4)),
        (created, created + timedelta(days=6)),
    ]
    summary = analytics_service._summarise_durations(pairs)

    assert summary["measured_count"] == 3
    assert summary["average_days"] == 4.0
    assert summary["median_days"] == 4.0
    assert summary["fastest_days"] == 2.0
    assert summary["slowest_days"] == 6.0


def test_duration_summary_ignores_unmeasurable_pairs():
    """A missing or inverted timestamp is dropped, never guessed at."""
    created = utcnow()
    pairs = [
        (created, None),
        (None, created),
        (created, created - timedelta(days=1)),  # resolved before created
        (created, created + timedelta(days=3)),
    ]
    summary = analytics_service._summarise_durations(pairs)
    assert summary["measured_count"] == 1
    assert summary["average_days"] == 3.0


def test_duration_summary_with_no_data_returns_none_not_zero():
    summary = analytics_service._summarise_durations([])
    assert summary["measured_count"] == 0
    assert summary["average_days"] is None


# --- authority performance -------------------------------------------------


def test_authority_performance_requires_a_role(client, citizen_headers, authority_record):
    """Per-office league tables are an editorial decision, not a default."""
    response = client.get(
        "/api/v1/analytics/authorities/performance", headers=citizen_headers
    )
    assert response.status_code == 403


def test_authority_performance_is_not_public(client, authority_record, db):
    assert client.get("/api/v1/analytics/authorities/performance").status_code == 401


def test_authority_can_view_performance(client, authority_headers, authority_record):
    response = client.get(
        "/api/v1/analytics/authorities/performance", headers=authority_headers
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["count"] == 1


def test_admin_can_view_performance(client, admin_headers, authority_record):
    assert client.get(
        "/api/v1/analytics/authorities/performance", headers=admin_headers
    ).status_code == 200


def test_performance_reports_backlog_and_workload(
    client, citizen_headers, authority_headers, located, authority_record, db
):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.6)
    open_incident = _promote(client, authority_headers, first)
    resolved_incident = _promote(client, authority_headers, second)

    for incident_id in (open_incident, resolved_incident):
        client.post(
            f"/api/v1/incidents/{incident_id}/assign",
            json={"authority_id": str(authority_record.id)},
            headers=authority_headers,
        )
    client.patch(
        f"/api/v1/incidents/{resolved_incident}",
        json={"status": "resolved"},
        headers=authority_headers,
    )

    row = client.get(
        "/api/v1/analytics/authorities/performance", headers=authority_headers
    ).get_json()["data"]["authorities"][0]

    assert row["assigned_incidents"] == 2
    assert row["backlog"] == 1
    assert row["resolved_incidents"] == 1


def test_performance_measures_resolution_time(
    client, citizen_headers, authority_headers, located, authority_record, db
):
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id)
    client.post(
        f"/api/v1/incidents/{incident_id}/assign",
        json={"authority_id": str(authority_record.id)},
        headers=authority_headers,
    )

    # Backdate creation so the measured duration is a known five days.
    incident = db.session.get(Incident, uuid.UUID(incident_id))
    incident.created_at = utcnow() - timedelta(days=5)
    db.session.commit()

    client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "resolved"},
        headers=authority_headers,
    )

    resolution = client.get(
        "/api/v1/analytics/authorities/performance", headers=authority_headers
    ).get_json()["data"]["authorities"][0]["resolution"]

    assert resolution["measured_count"] == 1
    assert 4.9 < resolution["average_days"] < 5.1
    assert resolution["unmeasured_count"] == 0


def test_untimed_resolutions_are_counted_not_hidden(
    client, citizen_headers, authority_headers, located, authority_record, db
):
    """A mean computed over whatever happens to have data is worse than a mean
    with a stated denominator."""
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id)
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

    # Simulate a row resolved before resolved_at existed.
    incident = db.session.get(Incident, uuid.UUID(incident_id))
    incident.resolved_at = None
    db.session.commit()

    resolution = client.get(
        "/api/v1/analytics/authorities/performance", headers=authority_headers
    ).get_json()["data"]["authorities"][0]["resolution"]

    assert resolution["measured_count"] == 0
    assert resolution["average_days"] is None
    assert resolution["unmeasured_count"] == 1


def test_performance_can_be_narrowed_to_one_authority(
    client, authority_headers, authority_record, db, areas
):
    db.session.add(
        Authority(
            name="Second Office",
            level=GovernmentLevel.LOCAL,
            type=AuthorityType.MUNICIPAL_OFFICE,
            district_id=areas["district"].id,
        )
    )
    db.session.commit()

    everything = client.get(
        "/api/v1/analytics/authorities/performance", headers=authority_headers
    ).get_json()["data"]
    narrowed = client.get(
        f"/api/v1/analytics/authorities/performance?authority_id={authority_record.id}",
        headers=authority_headers,
    ).get_json()["data"]

    assert everything["count"] == 2
    assert narrowed["count"] == 1
    assert narrowed["authorities"][0]["authority_id"] == str(authority_record.id)


def test_performance_for_an_unknown_authority_is_404(client, authority_headers, db):
    response = client.get(
        f"/api/v1/analytics/authorities/performance?authority_id={uuid.uuid4()}",
        headers=authority_headers,
    )
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "authority_not_found"


def test_performance_surfaces_unassigned_incidents(
    client, citizen_headers, authority_headers, located, authority_record
):
    report_id = _make_report(client, citizen_headers)
    _promote(client, authority_headers, report_id)

    data = client.get(
        "/api/v1/analytics/authorities/performance", headers=authority_headers
    ).get_json()["data"]
    assert data["unassigned_incidents"] == 1


def test_performance_counts_projects(
    client, authority_headers, authority_record, db
):
    db.session.add_all(
        [
            Project(
                title="Active works",
                description="x" * 20,
                authority_id=authority_record.id,
                status=ProjectStatus.ACTIVE,
            ),
            Project(
                title="Finished works",
                description="x" * 20,
                authority_id=authority_record.id,
                status=ProjectStatus.COMPLETED,
            ),
        ]
    )
    db.session.commit()

    row = client.get(
        "/api/v1/analytics/authorities/performance", headers=authority_headers
    ).get_json()["data"]["authorities"][0]
    assert row["total_projects"] == 2
    assert row["active_projects"] == 1
    assert row["completed_projects"] == 1


# --- category distribution -------------------------------------------------


def test_category_distribution_lists_every_category(client, db):
    """A chart with a silently missing slice is a misleading chart."""
    from app.models import ReportCategory

    data = client.get("/api/v1/analytics/categories").get_json()["data"]
    assert {row["category"] for row in data["categories"]} == {
        member.value for member in ReportCategory
    }


def test_category_counts(client, citizen_headers, authority_headers, located):
    _make_report(client, citizen_headers, category="road_damage")
    _make_report(client, citizen_headers, category="road_damage", lat=0.6)
    water = _make_report(client, citizen_headers, category="water_leak", lat=0.7)
    _promote(client, authority_headers, water)

    data = client.get("/api/v1/analytics/categories").get_json()["data"]
    roads = next(row for row in data["categories"] if row["category"] == "road_damage")
    leaks = next(row for row in data["categories"] if row["category"] == "water_leak")

    assert roads["report_count"] == 2
    assert roads["incident_count"] == 0
    assert leaks["incident_count"] == 1
    assert data["totals"]["reports"] == 3


def test_escalation_rate(client, citizen_headers, authority_headers, located):
    first = _make_report(client, citizen_headers, category="road_damage")
    _make_report(client, citizen_headers, category="road_damage", lat=0.6)
    _promote(client, authority_headers, first)

    data = client.get("/api/v1/analytics/categories").get_json()["data"]
    roads = next(row for row in data["categories"] if row["category"] == "road_damage")
    assert roads["escalation_rate"] == 50.0


def test_shares_are_none_with_no_data(client, db):
    data = client.get("/api/v1/analytics/categories").get_json()["data"]
    assert all(row["report_share"] is None for row in data["categories"])
    assert all(row["escalation_rate"] is None for row in data["categories"])


def test_category_distribution_includes_severity(
    client, citizen_headers, authority_headers, located
):
    from app.models import IncidentSeverity

    report_id = _make_report(client, citizen_headers)
    _promote(client, authority_headers, report_id, severity="critical")

    data = client.get("/api/v1/analytics/categories").get_json()["data"]
    assert set(data["by_severity"]) == {member.value for member in IncidentSeverity}
    assert data["by_severity"]["critical"] == 1


def test_categories_are_public(client, db):
    assert client.get("/api/v1/analytics/categories").status_code == 200


# --- trends ----------------------------------------------------------------


def test_trend_series_covers_the_window(client, db):
    data = client.get("/api/v1/analytics/trends?days=7").get_json()["data"]
    assert data["days"] == 7
    assert len(data["series"]) == 8  # inclusive of both ends


def test_trend_counts_todays_activity(client, citizen_headers, located):
    _make_report(client, citizen_headers)
    series = client.get("/api/v1/analytics/trends?days=1").get_json()["data"]["series"]
    assert sum(day["reports"] for day in series) == 1


def test_trend_window_is_clamped(client, db):
    assert client.get("/api/v1/analytics/trends?days=99999").get_json()["data"]["days"] == 365
    assert client.get("/api/v1/analytics/trends?days=-5").get_json()["data"]["days"] == 1
    assert client.get("/api/v1/analytics/trends?days=abc").get_json()["data"]["days"] == 30


# --- query efficiency ------------------------------------------------------


def test_district_summary_does_not_scale_queries_with_districts(client, db, areas):
    """Guards against a regression to one query per district."""
    from sqlalchemy import event

    statements = []
    engine = db.engine

    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    # Twenty districts; the query count must not move with them.
    for index in range(20):
        db.session.add(District(name=f"Bulk-{index}", code=f"B-{index:02d}"))
    db.session.commit()

    event.listen(engine, "before_cursor_execute", _record)
    try:
        client.get("/api/v1/analytics/map/districts")
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    selects = [s for s in statements if s.strip().upper().startswith("SELECT")]
    assert len(selects) <= 8, f"{len(selects)} SELECTs for 22 districts"


def test_map_points_issue_one_query_per_entity_type(client, citizen_headers, located, db):
    from sqlalchemy import event

    for index in range(10):
        _make_report(client, citizen_headers, lat=0.5 + index / 1000)

    statements = []

    def _record(conn, cursor, statement, params, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", _record)
    try:
        client.get("/api/v1/analytics/map/points?type=reports")
    finally:
        event.remove(db.engine, "before_cursor_execute", _record)

    assert len(statements) == 1, f"{len(statements)} SELECTs for 10 points"


# --- envelope --------------------------------------------------------------


def test_analytics_uses_the_locked_success_envelope(client, db):
    body = client.get("/api/v1/analytics/overview").get_json()
    assert set(body) == {"status", "data"}
    assert body["status"] == "success"


def test_analytics_errors_use_the_locked_error_envelope(client, db):
    body = client.get("/api/v1/analytics/map/points?type=banana").get_json()
    assert set(body) == {"status", "error"}
    assert set(body["error"]) >= {"code", "message"}


def test_analytics_exposes_no_mutation_endpoints(app):
    """Analytics is read-only; this fails the moment someone adds a writer."""
    writable = [
        rule
        for rule in app.url_map.iter_rules()
        if rule.rule.startswith("/api/v1/analytics")
        and {"POST", "PUT", "PATCH", "DELETE"} & rule.methods
    ]
    assert writable == []


# --- earlier phases still work ---------------------------------------------


def test_previous_endpoints_are_unaffected(client, db):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/reports").status_code == 200
    assert client.get("/api/v1/incidents").status_code == 200
    assert client.get("/api/v1/projects").status_code == 200
    assert client.get("/api/v1/authorities").status_code == 200
    assert client.get("/api/v1/media/statistics").status_code == 200
    assert client.get("/api/v1/map/districts").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


def test_a_verified_report_is_not_plotted_alongside_its_incident(
    client, citizen_headers, authority_headers, located
):
    """The same pothole must not appear twice on a `type=all` map.

    Once a report is verified it is represented by its incident; showing both
    is the same double-counting that duplicates cause.
    """
    report_id = _make_report(client, citizen_headers)
    incident_id = _promote(client, authority_headers, report_id)

    points = client.get("/api/v1/analytics/map/points").get_json()["data"]["points"]
    assert len(points) == 1
    assert points[0]["type"] == "incident"
    assert points[0]["id"] == incident_id


def test_a_verified_report_is_still_reachable_with_include_inactive(
    client, citizen_headers, authority_headers, located
):
    report_id = _make_report(client, citizen_headers)
    _promote(client, authority_headers, report_id)

    data = client.get(
        "/api/v1/analytics/map/points?type=reports&include_inactive=true"
    ).get_json()["data"]
    assert data["count"] == 1
    assert data["points"][0]["id"] == report_id
