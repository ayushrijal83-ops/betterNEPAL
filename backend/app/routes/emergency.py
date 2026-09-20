"""Emergency contact endpoints. Public - a citizen needs these unauthenticated."""
from __future__ import annotations

from flask import Blueprint

from ..services import emergency_contact_service
from ..utils.helpers import success_response

emergency_bp = Blueprint("emergency", __name__, url_prefix="/emergency")


@emergency_bp.get("/national")
def national_contacts():
    """The national emergency number (MoHA NEOC), centrally defined."""
    return success_response(emergency_contact_service.get_national_contacts())


@emergency_bp.get("/district/<district_id>")
def district_contacts(district_id: str):
    """DEOC/police/hospital for one district, always all three kinds present."""
    return success_response(emergency_contact_service.get_district_contacts(district_id))
