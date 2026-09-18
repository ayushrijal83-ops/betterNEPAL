"""Phase 3: users, roles, authentication and authorization."""
import uuid
from datetime import timedelta

import jwt
import pytest
import sqlalchemy as sa

from app.models import ROLE_NAMES, RefreshToken, Role, User
from app.models.base import utcnow
from app.services import auth_service
from app.utils.decorators import require_auth, require_roles
from app.utils.helpers import ApiError

PASSWORD = "correct-horse-battery"

REGISTRATION = {
    "email": "Sita@BetterNepal.NP",
    "password": PASSWORD,
    "full_name": "Sita Gurung",
    "phone": "+977 9801234567",
}


# --- roles -----------------------------------------------------------------


def test_seed_creates_exactly_the_five_roles(roles):
    assert set(roles) == set(ROLE_NAMES)
    assert len(ROLE_NAMES) == 5


def test_seed_is_idempotent(db, roles):
    from app.seed import seed_roles

    summary = seed_roles()
    assert summary["created"] == 0
    assert db.session.scalar(sa.select(sa.func.count()).select_from(Role)) == 5


def test_role_names_are_unique(db, roles):
    db.session.add(Role(name="citizen", description="duplicate"))
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_seed_creates_no_users(db, roles):
    assert db.session.scalar(sa.select(sa.func.count()).select_from(User)) == 0


# --- user model ------------------------------------------------------------


def test_create_user_with_role(make_user):
    user = make_user()
    assert isinstance(user.id, uuid.UUID)
    assert user.role_names == ["citizen"]
    assert user.is_active is True


def test_email_is_normalised_on_assignment(make_user):
    user = make_user(email="  Mixed.Case@Example.COM  ")
    assert user.email == "mixed.case@example.com"


