"""Consistent API response envelopes."""
from __future__ import annotations

from typing import Any

from flask import jsonify
from flask.wrappers import Response


def success_response(data: Any = None, status: int = 200, **extra: Any) -> tuple[Response, int]:
    """Envelope: {"status": "success", "data": ...}."""
    payload: dict[str, Any] = {"status": "success", "data": data}
    payload.update(extra)
    return jsonify(payload), status


def error_response(
    message: str,
    status: int = 400,
    code: str | None = None,
    details: Any = None,
) -> tuple[Response, int]:
    """Envelope: {"status": "error", "error": {...}}."""
    error: dict[str, Any] = {"code": code or f"http_{status}", "message": message}
    if details is not None:
        error["details"] = details
    return jsonify({"status": "error", "error": error}), status


class ApiError(Exception):
    """Raised by services to signal a client-visible failure.

    The application-level handler turns this into an error_response.
    """

    def __init__(
        self,
        message: str,
        status: int = 400,
        code: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.details = details
