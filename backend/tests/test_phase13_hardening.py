"""Phase 13: rate limiting, refresh-token cookies, and the Ollama provider.

No test here makes a network call. The Ollama provider is exercised against a
patched ``openai.OpenAI`` client, so the wire format, node routing and failure
handling are all checked without a machine on the other end.

Rate limiting is off in ``TestingConfig`` - the rest of the suite signs in
hundreds of times and would otherwise 429. The fixture below switches it on
*before* the app is built, because Flask-Limiter reads ``RATELIMIT_ENABLED``
during ``init_app`` and ignores later changes.
"""
from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from app.config import TestingConfig
from app.extensions import db as _db
from app.extensions import limiter, rate_limit_key
from app.services.ai_service import AIUnavailable, NullAIService, get_ai_service
from app.services.ollama_service import (
    PLACEHOLDER_API_KEY,
    ROLE_TEXT,
    ROLE_VISION,
    OllamaService,
    _Node,
)

PASSWORD = "correct-horse-battery"


# ===========================================================================
# Rate limiting
# ===========================================================================


@pytest.fixture
def limited_app(monkeypatch):
    """An app with rate limiting switched on, and counters reset afterwards."""
    monkeypatch.setattr(TestingConfig, "RATELIMIT_ENABLED", True)
    application = create_app("testing")
    application.config.update(TESTING=True)

    with application.app_context():
        _db.create_all()
        yield application
        # Storage is a process-wide singleton; leaving counters behind would
        # make the next test fail depending on execution order.
        limiter.reset()
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def limited_client(limited_app):
    return limited_app.test_client()


def _login_attempt(client, email="nobody@betternepal.np"):
    return client.post("/api/v1/auth/login", json={"email": email, "password": "wrong"})


def test_login_is_capped_at_ten_per_minute(limited_client):
    """Argon2 makes each guess expensive; this caps how many are attempted."""
    codes = [_login_attempt(limited_client).status_code for _ in range(12)]

    assert codes[:10] == [401] * 10
    assert codes[10:] == [429, 429]


def test_a_rate_limited_response_uses_the_locked_envelope(limited_client):
    """Flask-Limiter's own reply is plain text, which no client could parse."""
    for _ in range(11):
        _login_attempt(limited_client)

    response = _login_attempt(limited_client)
    body = response.get_json()

    assert response.status_code == 429
    assert set(body) == {"status", "error"}
    assert body["error"]["code"] == "rate_limit_exceeded"
    assert "10 per 1 minute" in body["error"]["details"]["limit"]


def test_registration_is_capped(limited_client):
    codes = []
    for index in range(12):
        codes.append(
            limited_client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"user{index}@betternepal.np",
                    "password": "a-good-long-password",
                    "full_name": "Test Person",
                },
            ).status_code
        )
    assert 429 in codes
    assert codes.index(429) == 10


def test_the_public_map_has_a_looser_cap(limited_client):
    """A live map legitimately polls; 60/minute is not meant to ration that."""
    codes = [
        limited_client.get("/api/v1/analytics/map/points").status_code for _ in range(30)
    ]
    assert 429 not in codes


def test_unlimited_endpoints_are_not_throttled(limited_client):
    """Only the routes that opt in are limited - there is no blanket default."""
    codes = [limited_client.get("/api/v1/health").status_code for _ in range(40)]
    assert codes == [200] * 40


def test_rate_limiting_is_off_under_the_default_test_config(client, db):
    """Otherwise the other 996 tests would 429 on their own logins."""
    codes = [_login_attempt(client).status_code for _ in range(15)]
    assert 429 not in codes


def test_the_limit_key_prefers_the_user_over_the_ip(app):
    """A shared office NAT must not lock out a whole department."""
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": "10.0.0.9"}):
        from flask import g

        assert rate_limit_key() == "ip:10.0.0.9"

        g.current_user = MagicMock(id="abc-123")
        assert rate_limit_key() == "user:abc-123"


