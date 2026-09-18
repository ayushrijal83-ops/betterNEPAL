"""Enumerated vocabularies for the domain models.

These are Python ``enum.Enum`` classes, but they are mapped to the database as
``VARCHAR + CHECK`` rather than a native PostgreSQL ``ENUM`` type - see
:func:`enum_column`. Two reasons:

* Adding a value to a native PG enum needs ``ALTER TYPE``, which historically
  could not run inside a transaction and makes migrations awkward. This domain
  will certainly grow new report categories.
* It matches how the rest of this codebase already constrains vocabularies
  (``ck_municipalities_type``, ``ck_districts_verification_status``), and it
  works identically on SQLite, so the test suite keeps running without
  PostgreSQL.

The stored value is the enum's ``value`` (a lowercase string), which is also
what the API accepts and returns.
"""
from __future__ import annotations

import enum

from sqlalchemy import Enum as SAEnum


class ReportStatus(str, enum.Enum):
    """Lifecycle of a citizen report.

    ``VERIFIED_AS_INCIDENT`` is the terminal success state for this phase: a
    human has confirmed the report and it becomes an Incident in Phase 6.
    """

    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    VERIFIED_AS_INCIDENT = "verified_as_incident"
    REJECTED = "rejected"


class ReportCategory(str, enum.Enum):
    """What kind of problem the citizen is reporting."""

    ROAD_DAMAGE = "road_damage"
    WATER_LEAK = "water_leak"
    WASTE_MANAGEMENT = "waste_management"
    ELECTRICITY = "electricity"
    PUBLIC_PROPERTY = "public_property"
    NATURAL_DISASTER = "natural_disaster"
    OTHER = "other"


class IncidentSeverity(str, enum.Enum):
    """How urgent a verified incident is.

    Set by the verifying human, not inferred: severity drives who gets woken
    up, and a wrong guess is worse than an explicit judgement.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentStatus(str, enum.Enum):
    """Lifecycle of a verified incident, from confirmed to finished."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class GovernmentLevel(str, enum.Enum):
    """Which tier of government owns a responsibility.

    Nepal's federal restructuring split infrastructure ownership three ways -
    national highways to the federal Department of Roads, provincial roads to
    the provinces, local roads to municipalities - and the project's research
    files describe exactly that split. ``UTILITY`` covers service providers
    that are not a tier of government at all (NEA, water boards), which is why
    it sits alongside rather than inside the others.
    """

    FEDERAL = "federal"
    PROVINCIAL = "provincial"
    LOCAL = "local"
    UTILITY = "utility"


class AuthorityType(str, enum.Enum):
    """What kind of body this is, independent of its tier."""

    DEPARTMENT_OF_ROADS = "department_of_roads"
    MUNICIPAL_OFFICE = "municipal_office"
    WATER_AUTHORITY = "water_authority"
    ELECTRICITY_AUTHORITY = "electricity_authority"
    OTHER = "other"


def enum_column(enum_class: type[enum.Enum], name: str) -> SAEnum:
    """Map an enum to VARCHAR + a named CHECK constraint.

    ``native_enum=False`` keeps the column portable; ``values_callable`` stores
    the enum's value rather than its member name, so the database holds
    ``'road_damage'`` and not ``'ROAD_DAMAGE'``.
    """
    return SAEnum(
        enum_class,
        name=name,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


def parse_enum(enum_class: type[enum.Enum], value: object) -> enum.Enum | None:
    """Coerce untrusted input to an enum member, or None if it does not match.

    Accepts the stored value (``"road_damage"``) case-insensitively, and also
    the member name (``"ROAD_DAMAGE"``), since clients reasonably send either.
    """
    if isinstance(value, enum_class):
        return value
    if not isinstance(value, str):
        return None

    candidate = value.strip()
    if not candidate:
        return None

    for member in enum_class:
        if candidate.lower() == member.value.lower() or candidate.upper() == member.name:
            return member
    return None


def enum_values(enum_class: type[enum.Enum]) -> list[str]:
    """The accepted API values, for error messages and documentation."""
    return [member.value for member in enum_class]
