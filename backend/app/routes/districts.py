"""District endpoints.

Public reference data for Nepal's 77 districts. No authentication required.
"""
from __future__ import annotations

from flask import Blueprint, request
from sqlalchemy.orm import selectinload

from ..extensions import db
from ..models.district import District
from ..utils.helpers import success_response

districts_bp = Blueprint("districts", __name__, url_prefix="/districts")


def _eager_query():
    return db.select(District).options(
        selectinload(District.highways),
        selectinload(District.emergency_contacts),
        selectinload(District.corridors),
        selectinload(District.risk_profiles),
    )


@districts_bp.get("/all")
def list_all_districts():
    """Return all 77 districts with full details.

    Response includes: id, name, name_ne, name_mai, province, code,
    headquarters, latitude, longitude, provenance, highways, corridors,
    risk_profile, and emergency contacts (deoc/police/hospital - always all
    three kinds, "unverified"/null where no contact has been seeded yet).
    """
    districts = db.session.scalars(
        _eager_query().order_by(District.province, District.name)
    ).all()

    return success_response(
        {
            "count": len(districts),
            "districts": [d.to_full_dict() for d in districts],
        }
    )


@districts_bp.get("/search")
def search_districts():
    """Search districts by name (English, Nepali, or Maithili).

    Query: ``q`` (required, min 1 char)
    Optional: ``province`` to filter by province

    Returns matching districts with full details.
    """
    query = request.args.get("q", "").strip()
    province = request.args.get("province")

    if not query:
        return success_response({"count": 0, "districts": []})

    stmt = db.select(District)
    if province:
        stmt = stmt.where(District.province == province)

    # Search in name, name_ne, name_mai (case-insensitive)
    query_lower = f"%{query.lower()}%"
    stmt = stmt.where(
        db.or_(
            db.func.lower(District.name).like(query_lower),
            db.func.lower(District.name_ne).like(query_lower),
            db.func.lower(District.name_mai).like(query_lower),
        )
    )

    districts = db.session.scalars(stmt.order_by(District.name).limit(20)).all()

    return success_response(
        {
            "count": len(districts),
            "districts": [d.to_dict() for d in districts],
        }
    )


@districts_bp.get("/<district_id>")
def get_district(district_id: str):
    """Get a single district by ID, with highways, corridors, risk profile
    and emergency contacts."""
    import uuid

    from ..utils.helpers import ApiError

    try:
        parsed_id = uuid.UUID(district_id)
    except (ValueError, AttributeError, TypeError):
        raise ApiError("District not found", status=404, code="not_found") from None

    district = db.session.scalar(_eager_query().where(District.id == parsed_id))
    if not district:
        raise ApiError("District not found", status=404, code="not_found")

    return success_response({"district": district.to_full_dict()})


@districts_bp.get("/province/<province_name>")
def get_districts_by_province(province_name: str):
    """Get all districts in a province."""
    districts = db.session.scalars(
        db.select(District)
        .where(District.province == province_name)
        .order_by(District.name)
    ).all()

    return success_response(
        {
            "province": province_name,
            "count": len(districts),
            "districts": [d.to_dict() for d in districts],
        }
    )