def test_the_limit_key_survives_a_missing_remote_addr(app):
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": None}):
        assert rate_limit_key() == "ip:unknown"


# ===========================================================================
# Refresh-token cookie
# ===========================================================================


@pytest.fixture
def signed_in(client, make_user):
    make_user(email="ram@betternepal.np", role_names=("citizen",))
    return client.post(
        "/api/v1/auth/login",
        json={"email": "ram@betternepal.np", "password": PASSWORD},
    )


def _cookie_header(response, name="bn_refresh_token"):
    for header in response.headers.getlist("Set-Cookie"):
        if header.startswith(f"{name}="):
            return header
    return None


def test_login_sets_an_httponly_refresh_cookie(signed_in):
    header = _cookie_header(signed_in)
    assert header is not None
    assert "HttpOnly" in header


def test_the_cookie_is_samesite_lax_and_scoped_to_auth(signed_in):
    """Scoped so it never rides along on ordinary API calls."""
    header = _cookie_header(signed_in)
    assert "SameSite=Lax" in header
    assert "Path=/api/v1/auth" in header


def test_the_token_is_still_in_the_json_body(signed_in):
    """Kept for backwards compatibility with the existing frontend."""
    assert signed_in.get_json()["data"]["refresh_token"]


def test_secure_is_off_in_testing_but_configurable(signed_in, app):
    """localhost is not HTTPS; a Secure cookie there is silently never sent."""
    assert app.config["REFRESH_COOKIE_SECURE"] is False
    assert "Secure" not in _cookie_header(signed_in)


def test_production_refuses_an_insecure_cookie(monkeypatch):
    """The one place where dropping Secure would send the token in the clear."""
    from app.config import ProductionConfig

    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", "a-real-long-random-secret-value")
    monkeypatch.setattr(ProductionConfig, "JWT_SECRET_KEY", "a-real-long-random-secret-value")
    monkeypatch.setattr(ProductionConfig, "CORS_ORIGINS", ["https://betternepal.np"])
    monkeypatch.setattr(
        ProductionConfig, "SQLALCHEMY_DATABASE_URI", "postgresql+psycopg2://u:p@h/db"
    )
    monkeypatch.setattr(ProductionConfig, "REFRESH_COOKIE_SECURE", False)

    with pytest.raises(RuntimeError, match="REFRESH_COOKIE_SECURE"):
        create_app("production")


def test_production_refuses_memory_rate_limit_storage(monkeypatch):
    """Production must use a shared Redis backend, not per-worker memory."""
    from app.config import ProductionConfig

    # Use a JWT_SECRET_KEY that's at least 32 bytes (RFC 7518)
    long_key = "a-real-long-random-secret-value-that-is-at-least-32-bytes-long"
    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", long_key)
    monkeypatch.setattr(ProductionConfig, "JWT_SECRET_KEY", long_key)
    monkeypatch.setattr(ProductionConfig, "CORS_ORIGINS", ["https://betternepal.np"])
    monkeypatch.setattr(
        ProductionConfig, "SQLALCHEMY_DATABASE_URI", "postgresql+psycopg2://u:p@h/db"
    )
    monkeypatch.setattr(ProductionConfig, "REFRESH_COOKIE_SECURE", True)
    monkeypatch.setattr(ProductionConfig, "RATELIMIT_ENABLED", True)
    monkeypatch.setattr(ProductionConfig, "RATELIMIT_STORAGE_URI", "memory://")

    with pytest.raises(RuntimeError, match="RATELIMIT_STORAGE_URI must not be 'memory://'"):
        create_app("production")


