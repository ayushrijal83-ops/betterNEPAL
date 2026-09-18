"""Environment-driven configuration objects.

Values come from the process environment (loaded from .env in development).
Nothing secret is ever hardcoded here.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2]

# Loaded once, at import time, so config classes can read os.environ below.
load_dotenv(BACKEND_DIR / ".env")


# RFC 7518 section 3.2: an HS256 key should be at least as long as the hash
# output. Shorter keys weaken every token the platform issues.
MIN_SIGNING_KEY_BYTES = 32


def _split_origins(raw: str) -> list[str]:
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _postgres_url() -> str:
    """Build the PostgreSQL URI from the environment.

    A full ``DATABASE_URL`` wins when present; otherwise the URI is assembled
    from the ``POSTGRES_*`` parts. Only the password is genuinely secret, so it
    is the one component without a usable default — the rest fall back to
    conventional local values. The password is URL-quoted because real
    passwords contain characters that would otherwise break the URI.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        # Normalise the legacy "postgres://" scheme some providers still emit,
        # and pin the driver so SQLAlchemy does not guess.
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg2://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return url

    user = quote_plus(os.environ.get("POSTGRES_USER", "postgres"))
    password = quote_plus(os.environ.get("POSTGRES_PASSWORD", ""))
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    name = os.environ.get("POSTGRES_DB", "betternepal")
    credentials = f"{user}:{password}" if password else user
    return f"postgresql+psycopg2://{credentials}@{host}:{port}/{name}"


class BaseConfig:
    ENV_NAME = "base"
    DEBUG = False
    TESTING = False

    API_PREFIX = "/api/v1"
    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    CORS_ORIGINS = _split_origins(os.environ.get("CORS_ORIGINS", ""))

    # Werkzeug rejects larger bodies before they reach a view.
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024

    SQLALCHEMY_DATABASE_URI = _postgres_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # --- Authentication ---------------------------------------------------
    # Falls back to SECRET_KEY so a single secret is enough to run, while
    # still allowing the signing key to be rotated independently.
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "") or SECRET_KEY
    JWT_ALGORITHM = "HS256"
    ACCESS_TOKEN_TTL_MINUTES = int(os.environ.get("ACCESS_TOKEN_TTL_MINUTES", "15"))
    REFRESH_TOKEN_TTL_DAYS = int(os.environ.get("REFRESH_TOKEN_TTL_DAYS", "30"))

    @classmethod
    def validate(cls) -> None:
        """Fail fast on an unusable configuration."""
        if not cls.SECRET_KEY:
            raise RuntimeError("SECRET_KEY is not set; refusing to start.")
        if not cls.SQLALCHEMY_DATABASE_URI:
            raise RuntimeError("No database URI configured; refusing to start.")
        if not cls.JWT_SECRET_KEY:
            raise RuntimeError("JWT_SECRET_KEY is not set; refusing to start.")


class DevelopmentConfig(BaseConfig):
    ENV_NAME = "development"
    DEBUG = True
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-secret-key")
    CORS_ORIGINS = _split_origins(os.environ.get("CORS_ORIGINS", "*")) or ["*"]
    SQLALCHEMY_ECHO = os.environ.get("SQLALCHEMY_ECHO", "").lower() == "true"
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "") or SECRET_KEY


class TestingConfig(BaseConfig):
    ENV_NAME = "testing"
    TESTING = True
    # >= 32 bytes so HS256 signing matches production constraints (RFC 7518).
    SECRET_KEY = "testing-secret-key-not-used-outside-tests"
    CORS_ORIGINS = ["*"]

    # In-memory SQLite keeps the suite runnable without PostgreSQL/PostGIS.
    # Flask-SQLAlchemy applies a StaticPool here, so one test app == one
    # isolated database that disappears when the app is discarded.
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS: dict = {}
    JWT_SECRET_KEY = SECRET_KEY


class ProductionConfig(BaseConfig):
    ENV_NAME = "production"

    @classmethod
    def validate(cls) -> None:
        super().validate()
        if cls.SECRET_KEY.startswith(("dev-", "change-me", "testing-")):
            raise RuntimeError("SECRET_KEY looks like a placeholder; set a real one.")
        if not cls.CORS_ORIGINS or "*" in cls.CORS_ORIGINS:
            raise RuntimeError("CORS_ORIGINS must list explicit origins in production.")
        if not cls.SQLALCHEMY_DATABASE_URI.startswith("postgresql"):
            raise RuntimeError("Production requires a PostgreSQL database URI.")
        if cls.JWT_SECRET_KEY.startswith(("dev-", "change-me", "testing-")):
            raise RuntimeError("JWT_SECRET_KEY looks like a placeholder; set a real one.")
        if len(cls.JWT_SECRET_KEY.encode()) < MIN_SIGNING_KEY_BYTES:
            raise RuntimeError(
                f"JWT_SECRET_KEY must be at least {MIN_SIGNING_KEY_BYTES} bytes "
                "for HS256 (RFC 7518 section 3.2)."
            )


CONFIGS: dict[str, type[BaseConfig]] = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def get_config(name: str | None = None) -> type[BaseConfig]:
    """Resolve a config class by name, falling back to FLASK_ENV then development."""
    key = (name or os.environ.get("FLASK_ENV") or "development").lower()
    if key not in CONFIGS:
        raise ValueError(f"Unknown config '{key}'. Expected one of {sorted(CONFIGS)}.")
    return CONFIGS[key]
