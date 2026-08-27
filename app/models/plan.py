from decimal import Decimal

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel

from app.models.base import TimestampMixin
from app.models.company import Company


class Plan(SQLModel, TimestampMixin, table=True):
    """Plan comercial de una compañía (catálogo local)."""

    __tablename__ = "plans"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "external_plan_id",
            name="uq_plans_company_external_id",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    external_plan_id: str = Field(index=True)
    name: str = Field()
    producer_markup: Decimal = Field(default=Decimal("0"))
    organizer_markup: Decimal = Field(default=Decimal("0"))
    operating_expenses: Decimal = Field(default=Decimal("0"))
    active: bool = Field(default=True)

    company: Company = Relationship(back_populates="plans")
    destinations: list["PlanDestination"] = Relationship(back_populates="plan")

    @property
    def total_markup(self) -> Decimal:
        return self.producer_markup + self.organizer_markup + self.operating_expenses


class PlanDestination(SQLModel, TimestampMixin, table=True):
    """Disponibilidad de un plan en un destino del cotizador (1-5)."""

    __tablename__ = "plan_destinations"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "destination_id",
            name="uq_plan_destinations_plan_dest",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    plan_id: int = Field(foreign_key="plans.id", index=True)
    destination_id: int = Field(index=True)
    enabled: bool = Field(default=False)

    plan: Plan = Relationship(back_populates="destinations")