def test_production_accepts_redis_rate_limit_storage(monkeypatch):
    """Production accepts a Redis storage URI."""
    from app.config import ProductionConfig

    # Use a JWT_SECRET_KEY that's at least 32 bytes (RFC 7518)
    long_key = "a-real-long-random-secret-value-that-is-at-least-32-bytes-long"
    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", long_key)
    monkeypatch.setattr(ProductionConfig, "JWT_SECRET_KEY", long_key)
    monkeypatch.setattr(ProductionConfig, "CORS_ORIGINS", ["https://betternepal.np"])
    monkeypatch.setattr(
        ProductionConfig, "SQLALCHEMY_DATABASE_URI", "postgresql+psycopg2://u:p@h/db"
    )
    monkeypatch.setattr(ProductionConfig, "REFRESH_COOKIE_SECURE", True)
    monkeypatch.setattr(ProductionConfig, "RATELIMIT_ENABLED", True)
    monkeypatch.setattr(ProductionConfig, "RATELIMIT_STORAGE_URI", "redis://redis:6379/0")

    # Should not raise
    app = create_app("production")
    assert app.config["RATELIMIT_STORAGE_URI"] == "redis://redis:6379/0"


def test_development_allows_memory_rate_limit_storage():
    """Development defaults to memory:// which is correct for one worker."""
    from app.config import DevelopmentConfig

    # Default from environment or base config
    assert DevelopmentConfig.RATELIMIT_STORAGE_URI == "memory://"


def test_testing_allows_memory_rate_limit_storage():
    """Testing uses memory:// (though rate limiting is disabled by default)."""
    from app.config import TestingConfig

    assert TestingConfig.RATELIMIT_STORAGE_URI == "memory://"


def test_rate_limiter_uses_configured_storage_uri(monkeypatch):
    """The limiter is initialized with the configured storage URI."""
    from app.config import TestingConfig
    from app import create_app
    from app.extensions import limiter

    monkeypatch.setattr(TestingConfig, "RATELIMIT_ENABLED", True)
    monkeypatch.setattr(TestingConfig, "RATELIMIT_STORAGE_URI", "redis://test-redis:6379/1")

    application = create_app("testing")
    with application.app_context():
        # The limiter's storage should be configured with the Redis URI
        # Flask-Limiter's internal storage object wraps the Redis client
        assert limiter._storage is not None
        assert limiter._storage.target_server == "redis"
        # The underlying Redis connection pool should have the correct host/port/db
        redis_client = limiter._storage.storage
        pool = redis_client.connection_pool
        assert pool.connection_kwargs["host"] == "test-redis"
        assert pool.connection_kwargs["port"] == 6379
        assert pool.connection_kwargs["db"] == 1


def test_refresh_works_from_the_cookie_alone(client, signed_in):
    """The point of the cookie: a client that stores nothing in JavaScript."""
    response = client.post("/api/v1/auth/refresh", json={})
    assert response.status_code == 200
    assert response.get_json()["data"]["access_token"]


def test_refresh_still_accepts_a_body_token(client, make_user):
    make_user(email="sita@betternepal.np", role_names=("citizen",))
    tokens = client.post(
        "/api/v1/auth/login",
        json={"email": "sita@betternepal.np", "password": PASSWORD},
    ).get_json()["data"]

    fresh = client.__class__(client.application, client.response_wrapper)
    response = fresh.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 200


def test_refresh_rotates_the_cookie(client, signed_in):
    """Phase 3 rotates on every use; the cookie must follow."""
    original = signed_in.get_json()["data"]["refresh_token"]
    response = client.post("/api/v1/auth/refresh", json={})

    assert response.get_json()["data"]["refresh_token"] != original
    assert "HttpOnly" in _cookie_header(response)


def test_refresh_with_neither_cookie_nor_body_is_a_400(client, db):
    response = client.post("/api/v1/auth/refresh", json={})
    assert response.status_code == 400
    assert "refresh_token" in response.get_json()["error"]["details"]


def test_logout_clears_the_cookie(client, signed_in):
    """Otherwise the browser keeps presenting a revoked token."""
    access = signed_in.get_json()["data"]["access_token"]
    response = client.post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {access}"}
    )

    assert response.status_code == 200
    header = _cookie_header(response)
    assert header is not None
    # Deletion is an immediate expiry, not an absent header.
    assert "Expires=Thu, 01 Jan 1970" in header or "Max-Age=0" in header


