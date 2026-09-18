"""Authentication and authorization decorators for route handlers."""
from __future__ import annotations

from functools import wraps
from typing import Callable

from flask import g, request

from .helpers import ApiError
from .permissions import user_has_any_role

BEARER_PREFIX = "bearer "


def _bearer_token() -> str:
    """Extract the bearer token, or raise 401."""
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith(BEARER_PREFIX):
        raise ApiError(
            "Authentication required.", status=401, code="authentication_required"
        )
    token = header[len(BEARER_PREFIX):].strip()
    if not token:
        raise ApiError(
            "Authentication required.", status=401, code="authentication_required"
        )
    return token


def require_auth(view: Callable) -> Callable:
    """Reject the request unless it carries a valid, non-expired access token.

    On success the authenticated user is placed on ``g.current_user``.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        # Imported here to avoid a circular import: the service imports models,
        # which is fine, but the service also imports this module's siblings.
        from ..services.auth_service import resolve_access_token

        g.current_user = resolve_access_token(_bearer_token())
        return view(*args, **kwargs)

    return wrapper


def require_roles(*role_names: str) -> Callable:
    """Require authentication plus at least one of the named roles.

    Usage::

        @require_roles("admin")
        @require_roles("admin", "authority")
    """
    if not role_names:
        raise ValueError("require_roles needs at least one role name.")

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        @require_auth
        def wrapper(*args, **kwargs):
            if not user_has_any_role(g.current_user, *role_names):
                # 403, not 404: the caller is authenticated, and hiding the
                # endpoint's existence buys nothing once they hold a token.
                raise ApiError(
                    "You do not have permission to perform this action.",
                    status=403,
                    code="permission_denied",
                )
            return view(*args, **kwargs)

        return wrapper

    return decorator
