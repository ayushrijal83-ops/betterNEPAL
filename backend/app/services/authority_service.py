"""Authority business logic.

Authorities are reference records about who owns what. This module manages
them; deciding which one an incident belongs to is a human act performed
through ``incident_service.assign_incident_to_authority``.

There is deliberately no automatic routing here. The project's research files
are explicit that a provincial fallback contact is not proof that the province
owns a particular asset, and that office structures change. Guessing an owner
from a category and a district would produce confident wrong answers and send
citizens to the wrong office. A human picks; the database records the pick and
who made it.

:func:`suggest_authorities` exists to make that human choice quick - it narrows
the list to plausible candidates and says why each one matched. It ranks; it
does not decide.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from ..extensions import db
from ..models.authority import Authority
from ..models.enums import AuthorityType, GovernmentLevel, ReportCategory, enum_values, parse_enum
from ..utils.helpers import ApiError

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 50

# Which authority types plausibly own which kind of problem. Used only to order
# candidates in front of a human - never to assign anything automatically.
CATEGORY_TO_TYPES: dict[ReportCategory, tuple[AuthorityType, ...]] = {
    ReportCategory.ROAD_DAMAGE: (
        AuthorityType.DEPARTMENT_OF_ROADS,
        AuthorityType.MUNICIPAL_OFFICE,
    ),
    ReportCategory.WATER_LEAK: (
        AuthorityType.WATER_AUTHORITY,
        AuthorityType.MUNICIPAL_OFFICE,
    ),
    ReportCategory.ELECTRICITY: (AuthorityType.ELECTRICITY_AUTHORITY,),
    ReportCategory.WASTE_MANAGEMENT: (AuthorityType.MUNICIPAL_OFFICE,),
    ReportCategory.PUBLIC_PROPERTY: (AuthorityType.MUNICIPAL_OFFICE,),
    ReportCategory.NATURAL_DISASTER: (
        AuthorityType.MUNICIPAL_OFFICE,
        AuthorityType.DEPARTMENT_OF_ROADS,
    ),
    ReportCategory.OTHER: (),
}

# Human-readable form of the same AuthorityType values used above and stored
# on Authority.type - not a new list, just how an existing enum member reads
# to a person instead of a person_snake_case token.
AUTHORITY_TYPE_LABELS: dict[AuthorityType, str] = {
    AuthorityType.DEPARTMENT_OF_ROADS: "Department of Roads",
    AuthorityType.MUNICIPAL_OFFICE: "Municipal Office",
    AuthorityType.WATER_AUTHORITY: "Water Authority",
    AuthorityType.ELECTRICITY_AUTHORITY: "Electricity Authority",
    AuthorityType.OTHER: "Other",
}


def likely_department_label(category: ReportCategory) -> str | None:
    """The most plausible responsible authority *type*, by name, for a report
    category - reusing CATEGORY_TO_TYPES, the same mapping suggest_authorities()
    already ranks candidates by. Advisory only, same as everything else in
    this module: never written as a real Authority assignment, only ever
    shown as a suggestion (see the module docstring above).
    """
    types = CATEGORY_TO_TYPES.get(category, ())
    if not types:
        return None
    return AUTHORITY_TYPE_LABELS.get(types[0])


def _parse_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise ApiError(
            f"{field} is not a valid identifier.", status=400, code="invalid_identifier"
        ) from None


def _base_query():
    return select(Authority).options(selectinload(Authority.district))


def create_authority(data: dict[str, Any]) -> Authority:
    """Create an authority from validated data.

    ``data`` is the output of ``validate_authority_creation``.
    """
    district_id = data.get("district_id")
    if district_id:
        from ..models.district import District

        parsed = _parse_uuid(district_id, "district_id")
        if db.session.get(District, parsed) is None:
            raise ApiError(
                "District not found.", status=404, code="district_not_found"
            )
        district_id = parsed
    else:
        district_id = None

    authority = Authority(
        name=data["name"],
        level=data["level"],
        type=data["type"],
        contact_email=data.get("contact_email"),
        contact_phone=data.get("contact_phone"),
        district_id=district_id,
    )
    db.session.add(authority)
    try:
        db.session.commit()
    except IntegrityError:
        # The unique index is the real guard; checking first would still race.
        db.session.rollback()
        raise ApiError(
            "An authority with this name already exists.",
            status=409,
            code="authority_name_taken",
        ) from None

    return get_authority_by_id(authority.id)


def get_authority_by_id(authority_id: Any) -> Authority:
    authority = db.session.scalar(
        _base_query().where(Authority.id == _parse_uuid(authority_id, "authority_id"))
    )
    if authority is None:
        raise ApiError("Authority not found.", status=404, code="authority_not_found")
    return authority


def get_authorities(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """List authorities with optional filtering and paging."""
    filters = filters or {}
    statement = _base_query()
    count_statement = select(func.count()).select_from(Authority)

    def _enum_filter(key: str, enum_class, column):
        nonlocal statement, count_statement
        raw = filters.get(key)
        if raw in (None, ""):
            return
        member = parse_enum(enum_class, raw)
        if member is None:
            raise ApiError(
                f"{key} must be one of: {', '.join(enum_values(enum_class))}.",
                status=400,
                code="invalid_filter",
            )
        statement = statement.where(column == member)
        count_statement = count_statement.where(column == member)

    _enum_filter("level", GovernmentLevel, Authority.level)
    _enum_filter("type", AuthorityType, Authority.type)

    district_id = filters.get("district_id")
    if district_id:
        parsed = _parse_uuid(district_id, "district_id")
        if filters.get("include_national"):
            # "Who could handle this district" legitimately includes bodies
            # with national remit, which carry no district of their own.
            statement = statement.where(
                or_(Authority.district_id == parsed, Authority.district_id.is_(None))
            )
            count_statement = count_statement.where(
                or_(Authority.district_id == parsed, Authority.district_id.is_(None))
            )
        else:
            statement = statement.where(Authority.district_id == parsed)
            count_statement = count_statement.where(Authority.district_id == parsed)

    search = (filters.get("search") or "").strip()
    if search:
        pattern = f"%{search}%"
        statement = statement.where(Authority.name.ilike(pattern))
        count_statement = count_statement.where(Authority.name.ilike(pattern))

    page = max(1, _as_int(filters.get("page"), 1))
    per_page = min(
        MAX_PAGE_SIZE, max(1, _as_int(filters.get("per_page"), DEFAULT_PAGE_SIZE))
    )

    total = db.session.scalar(count_statement) or 0
    records = db.session.scalars(
        statement.order_by(Authority.level, Authority.name)
        .limit(per_page)
        .offset((page - 1) * per_page)
    ).all()

    return {
        "authorities": [authority.to_dict() for authority in records],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page if total else 0,
        },
    }


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def update_authority(authority_id: Any, data: dict[str, Any]) -> Authority:
    """Update mutable fields. Only keys actually supplied are touched."""
    authority = get_authority_by_id(authority_id)

    for field in ("name", "level", "type"):
        if data.get(field) is not None:
            setattr(authority, field, data[field])

    # Contact details use a sentinel so an explicit null can clear a stale
    # number, which "if value" alone could never express.
    for field in ("contact_email", "contact_phone"):
        if field in data:
            setattr(authority, field, data[field])

    if "district_id" in data:
        district_id = data["district_id"]
        if district_id:
            from ..models.district import District

            parsed = _parse_uuid(district_id, "district_id")
            if db.session.get(District, parsed) is None:
                raise ApiError(
                    "District not found.", status=404, code="district_not_found"
                )
            authority.district_id = parsed
        else:
            authority.district_id = None

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError(
            "An authority with this name already exists.",
            status=409,
            code="authority_name_taken",
        ) from None

    return get_authority_by_id(authority.id)


def suggest_authorities(
    category: ReportCategory, district_id: uuid.UUID | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    """Plausible authorities for a category and district, best match first.

    A ranking aid for whoever is doing the routing, not a decision. Every
    result carries ``match_reasons`` so the human can see exactly why it was
    offered and disagree.
    """
    likely_types = CATEGORY_TO_TYPES.get(category, ())

    statement = _base_query()
    if district_id is not None:
        statement = statement.where(
            or_(Authority.district_id == district_id, Authority.district_id.is_(None))
        )

    candidates = db.session.scalars(statement).all()
    ranked: list[tuple[int, list[str], Authority]] = []

    for authority in candidates:
        score = 0
        reasons: list[str] = []

        if authority.type in likely_types:
            # Earlier in the tuple means a more usual owner for this category.
            score += 10 - likely_types.index(authority.type)
            reasons.append(f"handles {category.value} problems")

        if district_id is not None and authority.district_id == district_id:
            score += 5
            reasons.append("covers this district")
        elif authority.is_national:
            score += 1
            reasons.append("national remit")

        if score:
            ranked.append((score, reasons, authority))

    ranked.sort(key=lambda row: (-row[0], row[2].name))
    return [
        {**authority.to_dict(), "match_score": score, "match_reasons": reasons}
        for score, reasons, authority in ranked[:limit]
    ]


def get_authority_statistics() -> dict[str, Any]:
    """Counts by level and type, plus how many have no published contact."""
    by_level = dict(
        db.session.execute(
            select(Authority.level, func.count()).group_by(Authority.level)
        ).all()
    )
    by_type = dict(
        db.session.execute(
            select(Authority.type, func.count()).group_by(Authority.type)
        ).all()
    )

    return {
        "total": db.session.scalar(select(func.count()).select_from(Authority)) or 0,
        "by_level": {
            member.value: by_level.get(member, 0) for member in GovernmentLevel
        },
        "by_type": {member.value: by_type.get(member, 0) for member in AuthorityType},
        "without_contact": db.session.scalar(
            select(func.count())
            .select_from(Authority)
            .where(
                Authority.contact_email.is_(None), Authority.contact_phone.is_(None)
            )
        )
        or 0,
    }
