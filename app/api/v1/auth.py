from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app.auth.schemas import LoginRequest, LoginResponse, UserLoginData
from app.auth.service import authenticate_user
from app.core.database import get_session
from app.core.security import create_access_token

router = APIRouter()


@router.post("/login", response_model=LoginResponse)
def login(
    request: LoginRequest,
    session: Annotated[Session, Depends(get_session)],
) -> LoginResponse:
    user = authenticate_user(session, request.email, request.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos",
        )
    token = create_access_token(user)
    return LoginResponse(
        access_token=token,
        user=UserLoginData(
            id=user.id,
            email=user.email,
            role=user.role,
            agency_id=user.agency_id,
        ),
    )
