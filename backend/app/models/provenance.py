"""Provenance columns for geographic records.

Every geographic row should be able to answer "where did this come from, and
how much do we trust it?". A district name transcribed from a project research
document and a boundary polygon published by a government survey department
are both rows in the same table, and the platform must not present them as
equally authoritative.

``verification_status`` is deliberately conservative: nothing is
``official_source`` merely because it exists in the project's own files.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import UtcDateTime

# Not yet assessed.
VERIFICATION_UNVERIFIED = "unverified"
# Transcribed from project research/reference material. Usable for names and
# hierarchy; not evidence of an authoritative government publication.
VERIFICATION_REFERENCE_ONLY = "reference_only"
# Imported from an authoritative government dataset, with a source URL.
VERIFICATION_OFFICIAL_SOURCE = "official_source"
# Confirmed on the ground by a human.
VERIFICATION_FIELD_VERIFIED = "field_verified"

VERIFICATION_STATUSES: tuple[str, ...] = (
    VERIFICATION_UNVERIFIED,
    VERIFICATION_REFERENCE_ONLY,
    VERIFICATION_OFFICIAL_SOURCE,
    VERIFICATION_FIELD_VERIFIED,
)

# Where a row came from.
SOURCE_TYPE_RESEARCH_REFERENCE = "research_reference"
SOURCE_TYPE_OFFICIAL_DATASET = "official_dataset"
SOURCE_TYPE_FIELD_SURVEY = "field_survey"
SOURCE_TYPE_MANUAL_ENTRY = "manual_entry"

SOURCE_TYPES: tuple[str, ...] = (
    SOURCE_TYPE_RESEARCH_REFERENCE,
    SOURCE_TYPE_OFFICIAL_DATASET,
    SOURCE_TYPE_FIELD_SURVEY,
    SOURCE_TYPE_MANUAL_ENTRY,
)


def verification_status_constraint(table_name: str) -> CheckConstraint:
    """CHECK constraint restricting ``verification_status`` to the known set.

    Enforced in the database, not just in Python, so a direct SQL import cannot
    invent a status the application does not understand.
    """
    allowed = ", ".join(f"'{status}'" for status in VERIFICATION_STATUSES)
    return CheckConstraint(
        f"verification_status IN ({allowed})",
        name=f"ck_{table_name}_verification_status",
    )


class ProvenanceMixin:
    """Adds source-tracking columns to a model."""

    # Human-readable origin: a filename, dataset name or organisation.
    source: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(String(512))
    source_type: Mapped[str | None] = mapped_column(String(32))

    # Null until somebody actually checks the record against its source.
    last_verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=VERIFICATION_UNVERIFIED
    )

    def provenance_dict(self) -> dict:
        return {
            "source": self.source,
            "source_url": self.source_url,
            "source_type": self.source_type,
            "verification_status": self.verification_status,
            "last_verified_at": (
                self.last_verified_at.isoformat() if self.last_verified_at else None
            ),
        }
