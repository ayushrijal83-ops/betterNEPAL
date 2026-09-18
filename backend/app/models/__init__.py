"""Model package.

Every model module must be imported here so that ``Base.metadata`` is fully
populated before Alembic autogenerates a migration.
"""
from .authority import Authority
from .base import Base, BaseModel, UtcDateTime, utcnow
from .district import District
from .enums import (
    AuthorityType,
    GovernmentLevel,
    IncidentSeverity,
    IncidentStatus,
    ReportCategory,
    ReportStatus,
)
from .incident import Incident
from .municipality import MUNICIPALITY_TYPES, Municipality
from .refresh_token import RefreshToken
from .report import Report
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
    "Authority",
    "AuthorityType",
    "Base",
    "BaseModel",
    "DEFAULT_ROLE",
    "District",
    "GovernmentLevel",
    "Incident",
    "IncidentSeverity",
    "IncidentStatus",
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
    "Report",
    "ReportCategory",
    "ReportStatus",
    "Role",
    "User",
    "UtcDateTime",
    "user_roles",
    "utcnow",
]
