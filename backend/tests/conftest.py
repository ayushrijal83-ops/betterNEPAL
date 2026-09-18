import pytest

from app import create_app
from app.extensions import db as _db


@pytest.fixture
def app():
    application = create_app("testing")
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    """An initialised in-memory schema, torn down after each test.

    Each test builds its own app, so each gets its own SQLite in-memory
    database; create_all/drop_all just make the table state explicit.
    """
    with app.app_context():
        _db.create_all()
        yield _db
        _db.session.remove()
        _db.drop_all()
