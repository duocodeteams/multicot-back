"""add company credentials per agency

Revision ID: e4b8d21c9a70
Revises: b7e2c91a4d55
Create Date: 2026-11-06 16:30:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "e4b8d21c9a70"
down_revision = "b7e2c91a4d55"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_credentials",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("payload_encrypted", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["agency_id"], ["agencies.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agency_id",
            "company_id",
            name="uq_company_credentials_agency_company",
        ),
    )
    op.create_index(
        op.f("ix_company_credentials_agency_id"),
        "company_credentials",
        ["agency_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_company_credentials_company_id"),
        "company_credentials",
        ["company_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_company_credentials_company_id"), table_name="company_credentials")
    op.drop_index(op.f("ix_company_credentials_agency_id"), table_name="company_credentials")
    op.drop_table("company_credentials")
