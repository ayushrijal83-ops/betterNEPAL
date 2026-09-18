"""Media business logic: validating, storing and serving file evidence.

Why the declared content type is not trusted
--------------------------------------------

A multipart upload carries a ``Content-Type`` and a filename, and the client
chooses both. Accepting ``image/jpeg`` on the client's word means an executable
renamed ``photo.jpg`` and declared as an image passes every check - and is then
sitting in a directory the server serves.

So three things must agree before a byte is written:

1. the extension is on the allowlist,
2. the declared MIME type is on the allowlist,
3. the file's own leading bytes match that type.

(3) is the one that matters. File signatures are not a complete defence - a
polyglot file can satisfy two formats at once - but they close the trivial
rename, which is the attack that actually gets attempted.

No image analysis happens here; understanding what a photo *shows* is Phase 10.
"""
from __future__ import annotations

import os
import uuid
from typing import Any, BinaryIO

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..extensions import db
from ..models.enums import EntityType, MediaType
from ..models.media_attachment import MediaAttachment
from ..models.role import ROLE_ADMIN, ROLE_AUTHORITY
from ..models.user import User
from ..utils.helpers import ApiError
from .storage_service import StorageError, get_storage

# Declared MIME type -> (media class, allowed extensions).
ALLOWED_MIME_TYPES: dict[str, tuple[MediaType, tuple[str, ...]]] = {
    "image/jpeg": (MediaType.IMAGE, (".jpg", ".jpeg")),
    "image/png": (MediaType.IMAGE, (".png",)),
    "image/webp": (MediaType.IMAGE, (".webp",)),
    "image/heic": (MediaType.IMAGE, (".heic", ".heif")),
    "image/heif": (MediaType.IMAGE, (".heic", ".heif")),
    "application/pdf": (MediaType.DOCUMENT, (".pdf",)),
}

ALLOWED_EXTENSIONS: set[str] = {
    extension
    for _, extensions in ALLOWED_MIME_TYPES.values()
    for extension in extensions
}

# How many leading bytes to read for signature checking.
SIGNATURE_BYTES = 32

MIN_UPLOAD_BYTES = 1


def _normalise_mime(value: str | None) -> str:
    """Strip any parameters: 'image/jpeg; charset=binary' -> 'image/jpeg'."""
    if not value:
        return ""
    return value.split(";")[0].strip().lower()


def _looks_like_jpeg(head: bytes) -> bool:
    return head[:3] == b"\xff\xd8\xff"


def _looks_like_png(head: bytes) -> bool:
    return head[:8] == b"\x89PNG\r\n\x1a\n"


def _looks_like_webp(head: bytes) -> bool:
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def _looks_like_heic(head: bytes) -> bool:
    """HEIC/HEIF are ISO base media files: a 'ftyp' box with an HEIF brand."""
    if head[4:8] != b"ftyp":
        return False
    brand = head[8:12]
    return brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis"}


def _looks_like_pdf(head: bytes) -> bool:
    return head[:5] == b"%PDF-"


SIGNATURE_CHECKS = {
    "image/jpeg": _looks_like_jpeg,
    "image/png": _looks_like_png,
    "image/webp": _looks_like_webp,
    "image/heic": _looks_like_heic,
    "image/heif": _looks_like_heic,
    "application/pdf": _looks_like_pdf,
}


def _parse_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise ApiError(
            f"{field} is not a valid identifier.", status=400, code="invalid_identifier"
        ) from None


def parse_entity_type(value: Any) -> EntityType:
    from ..models.enums import enum_values, parse_enum

    member = parse_enum(EntityType, value)
    if member is None:
        raise ApiError(
            f"entity_type must be one of: {', '.join(enum_values(EntityType))}.",
            status=400,
            code="invalid_entity_type",
        )
    return member


def resolve_entity(entity_type: EntityType, entity_id: Any):
    """Load the record an attachment points at, or 404.

    This is what stands in for the foreign key the polymorphic column cannot
    have: no attachment row is ever written for a target that does not exist.
    """
    from ..models.incident import Incident
    from ..models.progress_update import ProgressUpdate
    from ..models.project import Project
    from ..models.report import Report

    models = {
        EntityType.REPORT: (Report, "Report"),
        EntityType.INCIDENT: (Incident, "Incident"),
        EntityType.PROJECT: (Project, "Project"),
        EntityType.PROGRESS_UPDATE: (ProgressUpdate, "Progress update"),
    }
    model, label = models[entity_type]

    record = db.session.get(model, _parse_uuid(entity_id, "entity_id"))
    if record is None:
        raise ApiError(
            f"{label} not found.", status=404, code=f"{entity_type.value}_not_found"
        )
    return record


