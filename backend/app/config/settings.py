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

    # --- Uploads ----------------------------------------------------------
    # The limit that matters to a user is the file's own size.
    MAX_UPLOAD_BYTES = 10 * 1024 * 1024

    # Werkzeug rejects larger bodies before they reach a view, so this sits
    # deliberately above MAX_UPLOAD_BYTES: a request carrying a 10MB file also
    # carries multipart boundaries and headers, and if the two limits were
    # equal an exactly-at-limit upload would die as an opaque 413 instead of
    # reaching the validator that can explain what was wrong.
    MAX_CONTENT_LENGTH = MAX_UPLOAD_BYTES + (2 * 1024 * 1024)

    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "") or str(BACKEND_DIR / "uploads")

    # --- AI ---------------------------------------------------------------
    # Absent by design in development and test: every AI entry point degrades
    # to a clear "not configured" response rather than failing at import, so
    # the platform runs perfectly well with no LLM provider at all.
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
    AI_REQUEST_TIMEOUT_SECONDS = int(os.environ.get("AI_REQUEST_TIMEOUT_SECONDS", "30"))
    # Metres to search when looking for duplicate reports.
    AI_DUPLICATE_RADIUS_METRES = int(os.environ.get("AI_DUPLICATE_RADIUS_METRES", "100"))

    # --- Ollama (self-hosted, preferred over Gemini when configured) -------
    # Three roles, so work lands on hardware suited to it: vision and long-form
    # reasoning on the GPU box, cheap classification and embeddings on the CPU
    # box. Any of them may be blank - the service degrades per role rather than
    # failing wholesale, so a text node alone still classifies reports.
    OLLAMA_VISION_NODE = os.environ.get("OLLAMA_VISION_NODE", "").rstrip("/")
    OLLAMA_TEXT_NODE = os.environ.get("OLLAMA_TEXT_NODE", "").rstrip("/")
    OLLAMA_EMBED_NODE = os.environ.get("OLLAMA_EMBED_NODE", "").rstrip("/")

    # --- AI Disaster Dispatch (Victus node) -------------------------------
    # Dedicated endpoint for structured disaster triage. If not configured,
    # the system degrades gracefully: reports still save, no dispatch occurs.
    # Use a separate node so disaster classification does not contend with
    # chat/assistant traffic on the vision node.
    OLLAMA_DISPATCH_NODE = os.environ.get("OLLAMA_DISPATCH_NODE", "").rstrip("/")
    OLLAMA_DISPATCH_MODEL = os.environ.get("OLLAMA_DISPATCH_MODEL", "qwen2.5:3b")
    AI_DISPATCH_ENABLED = os.environ.get("AI_DISPATCH_ENABLED", "true").lower() != "false"
    AI_DISPATCH_TIMEOUT_SECONDS = int(os.environ.get("AI_DISPATCH_TIMEOUT_SECONDS", "30"))
    # Minimum confidence for the backend to consider immediate dispatch.
    # The model's own requires_immediate_dispatch flag is NOT sufficient.
    AI_DISPATCH_MIN_CONFIDENCE = float(os.environ.get("AI_DISPATCH_MIN_CONFIDENCE", "0.85"))
    # Minimum severity that qualifies for dispatch consideration.
    AI_DISPATCH_MIN_SEVERITY = os.environ.get("AI_DISPATCH_MIN_SEVERITY", "HIGH")
    # Radius (metres) to search for existing incidents to correlate with.
    AI_DISPATCH_CORRELATION_RADIUS_METRES = int(os.environ.get("AI_DISPATCH_CORRELATION_RADIUS_METRES", "200"))
    # Time window (hours) for correlation - reports within this window of an
    # existing active incident are candidates for attachment.
    AI_DISPATCH_CORRELATION_WINDOW_HOURS = int(os.environ.get("AI_DISPATCH_CORRELATION_WINDOW_HOURS", "24"))

    # The GPU node currently serves the assistant, so its model must be a good
    # general-purpose one. llava is an image model: excellent at describing a
    # photo, poor at writing a sentence for a citizen. When image analysis is
    # actually implemented it needs its own setting rather than borrowing this.
    OLLAMA_VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", "qwen2.5:7b")
    OLLAMA_TEXT_MODEL = os.environ.get("OLLAMA_TEXT_MODEL", "qwen2.5:3b")
    OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")

    # A local model on a CPU box is slower than a hosted API, so the timeout is
    # generous. It is still finite: a hung node must not hold a worker forever.
    OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "120"))
    # How long a node that failed its health check is left alone before being
    # probed again, so one unreachable machine does not add its connect timeout
    # to every request.
    OLLAMA_HEALTH_TTL_SECONDS = int(os.environ.get("OLLAMA_HEALTH_TTL_SECONDS", "60"))

    # --- Rate limiting ----------------------------------------------------
    RATELIMIT_ENABLED = os.environ.get("RATELIMIT_ENABLED", "true").lower() != "false"
    # Default is in-process memory. That is correct for one worker and WRONG
    # for several: with `gunicorn -w 4` each worker keeps its own counters, so
    # a "10 per minute" limit actually admits 40. Set RATELIMIT_STORAGE_URI to
    # a shared redis:// before running more than one worker.
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")

    # --- Refresh-token cookie ---------------------------------------------
    # Secure defaults to on, and MUST stay on in production: without it the
    # browser will send the refresh token over plain HTTP. It is off in
    # development only because localhost is not HTTPS, and a Secure cookie
    # there is silently never sent, which looks like a broken login.
    REFRESH_COOKIE_NAME = os.environ.get("REFRESH_COOKIE_NAME", "bn_refresh_token")
    REFRESH_COOKIE_SECURE = os.environ.get("REFRESH_COOKIE_SECURE", "true").lower() != "false"
    REFRESH_COOKIE_SAMESITE = os.environ.get("REFRESH_COOKIE_SAMESITE", "Lax")
    REFRESH_COOKIE_PATH = "/api/v1/auth"

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
    # localhost is plain HTTP; a Secure cookie there is silently dropped.
    REFRESH_COOKIE_SECURE = os.environ.get("REFRESH_COOKIE_SECURE", "false").lower() == "true"


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

    # Off by default: the existing suite logs in hundreds of times, and a
    # 10/minute cap would turn passing tests into 429s that look like auth
    # bugs. The hardening tests switch it back on for the cases that need it.
    RATELIMIT_ENABLED = False

    # Force get_ai_service() to fall through to NullAIService regardless of
    # what the real OLLAMA_*/GEMINI_API_KEY env vars are set to on this
    # machine (BaseConfig reads them from the process environment). A test
    # that wants a real provider installs one explicitly via set_ai_service();
    # everything else must stay hermetic, per the project rule that tests
    # never make live network calls. Scoped to the citizen chat/analysis
    # provider only - OLLAMA_DISPATCH_NODE (disaster AI) is untouched here.
    OLLAMA_VISION_NODE = ""
    OLLAMA_TEXT_NODE = ""
    OLLAMA_EMBED_NODE = ""
    GEMINI_API_KEY = ""
    REFRESH_COOKIE_SECURE = False


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
        if not cls.REFRESH_COOKIE_SECURE:
            raise RuntimeError(
                "REFRESH_COOKIE_SECURE must stay enabled in production; "
                "without it the refresh token is sent over plain HTTP."
            )
        if cls.JWT_SECRET_KEY.startswith(("dev-", "change-me", "testing-")):
            raise RuntimeError("JWT_SECRET_KEY looks like a placeholder; set a real one.")
        if len(cls.JWT_SECRET_KEY.encode()) < MIN_SIGNING_KEY_BYTES:
            raise RuntimeError(
                f"JWT_SECRET_KEY must be at least {MIN_SIGNING_KEY_BYTES} bytes "
                "for HS256 (RFC 7518 section 3.2)."
            )
        if cls.RATELIMIT_ENABLED and cls.RATELIMIT_STORAGE_URI.startswith("memory://"):
            raise RuntimeError(
                "RATELIMIT_STORAGE_URI must not be 'memory://' in production; "
                "set a shared redis:// URI for multi-worker rate limiting."
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
