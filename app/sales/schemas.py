"""Contrato de la venta. No incluye el id de la cotización de Omint."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, model_validator

DocumentType = Literal["dni", "passport", "cuit"]
TripType = Literal["unico_viaje", "multiviaje", "larga_estadia"]
SimulatePaymentResult = Literal["pending", "approved", "rejected"]


class InterAssistQuoteSnapshot(BaseModel):
    """Datos del viaje ya cotizado. Inter Assist no guarda cotización remota."""

    departure_date: date
    return_date: date | None = None
    ages: list[int] = Field(min_length=1)
    origin: str = Field(default="AR", min_length=2, max_length=16)
    destination_id: int
    trip_type: TripType
    days_range: int | None = None
    amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    plan_name: str = Field(min_length=1, max_length=200)
    promocion_id: int = Field(default=0, ge=0)
    markup: Decimal = Field(default=Decimal("0"))
    net_amount: Decimal | None = None
    base_amount: Decimal | None = None


class SaleCreate(BaseModel):
    """Omint: `quote_id` = `quotationId|productId`. Inter Assist: `quote_id` = plan_id + `quote`."""

    company: str = Field(description="Slug de la compañía: omint o inter_assist.")
    quote_id: str = Field(min_length=1)
    quote: InterAssistQuoteSnapshot | None = None

    @model_validator(mode="after")
    def inter_assist_requires_quote(self) -> "SaleCreate":
        slug = self.company.strip().lower().replace(" ", "_")
        if slug in {"inter_assist", "interassist"} and self.quote is None:
            raise ValueError("Inter Assist requiere el snapshot del viaje en `quote`")
        return self


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
    """Omint: medio de AssignPassangers. Inter Assist (simulado): payment_method_id opcional."""

    payment_method_id: str = Field(default="simulated", min_length=1, max_length=80)
    financing_id: str | None = Field(default=None, max_length=80)
    return_url_ok: str = Field(min_length=8, max_length=2000)
    return_url_error: str = Field(min_length=8, max_length=2000)
    return_url_pending: str = Field(min_length=8, max_length=2000)


class PaymentSimulate(BaseModel):
    """Mueve el pago simulado a pending, approved o rejected."""

    result: SimulatePaymentResult


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
