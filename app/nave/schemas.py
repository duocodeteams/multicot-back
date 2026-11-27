"""Request/response de Nave Checkout (link de pago y webhook)."""

from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class NaveBuyerAddress(BaseModel):
    street_1: str | None = None
    street_2: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None
    zipcode: str | None = None


class NaveBuyer(BaseModel):
    user_id: str | None = None
    session_id: str | None = None
    user_email: str | None = None
    name: str | None = None
    doc_type: str | None = None
    doc_number: str | None = None
    phone: str | None = None
    billing_address: NaveBuyerAddress | None = None


class NavePaymentLinkCreate(BaseModel):
    """Crea un link de checkout para enviar al pax. Moneda fija ARS."""

    amount: Decimal = Field(..., gt=0, decimal_places=2)
    product_name: str = Field(..., min_length=1, max_length=200)
    product_description: str | None = Field(default=None, max_length=500)
    external_payment_id: str | None = Field(default=None, min_length=1, max_length=36)
    buyer: NaveBuyer | None = None
    callback_url: str | None = Field(default=None, max_length=2000)
    duration_time: int | None = Field(default=None, gt=0)

    @field_validator("amount")
    @classmethod
    def _two_decimals(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))


class NavePaymentLinkResponse(BaseModel):
    external_payment_id: str
    payment_request_id: str
    checkout_url: str


class NaveWebhookPayload(BaseModel):
    payment_id: str
    payment_check_url: str
    external_payment_id: str


class NaveWebhookResponse(BaseModel):
    received: bool = True
    payment_id: str
    external_payment_id: str
    payment_status: str | None = None
