"""WebSocket event handlers for real-time disaster alerts."""
from __future__ import annotations

from flask import request
from flask_socketio import emit, join_room, leave_room, disconnect

from .extensions import socketio
from .utils.permissions import current_user


@socketio.on("connect")
def handle_connect():
    """Handle new WebSocket connection."""
    user = current_user()
    if not user:
        # Allow anonymous connections for public map, but no private rooms
        emit("connected", {"authenticated": False})
        return

    # Join user's personal room for targeted alerts
    join_room(f"authority:{user.id}")
    # Join district room if user has a district
    if user.temporary_district_id:
        join_room(f"district:{user.temporary_district_id}")
    elif user.permanent_district_id:
        join_room(f"district:{user.permanent_district_id}")

    emit("connected", {
        "authenticated": True,
        "user_id": str(user.id),
        "roles": user.role_names,
    })


@socketio.on("disconnect")
def handle_disconnect():
    """Handle WebSocket disconnect."""
    user = current_user()
    if user:
        leave_room(f"authority:{user.id}")
        if user.temporary_district_id:
            leave_room(f"district:{user.temporary_district_id}")
        elif user.permanent_district_id:
            leave_room(f"district:{user.permanent_district_id}")


@socketio.on("join_district")
def handle_join_district(data: dict):
    """Allow authority users to join a district room for live updates.

    Users can only join districts they are authorized for:
    - Authority users: their temporary/permanent district, or districts their authority covers
    - Admin users: any district
    """
    user = current_user()
    if not user or not user.has_role("authority", "admin"):
        emit("error", {"message": "Authorization required"})
        return

    district_id = data.get("district_id")
    if not district_id:
        emit("error", {"message": "district_id required"})
        return

    # Admins can join any district
    if user.has_role("admin"):
        join_room(f"district:{district_id}")
        emit("joined_district", {"district_id": district_id})
        return

    # Authority users: check if they're authorized for this district
    # They can join their own temporary/permanent district
    authorized = False
    if user.temporary_district_id and str(user.temporary_district_id) == district_id:
        authorized = True
    elif user.permanent_district_id and str(user.permanent_district_id) == district_id:
        authorized = True

    # Also check if user has an authority role covering this district
    if not authorized:
        from ..models.authority import Authority
        from sqlalchemy import select
        from ..extensions import db

        user_authorities = db.session.scalars(
            select(Authority).where(Authority.is_active == True)  # noqa: E712
        ).all()
        for auth in user_authorities:
            if auth.district_id is None or str(auth.district_id) == district_id:
                authorized = True
                break

    if not authorized:
        emit("error", {"message": "Not authorized for this district"})
        return

    join_room(f"district:{district_id}")
    emit("joined_district", {"district_id": district_id})


@socketio.on("leave_district")
def handle_leave_district(data: dict):
    """Leave a district room."""
    user = current_user()
    if not user:
        return

    district_id = data.get("district_id")
    if district_id:
        leave_room(f"district:{district_id}")
        emit("left_district", {"district_id": district_id})


@socketio.on("subscribe_incidents")
def handle_subscribe_incidents(data: dict):
    """Subscribe to incident updates for a specific incident or all."""
    user = current_user()
    if not user or not user.has_role("authority", "admin"):
        emit("error", {"message": "Authorization required"})
        return

    incident_id = data.get("incident_id")
    if incident_id:
        # Verify user has access to this incident's district
        from ..services import disaster_incident_service
        from ..extensions import db

        incident = db.session.get(
            disaster_incident_service.DisasterIncident,
            incident_id,
        )
        if incident and incident.district_id:
            authorized = False
            if user.has_role("admin"):
                authorized = True
            elif user.temporary_district_id and user.temporary_district_id == incident.district_id:
                authorized = True
            elif user.permanent_district_id and user.permanent_district_id == incident.district_id:
                authorized = True
            else:
                from ..models.authority import Authority
                from sqlalchemy import select
                user_authorities = db.session.scalars(
                    select(Authority).where(Authority.is_active == True)  # noqa: E712
                ).all()
                for auth in user_authorities:
                    if auth.district_id is None or auth.district_id == incident.district_id:
                        authorized = True
                        break
            if not authorized:
                emit("error", {"message": "Not authorized for this incident"})
                return

        join_room(f"incident:{incident_id}")
        emit("subscribed_incident", {"incident_id": incident_id})
    else:
        # Subscribe to all incidents for this user's district
        if user.temporary_district_id:
            join_room(f"incidents:district:{user.temporary_district_id}")
        elif user.permanent_district_id:
            join_room(f"incidents:district:{user.permanent_district_id}")
        emit("subscribed_incidents", {"district": True})


# Helper functions for other services to emit events


def emit_disaster_incident_created(incident):
    """Emit event when a new disaster incident is created."""
    socketio.emit(
        "disaster_incident_created",
        incident.to_public_dict(),
        room="public_incidents",
    )
    # Also emit to district room
    if incident.district_id:
        socketio.emit(
            "disaster_incident_created",
            incident.to_public_dict(),
            room=f"district:{incident.district_id}",
        )


def emit_disaster_incident_updated(incident):
    """Emit event when a disaster incident is updated."""
    socketio.emit(
        "disaster_incident_updated",
        incident.to_public_dict(),
        room="public_incidents",
    )
    if incident.district_id:
        socketio.emit(
            "disaster_incident_updated",
            incident.to_public_dict(),
            room=f"district:{incident.district_id}",
        )
    socketio.emit(
        "disaster_incident_updated",
        incident.to_authority_dict(),
        room=f"incident:{incident.id}",
    )


def emit_disaster_incident_status_changed(incident, old_status: str, new_status: str):
    """Emit event when incident status changes."""
    payload = {
        "incident_id": str(incident.id),
        "old_status": old_status,
        "new_status": new_status,
        "incident": incident.to_public_dict(),
    }
    socketio.emit("disaster_incident_status_changed", payload, room="public_incidents")
    if incident.district_id:
        socketio.emit("disaster_incident_status_changed", payload, room=f"district:{incident.district_id}")
    socketio.emit("disaster_incident_status_changed", payload, room=f"incident:{incident.id}")


def emit_critical_incident_alert(incident):
    """Emit high-priority alert for critical incidents."""
    payload = incident.to_authority_dict()
    socketio.emit("critical_incident_alert", payload, room="public_incidents")
    if incident.district_id:
        socketio.emit("critical_incident_alert", payload, room=f"district:{incident.district_id}")
    # Also send to all authority users
    socketio.emit("critical_incident_alert", payload, room="authority_all")