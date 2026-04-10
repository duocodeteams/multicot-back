import enum

from sqlmodel import Field, Relationship, SQLModel

from app.models.base import TimestampMixin


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    AGENCY = "agency"
    SELLER = "seller"


class User(SQLModel, TimestampMixin, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    password_hash: str = Field()
    # Misma contraseña cifrada (Fernet) para que el admin pueda verla; requiere PASSWORD_ENCRYPTION_KEY.
    password_encrypted: str | None = Field(default=None)
    role: UserRole = Field(index=True)
    active: bool = Field(default=True)
    agency_id: int | None = Field(default=None, foreign_key="agencies.id")  # Solo para role=AGENCY

    # La agencia que administra (solo cuando role=AGENCY). Puede ser None si agency_id es null.
    agency: "Agency" = Relationship(back_populates="users")
    # Perfil de vendedor (solo cuando role=SELLER). Puede ser None.
    seller: "Seller" = Relationship(back_populates="user")
