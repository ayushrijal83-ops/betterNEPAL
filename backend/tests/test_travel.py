"""Tests for the travel planner: hazard-along-a-route, and the trip planner."""
from __future__ import annotations

import pytest

from app.models.district import District
from app.models.incident import Incident
from app.models.enums import IncidentSeverity, IncidentStatus, ReportCategory


@pytest.fixture
def kaski(db):
    district = District(name="Kaski", province="Gandaki", code="D-KAS")
    db.session.add(district)
    db.session.commit()
    return district


def test_plan_trip_resolves_known_city_to_its_real_district(client, db, kaski):
    """Pokhara is not itself a district - it must resolve to Kaski."""
    response = client.get("/api/v1/travel/plan?destination=Pokhara&days=3")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["destination"]["district"] == "Kaski"
    assert data["destination"]["province"] == "Gandaki"
    assert data["duration_days"] == 3
    assert len(data["itinerary"]) == 3
    assert "Pokhara" in data["itinerary"][0]["focus"]


def test_plan_trip_uses_real_district_name_directly(client, db, kaski):
    response = client.get("/api/v1/travel/plan?destination=Kaski&days=2")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["destination"]["district"] == "Kaski"


def test_plan_trip_returns_real_rivers_not_fabricated(client, db, kaski):
    response = client.get("/api/v1/travel/plan?destination=Pokhara&days=1")
    data = response.get_json()["data"]
    # Extracted verbatim from the project's own rivers/roads reference file.
    assert "Seti Gandaki" in data["geography"]["major_rivers"]


def test_plan_trip_flags_itinerary_as_not_curated(client, db, kaski):
    response = client.get("/api/v1/travel/plan?destination=Pokhara&days=1")
    data = response.get_json()["data"]
    assert data["is_curated_recommendations"] is False


def test_plan_trip_unknown_destination_is_404_not_a_guess(client, db):
    response = client.get("/api/v1/travel/plan?destination=Atlantis&days=3")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "destination_not_found"


def test_plan_trip_requires_destination(client, db):
    response = client.get("/api/v1/travel/plan?days=3")
    assert response.status_code == 400


@pytest.mark.parametrize("days", ["0", "31", "abc", "-1"])
def test_plan_trip_rejects_invalid_days(client, db, kaski, days):
    response = client.get(f"/api/v1/travel/plan?destination=Kaski&days={days}")
    assert response.status_code == 400


def test_plan_trip_surfaces_real_active_hazards_for_the_district(client, db, kaski):
    incident = Incident(
        title="Landslide blocking Prithvi Highway",
        description="Road blocked near Pokhara.",
        category=ReportCategory.ROAD_DAMAGE,
        severity=IncidentSeverity.CRITICAL,
        status=IncidentStatus.OPEN,
        district_id=kaski.id,
        latitude=28.2,
        longitude=83.98,
    )
    db.session.add(incident)
    db.session.commit()

    response = client.get("/api/v1/travel/plan?destination=Pokhara&days=2")
    data = response.get_json()["data"]
    assert data["safety_context"]["active_hazard_count"] == 1
    assert data["safety_context"]["danger_level"] == "HIGH"


def test_plan_trip_no_hazards_reports_low_danger(client, db, kaski):
    response = client.get("/api/v1/travel/plan?destination=Pokhara&days=2")
    data = response.get_json()["data"]
    assert data["safety_context"]["active_hazard_count"] == 0
    assert data["safety_context"]["danger_level"] == "LOW"


def test_plan_trip_road_corridor_matches_named_district(client, db, kaski):
    response = client.get("/api/v1/travel/plan?destination=Pokhara&days=1")
    data = response.get_json()["data"]
    names = [c["name"] for c in data["geography"]["road_corridors"]]
    assert "Prithvi Highway" in names
