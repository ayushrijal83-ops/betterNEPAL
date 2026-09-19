"""Tests for Notification Service."""
from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.models.disaster_incident import DisasterIncident, DisasterDispatch
from app.models.enums import DisasterIncidentStatus, DisasterSeverity, DisasterType
from app.models.user import User
from app.models.role import Role
from app.models.authority import Authority
from app.models.enums import AuthorityType, GovernmentLevel
from app.services.notification_service import (
    InAppChannel,
    WebSocketChannel,
    WebSocketEvent,
    get_channel,
    notify_authorities_for_incident,
    notify_authority,
    register_channel,
)


@pytest.fixture
def app():
    from app import create_app
    application = create_app("testing")
    application.config.update(TESTING=True)
    return application


def _create_incident(db, title="Test", description="Test", **kwargs):
    """Helper to create a disaster incident and return it."""
    incident = DisasterIncident(
        id=uuid.uuid4(),
        title=title,
        description=description,
        disaster_type=kwargs.get("disaster_type", DisasterType.LANDSLIDE),
        severity=kwargs.get("severity", DisasterSeverity.HIGH),
        status=kwargs.get("status", DisasterIncidentStatus.DETECTED),
        latitude=kwargs.get("latitude", 27.7),
        longitude=kwargs.get("longitude", 85.3),
    )
    db.session.add(incident)
    db.session.flush()
    db.session.commit()
    return incident


def _create_authority(db, name="Test Authority", level=GovernmentLevel.LOCAL, type=AuthorityType.MUNICIPAL_OFFICE):
    """Helper to create an authority and return it."""
    auth = Authority(
        id=uuid.uuid4(),
        name=name,
        level=level,
        type=type,
        district_id=None,
    )
    db.session.add(auth)
    db.session.flush()
    db.session.commit()
    return auth


# --- WebSocketEvent tests -----------------------------------------------------


def test_websocket_event_creation():
    event = WebSocketEvent(
        event_name="test_event",
        payload={"key": "value"},
        room="room:123",
    )
    assert event.event_name == "test_event"
    assert event.payload == {"key": "value"}
    assert event.room == "room:123"


# --- Channel tests ------------------------------------------------------------


def test_in_app_channel_send(db):
    """InAppChannel returns success without events."""
    from app.services.auth_service import hash_password
    incident = _create_incident(db, title="Test", description="Test")
    recipient = User(id=uuid.uuid4(), email="test@test.np", password_hash=hash_password("test"), full_name="Test User", is_active=True)
    db.session.add(recipient)
    db.session.commit()

    dispatch = DisasterDispatch(id=uuid.uuid4(), disaster_incident_id=incident.id, authority_user_id=recipient.id)

    channel = InAppChannel()
    result, events = channel.send(incident, recipient, dispatch)

    assert result.success is True
    assert result.channel == "in_app"
    assert result.recipient_id == recipient.id
    assert events == []


def test_websocket_channel_no_socketio(app):
    """WebSocketChannel returns failure when SocketIO not initialized."""
    with app.app_context():
        from app.models.disaster_incident import DisasterIncident
        from app.models.enums import DisasterType, DisasterSeverity

        incident = DisasterIncident(
            id=uuid.uuid4(),
            title="Test",
            description="Test",
            disaster_type=DisasterType.LANDSLIDE,
            severity=DisasterSeverity.HIGH,
            status="detected",
            latitude=27.7,
            longitude=85.3,
        )
        recipient = User(id=uuid.uuid4(), email="test@test.np")
        dispatch = DisasterDispatch(id=uuid.uuid4(), disaster_incident_id=incident.id, authority_user_id=recipient.id)

        channel = WebSocketChannel()
        with patch("app.extensions.socketio", None):
            result, events = channel.send(incident, recipient, dispatch)

        assert result.success is False
        assert result.failure_reason == "SocketIO not initialized"
        assert events == []


