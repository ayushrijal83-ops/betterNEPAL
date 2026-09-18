"""Flask extension singletons.

Instantiated here without an app, then bound in the application factory.
"""
from flask_cors import CORS
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

from .models.base import Base

# Flask-SQLAlchemy 3.1 wraps an existing DeclarativeBase rather than generating
# its own, which keeps the model definitions plain SQLAlchemy 2.0.
db = SQLAlchemy(model_class=Base)
migrate = Migrate()
cors = CORS()

__all__ = ["cors", "db", "migrate"]
