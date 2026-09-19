"""Authentication endpoints.

Handlers stay thin: validate the payload, call the service, shape the response.
All business rules live in ``app/services/auth_service.py``.
"""
from __future__ import annotations

from flask import Blueprint, current_app, make_response, request

from ..extensions import limiter
from ..services import auth_service
from ..utils.decorators import require_auth
from ..utils.helpers import success_response
from ..utils.permissions import current_user
from ..utils.validators import validate_login, validate_refresh, validate_registration

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

# Credential endpoints are where brute force happens. Argon2 already makes each
# guess expensive; this caps how many a single source may even attempt.
CREDENTIAL_LIMIT = "10 per minute"


def _with_refresh_cookie(payload, status: int = 200):
    """Return a response that also carries the refresh token as a cookie.

    The token stays in the JSON body too, so existing clients keep working.
    The cookie is the part that matters: HttpOnly puts it out of reach of
    JavaScript, so a cross-site scripting bug can no longer read the one
    credential that outlives an access token.

    Scoped to /api/v1/auth, so it is sent only to the endpoints that consume
    it and never attached to ordinary API calls. SameSite=Lax keeps it off
    cross-site requests; Secure is configurable only because localhost is not
    HTTPS, and production refuses to start without it.
    """
    token = payload.get("refresh_token")
    response = make_response(success_response(payload, status=status))

    if token:
        response.set_cookie(
            current_app.config["REFRESH_COOKIE_NAME"],
            token,
            httponly=True,
            secure=current_app.config["REFRESH_COOKIE_SECURE"],
            samesite=current_app.config["REFRESH_COOKIE_SAMESITE"],
            path=current_app.config["REFRESH_COOKIE_PATH"],
            max_age=current_app.config["REFRESH_TOKEN_TTL_DAYS"] * 24 * 60 * 60,
        )
    return response


def _clear_refresh_cookie(response):
    response.delete_cookie(
        current_app.config["REFRESH_COOKIE_NAME"],
        path=current_app.config["REFRESH_COOKIE_PATH"],
    )
    return response


def _refresh_token_from_request() -> str | None:
    """The explicit body token, or the cookie when none was sent.

    Body first, deliberately. Cookie-first looks safer but silently ignores
    what the caller actually sent: presenting a bogus or expired token would
    quietly succeed on the strength of an ambient cookie, and a request that
    should have failed would not. The cookie is the fallback for clients that
    hold no token in JavaScript at all.
    """
    payload = request.get_json(silent=True) or {}
    token = payload.get("refresh_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return request.cookies.get(current_app.config["REFRESH_COOKIE_NAME"]) or None


def _body_refresh_token() -> str | None:
    """Only the body token, ignoring any cookie.

    Logout scoping uses this. "No token means end every session" is a real
    feature from Phase 3, and letting the browser's own cookie satisfy the
    check would make it unreachable from a browser - the one place it matters.
    """
    payload = request.get_json(silent=True) or {}
    token = payload.get("refresh_token")
    return token.strip() if isinstance(token, str) and token.strip() else None



@auth_bp.post("/register")
@limiter.limit(CREDENTIAL_LIMIT)
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
        permanent_district_id=data["permanent_district_id"],
        temporary_district_id=data["temporary_district_id"],
    )
    return success_response({"user": user.to_public_dict()}, status=201)


@auth_bp.post("/login")
@limiter.limit(CREDENTIAL_LIMIT)
def login():
    data = validate_login(request.get_json(silent=True))
    user = auth_service.authenticate(
        data["email"], data["password"], expected_role=data["expected_role"]
    )
    tokens = auth_service.issue_token_pair(user)
    return _with_refresh_cookie({"user": user.to_public_dict(), **tokens})


@auth_bp.post("/refresh")
@limiter.limit(CREDENTIAL_LIMIT)
def refresh():
    """Rotate the token pair.

    Accepts the refresh token from the HttpOnly cookie or the JSON body. The
    validator still runs when neither is present, so a caller gets the same
    field-level error as before rather than a bare 401.
    """
    token = _refresh_token_from_request()
    if not token:
        validate_refresh(request.get_json(silent=True))  # raises the 400
    return _with_refresh_cookie(auth_service.refresh_token_pair(token))


@auth_bp.post("/logout")
@require_auth
def logout():
    """Revoke the supplied refresh token.

    Requires a valid access token so one user cannot revoke another's session
    by guessing. The already-issued access token stays valid until it expires;
    see the auth service docstring.
    """
    token = _body_refresh_token()
    if token:
        auth_service.revoke_refresh_token(current_user(), token)
        return _clear_refresh_cookie(make_response(success_response({"revoked": "token"})))

    revoked = auth_service.revoke_all_refresh_tokens(current_user())
    return _clear_refresh_cookie(
        make_response(success_response({"revoked": "all", "count": revoked}))
    )


@auth_bp.get("/me")
@require_auth
def me():
    return success_response({"user": current_user().to_public_dict()})
