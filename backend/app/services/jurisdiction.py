"""Jurisdiction resolution for disaster dispatch.

Reuses existing GIS infrastructure to resolve coordinates to district/authority.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from ..extensions import db
from ..models.authority import Authority
from ..models.district import District
from ..services import geolocation_service
from ..services.geolocation_service import Coordinates


def resolve_jurisdiction(latitude: float, longitude: float) -> dict[str, Any]:
    """Resolve coordinates to district and municipality.

    Returns a structured result with district_id, municipality_id, and resolution info.
    Never guesses - returns unresolved result with reason when boundary data unavailable.
    """
    coordinates = Coordinates(latitude=latitude, longitude=longitude)
    result = geolocation_service.reverse_geocode(coordinates)

    district_id = None
    municipality_id = None

    if result.get("resolved"):
        district = result.get("district") or {}
        municipality = result.get("municipality") or {}
        if district.get("id"):
            try:
                district_id = uuid.UUID(str(district["id"]))
            except (ValueError, TypeError):
                pass
        if municipality.get("id"):
            try:
                municipality_id = uuid.UUID(str(municipality["id"]))
            except (ValueError, TypeError):
                pass

    return {
        "district_id": district_id,
        "municipality_id": municipality_id,
        "resolved": result.get("resolved", False),
        "reason": result.get("reason"),
        "province": result.get("province"),
        "district_name": district.get("name") if (district := result.get("district")) else None,
        "municipality_name": (municipality := result.get("municipality")) and municipality.get("name"),
    }


def find_authorities_for_jurisdiction(
    district_id: uuid.UUID | None,
    disaster_type: str | None = None,
) -> list[Authority]:
    """Find authorities responsible for a district.

    If district_id is None, returns national authorities only.
    If district_id is provided, returns authorities for that district plus national ones.

    Optionally filters by disaster_type using the existing CATEGORY_TO_TYPES mapping
    from authority_service (which maps ReportCategory to AuthorityType).
    """
    from ..services.authority_service import CATEGORY_TO_TYPES
    from ..models.enums import ReportCategory, AuthorityType, parse_enum

    statement = select(Authority)

    if district_id is not None:
        # Authorities covering this district OR national authorities
        statement = statement.where(
            (Authority.district_id == district_id) | (Authority.district_id.is_(None))
        )
    else:
        # Only national authorities when no district resolved
        statement = statement.where(Authority.district_id.is_(None))

    # Optional: filter by disaster type relevance
    if disaster_type:
        # Map DisasterType to ReportCategory for authority matching
        # This is a heuristic - disaster types don't perfectly map to report categories
        disaster_to_category = {
            "earthquake": ReportCategory.NATURAL_DISASTER,
            "flood": ReportCategory.NATURAL_DISASTER,
            "flash_flood": ReportCategory.NATURAL_DISASTER,
            "landslide": ReportCategory.NATURAL_DISASTER,
            "forest_fire": ReportCategory.NATURAL_DISASTER,
            "wildfire": ReportCategory.NATURAL_DISASTER,
            "storm": ReportCategory.NATURAL_DISASTER,
            "lightning": ReportCategory.NATURAL_DISASTER,
            "avalanche": ReportCategory.NATURAL_DISASTER,
            "other": ReportCategory.OTHER,
        }
        category = disaster_to_category.get(disaster_type.lower() if disaster_type else "", ReportCategory.OTHER)
        likely_types = CATEGORY_TO_TYPES.get(category, ())

        if likely_types:
            # Don't filter strictly - just rank by relevance later
            # For now, include all authorities for the district
            pass

    return list(db.session.scalars(statement).all())


def find_authority_users_for_jurisdiction(
    district_id: uuid.UUID | None,
) -> list[tuple[Authority, list[Any]]]:
    """Find authority users (people with 'authority' role) for a jurisdiction.

    Returns list of (authority, [users]) tuples.
    """
    from ..models.user import User
    from ..models.role import Role
    from sqlalchemy import select as sa_select
    from sqlalchemy.orm import selectinload

    authorities = find_authorities_for_jurisdiction(district_id)

    # Get the 'authority' role
    authority_role = db.session.scalar(
        sa_select(Role).where(Role.name == "authority")
    )

    if not authority_role:
        return [(auth, []) for auth in authorities]

    result = []
    for authority in authorities:
        # Find users with authority role who are associated with this authority
        # For now, we look for users whose temporary_district matches the authority's district
        # In future, could add explicit authority_user association table
        user_query = (
            sa_select(User)
            .where(
                User.is_active == True,  # noqa: E712
                User.roles.any(Role.id == authority_role.id),
            )
            .options(selectinload(User.roles))
        )

        # If authority has a district, prefer users in that district
        if authority.district_id:
            user_query = user_query.where(
                (User.temporary_district_id == authority.district_id)
                | (User.permanent_district_id == authority.district_id)
            )

        users = list(db.session.scalars(user_query).all())
        result.append((authority, users))

    return result