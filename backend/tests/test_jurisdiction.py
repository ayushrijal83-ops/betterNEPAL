"""Tests for Jurisdiction Service."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.services.jurisdiction import (
    find_authorities_for_jurisdiction,
    find_authority_users_for_jurisdiction,
    resolve_jurisdiction,
)

from app.models import Authority, District
from app.models.enums import AuthorityType, GovernmentLevel
from app.models.user import User
from app.models.role import Role
from app.services.auth_service import hash_password


# --- resolve_jurisdiction tests -----------------------------------------------


def test_resolve_jurisdiction_success(db, make_user):
    """Valid coordinates resolve to district."""
    # Create a district
    from app.models import District

    district = District(name="Test District", code="TD-01")
    db.session.add(district)
    db.session.commit()

    # Mock geolocation_service to return the district
    with patch("app.services.geolocation_service.reverse_geocode") as mock_reverse:
        mock_reverse.return_value = {
            "resolved": True,
            "district": {"id": str(district.id), "name": "Test District"},
            "municipality": {"id": str(uuid.uuid4()), "name": "Test Municipality"},
            "province": "Test Province",
        }
        # Also need to mock spatial_backend_available
        with patch("app.services.geolocation_service.spatial_backend_available", return_value=True):
            result = resolve_jurisdiction(27.7172, 85.3240)

            assert result["resolved"] is True
            assert result["district_id"] == district.id
            assert result["district_name"] == "Test District"


def test_resolve_jurisdiction_no_spatial_backend(db):
    """No spatial backend AND no seeded districts for the dev fallback to
    match against -> genuinely unresolved, not a guess.
    """
    with patch("app.services.geolocation_service.spatial_backend_available", return_value=False):
        result = resolve_jurisdiction(27.7172, 85.3240)

        assert result["resolved"] is False
        assert result["reason"] == "spatial_backend_unavailable"
        assert result["district_id"] is None


def test_resolve_jurisdiction_dev_fallback_matches_nearest_seeded_district(db):
    """No PostGIS, but a real seeded district close to the point - the dev
    fallback resolves it, and says plainly that it did.
    """
    district = District(name="Kathmandu", province="Bagmati", code="D-KTM-FB")
    db.session.add(district)
    db.session.commit()

    with patch("app.services.geolocation_service.spatial_backend_available", return_value=False):
        result = resolve_jurisdiction(27.7172, 85.3240)

        assert result["resolved"] is True
        assert result["reason"] == "dev_nearest_centroid_fallback"
        assert result["district_id"] == district.id


def test_resolve_jurisdiction_dev_fallback_refuses_a_point_far_from_any_seeded_district(db):
    """A point nowhere near Nepal must not be force-matched to the nearest
    seeded district just because the fallback is active.
    """
    district = District(name="Kathmandu", province="Bagmati", code="D-KTM-FB2")
    db.session.add(district)
    db.session.commit()

    with patch("app.services.geolocation_service.spatial_backend_available", return_value=False):
        # London - genuinely nowhere near any Nepali district reference point.
        result = resolve_jurisdiction(51.5072, -0.1276)

        assert result["resolved"] is False
        assert result["reason"] == "spatial_backend_unavailable"


def test_resolve_jurisdiction_no_boundary_data():
    """When no boundary data, returns unresolved."""
    with patch("app.services.geolocation_service.spatial_backend_available", return_value=True):
        with patch("app.services.geolocation_service._boundary_count", return_value=0):
            result = resolve_jurisdiction(27.7172, 85.3240)

            assert result["resolved"] is False
            assert result["reason"] == "no_boundary_data"


def test_resolve_jurisdiction_outside_coverage():
    """When point outside all boundaries, returns unresolved."""
    with patch("app.services.geolocation_service.spatial_backend_available", return_value=True):
        with patch("app.services.geolocation_service._boundary_count", return_value=10):
            with patch("app.services.geolocation_service.reverse_geocode") as mock_reverse:
                mock_reverse.return_value = {
                    "resolved": False,
                    "reason": "outside_known_boundaries",
                    "district": None,
                    "municipality": None,
                    "province": None,
                }
                result = resolve_jurisdiction(0.0, 0.0)

                assert result["resolved"] is False
                assert result["reason"] == "outside_known_boundaries"


# --- find_authorities_for_jurisdiction tests ----------------------------------


def _create_district(db, name="Test District", code="TD-01"):
    """Helper to create a district and return its id."""
    district = District(name=name, code=code)
    db.session.add(district)
    db.session.flush()
    db.session.commit()
    return district


def _create_authority(db, name, level, type, district_id=None):
    """Helper to create an authority and return it."""
    # Ensure district exists if provided
    if district_id is not None:
        from app.models import District
        district = db.session.get(District, district_id)
        if not district:
            district = District(id=district_id, name=f"District {district_id}", code="AUTO")
            db.session.add(district)
            db.session.commit()

    auth = Authority(
        name=name,
        level=level,
        type=type,
        district_id=district_id,
    )
    db.session.add(auth)
    db.session.flush()
    db.session.commit()
    return auth


def test_find_authorities_district_only(db):
    """Authorities for specific district."""
    district = _create_district(db, "Test District", "TD-01")
    db.session.commit()

    _create_authority(db, "Local Roads Office", GovernmentLevel.LOCAL, AuthorityType.MUNICIPAL_OFFICE, district.id)
    _create_authority(db, "National Roads Dept", GovernmentLevel.FEDERAL, AuthorityType.DEPARTMENT_OF_ROADS, None)
    # Create a separate district for the third authority
    other_district = _create_district(db, "Other District", "OD-01")
    db.session.commit()
    _create_authority(db, "Other District Office", GovernmentLevel.LOCAL, AuthorityType.MUNICIPAL_OFFICE, other_district.id)
    db.session.commit()

    # Should find district authority + national authority
    authorities = find_authorities_for_jurisdiction(district.id)
    assert len(authorities) == 2
    names = {a.name for a in authorities}
    assert "Local Roads Office" in names
    assert "National Roads Dept" in names


def test_find_authorities_national_only(db):
    """When no district, only national authorities."""
    _create_authority(db, "National Roads Dept", GovernmentLevel.FEDERAL, AuthorityType.DEPARTMENT_OF_ROADS, None)
    _create_authority(db, "National Water Board", GovernmentLevel.FEDERAL, AuthorityType.WATER_AUTHORITY, None)
    # Create a district for the third authority
    other_district = _create_district(db, "Other District", "OD-01")
    db.session.commit()
    _create_authority(db, "District Office", GovernmentLevel.LOCAL, AuthorityType.MUNICIPAL_OFFICE, other_district.id)
    db.session.commit()

    authorities = find_authorities_for_jurisdiction(None)
    assert len(authorities) == 2
    names = {a.name for a in authorities}
    assert "National Roads Dept" in names
    assert "National Water Board" in names


# --- find_authority_users_for_jurisdiction tests ------------------------------


def _make_authority_user(db, district_id, email="authority@test.np"):
    """Helper to create an authority user with district."""
    role = db.session.query(Role).filter(Role.name == "authority").first()
    if not role:
        pytest.skip("Authority role not seeded")

    # Ensure the district exists
    from app.models import District
    district = db.session.get(District, district_id)
    if not district:
        district = District(id=district_id, name=f"Test District {district_id}", code="TD-01")
        db.session.add(district)
        db.session.commit()

    user = User(
        email=email,
        password_hash=hash_password("password"),
        full_name="Test Authority",
        is_active=True,
        temporary_district_id=district_id,
    )
    user.roles.append(role)
    db.session.add(user)
    db.session.flush()
    db.session.commit()
    return user


def test_find_authority_users_for_jurisdiction(db, make_user, roles):
    """Find authority users for district."""
    district_id = uuid.uuid4()
    # Create district first
    from app.models import District
    district = District(id=district_id, name="Test District", code="TD-01")
    db.session.add(district)
    db.session.commit()

    auth = Authority(
        name="Test Authority",
        level=GovernmentLevel.LOCAL,
        type=AuthorityType.MUNICIPAL_OFFICE,
        district_id=district_id,
    )
    db.session.add(auth)
    db.session.commit()

    # Create authority user in same district
    user = _make_authority_user(db, district_id, email="authority@test.np")
    db.session.commit()

    # Create another authority user in different district
    other_district = uuid.uuid4()
    other_district_obj = District(id=other_district, name="Other District", code="OD-01")
    db.session.add(other_district_obj)
    db.session.commit()
    _make_authority_user(db, other_district, email="other@test.np")
    db.session.commit()

    result = find_authority_users_for_jurisdiction(district_id)
    assert len(result) == 1
    authority, users = result[0]
    assert len(users) == 1
    assert users[0].id == user.id
    assert authority.id == auth.id


def test_find_authority_users_national_authority(db, make_user, roles):
    """National authority users included."""
    district_id = uuid.uuid4()
    auth = Authority(
        name="National Roads",
        level=GovernmentLevel.FEDERAL,
        type=AuthorityType.DEPARTMENT_OF_ROADS,
        district_id=None,  # National
    )
    db.session.add(auth)
    db.session.commit()

    user = _make_authority_user(db, district_id=uuid.uuid4(), email="national@test.np")
    db.session.commit()

    result = find_authority_users_for_jurisdiction(district_id)
    assert len(result) == 1
    authority, users = result[0]
    assert len(users) == 1
    assert users[0].id == user.id
    assert authority.id == auth.id


def test_find_authority_users_no_role(db):
    """When no authority role exists, returns empty."""
    district_id = uuid.uuid4()
    # Create district first
    from app.models import District
    district = District(id=district_id, name="Test District", code="TD-01")
    db.session.add(district)
    db.session.commit()

    auth = Authority(
        name="Test Authority",
        level=GovernmentLevel.LOCAL,
        type=AuthorityType.MUNICIPAL_OFFICE,
        district_id=district_id,
    )
    db.session.add(auth)
    db.session.commit()

    result = find_authority_users_for_jurisdiction(district_id)
    assert len(result) == 1
    authority, users = result[0]
    assert len(users) == 0