def test_email_must_be_unique(db, make_user):
    make_user(email="dup@betternepal.np")
    db.session.add(
        User(
            email="dup@betternepal.np",
            password_hash="x",
            full_name="Second",
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_unique_email_is_case_insensitive_via_normalisation(db, make_user):
    make_user(email="dup@betternepal.np")
    db.session.add(
        User(email="DUP@BetterNepal.NP", password_hash="x", full_name="Second")
    )
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_inactive_user_can_be_represented(make_user):
    assert make_user(is_active=False).is_active is False


def test_user_can_hold_multiple_roles(make_user):
    user = make_user(role_names=("authority", "admin"))
    assert user.role_names == ["admin", "authority"]
    assert user.has_role("authority") is True
    assert user.has_role("citizen") is False


def test_role_relationship_is_bidirectional(make_user, roles):
    user = make_user(role_names=("contractor",))
    assert user in roles["contractor"].users


def test_user_role_link_requires_a_real_role(db, make_user):
    """The association table enforces referential integrity."""
    user = make_user()
    with pytest.raises(sa.exc.IntegrityError):
        db.session.execute(
            sa.text(
                "INSERT INTO user_roles (user_id, role_id) VALUES (:u, :r)"
            ),
            {"u": str(user.id), "r": str(uuid.uuid4())},
        )
        db.session.commit()
    db.session.rollback()


def test_public_dict_never_contains_the_password_hash(make_user):
    payload = make_user().to_public_dict()
    assert "password_hash" not in payload
    assert PASSWORD not in str(payload)
    assert set(payload) == {
        "id",
        "email",
        "full_name",
        "phone",
        "roles",
        "is_active",
        "created_at",
        "last_login_at",
    }


# --- password hashing ------------------------------------------------------


def test_password_is_hashed_with_argon2id():
    digest = auth_service.hash_password(PASSWORD)
    assert digest.startswith("$argon2id$")
    assert PASSWORD not in digest


def test_same_password_hashes_differently_each_time():
    assert auth_service.hash_password(PASSWORD) != auth_service.hash_password(PASSWORD)


def test_correct_password_verifies():
    digest = auth_service.hash_password(PASSWORD)
    assert auth_service.verify_password(digest, PASSWORD) is True


def test_incorrect_password_fails():
    digest = auth_service.hash_password(PASSWORD)
    assert auth_service.verify_password(digest, "wrong-password") is False


def test_malformed_hash_is_rejected_rather_than_raising():
    assert auth_service.verify_password("not-a-hash", PASSWORD) is False


def test_plaintext_password_is_never_stored(db, make_user):
    make_user()
    stored = db.session.scalar(sa.select(User.password_hash))
    assert stored != PASSWORD
    assert PASSWORD not in stored


# --- registration ----------------------------------------------------------


def test_registration_succeeds_and_returns_201(client, roles):
    response = client.post("/api/v1/auth/register", json=REGISTRATION)
    assert response.status_code == 201

    user = response.get_json()["data"]["user"]
    assert user["email"] == "sita@betternepal.np"
    assert user["full_name"] == "Sita Gurung"
    assert user["roles"] == ["citizen"]
    assert "password_hash" not in user


def test_registration_response_never_leaks_the_password(client, roles):
    response = client.post("/api/v1/auth/register", json=REGISTRATION)
    assert PASSWORD not in response.get_data(as_text=True)


def test_registration_assigns_only_the_citizen_role(client, db, roles):
    client.post("/api/v1/auth/register", json=REGISTRATION)
    user = db.session.scalar(sa.select(User))
    assert user.role_names == ["citizen"]


@pytest.mark.parametrize("privileged", ["admin", "authority", "contractor", "trekking_guide"])
def test_registration_cannot_select_a_privileged_role(client, db, roles, privileged):
    """Role keys in the body are ignored, not honoured."""
    payload = {**REGISTRATION, "role": privileged, "roles": [privileged]}
    response = client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201
    assert response.get_json()["data"]["user"]["roles"] == ["citizen"]

    user = db.session.scalar(sa.select(User))
    assert user.role_names == ["citizen"]


def test_registration_rejects_duplicate_email_with_409(client, roles):
    client.post("/api/v1/auth/register", json=REGISTRATION)
    response = client.post("/api/v1/auth/register", json=REGISTRATION)
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "email_already_registered"


def test_registration_duplicate_check_ignores_email_case(client, roles):
    client.post("/api/v1/auth/register", json=REGISTRATION)
    response = client.post(
        "/api/v1/auth/register", json={**REGISTRATION, "email": "SITA@betternepal.np"}
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "email", ["", "not-an-email", "no@domain", "spaces in@example.com", "@example.com"]
)
def test_registration_rejects_invalid_email(client, roles, email):
    response = client.post("/api/v1/auth/register", json={**REGISTRATION, "email": email})
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "validation_error"
    assert "email" in response.get_json()["error"]["details"]


@pytest.mark.parametrize("password", ["", "short", "1234567"])
def test_registration_rejects_weak_password(client, roles, password):
    response = client.post(
        "/api/v1/auth/register", json={**REGISTRATION, "password": password}
    )
    assert response.status_code == 400
    assert "password" in response.get_json()["error"]["details"]


def test_registration_rejects_overlong_password(client, roles):
    response = client.post(
        "/api/v1/auth/register", json={**REGISTRATION, "password": "a" * 129}
    )
    assert response.status_code == 400


def test_validation_error_never_echoes_the_password(client, roles):
    secret = "my-secret-but-short"
    response = client.post(
        "/api/v1/auth/register",
        json={**REGISTRATION, "email": "bad", "password": secret},
    )
    assert secret not in response.get_data(as_text=True)


def test_registration_rejects_missing_full_name(client, roles):
    payload = {k: v for k, v in REGISTRATION.items() if k != "full_name"}
    response = client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 400
    assert "full_name" in response.get_json()["error"]["details"]


def test_registration_accepts_a_missing_phone(client, roles):
    payload = {k: v for k, v in REGISTRATION.items() if k != "phone"}
    response = client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201
    assert response.get_json()["data"]["user"]["phone"] is None


def test_registration_rejects_a_malformed_phone(client, roles):
    response = client.post("/api/v1/auth/register", json={**REGISTRATION, "phone": "abc"})
    assert response.status_code == 400
    assert "phone" in response.get_json()["error"]["details"]


def test_registration_rejects_a_non_object_body(client, roles):
    response = client.post("/api/v1/auth/register", json=["not", "an", "object"])
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_payload"


def test_registration_reports_every_invalid_field_at_once(client, roles):
    response = client.post(
        "/api/v1/auth/register", json={"email": "bad", "password": "x", "full_name": ""}
    )
    details = response.get_json()["error"]["details"]
    assert {"email", "password", "full_name"} <= set(details)


def test_registration_fails_cleanly_when_roles_are_not_seeded(client, db):
    """No roles table content means no account, not a role-less account."""
    response = client.post("/api/v1/auth/register", json=REGISTRATION)
    assert response.status_code == 500
    assert response.get_json()["error"]["code"] == "roles_not_seeded"
    assert db.session.scalar(sa.select(sa.func.count()).select_from(User)) == 0


# --- login -----------------------------------------------------------------


def test_login_with_valid_credentials(client, make_user):
    make_user(email="ram@betternepal.np")
    response = client.post(
        "/api/v1/auth/login", json={"email": "ram@betternepal.np", "password": PASSWORD}
    )
    assert response.status_code == 200

    data = response.get_json()["data"]
    assert data["token_type"] == "Bearer"
    assert data["access_token"]
    assert data["refresh_token"]
    assert data["expires_in"] == 15 * 60
    assert data["user"]["email"] == "ram@betternepal.np"
    assert "password_hash" not in data["user"]


def test_login_email_is_case_insensitive(client, make_user):
    make_user(email="ram@betternepal.np")
    response = client.post(
        "/api/v1/auth/login", json={"email": "RAM@BetterNepal.NP", "password": PASSWORD}
    )
    assert response.status_code == 200


def test_login_with_wrong_password_returns_401(client, make_user):
    make_user(email="ram@betternepal.np")
    response = client.post(
        "/api/v1/auth/login", json={"email": "ram@betternepal.np", "password": "nope"}
    )
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "invalid_credentials"


def test_login_with_unknown_email_returns_401(client, roles):
    response = client.post(
        "/api/v1/auth/login", json={"email": "ghost@betternepal.np", "password": PASSWORD}
    )
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "invalid_credentials"


def test_unknown_email_and_wrong_password_are_indistinguishable(client, make_user):
    """Otherwise the endpoint becomes an account enumerator."""
    make_user(email="ram@betternepal.np")
    wrong_password = client.post(
        "/api/v1/auth/login", json={"email": "ram@betternepal.np", "password": "nope"}
    )
    unknown_user = client.post(
        "/api/v1/auth/login", json={"email": "ghost@betternepal.np", "password": "nope"}
    )
    assert wrong_password.status_code == unknown_user.status_code
    assert wrong_password.get_json() == unknown_user.get_json()


def test_inactive_user_cannot_log_in(client, make_user):
    make_user(email="banned@betternepal.np", is_active=False)
    response = client.post(
        "/api/v1/auth/login", json={"email": "banned@betternepal.np", "password": PASSWORD}
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "account_disabled"


def test_inactive_user_with_wrong_password_reveals_nothing(client, make_user):
    """A disabled account must not be discoverable without the password."""
    make_user(email="banned@betternepal.np", is_active=False)
    response = client.post(
        "/api/v1/auth/login", json={"email": "banned@betternepal.np", "password": "nope"}
    )
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "invalid_credentials"


def test_login_updates_last_login_at(client, db, make_user):
    user = make_user(email="ram@betternepal.np")
    assert user.last_login_at is None

    client.post(
        "/api/v1/auth/login", json={"email": "ram@betternepal.np", "password": PASSWORD}
    )
    db.session.refresh(user)
    assert user.last_login_at is not None
    assert user.last_login_at.tzinfo is not None


def test_login_requires_both_fields(client, roles):
    response = client.post("/api/v1/auth/login", json={"email": "ram@betternepal.np"})
    assert response.status_code == 400
    assert "password" in response.get_json()["error"]["details"]


# --- authentication --------------------------------------------------------


def test_me_without_a_token_returns_401(client, make_user):
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "authentication_required"


def test_me_with_a_valid_token(client, make_user, auth_headers):
    make_user(email="ram@betternepal.np", phone="+977 9801234567")
    response = client.get(
        "/api/v1/auth/me", headers=auth_headers("ram@betternepal.np", PASSWORD)
    )
    assert response.status_code == 200

    user = response.get_json()["data"]["user"]
    assert user["email"] == "ram@betternepal.np"
    assert user["phone"] == "+977 9801234567"
    assert user["roles"] == ["citizen"]
    assert user["is_active"] is True


def test_me_response_contains_no_password_hash(client, make_user, auth_headers):
    make_user(email="ram@betternepal.np")
    response = client.get(
        "/api/v1/auth/me", headers=auth_headers("ram@betternepal.np", PASSWORD)
    )
    body = response.get_data(as_text=True)
    assert "password_hash" not in body
    assert "argon2" not in body


@pytest.mark.parametrize(
    "header",
    ["", "Bearer", "Bearer ", "Basic abc123", "token abc123", "Bearer not.a.jwt"],
)
def test_malformed_authorization_headers_are_rejected(client, make_user, header):
    make_user()
    response = client.get("/api/v1/auth/me", headers={"Authorization": header})
    assert response.status_code == 401


def test_expired_access_token_is_rejected(app, client, make_user):
    user = make_user(email="ram@betternepal.np")
    with app.app_context():
        app.config["ACCESS_TOKEN_TTL_MINUTES"] = -1
        token = auth_service.create_access_token(user)

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "token_expired"


def test_token_signed_with_another_key_is_rejected(app, client, make_user):
    user = make_user(email="ram@betternepal.np")
    token = jwt.encode(
        {"sub": str(user.id), "type": "access", "exp": utcnow() + timedelta(minutes=5)},
        "an-attacker-chosen-key-of-sufficient-length",
        algorithm="HS256",
    )
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "invalid_token"


def test_unsigned_none_algorithm_token_is_rejected(app, client, make_user):
    """The classic JWT forgery: alg=none with no signature."""
    user = make_user(email="ram@betternepal.np")
    token = jwt.encode(
        {"sub": str(user.id), "type": "access", "exp": utcnow() + timedelta(minutes=5)},
        key="",
        algorithm="none",
    )
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_token_without_an_expiry_is_rejected(app, client, make_user):
    user = make_user(email="ram@betternepal.np")
    with app.app_context():
        token = jwt.encode(
            {"sub": str(user.id), "type": "access"},
            app.config["JWT_SECRET_KEY"],
            algorithm="HS256",
        )
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_refresh_token_is_not_accepted_as_an_access_token(client, make_user, login):
    make_user(email="ram@betternepal.np")
    refresh = login("ram@betternepal.np", PASSWORD)["refresh_token"]
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert response.status_code == 401


def test_token_for_a_deleted_user_is_rejected(app, client, db, make_user):
    user = make_user(email="ram@betternepal.np")
    with app.app_context():
        token = auth_service.create_access_token(user)
    db.session.delete(user)
    db.session.commit()

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_token_stops_working_once_the_account_is_deactivated(
    app, client, db, make_user, auth_headers
):
    make_user(email="ram@betternepal.np")
    headers = auth_headers("ram@betternepal.np", PASSWORD)
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200

    user = db.session.scalar(sa.select(User))
    user.is_active = False
    db.session.commit()

    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_access_token_carries_no_personal_data(app, make_user):
    user = make_user(email="ram@betternepal.np", full_name="Ram Thapa")
    with app.app_context():
        token = auth_service.create_access_token(user)
        payload = jwt.decode(
            token, app.config["JWT_SECRET_KEY"], algorithms=["HS256"]
        )
    assert set(payload) == {"sub", "type", "iat", "exp", "jti"}
    assert "ram@betternepal.np" not in str(payload)
    assert "Ram Thapa" not in str(payload)


# --- authorization ---------------------------------------------------------


@pytest.fixture
def guarded_app(app):
    """Register throwaway endpoints exercising the authorization decorators."""

    @app.get("/api/v1/_test/authenticated")
    @require_auth
    def _authenticated():
        return {"status": "success", "data": {"ok": True}}

    @app.get("/api/v1/_test/admin-only")
    @require_roles("admin")
    def _admin_only():
        return {"status": "success", "data": {"ok": True}}

    @app.get("/api/v1/_test/staff")
    @require_roles("admin", "authority")
    def _staff():
        return {"status": "success", "data": {"ok": True}}

    return app


def test_require_auth_allows_any_authenticated_user(guarded_app, client, make_user, auth_headers):
    make_user(email="ram@betternepal.np")
    response = client.get(
        "/api/v1/_test/authenticated", headers=auth_headers("ram@betternepal.np", PASSWORD)
    )
    assert response.status_code == 200


def test_correct_role_is_allowed(guarded_app, client, make_user, auth_headers):
    make_user(email="admin@betternepal.np", role_names=("admin",))
    response = client.get(
        "/api/v1/_test/admin-only", headers=auth_headers("admin@betternepal.np", PASSWORD)
    )
    assert response.status_code == 200


def test_incorrect_role_returns_403(guarded_app, client, make_user, auth_headers):
    make_user(email="ram@betternepal.np", role_names=("citizen",))
    response = client.get(
        "/api/v1/_test/admin-only", headers=auth_headers("ram@betternepal.np", PASSWORD)
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "permission_denied"


def test_role_check_without_a_token_is_401_not_403(guarded_app, client, make_user):
    make_user()
    response = client.get("/api/v1/_test/admin-only")
    assert response.status_code == 401


@pytest.mark.parametrize("role", ["admin", "authority"])
def test_multiple_allowed_roles_each_work(guarded_app, client, make_user, auth_headers, role):
    make_user(email=f"{role}@betternepal.np", role_names=(role,))
    response = client.get(
        "/api/v1/_test/staff", headers=auth_headers(f"{role}@betternepal.np", PASSWORD)
    )
    assert response.status_code == 200


def test_a_role_outside_the_allowed_set_is_refused(guarded_app, client, make_user, auth_headers):
    make_user(email="builder@betternepal.np", role_names=("contractor",))
    response = client.get(
        "/api/v1/_test/staff", headers=auth_headers("builder@betternepal.np", PASSWORD)
    )
    assert response.status_code == 403


def test_admin_is_not_implicitly_granted_other_roles(make_user):
    from app.utils.permissions import user_has_any_role

    admin = make_user(email="admin@betternepal.np", role_names=("admin",))
    assert user_has_any_role(admin, "admin") is True
    assert user_has_any_role(admin, "authority") is False


def test_inactive_user_holds_no_effective_roles(make_user):
    from app.utils.permissions import user_has_any_role

    user = make_user(role_names=("admin",), is_active=False)
    assert user_has_any_role(user, "admin") is False


def test_require_roles_rejects_an_empty_role_list():
    with pytest.raises(ValueError):
        require_roles()


# --- refresh ---------------------------------------------------------------


def test_refresh_issues_a_new_token_pair(client, make_user, login):
    make_user(email="ram@betternepal.np")
    tokens = login("ram@betternepal.np", PASSWORD)

    response = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 200

    data = response.get_json()["data"]
    assert data["access_token"]
    assert data["refresh_token"] != tokens["refresh_token"]


def test_new_access_token_from_refresh_works(client, make_user, login):
    make_user(email="ram@betternepal.np")
    tokens = login("ram@betternepal.np", PASSWORD)
    refreshed = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    ).get_json()["data"]

    response = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {refreshed['access_token']}"}
    )
    assert response.status_code == 200


def test_rotated_refresh_token_cannot_be_reused(client, make_user, login):
    """Rotation: a stolen token is good for at most one use."""
    make_user(email="ram@betternepal.np")
    tokens = login("ram@betternepal.np", PASSWORD)
    client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

    response = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "invalid_refresh_token"


def test_refresh_rejects_an_access_token(client, make_user, login):
    make_user(email="ram@betternepal.np")
    tokens = login("ram@betternepal.np", PASSWORD)
    response = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]}
    )
    assert response.status_code == 401


