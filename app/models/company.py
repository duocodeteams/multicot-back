from sqlmodel import Field, Relationship, SQLModel

from app.models.base import TimestampMixin


class Company(SQLModel, TimestampMixin, table=True):
    """Compañía de asistencia con adapter de cotización."""

    __tablename__ = "companies"

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(unique=True, index=True, max_length=50)
    name: str = Field()
    active: bool = Field(default=True)

    plans: list["Plan"] = Relationship(back_populates="company")
