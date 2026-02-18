from datetime import date
from decimal import Decimal

from sqlmodel import Field, Relationship, SQLModel

from app.models.base import TimestampMixin


class Seller(SQLModel, TimestampMixin, table=True):
    __tablename__ = "sellers"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(unique=True, foreign_key="users.id", index=True)
    agency_id: int | None = Field(default=None, foreign_key="agencies.id")
    first_name: str = Field()
    last_name: str = Field()
    address: str = Field()
    nationality: str = Field()
    birth_date: date = Field()
    comments: str = Field()
    commission: Decimal = Field(default=Decimal("0"))  # Porcentaje, ej: 10.50
    active: bool = Field(default=True)

    user: "User" = Relationship(back_populates="seller")
    # Puede ser None si agency_id es null (vendedor independiente)
    agency: "Agency" = Relationship(back_populates="sellers")
