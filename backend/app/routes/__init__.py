"""API v1 blueprint assembly.

Every versioned blueprint is registered on `api_v1`, which the application
factory mounts at the configured API prefix. Feature blueprints (reports,
incidents, ...) are added here as their phases land.
"""
from flask import Blueprint

from .auth import auth_bp
from .health import health_bp
from .incidents import incidents_bp
from .map import map_bp
from .reports import reports_bp

api_v1 = Blueprint("api_v1", __name__)
api_v1.register_blueprint(health_bp)
api_v1.register_blueprint(auth_bp)
api_v1.register_blueprint(map_bp)
api_v1.register_blueprint(reports_bp)
api_v1.register_blueprint(incidents_bp)

__all__ = ["api_v1"]
