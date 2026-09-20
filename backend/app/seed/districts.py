"""Importing districts and municipalities from dataset files.

Two importers, matching the two kinds of source material (see
``app/gis/datasets.py``):

* :func:`import_district_reference` - names and province only, from the
  project's research files. Records land as ``reference_only``.
* :func:`import_district_boundaries` / :func:`import_municipality_boundaries` -
  authoritative GeoJSON with real polygons and official codes. PostGIS only.

Both are idempotent: rerunning updates existing rows instead of duplicating
them. Districts match on ``name`` (unique nationally), municipalities on
``(district, name)``.

Nothing here invents data. If a boundary file is absent, boundaries simply stay
NULL and the reverse-geocode endpoint keeps reporting that it has no data.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from ..extensions import db
from ..gis.datasets import (
    DatasetError,
    boundary_provenance,
    load_json_file,
    parse_boundary_featurecollection,
    parse_reference_dataset,
    reference_provenance,
    validate_district_reference,
    validate_municipality_reference,
)
from ..models.district import District
from ..models.municipality import MUNICIPALITY_TYPES, Municipality

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DISTRICT_REFERENCE_FILE = DATA_DIR / "districts" / "nepal_districts_enhanced.json"
DISTRICT_ENRICHMENT_FILE = DATA_DIR / "districts" / "nepal_district_enrichment.json"


def _apply_provenance(record, provenance: dict) -> None:
    record.source = provenance.get("source")
    record.source_url = provenance.get("source_url")
    record.source_type = provenance.get("source_type")
    record.verification_status = provenance["verification_status"]


def import_district_reference(path: str | Path | None = None) -> dict[str, int]:
    """Load district names and provinces. Never touches geometry or codes."""
    file_path = Path(path) if path else DISTRICT_REFERENCE_FILE
    meta, raw = parse_reference_dataset(load_json_file(file_path), entity="districts")
    records = validate_district_reference(raw)
    provenance = reference_provenance(meta, file_path.name)

    existing = {
        district.name: district for district in db.session.scalars(select(District)).all()
    }

    created = updated = 0
    for record in records:
        district = existing.get(record["name"])
        if district is None:
            district = District(name=record["name"])
            db.session.add(district)
            created += 1
        else:
            updated += 1

        district.name_ne = record["name_ne"] or district.name_ne
        district.name_mai = record["name_mai"] or district.name_mai
        district.province = record["province"] or district.province
        district.headquarters = record["headquarters"] or district.headquarters
        if record["latitude"] is not None:
            district.latitude = record["latitude"]
        if record["longitude"] is not None:
            district.longitude = record["longitude"]
        # A reference import must never overwrite an official code with NULL.
        if record["code"]:
            district.code = record["code"]
        _apply_provenance(district, provenance)

    db.session.commit()
    return {"created": created, "updated": updated, "total": len(records)}


def import_district_enrichment(path: str | Path | None = None) -> dict[str, int]:
    """Load the small, honestly-incomplete highway/emergency-contact/corridor
    dataset (``DISTRICT_ENRICHMENT_FILE`` by default).

    Idempotent per row: a highway is matched on ``(district, code)``, a
    contact on ``(district, kind)``, a corridor on ``(district, name)`` - a
    second run updates the same rows rather than duplicating them. Districts
    named in the file that do not exist yet are a hard error (nothing here
    guesses a parent district), matching ``import_municipality_reference``.
    """
    from ..models.district_extras import (
        DistrictCorridor,
        DistrictEmergencyContact,
        DistrictHighway,
    )

    file_path = Path(path) if path else DISTRICT_ENRICHMENT_FILE
    payload = load_json_file(file_path)
    meta = payload.get("_meta", {})
    default_provenance = {
        "source": meta.get("source"),
        "source_url": None,
        "source_type": meta.get("source_type"),
        "verification_status": meta.get("verification_status", "reference_only"),
    }

    districts = {
        d.name.casefold(): d for d in db.session.scalars(select(District)).all()
    }

    def _resolve(name: str) -> District:
        district = districts.get(name.casefold())
        if district is None:
            raise DatasetError(
                "Enrichment dataset references a district that is not in the database",
                [f"unknown district: {name}"],
            )
        return district

    created = updated = 0

    for row in payload.get("highways", []):
        district = _resolve(row["district"])
        existing = db.session.scalar(
            select(DistrictHighway).where(
                DistrictHighway.district_id == district.id,
                DistrictHighway.code == row["code"],
            )
        )
        if existing is None:
            existing = DistrictHighway(district_id=district.id, code=row["code"])
            db.session.add(existing)
            created += 1
        else:
            updated += 1
        existing.name = row["name"]
        _apply_provenance(existing, default_provenance)

    for row in payload.get("emergency_contacts", []):
        district = _resolve(row["district"])
        existing = db.session.scalar(
            select(DistrictEmergencyContact).where(
                DistrictEmergencyContact.district_id == district.id,
                DistrictEmergencyContact.kind == row["kind"],
            )
        )
        if existing is None:
            existing = DistrictEmergencyContact(district_id=district.id, kind=row["kind"])
            db.session.add(existing)
            created += 1
        else:
            updated += 1
        existing.name = row.get("name")
        existing.phone = row.get("phone")
        _apply_provenance(
            existing,
            {
                "source": row.get("source", default_provenance["source"]),
                "source_url": row.get("source_url"),
                "source_type": default_provenance["source_type"],
                "verification_status": row.get(
                    "verification_status", default_provenance["verification_status"]
                ),
            },
        )

    for row in payload.get("corridors", []):
        district = _resolve(row["district"])
        existing = db.session.scalar(
            select(DistrictCorridor).where(
                DistrictCorridor.district_id == district.id,
                DistrictCorridor.name == row["name"],
            )
        )
        if existing is None:
            existing = DistrictCorridor(district_id=district.id, name=row["name"])
            db.session.add(existing)
            created += 1
        else:
            updated += 1
        existing.description = row.get("description")
        _apply_provenance(existing, default_provenance)

    db.session.commit()
    total = (
        len(payload.get("highways", []))
        + len(payload.get("emergency_contacts", []))
        + len(payload.get("corridors", []))
    )
    return {"created": created, "updated": updated, "total": total}


def import_municipality_reference(path: str | Path) -> dict[str, int]:
    """Load municipality names under their parent districts.

    Fails if a record names a district that does not exist - the schema forbids
    an orphan municipality, and guessing a parent would be worse than stopping.
    """
    file_path = Path(path)
    meta, raw = parse_reference_dataset(
        load_json_file(file_path), entity="municipalities"
    )
    records = validate_municipality_reference(raw)
    provenance = reference_provenance(meta, file_path.name)

    districts = {
        district.name.casefold(): district
        for district in db.session.scalars(select(District)).all()
    }

    missing = sorted(
        {r["district"] for r in records if r["district"].casefold() not in districts}
    )
    if missing:
        raise DatasetError(
            "Dataset references districts that are not in the database",
            [f"unknown district: {name}" for name in missing],
        )

    bad_types = sorted(
        {
            r["municipality_type"]
            for r in records
            if r["municipality_type"] and r["municipality_type"] not in MUNICIPALITY_TYPES
        }
    )
    if bad_types:
        raise DatasetError(
            "Dataset contains unknown municipality types",
            [f"unknown type: {value}" for value in bad_types],
        )

    existing = {
        (municipality.district_id, municipality.name): municipality
        for municipality in db.session.scalars(select(Municipality)).all()
    }

    created = updated = 0
    for record in records:
        district = districts[record["district"].casefold()]
        municipality = existing.get((district.id, record["name"]))
        if municipality is None:
            municipality = Municipality(district_id=district.id, name=record["name"])
            db.session.add(municipality)
            created += 1
        else:
            updated += 1

        municipality.name_ne = record["name_ne"] or municipality.name_ne
        municipality.municipality_type = (
            record["municipality_type"] or municipality.municipality_type
        )
        if record["code"]:
            municipality.code = record["code"]
        _apply_provenance(municipality, provenance)

    db.session.commit()
    return {"created": created, "updated": updated, "total": len(records)}


# --- authoritative boundaries ----------------------------------------------


def _require_spatial_backend() -> None:
    from ..services.geolocation_service import spatial_backend_available

    if not spatial_backend_available():
        raise DatasetError(
            "Importing boundary geometry requires PostgreSQL with PostGIS. "
            "The current database cannot store geometry."
        )


def _set_boundary(record, geometry: dict[str, Any]) -> None:
    """Hand GeoJSON to PostGIS for conversion.

    ``ST_GeomFromGeoJSON`` parses it, ``ST_Multi`` normalises a Polygon into a
    MultiPolygon so the column type is always satisfied, and the SRID is
    stamped as 4326. Parameterised - no SQL is built by string concatenation.
    """
    import json

    record.boundary = func.ST_SetSRID(
        func.ST_Multi(func.ST_GeomFromGeoJSON(json.dumps(geometry))), 4326
    )


def import_district_boundaries(path: str | Path) -> dict[str, int]:
    """Import authoritative district boundaries from a GeoJSON FeatureCollection."""
    _require_spatial_backend()
    file_path = Path(path)
    payload = load_json_file(file_path)
    features = parse_boundary_featurecollection(payload)
    provenance = boundary_provenance(payload.get("_meta"), file_path.name)

    by_name = {d.name.casefold(): d for d in db.session.scalars(select(District)).all()}

    created = updated = 0
    for feature in features:
        district = by_name.get(feature["name"].casefold())
        if district is None:
            district = District(name=feature["name"])
            db.session.add(district)
            created += 1
        else:
            updated += 1

        district.code = feature["code"]
        district.province = feature["province"] or district.province
        district.name_ne = feature["name_ne"] or district.name_ne
        _set_boundary(district, feature["geometry"])
        _apply_provenance(district, provenance)

    db.session.commit()
    return {"created": created, "updated": updated, "total": len(features)}


def import_municipality_boundaries(path: str | Path) -> dict[str, int]:
    """Import authoritative municipality boundaries from a GeoJSON FeatureCollection."""
    _require_spatial_backend()
    file_path = Path(path)
    payload = load_json_file(file_path)
    features = parse_boundary_featurecollection(payload)
    provenance = boundary_provenance(payload.get("_meta"), file_path.name)

    districts = {d.name.casefold(): d for d in db.session.scalars(select(District)).all()}
    missing = sorted(
        {f["district"] for f in features if (f["district"] or "").casefold() not in districts}
    )
    if missing:
        raise DatasetError(
            "Boundary file references districts that are not in the database",
            [f"unknown district: {name}" for name in missing],
        )

    existing = {
        municipality.code: municipality
        for municipality in db.session.scalars(select(Municipality)).all()
        if municipality.code
    }

    created = updated = 0
    for feature in features:
        municipality = existing.get(feature["code"])
        district = districts[feature["district"].casefold()]
        if municipality is None:
            municipality = Municipality(
                district_id=district.id, name=feature["name"], code=feature["code"]
            )
            db.session.add(municipality)
            created += 1
        else:
            municipality.district_id = district.id
            municipality.name = feature["name"]
            updated += 1

        municipality.name_ne = feature["name_ne"] or municipality.name_ne
        municipality.municipality_type = (
            feature["municipality_type"] or municipality.municipality_type
        )
        _set_boundary(municipality, feature["geometry"])
        _apply_provenance(municipality, provenance)

    db.session.commit()
    return {"created": created, "updated": updated, "total": len(features)}
