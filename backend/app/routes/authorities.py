"""Authority endpoints.

Reading is public: knowing which office is responsible for a broken bridge, and
how to contact them, is exactly the information a civic platform exists to
publish. Creating and editing authorities is admin-only - this is the registry
the whole routing layer trusts, and a wrong or invented entry would send
citizens to an office that does not own the problem.
"""
from __future__ import annotations

from flask import Blueprint, request

from ..models.enums import ReportCategory, enum_values, parse_enum
from ..models.role import ROLE_ADMIN
from ..services import authority_service
from ..utils.decorators import require_roles
from ..utils.helpers import ApiError, success_response
from ..utils.validators import validate_authority_creation, validate_authority_update

authorities_bp = Blueprint("authorities", __name__, url_prefix="/authorities")

TRUTHY = {"1", "true", "yes", "on"}


@authorities_bp.post("")
@require_roles(ROLE_ADMIN)
def create_authority():
    """Register a government body or utility. Admin only."""
    data = validate_authority_creation(request.get_json(silent=True))
    authority = authority_service.create_authority(data)
    return success_response({"authority": authority.to_dict()}, status=201)


@authorities_bp.get("")
def list_authorities():
    """List authorities. Public.

    Filters: ``level``, ``type``, ``district_id``, ``search``, plus ``page``
    and ``per_page``. Pass ``include_national=true`` alongside ``district_id``
    to also return bodies with a national remit, which carry no district of
    their own but can still own a problem in one.
    """
    return success_response(
        authority_service.get_authorities(
            {
                "level": request.args.get("level"),
                "type": request.args.get("type"),
                "district_id": request.args.get("district_id"),
                "include_national": (
                    request.args.get("include_national", "").strip().lower() in TRUTHY
                ),
                "search": request.args.get("search"),
                "page": request.args.get("page"),
                "per_page": request.args.get("per_page"),
            }
        )
    )


@authorities_bp.get("/statistics")
def authority_statistics():
    """Aggregate counts. Public.

    Declared before ``/<authority_id>`` so the literal path is not swallowed by
    the identifier converter.
    """
    return success_response(authority_service.get_authority_statistics())


@authorities_bp.get("/suggestions")
def authority_suggestions():
    """Rank plausible authorities for a category and district. Public.

    A ranking aid for whoever routes an incident, not a decision: each result
    carries ``match_reasons`` so a human can see why it was offered and
    override it. Nothing is assigned by this endpoint.
    """
    raw_category = request.args.get("category")
    category = parse_enum(ReportCategory, raw_category)
    if category is None:
        raise ApiError(
            f"category must be one of: {', '.join(enum_values(ReportCategory))}.",
            status=400,
            code="invalid_filter",
        )

    district_id = request.args.get("district_id")
    parsed_district = None
    if district_id:
        parsed_district = authority_service._parse_uuid(district_id, "district_id")

    return success_response(
        {
            "category": category.value,
            "district_id": str(parsed_district) if parsed_district else None,
            "suggestions": authority_service.suggest_authorities(
                category, parsed_district
            ),
        }
    )


@authorities_bp.get("/<authority_id>")
def get_authority(authority_id: str):
    """Fetch one authority. Public."""
    authority = authority_service.get_authority_by_id(authority_id)
    return success_response({"authority": authority.to_dict(include_counts=True)})


@authorities_bp.patch("/<authority_id>")
@require_roles(ROLE_ADMIN)
def update_authority(authority_id: str):
    """Update an authority's details. Admin only."""
    data = validate_authority_update(request.get_json(silent=True))
    authority = authority_service.update_authority(authority_id, data)
    return success_response({"authority": authority.to_dict()})
