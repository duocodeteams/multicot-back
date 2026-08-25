from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.core.database import get_session
from app.core.security import get_current_user
from app.models import User
from app.quotations.schemas import QuoteRequest, QuoteResponse
from app.services.quote_service import get_quotes

router = APIRouter()


@router.post("", response_model=QuoteResponse)
def create_quote(
    request: QuoteRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_session)],
) -> QuoteResponse:
    """
    Cotiza con las compañías activas (Cardinal, Go, New Travel, Inter Assist, Universal, Omint, etc.).
    Aplica catálogo (whitelist + markup) si hay planes cargados.
    Requiere autenticación.
    """
    return get_quotes(session, request)
