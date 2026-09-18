"""Flask extension singletons.

Instantiated here without an app, then bound in the application factory.
"""
import sqlite3

from flask_cors import CORS
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

from .models.base import Base

# Flask-SQLAlchemy 3.1 wraps an existing DeclarativeBase rather than generating
# its own, which keeps the model definitions plain SQLAlchemy 2.0.
db = SQLAlchemy(model_class=Base)
migrate = Migrate()
cors = CORS()


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


__all__ = ["cors", "db", "migrate"]
