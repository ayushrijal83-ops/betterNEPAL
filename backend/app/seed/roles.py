"""Idempotent seeding of the five application roles.

Safe to run repeatedly: existing roles are matched by name and only their
description is refreshed. No user accounts are created here - in particular no
administrator, because that would mean shipping a known password.
"""
from __future__ import annotations

from sqlalchemy import select

from ..extensions import db
from ..models.role import ROLE_DESCRIPTIONS, ROLE_NAMES, Role


def seed_roles() -> dict[str, int]:
    """Ensure every role in ``ROLE_NAMES`` exists. Returns a small summary."""
    existing = {
        role.name: role
        for role in db.session.scalars(
            select(Role).where(Role.name.in_(ROLE_NAMES))
        ).all()
    }

    created = 0
    for name in ROLE_NAMES:
        description = ROLE_DESCRIPTIONS[name]
        role = existing.get(name)
        if role is None:
            db.session.add(Role(name=name, description=description))
            created += 1
        elif role.description != description:
            role.description = description

    db.session.commit()
    return {"created": created, "existing": len(existing), "total": len(ROLE_NAMES)}