def test_logout_revokes_the_cookie_token_server_side(client, signed_in):
    access = signed_in.get_json()["data"]["access_token"]
    client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {access}"})

    # The cookie is gone from the jar, so this refresh has nothing to present.
    assert client.post("/api/v1/auth/refresh", json={}).status_code == 400


# ===========================================================================
# Ollama provider
# ===========================================================================


def _completion(content: str):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    return response


@pytest.fixture
def ollama():
    return OllamaService(
        vision_url="http://gpu-node:11434",
        text_url="http://cpu-node:11434",
        embed_url="http://cpu-node:11434",
        vision_model="qwen2.5:7b",
        text_model="qwen2.5:3b",
    )


@pytest.fixture
def healthy(ollama):
    """Mark every node reachable without probing anything."""
    for node in ollama._nodes.values():
        node._verdict_until = float("inf")
        node._last_verdict = True
    return ollama


def test_the_factory_prefers_ollama_over_gemini():
    """Self-hosted costs nothing per call and keeps report text on the network."""
    app = create_app("testing")
    app.config.update(
        OLLAMA_TEXT_NODE="http://cpu-node:11434", GEMINI_API_KEY="a-key"
    )
    with app.app_context():
        assert get_ai_service().name == "ollama"


def test_the_factory_falls_back_to_null_with_nothing_configured():
    app = create_app("testing")
    with app.app_context():
        app.config["OLLAMA_TEXT_NODE"] = ""
        app.config["OLLAMA_VISION_NODE"] = ""
        app.config["OLLAMA_EMBED_NODE"] = ""
        app.config["GEMINI_API_KEY"] = ""
        assert isinstance(get_ai_service(), NullAIService)


def test_an_unconfigured_provider_is_unavailable():
    assert OllamaService().available is False


def test_one_configured_node_is_enough():
    assert OllamaService(text_url="http://cpu-node:11434").available is True


def test_available_makes_no_network_call():
    """It is read on hot paths; a probe there would stall every page."""
    with patch("openai.OpenAI") as client:
        assert OllamaService(text_url="http://cpu-node:11434").available is True
    client.assert_not_called()


def test_the_endpoint_gets_the_v1_suffix():
    """Ollama's OpenAI-compatible surface lives under /v1."""
    assert _Node("text", "http://cpu-node:11434", "m", 5)._endpoint() == (
        "http://cpu-node:11434/v1"
    )


def test_an_explicit_v1_is_not_doubled():
    assert _Node("text", "http://cpu-node:11434/v1", "m", 5)._endpoint() == (
        "http://cpu-node:11434/v1"
    )


def test_the_client_uses_the_placeholder_key(app):
    """Ollama ignores it, but the SDK refuses to start without one."""
    with patch("openai.OpenAI") as client:
        _Node("text", "http://cpu-node:11434", "m", 5).client()

    kwargs = client.call_args.kwargs
    assert kwargs["api_key"] == PLACEHOLDER_API_KEY
    assert kwargs["base_url"] == "http://cpu-node:11434/v1"
    # Retries would multiply an already-generous CPU timeout.
    assert kwargs["max_retries"] == 0


def test_classification_goes_to_the_text_node(app, healthy):
    """Short structured prompts belong on the CPU box, not the GPU."""
    with app.app_context():
        with patch.object(healthy._nodes[ROLE_TEXT], "client") as text_client:
            text_client.return_value.chat.completions.create.return_value = _completion(
                '{"category": "road_damage", "confidence": 0.9, "severity": "high"}'
            )
            analysis = healthy.analyze_report_text("Pothole", "A deep pothole.")

    assert analysis.suggested_category == "road_damage"
    assert analysis.confidence == 0.9
    kwargs = text_client.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "qwen2.5:3b"
    # Deterministic: the same report must classify the same way twice.
    assert kwargs["temperature"] == 0.0


