"""disaster_incident_tables

Revision ID: a2b11b58de58
Revises: 95dc4329bd09
Create Date: 2026-09-19 12:29:44.389820

"""
from alembic import op
import sqlalchemy as sa
# Custom column types (e.g. UtcDateTime) are rendered by Alembic as
# fully-qualified names, so the module must be importable here.
import app.models.base  # noqa: F401
import app.gis.types  # noqa: F401


# revision identifiers, used by Alembic.
revision = 'a2b11b58de58'
down_revision = '95dc4329bd09'
branch_labels = None
depends_on = None


def upgrade():
    # disaster_incidents table
    op.create_table(
        "disaster_incidents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("disaster_type", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="moderate"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="detected"),
        sa.Column("district_id", sa.Uuid(), nullable=True),
        sa.Column("municipality_id", sa.Uuid(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("location", app.gis.types.GeometryColumn("POINT", 4326), nullable=True),
        sa.Column("ai_confidence", sa.Float(), nullable=True),
        sa.Column("ai_reason", sa.Text(), nullable=True),
        sa.Column("ai_evidence", sa.JSON(), nullable=True),
        sa.Column("ai_analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ai_provider", sa.String(length=64), nullable=True),
        sa.Column("source_report_id", sa.Uuid(), nullable=True),
        sa.Column("authority_id", sa.Uuid(), nullable=True),
        sa.Column("assigned_by_id", sa.Uuid(), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["assigned_by_id"], ["users.id"],
            name="fk_disaster_incidents_assigned_by_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["authority_id"], ["authorities.id"],
            name="fk_disaster_incidents_authority_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["district_id"], ["districts.id"],
            name="fk_disaster_incidents_district_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["municipality_id"], ["municipalities.id"],
            name="fk_disaster_incidents_municipality_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_report_id"], ["reports.id"],
            name="fk_disaster_incidents_source_report_id", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_disaster_incidents_created_at", "disaster_incidents", ["created_at"])
    op.create_index("ix_disaster_incidents_district_status", "disaster_incidents", ["district_id", "status"])
    op.create_index("ix_disaster_incidents_location", "disaster_incidents", ["location"], postgresql_using="gist")
    op.create_check_constraint(
        "ck_disaster_incidents_latitude_range",
        "disaster_incidents",
        "latitude >= -90 AND latitude <= 90",
    )
    op.create_check_constraint(
        "ck_disaster_incidents_longitude_range",
        "disaster_incidents",
        "longitude >= -180 AND longitude <= 180",
    )
    op.create_check_constraint(
        "ck_disaster_incidents_confidence_range",
        "disaster_incidents",
        "ai_confidence IS NULL OR (ai_confidence >= 0 AND ai_confidence <= 1)",
    )

    # disaster_dispatches table
    op.create_table(
        "disaster_dispatches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disaster_incident_id", sa.Uuid(), nullable=False),
        sa.Column("authority_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["authority_user_id"], ["users.id"],
            name="fk_disaster_dispatches_user_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["disaster_incident_id"], ["disaster_incidents.id"],
            name="fk_disaster_dispatches_incident_id", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_disaster_dispatches_incident_user", "disaster_dispatches", ["disaster_incident_id", "authority_user_id"])
    op.create_index("ix_disaster_dispatches_status", "disaster_dispatches", ["status"])

    # Add unique constraint to prevent duplicate dispatches
    op.create_unique_constraint(
        "uq_disaster_dispatches_incident_user_channel",
        "disaster_dispatches",
        ["disaster_incident_id", "authority_user_id", "channel"],
    )


def downgrade():
    op.drop_constraint("uq_disaster_dispatches_incident_user_channel", "disaster_dispatches", type_="unique")
    op.drop_table("disaster_dispatches")
    op.drop_table("disaster_incidents")