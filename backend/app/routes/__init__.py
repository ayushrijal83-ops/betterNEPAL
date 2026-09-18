"""API v1 blueprint assembly.

Every versioned blueprint is registered on `api_v1`, which the application
factory mounts at the configured API prefix. Feature blueprints (auth, reports,
incidents, ...) are added here as their phases land.
"""
from flask import Blueprint

from .health import health_bp

api_v1 = Blueprint("api_v1", __name__)
api_v1.register_blueprint(health_bp)

__all__ = ["api_v1"]
