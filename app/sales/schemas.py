"""Contrato de la venta. No incluye el id de la cotización de Omint."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, model_validator

DocumentType = Literal["dni", "passport", "cuit"]


class SaleCreate(BaseModel):
    """El `quote_id` es el `id` que devolvió la cotización (`quotationId|productId`)."""

    company: str = Field(description="Slug o nombre de la compañía. En este corte, solo Omint.")
    quote_id: str = Field(min_length=3)


class SaleResponse(BaseModel):
    id: int
    company: str
    emission_status: str
    payment_status: str
    created_by_user_id: int
    agency_id: int | None
    seller_id: int | None
    origin: str | None
    destination_id: int | None
    trip_type: str | None
    days_range: int | None
    departure_date: date | None
    return_date: date | None
    quoted_ages: list[int]
    external_plan_id: str
    plan_name: str
    plan_id: int | None
    currency: str
    amount: Decimal
    net_amount: Decimal | None
    base_amount: Decimal | None
    markup: Decimal
    created_at: datetime


class EmergencyContactInput(BaseModel):
    first_name: str = Field(min_length=1, max_length=120)
    last_name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=1, max_length=40)
    email: EmailStr | None = None


class PassengerInput(BaseModel):
    is_holder: bool = False
    first_name: str = Field(min_length=1, max_length=120)
    last_name: str = Field(min_length=1, max_length=120)
    birth_date: date
    document_type: DocumentType
    document_number: str = Field(min_length=1, max_length=40)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    country_id: str | None = Field(default=None, max_length=80)
    province_id: str | None = Field(default=None, max_length=80)
    country: str | None = Field(default=None, max_length=120)
    province: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=120)
    street: str | None = Field(default=None, max_length=200)
    street_number: str | None = Field(default=None, max_length=20)


class PassengersCreate(BaseModel):
    """Un titular, el resto de los viajeros y un contacto de emergencia para toda la venta."""

    receive_promotions: bool = False
    emergency_contact: EmergencyContactInput
    passengers: list[PassengerInput] = Field(min_length=1)

    @model_validator(mode="after")
    def one_holder_and_unique_documents(self) -> "PassengersCreate":
        holders = [passenger for passenger in self.passengers if passenger.is_holder]
        if len(holders) != 1:
            raise ValueError("Tiene que haber un solo titular, y tiene que ser uno de los pasajeros")
        holder = holders[0]
        missing = [
            label
            for label, value in (
                ("email", holder.email),
                ("phone", holder.phone),
                ("country_id", holder.country_id),
                ("province_id", holder.province_id),
                ("street", holder.street),
                ("street_number", holder.street_number),
                ("city", holder.city),
            )
            if value is None or not str(value).strip()
        ]
        if missing:
            raise ValueError("Al titular le falta: " + ", ".join(missing))
        documents = [passenger.document_number.strip().lower() for passenger in self.passengers]
        if len(documents) != len(set(documents)):
            raise ValueError("Hay números de documento repetidos")
        return self


class EmergencyContactResponse(BaseModel):
    first_name: str | None
    last_name: str | None
    phone: str | None
    email: str | None


class PassengerResponse(BaseModel):
    id: int
    is_holder: bool
    first_name: str
    last_name: str
    birth_date: date
    document_type: str
    document_number: str
    email: str | None
    phone: str | None
    country: str | None
    province: str | None
    city: str | None
    street: str | None
    street_number: str | None
    country_id: str | None
    province_id: str | None


class FinancingOption(BaseModel):
    """Cuota de un medio de pago. El `id` es el que se manda al abrir el checkout."""

    id: str
    dues: int | None = None


class PaymentMethodOption(BaseModel):
    """Medio que devolvió Omint al cargar los pasajeros."""

    id: str
    code: str
    requires_financing: bool
    financing: list[FinancingOption]


class PassengersResponse(BaseModel):
    sale_id: int
    emission_status: str
    emergency_contact: EmergencyContactResponse
    passengers: list[PassengerResponse]
    payment_methods: list[PaymentMethodOption]


class SaleDetailResponse(SaleResponse):
    """Venta para retomar la pantalla: pasajeros, medios, checkout y voucher."""

    emergency_contact: EmergencyContactResponse
    passengers: list[PassengerResponse]
    payment_methods: list[PaymentMethodOption]
    checkout_url: str | None = None
    voucher_number: str | None = None
    voucher_url: str | None = None


class CatalogItem(BaseModel):
    id: str
    name: str


class CatalogResponse(BaseModel):
    items: list[CatalogItem]


class PaymentStart(BaseModel):
    """Medio elegido de los que devolvió AssignPassangers. Las URLs son del panel."""

    payment_method_id: str = Field(min_length=1, max_length=80)
    financing_id: str | None = Field(default=None, max_length=80)
    return_url_ok: str = Field(min_length=8, max_length=2000)
    return_url_error: str = Field(min_length=8, max_length=2000)
    return_url_pending: str = Field(min_length=8, max_length=2000)


class PaymentResponse(BaseModel):
    sale_id: int
    emission_status: str
    payment_status: str
    checkout_url: str | None
    created: bool


class PaymentConfirmResponse(BaseModel):
    sale_id: int
    emission_status: str
    payment_status: str
    payment_received: bool
    voucher_number: str | None
    voucher_url: str | None
