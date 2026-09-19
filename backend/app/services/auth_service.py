"""Authentication business logic.

Token architecture
------------------

* **Access token** - a short-lived (default 15 min) HS256 JWT. Stateless, so
  verifying it costs no database round trip beyond loading the user to confirm
  the account is still active. Carries only the user id, never personal data.
* **Refresh token** - a long-lived (default 30 days) opaque random string, with
  only its SHA-256 stored in ``refresh_tokens``. Revocable, and rotated on
  every use.

Logout revokes the refresh token. It does **not** invalidate an access token
that has already been issued - nothing can, short of a denylist checked on
every request. The access-token lifetime is therefore the real revocation
window, which is why it is deliberately short.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from flask import current_app, has_request_context, request
from sqlalchemy import select

from ..extensions import db
from ..models.base import utcnow
from ..models.refresh_token import RefreshToken
from ..models.role import DEFAULT_ROLE, Role
from ..models.user import User
from ..utils.helpers import ApiError

# Argon2id with argon2-cffi's defaults, which track the library's current
# recommendations rather than being pinned to numbers that age badly here.
_hasher = PasswordHasher()

ACCESS_TOKEN_TYPE = "access"

# Returned for every failed login regardless of cause, so the endpoint cannot
# be used to discover which email addresses have accounts.
_INVALID_CREDENTIALS = "Invalid email or password."

# A real Argon2 hash of a throwaway value. Verifying against it when no user
# exists keeps the failure path's timing close to the success path's, so
# response time does not reveal whether an account exists.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))


# --- passwords -------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# --- tokens ----------------------------------------------------------------


def _hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_access_token(user: User) -> str:
    now = utcnow()
    ttl = timedelta(minutes=current_app.config["ACCESS_TOKEN_TTL_MINUTES"])
    payload = {
        "sub": str(user.id),
        "type": ACCESS_TOKEN_TYPE,
        "iat": now,
        "exp": now + ttl,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(
        payload,
        current_app.config["JWT_SECRET_KEY"],
        algorithm=current_app.config["JWT_ALGORITHM"],
    )


def create_refresh_token(user: User) -> str:
    """Issue an opaque refresh token and persist only its hash."""
    token = secrets.token_urlsafe(32)
    ttl = timedelta(days=current_app.config["REFRESH_TOKEN_TTL_DAYS"])
    db.session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=_hash_refresh_token(token),
            expires_at=utcnow() + ttl,
        )
    )
    return token


def resolve_access_token(token: str) -> User:
    """Verify an access token and return its user, or raise 401."""
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            current_app.config["JWT_SECRET_KEY"],
            # Pinning the algorithm list is what stops an attacker presenting a
            # token signed with "none", or an HS256 token against an RS256 key.
            algorithms=[current_app.config["JWT_ALGORITHM"]],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise ApiError("Token has expired.", status=401, code="token_expired") from None
    except jwt.InvalidTokenError:
        raise ApiError("Invalid token.", status=401, code="invalid_token") from None

    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise ApiError("Invalid token.", status=401, code="invalid_token")

    user = _load_user(payload["sub"])
    if user is None or not user.is_active:
        # Covers a deleted or deactivated account whose token has not expired.
        raise ApiError("Invalid token.", status=401, code="invalid_token")
    return user


def _load_user(subject: str) -> User | None:
    try:
        user_id = uuid.UUID(subject)
    except (ValueError, TypeError, AttributeError):
        return None
    return db.session.get(User, user_id)


def _find_usable_refresh_token(token: str) -> RefreshToken:
    record = db.session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == _hash_refresh_token(token))
    )
    # An access token presented here simply will not match any stored hash.
    if record is None or not record.is_usable:
        raise ApiError(
            "Invalid or expired refresh token.", status=401, code="invalid_refresh_token"
        )
    return record


# --- operations ------------------------------------------------------------


def register_user(
    email: str,
    password: str,
    full_name: str,
    phone: str | None,
    permanent_district_id: Any = None,
    temporary_district_id: Any = None,
) -> User:
    """Create a citizen account.

    The role is fixed here rather than read from the request: that is the only
    thing preventing a caller from registering themselves as an admin.
    """
    existing = db.session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise ApiError(
            "An account with this email already exists.",
            status=409,
            code="email_already_registered",
        )

    permanent = _resolve_district(permanent_district_id, "permanent_district_id")
    # Falls back to the permanent district, so the field is never left empty
    # just because the form omitted it.
    temporary = (
        _resolve_district(temporary_district_id, "temporary_district_id")
        if temporary_district_id
        else permanent
    )

    role = db.session.scalar(select(Role).where(Role.name == DEFAULT_ROLE))
    if role is None:
        # Seeding is a deployment step; failing loudly beats creating a user
        # with no role at all.
        raise ApiError(
            "Roles are not seeded; cannot create account.",
            status=500,
            code="roles_not_seeded",
        )

    user = User(
        email=email,
        password_hash=hash_password(password),
        full_name=full_name,
        phone=phone,
        is_active=True,
        permanent_district_id=permanent,
        temporary_district_id=temporary,
    )
    user.roles.append(role)

    db.session.add(user)
    db.session.commit()
    return user


def _resolve_district(value: Any, field: str) -> uuid.UUID | None:
    """Turn a district id from a form into a verified id, or refuse it.

    Checked against the table rather than trusted: an unknown district would
    otherwise fail later at the foreign key as an opaque 500, instead of here
    as a message naming the field.
    """
    if not value:
        return None

    from ..models.district import District

    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise ApiError(
            f"{field} is not a valid identifier.", status=400, code="invalid_identifier"
        ) from None

    if db.session.get(District, parsed) is None:
        raise ApiError("District not found.", status=404, code="district_not_found")
    return parsed


def authenticate(email: str, password: str, expected_role: str | None = None) -> User:
    """Verify credentials and stamp ``last_login_at``.

    A wrong password and an unknown email raise the identical 401: a distinct
    "no such user" would turn this endpoint into an account enumerator.

    ``expected_role`` fences a login to one portal. It is the *server* that
    decides whether the account holds that role - the caller only says which
    door was knocked on, never who it belongs to.
    """
    user = db.session.scalar(select(User).where(User.email == email))

    if user is None:
        verify_password(_DUMMY_HASH, password)
        raise ApiError(_INVALID_CREDENTIALS, status=401, code="invalid_credentials")

    if not verify_password(user.password_hash, password):
        raise ApiError(_INVALID_CREDENTIALS, status=401, code="invalid_credentials")

    if not user.is_active:
        # Checked after the password, so a disabled account is only revealed to
        # someone who already proved they know the password for it.
        raise ApiError("This account is disabled.", status=403, code="account_disabled")

    if expected_role is not None and not user.has_role(expected_role):
        # Same placement argument as is_active: the check runs only once the
        # password is proven, so this endpoint cannot be used to ask "is this
        # address an authority account?" without the credentials to match.
        _log_portal_mismatch(user, expected_role)
        raise ApiError(
            f"This account does not have {expected_role.replace('_', ' ')} access.",
            status=403,
            code="role_not_permitted",
        )

    user.last_login_at = utcnow()
    db.session.commit()
    return user


def _log_portal_mismatch(user: User, expected_role: str) -> None:
    """Record a correct-password login aimed at the wrong portal.

    Worth a warning rather than an info: valid credentials presented at an
    official door the account has no claim to is either a confused staff member
    or someone probing the fence, and the two are indistinguishable from here.
    """
    current_app.logger.warning(
        "security: portal role mismatch - user=%s expected=%s actual=%s ip=%s",
        user.id,
        expected_role,
        ",".join(user.role_names) or "none",
        request.remote_addr if has_request_context() else "n/a",
    )


def issue_token_pair(user: User) -> dict[str, Any]:
    tokens = {
        "access_token": create_access_token(user),
        "refresh_token": create_refresh_token(user),
        "token_type": "Bearer",
        "expires_in": current_app.config["ACCESS_TOKEN_TTL_MINUTES"] * 60,
    }
    db.session.commit()
    return tokens


def refresh_token_pair(token: str) -> dict[str, Any]:
    """Rotate a refresh token, returning a fresh pair.

    Rotation means a stolen token is usable at most once before the legitimate
    holder's next refresh invalidates it.
    """
    record = _find_usable_refresh_token(token)
    user = record.user
    if not user.is_active:
        raise ApiError("This account is disabled.", status=403, code="account_disabled")

    record.revoke()
    return issue_token_pair(user)


def revoke_refresh_token(user: User, token: str) -> None:
    """Log out by revoking one of *this user's* refresh tokens.

    Scoped to the caller: without the ``user_id`` filter an authenticated user
    could revoke somebody else's session by submitting their token. Idempotent,
    and silent on a token that does not match, so it cannot be used to probe
    which tokens exist.
    """
    record = db.session.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == _hash_refresh_token(token),
            RefreshToken.user_id == user.id,
        )
    )
    if record is not None:
        record.revoke()
        db.session.commit()


def revoke_all_refresh_tokens(user: User) -> int:
    """Revoke every outstanding refresh token for a user."""
    records = db.session.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
        )
    ).all()
    for record in records:
        record.revoke()
    db.session.commit()
    return len(records)
