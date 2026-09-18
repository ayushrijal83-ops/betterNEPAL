"""Seed data and the CLI commands that apply it."""
from __future__ import annotations

import click
from flask import Flask

from ..gis.datasets import DatasetError
from .districts import (
    import_district_boundaries,
    import_district_reference,
    import_municipality_boundaries,
    import_municipality_reference,
)
from .roles import seed_roles

__all__ = [
    "import_district_boundaries",
    "import_district_reference",
    "import_municipality_boundaries",
    "import_municipality_reference",
    "register_seed_commands",
    "seed_roles",
]


def _report(summary: dict) -> None:
    click.echo(
        f"  created: {summary['created']}, updated: {summary['updated']}, "
        f"total in file: {summary['total']}"
    )


def _run(action, *args) -> None:
    """Run an importer, turning dataset problems into clean CLI errors."""
    try:
        _report(action(*args))
    except DatasetError as exc:
        raise click.ClickException(str(exc)) from None


def register_seed_commands(app: Flask) -> None:
    """Expose ``flask seed ...`` and ``flask gis ...``."""

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

    @app.cli.group("gis")
    def gis_group() -> None:
        """Import geographic datasets."""

    @gis_group.command("import-districts")
    @click.option(
        "--file",
        "path",
        type=click.Path(exists=True, dir_okay=False),
        default=None,
        help="Reference JSON file. Defaults to the bundled 77-district dataset.",
    )
    def import_districts_command(path: str | None) -> None:
        """Import district names and provinces (no geometry, no codes)."""
        click.echo("Importing district reference data...")
        _run(import_district_reference, path)

    @gis_group.command("import-municipalities")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False))
    def import_municipalities_command(path: str) -> None:
        """Import municipality names under existing districts."""
        click.echo("Importing municipality reference data...")
        _run(import_municipality_reference, path)

    @gis_group.command("import-district-boundaries")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False))
    def import_district_boundaries_command(path: str) -> None:
        """Import authoritative district boundaries (GeoJSON, PostGIS only)."""
        click.echo("Importing district boundaries...")
        _run(import_district_boundaries, path)

    @gis_group.command("import-municipality-boundaries")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False))
    def import_municipality_boundaries_command(path: str) -> None:
        """Import authoritative municipality boundaries (GeoJSON, PostGIS only)."""
        click.echo("Importing municipality boundaries...")
        _run(import_municipality_boundaries, path)

    @gis_group.command("coverage")
    def coverage_command() -> None:
        """Report what geographic data is currently loaded."""
        from ..services.geolocation_service import coverage_summary

        for key, value in coverage_summary().items():
            click.echo(f"  {key}: {value}")
