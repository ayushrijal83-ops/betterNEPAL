"""Chatbot endpoint.

Authenticated. Two reasons, and the second is the operative one: every message
costs a model call, and an open endpoint that bills or pins a GPU per request
is a denial-of-wallet. Requiring a token also makes abuse attributable.
"""
from __future__ import annotations

from flask import Blueprint, request

from ..extensions import limiter
from ..services import chat_service
from ..utils.decorators import require_auth
from ..utils.helpers import success_response
from ..utils.validators import validate_chat

chat_bp = Blueprint("chat", __name__, url_prefix="/chat")

# A chat turn pins a model for seconds; the same budget as the other AI routes.
AI_LIMIT = "20 per minute"


@chat_bp.post("")
@limiter.limit(AI_LIMIT)
@require_auth
def chat():
    """Answer a question from the platform's own records.

    Body: ``{"message": "...", "district_id": "optional-uuid"}``.

    The response carries the ``sources`` the answer was built from and a
    ``grounded`` flag. Both are there so a reader can check the answer rather
    than trust it: when ``grounded`` is false the model was told it had no
    records and asked to say so, instead of answering from training data - an
    invented road closure is worse than no answer at all.
    """
    data = validate_chat(request.get_json(silent=True))
    return success_response(
        chat_service.ask(data["message"], data["district_id"])
    )


@chat_bp.get("/context")
@require_auth
def context_preview():
    """What the assistant would be given for a question right now.

    Exists so the retrieval half can be inspected without spending a model
    call - when an answer looks wrong, the first question is always whether the
    context was empty or wrong, and this answers it directly.
    """
    district_id = request.args.get("district_id") or None
    context = chat_service.gather_context(district_id)
    text = chat_service.format_context(context)

    return success_response(
        {
            "district_id": district_id,
            "grounded": bool(text),
            "context_text": text,
            "announcement_count": len(context["announcements"]),
            "incident_count": len(context["incidents"]),
        }
    )
