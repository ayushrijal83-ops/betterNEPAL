"""Role predicates and the current-request user.

Pure authorization logic lives here; the Flask plumbing that uses it is in
``decorators.py``. Keeping them apart means the rules can be unit tested
without a request context.
"""
from __future__ import annotations

from flask import g

from ..models.role import (
    ROLE_ADMIN,
    ROLE_AUTHORITY,
    ROLE_CITIZEN,
    ROLE_CONTRACTOR,
    ROLE_NAMES,
    ROLE_TREKKING_GUIDE,
)
from ..models.user import User

__all__ = [
    "ROLE_ADMIN",
    "ROLE_AUTHORITY",
    "ROLE_CITIZEN",
    "ROLE_CONTRACTOR",
    "ROLE_NAMES",
    "ROLE_TREKKING_GUIDE",
    "current_user",
    "is_authenticated",
    "user_has_any_role",
]


def user_has_any_role(user: User | None, *role_names: str) -> bool:
    """True when the user holds at least one of the named roles.

    No role name is special-cased — an admin is only permitted where the
    caller lists ``admin``. Implicit superuser rules are the kind of thing that
    quietly grants access nobody meant to grant.
    """
    if user is None or not user.is_active:
        return False
    return user.has_role(*role_names)


def current_user() -> User | None:
    """The user attached to this request by ``require_auth``, if any."""
    return g.get("current_user")


def is_authenticated() -> bool:
    return current_user() is not None
