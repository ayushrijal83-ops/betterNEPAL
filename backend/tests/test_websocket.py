"""Tests for WebSocket Handlers."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.websocket import handle_connect, handle_join_district, handle_subscribe_incidents


# Mock socketio for testing
@pytest.fixture
def mock_socketio():
    with patch("app.websocket.socketio") as mock:
        yield mock


@pytest.fixture
def mock_emit():
    with patch("app.websocket.emit") as mock:
        yield mock


# --- connect tests ------------------------------------------------------------


def test_connect_anonymous(mock_emit):
    """Anonymous user connects but gets no private rooms."""
    with patch("app.websocket.current_user", return_value=None):
        handle_connect()

    mock_emit.assert_called_once_with("connected", {"authenticated": False})


def test_connect_authority(mock_emit, monkeypatch):
    """Authority user connects and joins personal and district rooms."""
    from app.models.user import User

    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role_names = ["authority"]
    user.temporary_district_id = uuid.uuid4()
    user.permanent_district_id = None
    user.has_role.return_value = True

    with patch("app.websocket.current_user", return_value=user):
        with patch("app.websocket.join_room") as mock_join:
            handle_connect()

    mock_emit.assert_called_once()
    call_args = mock_emit.call_args[0]
    assert call_args[0] == "connected"
    assert call_args[1]["authenticated"] is True
    assert str(call_args[1]["user_id"]) == str(user.id)
    assert call_args[1]["roles"] == ["authority"]

    # Should join personal room and district room
    assert mock_join.call_count == 2
    rooms_joined = [call[0][0] for call in mock_join.call_args_list]
    assert f"authority:{user.id}" in rooms_joined
    assert f"district:{user.temporary_district_id}" in rooms_joined


def test_connect_admin(mock_emit, monkeypatch):
    """Admin user connects and joins rooms."""
    from app.models.user import User

    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role_names = ["admin"]
    user.temporary_district_id = None
    user.permanent_district_id = uuid.uuid4()
    user.has_role.return_value = True

    with patch("app.websocket.current_user", return_value=user):
        with patch("app.websocket.join_room") as mock_join:
            handle_connect()

    mock_emit.assert_called_once()
    call_args = mock_emit.call_args[0]
    assert call_args[1]["roles"] == ["admin"]


# --- join_district tests ------------------------------------------------------


def test_join_district_unauthenticated(mock_emit):
    """Unauthenticated user cannot join district."""
    with patch("app.websocket.current_user", return_value=None):
        handle_join_district({"district_id": str(uuid.uuid4())})

    mock_emit.assert_called_with("error", {"message": "Authorization required"})


def test_join_district_citizen(mock_emit):
    """Citizen cannot join district."""
    from app.models.user import User

    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role_names = ["citizen"]
    user.has_role.return_value = False

    with patch("app.websocket.current_user", return_value=user):
        handle_join_district({"district_id": str(uuid.uuid4())})

    mock_emit.assert_called_with("error", {"message": "Authorization required"})


def test_join_district_missing_id(mock_emit):
    """Missing district_id returns error."""
    from app.models.user import User

    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role_names = ["authority"]
    user.has_role.return_value = True

    with patch("app.websocket.current_user", return_value=user):
        handle_join_district({})

    mock_emit.assert_called_with("error", {"message": "district_id required"})


def test_join_district_admin_any(mock_emit, monkeypatch):
    """Admin can join any district."""
    from app.models.user import User

    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role_names = ["admin"]
    user.has_role.return_value = True

    district_id = str(uuid.uuid4())

    with patch("app.websocket.current_user", return_value=user):
        with patch("app.websocket.join_room") as mock_join:
            handle_join_district({"district_id": district_id})

    mock_join.assert_called_once_with(f"district:{district_id}")
    mock_emit.assert_called_with("joined_district", {"district_id": district_id})


def test_join_district_authority_own_district(mock_emit, monkeypatch):
    """Authority can join their own temporary district."""
    from app.models.user import User

    district_id = str(uuid.uuid4())
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role_names = ["authority"]
    user.temporary_district_id = uuid.UUID(district_id)
    user.permanent_district_id = None
    user.has_role.return_value = True

    with patch("app.websocket.current_user", return_value=user):
        with patch("app.websocket.join_room") as mock_join:
            handle_join_district({"district_id": district_id})

    mock_join.assert_called_once_with(f"district:{district_id}")
    mock_emit.assert_called_with("joined_district", {"district_id": district_id})


def test_join_district_authority_permanent_district(mock_emit, monkeypatch):
    """Authority can join their permanent district."""
    from app.models.user import User

    district_id = str(uuid.uuid4())
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role_names = ["authority"]
    user.temporary_district_id = None
    user.permanent_district_id = uuid.UUID(district_id)
    user.has_role.return_value = True

    with patch("app.websocket.current_user", return_value=user):
        with patch("app.websocket.join_room") as mock_join:
            handle_join_district({"district_id": district_id})

    mock_join.assert_called_once_with(f"district:{district_id}")
    mock_emit.assert_called_with("joined_district", {"district_id": district_id})


def test_join_district_authority_authority_covers(mock_emit, monkeypatch):
    """Authority can join district their authority covers."""
    from app.models.user import User

    district_id = str(uuid.uuid4())
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role_names = ["authority"]
    user.temporary_district_id = None
    user.permanent_district_id = None
    user.has_role.return_value = True

    # Authority covers the district
    auth = MagicMock()
    auth.district_id = uuid.UUID(district_id)

    with patch("app.websocket.current_user", return_value=user):
        with patch("app.websocket.join_room") as mock_join:
            with patch("app.websocket.db") as mock_db:
                mock_auth_query = MagicMock()
                mock_auth_query.all.return_value = [auth]
                mock_db.session.scalars.return_value = mock_auth_query

                handle_join_district({"district_id": district_id})

    mock_join.assert_called_once_with(f"district:{district_id}")
    mock_emit.assert_called_with("joined_district", {"district_id": district_id})


def test_join_district_authority_national_authority(mock_emit, monkeypatch):
    """Authority with national authority can join any district."""
    from app.models.user import User

    district_id = str(uuid.uuid4())
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role_names = ["authority"]
    user.temporary_district_id = None
    user.permanent_district_id = None
    user.has_role.return_value = True

    # National authority (district_id = None)
    auth = MagicMock()
    auth.district_id = None

    with patch("app.websocket.current_user", return_value=user):
        with patch("app.websocket.join_room") as mock_join:
            with patch("app.websocket.db") as mock_db:
                mock_auth_query = MagicMock()
                mock_auth_query.all.return_value = [auth]
                mock_db.session.scalars.return_value = mock_auth_query

                handle_join_district({"district_id": district_id})

    mock_join.assert_called_once_with(f"district:{district_id}")
    mock_emit.assert_called_with("joined_district", {"district_id": district_id})


def test_join_district_authority_unauthorized(mock_emit):
    """Authority without permission cannot join district."""
    from app.models.user import User

    district_id = str(uuid.uuid4())
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role_names = ["authority"]
    user.temporary_district_id = None
    user.permanent_district_id = None
    user.has_role.return_value = True

    # Authority does not cover this district
    auth = MagicMock()
    auth.district_id = uuid.uuid4()  # Different district

    with patch("app.websocket.current_user", return_value=user):
        with patch("app.websocket.db") as mock_db:
            mock_auth_query = MagicMock()
            mock_auth_query.all.return_value = [MagicMock(district_id=uuid.uuid4())]
            mock_db.session.scalars.return_value = mock_auth_query

            handle_join_district({"district_id": district_id})

    mock_emit.assert_called_with("error", {"message": "Not authorized for this district"})


# --- subscribe_incidents tests ------------------------------------------------


def test_subscribe_incidents_unauthenticated(mock_emit):
    """Unauthenticated user cannot subscribe."""
    with patch("app.websocket.current_user", return_value=None):
        handle_subscribe_incidents({})

    mock_emit.assert_called_with("error", {"message": "Authorization required"})


def test_subscribe_incidents_citizen(mock_emit):
    """Citizen cannot subscribe."""
    from app.models.user import User

    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role_names = ["citizen"]
    user.has_role.return_value = False

    with patch("app.websocket.current_user", return_value=user):
        handle_subscribe_incidents({})

    mock_emit.assert_called_with("error", {"message": "Authorization required"})


def test_subscribe_incidents_specific_incident(mock_emit, monkeypatch):
    """Subscribe to specific incident with authorization."""
    from app.models.user import User

    incident_id = str(uuid.uuid4())
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role_names = ["authority"]
    user.temporary_district_id = uuid.uuid4()
    user.permanent_district_id = None
    user.has_role.return_value = True

    incident = MagicMock()
    incident.id = uuid.UUID(incident_id)
    incident.district_id = user.temporary_district_id

    with patch("app.websocket.current_user", return_value=user):
        with patch("app.websocket.join_room") as mock_join:
            with patch("app.websocket.db") as mock_db:
                mock_incident_query = MagicMock()
                mock_incident_query.get.return_value = incident
                mock_db.session.get.return_value = incident

                handle_subscribe_incidents({"incident_id": incident_id})

    mock_join.assert_called_once_with(f"incident:{incident_id}")
    mock_emit.assert_called_with("subscribed_incident", {"incident_id": incident_id})


def test_subscribe_incidents_unauthorized_incident(mock_emit, monkeypatch):
    """Cannot subscribe to incident in unauthorized district."""
    from app.models.user import User

    incident_id = str(uuid.uuid4())
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role_names = ["authority"]
    user.temporary_district_id = uuid.uuid4()  # Different district
    user.permanent_district_id = None
    user.has_role.return_value = True

    incident = MagicMock()
    incident.id = uuid.UUID(incident_id)
    incident.district_id = uuid.uuid4()  # Different district

    with patch("app.websocket.current_user", return_value=user):
        with patch("app.websocket.db") as mock_db:
            mock_db.session.get.return_value = incident

            handle_subscribe_incidents({"incident_id": incident_id})

    mock_emit.assert_called_with("error", {"message": "Not authorized for this incident"})


def test_subscribe_incidents_all_user_district(mock_emit, monkeypatch):
    """Subscribe to all incidents in user's district."""
    from app.models.user import User

    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role_names = ["authority"]
    user.temporary_district_id = uuid.uuid4()
    user.permanent_district_id = None
    user.has_role.return_value = True

    with patch("app.websocket.current_user", return_value=user):
        with patch("app.websocket.join_room") as mock_join:
            handle_subscribe_incidents({})

    mock_join.assert_called_once_with(f"incidents:district:{user.temporary_district_id}")
    mock_emit.assert_called_with("subscribed_incidents", {"district": True})


