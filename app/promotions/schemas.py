from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

PromotionStatus = Literal["current", "upcoming", "expired", "all"]
DESCRIPTION_MAX_LENGTH = 500


def _strip_description(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("no puede estar vacío")
    return stripped


def _unique_plan_ids(value: list[int]) -> list[int]:
    if len(value) != len(set(value)):
        raise ValueError("no puede repetir planes")
    return value


class PromotionPlanResponse(BaseModel):
    id: int
    name: str
    external_plan_id: str
    active: bool


class PromotionCreate(BaseModel):
    company_id: int
    description: str = Field(..., min_length=1, max_length=DESCRIPTION_MAX_LENGTH)
    starts_on: date
    ends_on: date
    plan_ids: list[int] = Field(..., min_length=1)

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str) -> str:
        return _strip_description(value)

    @field_validator("plan_ids")
    @classmethod
    def unique_plan_ids(cls, value: list[int]) -> list[int]:
        return _unique_plan_ids(value)

    @model_validator(mode="after")
    def ends_on_not_before_start(self):
        if self.ends_on < self.starts_on:
            raise ValueError("ends_on debe ser posterior o igual a starts_on")
        return self


class PromotionUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=1, max_length=DESCRIPTION_MAX_LENGTH)
    starts_on: date | None = None
    ends_on: date | None = None
    plan_ids: list[int] | None = Field(default=None, min_length=1)

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _strip_description(value)

    @field_validator("plan_ids")
    @classmethod
    def unique_plan_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return value
        return _unique_plan_ids(value)

    @model_validator(mode="after")
    def ends_on_not_before_start(self):
        if self.starts_on is not None and self.ends_on is not None and self.ends_on < self.starts_on:
            raise ValueError("ends_on debe ser posterior o igual a starts_on")
        return self


class PromotionResponse(BaseModel):
    id: int
    company_id: int
    company_slug: str
    company_name: str
    company_active: bool
    description: str
    starts_on: date
    ends_on: date
    plans: list[PromotionPlanResponse]


class PromotionListResponse(BaseModel):
    items: list[PromotionResponse]
    total: int
