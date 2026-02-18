from datetime import date
from decimal import Decimal
from pydantic import BaseModel, EmailStr, Field

from app.models.agency import BillingFrequency, PaymentMethod, TaxCondition
from app.models.user import UserRole


class AgencyUserCreate(BaseModel):
    """Datos del usuario principal de la agencia (login)."""

    email: EmailStr
    password: str = Field(..., min_length=8)


class AgencyCreate(BaseModel):
    """Request para crear agencia con su usuario principal."""

    name: str
    legal_name: str
    tax_id: str
    address: str
    country: str = Field(..., min_length=2, max_length=2)  # ISO 3166-1 alpha-2
    legal_representative_name: str
    agency_email: EmailStr
    administration_email: EmailStr | None = None
    office_phone: str
    contact_name: str | None = None
    contact_email: EmailStr | None = None
    contact_phone: str | None = None
    activation_date: date
    commission: Decimal | None = None
    billing_frequency: BillingFrequency
    payment_method: PaymentMethod
    tax_condition: TaxCondition
    bank_account: str | None = None
    ssn_register: str | None = None
    user: AgencyUserCreate


class AgencyUserResponse(BaseModel):
    id: int
    email: str
    role: UserRole = UserRole.AGENCY

    model_config = {"from_attributes": True}


class AgencyUpdate(BaseModel):
    """Campos editables de la agencia. Todos opcionales para PATCH."""

    name: str | None = None
    legal_name: str | None = None
    tax_id: str | None = None
    address: str | None = None
    country: str | None = Field(None, min_length=2, max_length=2)
    legal_representative_name: str | None = None
    agency_email: EmailStr | None = None
    administration_email: EmailStr | None = None
    office_phone: str | None = None
    contact_name: str | None = None
    contact_email: EmailStr | None = None
    contact_phone: str | None = None
    activation_date: date | None = None
    commission: Decimal | None = None
    billing_frequency: BillingFrequency | None = None
    payment_method: PaymentMethod | None = None
    tax_condition: TaxCondition | None = None
    bank_account: str | None = None
    ssn_register: str | None = None


class AgencyResponse(BaseModel):
    id: int
    name: str
    legal_name: str
    tax_id: str
    address: str
    country: str
    legal_representative_name: str
    agency_email: str
    administration_email: str | None
    office_phone: str
    contact_name: str | None
    contact_email: str | None
    contact_phone: str | None
    activation_date: date
    commission: Decimal | None
    billing_frequency: BillingFrequency
    payment_method: PaymentMethod
    tax_condition: TaxCondition
    bank_account: str | None
    ssn_register: str | None
    user: AgencyUserResponse | None  # Puede ser None en casos edge (agencia sin user)

    model_config = {"from_attributes": True}


class AgencyListResponse(BaseModel):
    """Respuesta paginada de agencias."""

    items: list[AgencyResponse]
    total: int
    limit: int
    offset: int
