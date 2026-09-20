"""Emergency contacts: district-level (DEOC/police/hospital) and national.

Centralised so the national toll-free number lives in exactly one place
instead of being copy-pasted into every component that needs it (see
``get_national_contacts``) - the platform's own Phase 19 instruction, made
necessary by the same DEOC number appearing in three different places in a
prior draft of this feature.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from ..extensions import db
from ..models.district import District
from ..models.district_extras import EMERGENCY_CONTACT_KINDS, DistrictEmergencyContact
from ..utils.helpers import ApiError

# Ministry of Home Affairs National Emergency Operation Center toll-free
# number. Not district data - kept here, once, rather than hardcoded per
# component (spec's own instruction).
NATIONAL_EMERGENCY_PHONE = "1112"
NATIONAL_EMERGENCY_NAME = "National Emergency Operation Center (MoHA)"


def get_national_contacts() -> dict[str, Any]:
    return {
        "name": NATIONAL_EMERGENCY_NAME,
        "phone": NATIONAL_EMERGENCY_PHONE,
        "provenance": {
            "source": "Ministry of Home Affairs",
            "source_url": None,
            "source_type": "official_dataset",
            "verification_status": "official_source",
        },
    }


def get_district_contacts(district_id: Any) -> dict[str, Any]:
    """DEOC/police/hospital for one district.

    Every kind in ``EMERGENCY_CONTACT_KINDS`` always appears in the result,
    even when no row exists - an absent kind reads as
    ``verification_status: "unverified"`` with ``phone: None``, never a
    missing key a caller could mistake for "this feature has no contacts".
    """
    try:
        parsed = uuid.UUID(str(district_id))
    except (ValueError, AttributeError, TypeError):
        raise ApiError(
            "district_id is not a valid identifier.", status=400, code="invalid_identifier"
        ) from None

    district = db.session.get(District, parsed)
    if district is None:
        raise ApiError("District not found.", status=404, code="district_not_found")

    rows = db.session.scalars(
        select(DistrictEmergencyContact).where(
            DistrictEmergencyContact.district_id == parsed
        )
    ).all()
    by_kind = {row.kind: row for row in rows}

    contacts = {}
    for kind in EMERGENCY_CONTACT_KINDS:
        row = by_kind.get(kind)
        if row is None:
            contacts[kind] = {
                "name": None,
                "phone": None,
                "provenance": {
                    "source": None,
                    "source_url": None,
                    "source_type": None,
                    "verification_status": "unverified",
                    "last_verified_at": None,
                },
            }
        else:
            contacts[kind] = {
                "name": row.name,
                "phone": row.phone,
                "provenance": row.provenance_dict(),
            }

    return {
        "district_id": str(district.id),
        "district": district.name,
        "contacts": contacts,
        "national": get_national_contacts(),
    }
