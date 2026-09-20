"""Flask extension singletons.

Instantiated here without an app, then bound in the application factory.
"""
import sqlite3

from flask_cors import CORS
from flask_limiter import Limiter
from flask_migrate import Migrate
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

from .models.base import Base

# Flask-SQLAlchemy 3.1 wraps an existing DeclarativeBase rather than generating
# its own, which keeps the model definitions plain SQLAlchemy 2.0.
db = SQLAlchemy(model_class=Base)
migrate = Migrate()
cors = CORS()
socketio = SocketIO(
    cors_allowed_origins="*",
    async_mode="threading",
    logger=False,
    engineio_logger=False,
)


def rate_limit_key() -> str:
    """Who a rate limit counts against.

    The authenticated user where there is one, so a shared office NAT does not
    lock out a whole department because one person is busy. Otherwise the
    client IP, which is all an anonymous caller has.

    Note the trust boundary: ``remote_addr`` is the peer address. Behind a
    reverse proxy every request appears to come from the proxy, so the limit
    becomes global rather than per-client. Fixing that needs ProxyFix with a
    trusted hop count - not a header the caller can forge.
    """
    from flask import g, request

    user = g.get("current_user")
    if user is not None:
        return f"user:{user.id}"
    return f"ip:{request.remote_addr or 'unknown'}"


limiter = Limiter(
    key_func=rate_limit_key,
    # Nothing is limited unless a route says so. A blanket default would throttle
    # the public map feed a dashboard polls, which is not what this is for.
    default_limits=[],
)


@event.listens_for(Engine, "connect")
def _enforce_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    """SQLite ignores FOREIGN KEY constraints unless asked not to.

    Without this the test suite would silently accept rows that PostgreSQL
    would reject, so referential-integrity tests would prove nothing. No-op on
    every other backend.
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


__all__ = ["cors", "db", "limiter", "migrate", "rate_limit_key", "socketio"]
