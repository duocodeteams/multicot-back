from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app.core.database import get_session
from app.core.security import get_current_admin_user
from app.models import User
from app.users.schemas import AdminPasswordUpdate, AdminPasswordUpdateResponse
from app.users.service import update_user_password

router = APIRouter()


@router.patch("/{user_id}/password", response_model=AdminPasswordUpdateResponse)
def admin_update_user_password(
    user_id: int,
    data: AdminPasswordUpdate,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin_user)],
) -> AdminPasswordUpdateResponse:
    """
    Cambia la contraseña de cualquier usuario (agencia, vendedor u otro admin).
    Solo administradores.
    """
    user = update_user_password(session, user_id, data.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")
    return AdminPasswordUpdateResponse(
        id=user.id,
        email=user.email,
        role=user.role,
        password=data.password,
    )
