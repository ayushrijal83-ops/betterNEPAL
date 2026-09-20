"""Validation of incoming geographic datasets.

Pure parsing and validation - no database access, no Flask. The importer in
``app/seed/`` feeds files through here and only ever writes rows that came back
validated.

Two input shapes are supported, because the project has two very different
kinds of source material:

**Reference datasets** (``parse_reference_dataset``) carry names and hierarchy
only - the sort of thing transcribed from a research document. No geometry, no
official codes, and they are never allowed to claim ``official_source``.

**Boundary datasets** (``parse_boundary_featurecollection``) are authoritative
GeoJSON FeatureCollections from a government survey/statistics body, carrying
real polygons and official codes.

Keeping them apart is the point: it is what stops a name list from being
quietly promoted into a spatial layer.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from ..models.provenance import (
    SOURCE_TYPE_OFFICIAL_DATASET,
    VERIFICATION_OFFICIAL_SOURCE,
    VERIFICATION_REFERENCE_ONLY,
    VERIFICATION_STATUSES,
)
from .location import DEFAULT_SRID

# The only geometry types accepted for an administrative boundary. A district
# is an area; a Point or LineString in that column means the source file is
# wrong, not that we should coerce it.
BOUNDARY_GEOMETRY_TYPES = frozenset({"Polygon", "MultiPolygon"})

MAX_NAME_LENGTH = 120
MAX_CODE_LENGTH = 16


class DatasetError(ValueError):
    """Raised when a source dataset is malformed or internally inconsistent.

    Carries every problem found rather than only the first, so a bad import
    file can be fixed in one pass.
    """

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.errors = errors or []

    def __str__(self) -> str:
        if not self.errors:
            return self.message
        shown = "; ".join(self.errors[:10])
        more = f" (+{len(self.errors) - 10} more)" if len(self.errors) > 10 else ""
        return f"{self.message}: {shown}{more}"


def load_json_file(path: str | Path) -> Any:
    """Read a JSON/GeoJSON file, failing clearly rather than with a traceback."""
    file_path = Path(path)
    if not file_path.is_file():
        raise DatasetError(f"Dataset file not found: {file_path}")
    try:
        with file_path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{file_path} is not valid JSON: {exc}") from None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


# --- reference datasets ----------------------------------------------------


def parse_reference_dataset(payload: Any, *, entity: str) -> tuple[dict, list[dict]]:
    """Validate a name/hierarchy reference dataset.

    Returns ``(meta, records)``. ``entity`` is ``"districts"`` or
    ``"municipalities"`` and names the list key in the file.
    """
    if not isinstance(payload, dict):
        raise DatasetError("Reference dataset must be a JSON object.")

    meta = payload.get("_meta")
    if not isinstance(meta, dict):
        raise DatasetError("Reference dataset is missing its '_meta' block.")

    records = payload.get(entity)
    if not isinstance(records, list) or not records:
        raise DatasetError(f"Reference dataset has no '{entity}' records.")

    errors: list[str] = []

    if meta.get("contains_geometry"):
        errors.append(
            "_meta.contains_geometry is true; use the boundary importer for spatial data"
        )

    status = meta.get("verification_status")
    if status not in VERIFICATION_STATUSES:
        errors.append(f"_meta.verification_status {status!r} is not a known status")
    elif status == VERIFICATION_OFFICIAL_SOURCE and not meta.get("source_url"):
        # Refuse to record an official claim that cannot be traced back.
        errors.append(
            "_meta.verification_status is 'official_source' but no source_url is given"
        )

    if not _text(meta.get("source")):
        errors.append("_meta.source is required")

    if errors:
        raise DatasetError("Reference dataset metadata is invalid", errors)

    return meta, records


def validate_district_reference(records: Iterable[Any]) -> list[dict]:
    """Validate district reference records: name required, province optional."""
    cleaned: list[dict] = []
    errors: list[str] = []
    seen_names: set[str] = set()
    seen_codes: set[str] = set()

    for index, record in enumerate(records):
        label = f"districts[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{label} is not an object")
            continue

        # Support both "name" (legacy) and "name_en" (enhanced dataset)
        name = _text(record.get("name") or record.get("name_en"))
        if not name:
            errors.append(f"{label} is missing 'name' or 'name_en'")
            continue
        if len(name) > MAX_NAME_LENGTH:
            errors.append(f"{label} name exceeds {MAX_NAME_LENGTH} characters")
            continue

        key = name.casefold()
        if key in seen_names:
            errors.append(f"{label} duplicates district name {name!r}")
            continue
        seen_names.add(key)

        code = _text(record.get("code"))
        if code:
            if len(code) > MAX_CODE_LENGTH:
                errors.append(f"{label} code exceeds {MAX_CODE_LENGTH} characters")
                continue
            code_key = code.casefold()
            if code_key in seen_codes:
                errors.append(f"{label} duplicates code {code!r}")
                continue
            seen_codes.add(code_key)

        latitude = record.get("latitude")
        longitude = record.get("longitude")
        if latitude is not None:
            try:
                latitude = float(latitude)
                if not (-90 <= latitude <= 90):
                    errors.append(f"{label} latitude out of range")
                    continue
            except (TypeError, ValueError):
                errors.append(f"{label} latitude must be a number")
                continue
        if longitude is not None:
            try:
                longitude = float(longitude)
                if not (-180 <= longitude <= 180):
                    errors.append(f"{label} longitude out of range")
                    continue
            except (TypeError, ValueError):
                errors.append(f"{label} longitude must be a number")
                continue

        cleaned.append(
            {
                "name": name,
                "name_ne": _text(record.get("name_ne")),
                "name_mai": _text(record.get("name_mai")),
                "province": _text(record.get("province")),
                "code": code,
                "headquarters": _text(record.get("headquarters")),
                "latitude": latitude,
                "longitude": longitude,
            }
        )

    if errors:
        raise DatasetError("District reference records are invalid", errors)
    return cleaned


def validate_municipality_reference(records: Iterable[Any]) -> list[dict]:
    """Validate municipality reference records.

    ``district`` (the parent district's name) is required - a municipality with
    no parent is exactly the "silently reference a nonexistent district" case
    the schema forbids, and it should fail here rather than at the foreign key.
    """
    cleaned: list[dict] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()

    for index, record in enumerate(records):
        label = f"municipalities[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{label} is not an object")
            continue

        name = _text(record.get("name"))
        district = _text(record.get("district"))
        if not name:
            errors.append(f"{label} is missing 'name'")
            continue
        if not district:
            errors.append(f"{label} ({name}) is missing its parent 'district'")
            continue

        key = (district.casefold(), name.casefold())
        if key in seen:
            errors.append(f"{label} duplicates {name!r} within district {district!r}")
            continue
        seen.add(key)

        code = _text(record.get("code"))
        if code and len(code) > MAX_CODE_LENGTH:
            errors.append(f"{label} code exceeds {MAX_CODE_LENGTH} characters")
            continue

        cleaned.append(
            {
                "name": name,
                "name_ne": _text(record.get("name_ne")),
                "district": district,
                "municipality_type": _text(record.get("municipality_type")),
                "code": code,
            }
        )

    if errors:
        raise DatasetError("Municipality reference records are invalid", errors)
    return cleaned


# --- authoritative boundary datasets ---------------------------------------


def _validate_crs(payload: dict, errors: list[str]) -> None:
    """Check the CRS is WGS84.

    RFC 7946 removed the ``crs`` member and mandates EPSG:4326, so an absent
    ``crs`` is correct and means WGS84. A *present* one that names something
    else is a real problem: reprojecting is a job for the export tool, not for
    this importer, and silently accepting it would place Nepal in the sea.
    """
    crs = payload.get("crs")
    if crs is None:
        return
    name = ""
    if isinstance(crs, dict):
        name = str(crs.get("properties", {}).get("name", ""))
    if not name:
        errors.append("crs is present but unreadable")
        return
    upper = name.upper()
    if not (upper.endswith(f"::{DEFAULT_SRID}") or upper.endswith(f":{DEFAULT_SRID}")):
        errors.append(
            f"crs {name!r} is not EPSG:{DEFAULT_SRID}; reproject the source to WGS84"
        )


def parse_boundary_featurecollection(
    payload: Any,
    *,
    code_property: str = "code",
    name_property: str = "name",
) -> list[dict]:
    """Validate an authoritative GeoJSON FeatureCollection of boundaries.

    Requires: a real FeatureCollection, EPSG:4326, Polygon/MultiPolygon
    geometry, a name, and a unique non-empty official code on every feature.
    Codes are mandatory here because this is the dataset that is allowed to
    claim ``official_source``.
    """
    if not isinstance(payload, dict):
        raise DatasetError("Boundary dataset must be a GeoJSON object.")
    if payload.get("type") != "FeatureCollection":
        raise DatasetError("Boundary dataset must be a GeoJSON FeatureCollection.")

    features = payload.get("features")
    if not isinstance(features, list) or not features:
        raise DatasetError("Boundary dataset contains no features.")

    errors: list[str] = []
    _validate_crs(payload, errors)

    cleaned: list[dict] = []
    seen_codes: set[str] = set()

    for index, feature in enumerate(features):
        label = f"features[{index}]"
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            errors.append(f"{label} is not a GeoJSON Feature")
            continue

        properties = feature.get("properties")
        if not isinstance(properties, dict):
            errors.append(f"{label} has no properties object")
            continue

        name = _text(properties.get(name_property))
        if not name:
            errors.append(f"{label} is missing property {name_property!r}")
            continue

        code = _text(properties.get(code_property))
        if not code:
            errors.append(f"{label} ({name}) is missing property {code_property!r}")
            continue
        if len(code) > MAX_CODE_LENGTH:
            errors.append(f"{label} code {code!r} exceeds {MAX_CODE_LENGTH} characters")
            continue
        if code in seen_codes:
            errors.append(f"{label} duplicates code {code!r}")
            continue
        seen_codes.add(code)

        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            errors.append(f"{label} ({name}) has no geometry")
            continue
        geometry_type = geometry.get("type")
        if geometry_type not in BOUNDARY_GEOMETRY_TYPES:
            errors.append(
                f"{label} ({name}) geometry is {geometry_type!r}; "
                f"expected one of {sorted(BOUNDARY_GEOMETRY_TYPES)}"
            )
            continue
        if not isinstance(geometry.get("coordinates"), list) or not geometry["coordinates"]:
            errors.append(f"{label} ({name}) geometry has no coordinates")
            continue

        cleaned.append(
            {
                "name": name,
                "name_ne": _text(properties.get("name_ne")),
                "province": _text(properties.get("province")),
                "district": _text(properties.get("district")),
                "municipality_type": _text(properties.get("municipality_type")),
                "code": code,
                "geometry": geometry,
            }
        )

    if errors:
        raise DatasetError("Boundary dataset is invalid", errors)
    return cleaned


def boundary_provenance(meta: dict | None, source_name: str) -> dict:
    """Provenance for an authoritative boundary import."""
    meta = meta or {}
    return {
        "source": _text(meta.get("source")) or source_name,
        "source_url": _text(meta.get("source_url")),
        "source_type": SOURCE_TYPE_OFFICIAL_DATASET,
        "verification_status": VERIFICATION_OFFICIAL_SOURCE,
    }


def reference_provenance(meta: dict, source_name: str) -> dict:
    """Provenance for a reference import.

    Capped at ``reference_only`` regardless of what the file claims: a dataset
    cannot promote itself to authoritative.
    """
    return {
        "source": _text(meta.get("source")) or source_name,
        "source_url": _text(meta.get("source_url")),
        "source_type": _text(meta.get("source_type")) or "research_reference",
        "verification_status": VERIFICATION_REFERENCE_ONLY,
    }
