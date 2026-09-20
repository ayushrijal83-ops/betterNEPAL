"""Static rivers/roads reference, extracted from the project's own research file.

Source: ``BETTER_NEPAL_Rivers_Roads_7_Provinces_77_Districts.docx``, parsed once
into ``app/data/districts/nepal_rivers_roads_reference.json``. This is a
major-river and major-road-corridor reference only - not live traffic, closure
or flood data, and it says so in every response that uses it. See the JSON
file's own ``_meta.notes`` for the source document's stated limitations.

Loaded once per process and cached: it is a small static file, not something
that benefits from a database round trip.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_FILE = (
    Path(__file__).resolve().parents[1] / "data" / "districts" / "nepal_rivers_roads_reference.json"
)

# A handful of well-known Nepali city names that are not themselves districts,
# mapped to the real district they sit in. Real geography, not a guess - e.g.
# Pokhara is the administrative seat of Kaski district. Kept short
# deliberately: anything not listed here falls back to matching the district
# name itself, so this never has to be exhaustive to stay honest.
CITY_TO_DISTRICT_ALIAS = {
    "pokhara": "Kaski",
    "lumbini": "Rupandehi",
    "janakpur": "Dhanusha",
    "nepalgunj": "Banke",
    "biratnagar": "Morang",
    "bharatpur": "Chitwan",
    "dharan": "Sunsari",
    "butwal": "Rupandehi",
    "dhangadhi": "Kailali",
    "birgunj": "Parsa",
}


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def resolve_district_name(query: str) -> str | None:
    """Match free-text input to a real district name.

    Tries, in order: an exact/case-insensitive district name, then the small
    known-city alias table, then a substring match. Returns ``None`` rather
    than guessing when nothing matches - a wrong destination is worse than no
    destination.
    """
    if not query or not query.strip():
        return None
    query_norm = query.strip().lower()

    rivers_by_district = _load()["rivers_by_district"]
    by_lower = {name.lower(): name for name in rivers_by_district}

    if query_norm in by_lower:
        return by_lower[query_norm]
    if query_norm in CITY_TO_DISTRICT_ALIAS:
        return CITY_TO_DISTRICT_ALIAS[query_norm]
    for lower_name, real_name in by_lower.items():
        if query_norm in lower_name or lower_name in query_norm:
            return real_name
    return None


def rivers_for_district(district_name: str) -> list[str]:
    return list(_load()["rivers_by_district"].get(district_name, []))


def road_corridors_for_district(district_name: str) -> list[dict[str, str]]:
    """National corridors whose own description names this district or its
    known city alias. Text-matched against the source document's own wording
    only - never inferred routing.
    """
    data = _load()
    names_to_match = {district_name.lower()}
    for city, district in CITY_TO_DISTRICT_ALIAS.items():
        if district == district_name:
            names_to_match.add(city)

    matches = []
    for corridor in data["road_corridors"]:
        haystack = (corridor["name"] + " " + corridor["description"]).lower()
        if any(name in haystack for name in names_to_match):
            matches.append(corridor)
    return matches


def province_road_authority(province: str | None) -> str | None:
    if not province:
        return None
    return _load()["province_road_authority"].get(province)


def dataset_meta() -> dict[str, Any]:
    return dict(_load()["_meta"])
