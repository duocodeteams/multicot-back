"""add sales, passengers, payments, vouchers and emission attempts

Revision ID: b7e2c91a4d55
Revises: c4e8a91b2d70
Create Date: 2026-10-30 15:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "b7e2c91a4d55"
down_revision = "c4e8a91b2d70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sales",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=True),
        sa.Column("seller_id", sa.Integer(), nullable=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("emission_status", sa.String(length=32), nullable=False),
        sa.Column("payment_status", sa.String(length=32), nullable=False),
        sa.Column("provider_reference", sa.String(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=True),
        sa.Column("destination_id", sa.Integer(), nullable=True),
        sa.Column("trip_type", sa.String(length=32), nullable=True),
        sa.Column("days_range", sa.Integer(), nullable=True),
        sa.Column("departure_date", sa.Date(), nullable=True),
        sa.Column("return_date", sa.Date(), nullable=True),
        sa.Column("quoted_ages", sa.JSON(), nullable=False),
        sa.Column("external_plan_id", sa.String(length=80), nullable=False),
        sa.Column("plan_name", sa.String(length=200), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("net_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("base_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("markup", sa.Numeric(8, 2), nullable=False),
        sa.Column("emergency_first_name", sa.String(length=120), nullable=True),
        sa.Column("emergency_last_name", sa.String(length=120), nullable=True),
        sa.Column("emergency_phone", sa.String(length=40), nullable=True),
        sa.Column("emergency_email", sa.String(length=255), nullable=True),
        sa.Column("provider_context", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["agency_id"], ["agencies.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.ForeignKeyConstraint(["seller_id"], ["sellers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_reference"),
    )
    op.create_index(op.f("ix_sales_agency_id"), "sales", ["agency_id"], unique=False)
    op.create_index(op.f("ix_sales_company_id"), "sales", ["company_id"], unique=False)
    op.create_index(
        op.f("ix_sales_created_by_user_id"),
        "sales",
        ["created_by_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sales_emission_status"),
        "sales",
        ["emission_status"],
        unique=False,
    )
    op.create_index(op.f("ix_sales_plan_id"), "sales", ["plan_id"], unique=False)
    op.create_index(op.f("ix_sales_seller_id"), "sales", ["seller_id"], unique=False)

    op.create_table(
        "passengers",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sale_id", sa.Integer(), nullable=False),
        sa.Column("is_holder", sa.Boolean(), nullable=False),
        sa.Column("first_name", sa.String(length=120), nullable=False),
        sa.Column("last_name", sa.String(length=120), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=False),
        sa.Column("document_type", sa.String(length=20), nullable=False),
        sa.Column("document_number", sa.String(length=40), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=40), nullable=True),
        sa.Column("gender", sa.String(length=1), nullable=True),
        sa.Column("country", sa.String(length=120), nullable=True),
        sa.Column("province", sa.String(length=120), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("street", sa.String(length=200), nullable=True),
        sa.Column("street_number", sa.String(length=20), nullable=True),
        sa.Column("emergency_first_name", sa.String(length=120), nullable=True),
        sa.Column("emergency_last_name", sa.String(length=120), nullable=True),
        sa.Column("emergency_phone", sa.String(length=40), nullable=True),
        sa.Column("provider_data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["sale_id"], ["sales.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_passengers_sale_id"), "passengers", ["sale_id"], unique=False)
    op.create_index(
        "uq_passengers_one_holder_per_sale",
        "passengers",
        ["sale_id"],
        unique=True,
        sqlite_where=sa.text("is_holder = 1"),
        postgresql_where=sa.text("is_holder"),
    )

    op.create_table(
        "payments",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sale_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("checkout_url", sa.String(length=2000), nullable=True),
        sa.Column("external_id", sa.String(length=80), nullable=True),
        sa.Column("financing_id", sa.String(length=80), nullable=True),
        sa.Column("provider_data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["sale_id"], ["sales.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_payments_sale_id"), "payments", ["sale_id"], unique=False)

    op.create_table(
        "vouchers",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sale_id", sa.Integer(), nullable=False),
        sa.Column("passenger_id", sa.Integer(), nullable=True),
        sa.Column("external_id", sa.String(length=80), nullable=False),
        sa.Column("number", sa.String(length=80), nullable=True),
        sa.Column("url", sa.String(length=2000), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["passenger_id"], ["passengers.id"]),
        sa.ForeignKeyConstraint(["sale_id"], ["sales.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_vouchers_passenger_id"), "vouchers", ["passenger_id"], unique=False)
    op.create_index(op.f("ix_vouchers_sale_id"), "vouchers", ["sale_id"], unique=False)

    op.create_table(
        "emission_attempts",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sale_id", sa.Integer(), nullable=False),
        sa.Column("step", sa.String(length=40), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["sale_id"], ["sales.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_emission_attempts_sale_id"),
        "emission_attempts",
        ["sale_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_emission_attempts_sale_id"), table_name="emission_attempts")
    op.drop_table("emission_attempts")
    op.drop_index(op.f("ix_vouchers_sale_id"), table_name="vouchers")
    op.drop_index(op.f("ix_vouchers_passenger_id"), table_name="vouchers")
    op.drop_table("vouchers")
    op.drop_index(op.f("ix_payments_sale_id"), table_name="payments")
    op.drop_table("payments")
    op.drop_index("uq_passengers_one_holder_per_sale", table_name="passengers")
    op.drop_index(op.f("ix_passengers_sale_id"), table_name="passengers")
    op.drop_table("passengers")
    op.drop_index(op.f("ix_sales_seller_id"), table_name="sales")
    op.drop_index(op.f("ix_sales_plan_id"), table_name="sales")
    op.drop_index(op.f("ix_sales_emission_status"), table_name="sales")
    op.drop_index(op.f("ix_sales_created_by_user_id"), table_name="sales")
    op.drop_index(op.f("ix_sales_company_id"), table_name="sales")
    op.drop_index(op.f("ix_sales_agency_id"), table_name="sales")
    op.drop_table("sales")
