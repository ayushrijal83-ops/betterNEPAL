"""Request payload validation.

Validators collect every field error before raising, so a client gets one
response listing all problems rather than discovering them one at a time.

A submitted password is never echoed back in an error — only a description of
the rule it broke.
"""
from __future__ import annotations

import re
from typing import Any

from .helpers import ApiError

# Deliberately permissive: the authoritative test of an address is sending mail
# to it, and over-strict patterns reject valid addresses.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

# Nepali numbers are typically +977 followed by 9-10 digits, but the platform
# should not refuse a foreign guide's number, so this only checks shape.
PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9\s\-]{6,19}$")

EMAIL_MAX_LENGTH = 255
FULL_NAME_MIN_LENGTH = 2
FULL_NAME_MAX_LENGTH = 120

# Length-only policy, following current NIST guidance: composition rules push
# people towards predictable substitutions without adding real entropy. The
# upper bound exists so an enormous input cannot burn CPU in the hasher.
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128


class ValidationErrors(dict):
    """Accumulates ``field -> message`` and raises once, as a 400."""

    def add(self, field: str, message: str) -> None:
        self.setdefault(field, message)

    def raise_if_any(self) -> None:
        if self:
            raise ApiError(
                "The submitted data is invalid.",
                status=400,
                code="validation_error",
                details=dict(self),
            )


def require_json(payload: Any) -> dict[str, Any]:
    """Reject anything that is not a JSON object before field validation."""
    if not isinstance(payload, dict):
        raise ApiError(
            "Request body must be a JSON object.", status=400, code="invalid_payload"
        )
    return payload


def _as_text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    return value.strip() if isinstance(value, str) else ""


def normalise_email(value: str) -> str:
    return value.strip().lower()


def validate_email(value: str, errors: ValidationErrors, field: str = "email") -> str:
    if not value:
        errors.add(field, "Email is required.")
        return ""
    if len(value) > EMAIL_MAX_LENGTH:
        errors.add(field, f"Email must be at most {EMAIL_MAX_LENGTH} characters.")
        return ""
    if not EMAIL_PATTERN.match(value):
        errors.add(field, "Email is not a valid address.")
        return ""
    return normalise_email(value)


def validate_password(value: str, errors: ValidationErrors, field: str = "password") -> str:
    if not value:
        errors.add(field, "Password is required.")
        return ""
    if len(value) < PASSWORD_MIN_LENGTH:
        errors.add(field, f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
        return ""
    if len(value) > PASSWORD_MAX_LENGTH:
        errors.add(field, f"Password must be at most {PASSWORD_MAX_LENGTH} characters.")
        return ""
    return value


def validate_full_name(value: str, errors: ValidationErrors, field: str = "full_name") -> str:
    if not value:
        errors.add(field, "Full name is required.")
        return ""
    if not (FULL_NAME_MIN_LENGTH <= len(value) <= FULL_NAME_MAX_LENGTH):
        errors.add(
            field,
            f"Full name must be between {FULL_NAME_MIN_LENGTH} and "
            f"{FULL_NAME_MAX_LENGTH} characters.",
        )
        return ""
    return value


def validate_phone(value: str, errors: ValidationErrors, field: str = "phone") -> str | None:
    """Phone is optional; an empty value is accepted and stored as NULL."""
    if not value:
        return None
    if not PHONE_PATTERN.match(value):
        errors.add(field, "Phone number is not a valid number.")
        return None
    return value


def validate_registration(payload: Any) -> dict[str, Any]:
    """Validate a registration body. Roles are intentionally not read here."""
    data = require_json(payload)
    errors = ValidationErrors()

    # Password is read raw: stripping it would silently change what the user
    # typed, and leading/trailing spaces are legitimate password characters.
    raw_password = payload.get("password")
    password = raw_password if isinstance(raw_password, str) else ""

    result = {
        "email": validate_email(_as_text(data, "email"), errors),
        "password": validate_password(password, errors),
        "full_name": validate_full_name(_as_text(data, "full_name"), errors),
        "phone": validate_phone(_as_text(data, "phone"), errors),
    }
    errors.raise_if_any()
    return result


def validate_login(payload: Any) -> dict[str, str]:
    """Validate a login body.

    Only presence is checked. Applying the registration password rules here
    would tell an attacker which stored passwords are short.
    """
    data = require_json(payload)
    errors = ValidationErrors()

    email = _as_text(data, "email")
    raw_password = data.get("password")
    password = raw_password if isinstance(raw_password, str) else ""

    if not email:
        errors.add("email", "Email is required.")
    if not password:
        errors.add("password", "Password is required.")
    errors.raise_if_any()

    return {"email": normalise_email(email), "password": password}


def validate_refresh(payload: Any) -> str:
    data = require_json(payload)
    token = _as_text(data, "refresh_token")
    if not token:
        errors = ValidationErrors()
        errors.add("refresh_token", "Refresh token is required.")
        errors.raise_if_any()
    return token