def _measure(file_obj: BinaryIO) -> int:
    """Byte length of a stream, without reading it into memory."""
    try:
        file_obj.seek(0, os.SEEK_END)
        size = file_obj.tell()
        file_obj.seek(0)
        return size
    except (AttributeError, OSError):
        raise ApiError(
            "The uploaded file could not be read.", status=400, code="unreadable_upload"
        ) from None


def validate_upload(
    file_obj: BinaryIO, original_filename: str, mime_type: str | None
) -> tuple[str, MediaType, int]:
    """Check extension, declared type, signature and size.

    Returns ``(mime_type, media_type, size)``. Raises 400 on anything it does
    not like, with a message naming what is actually accepted.
    """
    declared = _normalise_mime(mime_type)
    if declared not in ALLOWED_MIME_TYPES:
        raise ApiError(
            "This file type is not accepted.",
            status=400,
            code="unsupported_media_type",
            details={"allowed_types": sorted(ALLOWED_MIME_TYPES)},
        )

    media_type, permitted_extensions = ALLOWED_MIME_TYPES[declared]

    extension = os.path.splitext(original_filename or "")[1].lower()
    if extension not in permitted_extensions:
        raise ApiError(
            f"A {declared} file must have one of these extensions: "
            f"{', '.join(permitted_extensions)}.",
            status=400,
            code="extension_mismatch",
            details={"filename": original_filename, "declared_type": declared},
        )

    size = _measure(file_obj)
    if size < MIN_UPLOAD_BYTES:
        raise ApiError("The uploaded file is empty.", status=400, code="empty_file")

    maximum = current_app.config["MAX_UPLOAD_BYTES"]
    if size > maximum:
        raise ApiError(
            f"Files must be {maximum // (1024 * 1024)}MB or smaller.",
            status=400,
            code="file_too_large",
            details={"size_bytes": size, "max_bytes": maximum},
        )

    head = file_obj.read(SIGNATURE_BYTES)
    file_obj.seek(0)
    if not SIGNATURE_CHECKS[declared](head):
        # The decisive check: contents must match the claim.
        raise ApiError(
            f"The file's contents do not match the declared type {declared}.",
            status=400,
            code="content_type_mismatch",
        )

    return declared, media_type, size


def upload_media(
    user_id: Any,
    entity_type: EntityType | str,
    entity_id: Any,
    file_obj: BinaryIO,
    original_filename: str,
    mime_type: str | None,
) -> MediaAttachment:
    """Validate, store and record one attachment."""
    from werkzeug.utils import secure_filename

    uploader_id = _parse_uuid(user_id, "user_id")
    if db.session.get(User, uploader_id) is None:
        raise ApiError("Uploader not found.", status=404, code="user_not_found")

    resolved_type = (
        entity_type
        if isinstance(entity_type, EntityType)
        else parse_entity_type(entity_type)
    )
    entity = resolve_entity(resolved_type, entity_id)

    declared, media_type, size = validate_upload(file_obj, original_filename, mime_type)

    # Stored for display only, never used to build a path - but sanitised
    # anyway so it cannot carry control characters into a UI or a header.
    safe_name = secure_filename(original_filename or "") or f"upload{os.path.splitext(original_filename or '')[1].lower()}"

    try:
        storage_key = get_storage().save_file(file_obj, original_filename)
    except StorageError as exc:
        raise ApiError(str(exc), status=500, code="storage_failure") from None

    attachment = MediaAttachment(
        uploader_id=uploader_id,
        entity_type=resolved_type,
        entity_id=entity.id,
        file_path=storage_key,
        original_filename=safe_name[:255],
        mime_type=declared,
        file_size_bytes=size,
        media_type=media_type,
    )
    db.session.add(attachment)
    try:
        db.session.commit()
    except Exception:
        # The row failed but the bytes landed; do not leak an orphan file.
        db.session.rollback()
        get_storage().delete_file(storage_key)
        raise

    return get_media_by_id(attachment.id)


