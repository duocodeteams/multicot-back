from datetime import date
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field

from app.models.user import UserRole


class SellerUserCreate(BaseModel):
    """Datos del usuario de login del vendedor."""

    email: EmailStr
    password: str = Field(..., min_length=8)


class SellerCreate(BaseModel):
    """Request para crear vendedor con su usuario de login."""

    first_name: str
    last_name: str
    address: str
    nationality: str
    birth_date: date
    comments: str = ""
    commission: Decimal = Decimal("0")
    agency_id: int | None = None  # Solo ADMIN puede especificar; null = independiente
    user: SellerUserCreate


class SellerUpdate(BaseModel):
    """Campos editables. agency_id NO se puede modificar."""

    first_name: str | None = None
    last_name: str | None = None
    address: str | None = None
    nationality: str | None = None
    birth_date: date | None = None
    comments: str | None = None
    commission: Decimal | None = None


class SellerUserResponse(BaseModel):
    id: int
    email: str
    role: UserRole = UserRole.SELLER

    model_config = {"from_attributes": True}


class SellerResponse(BaseModel):
    id: int
    user_id: int
    agency_id: int | None
    first_name: str
    last_name: str
    address: str
    nationality: str
    birth_date: date
    comments: str
    commission: Decimal
    user: SellerUserResponse | None

    model_config = {"from_attributes": True}


class SellerListResponse(BaseModel):
    items: list[SellerResponse]
    total: int
    limit: int
    offset: int
