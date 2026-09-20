"""Road-route geometry: a provider abstraction plus one live implementation.

Everywhere else in this codebase that walks a "route" (``travel_service``)
has, until now, used the straight line between two points - honestly labelled
as such (``is_straight_line_corridor: true``), because there was no road graph
to follow. This module is the first actual road-route source: it wraps the
public OSRM demo API behind :class:`RouteProvider`, so a self-hosted OSRM
instance (or a different provider entirely) can be swapped in later by adding
one class, not by touching ``travel_service``.

The demo server is free and requires no key, but it is shared, rate-limited,
and carries no uptime guarantee - explicitly not for production load per its
own usage policy. Every call is wrapped so a slow or unreachable demo server
degrades to :class:`RouteUnavailable`, never a 500, and the caller decides
what "unavailable" means for it (``travel_service`` falls back to the
straight-line corridor).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import requests
from flask import current_app

from ..gis.location import Coordinates


class RouteUnavailable(RuntimeError):
    """The provider could not produce a route (timeout, error, no route found)."""


@dataclass
class RouteResult:
    geometry: dict[str, Any]  # GeoJSON LineString
    distance_km: float
    duration_minutes: float
    provider: str


# Requested transport modes -> OSRM routing profiles. OSRM has no bus
# profile, so "public_bus" is served by the driving profile - the response's
# own `route.provider`/`route.transport_mode` fields say so, rather than
# silently pretending there is bus-specific routing.
_OSRM_PROFILE = {
    "driving": "driving",
    "public_bus": "driving",
    "trekking": "walking",
}


class RouteProvider(ABC):
    """A source of real route geometry between two points."""

    @abstractmethod
    def get_route(
        self, start: Coordinates, end: Coordinates, mode: str
    ) -> RouteResult:
        """Return a route, or raise :class:`RouteUnavailable`."""


class OSRMRouteProvider(RouteProvider):
    """Routes via an OSRM-compatible HTTP API (the public demo by default)."""

    def __init__(self, base_url: str, timeout_seconds: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def get_route(
        self, start: Coordinates, end: Coordinates, mode: str
    ) -> RouteResult:
        profile = _OSRM_PROFILE.get(mode, "driving")
        url = (
            f"{self.base_url}/route/v1/{profile}/"
            f"{start.longitude},{start.latitude};{end.longitude},{end.latitude}"
        )
        try:
            response = requests.get(
                url,
                params={"overview": "full", "geometries": "geojson"},
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise RouteUnavailable(f"OSRM request failed: {exc}") from None

        if response.status_code != 200:
            raise RouteUnavailable(f"OSRM returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError:
            raise RouteUnavailable("OSRM returned a non-JSON response") from None

        if payload.get("code") != "Ok" or not payload.get("routes"):
            raise RouteUnavailable(
                f"OSRM could not find a route: {payload.get('code', 'unknown error')}"
            )

        route = payload["routes"][0]
        return RouteResult(
            geometry=route["geometry"],
            distance_km=round(route["distance"] / 1000, 2),
            duration_minutes=round(route["duration"] / 60, 1),
            provider="osrm",
        )


def get_route_provider() -> RouteProvider | None:
    """The configured route provider, or ``None`` when routing is unconfigured.

    ``None`` (not an exception) when ``OSRM_BASE_URL`` is blank - a caller
    that has no provider configured should fall back the same way it would if
    a configured one timed out, not treat "no provider" as a different kind
    of failure.
    """
    base_url = current_app.config.get("OSRM_BASE_URL")
    if not base_url:
        return None
    timeout = current_app.config.get("OSRM_TIMEOUT_SECONDS", 8)
    return OSRMRouteProvider(base_url, timeout)
