"""MediaAttachment - file evidence attached to something in the system.

A photo of a collapsed culvert is the difference between a claim and a case.
Attachments hang off reports, incidents, projects and progress updates alike,
so the reference is polymorphic: ``(entity_type, entity_id)``.

The cost of a polymorphic reference
-----------------------------------

``entity_id`` carries **no foreign key**, because SQL cannot point one column
at four different tables. Two consequences, both handled deliberately rather
than ignored:

* *Nothing stops an attachment naming a row that does not exist.* The service
  therefore resolves and verifies the target before writing the row - see
  ``media_service.resolve_entity``.
* *Deleting the target leaves the attachment behind*, and with it a file on
  disk that nothing references. ``media_service.delete_media_for_entity``
  exists so a caller removing a report can clear its evidence in the same
  breath; without it those files would accumulate forever.

The alternative - four nullable FK columns plus a CHECK that exactly one is
set - buys real integrity but costs a schema change for every new attachable
type. For a system that will keep gaining them, the polymorphic reference with
an enforced service boundary is the better trade.

Like ``ProgressUpdate``, this is an append-only record and carries no
``updated_at``: the bytes on disk never change, so neither does the row
describing them. Replacing evidence means uploading a new attachment, which
leaves the original visible rather than quietly rewriting history.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UtcDateTime, utcnow
from .enums import EntityType, MediaType, enum_column

if TYPE_CHECKING:  # pragma: no cover
    from .user import User

FILE_PATH_MAX_LENGTH = 255
FILENAME_MAX_LENGTH = 255
MIME_TYPE_MAX_LENGTH = 100


class MediaAttachment(Base):
    __tablename__ = "media_attachments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False
    )

    # RESTRICT: who supplied a piece of evidence is part of what makes it
    # evidence, so the account cannot be deleted out from under it.
    uploader_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "users.id", ondelete="RESTRICT", name="fk_media_attachments_uploader_id"
        ),
        nullable=False,
        index=True,
    )

    entity_type: Mapped[EntityType] = mapped_column(
        enum_column(EntityType, "media_entity_type"), nullable=False
    )
    # Deliberately not a foreign key; see module docstring.
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    # Storage key, never a client-supplied name. Relative to the storage root
    # so the same row survives a move from local disk to object storage.
    file_path: Mapped[str] = mapped_column(String(FILE_PATH_MAX_LENGTH), nullable=False)

    # Kept for display and download only. Never used to build a path.
    original_filename: Mapped[str] = mapped_column(
        String(FILENAME_MAX_LENGTH), nullable=False
    )

    mime_type: Mapped[str] = mapped_column(String(MIME_TYPE_MAX_LENGTH), nullable=False)
    # BigInteger: a 32-bit signed int caps at ~2GB, which is a limit nobody
    # should discover by hitting it if the upload cap is ever raised.
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    media_type: Mapped[MediaType] = mapped_column(
        enum_column(MediaType, "media_type"), nullable=False, index=True
    )

    uploader: Mapped["User"] = relationship(back_populates="media_attachments")

    __table_args__ = (
        # An empty file is never evidence of anything, and a negative size
        # means the writer was confused.
        CheckConstraint("file_size_bytes > 0", name="ck_media_attachments_size_positive"),
        # The only lookup that matters: "show me everything attached to this".
        Index("ix_media_attachments_entity", "entity_type", "entity_id"),
        Index("ix_media_attachments_created_at", "created_at"),
    )

    @property
    def is_image(self) -> bool:
        return self.media_type == MediaType.IMAGE

    def download_url(self) -> str:
        """Path the client uses to fetch the bytes.

        Built from the id, never from ``file_path`` - the storage key is an
        internal detail and publishing it would invite people to probe it.
        """
        return f"/api/v1/media/{self.id}/download"

    def to_dict(self) -> dict[str, Any]:
        """Explicit serialisation.

        ``file_path`` is deliberately absent: it is where the bytes live on the
        server, which is nobody's business outside this process.
        """
        return {
            "id": str(self.id),
            "entity_type": self.entity_type.value,
            "entity_id": str(self.entity_id),
            "original_filename": self.original_filename,
            "mime_type": self.mime_type,
            "media_type": self.media_type.value,
            "file_size_bytes": self.file_size_bytes,
            "is_image": self.is_image,
            "url": self.download_url(),
            "uploader": {
                "id": str(self.uploader_id),
                "full_name": self.uploader.full_name if self.uploader else None,
            },
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        return f"<MediaAttachment {self.id} {self.entity_type.value}>"
