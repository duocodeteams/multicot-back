"""Contrato de la venta. No incluye el id de la cotización de Omint."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

# Contrato del form. También se aceptan alias New Travel (PAS/CC/CE) y se normalizan.
DocumentType = Literal["dni", "passport", "cuit", "PAS", "CC", "CE", "pas", "cc", "ce"]
TripType = Literal["unico_viaje", "multiviaje", "larga_estadia"]
SimulatePaymentResult = Literal["pending", "approved", "rejected"]

_DOCUMENT_TYPE_ALIASES = {
    "dni": "dni",
    "passport": "passport",
    "cuit": "cuit",
    "pas": "passport",
    "cc": "dni",
    "ce": "cuit",
}


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


class UniversalQuoteSnapshot(BaseModel):
    """Viaje ya cotizado. `quote_id` de la venta es el IdLeadOut."""

    departure_date: date
    return_date: date | None = None
    ages: list[int] = Field(min_length=1, max_length=10)
    origin: str = Field(default="AR", min_length=2, max_length=16)
    destination_id: int
    trip_type: TripType
    days_range: int | None = None
    plan_name: str = Field(min_length=1, max_length=200)
    product_id: str | None = Field(default=None, max_length=80)
    amount_usd: Decimal = Field(gt=0)
    amount_local: Decimal | None = Field(default=None, gt=0)
    markup: Decimal = Field(default=Decimal("0"))


class TerrawindQuoteSnapshot(BaseModel):
    """Datos del viaje ya cotizado. SETW no guarda cotización remota."""

    departure_date: date
    return_date: date
    ages: list[int] = Field(min_length=1)
    origin: str = Field(default="AR", min_length=2, max_length=16)
    destination_id: int
    trip_type: TripType = "unico_viaje"
    days_range: int | None = None
    amount: Decimal = Field(gt=0)
    currency: str = Field(default="ARS", min_length=3, max_length=3)
    plan_name: str = Field(min_length=1, max_length=200)
    markup: Decimal = Field(default=Decimal("0"))
    net_amount: Decimal | None = None
    base_amount: Decimal | None = None
    # Destino SETW (country_id). Default 9999 = Múltiples Destinos.
    setw_country_id: str = Field(default="9999", min_length=1, max_length=20)


class NewTravelQuoteSnapshot(BaseModel):
    """Datos del viaje ya cotizado. New Travel no guarda cotización remota."""

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
    markup: Decimal = Field(default=Decimal("0"))
    net_amount: Decimal | None = None
    base_amount: Decimal | None = None


class SaleCreate(BaseModel):
    """Omint: `quote_id` = quotationId|productId. El resto manda el snapshot en `quote`."""

    company: str = Field(
        description="Slug: omint, inter_assist, universal, terrawind o new_travel.",
    )
    quote_id: str = Field(min_length=1)
    quote: (
        InterAssistQuoteSnapshot
        | UniversalQuoteSnapshot
        | TerrawindQuoteSnapshot
        | NewTravelQuoteSnapshot
        | None
    ) = None

    @model_validator(mode="after")
    def company_requires_its_snapshot(self) -> "SaleCreate":
        slug = self.company.strip().lower().replace(" ", "_")
        if slug in {"inter_assist", "interassist"} and not isinstance(self.quote, InterAssistQuoteSnapshot):
            raise ValueError("Inter Assist requiere el snapshot del viaje en `quote`")
        if slug == "universal" and not isinstance(self.quote, UniversalQuoteSnapshot):
            raise ValueError("Universal requiere el snapshot del viaje en `quote`")
        if slug == "terrawind" and self.quote is None:
            raise ValueError("Terrawind requiere el snapshot del viaje en `quote`")
        if slug in {"new_travel", "newtravel"} and self.quote is None:
            raise ValueError("New Travel requiere el snapshot del viaje en `quote`")
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


class SaleListResponse(BaseModel):
    """Listado del usuario del token. El admin ve todas. El detalle sigue en GET /v1/sales/{id}."""

    items: list[SaleResponse]
    total: int
    limit: int
    offset: int


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
    # Requerido para Terrawind (SETW). Omint / Inter Assist lo ignoran.
    gender: Literal["m", "f", "M", "F"] | None = None
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
    gender: str | None = None
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


class PaywayCardOption(BaseModel):
    """Tarjeta habilitada en Payway. El `id` se manda como `credit_card_id`."""

    id: str
    name: str


class PaymentMethodOption(BaseModel):
    """Medio de pago para el panel.

    Omint lo trae de AssignPassangers. Inter Assist ofrece Mercado Pago y Payway:
    en Payway `cards` son las tarjetas y `financing` las cuotas (1..max_installments).
    """

    id: str
    code: str
    name: str
    requires_financing: bool
    financing: list[FinancingOption]
    cards: list[PaywayCardOption] = Field(default_factory=list)
    max_installments: int | None = None


class PassengersResponse(BaseModel):
    sale_id: int
    emission_status: str
    emergency_contact: EmergencyContactResponse
    passengers: list[PassengerResponse]
    payment_methods: list[PaymentMethodOption]


class SaleVoucherItem(BaseModel):
    """Un voucher de Inter Assist (uno por pasajero). PDF: GET .../voucher/pdf?voucher_id=."""

    id: int
    number: str
    url: str
    first_name: str | None = None
    last_name: str | None = None
    document_number: str | None = None
    passenger_id: int | None = None


class SaleDetailResponse(SaleResponse):
    """Venta para retomar la pantalla: pasajeros, medios, checkout y voucher."""

    emergency_contact: EmergencyContactResponse
    passengers: list[PassengerResponse]
    payment_methods: list[PaymentMethodOption]
    checkout_url: str | None = None
    voucher_number: str | None = None
    voucher_url: str | None = None
    # Inter Assist: todos los vouchers de la venta (1 por pasajero).
    vouchers: list[SaleVoucherItem] = Field(default_factory=list)
    # Cobro real de la pasarela (Inter Assist: ARS). Distinto de `amount` de la cotización (USD).
    payment_amount: Decimal | None = None
    payment_currency: str | None = None
    exchange_rate: Decimal | None = None


class CatalogItem(BaseModel):
    id: str
    name: str


class CatalogResponse(BaseModel):
    items: list[CatalogItem]


class PaymentStart(BaseModel):
    """Omint: medio AssignPassangers. Terrawind: `nave`. Inter Assist: `mercadopago` o `payway`. New Travel: `new_travel`.

    Payway exige `credit_card_id` (tarjeta de `payment_methods[].cards`) y
    `financing_id` con la cantidad de cuotas.
    """

    payment_method_id: str = Field(default="simulated", min_length=1, max_length=80)
    financing_id: str | None = Field(default=None, max_length=80)
    credit_card_id: str | None = Field(default=None, max_length=80)
    return_url_ok: str = Field(default="", max_length=2000)
    return_url_error: str = Field(default="", max_length=2000)
    return_url_pending: str = Field(default="", max_length=2000)


class PaymentSimulate(BaseModel):
    """Mueve el pago simulado a pending, approved o rejected."""

    result: SimulatePaymentResult


class PaymentResponse(BaseModel):
    sale_id: int
    emission_status: str
    payment_status: str
    checkout_url: str | None
    created: bool
    # Inter Assist: monto cobrado en la pasarela (ARS). Omint/otros pueden omitirlo.
    amount: Decimal | None = None
    currency: str | None = None
    exchange_rate: Decimal | None = None


class PaymentConfirmResponse(BaseModel):
    sale_id: int
    emission_status: str
    payment_status: str
    payment_received: bool
    voucher_number: str | None
    voucher_url: str | None
    vouchers: list[SaleVoucherItem] = Field(default_factory=list)
