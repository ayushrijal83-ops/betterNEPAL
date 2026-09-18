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


# --- reports ---------------------------------------------------------------

TITLE_MIN_LENGTH = 5
TITLE_MAX_LENGTH = 100
DESCRIPTION_MIN_LENGTH = 10
DESCRIPTION_MAX_LENGTH = 5000


def validate_enum_field(
    value: Any,
    enum_class: type,
    errors: ValidationErrors,
    field: str,
    required: bool = True,
):
    """Coerce a request value to an enum member, recording an error if it fails.

    The error message lists the accepted values, because a client that sent the
    wrong one cannot guess the vocabulary from a bare rejection.
    """
    from ..models.enums import enum_values, parse_enum

    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            errors.add(field, f"{field} is required.")
        return None

    member = parse_enum(enum_class, value)
    if member is None:
        errors.add(
            field, f"{field} must be one of: {', '.join(enum_values(enum_class))}."
        )
        return None
    return member


def validate_coordinates(
    payload: dict[str, Any],
    errors: ValidationErrors,
    lat_field: str = "lat",
    lng_field: str = "lng",
):
    """Validate a lat/lng pair into a :class:`Coordinates`.

    Delegates to ``app/gis/location.py`` rather than re-checking ranges here,
    so there is exactly one definition of a valid coordinate in the codebase.
    """
    from ..gis.location import Coordinates, InvalidCoordinate

    latitude = payload.get(lat_field)
    longitude = payload.get(lng_field)
    if longitude is None:
        longitude = payload.get("lon")
    if longitude is None:
        longitude = payload.get("longitude")
    if latitude is None:
        latitude = payload.get("latitude")

    try:
        return Coordinates.parse(latitude=latitude, longitude=longitude)
    except InvalidCoordinate as exc:
        message = str(exc)
        # Attribute the failure to whichever field it actually concerns.
        field = lat_field if "latitude" in message else lng_field
        errors.add(field, message)
        return None


def validate_report_creation(payload: Any) -> dict[str, Any]:
    """Validate a report submission body.

    ``status``, ``district_id`` and ``municipality_id`` are deliberately not
    read: status is server-controlled, and the geographic fields are resolved
    by reverse geocoding rather than trusted from the client.
    """
    from ..models.enums import ReportCategory

    data = require_json(payload)
    errors = ValidationErrors()

    title = _as_text(data, "title")
    if not title:
        errors.add("title", "title is required.")
    elif not (TITLE_MIN_LENGTH <= len(title) <= TITLE_MAX_LENGTH):
        errors.add(
            "title",
            f"title must be between {TITLE_MIN_LENGTH} and {TITLE_MAX_LENGTH} characters.",
        )

    description = _as_text(data, "description")
    if not description:
        errors.add("description", "description is required.")
    elif not (DESCRIPTION_MIN_LENGTH <= len(description) <= DESCRIPTION_MAX_LENGTH):
        errors.add(
            "description",
            f"description must be between {DESCRIPTION_MIN_LENGTH} and "
            f"{DESCRIPTION_MAX_LENGTH} characters.",
        )

    category = validate_enum_field(data.get("category"), ReportCategory, errors, "category")
    coordinates = validate_coordinates(data, errors)

    errors.raise_if_any()
    return {
        "title": title,
        "description": description,
        "category": category,
        "coordinates": coordinates,
    }


def validate_status_update(payload: Any) -> Any:
    """Validate a status-change body."""
    from ..models.enums import ReportStatus

    data = require_json(payload)
    errors = ValidationErrors()
    status = validate_enum_field(data.get("status"), ReportStatus, errors, "status")
    errors.raise_if_any()
    return status
