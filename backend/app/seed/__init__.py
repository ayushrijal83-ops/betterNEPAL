"""Seed data and the CLI commands that apply it."""
from __future__ import annotations

import click
from flask import Flask

from .roles import seed_roles

__all__ = ["register_seed_commands", "seed_roles"]


def register_seed_commands(app: Flask) -> None:
    """Expose ``flask seed roles``."""

    @app.cli.group("seed")
    def seed_group() -> None:
        """Populate reference data."""

    @seed_group.command("roles")
    def seed_roles_command() -> None:
        """Create the five application roles if they do not exist."""
        summary = seed_roles()
        click.echo(
            f"Roles: {summary['created']} created, "
            f"{summary['existing']} already present, {summary['total']} total."
        )
