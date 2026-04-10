from pydantic import BaseModel, Field

from app.models.user import UserRole


class AdminPasswordUpdate(BaseModel):
    """Nueva contraseña (mismo mínimo que en altas de usuario)."""

    password: str = Field(..., min_length=8)


class AdminPasswordUpdateResponse(BaseModel):
    id: int
    email: str
    role: UserRole
    password: str