def test_subscribe_incidents_admin(mock_emit, monkeypatch):
    """Admin can subscribe to specific incident."""
    from app.models.user import User

    incident_id = str(uuid.uuid4())
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role_names = ["admin"]
    user.has_role.return_value = True

    incident = MagicMock()
    incident.id = uuid.UUID(incident_id)
    incident.district_id = uuid.uuid4()

    with patch("app.websocket.current_user", return_value=user):
        with patch("app.websocket.join_room") as mock_join:
            with patch("app.websocket.db") as mock_db:
                mock_db.session.get.return_value = incident

                handle_subscribe_incidents({"incident_id": incident_id})

    mock_join.assert_called_once_with(f"incident:{incident_id}")
    mock_emit.assert_called_with("subscribed_incident", {"incident_id": incident_id})


# --- disconnect tests ---------------------------------------------------------


def test_disconnect_authority(mock_emit):
    """Authority user leaves rooms on disconnect."""
    from app.models.user import User

    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.temporary_district_id = uuid.uuid4()
    user.permanent_district_id = None

    with patch("app.websocket.current_user", return_value=user):
        with patch("app.websocket.leave_room") as mock_leave:
            handle_disconnect()

    assert mock_leave.call_count == 2
    rooms_left = [call[0][0] for call in mock_leave.call_args_list]
    assert f"authority:{user.id}" in rooms_left
    assert f"district:{user.temporary_district_id}" in rooms_left


def test_disconnect_no_user():
    """No user on disconnect does nothing."""
    with patch("app.websocket.current_user", return_value=None):
        with patch("app.websocket.leave_room") as mock_leave:
            handle_disconnect()

    mock_leave.assert_not_called()