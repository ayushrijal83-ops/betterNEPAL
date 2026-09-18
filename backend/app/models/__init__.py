"""Model package.

Every model module must be imported here so that ``Base.metadata`` is fully
populated before Alembic autogenerates a migration.
"""
from .base import Base, BaseModel, UtcDateTime, utcnow
from .district import District
from .municipality import MUNICIPALITY_TYPES, Municipality
from .refresh_token import RefreshToken
from .role import (
    DEFAULT_ROLE,
    ROLE_ADMIN,
    ROLE_AUTHORITY,
    ROLE_CITIZEN,
    ROLE_CONTRACTOR,
    ROLE_DESCRIPTIONS,
    ROLE_NAMES,
    ROLE_TREKKING_GUIDE,
    Role,
    user_roles,
)
from .user import User

__all__ = [
    "Base",
    "BaseModel",
    "DEFAULT_ROLE",
    "District",
    "MUNICIPALITY_TYPES",
    "Municipality",
    "ROLE_ADMIN",
    "ROLE_AUTHORITY",
    "ROLE_CITIZEN",
    "ROLE_CONTRACTOR",
    "ROLE_DESCRIPTIONS",
    "ROLE_NAMES",
    "ROLE_TREKKING_GUIDE",
    "RefreshToken",
    "Role",
    "User",
    "UtcDateTime",
    "user_roles",
    "utcnow",
]
