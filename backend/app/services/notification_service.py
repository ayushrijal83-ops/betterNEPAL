"""Notification service abstraction for disaster dispatch.

Decouples incident creation from delivery mechanisms (WebSocket, email, SMS, push).
Each adapter implements a simple interface; the service coordinates delivery and records state.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from flask import current_app

from ..extensions import db
from ..models.disaster_incident import DisasterDispatch, DisasterIncident
from ..models.user import User
from ..services.ai_dispatch import DisasterAnalysis


@dataclass
class NotificationResult:
    """Result of a single notification attempt."""
    success: bool
    channel: str
    recipient_id: uuid.UUID
    dispatched_at: datetime | None = None
    failure_reason: str | None = None
    external_id: str | None = None  # e.g., SMS message ID, email message ID


@dataclass
class WebSocketEvent:
    """Deferred WebSocket event to emit after commit."""
    event_name: str
    payload: dict[str, Any]
    room: str


class NotificationChannel(ABC):
    """Abstract base for notification delivery channels."""

    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Unique identifier for this channel (e.g., 'websocket', 'email', 'sms')."""

    @abstractmethod
    def send(
        self,
        incident: DisasterIncident,
        recipient: User,
        dispatch_record: DisasterDispatch,
    ) -> tuple[NotificationResult, list[WebSocketEvent]]:
        """Attempt delivery. Returns result with success/failure details and any deferred WebSocket events."""


class WebSocketChannel(NotificationChannel):
    """Deliver via Flask-SocketIO to authority user's room."""

    @property
    def channel_name(self) -> str:
        return "websocket"

    def send(
        self,
        incident: DisasterIncident,
        recipient: User,
        dispatch_record: DisasterDispatch,
    ) -> tuple[NotificationResult, list[WebSocketEvent]]:
        # Import here to avoid circular dependency
        from ..extensions import socketio

        if not socketio:
            return NotificationResult(
                success=False,
                channel=self.channel_name,
                recipient_id=recipient.id,
                failure_reason="SocketIO not initialized",
            ), []

        try:
            payload = {
                "type": "disaster_incident_alert",
                "incident": incident.to_authority_dict(),
                "dispatch_id": str(dispatch_record.id),
                "priority": incident.severity.value,
            }
            # Send to user's personal room
            room = f"authority:{recipient.id}"

            return NotificationResult(
                success=True,
                channel=self.channel_name,
                recipient_id=recipient.id,
                dispatched_at=datetime.utcnow(),
            ), [
                WebSocketEvent(
                    event_name="disaster_alert",
                    payload=payload,
                    room=room,
                )
            ]
        except Exception as exc:
            current_app.logger.warning(
                "WebSocket delivery failed for user %s: %s", recipient.id, exc
            )
            return NotificationResult(
                success=False,
                channel=self.channel_name,
                recipient_id=recipient.id,
                failure_reason=str(exc),
            ), []


class InAppChannel(NotificationChannel):
    """Create an in-app notification record (stored in DB for dashboard badge)."""

    @property
    def channel_name(self) -> str:
        return "in_app"

    def send(
        self,
        incident: DisasterIncident,
        recipient: User,
        dispatch_record: DisasterDispatch,
    ) -> tuple[NotificationResult, list[WebSocketEvent]]:
        # For now, the DisasterDispatch record itself serves as the in-app notification
        # The authority dashboard polls or receives WebSocket for these
        return NotificationResult(
            success=True,
            channel=self.channel_name,
            recipient_id=recipient.id,
            dispatched_at=datetime.utcnow(),
        ), []


# Channel registry - add new channels here
_CHANNELS: dict[str, NotificationChannel] = {
    "websocket": WebSocketChannel(),
    "in_app": InAppChannel(),
}


def register_channel(channel: NotificationChannel) -> None:
    """Register a custom notification channel."""
    _CHANNELS[channel.channel_name] = channel


