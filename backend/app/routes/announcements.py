"""Announcement endpoints: the public feed and the disaster-alert channel.

Reading published items is public - a flood warning nobody can read without an
account is not a warning. Writing is authority/admin, and drafts are visible
only to them.
"""
from __future__ import annotations

from flask import Blueprint, request

from ..models.role import ROLE_ADMIN, ROLE_AUTHORITY
from ..services import announcement_service
from ..utils.decorators import require_auth, require_roles
from ..utils.helpers import success_response
from ..utils.permissions import current_user, user_has_any_role
from ..utils.validators import validate_announcement

announcements_bp = Blueprint("announcements", __name__, url_prefix="/announcements")

TRUTHY = {"1", "true", "yes", "on"}


@announcements_bp.get("")
def list_announcements():
    """The feed. Public.

    ``?district_id=`` returns that district's items **and** national ones - a
    reader in Kaski needs the nationwide flood warning as much as the local
    notice, and filtering it out would hide the most important alerts. Without
    it, national items only.

    Drafts never appear here.
    """
    return success_response(
        announcement_service.list_announcements(
            {
                "district_id": request.args.get("district_id"),
                "national_only": request.args.get("scope", "").strip().lower() == "national",
                "page": request.args.get("page"),
                "per_page": request.args.get("per_page"),
            }
        )
    )


@announcements_bp.get("/drafts")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def list_drafts():
    """AI-written alerts awaiting human review. Authority and admin only."""
    return success_response(
        announcement_service.list_announcements(
            {
                "include_drafts": True,
                "drafts_only": True,
                "district_id": request.args.get("district_id"),
                "page": request.args.get("page"),
                "per_page": request.args.get("per_page"),
            }
        )
    )


@announcements_bp.get("/<announcement_id>")
def get_announcement(announcement_id: str):
    """One announcement. Public for published items.

    A draft 404s for everyone but an authority or admin - returning 403 would
    confirm that something exists at that id, which is itself a disclosure.
    """
    from ..utils.decorators import BEARER_PREFIX

    privileged = False
    header = request.headers.get("Authorization", "")
    if header.lower().startswith(BEARER_PREFIX):
        try:
            from ..services.auth_service import resolve_access_token

            user = resolve_access_token(header[len(BEARER_PREFIX):].strip())
            privileged = user_has_any_role(user, ROLE_AUTHORITY, ROLE_ADMIN)
        except Exception:
            # A bad token on a public endpoint is simply "not privileged".
            privileged = False

    record = announcement_service.get_announcement(
        announcement_id, include_drafts=privileged
    )
    return success_response({"announcement": record.to_dict()})


@announcements_bp.post("")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def create_announcement():
    """Publish an announcement, or file an AI-written draft for review.

    ``is_draft: true`` is how a generated disaster alert enters the system: it
    is stored, it is reviewable, and it reaches nobody until a human publishes
    it. The author is the authenticated caller, never a body field.
    """
    data = validate_announcement(request.get_json(silent=True))
    record = announcement_service.create_announcement(
        author_id=current_user().id,
        title=data["title"],
        body=data["body"],
        district_id=data["district_id"],
        is_draft=data["is_draft"],
    )
    return success_response({"announcement": record.to_dict()}, status=201)


@announcements_bp.post("/<announcement_id>/publish")
@require_roles(ROLE_AUTHORITY, ROLE_ADMIN)
def publish(announcement_id: str):
    """Clear a draft for publication - the human half of an AI-drafted alert."""
    record = announcement_service.publish_draft(announcement_id, current_user().id)
    return success_response({"announcement": record.to_dict()})


@announcements_bp.delete("/<announcement_id>")
@require_roles(ROLE_ADMIN)
def delete(announcement_id: str):
    """Remove an announcement. Admin only.

    Narrower than creation on purpose: withdrawing something already broadcast
    is a heavier act than issuing it.
    """
    announcement_service.delete_announcement(announcement_id)
    return success_response({"deleted": True})