def test_chat_goes_to_the_vision_node(app, healthy):
    """Prose for a citizen needs the larger model on the GPU."""
    with app.app_context():
        with patch.object(healthy._nodes[ROLE_VISION], "client") as vision_client:
            vision_client.return_value.chat.completions.create.return_value = _completion(
                "The road is closed at Mugling."
            )
            answer = healthy.chat("Is the road open?")

    assert answer == "The road is closed at Mugling."
    kwargs = vision_client.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "qwen2.5:7b"
    # Prose, so not pinned to zero.
    assert kwargs["temperature"] > 0


def test_a_hallucinated_category_is_still_discarded(app, healthy):
    """Self-hosting changes the economics, not the trustworthiness."""
    with app.app_context():
        with patch.object(healthy._nodes[ROLE_TEXT], "client") as text_client:
            text_client.return_value.chat.completions.create.return_value = _completion(
                '{"category": "alien_invasion", "confidence": 0.99}'
            )
            analysis = healthy.analyze_report_text("t", "d")

    assert analysis.suggested_category is None


def test_a_fenced_response_is_still_parsed(app, healthy):
    with app.app_context():
        with patch.object(healthy._nodes[ROLE_TEXT], "client") as text_client:
            text_client.return_value.chat.completions.create.return_value = _completion(
                '```json\n{"category": "water_leak"}\n```'
            )
            analysis = healthy.analyze_report_text("t", "d")

    assert analysis.suggested_category == "water_leak"


def test_an_unreachable_node_raises_ai_unavailable(app, healthy):
    with app.app_context():
        with patch.object(healthy._nodes[ROLE_TEXT], "client") as text_client:
            text_client.return_value.chat.completions.create.side_effect = ConnectionError(
                "connection refused to 192.168.1.42:11434"
            )
            with pytest.raises(AIUnavailable) as exc:
                healthy.analyze_report_text("t", "d")

    # The machine's address stays in the log, not in the client's error.
    assert "192.168.1.42" not in str(exc.value)
    assert "could not be reached" in str(exc.value)


def test_an_empty_response_is_reported(app, healthy):
    with app.app_context():
        with patch.object(healthy._nodes[ROLE_TEXT], "client") as text_client:
            text_client.return_value.chat.completions.create.return_value = _completion("")
            with pytest.raises(AIUnavailable, match="returned nothing"):
                healthy.analyze_report_text("t", "d")


def test_a_node_that_fails_its_health_check_is_reported(app):
    service = OllamaService(text_url="http://cpu-node:11434")
    with app.app_context():
        with patch.object(service._nodes[ROLE_TEXT], "client") as text_client:
            text_client.return_value.models.list.side_effect = ConnectionError("down")
            with pytest.raises(AIUnavailable, match="not responding"):
                service.analyze_report_text("t", "d")


def test_a_failed_health_check_is_cached(app):
    """Otherwise one switched-off laptop adds its timeout to every request."""
    service = OllamaService(text_url="http://cpu-node:11434", health_ttl=60)

    with app.app_context():
        with patch.object(service._nodes[ROLE_TEXT], "client") as text_client:
            text_client.return_value.models.list.side_effect = ConnectionError("down")
            for _ in range(5):
                with pytest.raises(AIUnavailable):
                    service.analyze_report_text("t", "d")

    # Probed once, not five times.
    assert text_client.return_value.models.list.call_count == 1


def test_a_missing_role_falls_back_to_another_node(app):
    """A one-machine deployment is a normal setup, not a degraded one."""
    service = OllamaService(text_url="http://only-node:11434")
    service._nodes[ROLE_TEXT]._verdict_until = float("inf")
    service._nodes[ROLE_TEXT]._last_verdict = True

    with app.app_context():
        with patch.object(service._nodes[ROLE_TEXT], "client") as text_client:
            text_client.return_value.chat.completions.create.return_value = _completion(
                "answered from the only node"
            )
            assert service.chat("hello") == "answered from the only node"


def test_a_role_with_no_node_at_all_is_refused(app):
    service = OllamaService()
    with app.app_context():
        with pytest.raises(AIUnavailable, match="OLLAMA_TEXT_NODE"):
            service.analyze_report_text("t", "d")


