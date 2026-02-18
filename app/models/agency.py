import enum
from datetime import date
from decimal import Decimal

from sqlmodel import Field, Relationship, SQLModel

from app.models.base import TimestampMixin


class BillingFrequency(str, enum.Enum):
    """frecuenciaFacturacion"""

    MONTHLY = "monthly"  # mensual
    QUARTERLY = "quarterly"  # trimestral
    YEARLY = "yearly"  # anual


class PaymentMethod(str, enum.Enum):
    """metodoPago"""

    TRANSFER = "transfer"  # transferencia
    CREDIT_CARD = "credit_card"  # tarjeta crédito
    DEBIT = "debit"  # débito
    CHECK = "check"  # cheque


class TaxCondition(str, enum.Enum):
    """condicionFiscal"""

    RESPONSABLE_INSCRIPTO = "responsable_inscripto"
    MONOTRIBUTO = "monotributo"
    EXENTO = "exento"
    CONSUMIDOR_FINAL = "consumidor_final"


class Agency(SQLModel, TimestampMixin, table=True):
    """
    Agencia de venta.

    Mapeo de campos (ES → EN):
        nombre                  → name
        razonSocial             → legal_name
        CUIT                    → tax_id
        direccion               → address
        pais                    → country
        nombreRepresentanteLegal → legal_representative_name
        correoAgencia           → agency_email
        correoAdministracion    → administration_email
        telefonoOficina         → office_phone
        nombreContacto          → contact_name
        correoContacto          → contact_email
        telefonoContacto        → contact_phone
        fechaAlta               → activation_date
        comision                → commission
        frecuenciaFacturacion   → billing_frequency
        metodoPago              → payment_method
        condicionFiscal         → tax_condition
        cbu                     → bank_account
        matriculaSSN            → ssn_register
    """

    __tablename__ = "agencies"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    legal_name: str = Field()
    tax_id: str = Field(index=True)
    address: str = Field()
    country: str = Field(max_length=2)  # ISO 3166-1 alpha-2
    legal_representative_name: str = Field()
    agency_email: str = Field(index=True)
    administration_email: str | None = Field(default=None)
    office_phone: str = Field()
    contact_name: str | None = Field(default=None)
    contact_email: str | None = Field(default=None)
    contact_phone: str | None = Field(default=None)
    activation_date: date = Field()
    commission: Decimal | None = Field(default=None)
    billing_frequency: BillingFrequency = Field()
    payment_method: PaymentMethod = Field()
    tax_condition: TaxCondition = Field()
    bank_account: str | None = Field(default=None)
    ssn_register: str | None = Field(default=None)
    active: bool = Field(default=True)

    users: list["User"] = Relationship(back_populates="agency")
    sellers: list["Seller"] = Relationship(back_populates="agency")
