"""Authentication endpoints.

Handlers stay thin: validate the payload, call the service, shape the response.
All business rules live in ``app/services/auth_service.py``.
"""
from __future__ import annotations

from flask import Blueprint, request

from ..services import auth_service
from ..utils.decorators import require_auth
from ..utils.helpers import success_response
from ..utils.permissions import current_user
from ..utils.validators import validate_login, validate_refresh, validate_registration

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.post("/register")
def register():
    """Create a citizen account.

    Any ``role`` or ``roles`` key in the body is ignored rather than rejected -
    the validator never reads it, so there is no path from request data to role
    assignment.
    """
    data = validate_registration(request.get_json(silent=True))
    user = auth_service.register_user(
        email=data["email"],
        password=data["password"],
        full_name=data["full_name"],
        phone=data["phone"],
    )
    return success_response({"user": user.to_public_dict()}, status=201)


@auth_bp.post("/login")
def login():
    data = validate_login(request.get_json(silent=True))
    user = auth_service.authenticate(data["email"], data["password"])
    tokens = auth_service.issue_token_pair(user)
    return success_response({"user": user.to_public_dict(), **tokens})


@auth_bp.post("/refresh")
def refresh():
    token = validate_refresh(request.get_json(silent=True))
    return success_response(auth_service.refresh_token_pair(token))


@auth_bp.post("/logout")
@require_auth
def logout():
    """Revoke the supplied refresh token.

    Requires a valid access token so one user cannot revoke another's session
    by guessing. The already-issued access token stays valid until it expires;
    see the auth service docstring.
    """
    payload = request.get_json(silent=True) or {}
    token = payload.get("refresh_token")
    if isinstance(token, str) and token.strip():
        auth_service.revoke_refresh_token(current_user(), token.strip())
        return success_response({"revoked": "token"})

    revoked = auth_service.revoke_all_refresh_tokens(current_user())
    return success_response({"revoked": "all", "count": revoked})


@auth_bp.get("/me")
@require_auth
def me():
    return success_response({"user": current_user().to_public_dict()})
