"""split plan markup into producer, organizer and operating expenses

Revision ID: 9f3c1d8a4e21
Revises: 7c9e2a14b8d1
Create Date: 2026-08-27 11:31:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "9f3c1d8a4e21"
down_revision = "7c9e2a14b8d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column(
            "producer_markup",
            sa.Numeric(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "plans",
        sa.Column(
            "organizer_markup",
            sa.Numeric(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "plans",
        sa.Column(
            "operating_expenses",
            sa.Numeric(),
            nullable=False,
            server_default="0",
        ),
    )
    op.execute("UPDATE plans SET producer_markup = markup")
    op.drop_column("plans", "markup")


def downgrade() -> None:
    op.add_column(
        "plans",
        sa.Column(
            "markup",
            sa.Numeric(),
            nullable=False,
            server_default="0",
        ),
    )
    op.execute(
        """
        UPDATE plans SET markup =
            producer_markup + organizer_markup + operating_expenses
        """
    )
    op.drop_column("plans", "operating_expenses")
    op.drop_column("plans", "organizer_markup")
    op.drop_column("plans", "producer_markup")
