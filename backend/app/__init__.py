"""Better Nepal backend application factory."""
from __future__ import annotations

import logging

from flask import Flask
from werkzeug.exceptions import HTTPException

from .config import BaseConfig, get_config
from .config.settings import BACKEND_DIR
from .extensions import cors, db, migrate
from .routes import api_v1
from .utils.helpers import ApiError, error_response, success_response

MIGRATIONS_DIR = BACKEND_DIR / "migrations"


def create_app(config_name: str | None = None) -> Flask:
    """Build a configured Flask application.

    `config_name` overrides FLASK_ENV; tests pass "testing".
    """
    app = Flask(__name__, instance_relative_config=True)

    config: type[BaseConfig] = get_config(config_name)
    config.validate()
    app.config.from_object(config)

    _register_extensions(app)
    _register_database(app)
    _register_blueprints(app)
    _register_frontend(app)
    _register_error_handlers(app)
    _register_cli(app)
    _configure_logging(app)

    return app


def _register_frontend(app: Flask) -> None:
    """Serve ../frontend in development only.

    Convenience, not deployment. Flask's development server is single-threaded
    and does no caching or compression, so in production a real web server
    sits in front of these files - which is why this is gated on DEBUG rather
    than simply always on.

    The benefit of having it is that the frontend and API then share an origin,
    so the browser makes no preflight request and CORS stops being something
    that has to be right before anything works at all.
    """
    if not app.config.get("DEBUG"):
        return

    frontend_dir = BACKEND_DIR.parent / "frontend"
    if not frontend_dir.is_dir():
        return

    from flask import send_from_directory

    @app.get("/")
    def _frontend_index():
        return send_from_directory(frontend_dir, "index.html")

    @app.get("/<path:filename>")
    def _frontend_file(filename: str):
        """Serve a frontend file.

        ``send_from_directory`` rejects any path that escapes the directory, so
        a request for ``../backend/.env`` is refused rather than served.
        """
        target = frontend_dir / filename
        if target.is_dir():
            filename = f"{filename.rstrip('/')}/index.html"
        return send_from_directory(frontend_dir, filename)


def _register_extensions(app: Flask) -> None:
    cors.init_app(
        app,
        resources={f"{app.config['API_PREFIX']}/*": {"origins": app.config["CORS_ORIGINS"]}},
        supports_credentials=True,
    )


def _register_database(app: Flask) -> None:
    """Bind SQLAlchemy and Alembic. No connection is opened here."""
    # Importing the package populates Base.metadata, which is what Alembic
    # autogenerates against. Every new model module must be imported from
    # app/models/__init__.py or its table will be missing from migrations.
    from . import models  # noqa: F401

    db.init_app(app)
    # Absolute path so `flask db ...` behaves the same from any working directory.
    migrate.init_app(app, db, directory=str(MIGRATIONS_DIR))


def _register_cli(app: Flask) -> None:
    """Attach `flask seed ...` commands."""
    from .seed import register_seed_commands

    register_seed_commands(app)


def _register_blueprints(app: Flask) -> None:
    app.register_blueprint(api_v1, url_prefix=app.config["API_PREFIX"])

    # Development-only smoke endpoint kept from the original scaffold.
    @app.get("/api/hello")
    def hello():
        return success_response({"message": "Hello from Better Nepal backend!"})


def _register_error_handlers(app: Flask) -> None:
    """Return JSON for every failure, and never leak internals."""

    @app.errorhandler(ApiError)
    def handle_api_error(exc: ApiError):
        return error_response(exc.message, exc.status, exc.code, exc.details)

    @app.errorhandler(HTTPException)
    def handle_http_exception(exc: HTTPException):
        return error_response(
            exc.description or exc.name,
            exc.code or 500,
            code=exc.name.lower().replace(" ", "_"),
        )

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        app.logger.exception("Unhandled exception: %s", exc)
        if app.config["DEBUG"]:
            raise exc
        return error_response("An internal error occurred.", 500, code="internal_error")


def _configure_logging(app: Flask) -> None:
    if not app.config["TESTING"]:
        logging.basicConfig(
            level=logging.DEBUG if app.config["DEBUG"] else logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )


__all__ = ["create_app"]
