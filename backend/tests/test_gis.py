"""Phase 4: geographic foundation.

Database-independent tests. Everything here runs on SQLite; nothing here
asserts that a spatial query works. Real PostGIS behaviour lives in
``tests/test_postgis.py``, which skips when PostGIS is absent.

All polygons in this file are SYNTHETIC test geometry - small squares around
(0, 0) and (10, 10). They are not Nepal, are never imported into a real
database, and must never be treated as production boundary data.
"""
import json
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.gis.datasets import (
    DatasetError,
    parse_boundary_featurecollection,
    parse_reference_dataset,
    validate_district_reference,
    validate_municipality_reference,
)
from app.gis.location import (
    Coordinates,
    InvalidCoordinate,
    coordinates_from_geojson,
    point_to_geojson,
    validate_latitude,
    validate_longitude,
)
from app.models import District, Municipality
from app.models.provenance import (
    VERIFICATION_OFFICIAL_SOURCE,
    VERIFICATION_REFERENCE_ONLY,
    VERIFICATION_UNVERIFIED,
)

REFERENCE_FILE = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "data"
    / "districts"
    / "nepal_districts_reference.json"
)

# --- SYNTHETIC TEST GEOMETRY - NOT NEPAL ----------------------------------
SYNTHETIC_SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0]]],
}


# --- coordinate validation -------------------------------------------------


@pytest.mark.parametrize("value", [0, 27.7, -90, 90, "27.7", 89.999999])
def test_valid_latitude_is_accepted(value):
    assert validate_latitude(value) == float(value)


@pytest.mark.parametrize("value", [90.1, -90.1, 91, -1000, "abc", None, "", [], {}, True])
def test_invalid_latitude_is_rejected(value):
    with pytest.raises(InvalidCoordinate):
        validate_latitude(value)


@pytest.mark.parametrize("value", [0, 85.3, -180, 180, "85.3", -179.999])
def test_valid_longitude_is_accepted(value):
    assert validate_longitude(value) == float(value)