def test_refresh_rejects_a_garbage_token(client, roles):
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": "nonsense"})
    assert response.status_code == 401


def test_refresh_requires_a_token(client, roles):
    response = client.post("/api/v1/auth/refresh", json={})
    assert response.status_code == 400
    assert "refresh_token" in response.get_json()["error"]["details"]


def test_expired_refresh_token_is_rejected(client, db, make_user, login):
    make_user(email="ram@betternepal.np")
    tokens = login("ram@betternepal.np", PASSWORD)

    record = db.session.scalar(sa.select(RefreshToken))
    record.expires_at = utcnow() - timedelta(seconds=1)
    db.session.commit()

    response = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 401


def test_refresh_is_refused_for_a_deactivated_account(client, db, make_user, login):
    make_user(email="ram@betternepal.np")
    tokens = login("ram@betternepal.np", PASSWORD)

    user = db.session.scalar(sa.select(User))
    user.is_active = False
    db.session.commit()

    response = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 403


def test_only_the_token_hash_is_stored(db, make_user, login):
    make_user(email="ram@betternepal.np")
    tokens = login("ram@betternepal.np", PASSWORD)

    record = db.session.scalar(sa.select(RefreshToken))
    assert record.token_hash != tokens["refresh_token"]
    assert len(record.token_hash) == 64
    assert tokens["refresh_token"] not in record.token_hash


