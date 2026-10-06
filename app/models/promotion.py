from datetime import date

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel

from app.models.base import TimestampMixin
from app.models.company import Company
from app.models.plan import Plan


class Promotion(SQLModel, TimestampMixin, table=True):
    """Promo comercial informativa de una compañía, vigente entre dos fechas."""

    __tablename__ = "promotions"

    id: int | None = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    description: str = Field(max_length=500)
    starts_on: date
    ends_on: date

    company: Company = Relationship(back_populates="promotions")
    plan_links: list["PromotionPlan"] = Relationship(
        back_populates="promotion",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class PromotionPlan(SQLModel, TimestampMixin, table=True):
    """Planes de la compañía a los que aplica una promo."""

    __tablename__ = "promotion_plans"
    __table_args__ = (
        UniqueConstraint(
            "promotion_id",
            "plan_id",
            name="uq_promotion_plans_promotion_plan",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    promotion_id: int = Field(foreign_key="promotions.id", index=True)
    plan_id: int = Field(foreign_key="plans.id", index=True)

    promotion: Promotion = Relationship(back_populates="plan_links")
    plan: Plan = Relationship(back_populates="promotion_links")