def get_media_by_id(media_id: Any) -> MediaAttachment:
    attachment = db.session.scalar(
        select(MediaAttachment)
        .options(selectinload(MediaAttachment.uploader))
        .where(MediaAttachment.id == _parse_uuid(media_id, "media_id"))
    )
    if attachment is None:
        raise ApiError("Media not found.", status=404, code="media_not_found")
    return attachment


def get_media_for_entity(
    entity_type: EntityType | str, entity_id: Any, verify_entity: bool = True
) -> list[MediaAttachment]:
    """Every attachment on one record, newest first."""
    resolved_type = (
        entity_type
        if isinstance(entity_type, EntityType)
        else parse_entity_type(entity_type)
    )
    parsed_id = _parse_uuid(entity_id, "entity_id")

    if verify_entity:
        resolve_entity(resolved_type, parsed_id)

    return list(
        db.session.scalars(
            select(MediaAttachment)
            .options(selectinload(MediaAttachment.uploader))
            .where(
                MediaAttachment.entity_type == resolved_type,
                MediaAttachment.entity_id == parsed_id,
            )
            .order_by(MediaAttachment.created_at.desc())
        ).all()
    )


def can_delete_media(attachment: MediaAttachment, user: User) -> bool:
    """Who may remove a piece of evidence.

    The uploader can withdraw their own; admins and authorities can remove
    anything, since they are the ones accountable for what the platform
    publishes.

    Note the limitation: *any* authority user qualifies, not only one attached
    to the owning body. There is no user-to-authority membership in the schema
    yet, so a narrower rule cannot be expressed. Worth tightening when that
    link exists.
    """
    if user.has_role(ROLE_ADMIN, ROLE_AUTHORITY):
        return True
    return attachment.uploader_id == user.id


def delete_media(media_id: Any, user_id: Any) -> dict[str, Any]:
    """Remove an attachment and the bytes behind it."""
    attachment = get_media_by_id(media_id)

    user = db.session.get(User, _parse_uuid(user_id, "user_id"))
    if user is None:
        raise ApiError("User not found.", status=404, code="user_not_found")

    if not can_delete_media(attachment, user):
        raise ApiError(
            "You do not have permission to delete this media.",
            status=403,
            code="permission_denied",
        )

    storage_key = attachment.file_path
    db.session.delete(attachment)
    db.session.commit()

    # Row first, file second: an orphaned file wastes disk, whereas a row
    # pointing at bytes that are gone breaks every page that renders it.
    file_removed = get_storage().delete_file(storage_key)

    return {"deleted": True, "file_removed": file_removed}


def delete_media_for_entity(entity_type: EntityType, entity_id: Any) -> int:
    """Remove every attachment on a record, and its files.

    The counterpart to the missing foreign key: a caller deleting a report can
    clear its evidence in the same operation, instead of leaving rows and files
    behind with nothing pointing at them.
    """
    attachments = get_media_for_entity(entity_type, entity_id, verify_entity=False)
    storage = get_storage()
    keys = [attachment.file_path for attachment in attachments]

    for attachment in attachments:
        db.session.delete(attachment)
    db.session.commit()

    for key in keys:
        storage.delete_file(key)
    return len(keys)


def get_media_statistics() -> dict[str, Any]:
    """Counts and total bytes held, by type."""
    from sqlalchemy import func

    by_type = dict(
        db.session.execute(
            select(MediaAttachment.media_type, func.count()).group_by(
                MediaAttachment.media_type
            )
        ).all()
    )
    by_entity = dict(
        db.session.execute(
            select(MediaAttachment.entity_type, func.count()).group_by(
                MediaAttachment.entity_type
            )
        ).all()
    )

    return {
        "total": db.session.scalar(select(func.count()).select_from(MediaAttachment)) or 0,
        "total_bytes": db.session.scalar(
            select(func.coalesce(func.sum(MediaAttachment.file_size_bytes), 0))
        )
        or 0,
        "by_media_type": {
            member.value: by_type.get(member, 0) for member in MediaType
        },
        "by_entity_type": {
            member.value: by_entity.get(member, 0) for member in EntityType
        },
    }
