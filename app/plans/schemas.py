from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.quotations.schemas import DESTINO_IDS_VALIDOS


class PlanDestinationInput(BaseModel):
    destination_id: int
    enabled: bool

    @field_validator("destination_id")
    @classmethod
    def destination_id_valid(cls, value: int) -> int:
        if value not in DESTINO_IDS_VALIDOS:
            raise ValueError(f"destination_id debe ser 1-5, recibido: {value}")
        return value


class PlanDestinationResponse(BaseModel):
    destination_id: int
    enabled: bool

    model_config = {"from_attributes": True}


class PlanCreate(BaseModel):
    company_id: int
    external_plan_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    producer_markup: Decimal = Field(default=Decimal("0"), ge=0)
    organizer_markup: Decimal = Field(default=Decimal("0"), ge=0)
    operating_expenses: Decimal = Field(default=Decimal("0"), ge=0)

    @field_validator("external_plan_id", "name")
    @classmethod
    def strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("no puede estar vacío")
        return stripped


class PlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    producer_markup: Decimal | None = Field(default=None, ge=0)
    organizer_markup: Decimal | None = Field(default=None, ge=0)
    operating_expenses: Decimal | None = Field(default=None, ge=0)
    active: bool | None = None
    destinations: list[PlanDestinationInput] | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("no puede estar vacío")
        return stripped

    @model_validator(mode="after")
    def unique_destinations(self):
        if self.destinations is None:
            return self
        ids = [item.destination_id for item in self.destinations]
        if len(ids) != len(set(ids)):
            raise ValueError("destinations no puede repetir destination_id")
        return self


class PlanResponse(BaseModel):
    id: int
    company_id: int
    company_slug: str
    company_name: str
    external_plan_id: str
    name: str
    producer_markup: Decimal
    organizer_markup: Decimal
    operating_expenses: Decimal
    active: bool
    destinations: list[PlanDestinationResponse]


class PlanListResponse(BaseModel):
    items: list[PlanResponse]
    total: int
    limit: int
    offset: int
