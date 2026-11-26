"""add universal organization id on sellers

Revision ID: a1c4e8b27d90
Revises: e4b8d21c9a70
Create Date: 2026-11-16 15:55:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "a1c4e8b27d90"
down_revision = "e4b8d21c9a70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sellers",
        sa.Column("universal_organization_id", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sellers", "universal_organization_id")