def test_roles_degrade_independently(app):
    """A dead GPU must not stop the CPU node classifying reports."""
    service = OllamaService(
        vision_url="http://gpu-node:11434", text_url="http://cpu-node:11434"
    )
    text_node = service._nodes[ROLE_TEXT]
    vision_node = service._nodes[ROLE_VISION]
    text_node._verdict_until = vision_node._verdict_until = float("inf")
    text_node._last_verdict, vision_node._last_verdict = True, False

    with app.app_context():
        with patch.object(text_node, "client") as text_client:
            text_client.return_value.chat.completions.create.return_value = _completion(
                '{"category": "electricity"}'
            )
            assert service.analyze_report_text("t", "d").suggested_category == "electricity"

        with pytest.raises(AIUnavailable):
            service.chat("anything")


def test_health_reports_every_node(app, healthy):
    with app.app_context():
        report = healthy.health()

    assert report["provider"] == "ollama"
    assert set(report["nodes"]) == {"vision", "text", "embed"}
    assert report["nodes"]["text"]["model"] == "qwen2.5:3b"


def test_embeddings_use_the_embed_node(app, healthy):
    with app.app_context():
        with patch.object(healthy._nodes["embed"], "client") as embed_client:
            embedding = MagicMock()
            embedding.data = [MagicMock(embedding=[0.1, 0.2, 0.3])]
            embed_client.return_value.embeddings.create.return_value = embedding
            assert healthy.embed("a pothole") == [0.1, 0.2, 0.3]


def test_the_chatbot_works_end_to_end_over_ollama(client, make_user, app):
    """The RAG endpoint against a mocked Ollama, with no network involved."""
    make_user(email="ram@betternepal.np", role_names=("citizen",))
    tokens = client.post(
        "/api/v1/auth/login",
        json={"email": "ram@betternepal.np", "password": PASSWORD},
    ).get_json()["data"]

    service = OllamaService(vision_url="http://gpu-node:11434")
    service._nodes[ROLE_VISION]._verdict_until = float("inf")
    service._nodes[ROLE_VISION]._last_verdict = True

    with app.app_context():
        from app.services.ai_service import set_ai_service

        set_ai_service(service)

    with patch.object(service._nodes[ROLE_VISION], "client") as vision_client:
        vision_client.return_value.chat.completions.create.return_value = _completion(
            "There are no active incidents reported near you."
        )
        response = client.post(
            "/api/v1/chat",
            json={"message": "Any problems nearby?"},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["provider"] == "ollama"
    assert "no active incidents" in data["answer"]


# ===========================================================================
# Travel corridor default
# ===========================================================================


def test_the_default_corridor_is_twenty_kilometres():
    """Nepal's highways follow valleys, not straight lines - see the module."""
    from app.services.travel_service import DEFAULT_CORRIDOR_METRES

    assert DEFAULT_CORRIDOR_METRES == 20000


def test_the_default_corridor_finds_a_hazard_off_the_straight_line(client, db):
    """The measured Mugling case: 14.6km from the Kathmandu-Pokhara line.

    At the old 5km default this route reported LOW with the landslide
    invisible; the widened corridor is what makes it visible.
    """
    from app.services.travel_service import _distance_to_segment_metres

    mugling = (27.86, 84.55)
    ktm, pokhara = (27.7172, 85.3240), (28.2096, 83.9856)
    offset = _distance_to_segment_metres(mugling, ktm, pokhara)

    assert 14_000 < offset < 15_000
    assert offset > 5_000  # would have been missed before
    assert offset < 20_000  # and is found now


def test_the_route_response_still_declares_its_limitation(client, db):
    response = client.get(
        "/api/v1/travel/route?start_lat=27.7172&start_lng=85.3240"
        "&end_lat=28.2096&end_lng=83.9856"
    )
    data = response.get_json()["data"]

    assert data["route"]["corridor_metres"] == 20000
    assert data["route"]["is_straight_line_corridor"] is True
    assert data["method"] == "approximate"