# --- logout ----------------------------------------------------------------


def test_logout_revokes_the_supplied_refresh_token(client, make_user, login, auth_headers):
    make_user(email="ram@betternepal.np")
    tokens = login("ram@betternepal.np", PASSWORD)

    response = client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert response.status_code == 200

    reused = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert reused.status_code == 401


def test_logout_without_a_body_revokes_every_session(client, db, make_user, login):
    make_user(email="ram@betternepal.np")
    first = login("ram@betternepal.np", PASSWORD)
    second = login("ram@betternepal.np", PASSWORD)

    response = client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {first['access_token']}"},
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["count"] == 2

    for tokens in (first, second):
        reused = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert reused.status_code == 401


def test_logout_requires_authentication(client, make_user, login):
    make_user(email="ram@betternepal.np")
    tokens = login("ram@betternepal.np", PASSWORD)
    response = client.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 401


def test_logout_is_idempotent(client, make_user, login):
    make_user(email="ram@betternepal.np")
    tokens = login("ram@betternepal.np", PASSWORD)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    first = client.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}, headers=headers
    )
    second = client.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}, headers=headers
    )
    assert first.status_code == 200
    assert second.status_code == 200


def test_access_token_still_works_briefly_after_logout(client, make_user, login):
    """Documented limitation: logout revokes refresh tokens, not issued access
    tokens. The 15-minute access TTL is the actual revocation window."""
    make_user(email="ram@betternepal.np")
    tokens = login("ram@betternepal.np", PASSWORD)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    client.post("/api/v1/auth/logout", headers=headers)
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200


