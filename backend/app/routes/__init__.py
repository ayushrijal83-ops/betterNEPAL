"""API v1 blueprint assembly.

Every versioned blueprint is registered on `api_v1`, which the application
factory mounts at the configured API prefix. Feature blueprints (reports,
incidents, ...) are added here as their phases land.
"""
from flask import Blueprint

from .ai import ai_bp
from .analytics import analytics_bp
from .announcements import announcements_bp
from .auth import auth_bp
from .chat import chat_bp
from .authorities import authorities_bp
from .disaster import disaster_bp
from .health import health_bp
from .incidents import incidents_bp
from .map import map_bp
from .media import media_bp
from .projects import projects_bp
from .reports import reports_bp
from .travel import travel_bp

api_v1 = Blueprint("api_v1", __name__)
api_v1.register_blueprint(health_bp)
api_v1.register_blueprint(auth_bp)
api_v1.register_blueprint(map_bp)
api_v1.register_blueprint(reports_bp)
api_v1.register_blueprint(incidents_bp)
api_v1.register_blueprint(authorities_bp)
api_v1.register_blueprint(projects_bp)
api_v1.register_blueprint(media_bp)
api_v1.register_blueprint(ai_bp)
api_v1.register_blueprint(analytics_bp)
api_v1.register_blueprint(announcements_bp)
api_v1.register_blueprint(travel_bp)
api_v1.register_blueprint(chat_bp)
api_v1.register_blueprint(disaster_bp)

__all__ = ["api_v1"]
