"""Model package.

Domain models land here in later phases. Import each new model module in this
file so that ``Base.metadata`` is fully populated before Alembic autogenerates
a migration.
"""
from .base import Base, BaseModel, UtcDateTime, utcnow

__all__ = ["Base", "BaseModel", "UtcDateTime", "utcnow"]
