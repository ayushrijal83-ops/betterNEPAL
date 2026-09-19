"""Liveness / readiness endpoints."""
from flask import Blueprint, current_app

from ..services.ai_service import get_ai_service
from ..utils.helpers import success_response

health_bp = Blueprint("health", __name__)


def _ai_status() -> dict:
    """Real provider/node status for the UI to show - never fabricated.

    ``OllamaService.health()`` already exists for operator diagnostics; this
    just surfaces it publicly. Falls back to the provider name alone for
    Gemini/Null, which have no per-node concept.
    """
    service = get_ai_service()
    if hasattr(service, "health"):
        return service.health()
    return {"provider": service.name, "available": getattr(service, "available", service.name != "null")}


@health_bp.get("/health")
def health():
    return success_response(
        {
            "service": "better-nepal-backend",
            "status": "ok",
            "environment": current_app.config["ENV_NAME"],
            "api_version": "v1",
            "ai": _ai_status(),
        }
    )
