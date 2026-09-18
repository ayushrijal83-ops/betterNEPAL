"""Media endpoints.

Viewing evidence is public: a photo of the collapsed culvert is the most
persuasive thing on the page, and hiding it behind a login would defeat the
purpose. Uploading requires an account so every file is attributable, and
deleting is restricted to the uploader, an authority or an admin.

Serving files
-------------

Bytes are streamed through ``send_file`` from a path this process resolves
itself, never from a client-supplied string, and always as an attachment with
an explicit content type. That combination is deliberate: an uploaded file
served inline with a guessed content type is how a stored HTML or SVG file
becomes a cross-site scripting hole on your own domain.
"""
from __future__ import annotations

from flask import Blueprint, request, send_file

from ..services import media_service
from ..services.storage_service import StorageError, get_storage
from ..utils.decorators import require_auth
from ..utils.helpers import ApiError, success_response
from ..utils.permissions import current_user

media_bp = Blueprint("media", __name__, url_prefix="/media")


@media_bp.post("/upload")
@require_auth
def upload():
    """Attach a file to a report, incident, project or progress update.

    Multipart form: ``file``, ``entity_type``, ``entity_id``. The uploader is
    taken from the access token, never from the body.
    """
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        raise ApiError(
            "A file is required under the 'file' field.",
            status=400,
            code="file_required",
        )

    entity_type = (request.form.get("entity_type") or "").strip()
    entity_id = (request.form.get("entity_id") or "").strip()
    if not entity_type or not entity_id:
        raise ApiError(
            "entity_type and entity_id are required.",
            status=400,
            code="validation_error",
            details={
                "entity_type": "entity_type is required." if not entity_type else None,
                "entity_id": "entity_id is required." if not entity_id else None,
            },
        )

    attachment = media_service.upload_media(
        user_id=current_user().id,
        entity_type=entity_type,
        entity_id=entity_id,
        file_obj=uploaded.stream,
        original_filename=uploaded.filename,
        mime_type=uploaded.mimetype,
    )
    return success_response({"media": attachment.to_dict()}, status=201)


@media_bp.get("/statistics")
def statistics():
    """Aggregate counts and bytes held. Public.

    Declared before ``/<media_id>`` so the literal path is not swallowed by the
    identifier converter.
    """
    return success_response(media_service.get_media_statistics())


@media_bp.get("/entity/<entity_type>/<entity_id>")
def list_for_entity(entity_type: str, entity_id: str):
    """Every attachment on one record, newest first. Public."""
    attachments = media_service.get_media_for_entity(entity_type, entity_id)
    return success_response(
        {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "media": [attachment.to_dict() for attachment in attachments],
            "count": len(attachments),
        }
    )


@media_bp.get("/<media_id>")
def get_media(media_id: str):
    """Attachment metadata and its download URL. Public."""
    return success_response(
        {"media": media_service.get_media_by_id(media_id).to_dict()}
    )


@media_bp.get("/<media_id>/download")
def download(media_id: str):
    """Stream the file itself. Public.

    Served as an attachment with the stored content type. ``send_file`` streams
    from disk rather than loading the whole file into memory.
    """
    attachment = media_service.get_media_by_id(media_id)
    storage = get_storage()

    if not storage.exists(attachment.file_path):
        # The row survived but the bytes did not - say so plainly rather than
        # letting send_file raise an opaque 500.
        raise ApiError(
            "The stored file is no longer available.",
            status=404,
            code="file_missing",
        )

    try:
        path = storage.get_file_path(attachment.file_path)
    except StorageError:
        raise ApiError(
            "The stored file could not be read.", status=500, code="storage_failure"
        ) from None

    return send_file(
        path,
        mimetype=attachment.mime_type,
        # as_attachment, plus an explicit mimetype, keeps an uploaded file from
        # ever being rendered as a document in the site's own origin.
        as_attachment=True,
        download_name=attachment.original_filename,
        conditional=True,
    )


@media_bp.delete("/<media_id>")
@require_auth
def delete(media_id: str):
    """Remove an attachment and its file.

    Permitted for the uploader, an authority or an admin; see
    ``media_service.can_delete_media``.
    """
    return success_response(
        media_service.delete_media(media_id, current_user().id)
    )
