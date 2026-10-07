"""add promotions and promotion plans

Revision ID: c4e8a91b2d70
Revises: 9f3c1d8a4e21
Create Date: 2026-10-06 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = "c4e8a91b2d70"
down_revision = "9f3c1d8a4e21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "promotions",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(length=500), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_promotions_company_id"), "promotions", ["company_id"], unique=False)

    op.create_table(
        "promotion_plans",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("promotion_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.ForeignKeyConstraint(["promotion_id"], ["promotions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "promotion_id",
            "plan_id",
            name="uq_promotion_plans_promotion_plan",
        ),
    )
    op.create_index(
        op.f("ix_promotion_plans_promotion_id"),
        "promotion_plans",
        ["promotion_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_promotion_plans_plan_id"),
        "promotion_plans",
        ["plan_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_promotion_plans_plan_id"), table_name="promotion_plans")
    op.drop_index(op.f("ix_promotion_plans_promotion_id"), table_name="promotion_plans")
    op.drop_table("promotion_plans")
    op.drop_index(op.f("ix_promotions_company_id"), table_name="promotions")
    op.drop_table("promotions")