def test_websocket_channel_success(app, monkeypatch):
    """WebSocketChannel returns success with event."""
    with app.app_context():
        from app.models.disaster_incident import DisasterIncident
        from app.models.enums import DisasterType, DisasterSeverity, DisasterIncidentStatus

        incident = DisasterIncident(
            id=uuid.uuid4(),
            title="Test",
            description="Test",
            disaster_type=DisasterType.LANDSLIDE,
            severity=DisasterSeverity.HIGH,
            status=DisasterIncidentStatus.DETECTED,
            latitude=27.7,
            longitude=85.3,
        )
        recipient = User(id=uuid.uuid4(), email="test@test.np", full_name="Test User")
        dispatch = DisasterDispatch(id=uuid.uuid4(), disaster_incident_id=incident.id, authority_user_id=recipient.id)

        channel = WebSocketChannel()
        result, events = channel.send(incident, recipient, MagicMock(id=uuid.uuid4()))

        assert result.success is True
        assert result.channel == "websocket"
        assert len(events) == 1
        assert events[0].event_name == "disaster_alert"
        assert events[0].room == f"authority:{recipient.id}"
        assert events[0].payload["type"] == "disaster_incident_alert"
        # The actual emit happens in notify_authority after commit, not in channel.send


def test_websocket_channel_exception(app, monkeypatch):
    """WebSocketChannel handles exceptions."""
    with app.app_context():
        from app.models.disaster_incident import DisasterIncident
        from app.models.enums import DisasterType, DisasterSeverity, DisasterIncidentStatus

        incident = DisasterIncident(
            id=uuid.uuid4(),
            title="Test",
            description="Test",
            disaster_type=DisasterType.LANDSLIDE,
            severity=DisasterSeverity.HIGH,
            status=DisasterIncidentStatus.DETECTED,
            latitude=27.7,
            longitude=85.3,
        )
        recipient = User(id=uuid.uuid4(), email="test@test.np")
        dispatch = DisasterDispatch(id=uuid.uuid4(), disaster_incident_id=incident.id, authority_user_id=recipient.id)

        # The WebSocketChannel.send method catches exceptions and returns a failure result
        # So we need to simulate an exception in the channel.send method itself
        channel = WebSocketChannel()
        # Force an exception by passing an invalid incident that causes an error in to_authority_dict
        incident_with_error = MagicMock()
        incident_with_error.to_authority_dict.side_effect = Exception("connection failed")

        result, events = channel.send(incident_with_error, recipient, MagicMock(id=uuid.uuid4()))

        assert result.success is False
        assert "connection failed" in result.failure_reason
        assert events == []


# --- register_channel tests ---------------------------------------------------


def test_register_channel(app):
    """Custom channel can be registered."""
    with app.app_context():
        from app.services.notification_service import _CHANNELS

        class CustomChannel:
            channel_name = "custom"

            def send(self, incident, recipient, dispatch):
                return (None, [])

        custom = CustomChannel()
        register_channel(custom)
        assert _CHANNELS["custom"] is custom


# --- notify_authority tests ---------------------------------------------------


def test_notify_authority_websocket_in_app(app, db, monkeypatch):
    """notify_authority creates dispatch records and commits."""
    with app.app_context():
        from app.services.auth_service import hash_password
        recipient = User(id=uuid.uuid4(), email="authority@test.np", password_hash=hash_password("test"), full_name="Test Authority", is_active=True)
        role = db.session.query(Role).filter(Role.name == "authority").first()
        if role:
            recipient.roles.append(role)
        db.session.add(recipient)
        db.session.commit()

        incident = _create_incident(db, title="Test", description="Test")

        mock_socketio = MagicMock()
        monkeypatch.setattr("app.extensions.socketio", mock_socketio)

        with patch("app.services.notification_service.get_channel") as mock_get:
            mock_channel = MagicMock()
            mock_channel.send.return_value = (
                MagicMock(success=True, channel="websocket", recipient_id=recipient.id, dispatched_at=datetime.utcnow()),
                [MagicMock(event_name="disaster_alert", payload={}, room=f"authority:{recipient.id}")],
            )
            mock_get.return_value = mock_channel

            results = notify_authority(incident, recipient, MagicMock(id=uuid.uuid4()))

        assert len(results) == 2  # websocket + in_app
        for r in results:
            assert r.success is True

        # Verify dispatch records created
        from app.models.disaster_incident import DisasterDispatch
        dispatches = db.session.query(DisasterDispatch).filter_by(disaster_incident_id=incident.id).all()
        assert len(dispatches) == 2


