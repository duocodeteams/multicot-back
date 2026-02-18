from app.models import UserRole

from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserLoginData(BaseModel):
    id: int
    email: str
    role: UserRole
    agency_id: int | None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserLoginData
