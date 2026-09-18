"""Municipality model - Nepal's local level, below District.

Deletion behaviour is deliberate: the foreign key uses ``ON DELETE RESTRICT``.
Deleting a district that still holds municipalities fails loudly rather than
cascading (which would silently destroy local-level records, and later the
reports and projects attached to them) or nulling the link (which the NOT NULL
column forbids anyway). Districts are not routine deletions; a refused delete
is the correct outcome.

Unlike district names, municipality names are *not* unique nationally - the
same name recurs across districts - so uniqueness is scoped to the parent
district. ``code`` is nullable for the same reason as on District: no
authoritative code dataset is available to this project yet.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..gis.types import DEFAULT_SRID, GeometryColumn
from .base import BaseModel
from .provenance import ProvenanceMixin, verification_status_constraint

if TYPE_CHECKING:  # pragma: no cover
    from .district import District

# Nepal's four constitutionally defined local-level unit types.
MUNICIPALITY_TYPE_METROPOLITAN = "metropolitan"
MUNICIPALITY_TYPE_SUB_METROPOLITAN = "sub_metropolitan"
MUNICIPALITY_TYPE_MUNICIPALITY = "municipality"
MUNICIPALITY_TYPE_RURAL = "rural_municipality"

MUNICIPALITY_TYPES: tuple[str, ...] = (
    MUNICIPALITY_TYPE_METROPOLITAN,
    MUNICIPALITY_TYPE_SUB_METROPOLITAN,
    MUNICIPALITY_TYPE_MUNICIPALITY,
    MUNICIPALITY_TYPE_RURAL,
)


class Municipality(BaseModel, ProvenanceMixin):
    __tablename__ = "municipalities"

    district_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("districts.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    name_ne: Mapped[str | None] = mapped_column(String(120))
    municipality_type: Mapped[str | None] = mapped_column(String(32), index=True)

    code: Mapped[str | None] = mapped_column(String(16), unique=True, index=True)

    boundary = mapped_column(GeometryColumn("MULTIPOLYGON", DEFAULT_SRID), nullable=True)

    district: Mapped["District"] = relationship(back_populates="municipalities")

    __table_args__ = (
        UniqueConstraint("district_id", "name", name="uq_municipalities_district_name"),
        # Constrained in the database, not just in Python, so a direct SQL
        # import cannot introduce a fifth local-unit type.
        CheckConstraint(
            "municipality_type IS NULL OR municipality_type IN "
            "('metropolitan', 'sub_metropolitan', 'municipality', 'rural_municipality')",
            name="ck_municipalities_type",
        ),
        verification_status_constraint("municipalities"),
        # See District.boundary: resolving a GPS point to a local unit across
        # ~750 polygons is the query this table exists to serve.
        Index("ix_municipalities_boundary", "boundary", postgresql_using="gist"),
    )

    def to_dict(self, geometry: dict | None = None) -> dict:
        """Explicit serialisation; see ``District.to_dict`` on ``geometry``."""
        return {
            "id": str(self.id),
            "district_id": str(self.district_id),
            "name": self.name,
            "name_ne": self.name_ne,
            "municipality_type": self.municipality_type,
            "code": self.code,
            "geometry": geometry,
            "provenance": self.provenance_dict(),
        }

    def __repr__(self) -> str:
        return f"<Municipality {self.name}>"
