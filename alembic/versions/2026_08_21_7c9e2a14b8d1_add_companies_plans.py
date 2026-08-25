"""add companies plans and destinations

Revision ID: 7c9e2a14b8d1
Revises: 4b13f6bbf71c
Create Date: 2026-08-21 15:21:00.000000

"""

from datetime import datetime

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision = "7c9e2a14b8d1"
down_revision = "4b13f6bbf71c"
branch_labels = None
depends_on = None

COMPANY_SEED = (
    ("pax", "Pax", False),
    ("cardinal", "Cardinal", True),
    ("go_assistance", "GoAssistance", True),
    ("terrawind", "Terrawind", True),
    ("new_travel", "New Travel", True),
    ("inter_assist", "Inter Assist", True),
    ("universal", "Universal", True),
)


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sqlmodel.sql.sqltypes.AutoString(length=50), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_companies_slug"), "companies", ["slug"], unique=True)

    op.create_table(
        "plans",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("external_plan_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("markup", sa.Numeric(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "external_plan_id",
            name="uq_plans_company_external_id",
        ),
    )
    op.create_index(op.f("ix_plans_company_id"), "plans", ["company_id"], unique=False)
    op.create_index(op.f("ix_plans_external_plan_id"), "plans", ["external_plan_id"], unique=False)

    op.create_table(
        "plan_destinations",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_id",
            "destination_id",
            name="uq_plan_destinations_plan_dest",
        ),
    )
    op.create_index(
        op.f("ix_plan_destinations_plan_id"),
        "plan_destinations",
        ["plan_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_plan_destinations_destination_id"),
        "plan_destinations",
        ["destination_id"],
        unique=False,
    )

    companies = sa.table(
        "companies",
        sa.column("slug", sa.String),
        sa.column("name", sa.String),
        sa.column("active", sa.Boolean),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    now = datetime.utcnow()
    op.bulk_insert(
        companies,
        [
            {
                "slug": slug,
                "name": name,
                "active": active,
                "created_at": now,
                "updated_at": None,
            }
            for slug, name, active in COMPANY_SEED
        ],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_plan_destinations_destination_id"), table_name="plan_destinations")
    op.drop_index(op.f("ix_plan_destinations_plan_id"), table_name="plan_destinations")
    op.drop_table("plan_destinations")
    op.drop_index(op.f("ix_plans_external_plan_id"), table_name="plans")
    op.drop_index(op.f("ix_plans_company_id"), table_name="plans")
    op.drop_table("plans")
    op.drop_index(op.f("ix_companies_slug"), table_name="companies")
    op.drop_table("companies")
