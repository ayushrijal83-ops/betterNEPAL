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


@pytest.fixture
def roles(db):
    """Seed the five application roles and return them by name."""
    from app.models import Role
    from app.seed import seed_roles

    seed_roles()
    return {role.name: role for role in db.session.query(Role).all()}


@pytest.fixture
def make_user(db, roles):
    """Create a user directly, bypassing the registration endpoint.

    Lets a test set up an authority or admin account, which public
    registration deliberately cannot produce.
    """
    from app.models import User
    from app.services.auth_service import hash_password

    def _make_user(
        email: str = "citizen@betternepal.np",
        password: str = "correct-horse-battery",
        full_name: str = "Test User",
        role_names: tuple[str, ...] = ("citizen",),
        is_active: bool = True,
        phone: str | None = None,
    ) -> User:
        user = User(
            email=email,
            password_hash=hash_password(password),
            full_name=full_name,
            phone=phone,
            is_active=is_active,
        )
        for name in role_names:
            user.roles.append(roles[name])
        db.session.add(user)
        db.session.commit()
        return user

    return _make_user


@pytest.fixture
def login(client):
    """Log in and return the full token payload."""

    def _login(email: str, password: str) -> dict:
        response = client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        assert response.status_code == 200, response.get_json()
        return response.get_json()["data"]

    return _login


@pytest.fixture
def auth_headers(login):
    """Bearer header for a freshly logged-in user."""

    def _auth_headers(email: str, password: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {login(email, password)['access_token']}"}

    return _auth_headers
