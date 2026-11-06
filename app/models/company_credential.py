from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.base import TimestampMixin


class CompanyCredential(SQLModel, TimestampMixin, table=True):
    """Credencial de una agencia para la API de una compañía.

    El payload es JSON cifrado con Fernet. Si no hay fila, la cotización usa
    las credenciales genéricas de Settings.
    """

    __tablename__ = "company_credentials"
    __table_args__ = (
        UniqueConstraint(
            "agency_id",
            "company_id",
            name="uq_company_credentials_agency_company",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    agency_id: int = Field(foreign_key="agencies.id", index=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    payload_encrypted: str = Field(sa_column=Column(Text, nullable=False))
    active: bool = Field(default=True)
