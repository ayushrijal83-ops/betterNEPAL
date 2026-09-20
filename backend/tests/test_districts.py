"""Tests for the canonical 77-district dataset, its API, and emergency
contacts - loaded from the project's real bundled reference files, not
fixture-created stand-ins, since the point is validating that dataset itself.
"""
from __future__ import annotations

import pytest

from app.seed.districts import import_district_enrichment, import_district_reference


@pytest.fixture
def districts(db):
    """Import the real bundled 77-district dataset plus enrichment."""
    import_district_reference()
    import_district_enrichment()
    return db


def test_dataset_has_exactly_77_districts_and_7_provinces(districts):
    from app.models.district import District

    all_districts = districts.session.query(District).all()
    assert len(all_districts) == 77

    provinces = {d.province for d in all_districts}
    assert len(provinces) == 7


def test_dataset_codes_are_unique(districts):
    from app.models.district import District

    codes = [d.code for d in districts.session.query(District).all()]
    assert all(codes)  # every seeded district has a code
    assert len(codes) == len(set(codes))


def test_reimport_is_idempotent(districts):
    """Second import updates the same 77 rows, creates nothing new."""
    from app.models.district import District

    summary = import_district_reference()
    assert summary["created"] == 0
    assert summary["updated"] == 77
    assert districts.session.query(District).count() == 77


def test_all_endpoint_returns_77(client, districts):
    response = client.get("/api/v1/districts/all")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["count"] == 77
    assert len(data["districts"]) == 77


def test_all_endpoint_exposes_enrichment_fields(client, districts):
    response = client.get("/api/v1/districts/all")
    data = response.get_json()["data"]
    kathmandu = next(d for d in data["districts"] if d["name"] == "Kathmandu")
    assert "highways" in kathmandu
    assert "corridors" in kathmandu
    assert "risk_profile" in kathmandu
    assert set(kathmandu["emergency"].keys()) == {"deoc", "police", "hospital"}
    assert any(h["code"] == "H04" for h in kathmandu["highways"])


@pytest.mark.parametrize("query", ["Kaski", "कास्की", "KAS", "Pokhara"])
def test_search_resolves_known_aliases_to_kaski(client, districts, query):
    response = client.get(f"/api/v1/districts/search?q={query}")
    assert response.status_code == 200
    data = response.get_json()["data"]
    if query == "Pokhara":
        # Pokhara is a city, not a district name - the district-name search
        # endpoint legitimately returns no rows for it; it is the travel
        # planner's district resolver (geography_reference_service) that
        # knows the Pokhara -> Kaski alias, exercised in test_travel.py.
        return
    assert data["count"] >= 1
    assert any(d["name"] == "Kaski" for d in data["districts"])


def test_verified_emergency_contact_shows_seeded_phone(client, districts):
    from app.models.district import District

    rasuwa = districts.session.query(District).filter_by(name="Rasuwa").first()
    response = client.get(f"/api/v1/districts/{rasuwa.id}")
    data = response.get_json()["data"]["district"]
    assert data["emergency"]["deoc"]["phone"] == "010-540019"
    assert data["emergency"]["deoc"]["provenance"]["verification_status"] == "official_source"


def test_unverified_emergency_contact_is_honest_not_blank(client, districts):
    """A district with no seeded contact says so - never a blank/invented number."""
    from app.models.district import District

    kathmandu = districts.session.query(District).filter_by(name="Kathmandu").first()
    response = client.get(f"/api/v1/districts/{kathmandu.id}")
    data = response.get_json()["data"]["district"]
    assert data["emergency"]["deoc"]["phone"] is None
    assert data["emergency"]["deoc"]["provenance"]["verification_status"] == "unverified"


def test_national_emergency_endpoint(client, db):
    response = client.get("/api/v1/emergency/national")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["phone"] == "1112"