def test_one_users_logout_does_not_affect_another(client, make_user, login):
    make_user(email="ram@betternepal.np")
    make_user(email="sita@betternepal.np")
    ram = login("ram@betternepal.np", PASSWORD)
    sita = login("sita@betternepal.np", PASSWORD)

    client.post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {ram['access_token']}"}
    )

    response = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": sita["refresh_token"]}
    )
    assert response.status_code == 200


# --- envelope --------------------------------------------------------------


def test_auth_errors_use_the_locked_error_envelope(client, roles):
    body = client.post(
        "/api/v1/auth/login", json={"email": "ghost@betternepal.np", "password": "x"}
    ).get_json()
    assert set(body) == {"status", "error"}
    assert body["status"] == "error"
    assert set(body["error"]) >= {"code", "message"}


def test_auth_success_uses_the_locked_success_envelope(client, roles):
    body = client.post("/api/v1/auth/register", json=REGISTRATION).get_json()
    assert set(body) == {"status", "data"}
    assert body["status"] == "success"


def test_health_endpoint_still_works(client):
    assert client.get("/api/v1/health").status_code == 200


def test_api_error_default_status_is_400():
    error = ApiError("boom")
    assert error.status == 400


# --- configuration guards --------------------------------------------------


def test_production_rejects_a_placeholder_signing_key(monkeypatch):
    from app import create_app
    from app.config import ProductionConfig

    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", "a-real-long-random-secret-value")
    monkeypatch.setattr(ProductionConfig, "CORS_ORIGINS", ["https://betternepal.np"])
    monkeypatch.setattr(
        ProductionConfig, "SQLALCHEMY_DATABASE_URI", "postgresql+psycopg2://u:p@h/db"
    )
    monkeypatch.setattr(ProductionConfig, "JWT_SECRET_KEY", "change-me-in-production")
    with pytest.raises(RuntimeError, match="placeholder"):
        create_app("production")


