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


# --- incidents -------------------------------------------------------------

INCIDENT_TITLE_MIN_LENGTH = 5
INCIDENT_TITLE_MAX_LENGTH = 150


def validate_incident_from_report(payload: Any) -> dict[str, Any]:
    """Validate a promotion request.

    Only ``report_id`` and the verifier's judgement are read. Category,
    coordinates and administrative area are inherited from the report by the
    service, so a caller cannot make an incident disagree with its evidence.
    """
    from ..models.enums import IncidentSeverity

    data = require_json(payload)
    errors = ValidationErrors()

    report_id = _as_text(data, "report_id")
    if not report_id:
        errors.add("report_id", "report_id is required.")

    severity = validate_enum_field(
        data.get("severity"), IncidentSeverity, errors, "severity", required=False
    )

    title = _as_text(data, "title") or None
    if title and not (INCIDENT_TITLE_MIN_LENGTH <= len(title) <= INCIDENT_TITLE_MAX_LENGTH):
        errors.add(
            "title",
            f"title must be between {INCIDENT_TITLE_MIN_LENGTH} and "
            f"{INCIDENT_TITLE_MAX_LENGTH} characters.",
        )

    description = _as_text(data, "description") or None
    if description and len(description) < DESCRIPTION_MIN_LENGTH:
        errors.add(
            "description",
            f"description must be at least {DESCRIPTION_MIN_LENGTH} characters.",
        )

    errors.raise_if_any()
    return {
        "report_id": report_id,
        "severity": severity,
        "title": title,
        "description": description,
    }


def validate_link_report(payload: Any) -> str:
    """Validate a request to attach a report to an existing incident."""
    data = require_json(payload)
    errors = ValidationErrors()

    report_id = _as_text(data, "report_id")
    if not report_id:
        errors.add("report_id", "report_id is required.")

    errors.raise_if_any()
    return report_id


def validate_incident_update(payload: Any) -> dict[str, Any]:
    """Validate a status/severity change.

    Both fields are optional individually, but the service rejects a body that
    carries neither - an update that updates nothing is a client bug worth
    surfacing.
    """
    from ..models.enums import IncidentSeverity, IncidentStatus

    data = require_json(payload)
    errors = ValidationErrors()

    status = validate_enum_field(
        data.get("status"), IncidentStatus, errors, "status", required=False
    )
    severity = validate_enum_field(
        data.get("severity"), IncidentSeverity, errors, "severity", required=False
    )

    errors.raise_if_any()
    return {"status": status, "severity": severity}


# --- authorities -----------------------------------------------------------

AUTHORITY_NAME_MIN_LENGTH = 3
AUTHORITY_NAME_MAX_LENGTH = 150
CONTACT_EMAIL_MAX_LENGTH = 120
CONTACT_PHONE_MAX_LENGTH = 50

# Sentinel distinguishing "key absent" from "key present and null". Clearing a
# stale phone number needs the latter; a plain `.get()` cannot tell them apart.
UNSET = object()


def _validate_contact_email(data: dict[str, Any], errors: ValidationErrors):
    if "contact_email" not in data:
        return UNSET
    raw = data.get("contact_email")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    value = raw.strip() if isinstance(raw, str) else ""
    if len(value) > CONTACT_EMAIL_MAX_LENGTH:
        errors.add(
            "contact_email",
            f"contact_email must be at most {CONTACT_EMAIL_MAX_LENGTH} characters.",
        )
        return None
    if not EMAIL_PATTERN.match(value):
        errors.add("contact_email", "contact_email is not a valid address.")
        return None
    return value.lower()


def _validate_contact_phone(data: dict[str, Any], errors: ValidationErrors):
    if "contact_phone" not in data:
        return UNSET
    raw = data.get("contact_phone")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    value = raw.strip() if isinstance(raw, str) else ""
    if len(value) > CONTACT_PHONE_MAX_LENGTH:
        errors.add(
            "contact_phone",
            f"contact_phone must be at most {CONTACT_PHONE_MAX_LENGTH} characters.",
        )
        return None
    if not PHONE_PATTERN.match(value):
        errors.add("contact_phone", "contact_phone is not a valid number.")
        return None
    return value


def validate_authority_creation(payload: Any) -> dict[str, Any]:
    """Validate a new authority.

    ``district_id`` is optional: a genuinely national body has no district, and
    requiring one would mean inventing a fact.
    """
    from ..models.enums import AuthorityType, GovernmentLevel

    data = require_json(payload)
    errors = ValidationErrors()

    name = _as_text(data, "name")
    if not name:
        errors.add("name", "name is required.")
    elif not (AUTHORITY_NAME_MIN_LENGTH <= len(name) <= AUTHORITY_NAME_MAX_LENGTH):
        errors.add(
            "name",
            f"name must be between {AUTHORITY_NAME_MIN_LENGTH} and "
            f"{AUTHORITY_NAME_MAX_LENGTH} characters.",
        )

    level = validate_enum_field(data.get("level"), GovernmentLevel, errors, "level")
    authority_type = validate_enum_field(data.get("type"), AuthorityType, errors, "type")

    contact_email = _validate_contact_email(data, errors)
    contact_phone = _validate_contact_phone(data, errors)

    errors.raise_if_any()
    return {
        "name": name,
        "level": level,
        "type": authority_type,
        "contact_email": None if contact_email is UNSET else contact_email,
        "contact_phone": None if contact_phone is UNSET else contact_phone,
        "district_id": _as_text(data, "district_id") or None,
    }


