"""drop universal organization id from sellers

Revision ID: b2d9f04c6e18
Revises: a1c4e8b27d90
Create Date: 2026-11-25 10:45:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "b2d9f04c6e18"
down_revision = "a1c4e8b27d90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("sellers", "universal_organization_id")


def downgrade() -> None:
    op.add_column(
        "sellers",
        sa.Column("universal_organization_id", sa.String(length=40), nullable=True),
    )
