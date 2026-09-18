"""Phase 1: app factory, config, health, errors."""
import pytest

from app import create_app
from app.config import DevelopmentConfig, ProductionConfig, get_config
from app.utils.helpers import ApiError


def test_factory_builds_testing_app(app):
    assert app.config["TESTING"] is True
    assert app.config["ENV_NAME"] == "testing"
    assert app.config["API_PREFIX"] == "/api/v1"


def test_get_config_resolves_by_name():
    assert get_config("development") is DevelopmentConfig
    assert get_config("production") is ProductionConfig


def test_get_config_rejects_unknown_name():
    with pytest.raises(ValueError):
        get_config("staging")


def test_production_config_rejects_placeholder_secret(monkeypatch):
    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", "change-me-in-production")
    with pytest.raises(RuntimeError):
        create_app("production")


def test_production_config_rejects_wildcard_cors(monkeypatch):
    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", "a-real-long-random-secret")
    monkeypatch.setattr(ProductionConfig, "CORS_ORIGINS", ["*"])
    with pytest.raises(RuntimeError):
        create_app("production")


def test_health_endpoint(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "success"
    assert body["data"]["status"] == "ok"
    assert body["data"]["environment"] == "testing"
    assert body["data"]["api_version"] == "v1"


def test_hello_endpoint(client):
    response = client.get("/api/hello")
    assert response.status_code == 200
    assert response.get_json()["data"]["message"] == "Hello from Better Nepal backend!"


def test_unknown_route_returns_json_404(client):
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    body = response.get_json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "not_found"


def test_wrong_method_returns_json_405(client):
    response = client.post("/api/v1/health")
    assert response.status_code == 405
    assert response.get_json()["status"] == "error"


def test_api_error_is_rendered_as_json(app, client):
    @app.get("/api/v1/boom")
    def boom():
        raise ApiError("nope", status=422, code="unprocessable", details={"field": "x"})

    response = client.get("/api/v1/boom")
    assert response.status_code == 422
    error = response.get_json()["error"]
    assert error == {"code": "unprocessable", "message": "nope", "details": {"field": "x"}}


def test_unexpected_error_is_not_leaked(app, client):
    @app.get("/api/v1/kaboom")
    def kaboom():
        raise ValueError("secret internal detail")

    response = client.get("/api/v1/kaboom")
    assert response.status_code == 500
    body = response.get_json()
    assert body["error"]["message"] == "An internal error occurred."
    assert "secret internal detail" not in response.get_data(as_text=True)


def test_cors_headers_on_api_routes(client):
    response = client.get("/api/v1/health", headers={"Origin": "http://localhost:5500"})
    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5500"
