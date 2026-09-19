"""LOCAL DEVELOPMENT ONLY test accounts, one per meaningful role.

Not wired into any production path. Invoked only via the `flask seed
dev-accounts` CLI command registered below, which is guarded so it refuses to
run against a production config.

Idempotent: every account is looked up by its fixed, clearly-marked email
before creation. Running this twice updates (password reset + role/district
refresh) rather than duplicating.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import click
from flask import Flask
from sqlalchemy import select

from ..extensions import db
from ..models.authority import Authority
from ..models.district import District
from ..models.enums import (
    AuthorityType,
    GovernmentLevel,
    IncidentSeverity,
    IncidentStatus,
    ProjectStatus,
    ReportCategory,
    ReportStatus,
)
from ..models.incident import Incident
from ..models.project import Project
from ..models.report import Report
from ..models.role import ROLE_NAMES, Role
from ..models.user import User
from ..services.auth_service import hash_password

# Fixed, safe, obviously-fake dev password. Never used for any real account.
DEV_PASSWORD = "DevTest#2024"

# Every account uses this domain so grepping the DB for "@test.local" finds
# every one of them, and so nobody mistakes one for a real citizen.
DOMAIN = "test.local"


def _get_or_create_district(name: str, province: str) -> District:
    d = db.session.scalar(select(District).where(District.name == name))
    if d is None:
        d = District(name=name, province=province)
        db.session.add(d)
        db.session.flush()
    return d


def _get_or_create_authority(
    name: str, level: GovernmentLevel, atype: AuthorityType, district: District | None
) -> Authority:
    a = db.session.scalar(select(Authority).where(Authority.name == name))
    if a is None:
        a = Authority(
            name=name,
            level=level,
            type=atype,
            district_id=district.id if district else None,
            contact_email=f"office@{DOMAIN}",
            contact_phone="+977-1-4000000",
        )
        db.session.add(a)
        db.session.flush()
    else:
        a.level = level
        a.type = atype
        a.district_id = district.id if district else None
    return a


def _get_or_create_user(
    email: str,
    full_name: str,
    role_names: tuple[str, ...],
    roles_by_name: dict[str, Role],
    *,
    permanent_district: District | None = None,
    temporary_district: District | None = None,
    phone: str = "+977-9800000000",
) -> User:
    user = db.session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            email=email,
            password_hash=hash_password(DEV_PASSWORD),
            full_name=full_name,
            phone=phone,
            is_active=True,
            permanent_district_id=permanent_district.id if permanent_district else None,
            temporary_district_id=temporary_district.id if temporary_district else None,
        )
        db.session.add(user)
    else:
        # Reuse: reset password + refresh role/district so the account stays
        # usable even if it drifted from an earlier partial run.
        user.password_hash = hash_password(DEV_PASSWORD)
        user.full_name = full_name
        user.is_active = True
        user.permanent_district_id = permanent_district.id if permanent_district else None
        user.temporary_district_id = temporary_district.id if temporary_district else None

    user.roles = [roles_by_name[name] for name in role_names]
    db.session.flush()
    return user


def _get_or_create_report(
    title: str,
    reporter: User,
    district: District,
    category: ReportCategory,
    status: ReportStatus,
    lat: float,
    lng: float,
) -> Report:
    r = db.session.scalar(select(Report).where(Report.title == title))
    if r is None:
        r = Report(
            reporter_id=reporter.id,
            title=title,
            description=f"[DEV TEST DATA] Seeded report for local UI testing: {title}.",
            category=category,
            status=status,
            district_id=district.id,
            latitude=lat,
            longitude=lng,
        )
        db.session.add(r)
        db.session.flush()
    return r


def _get_or_create_incident(
    title: str,
    district: District,
    authority: Authority,
    severity: IncidentSeverity,
    status: IncidentStatus,
    verified_by: User,
    lat: float,
    lng: float,
) -> Incident:
    inc = db.session.scalar(select(Incident).where(Incident.title == title))
    if inc is None:
        inc = Incident(
            title=title,
            description=f"[DEV TEST DATA] Seeded incident for local UI testing: {title}.",
            category=ReportCategory.ROAD_DAMAGE,
            severity=severity,
            status=status,
            district_id=district.id,
            latitude=lat,
            longitude=lng,
            authority_id=authority.id,
            verified_by_id=verified_by.id,
        )
        db.session.add(inc)
        db.session.flush()
    else:
        inc.authority_id = authority.id
        inc.severity = severity
        inc.status = status
    return inc


def _get_or_create_project(
    title: str, authority: Authority, incident: Incident, contractor: User, status: ProjectStatus
) -> Project:
    p = db.session.scalar(select(Project).where(Project.title == title))
    if p is None:
        p = Project(
            title=title,
            description=f"[DEV TEST DATA] Seeded project for local UI testing: {title}.",
            status=status,
            incident_id=incident.id,
            authority_id=authority.id,
            contractor_id=contractor.id,
            start_date=date.today() - timedelta(days=14),
            estimated_end_date=date.today() + timedelta(days=30),
        )
        db.session.add(p)
        db.session.flush()
    else:
        p.contractor_id = contractor.id
        p.authority_id = authority.id
    return p


def seed_dev_test_accounts() -> dict[str, Any]:
    """Create/refresh one test account per meaningful role, plus the
    minimum jurisdiction and dashboard data each role's UI needs.

    Safe to call repeatedly. Returns a summary dict consumed by the CLI.
    """
    # Roles must exist first (flask seed roles / seed_roles()).
    roles_by_name = {
        r.name: r for r in db.session.scalars(select(Role).where(Role.name.in_(ROLE_NAMES))).all()
    }
    missing = set(ROLE_NAMES) - set(roles_by_name)
    if missing:
        raise RuntimeError(
            f"Roles not seeded: {sorted(missing)}. Run `flask seed roles` first."
        )

    # --- Jurisdiction: one real-named district per province tier we need to
    # distinguish (district-level authority vs. national authority). ---
    kathmandu = _get_or_create_district("Kathmandu", "Bagmati")
    kaski = _get_or_create_district("Kaski", "Gandaki")

    # --- Authorities: one district-scoped, one national, for permission
    # contrast testing (authority-vs-national/admin). ---
    dor_national = _get_or_create_authority(
        "[DEV TEST] Department of Roads - National",
        GovernmentLevel.FEDERAL,
        AuthorityType.DEPARTMENT_OF_ROADS,
        None,
    )
    kathmandu_muni = _get_or_create_authority(
        "[DEV TEST] Kathmandu Municipal Office",
        GovernmentLevel.LOCAL,
        AuthorityType.MUNICIPAL_OFFICE,
        kathmandu,
    )

    # --- Users: one per ROLE_NAMES entry, clearly named/emailed as test data.
    citizen = _get_or_create_user(
        f"test.citizen@{DOMAIN}", "[TEST] Citizen User", ("citizen",), roles_by_name,
        permanent_district=kathmandu, temporary_district=kathmandu,
    )
    guide = _get_or_create_user(
        f"test.guide@{DOMAIN}", "[TEST] Trekking Guide User", ("trekking_guide",), roles_by_name,
        permanent_district=kaski, temporary_district=kaski,
    )
    authority_district = _get_or_create_user(
        f"test.authority.district@{DOMAIN}", "[TEST] Authority User (Kathmandu)",
        ("authority",), roles_by_name,
        permanent_district=kathmandu, temporary_district=kathmandu,
    )
    authority_national = _get_or_create_user(
        f"test.authority.national@{DOMAIN}", "[TEST] Authority User (National)",
        ("authority",), roles_by_name,
        permanent_district=None, temporary_district=None,
    )
    contractor = _get_or_create_user(
        f"test.contractor@{DOMAIN}", "[TEST] Contractor User", ("contractor",), roles_by_name,
        permanent_district=kathmandu, temporary_district=kathmandu,
    )
    admin = _get_or_create_user(
        f"test.admin@{DOMAIN}", "[TEST] Admin User", ("admin",), roles_by_name,
        permanent_district=None, temporary_district=None,
    )
    # A second role combination, since the schema supports multi-role users
    # and the UI's role-switcher path is otherwise untested.
    multi = _get_or_create_user(
        f"test.multirole@{DOMAIN}", "[TEST] Citizen+Authority User",
        ("citizen", "authority"), roles_by_name,
        permanent_district=kathmandu, temporary_district=kathmandu,
    )

    db.session.flush()

    # --- Dashboard data: enough for each role to see something real. ---
    report = _get_or_create_report(
        "[DEV TEST DATA] Pothole on Ring Road",
        citizen, kathmandu, ReportCategory.ROAD_DAMAGE, ReportStatus.SUBMITTED,
        27.7172, 85.3240,
    )
    incident = _get_or_create_incident(
        "[DEV TEST DATA] Ring Road Pothole Cluster",
        kathmandu, kathmandu_muni, IncidentSeverity.MEDIUM, IncidentStatus.OPEN,
        authority_district, 27.7172, 85.3240,
    )
    _get_or_create_project(
        "[DEV TEST DATA] Ring Road Resurfacing",
        kathmandu_muni, incident, contractor, ProjectStatus.ACTIVE,
    )

    db.session.commit()

    return {
        "accounts": [
            ("citizen", citizen),
            ("trekking_guide", guide),
            ("authority (district)", authority_district),
            ("authority (national)", authority_national),
            ("contractor", contractor),
            ("admin", admin),
            ("citizen+authority", multi),
        ],
        "password": DEV_PASSWORD,
        "districts": [kathmandu.name, kaski.name],
        "authorities": [dor_national.name, kathmandu_muni.name],
    }


def make_dev_accounts_command(app: Flask) -> click.Command:
    """Build the `dev-accounts` click command, to be attached to the existing
    `seed` group by the caller. Refuses to run against a production config."""

    @click.command("dev-accounts")
    def dev_accounts_command() -> None:
        """Create/refresh LOCAL DEV ONLY test accounts for every role."""
        # Not app.config["DEBUG"]: Flask's CLI sets app.debug from the
        # FLASK_DEBUG env var after app creation, independent of the config
        # class, so it can read False even under DevelopmentConfig. Checking
        # the resolved config class itself is what the app factory actually
        # used to decide which settings apply, and isn't second-guessed later.
        from ..config.settings import get_config

        if get_config().__name__ != "DevelopmentConfig":
            raise click.ClickException(
                "Refusing to seed dev test accounts: resolved config is not "
                "DevelopmentConfig (set FLASK_ENV=development)."
            )
        summary = seed_dev_test_accounts()
        click.echo(f"Password for all accounts: {summary['password']}")
        for role_label, user in summary["accounts"]:
            click.echo(f"  [{role_label:<22}] {user.email}")
        click.echo(f"Districts: {', '.join(summary['districts'])}")
        click.echo(f"Authorities: {', '.join(summary['authorities'])}")

    return dev_accounts_command
