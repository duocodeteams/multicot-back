from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.security import get_current_user
from app.models import User
from app.quotations.schemas import QuoteRequest, QuoteResponse
from app.services.quote_service import get_quotes

router = APIRouter()


@router.post("", response_model=QuoteResponse)
def create_quote(
    request: QuoteRequest,
    current_user: Annotated[User, Depends(get_current_user)],
) -> QuoteResponse:
    """
    Cotiza con todas las compañías (Pax, Cardinal, Terrawind, New Travel, Inter Assist).
    Requiere autenticación.
    """
    return get_quotes(request)