def validate_authority_update(payload: Any) -> dict[str, Any]:
    """Validate a partial authority update.

    Only keys actually present are returned, so an update never silently
    blanks a field the caller did not mention.
    """
    from ..models.enums import AuthorityType, GovernmentLevel

    data = require_json(payload)
    errors = ValidationErrors()
    result: dict[str, Any] = {}

    if "name" in data:
        name = _as_text(data, "name")
        if not (AUTHORITY_NAME_MIN_LENGTH <= len(name) <= AUTHORITY_NAME_MAX_LENGTH):
            errors.add(
                "name",
                f"name must be between {AUTHORITY_NAME_MIN_LENGTH} and "
                f"{AUTHORITY_NAME_MAX_LENGTH} characters.",
            )
        else:
            result["name"] = name

    if "level" in data:
        result["level"] = validate_enum_field(
            data.get("level"), GovernmentLevel, errors, "level"
        )
    if "type" in data:
        result["type"] = validate_enum_field(
            data.get("type"), AuthorityType, errors, "type"
        )

    contact_email = _validate_contact_email(data, errors)
    if contact_email is not UNSET:
        result["contact_email"] = contact_email
    contact_phone = _validate_contact_phone(data, errors)
    if contact_phone is not UNSET:
        result["contact_phone"] = contact_phone

    if "district_id" in data:
        result["district_id"] = _as_text(data, "district_id") or None

    if not result:
        errors.add("body", "Provide at least one field to update.")

    errors.raise_if_any()
    return result


def validate_assignment(payload: Any) -> str:
    """Validate a request to assign an incident to an authority."""
    data = require_json(payload)
    errors = ValidationErrors()

    authority_id = _as_text(data, "authority_id")
    if not authority_id:
        errors.add("authority_id", "authority_id is required.")

    errors.raise_if_any()
    return authority_id


# --- projects --------------------------------------------------------------

PROJECT_TITLE_MIN_LENGTH = 5
PROJECT_TITLE_MAX_LENGTH = 150
NOTES_MIN_LENGTH = 5
NOTES_MAX_LENGTH = 5000


def validate_date(
    value: Any, errors: ValidationErrors, field: str, required: bool = False
):
    """Parse an ISO ``YYYY-MM-DD`` date.

    Deliberately strict about the format: accepting several would mean guessing
    between day-first and month-first on ambiguous input, and silently storing
    the wrong date.
    """
    from datetime import date as date_cls

    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            errors.add(field, f"{field} is required.")
        return None

    if isinstance(value, date_cls):
        return value
    if not isinstance(value, str):
        errors.add(field, f"{field} must be a date in YYYY-MM-DD format.")
        return None

    try:
        return date_cls.fromisoformat(value.strip())
    except ValueError:
        errors.add(field, f"{field} must be a date in YYYY-MM-DD format.")
        return None


def validate_project_creation(payload: Any) -> dict[str, Any]:
    """Validate a new project.

    ``status`` and ``actual_end_date`` are not read: a project begins as
    PLANNED, and its completion date is stamped by the service when the work is
    actually declared done.
    """
    data = require_json(payload)
    errors = ValidationErrors()

    title = _as_text(data, "title")
    if not title:
        errors.add("title", "title is required.")
    elif not (PROJECT_TITLE_MIN_LENGTH <= len(title) <= PROJECT_TITLE_MAX_LENGTH):
        errors.add(
            "title",
            f"title must be between {PROJECT_TITLE_MIN_LENGTH} and "
            f"{PROJECT_TITLE_MAX_LENGTH} characters.",
        )

    description = _as_text(data, "description")
    if not description:
        errors.add("description", "description is required.")
    elif len(description) < DESCRIPTION_MIN_LENGTH:
        errors.add(
            "description",
            f"description must be at least {DESCRIPTION_MIN_LENGTH} characters.",
        )

    authority_id = _as_text(data, "authority_id")
    if not authority_id:
        errors.add("authority_id", "authority_id is required.")

    start_date = validate_date(data.get("start_date"), errors, "start_date")
    estimated_end_date = validate_date(
        data.get("estimated_end_date"), errors, "estimated_end_date"
    )
    if start_date and estimated_end_date and estimated_end_date < start_date:
        errors.add("estimated_end_date", "estimated_end_date cannot precede start_date.")

    errors.raise_if_any()
    return {
        "title": title,
        "description": description,
        "authority_id": authority_id,
        "incident_id": _as_text(data, "incident_id") or None,
        "start_date": start_date,
        "estimated_end_date": estimated_end_date,
    }


def validate_contractor_assignment(payload: Any) -> str:
    """Validate a request to award a project to a contractor."""
    data = require_json(payload)
    errors = ValidationErrors()

    contractor_id = _as_text(data, "contractor_id")
    if not contractor_id:
        errors.add("contractor_id", "contractor_id is required.")

    errors.raise_if_any()
    return contractor_id


def validate_progress_update(payload: Any) -> dict[str, Any]:
    """Validate a ledger entry.

    ``notes`` are mandatory even on a status change: a transition with no
    explanation is the opacity this ledger exists to remove.
    """
    from ..models.enums import ProjectStatus

    data = require_json(payload)
    errors = ValidationErrors()

    notes = _as_text(data, "notes")
    if not notes:
        errors.add("notes", "notes are required.")
    elif not (NOTES_MIN_LENGTH <= len(notes) <= NOTES_MAX_LENGTH):
        errors.add(
            "notes",
            f"notes must be between {NOTES_MIN_LENGTH} and {NOTES_MAX_LENGTH} characters.",
        )

    new_status = validate_enum_field(
        data.get("new_status"), ProjectStatus, errors, "new_status", required=False
    )

    errors.raise_if_any()
    return {"notes": notes, "new_status": new_status}
