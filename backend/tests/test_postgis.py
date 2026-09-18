"""Phase 4: PostGIS integration tests.

These are the only tests that exercise real spatial behaviour. They need a live
PostgreSQL database with the PostGIS extension, so they are **skipped** unless
one is provided - never faked against SQLite, and never silently counted as
passing.

To run them::

    createdb betternepal_test
    psql -d betternepal_test -c "CREATE EXTENSION IF NOT EXISTS postgis;"
    set POSTGIS_TEST_DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/betternepal_test
    python -m pytest tests/test_postgis.py -v

The suite creates its own tables, runs, and drops them again, so it will not
disturb an existing schema in that database.

Every polygon below is SYNTHETIC: two one-degree squares near (0, 0) and
(10, 10). They are not Nepal boundaries and exist only to prove that
point-in-polygon lookup works.
"""
from __future__ import annotations

import json
import os

import pytest
import sqlalchemy as sa

POSTGIS_URL = os.environ.get("POSTGIS_TEST_DATABASE_URL", "").strip()

# --- SYNTHETIC TEST GEOMETRY - NOT NEPAL ----------------------------------
SQUARE_A = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0]]],
}
SQUARE_B = {
    "type": "Polygon",
    "coordinates": [
        [[10.0, 10.0], [10.0, 11.0], [11.0, 11.0], [11.0, 10.0], [10.0, 10.0]]
    ],
}
INSIDE_A = {"lat": 0.5, "lng": 0.5}
INSIDE_B = {"lat": 10.5, "lng": 10.5}
OUTSIDE_BOTH = {"lat": 50.0, "lng": 50.0}


def _postgis_reachable() -> tuple[bool, str]:
    if not POSTGIS_URL:
        return False, "POSTGIS_TEST_DATABASE_URL is not set"
    try:
        engine = sa.create_engine(POSTGIS_URL)
        with engine.connect() as connection:
            connection.execute(sa.text("SELECT PostGIS_version()"))
        engine.dispose()
        return True, ""
    except Exception as exc:  # pragma: no cover - environment dependent
        return False, f"PostGIS not reachable: {type(exc).__name__}: {exc}"


_AVAILABLE, _SKIP_REASON = _postgis_reachable()

# Applies to every test in this module.
pytestmark = pytest.mark.skipif(not _AVAILABLE, reason=_SKIP_REASON or "PostGIS unavailable")


@pytest.fixture(scope="module")
def pg_app():
    from app import create_app

    application = create_app("testing")
    application.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI=POSTGIS_URL,
        SQLALCHEMY_ENGINE_OPTIONS={},
    )
    return application


@pytest.fixture
def pg_db(pg_app):
    from app.extensions import db as _db

    with pg_app.app_context():
        _db.session.execute(sa.text("CREATE EXTENSION IF NOT EXISTS postgis"))
        _db.session.commit()
        _db.create_all()
        try:
            yield _db
        finally:
            _db.session.rollback()
            _db.session.remove()
            _db.drop_all()


@pytest.fixture
def synthetic_areas(pg_db):
    """Two synthetic districts, each with one synthetic municipality."""
    from app.models import District, Municipality
    from app.seed.districts import _set_boundary

    created = {}
    for key, name, geometry in (
        ("a", "SyntheticDistrictA", SQUARE_A),
        ("b", "SyntheticDistrictB", SQUARE_B),
    ):
        district = District(name=name, province="SyntheticProvince", code=f"SD-{key}")
        _set_boundary(district, geometry)
        pg_db.session.add(district)
        pg_db.session.flush()

        municipality = Municipality(
            district_id=district.id, name=f"SyntheticMuni{key.upper()}", code=f"SM-{key}"
        )
        _set_boundary(municipality, geometry)
        pg_db.session.add(municipality)
        created[key] = district

    pg_db.session.commit()
    return created


# --- schema ----------------------------------------------------------------


def test_geometry_columns_are_real_postgis_geometry(pg_db):
    rows = pg_db.session.execute(
        sa.text(
            "SELECT f_table_name, type, srid, coord_dimension "
            "FROM geometry_columns WHERE f_table_name IN ('districts','municipalities')"
        )
    ).all()
    found = {row[0]: row for row in rows}

    assert set(found) == {"districts", "municipalities"}
    for row in found.values():
        assert row[1] == "MULTIPOLYGON"
        assert row[2] == 4326
        assert row[3] == 2


def test_spatial_indexes_are_gist(pg_db):
    rows = pg_db.session.execute(
        sa.text(
            "SELECT i.relname, am.amname FROM pg_class t "
            "JOIN pg_index ix ON t.oid = ix.indrelid "
            "JOIN pg_class i ON i.oid = ix.indexrelid "
            "JOIN pg_am am ON i.relam = am.oid "
            "WHERE t.relname IN ('districts','municipalities') "
            "AND i.relname LIKE '%boundary%'"
        )
    ).all()
    index_types = {name: access_method for name, access_method in rows}

    assert index_types.get("ix_districts_boundary") == "gist"
    assert index_types.get("ix_municipalities_boundary") == "gist"