def test_notify_authority_unknown_channel(app):
    """Unknown channel returns failure result."""
    with app.app_context():
        recipient = User(id=uuid.uuid4(), email="test@test.np")
        incident = DisasterIncident(
            id=uuid.uuid4(),
            title="Test",
            description="Test",
            disaster_type=DisasterType.LANDSLIDE,
            severity=DisasterSeverity.HIGH,
            status="detected",
            latitude=27.7,
            longitude=85.3,
        )

        results = notify_authority(incident, recipient, MagicMock(id=uuid.uuid4()), channels=["unknown_channel"])

        assert len(results) == 1
        assert results[0].success is False
        assert "unknown_channel" in results[0].failure_reason


def test_notify_authority_websocket_failure_still_creates_dispatch(app, db, monkeypatch):
    """Failed websocket still creates dispatch record with failed status."""
    with app.app_context():
        from app.services.auth_service import hash_password
        recipient = User(id=uuid.uuid4(), email="authority@test.np", password_hash=hash_password("test"), full_name="Test Authority", is_active=True)
        role = db.session.query(Role).filter(Role.name == "authority").first()
        if role:
            recipient.roles.append(role)
        db.session.add(recipient)
        db.session.commit()

        incident = _create_incident(db, title="Test", description="Test")

        monkeypatch.setattr("app.extensions.socketio", None)

        with patch("app.services.notification_service.get_channel") as mock_get:
            mock_channel = MagicMock()
            mock_channel.send.return_value = (
                MagicMock(success=False, channel="websocket", recipient_id=recipient.id, failure_reason="SocketIO not initialized"),
                [],
            )
            mock_get.return_value = mock_channel

            results = notify_authority(incident, recipient, MagicMock(id=uuid.uuid4()), channels=["websocket"])

        assert results[0].success is False

        from app.models.disaster_incident import DisasterDispatch
        dispatch = db.session.query(DisasterDispatch).filter_by(disaster_incident_id=incident.id).first()
        assert dispatch is not None
        assert dispatch.status == "failed"
        assert dispatch.failure_reason == "SocketIO not initialized"


# --- notify_authorities_for_incident tests ------------------------------------


def test_notify_authorities_for_incident(app, db, monkeypatch):
    """Notify all authority users for an incident."""
    with app.app_context():
        from app.services.auth_service import hash_password
        recipient = User(id=uuid.uuid4(), email="authority@test.np", password_hash=hash_password("test"), full_name="Test Authority", is_active=True)
        role = db.session.query(Role).filter(Role.name == "authority").first()
        if role:
            recipient.roles.append(role)
        db.session.add(recipient)
        db.session.commit()

        authority = _create_authority(db)

        incident = _create_incident(db, title="Test", description="Test")

        mock_socketio = MagicMock()
        monkeypatch.setattr("app.extensions.socketio", mock_socketio)

        with patch("app.services.notification_service.get_channel") as mock_get:
            mock_channel = MagicMock()
            mock_channel.send.return_value = (
                MagicMock(success=True, channel="websocket", recipient_id=recipient.id, dispatched_at=datetime.utcnow()),
                [],
            )
            mock_get.return_value = mock_channel

            result = notify_authorities_for_incident(incident, [(authority, [recipient])])

        assert result["total_recipients"] == 1
        # 2 channels (websocket + in_app) both succeed, so 2 notifications
        assert result["notified"] == 2
        assert result["failed"] == 0
        assert len(result["details"]) == 1


