"""The SQLAlchemy column type used for stored geometry.

Why this exists
---------------

The platform targets PostgreSQL + PostGIS, but the test suite runs on
in-memory SQLite (Phase 2) so that ``pytest`` needs no database server. Those
two facts collide: GeoAlchemy2's ``Geometry`` type registers DDL listeners that
fire whenever a column's type *is a* ``Geometry``, and on SQLite they call
SpatiaLite functions (``RecoverGeometryColumn``) that do not exist. Using
``with_variant`` does not help - the listeners still see a ``Geometry``.

``GeometryColumn`` is a ``TypeDecorator``, so it is not an instance of
``Geometry`` and those listeners never fire. It resolves per dialect:

* **PostgreSQL** -> a real ``geometry(<TYPE>,<SRID>)`` PostGIS column.
* **anything else** -> ``TEXT``.

What this is NOT
----------------

The SQLite fallback is *not* a geometry implementation. It exists only so that
tables containing a geometry column can be created, letting the non-spatial
model tests (names, codes, foreign keys, uniqueness) run without PostgreSQL.
Nothing spatial works on SQLite: no containment, no distance, no GeoJSON
conversion. Every query that needs PostGIS is guarded by
``geolocation_service.spatial_backend_available()`` and every test that needs
PostGIS lives in ``tests/test_postgis.py``, which skips when it is absent.

Spatial indexes are declared explicitly on each model with
``postgresql_using="gist"`` rather than via GeoAlchemy2's ``spatial_index``
flag, so the index is visible in the migration and in the model.
"""
from __future__ import annotations

from geoalchemy2 import Geometry
from sqlalchemy.types import Text, TypeDecorator

# WGS84. Stored coordinates are plain longitude/latitude degrees, which is what
# GPS devices, GeoJSON and every phone reporting a problem already produce.
DEFAULT_SRID = 4326


class GeometryColumn(TypeDecorator):
    """A PostGIS geometry column that degrades to TEXT off PostgreSQL."""

    impl = Text
    cache_ok = True

    def __init__(self, geometry_type: str, srid: int = DEFAULT_SRID, **kwargs) -> None:
        self.geometry_type = geometry_type
        self.srid = srid
        super().__init__(**kwargs)

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(
                Geometry(
                    geometry_type=self.geometry_type,
                    srid=self.srid,
                    # Declared explicitly on the model instead; see module docstring.
                    spatial_index=False,
                )
            )
        return dialect.type_descriptor(Text())

    def __repr__(self) -> str:
        # Alembic writes this straight into a migration file, so it has to be
        # valid Python referring to this class.
        return f"GeometryColumn({self.geometry_type!r}, srid={self.srid})"