def test_service_detects_the_spatial_backend(pg_app, pg_db):
    from app.services.geolocation_service import spatial_backend_available

    assert spatial_backend_available() is True


# --- point in polygon ------------------------------------------------------


def test_reverse_geocode_resolves_a_point_inside_a_boundary(pg_app, synthetic_areas):
    from app.gis.location import Coordinates
    from app.services.geolocation_service import reverse_geocode

    result = reverse_geocode(Coordinates(INSIDE_A["lat"], INSIDE_A["lng"]))

    assert result["resolved"] is True
    assert result["reason"] is None
    assert result["district"]["name"] == "SyntheticDistrictA"
    assert result["municipality"]["name"] == "SyntheticMuniA"
    assert result["province"] == "SyntheticProvince"


def test_reverse_geocode_picks_the_correct_one_of_two_areas(pg_app, synthetic_areas):
    from app.gis.location import Coordinates
    from app.services.geolocation_service import reverse_geocode

    result = reverse_geocode(Coordinates(INSIDE_B["lat"], INSIDE_B["lng"]))
    assert result["district"]["name"] == "SyntheticDistrictB"


def test_reverse_geocode_outside_all_boundaries_is_unresolved(pg_app, synthetic_areas):
    from app.gis.location import Coordinates
    from app.services.geolocation_service import (
        REASON_OUTSIDE_COVERAGE,
        reverse_geocode,
    )

    result = reverse_geocode(Coordinates(OUTSIDE_BOTH["lat"], OUTSIDE_BOTH["lng"]))
    assert result["resolved"] is False
    assert result["reason"] == REASON_OUTSIDE_COVERAGE
    assert result["district"] is None


def test_swapped_coordinates_resolve_to_nothing(pg_app, synthetic_areas):
    """A lat/lng swap must not silently match the wrong polygon."""
    from app.gis.location import Coordinates
    from app.services.geolocation_service import reverse_geocode

    result = reverse_geocode(Coordinates(latitude=10.5, longitude=0.5))
    assert result["resolved"] is False


def test_reverse_geocode_with_no_boundary_data(pg_app, pg_db):
    from app.gis.location import Coordinates
    from app.services.geolocation_service import (
        REASON_NO_BOUNDARY_DATA,
        reverse_geocode,
    )

    result = reverse_geocode(Coordinates(0.5, 0.5))
    assert result["resolved"] is False
    assert result["reason"] == REASON_NO_BOUNDARY_DATA


# --- GeoJSON conversion ----------------------------------------------------


def test_stored_boundary_converts_back_to_geojson(pg_app, synthetic_areas):
    from app.services.geolocation_service import get_district

    district = get_district(str(synthetic_areas["a"].id), include_geometry=True)
    geometry = district["geometry"]

    assert geometry["type"] == "MultiPolygon"
    # ST_Multi promoted the input Polygon; the ring survives intact.
    ring = geometry["coordinates"][0][0]
    assert [0.0, 0.0] in [[round(x, 6), round(y, 6)] for x, y in ring]


def test_geometry_is_omitted_unless_requested(pg_app, synthetic_areas):
    from app.services.geolocation_service import list_districts

    without = list_districts(include_geometry=False)
    with_geometry = list_districts(include_geometry=True)

    assert all(d["geometry"] is None for d in without)
    assert all(d["geometry"] is not None for d in with_geometry)


def test_coverage_counts_boundaries(pg_app, synthetic_areas):
    from app.services.geolocation_service import coverage_summary

    summary = coverage_summary()
    assert summary["spatial_backend"] is True
    assert summary["districts"] == 2
    assert summary["districts_with_boundary"] == 2


# --- authoritative boundary import ----------------------------------------


def test_boundary_import_stores_real_geometry(pg_app, pg_db, tmp_path):
    from app.seed.districts import import_district_boundaries
    from app.services.geolocation_service import coverage_summary

    path = tmp_path / "synthetic_boundaries.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "_meta": {
                    "source": "SYNTHETIC TEST DATA - NOT NEPAL",
                    "source_url": "https://example.invalid/synthetic",
                },
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "name": "SyntheticImported",
                            "code": "SI-1",
                            "province": "SyntheticProvince",
                        },
                        "geometry": SQUARE_A,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    summary = import_district_boundaries(path)
    assert summary["created"] == 1
    assert coverage_summary()["districts_with_boundary"] == 1


def test_imported_boundary_is_queryable_by_point(pg_app, pg_db, tmp_path):
    from app.gis.location import Coordinates
    from app.seed.districts import import_district_boundaries
    from app.services.geolocation_service import reverse_geocode

    path = tmp_path / "synthetic_boundaries.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "SyntheticImported", "code": "SI-1"},
                        "geometry": SQUARE_A,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    import_district_boundaries(path)

    result = reverse_geocode(Coordinates(0.5, 0.5))
    assert result["resolved"] is True
    assert result["district"]["name"] == "SyntheticImported"