def get_channel(name: str) -> NotificationChannel | None:
    """Get a channel by name."""
    return _CHANNELS.get(name)


def notify_authority(
    incident: DisasterIncident,
    recipient: User,
    authority,
    channels: list[str] | None = None,
) -> list[NotificationResult]:
    """Send disaster alert to an authority user via specified channels.

    Creates a DisasterDispatch record for audit trail, attempts delivery
    via each channel, and updates the record with results.
    WebSocket events are emitted AFTER the database commit.

    Args:
        incident: The DisasterIncident to notify about
        recipient: The User to notify
        authority: The Authority this recipient represents
        channels: List of channel names to use. Defaults to ['websocket', 'in_app'].

    Returns:
        List of NotificationResult for each channel attempted.
    """
    if channels is None:
        channels = ["websocket", "in_app"]

    results = []
    websocket_events = []

    for channel_name in channels:
        channel = get_channel(channel_name)
        if not channel:
            current_app.logger.warning("Unknown notification channel: %s", channel_name)
            results.append(
                NotificationResult(
                    success=False,
                    channel=channel_name,
                    recipient_id=recipient.id,
                    failure_reason=f"Unknown channel: {channel_name}",
                )
            )
            continue

        # Create dispatch record for this attempt
        dispatch = DisasterDispatch(
            disaster_incident_id=incident.id,
            authority_user_id=recipient.id,
            status="pending",
            channel=channel_name,
        )
        db.session.add(dispatch)
        db.session.flush()

        # Attempt delivery
        result, ws_events = channel.send(incident, recipient, dispatch)
        websocket_events.extend(ws_events)

        # Update dispatch record with result
        if result.success:
            dispatch.status = "delivered"
            dispatch.notified_at = result.dispatched_at or datetime.utcnow()
            dispatch.delivered_at = result.dispatched_at
        else:
            dispatch.status = "failed"
            dispatch.failed_at = datetime.utcnow()
            dispatch.failure_reason = result.failure_reason

        results.append(result)

    db.session.commit()

    # Emit WebSocket events AFTER commit
    from ..extensions import socketio
    if socketio:
        for event in websocket_events:
            socketio.emit(event.event_name, event.payload, room=event.room)

    return results


def notify_authorities_for_incident(
    incident: DisasterIncident,
    authority_users: list[tuple[Any, list[User]]],
    channels: list[str] | None = None,
) -> dict[str, Any]:
    """Notify all authority users for an incident.

    Args:
        incident: The DisasterIncident
        authority_users: List of (authority, [users]) from jurisdiction service
        channels: Channels to use for delivery

    Returns:
        Summary dict with notification results.

    A user can legitimately appear under more than one authority here - e.g.
    someone with the authority role matches both their own district's office
    and every national authority, since a national authority's remit is not
    scoped to any one district's users. The dispatch table's own constraint
    is one row per (incident, user, channel), not per (incident, authority,
    user, channel), so this dedupes by user before notifying: the DB is the
    one making the "one notification per person" rule, this just avoids
    trying to insert a second row it would reject anyway.
    """
    seen_user_ids: set[Any] = set()
    deduped: list[tuple[Any, User]] = []
    for authority, users in authority_users:
        for user in users:
            if user.id in seen_user_ids:
                continue
            seen_user_ids.add(user.id)
            deduped.append((authority, user))

    total_users = len(deduped)
    notified = 0
    failed = 0
    details = []

    for authority, user in deduped:
        results = notify_authority(incident, user, authority, channels)
        for r in results:
            if r.success:
                notified += 1
            else:
                failed += 1
        details.append({
            "authority_id": str(authority.id),
            "authority_name": authority.name,
            "user_id": str(user.id),
            "user_name": user.full_name,
            "results": [
                {"channel": r.channel, "success": r.success, "failure_reason": r.failure_reason}
                for r in results
            ],
        })

    return {
        "total_recipients": total_users,
        "notified": notified,
        "failed": failed,
        "details": details,
    }