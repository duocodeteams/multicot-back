"""Venta de una emisión: pasajeros, pago, voucher e intentos.

Los estados se guardan como texto para poder sumar valores sin alterar un ENUM
de Postgres. Los GUID de la compañía viven en JSON y no salen en la API.
"""

import enum
from datetime import date
from decimal import Decimal

from sqlalchemy import Column, Index, JSON, Numeric, String, text
from sqlmodel import Field, Relationship, SQLModel

from app.models.base import TimestampMixin


class EmissionStatus(str, enum.Enum):
    PRODUCT_SELECTED = "product_selected"
    PASSENGERS_COMPLETE = "passengers_complete"
    EMITTING = "emitting"
    ISSUED = "issued"
    EMISSION_FAILED = "emission_failed"
    CANCELLED = "cancelled"


class SalePaymentStatus(str, enum.Enum):
    NOT_STARTED = "not_started"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PaymentProvider(str, enum.Enum):
    OMINT_GATEWAY = "omint_gateway"
    MERCADOPAGO = "mercadopago"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class VoucherStatus(str, enum.Enum):
    ISSUED = "issued"
    CANCELLED = "cancelled"


class Sale(SQLModel, TimestampMixin, table=True):
    """Operación de emisión. El precio queda congelado al elegir el plan."""

    __tablename__ = "sales"

    id: int | None = Field(default=None, primary_key=True)
    created_by_user_id: int = Field(foreign_key="users.id", index=True)
    agency_id: int | None = Field(default=None, foreign_key="agencies.id", index=True)
    seller_id: int | None = Field(default=None, foreign_key="sellers.id", index=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    emission_status: str = Field(
        sa_column=Column(String(32), nullable=False, index=True),
    )
    payment_status: str = Field(
        sa_column=Column(String(32), nullable=False),
    )
    # Referencia de la cotización en la compañía (omint:{quotationId}). Una venta por cotización.
    provider_reference: str | None = Field(default=None, unique=True)
    origin: str | None = Field(default=None, max_length=16)
    destination_id: int | None = Field(default=None)
    trip_type: str | None = Field(default=None, max_length=32)
    days_range: int | None = Field(default=None)
    departure_date: date | None = Field(default=None)
    return_date: date | None = Field(default=None)
    quoted_ages: list = Field(sa_column=Column(JSON, nullable=False))
    external_plan_id: str = Field(max_length=80)
    plan_name: str = Field(max_length=200)
    plan_id: int | None = Field(default=None, foreign_key="plans.id", index=True)
    currency: str = Field(max_length=3)
    amount: Decimal = Field(sa_column=Column(Numeric(14, 2), nullable=False))
    net_amount: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(14, 2), nullable=True)
    )
    base_amount: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(14, 2), nullable=True)
    )
    markup: Decimal = Field(sa_column=Column(Numeric(8, 2), nullable=False))
    emergency_first_name: str | None = Field(default=None, max_length=120)
    emergency_last_name: str | None = Field(default=None, max_length=120)
    emergency_phone: str | None = Field(default=None, max_length=40)
    emergency_email: str | None = Field(default=None, max_length=255)
    provider_context: dict = Field(sa_column=Column(JSON, nullable=False))

    passengers: list["Passenger"] = Relationship(back_populates="sale")
    payments: list["Payment"] = Relationship(back_populates="sale")
    vouchers: list["Voucher"] = Relationship(back_populates="sale")
    attempts: list["EmissionAttempt"] = Relationship(back_populates="sale")


class Passenger(SQLModel, TimestampMixin, table=True):
    """Pasajero de la venta. El titular es uno de ellos (`is_holder`)."""

    __tablename__ = "passengers"
    __table_args__ = (
        Index(
            "uq_passengers_one_holder_per_sale",
            "sale_id",
            unique=True,
            sqlite_where=text("is_holder = 1"),
            postgresql_where=text("is_holder"),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    sale_id: int = Field(foreign_key="sales.id", index=True)
    is_holder: bool = Field(default=False)
    first_name: str = Field(max_length=120)
    last_name: str = Field(max_length=120)
    birth_date: date
    document_type: str = Field(max_length=20)
    document_number: str = Field(max_length=40)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=40)
    gender: str | None = Field(default=None, max_length=1)
    country: str | None = Field(default=None, max_length=120)
    province: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=120)
    street: str | None = Field(default=None, max_length=200)
    street_number: str | None = Field(default=None, max_length=20)
    emergency_first_name: str | None = Field(default=None, max_length=120)
    emergency_last_name: str | None = Field(default=None, max_length=120)
    emergency_phone: str | None = Field(default=None, max_length=40)
    provider_data: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))

    sale: Sale = Relationship(back_populates="passengers")


class Payment(SQLModel, TimestampMixin, table=True):
    """Intento de cobro. En este corte el proveedor previsto es el gateway de Omint."""

    __tablename__ = "payments"

    id: int | None = Field(default=None, primary_key=True)
    sale_id: int = Field(foreign_key="sales.id", index=True)
    provider: str = Field(sa_column=Column(String(32), nullable=False))
    status: str = Field(sa_column=Column(String(32), nullable=False))
    amount: Decimal = Field(sa_column=Column(Numeric(14, 2), nullable=False))
    currency: str = Field(max_length=3)
    checkout_url: str | None = Field(default=None, max_length=2000)
    external_id: str | None = Field(default=None, max_length=80)
    financing_id: str | None = Field(default=None, max_length=80)
    provider_data: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))

    sale: Sale = Relationship(back_populates="payments")


class Voucher(SQLModel, TimestampMixin, table=True):
    """Voucher emitido. `passenger_id` vacío cuando la compañía emite uno para toda la venta."""

    __tablename__ = "vouchers"

    id: int | None = Field(default=None, primary_key=True)
    sale_id: int = Field(foreign_key="sales.id", index=True)
    passenger_id: int | None = Field(default=None, foreign_key="passengers.id", index=True)
    external_id: str = Field(max_length=80)
    number: str | None = Field(default=None, max_length=80)
    url: str | None = Field(default=None, max_length=2000)
    status: str = Field(sa_column=Column(String(32), nullable=False))
    provider_data: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))

    sale: Sale = Relationship(back_populates="vouchers")


class EmissionAttempt(SQLModel, TimestampMixin, table=True):
    """Llamada a la compañía. Sirve para reintentar sin abrir otro pago."""

    __tablename__ = "emission_attempts"

    id: int | None = Field(default=None, primary_key=True)
    sale_id: int = Field(foreign_key="sales.id", index=True)
    step: str = Field(max_length=40)
    http_status: int | None = Field(default=None)
    success: bool = Field(default=False)
    error_message: str | None = Field(default=None, max_length=500)
    request_payload: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    response_payload: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))

    sale: Sale = Relationship(back_populates="attempts")
