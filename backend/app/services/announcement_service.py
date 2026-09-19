"""Announcement business logic: the feed, and the draft gate on alerts.

The one rule worth stating plainly: **a draft is never public.** Every read path
that serves unauthenticated callers filters drafts out, and publishing one is an
explicit action by an authority or admin. AI can write a disaster alert; it
cannot broadcast one.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from ..extensions import db
from ..models.announcement import Announcement
from ..models.base import utcnow
from ..utils.helpers import ApiError

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


def _parse_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise ApiError(
            f"{field} is not a valid identifier.", status=400, code="invalid_identifier"
        ) from None


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _base_query():
    return select(Announcement).options(
        selectinload(Announcement.author), selectinload(Announcement.district)
    )


def get_announcement(announcement_id: Any, include_drafts: bool = False) -> Announcement:
    record = db.session.scalar(
        _base_query().where(Announcement.id == _parse_uuid(announcement_id, "announcement_id"))
    )
    if record is None or (record.is_draft and not include_drafts):
        # A draft is indistinguishable from a non-existent record to anyone not
        # allowed to see it; a 403 would confirm that something is there.
        raise ApiError("Announcement not found.", status=404, code="announcement_not_found")
    return record


def list_announcements(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """The feed, newest first.

    With ``district_id``: that district's items *plus* national ones, because a
    reader in Kaski needs the national flood warning as much as the local one -
    returning only district rows would hide exactly the alerts that matter most.
    Without it: national items only.

    ``include_drafts`` is honoured only when the caller has already been
    authorised; the route decides that, not this function.
    """
    filters = filters or {}
    statement = _base_query()
    count_statement = select(func.count()).select_from(Announcement)

    if not filters.get("include_drafts"):
        statement = statement.where(Announcement.is_draft.is_(False))
        count_statement = count_statement.where(Announcement.is_draft.is_(False))
    elif filters.get("drafts_only"):
        statement = statement.where(Announcement.is_draft.is_(True))
        count_statement = count_statement.where(Announcement.is_draft.is_(True))

    district_id = filters.get("district_id")
    if district_id:
        parsed = _parse_uuid(district_id, "district_id")
        scope = or_(
            Announcement.district_id == parsed, Announcement.district_id.is_(None)
        )
        statement = statement.where(scope)
        count_statement = count_statement.where(scope)
    elif filters.get("national_only"):
        statement = statement.where(Announcement.district_id.is_(None))
        count_statement = count_statement.where(Announcement.district_id.is_(None))

    page = max(1, _as_int(filters.get("page"), 1))
    per_page = min(MAX_PAGE_SIZE, max(1, _as_int(filters.get("per_page"), DEFAULT_PAGE_SIZE)))

    total = db.session.scalar(count_statement) or 0
    records = db.session.scalars(
        statement.order_by(Announcement.created_at.desc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    ).all()

    return {
        "announcements": [record.to_dict() for record in records],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page if total else 0,
        },
    }


def create_announcement(
    author_id: Any,
    title: str,
    body: str,
    district_id: Any = None,
    is_draft: bool = False,
) -> Announcement:
    """Publish, or park as a draft for review."""
    from ..models.district import District
    from ..models.user import User

    parsed_author = _parse_uuid(author_id, "author_id")
    if db.session.get(User, parsed_author) is None:
        raise ApiError("Author not found.", status=404, code="user_not_found")

    parsed_district = None
    if district_id:
        parsed_district = _parse_uuid(district_id, "district_id")
        if db.session.get(District, parsed_district) is None:
            raise ApiError("District not found.", status=404, code="district_not_found")

    record = Announcement(
        title=title,
        body=body,
        author_id=parsed_author,
        district_id=parsed_district,
        is_draft=bool(is_draft),
    )
    db.session.add(record)
    db.session.commit()
    return get_announcement(record.id, include_drafts=True)


def publish_draft(announcement_id: Any, publisher_id: Any) -> Announcement:
    """Clear a draft for publication - the human half of an AI-written alert.

    The publisher is recorded as the author, replacing whoever (or whatever)
    drafted it. An alert that goes out carries the name of the person
    accountable for it, not the process that suggested the wording.
    """
    record = get_announcement(announcement_id, include_drafts=True)
    if not record.is_draft:
        raise ApiError(
            "This announcement is already published.",
            status=409,
            code="already_published",
        )

    record.is_draft = False
    record.author_id = _parse_uuid(publisher_id, "publisher_id")
    record.created_at = utcnow()  # published now, not when the draft was written
    db.session.commit()
    return get_announcement(record.id, include_drafts=True)


def delete_announcement(announcement_id: Any) -> None:
    record = get_announcement(announcement_id, include_drafts=True)
    db.session.delete(record)
    db.session.commit()


def recent_for_context(district_id: Any = None, limit: int = 5) -> list[Announcement]:
    """Published announcements for the chatbot's context window.

    Drafts are excluded here as everywhere: an unreviewed AI draft must not be
    fed back to the AI and repeated to a citizen as though it were official.
    """
    statement = _base_query().where(Announcement.is_draft.is_(False))

    if district_id:
        parsed = _parse_uuid(district_id, "district_id")
        statement = statement.where(
            or_(Announcement.district_id == parsed, Announcement.district_id.is_(None))
        )

    return list(
        db.session.scalars(
            statement.order_by(Announcement.created_at.desc()).limit(limit)
        ).all()
    )
