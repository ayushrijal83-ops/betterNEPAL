"""Liveness / readiness endpoints."""
from flask import Blueprint, current_app

from ..utils.helpers import success_response

health_bp = Blueprint("health", __name__)


@health_bp.get("/health")
def health():
    return success_response(
        {
            "service": "better-nepal-backend",
            "status": "ok",
            "environment": current_app.config["ENV_NAME"],
            "api_version": "v1",
        }
    )
