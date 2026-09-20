"""Tests for the OSRM route provider abstraction."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.gis.location import Coordinates
from app.services.routing_service import (
    OSRMRouteProvider,
    RouteUnavailable,
    get_route_provider,
)


@pytest.fixture
def app():
    from app import create_app

    application = create_app("testing")
    application.config.update(TESTING=True)
    return application


START = Coordinates.parse(latitude=27.7172, longitude=85.3240)
END = Coordinates.parse(latitude=28.2096, longitude=83.9856)

_OSRM_OK_PAYLOAD = {
    "code": "Ok",
    "routes": [
        {
            "geometry": {
                "type": "LineString",
                "coordinates": [[85.324, 27.7172], [84.5, 28.0], [83.9856, 28.2096]],
            },
            "distance": 199840.0,
            "duration": 10440.0,
        }
    ],
}


def _mock_response(status_code=200, json_payload=None, raise_on_json=False):
    response = MagicMock()
    response.status_code = status_code
    if raise_on_json:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = json_payload or {}
    return response


def test_osrm_success_parses_geometry_distance_duration():
    provider = OSRMRouteProvider("https://router.project-osrm.org", timeout_seconds=8)
    with patch("app.services.routing_service.requests.get") as mock_get:
        mock_get.return_value = _mock_response(200, _OSRM_OK_PAYLOAD)
        result = provider.get_route(START, END, "driving")

    assert result.provider == "osrm"
    assert result.distance_km == 199.84
    assert result.duration_minutes == 174.0
    assert result.geometry["type"] == "LineString"
    assert len(result.geometry["coordinates"]) == 3


def test_osrm_uses_driving_profile_for_driving_and_public_bus():
    provider = OSRMRouteProvider("https://router.project-osrm.org", timeout_seconds=8)
    with patch("app.services.routing_service.requests.get") as mock_get:
        mock_get.return_value = _mock_response(200, _OSRM_OK_PAYLOAD)
        provider.get_route(START, END, "public_bus")
        called_url = mock_get.call_args[0][0]
    assert "/route/v1/driving/" in called_url


def test_osrm_uses_walking_profile_for_trekking():
    provider = OSRMRouteProvider("https://router.project-osrm.org", timeout_seconds=8)
    with patch("app.services.routing_service.requests.get") as mock_get:
        mock_get.return_value = _mock_response(200, _OSRM_OK_PAYLOAD)
        provider.get_route(START, END, "trekking")
        called_url = mock_get.call_args[0][0]
    assert "/route/v1/walking/" in called_url


def test_osrm_timeout_raises_route_unavailable():
    import requests

    provider = OSRMRouteProvider("https://router.project-osrm.org", timeout_seconds=1)
    with patch("app.services.routing_service.requests.get") as mock_get:
        mock_get.side_effect = requests.Timeout("timed out")
        with pytest.raises(RouteUnavailable):
            provider.get_route(START, END, "driving")


def test_osrm_non_200_raises_route_unavailable():
    provider = OSRMRouteProvider("https://router.project-osrm.org", timeout_seconds=8)
    with patch("app.services.routing_service.requests.get") as mock_get:
        mock_get.return_value = _mock_response(503)
        with pytest.raises(RouteUnavailable):
            provider.get_route(START, END, "driving")


def test_osrm_no_route_found_raises_route_unavailable():
    provider = OSRMRouteProvider("https://router.project-osrm.org", timeout_seconds=8)
    with patch("app.services.routing_service.requests.get") as mock_get:
        mock_get.return_value = _mock_response(200, {"code": "NoRoute", "routes": []})
        with pytest.raises(RouteUnavailable):
            provider.get_route(START, END, "driving")


def test_osrm_non_json_response_raises_route_unavailable():
    provider = OSRMRouteProvider("https://router.project-osrm.org", timeout_seconds=8)
    with patch("app.services.routing_service.requests.get") as mock_get:
        mock_get.return_value = _mock_response(200, raise_on_json=True)
        with pytest.raises(RouteUnavailable):
            provider.get_route(START, END, "driving")


def test_get_route_provider_returns_none_when_unconfigured(app):
    app.config["OSRM_BASE_URL"] = ""
    with app.app_context():
        assert get_route_provider() is None


def test_get_route_provider_returns_osrm_when_configured(app):
    app.config["OSRM_BASE_URL"] = "https://router.project-osrm.org"
    with app.app_context():
        provider = get_route_provider()
        assert isinstance(provider, OSRMRouteProvider)
