"""Phase 2: database foundation."""
import time
import uuid
from datetime import datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app import create_app
from app.config import DevelopmentConfig, ProductionConfig
from app.config.settings import _postgres_url
from app.extensions import db as _db
from app.models import Base, BaseModel


class Widget(BaseModel):
    """Throwaway model used only to exercise the base.

    It registers `test_widgets` on Base.metadata, but only while this module is
    imported — the `flask db` CLI never imports the test suite, so it cannot
    leak into a generated migration.
    """

    __tablename__ = "test_widgets"

    label: Mapped[str] = mapped_column(sa.String(50), nullable=False)


def test_testing_config_uses_in_memory_sqlite(app):
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"
    assert app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] is False


def test_development_config_is_postgresql():
    assert DevelopmentConfig.SQLALCHEMY_DATABASE_URI.startswith("postgresql+psycopg2://")


def test_extension_is_registered_on_app(app):
    assert app.extensions["sqlalchemy"] is _db
    assert "migrate" in app.extensions


def test_database_connection_works(db):
    assert db.session.execute(sa.text("SELECT 1")).scalar() == 1


def test_create_all_builds_tables_from_metadata(db):
    assert "test_widgets" in sa.inspect(db.engine).get_table_names()


def test_base_model_columns_exist():
    columns = {column.name for column in Widget.__table__.columns}
    assert {"id", "created_at", "updated_at", "label"} <= columns


def test_insert_and_query_roundtrip(db):
    widget = Widget(label="pokhara")
    db.session.add(widget)
    db.session.commit()

    fetched = db.session.get(Widget, widget.id)
    assert fetched is not None
    assert fetched.label == "pokhara"
    assert isinstance(fetched.id, uuid.UUID)


def test_timestamps_are_populated_and_timezone_aware(db):
    widget = Widget(label="mustang")
    db.session.add(widget)
    db.session.commit()

    assert isinstance(widget.created_at, datetime)
    assert widget.created_at.tzinfo is not None
    assert widget.updated_at.tzinfo is not None


def test_updated_at_advances_on_update(db):
    widget = Widget(label="before")
    db.session.add(widget)
    db.session.commit()
    original = widget.updated_at

    time.sleep(0.01)  # clock granularity, so the comparison is not a coin flip
    widget.label = "after"
    db.session.commit()

    assert widget.updated_at > original


def test_ids_are_unique_per_row(db):
    widgets = [Widget(label=f"w{n}") for n in range(3)]
    db.session.add_all(widgets)
    db.session.commit()

    assert len({widget.id for widget in widgets}) == 3


def _widget_count(db) -> int:
    return db.session.execute(sa.select(sa.func.count()).select_from(Widget)).scalar()


def test_database_starts_empty_then_is_written_to(db):
    assert _widget_count(db) == 0
    db.session.add(Widget(label="leak-check"))
    db.session.commit()
    assert _widget_count(db) == 1


def test_previous_test_did_not_leak_into_this_one(db):
    assert _widget_count(db) == 0


def test_metadata_is_shared_with_migrate(app):
    assert app.extensions["migrate"].db.metadata is Base.metadata


def test_database_url_env_wins_and_driver_is_pinned(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@host:5432/dbname")
    assert _postgres_url() == "postgresql+psycopg2://u:p@host:5432/dbname"

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host:5432/dbname")
    assert _postgres_url() == "postgresql+psycopg2://u:p@host:5432/dbname"


def test_postgres_parts_build_a_url_and_quote_the_password(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("POSTGRES_USER", "nepal")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss/word")
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.setenv("POSTGRES_DB", "betternepal")

    assert _postgres_url() == (
        "postgresql+psycopg2://nepal:p%40ss%2Fword@db.internal:6543/betternepal"
    )


def test_production_rejects_non_postgres_uri(monkeypatch):
    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", "a-real-long-random-secret")
    monkeypatch.setattr(ProductionConfig, "CORS_ORIGINS", ["https://betternepal.np"])
    monkeypatch.setattr(ProductionConfig, "SQLALCHEMY_DATABASE_URI", "sqlite:///prod.db")
    with pytest.raises(RuntimeError):
        create_app("production")