@pytest.mark.parametrize("value", [180.1, -180.1, 360, "xyz", None, "", [], {}, True])
def test_invalid_longitude_is_rejected(value):
    with pytest.raises(InvalidCoordinate):
        validate_longitude(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_coordinates_are_rejected(value):
    """NaN and infinity survive float() and would poison a spatial query."""
    with pytest.raises(InvalidCoordinate):
        validate_latitude(value)
    with pytest.raises(InvalidCoordinate):
        validate_longitude(value)


def test_latitude_range_is_narrower_than_longitude():
    """A longitude of 100 is valid; a latitude of 100 is not."""
    assert validate_longitude(100) == 100.0
    with pytest.raises(InvalidCoordinate):
        validate_latitude(100)


# --- GeoJSON ---------------------------------------------------------------


def test_geojson_point_uses_longitude_latitude_order():
    """The single most common GIS bug. Kathmandu is lat 27.7, lng 85.3."""
    point = point_to_geojson(latitude=27.7, longitude=85.3)
    assert point == {"type": "Point", "coordinates": [85.3, 27.7]}
    assert point["coordinates"][0] == 85.3  # longitude first
    assert point["coordinates"][1] == 27.7  # latitude second


def test_coordinates_object_keeps_human_order_on_construction():
    coordinates = Coordinates(latitude=27.7, longitude=85.3)
    assert coordinates.latitude == 27.7
    assert coordinates.longitude == 85.3
    assert coordinates.to_geojson()["coordinates"] == [85.3, 27.7]


def test_coordinates_are_immutable():
    coordinates = Coordinates(latitude=27.7, longitude=85.3)
    with pytest.raises(Exception):
        coordinates.latitude = 0.0


def test_coordinates_validate_on_construction():
    with pytest.raises(InvalidCoordinate):
        Coordinates(latitude=91.0, longitude=85.3)


def test_range_checks_alone_do_not_catch_a_swap_inside_nepal():
    """An honest limit, asserted so nobody assumes more safety than exists.

    Kathmandu is lat 27.7 / lng 85.3. Swapped, that is lat 85.3 / lng 27.7 -
    both still in range, so validation accepts it and the point lands in the
    Arctic Ocean. Range checks cannot catch this; only naming the arguments
    can, which is why Coordinates takes keywords and never a bare pair.
    """
    swapped = Coordinates(latitude=85.3, longitude=27.7)
    assert swapped.to_geojson()["coordinates"] == [27.7, 85.3]

    # A swap only raises when it pushes latitude past 90.
    with pytest.raises(InvalidCoordinate):
        Coordinates(latitude=185.3, longitude=27.7)


def test_wkt_uses_longitude_first():
    assert Coordinates(latitude=27.7, longitude=85.3).to_wkt() == "POINT(85.3 27.7)"


def test_as_dict_is_keyed_so_order_cannot_be_misread():
    assert Coordinates(latitude=27.7, longitude=85.3).as_dict() == {
        "latitude": 27.7,
        "longitude": 85.3,
    }


def test_geojson_round_trip():
    original = Coordinates(latitude=27.7, longitude=85.3)
    assert coordinates_from_geojson(original.to_geojson()) == original


@pytest.mark.parametrize(
    "geometry",
    [
        None,
        "Point",
        {"type": "Polygon", "coordinates": [[[0, 0]]]},
        {"type": "Point"},
        {"type": "Point", "coordinates": []},
        {"type": "Point", "coordinates": [1]},
        {"type": "Point", "coordinates": [200, 100]},
    ],
)
def test_invalid_geojson_point_is_rejected(geometry):
    with pytest.raises(InvalidCoordinate):
        coordinates_from_geojson(geometry)


# --- district model --------------------------------------------------------


def test_district_can_be_created(db):
    district = District(name="Testland", province="TestProvince")
    db.session.add(district)
    db.session.commit()
    assert isinstance(district.id, uuid.UUID)
    assert district.verification_status == VERIFICATION_UNVERIFIED


def test_district_name_must_be_unique(db):
    db.session.add(District(name="Testland"))
    db.session.commit()
    db.session.add(District(name="Testland"))
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_duplicate_district_code_is_rejected(db):
    db.session.add(District(name="One", code="D-01"))
    db.session.commit()
    db.session.add(District(name="Two", code="D-01"))
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_district_code_may_be_null_for_many_rows(db):
    """Official codes are unknown, so NULL must not collide with NULL."""
    db.session.add_all([District(name="One"), District(name="Two"), District(name="Three")])
    db.session.commit()
    assert db.session.scalar(sa.select(sa.func.count()).select_from(District)) == 3


def test_district_rejects_an_unknown_verification_status(db):
    db.session.add(District(name="Testland", verification_status="totally_legit"))
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_district_to_dict_shape(db):
    district = District(name="Testland", province="TestProvince", code="D-01")
    db.session.add(district)
    db.session.commit()

    payload = district.to_dict()
    assert set(payload) == {
        "id", "name", "name_ne", "name_mai", "province", "code", "headquarters",
        "latitude", "longitude", "geometry", "provenance",
    }
    assert payload["geometry"] is None
    assert payload["name_mai"] is None
    assert payload["headquarters"] is None
    assert payload["latitude"] is None
    assert payload["longitude"] is None
    assert set(payload["provenance"]) == {
        "source", "source_url", "source_type", "verification_status", "last_verified_at",
    }


# --- municipality model ----------------------------------------------------


@pytest.fixture
def district(db):
    record = District(name="Testland", province="TestProvince")
    db.session.add(record)
    db.session.commit()
    return record


def test_municipality_belongs_to_a_district(db, district):
    municipality = Municipality(district_id=district.id, name="Testville")
    db.session.add(municipality)
    db.session.commit()
    assert municipality.district is district
    assert municipality in district.municipalities


def test_municipality_requires_a_district(db):
    db.session.add(Municipality(name="Orphan"))
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_municipality_cannot_reference_a_nonexistent_district(db):
    db.session.add(Municipality(district_id=uuid.uuid4(), name="Ghostville"))
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_duplicate_municipality_code_is_rejected(db, district):
    db.session.add(Municipality(district_id=district.id, name="A", code="M-01"))
    db.session.commit()
    db.session.add(Municipality(district_id=district.id, name="B", code="M-01"))
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_municipality_name_is_unique_within_a_district(db, district):
    db.session.add(Municipality(district_id=district.id, name="Testville"))
    db.session.commit()
    db.session.add(Municipality(district_id=district.id, name="Testville"))
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_same_municipality_name_may_repeat_across_districts(db, district):
    """Municipality names are not unique nationally in Nepal."""
    other = District(name="Otherland")
    db.session.add(other)
    db.session.commit()

    db.session.add_all(
        [
            Municipality(district_id=district.id, name="Shared Name"),
            Municipality(district_id=other.id, name="Shared Name"),
        ]
    )
    db.session.commit()
    assert db.session.scalar(sa.select(sa.func.count()).select_from(Municipality)) == 2


def test_municipality_type_is_constrained(db, district):
    db.session.add(
        Municipality(district_id=district.id, name="Testville", municipality_type="city")
    )
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


@pytest.mark.parametrize(
    "value",
    ["metropolitan", "sub_metropolitan", "municipality", "rural_municipality"],
)
def test_valid_municipality_types_are_accepted(db, district, value):
    db.session.add(
        Municipality(district_id=district.id, name=f"T-{value}", municipality_type=value)
    )
    db.session.commit()


def test_deleting_a_district_with_municipalities_is_refused(db, district):
    """RESTRICT: local-level records are never silently destroyed."""
    db.session.add(Municipality(district_id=district.id, name="Testville"))
    db.session.commit()

    db.session.delete(district)
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


# --- bundled reference dataset ---------------------------------------------


def test_bundled_reference_dataset_is_valid():
    payload = json.loads(REFERENCE_FILE.read_text(encoding="utf-8"))
    meta, raw = parse_reference_dataset(payload, entity="districts")
    records = validate_district_reference(raw)
    assert len(records) == 77


def test_bundled_dataset_claims_no_geometry_and_no_codes():
    """It is a name list. It must not present itself as a spatial dataset."""
    meta = json.loads(REFERENCE_FILE.read_text(encoding="utf-8"))["_meta"]
    assert meta["contains_geometry"] is False
    assert meta["contains_official_codes"] is False
    assert meta["verification_status"] == VERIFICATION_REFERENCE_ONLY


def test_bundled_dataset_has_seven_provinces_totalling_77():
    payload = json.loads(REFERENCE_FILE.read_text(encoding="utf-8"))
    counts = payload["_meta"]["province_counts"]
    assert len(counts) == 7
    assert sum(counts.values()) == 77
    assert len({d["name"] for d in payload["districts"]}) == 77


def test_bundled_dataset_carries_no_invented_codes():
    payload = json.loads(REFERENCE_FILE.read_text(encoding="utf-8"))
    assert all(district["code"] is None for district in payload["districts"])


def test_importing_the_bundled_dataset_is_idempotent(db):
    from app.seed.districts import import_district_reference

    first = import_district_reference()
    second = import_district_reference()

    assert first["created"] == 77
    assert second["created"] == 0
    assert db.session.scalar(sa.select(sa.func.count()).select_from(District)) == 77


def test_imported_districts_are_marked_reference_only(db):
    from app.seed.districts import import_district_reference

    import_district_reference()
    district = db.session.scalar(sa.select(District).where(District.name == "Kathmandu"))
    assert district.province == "Bagmati"
    assert district.verification_status == VERIFICATION_REFERENCE_ONLY
    assert district.code == "D-KTM"
    assert district.boundary is None
    assert district.last_verified_at is None


# --- dataset validation ----------------------------------------------------


def test_reference_dataset_requires_meta():
    with pytest.raises(DatasetError):
        parse_reference_dataset({"districts": [{"name": "X"}]}, entity="districts")


def test_reference_dataset_cannot_claim_official_without_a_source_url():
    payload = {
        "_meta": {
            "source": "somewhere",
            "verification_status": VERIFICATION_OFFICIAL_SOURCE,
        },
        "districts": [{"name": "X"}],
    }
    with pytest.raises(DatasetError, match="source_url"):
        parse_reference_dataset(payload, entity="districts")


def test_reference_dataset_with_geometry_flag_is_refused():
    payload = {
        "_meta": {
            "source": "s",
            "verification_status": VERIFICATION_REFERENCE_ONLY,
            "contains_geometry": True,
        },
        "districts": [{"name": "X"}],
    }
    with pytest.raises(DatasetError, match="boundary importer"):
        parse_reference_dataset(payload, entity="districts")


def test_district_records_reject_duplicates_and_missing_names():
    with pytest.raises(DatasetError) as exc:
        validate_district_reference([{"name": "A"}, {"name": "a"}, {"province": "P"}])
    assert len(exc.value.errors) == 2


def test_municipality_records_require_a_parent_district():
    with pytest.raises(DatasetError, match="district"):
        validate_municipality_reference([{"name": "Orphanville"}])


def test_municipality_records_reject_duplicates_within_a_district():
    with pytest.raises(DatasetError):
        validate_municipality_reference(
            [{"name": "A", "district": "D"}, {"name": "a", "district": "d"}]
        )


_UNSET = object()


def _feature(name="A", code="C1", geometry=_UNSET, **properties):
    """Build a synthetic test Feature. `geometry=None` really means None."""
    return {
        "type": "Feature",
        "properties": {"name": name, "code": code, **properties},
        "geometry": SYNTHETIC_SQUARE if geometry is _UNSET else geometry,
    }


def test_boundary_featurecollection_accepts_a_valid_file():
    features = parse_boundary_featurecollection(
        {"type": "FeatureCollection", "features": [_feature()]}
    )
    assert features[0]["code"] == "C1"
    assert features[0]["geometry"]["type"] == "Polygon"


def test_boundary_file_must_be_a_featurecollection():
    with pytest.raises(DatasetError, match="FeatureCollection"):
        parse_boundary_featurecollection({"type": "Feature", "features": []})


def test_boundary_features_require_a_code():
    with pytest.raises(DatasetError, match="code"):
        parse_boundary_featurecollection(
            {"type": "FeatureCollection", "features": [_feature(code=None)]}
        )


def test_boundary_features_reject_duplicate_codes():
    with pytest.raises(DatasetError, match="duplicate"):
        parse_boundary_featurecollection(
            {"type": "FeatureCollection", "features": [_feature(), _feature(name="B")]}
        )


@pytest.mark.parametrize(
    "geometry",
    [
        {"type": "Point", "coordinates": [0, 0]},
        {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
        {"type": "Polygon"},
        None,
    ],
)
def test_boundary_features_reject_non_area_geometry(geometry):
    """A district is an area. A Point in that column means a broken source."""
    with pytest.raises(DatasetError):
        parse_boundary_featurecollection(
            {"type": "FeatureCollection", "features": [_feature(geometry=geometry)]}
        )


def test_boundary_file_rejects_a_non_wgs84_crs():
    """Silently accepting a projected CRS would place Nepal in the sea."""
    payload = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3857"}},
        "features": [_feature()],
    }
    with pytest.raises(DatasetError, match="EPSG:4326"):
        parse_boundary_featurecollection(payload)


def test_boundary_file_accepts_an_explicit_wgs84_crs():
    payload = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}},
        "features": [_feature()],
    }
    assert len(parse_boundary_featurecollection(payload)) == 1


def test_absent_crs_is_treated_as_wgs84():
    """RFC 7946 removed the crs member and mandates WGS84."""
    assert len(
        parse_boundary_featurecollection(
            {"type": "FeatureCollection", "features": [_feature()]}
        )
    ) == 1


def test_boundary_import_is_refused_without_postgis(db, tmp_path):
    """SQLite cannot store geometry, and the importer must say so."""
    from app.seed.districts import import_district_boundaries

    path = tmp_path / "boundaries.geojson"
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": [_feature()]}),
        encoding="utf-8",
    )
    with pytest.raises(DatasetError, match="PostGIS"):
        import_district_boundaries(path)


def test_missing_dataset_file_is_reported_clearly():
    from app.seed.districts import import_district_reference

    with pytest.raises(DatasetError, match="not found"):
        import_district_reference("does/not/exist.json")