def test_production_rejects_a_short_signing_key(monkeypatch):
    """RFC 7518: an HS256 key shorter than the digest weakens every token."""
    from app import create_app
    from app.config import ProductionConfig

    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", "a-real-long-random-secret-value")
    monkeypatch.setattr(ProductionConfig, "CORS_ORIGINS", ["https://betternepal.np"])
    monkeypatch.setattr(
        ProductionConfig, "SQLALCHEMY_DATABASE_URI", "postgresql+psycopg2://u:p@h/db"
    )
    monkeypatch.setattr(ProductionConfig, "JWT_SECRET_KEY", "too-short")
    with pytest.raises(RuntimeError, match="32 bytes"):
        create_app("production")


def test_missing_jwt_secret_is_refused(monkeypatch):
    from app import create_app
    from app.config import TestingConfig

    monkeypatch.setattr(TestingConfig, "JWT_SECRET_KEY", "")
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        create_app("testing")


def test_logout_cannot_revoke_another_users_refresh_token(client, make_user, login):
    """Revocation is scoped to the caller, so one account cannot end another's
    session by submitting a token it happens to hold."""
    make_user(email="ram@betternepal.np")
    make_user(email="sita@betternepal.np")
    ram = login("ram@betternepal.np", PASSWORD)
    sita = login("sita@betternepal.np", PASSWORD)

    response = client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": sita["refresh_token"]},
        headers={"Authorization": f"Bearer {ram['access_token']}"},
    )
    assert response.status_code == 200

    # Sita's session survives Ram's attempt to end it.
    still_valid = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": sita["refresh_token"]}
    )
    assert still_valid.status_code == 200