def test_notify_authorities_for_incident_dedupes_a_user_under_two_authorities(app, db, monkeypatch):
    """A real gap this closes: find_authority_users_for_jurisdiction can list
    the same person twice - once as a district authority's user, once again
    under a national authority, whose remit is not scoped to any one
    district's users. DisasterDispatch's unique constraint is one row per
    (incident, user, channel), not per (incident, authority, user, channel),
    so notifying the same person via two authority entries must not attempt
    two inserts.
    """
    with app.app_context():
        from app.services.auth_service import hash_password

        recipient = User(
            id=uuid.uuid4(), email="dual-authority@test.np",
            password_hash=hash_password("test"), full_name="Dual Authority", is_active=True,
        )
        role = db.session.query(Role).filter(Role.name == "authority").first()
        if role:
            recipient.roles.append(role)
        db.session.add(recipient)
        db.session.commit()

        district_authority = _create_authority(db, name="District Office")
        national_authority = _create_authority(db, name="National Office")

        incident = _create_incident(db, title="Test", description="Test")

        mock_socketio = MagicMock()
        monkeypatch.setattr("app.extensions.socketio", mock_socketio)

        with patch("app.services.notification_service.get_channel") as mock_get:
            mock_channel = MagicMock()
            mock_channel.send.return_value = (
                MagicMock(success=True, channel="websocket", recipient_id=recipient.id, dispatched_at=datetime.utcnow()),
                [],
            )
            mock_get.return_value = mock_channel

            # The same user appears under both authorities, exactly as
            # find_authority_users_for_jurisdiction can legitimately return it.
            result = notify_authorities_for_incident(
                incident,
                [(district_authority, [recipient]), (national_authority, [recipient])],
            )

        assert result["total_recipients"] == 1
        assert len(result["details"]) == 1
        # No IntegrityError from a second DisasterDispatch row for the same
        # (incident, user, channel) - this is the assertion that matters.
        from app.models.disaster_incident import DisasterDispatch
        dispatches = db.session.query(DisasterDispatch).filter_by(
            disaster_incident_id=incident.id, authority_user_id=recipient.id
        ).all()
        assert len(dispatches) == len({d.channel for d in dispatches})


def test_notify_authorities_partial_failure(app, db, monkeypatch):
    """Some users succeed, some fail."""
    with app.app_context():
        from app.services.auth_service import hash_password
        recipient1 = User(id=uuid.uuid4(), email="authority1@test.np", password_hash=hash_password("test"), full_name="Authority 1", is_active=True)
        role = db.session.query(Role).filter(Role.name == "authority").first()
        if role:
            recipient1.roles.append(role)
        db.session.add(recipient1)
        db.session.commit()

        recipient2 = User(id=uuid.uuid4(), email="authority2@test.np", password_hash=hash_password("test"), full_name="Authority 2", is_active=True)
        if role:
            recipient2.roles.append(role)
        db.session.add(recipient2)
        db.session.commit()

        authority = _create_authority(db)

        incident = _create_incident(db, title="Test", description="Test")

        mock_socketio = MagicMock()
        monkeypatch.setattr("app.extensions.socketio", mock_socketio)

        with patch("app.services.notification_service.get_channel") as mock_get:
            mock_channel = MagicMock()
            # First user succeeds (2 channels), second user fails (2 channels)
            mock_channel.send.side_effect = [
                (MagicMock(success=True, channel="websocket", recipient_id=recipient1.id, dispatched_at=datetime.utcnow()), []),
                (MagicMock(success=True, channel="in_app", recipient_id=recipient1.id, dispatched_at=datetime.utcnow()), []),
                (MagicMock(success=False, channel="websocket", recipient_id=recipient2.id, failure_reason="connection failed"), []),
                (MagicMock(success=False, channel="in_app", recipient_id=recipient2.id, failure_reason="connection failed"), []),
            ]
            mock_get.return_value = mock_channel

            result = notify_authorities_for_incident(incident, [(authority, [recipient1, recipient2])])

        assert result["total_recipients"] == 2
        # recipient1: 2 channels succeed = 2 notified, recipient2: 2 channels fail = 2 failed
        assert result["notified"] == 2
        assert result["failed"] == 2